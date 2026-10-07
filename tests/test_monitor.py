import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

import monitor
from collector import Collector, group_message_records, user_request

ROOT_SESSION = "fixture-root-session"
PROJECT_PATH = "/fixture/project"


class WorkflowExecutionChecks(unittest.TestCase):
    def setUp(self):
        self.collector = Collector.__new__(Collector)
        self.entries = [{"id": "root", "agent_id": "/root"}, {"id": "worker", "agent_id": "/root/review"}]
        self.node = {"id": "review", "agent_id": "/root/review", "status": "running",
                     "started_at": "2026-10-05T01:01:00Z", "finished_at": None, "evidence": []}
        self.collector.turns = {"root:one": self.turn("root", "one", "01:00:00", "01:10:00")}

    def turn(self, session, turn, start, end=None, root="one", status=None):
        return {"id": f"{session}:{turn}", "session_id": session, "turn_id": turn, "root_turn_id": root,
                "started_at": "2026-10-05T" + start + "Z", "finished_at": "2026-10-05T" + end + "Z" if end else None,
                "status": status or ("completed" if end else "running"), "detail": "Reviewed actual files",
                "evidence": ["Codex task_complete" if end else "Codex task_started"]}

    def project(self, node=None, extra=()):
        return self.collector.workflow_execution({"nodes": [node or self.node, *extra]}, self.entries, "root")["nodes"][0]

    def test_ended_worker_waits_for_confirmation_without_changing_manual_state(self):
        self.collector.turns["worker:review"] = self.turn("worker", "review", "01:01:01", "01:02:00")
        before = copy.deepcopy(self.node)
        node = self.project()
        self.assertEqual(node["status"], "awaiting_confirmation")
        self.assertEqual(node["recorded_status"], "running")
        self.assertEqual(node["execution_status"], "completed")
        self.assertEqual(node["execution_finished_at"], "2026-10-05T01:02:00Z")
        self.assertIn("worker:review", node["evidence"])
        self.assertEqual(self.node, before)
        # A server reconstruction derives the same binding without mutable cache.
        clone = Collector.__new__(Collector); clone.turns = copy.deepcopy(self.collector.turns)
        self.assertEqual(clone.workflow_execution({"nodes": [self.node]}, self.entries, "root")["nodes"][0], node)

    def test_worker_reuse_does_not_revive_previous_registered_stage(self):
        self.collector.turns["worker:review"] = self.turn("worker", "review", "01:01:01", "01:02:00")
        self.collector.turns["worker:again"] = self.turn("worker", "again", "01:03:00")
        self.assertEqual(self.project()["execution_request_id"], "worker:review")
        self.assertEqual(self.project()["status"], "awaiting_confirmation")

    def test_running_dispatch_and_existing_root_turn_are_observed(self):
        self.collector.turns["worker:review"] = self.turn("worker", "review", "01:01:01")
        self.assertEqual(self.project()["status"], "running")
        root_node = {**self.node, "agent_id": "/root"}
        self.assertEqual(self.project(root_node)["execution_request_id"], "root:one")
        self.assertEqual(self.project(root_node)["status"], "awaiting_confirmation")

    def test_no_start_or_ambiguous_assignment_is_not_claimed_running(self):
        self.assertEqual(self.project()["status"], "unconfirmed")
        self.assertEqual(self.project({**self.node, "agent_id": None})["status"], "unconfirmed")
        self.collector.turns["worker:a"] = self.turn("worker", "a", "01:01:01")
        self.collector.turns["worker:b"] = self.turn("worker", "b", "01:01:01")
        self.assertEqual(self.project()["status"], "unconfirmed")

    def test_next_root_request_cannot_claim_unstarted_old_stage(self):
        self.collector.turns["root:two"] = self.turn("root", "two", "02:00:00", root="two")
        self.collector.turns["worker:later"] = self.turn("worker", "later", "02:00:01", root="two")
        self.assertEqual(self.project()["status"], "unconfirmed")

    def test_next_registered_stage_bounds_dispatch_and_touching_end_is_excluded(self):
        self.collector.turns["worker:previous"] = self.turn("worker", "previous", "01:00:01", "01:01:00")
        self.collector.turns["worker:next"] = self.turn("worker", "next", "01:03:00")
        next_stage = {**self.node, "id": "next", "started_at": "2026-10-05T01:02:30Z"}
        self.assertEqual(self.project(extra=[next_stage])["status"], "unconfirmed")

    def test_failed_execution_requires_stage_confirmation_and_does_not_complete(self):
        self.collector.turns["worker:review"] = self.turn("worker", "review", "01:01:01", "01:02:00", status="failed")
        node = self.project()
        self.assertEqual(node["status"], "awaiting_confirmation")
        self.assertEqual(node["execution_status"], "failed")
        self.assertIn("실패·중단", node["detail"])

    def test_pending_and_completed_registered_nodes_keep_their_status(self):
        for status in ("pending", "completed", "blocked", "failed", "skipped"):
            self.assertEqual(self.project({**self.node, "status": status})["status"], status)

    def test_queued_new_stage_does_not_reuse_preceding_busy_worker_turn(self):
        previous = {**self.node, "id": "previous", "started_at": "2026-10-05T01:00:30Z"}
        self.collector.turns["worker:previous"] = self.turn("worker", "previous", "01:00:31", "01:02:00")
        self.collector.turns["worker:queued"] = self.turn("worker", "queued", "01:02:01")
        projected = self.collector.workflow_execution({"nodes": [previous, self.node]}, self.entries, "root")["nodes"]
        self.assertEqual(projected[0]["execution_request_id"], "worker:previous")
        self.assertEqual(projected[0]["status"], "awaiting_confirmation")
        self.assertEqual(projected[1]["execution_request_id"], "worker:queued")
        self.assertEqual(projected[1]["status"], "running")
        del self.collector.turns["worker:queued"]
        self.assertEqual(self.project(extra=[previous])["status"], "unconfirmed")

    def test_ambiguous_overlapping_root_turns_do_not_claim_worker_execution(self):
        self.collector.turns["root:other"] = self.turn("root", "other", "01:00:10", root="other")
        self.collector.turns["worker:old"] = self.turn("worker", "old", "01:00:31", "01:02:00")
        self.assertEqual(self.project()["status"], "unconfirmed")
        self.assertNotIn("execution_request_id", self.project())

    def test_observed_dispatch_excludes_worker_turn_started_before_assignment(self):
        self.collector.turns["worker:previous"] = self.turn("worker", "previous", "01:00:31", "01:02:00")
        self.collector.turns["worker:queued"] = self.turn("worker", "queued", "01:02:01")
        self.collector.dispatches = {"dispatch": {"target": "/root/review", "root_turn_id": "one", "at": "2026-10-05T01:01:01Z"}}
        self.assertEqual(self.project()["execution_request_id"], "worker:queued")

    def test_finished_old_stage_does_not_claim_unrelated_later_active_worker_turn(self):
        previous = {**self.node, "id": "previous", "status": "completed", "started_at": "2026-10-05T01:00:01Z",
                    "finished_at": "2026-10-05T01:00:30Z"}
        self.collector.turns["worker:current"] = self.turn("worker", "current", "01:00:59")
        self.assertEqual(self.project(extra=[previous])["execution_request_id"], "worker:current")


class WorkflowChecks(unittest.TestCase):
    def setUp(self):
        self.plan = {"run_id": "test", "title": "test", "nodes": [
            {"id": "build", "title": "build", "role": "dev"},
            {"id": "review", "title": "review", "role": "reviewer", "depends_on": ["build"]}]}
        self.events = [{"seq": 1, "at": "2026-10-05T00:00:00Z", "type": "workflow.created", "plan": self.plan}]

    def event(self, node, status, **kwargs):
        return {"seq": 2, "at": "2026-10-05T00:00:01Z", "type": "node.updated", "node_id": node, "status": status, **kwargs}

    def test_downstream_cannot_start_before_real_completion(self):
        with self.assertRaisesRegex(ValueError, "Dependencies not completed"):
            monitor.reduce_events(self.events + [self.event("review", "running")])

    def test_completion_requires_running_node_and_evidence(self):
        running = self.events + [self.event("build", "running")]
        with self.assertRaisesRegex(ValueError, "evidence"):
            monitor.reduce_events(running + [self.event("build", "completed")])
        completed = monitor.reduce_events(running + [self.event("build", "completed", evidence=["artifact.txt"])])
        self.assertEqual(completed["nodes"][0]["status"], "completed")
        self.assertIsNotNone(completed["nodes"][0]["finished_at"])

    def test_cycles_are_rejected(self):
        plan = copy.deepcopy(self.plan)
        plan["nodes"][0]["depends_on"] = ["review"]
        with self.assertRaisesRegex(ValueError, "cycle"):
            monitor.validate_plan(plan)

    def test_followup_workflow_can_extend_completed_nodes_without_rewriting_history(self):
        events = self.events + [self.event("build", "running"), self.event("build", "completed", evidence=["result"]),
            {"seq": 4, "at": "2026-10-05T01:00:00Z", "type": "workflow.extended", "nodes": [
                {"id": "followup", "title": "followup", "role": "dev", "depends_on": ["build"]}]},
            self.event("followup", "running")]
        state = monitor.reduce_events(events)
        self.assertEqual(state["nodes"][0]["status"], "completed")
        self.assertEqual(state["nodes"][-1]["status"], "running")
        with self.assertRaisesRegex(ValueError, "unique"):
            monitor.reduce_events(events + [{"type": "workflow.extended", "nodes": self.plan["nodes"]}])

    def test_crash_tail_is_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "events.jsonl"
            path.write_text(json.dumps(self.events[0]) + "\n" + '{"seq":2')
            before = path.read_bytes()
            with patch.object(monitor, "LOG", path):
                with self.assertRaisesRegex(ValueError, "Incomplete"):
                    monitor.append_event(self.event("build", "running"))
            self.assertEqual(path.read_bytes(), before)

    def test_sequence_cannot_be_overridden_by_publisher(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "events.jsonl"
            with patch.object(monitor, "LOG", path):
                monitor.append_event({**self.events[0], "seq": 99}, initialize=True)
                state = monitor.append_event(self.event("build", "running"))
            self.assertEqual(state["revision"], 2)

    def test_incoming_plaintext_message_is_captured(self):
        collector = Collector.__new__(Collector)
        collector.messages, collector.activity, collector.revision = {}, {}, 0
        collector.ingest({"id": "root", "agent_id": "/root", "cwd": "/p", "created_at": "2026-10-05T00:00:00Z"},
            {"timestamp": "2026-10-05T01:00:00Z", "type": "response_item", "payload": {
                "type": "agent_message", "id": "message1", "author": "/root/review", "recipient": "/root",
                "content": [{"type": "input_text", "text": "Message Type: MESSAGE\nPayload:\nactual full body"}]}})
        message = collector.messages["message1"]
        self.assertEqual(message["message"], "actual full body")
        self.assertEqual(message["from_agent"], "/root/review")


class CollectionChecks(unittest.TestCase):
    def setUp(self):
        self.collector = Collector.__new__(Collector)
        self.collector.messages = {}
        self.collector.activity = {}
        self.collector.revision = 0
        self.entry = {"id": "agent-session", "agent_id": "/root/reviewer", "cwd": "/project",
                      "created_at": "2026-10-05T01:00:00Z", "parent_id": "root-session"}

    def test_inherited_records_and_private_reasoning_are_excluded(self):
        for row in [
            {"timestamp": "2026-10-05T00:00:00Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "parent history"}]}},
            {"timestamp": "2026-10-05T02:00:00Z", "type": "response_item", "payload": {"type": "reasoning", "content": "private"}}]:
            self.collector.ingest(self.entry, row)
        self.assertFalse(self.collector.activity)
        self.assertFalse(self.collector.messages)

    def test_encrypted_message_is_a_placeholder_not_ciphertext(self):
        encrypted = "gAAAAA" + "a" * 100
        self.collector.ingest(self.entry, {"timestamp": "2026-10-05T02:00:00Z", "type": "response_item", "payload": {
            "type": "function_call", "id": "call1", "namespace": "collaboration", "name": "send_message",
            "arguments": json.dumps({"target": "/root", "message": encrypted})}})
        message = next(iter(self.collector.messages.values()))
        self.assertTrue(message["encrypted"])
        self.assertNotIn(encrypted, json.dumps(self.collector.messages))
        self.assertNotIn(encrypted, json.dumps(self.collector.activity))

    def test_nested_completion_targets_its_actual_parent_session(self):
        self.entry.update({"agent_id": "/root/worker/sub", "parent_id": "worker-session"})
        self.collector.ingest(self.entry, {"timestamp": "2026-10-05T02:00:00Z", "type": "event_msg",
            "payload": {"type": "task_complete", "last_agent_message": "complete"}})
        message = next(iter(self.collector.messages.values()))
        self.assertEqual(message["to_agent"], "worker-session")

    def test_encrypted_send_and_receipt_do_not_overwrite_original_ids(self):
        encrypted = "gAAAAA" + "a" * 100
        self.collector.ingest(self.entry, {"timestamp": "2026-10-05T02:00:00Z", "type": "response_item", "payload": {
            "type": "function_call", "call_id": "call1", "namespace": "collaboration", "name": "send_message",
            "arguments": json.dumps({"target": "/root", "message": encrypted})}})
        root = {"id": "root-session", "agent_id": "/root", "cwd": "/project", "created_at": self.entry["created_at"]}
        self.collector.ingest(root, {"timestamp": "2026-10-05T02:00:01Z", "type": "response_item", "payload": {
            "type": "agent_message", "id": "received1", "author": "/root/reviewer", "recipient": "/root",
            "content": [{"type": "encrypted_content", "encrypted_content": encrypted}]}})
        self.assertEqual(set(self.collector.messages), {"call1", "received1"})
        self.assertEqual(self.collector.messages["call1"]["delivery_key"], self.collector.messages["received1"]["delivery_key"])
        self.assertNotIn(encrypted, json.dumps(self.collector.messages))


class MessageGroupingChecks(unittest.TestCase):
    def setUp(self):
        self.turns = {"child:one": {"session_id": "child", "turn_id": "one", "root_turn_id": "request",
                                   "started_at": "2026-10-05T01:00:00Z"},
                      "root:request": {"session_id": "root", "turn_id": "request", "root_turn_id": "request",
                                       "started_at": "2026-10-05T00:50:00Z"}}
        common = {"type": "result", "from_agent": "/root/review", "to_agent": "/root",
                  "message": "Final report", "encrypted": False}
        self.journal = {**common, "id": "journal:49", "at": "2026-10-05T01:00:04.446+00:00",
                        "source": "sender_journal", "record_kind": "sender_journal", "session_id": "child",
                        "sender_session_id": "child", "sender_turn_id": "one"}
        self.complete = {**common, "id": "complete1", "at": "2026-10-05T01:00:12.044Z",
                         "source": "codex_session", "record_kind": "task_complete", "session_id": "child",
                         "sender_session_id": "child", "sender_turn_id": "one", "recipient_session_id": "root"}
        self.received = {**common, "id": "received1", "at": "2026-10-05T01:00:18.014Z",
                         "source": "codex_session", "record_kind": "agent_message", "session_id": "root",
                         "recipient_turn_id": "request"}

    def group(self, *records):
        return group_message_records(list(records), self.turns)

    def test_three_final_report_sources_have_one_card_and_all_original_ids(self):
        originals = [self.journal, self.complete, self.received]
        before = copy.deepcopy(originals)
        cards = self.group(*originals)
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["record_count"], 3)
        self.assertEqual([r["id"] for r in cards[0]["records"]], ["journal:49", "complete1", "received1"])
        self.assertEqual(originals, before)

    def test_card_identity_and_timestamp_survive_incremental_source_arrivals(self):
        first = self.group(self.journal)[0]
        second = self.group(self.journal, self.complete)[0]
        third = self.group(self.journal, self.complete, self.received)[0]
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["id"], third["id"])
        self.assertEqual(first["at"], third["at"])

    def citation(self, note="Original memory source"):
        return ("<oai-mem-citation>\n<citation_entries>\n" + note +
                "\n</citation_entries>\n<rollout_ids>\n</rollout_ids>\n</oai-mem-citation>")

    def test_final_citation_metadata_difference_keeps_one_card_and_original_bodies(self):
        final = dict(self.complete, id="final1", record_kind="assistant_final_answer",
                     at=self.journal["at"], message="Final report\n\n" + self.citation())
        complete = dict(self.complete, message="Final report\n\n")
        originals = [final, complete]
        before = copy.deepcopy(originals)
        early = self.group(final)[0]
        cards = self.group(*originals)
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["message"], "Final report")
        self.assertEqual(cards[0]["id"], early["id"])
        self.assertEqual(cards[0]["record_count"], 2)
        self.assertEqual(cards[0]["records"], originals)
        self.assertEqual(originals, before)

    def test_citation_normalization_does_not_merge_same_source_resends(self):
        first = dict(self.complete, id="final1", record_kind="assistant_final_answer",
                     message="Final report\n\n" + self.citation("First source"))
        repeated = dict(first, id="final2", message="Final report\n\n" + self.citation("Second source"))
        cards = self.group(first, repeated, self.complete)
        self.assertEqual(len(cards), 3)
        self.assertEqual({r["id"] for c in cards for r in c["records"]}, {"final1", "final2", "complete1"})

    def test_citation_normalization_keeps_identical_reports_in_distinct_turns(self):
        first = dict(self.complete, id="final1", record_kind="assistant_final_answer",
                     message="Final report\n\n" + self.citation())
        second = dict(first, id="final2", sender_turn_id="two")
        second_complete = dict(self.complete, id="complete2", sender_turn_id="two")
        cards = self.group(first, self.complete, second, second_complete)
        self.assertEqual(len(cards), 2)
        self.assertEqual({c["sender_turn_id"] for c in cards}, {"one", "two"})
        self.assertTrue(all(c["record_count"] == 2 for c in cards))

    def test_citation_normalization_never_hides_substantive_body_changes(self):
        final = dict(self.complete, id="final1", record_kind="assistant_final_answer",
                     message="Final report with another finding\n\n" + self.citation())
        self.assertEqual(len(self.group(final, self.complete)), 2)

    def test_literal_incomplete_or_nontrailing_citation_content_is_preserved(self):
        block = self.citation()
        for text in ("Final report\n\n```xml\n" + block,
                     "Final report\n\n~~~~xml\n" + block,
                     "Final report\n\n" + block + "\nAdditional finding",
                     "Final report\n\n" + block.replace("</oai-mem-citation>", ""),
                     "Final report\n\n    " + block.replace("\n", "\n    "),
                     "Final report\n\n> " + block.replace("\n", "\n> ")):
            with self.subTest(text=text):
                final = dict(self.complete, id="final1", record_kind="assistant_final_answer", message=text)
                cards = self.group(final, self.complete)
                self.assertEqual(len(cards), 2)
                self.assertEqual(next(c for c in cards if c["record_kind"] == "assistant_final_answer")["message"], text.rstrip())

    def test_metadata_normalization_does_not_rewrite_or_merge_explicit_sends(self):
        text = "Final report\n\n" + self.citation()
        journal = dict(self.journal, message=text)
        sent = dict(self.complete, id="call1", type="message", record_kind="collaboration_call", message=text)
        cards = self.group(journal, sent, self.complete, self.received)
        self.assertEqual(len(cards), 3)
        self.assertEqual(next(c for c in cards if c["id"] == "call1")["message"], text)
        self.assertEqual(next(c for c in cards if c["id"] == journal["id"])["message"], text)
        self.assertEqual(sum(c["record_count"] for c in cards), 4)
        cards = self.group(journal, dict(sent, message="Final report"), self.complete, self.received)
        self.assertEqual(len(cards), 3)
        self.assertEqual(next(c for c in cards if c["id"] == journal["id"])["message"], text)

    def test_only_last_citation_block_is_removed_without_swallowing_intervening_text(self):
        visible = "Final report\n\n" + self.citation() + "\n\nAdditional finding"
        final = dict(self.complete, id="final1", record_kind="assistant_final_answer",
                     message=visible + "\n\n" + self.citation("Actual trailing metadata"))
        cards = self.group(final, dict(self.complete, message=visible))
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["message"], visible)

    def test_closed_fenced_citation_example_is_kept_before_real_metadata_suffix(self):
        for fence in ("```", "~~~~"):
            with self.subTest(fence=fence):
                visible = "Final report\n\n" + fence + "xml\n" + self.citation() + "\n" + fence
                final = dict(self.complete, id="final1", record_kind="assistant_final_answer",
                             message=visible + "\n\n" + self.citation())
                cards = self.group(final, dict(self.complete, message=visible))
                self.assertEqual(len(cards), 1)
                self.assertEqual(cards[0]["message"], visible)

    def test_same_report_in_two_actual_turns_stays_two_deliveries(self):
        self.turns["child:two"] = {"session_id": "child", "turn_id": "two", "root_turn_id": "request",
                                   "started_at": "2026-10-05T01:01:00Z"}
        second = [dict(r, id="second:" + r["id"], at=r["at"].replace("01:00", "01:01"),
                       **({"sender_turn_id": "two"} if r.get("sender_turn_id") else {}))
                  for r in (self.journal, self.complete, self.received)]
        cards = self.group(self.journal, self.complete, self.received, *second)
        self.assertEqual(len(cards), 2)
        self.assertEqual([r["record_count"] for r in cards], [3, 3])

    def test_repeated_received_final_answers_are_not_silently_removed(self):
        repeated = dict(self.received, id="received2", at="2026-10-05T01:00:19Z")
        cards = self.group(self.journal, self.complete, self.received, repeated)
        self.assertEqual(len(cards), 3)
        self.assertEqual(sum(c["record_count"] for c in cards), 4)
        self.assertIn("received2", [c["id"] for c in cards])

    def test_multiple_journals_in_same_turn_are_ambiguous_and_preserved(self):
        repeated = dict(self.journal, id="journal:50", at="2026-10-05T01:00:05Z")
        cards = self.group(self.journal, repeated, self.complete, self.received)
        self.assertEqual(len(cards), 4)

    def test_message_body_alone_never_merges_actual_resends(self):
        sent = dict(self.journal, type="message")
        repeat = dict(sent, id="journal:50")
        cards = self.group(sent, repeat)
        self.assertEqual(len(cards), 2)

    def test_explicit_send_is_distinct_from_automatic_final_report(self):
        sent = dict(self.complete, type="message", id="call1", record_kind="collaboration_call",
                    at="2026-10-05T01:00:05Z")
        cards = self.group(self.journal, sent, self.complete, self.received)
        self.assertEqual(len(cards), 3)
        self.assertIn("journal:49", [r["id"] for r in cards])
        self.assertEqual(sum(r["record_count"] for r in cards), 4)

    def test_direction_recipient_session_and_root_request_must_match(self):
        self.turns["root:other"] = {"session_id": "root", "turn_id": "other", "root_turn_id": "other",
                                     "started_at": "2026-10-05T00:50:00Z"}
        for changes in ({"to_agent": "/root/other"}, {"session_id": "other-root"},
                        {"recipient_turn_id": "other"}, {"at": "2026-10-05T01:03:00Z"}):
            with self.subTest(changes=changes):
                self.assertEqual(len(self.group(self.complete, dict(self.received, **changes))), 2)

    def test_unobserved_sender_turn_and_redacted_bodies_are_not_guessed(self):
        untracked = dict(self.journal, sender_turn_id=None)
        self.assertEqual(len(self.group(untracked, self.complete, self.received)), 2)
        hidden = [dict(r, message="[키 숨김]") for r in (self.journal, self.complete, self.received)]
        self.assertEqual(len(self.group(*hidden)), 3)

    def test_encrypted_delivery_token_preserves_both_source_record_ids(self):
        outgoing = dict(self.complete, type="assignment", id="call1", record_kind="collaboration_call",
                        encrypted=True, delivery_key="opaque1", message="암호화됨")
        incoming = dict(self.received, type="message", encrypted=True, delivery_key="opaque1", message="암호화됨")
        cards = self.group(outgoing, incoming)
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["type"], "assignment")
        self.assertEqual([r["id"] for r in cards[0]["records"]], ["call1", "received1"])
        self.assertEqual(len(self.group(outgoing, dict(incoming, delivery_key="opaque2"))), 2)
        self.assertEqual(len(self.group(outgoing, dict(outgoing, id="call2"), incoming)), 3)


class AgentRoleChecks(unittest.TestCase):
    def setUp(self):
        self.collector = Collector.__new__(Collector)
        self.entry = {"id": "child", "agent_id": "/root/ui", "nickname": "Galileo"}
        self.turn = {"started_at": "2026-10-05T01:00:00Z", "finished_at": "2026-10-05T01:10:00Z"}
        self.frontend = {"id": "ui", "role": "Frontend Developer", "agent_id": "/root/ui",
                         "started_at": "2026-10-05T01:01:00Z", "finished_at": "2026-10-05T01:09:00Z"}

    def role(self, nodes=(), entry=None, turn=None):
        return self.collector.task_role(entry or self.entry, turn or self.turn, "root", nodes)

    def test_name_and_assigned_workflow_role_are_independent(self):
        resolved = self.role([self.frontend])
        self.assertEqual(resolved["role"], "Frontend Developer")
        self.assertEqual(resolved["role_node_ids"], ["ui"])
        self.assertNotEqual(resolved["role"], self.entry["nickname"])

    def test_reused_agent_keeps_the_role_of_each_turn(self):
        review = dict(self.frontend, id="review", role="Reviewer", started_at="2026-10-05T02:00:00Z",
                      finished_at="2026-10-05T02:10:00Z")
        self.assertEqual(self.role([self.frontend, review])["role"], "Frontend Developer")
        self.assertEqual(self.role([self.frontend, review], turn={"started_at": "2026-10-05T02:01:00Z",
                         "finished_at": "2026-10-05T02:09:00Z"})["role"], "Reviewer")

    def test_touching_assignment_endpoints_are_separate_roles(self):
        frontend = dict(self.frontend, finished_at="2026-10-05T01:10:00Z")
        review = dict(self.frontend, id="review", role="Reviewer", started_at="2026-10-05T01:10:00Z",
                      finished_at="2026-10-05T01:20:00Z")
        self.assertEqual(self.role([frontend, review])["role"], "Frontend Developer")
        self.assertEqual(self.role([frontend, review], turn={"started_at": "2026-10-05T01:10:00Z",
                         "finished_at": "2026-10-05T01:20:00Z"})["role"], "Reviewer")

    def test_unknown_or_conflicting_roles_never_fall_back_to_nickname(self):
        self.assertIsNone(self.role()["role"])
        conflict = dict(self.frontend, id="review", role="Reviewer")
        self.assertIsNone(self.role([self.frontend, conflict])["role"])

    def test_explicit_session_role_and_actual_coordinator_are_preserved(self):
        self.assertEqual(self.role([self.frontend], entry={**self.entry, "assigned_role": "QA Engineer"})["role"], "QA Engineer")
        self.assertEqual(self.role([self.frontend], entry={**self.entry, "id": "root", "nickname": "A name"})["role"], "Coordinator")

    def test_session_id_binding_and_consistent_early_registration_are_supported(self):
        by_session = dict(self.frontend, agent_id="child")
        early = {"started_at": "2026-10-05T00:00:00Z", "finished_at": "2026-10-05T00:01:00Z"}
        self.assertEqual(self.role([by_session], turn=early)["role"], "Frontend Developer")
        self.assertIsNone(self.role([dict(self.frontend, agent_id="/root/somebody-else")])["role"])


class LiveRequestChecks(unittest.TestCase):
    def setUp(self):
        self.collector = Collector.__new__(Collector)
        self.collector.messages, self.collector.activity, self.collector.turns = {}, {}, {}
        self.collector.revision = 0
        self.collector.lock = threading.RLock()
        self.collector.root_session, self.collector.project = ROOT_SESSION, PROJECT_PATH
        self.collector.error, self.collector.last_success_at = None, "2026-10-05T02:00:00Z"
        self.entry = {"id": ROOT_SESSION, "agent_id": "/root", "cwd": PROJECT_PATH,
                      "created_at": "2026-10-05T00:00:00Z", "parent_id": None,
                      "nickname": "Coordinator", "status": "unknown", "last_activity_at": "2026-10-05T00:00:00Z"}
        self.collector.files = {"root": self.entry}
        self.ordinal = 0

    def ingest(self, kind, at="2026-10-05T01:00:00Z", row_type="event_msg", **payload):
        self.ordinal += 1
        self.collector.ingest(self.entry, {"type": row_type, "timestamp": at, "ordinal": self.ordinal,
                                          "payload": {"type": kind, **payload}})

    def state(self, events=None):
        return self.collector.augment({"revision": 1, "title": "old plan", "run_id": "old",
            "started_at": "2026-10-05T00:00:00Z", "updated_at": "2026-10-05T00:30:00Z",
            "binding": {"project_path": PROJECT_PATH, "root_session_id": ROOT_SESSION},
            "nodes": [{"id": "old", "status": "completed"}], "events": events or []})

    def test_journal_turn_resolution_and_raw_api_records_are_preserved(self):
        self.ingest("task_started", turn_id="request", root_turn_id="request")
        child = {**self.entry, "id": "child", "agent_id": "/root/review", "parent_id": ROOT_SESSION,
                 "nickname": "Euler", "current_turn_id": None, "active_tools": {}, "call_turns": {}}
        self.collector.files["child"] = child
        for ordinal, at, kind, data in (
                (1, "2026-10-05T01:00:01Z", "task_started", {"turn_id": "one", "root_turn_id": "request"}),
                (2, "2026-10-05T01:00:12Z", "task_complete", {"turn_id": "one", "last_agent_message": "Final report"})):
            self.collector.ingest(child, {"type": "event_msg", "timestamp": at, "ordinal": ordinal,
                                          "payload": {"type": kind, **data}})
        self.ingest("agent_message", at="2026-10-05T01:00:18Z", row_type="response_item", id="received1",
                    author="/root/review", recipient="/root", internal_chat_message_metadata_passthrough={"turn_id": "request"},
                    content=[{"type": "input_text", "text": "Message Type: FINAL_ANSWER\nPayload:\nFinal report"}])
        event = {"seq": 49, "at": "2026-10-05T01:00:04.446+00:00", "type": "agent.message",
                 "session_id": "child", "message_kind": "result", "from_agent": "/root/review", "to_agent": "/root",
                 "message": "Final report"}
        state = self.state([event])
        self.assertEqual(len(state["messages"]), 1)
        self.assertEqual(len(state["message_records"]), 3)
        self.assertEqual(state["message_records"][0]["sender_turn_id"], "one")
        self.assertEqual(state["messages"][0]["record_count"], 3)
        self.assertIn(event, state["events"])
        final_rows = [e for e in state["execution_events"] if e["type"] in {"agent.message", "task_complete"}]
        self.assertEqual(len(final_rows), 1)
        self.assertEqual(final_rows[0]["type"], "task_complete")
        self.assertEqual(final_rows[0]["source_event_count"], 2)
        self.assertIn(event, final_rows[0]["source_events"])
        self.assertEqual([e["type"] for e in state["events"] if e.get("runtime")],
                         ["task_started", "task_started", "task_complete"])
        # A coordinator's copy of a child's report identifies the writer and
        # sender separately; the declared sender still has an observed turn.
        state = self.state([dict(event, session_id=ROOT_SESSION)])
        self.assertEqual(len(state["messages"]), 1)
        self.assertEqual(state["message_records"][0]["session_id"], ROOT_SESSION)
        self.assertEqual(state["message_records"][0]["sender_session_id"], "child")
        self.assertEqual(state["message_records"][0]["recorder_session_id"], ROOT_SESSION)
        self.assertEqual(state["messages"][0]["record_count"], 3)

    def test_sender_journal_resolution_displays_body_without_altering_raw_api_or_event(self):
        self.ingest("task_started", turn_id="request", root_turn_id="request")
        child = {**self.entry, "id": "child", "agent_id": "/root/review", "parent_id": ROOT_SESSION,
                 "nickname": "Euler", "current_turn_id": None}
        self.collector.files["child"] = child
        encrypted = "gAAAAA" + "a" * 100
        self.ingest("function_call", at="2026-10-05T01:00:07Z", row_type="response_item",
                    id="call", namespace="collaboration", name="send_message",
                    arguments=json.dumps({"target": "child", "message": encrypted}))
        self.collector.ingest(child, {"type": "response_item", "timestamp": "2026-10-05T01:00:08Z",
            "payload": {"type": "agent_message", "id": "receipt", "author": ROOT_SESSION, "recipient": "child",
                        "content": [{"type": "input_text", "text": "Message Type: MESSAGE\nPayload:\n"},
                                    {"type": "encrypted_content", "encrypted_content": encrypted}]}})
        event = {"seq": 48, "at": "2026-10-05T01:00:02+00:00", "type": "agent.message",
                 "session_id": ROOT_SESSION, "project_path": PROJECT_PATH, "message_kind": "message",
                 "from_agent": "/root", "to_agent": "child", "message": "Full public body\n검증 결과"}
        before = copy.deepcopy(event)
        state = self.state([event])
        self.assertEqual(len(state["messages"]), 1)
        self.assertEqual(state["messages"][0]["message"], event["message"])
        self.assertEqual(state["messages"][0]["message_source_record_id"], "journal:48")
        self.assertEqual(state["collection"]["encrypted_messages"], 0)
        self.assertEqual(state["collection"]["journal_backed_messages"], 1)
        self.assertEqual(state["collection"]["encrypted_records"], 2)
        self.assertEqual(len(state["message_records"]), 3)
        self.assertEqual(sum(r["encrypted"] for r in state["message_records"]), 2)
        self.assertNotIn(encrypted, json.dumps(state))
        self.assertEqual(event, before)
        self.assertIn(before, state["events"])

    def test_root_final_report_and_completion_have_one_user_card_with_full_sources(self):
        body = "최종 완료 보고\n" + "전체 원문과 검증 결과를 보존합니다. " * 60
        self.ingest("task_started", turn_id="request", root_turn_id="request")
        self.ingest("message", at="2026-10-05T01:00:01Z", row_type="response_item", id="final",
                    role="assistant", phase="final_answer", content=[{"type": "output_text", "text": body}],
                    internal_chat_message_metadata_passthrough={"turn_id": "request"})
        before = self.state()
        self.assertEqual(len(before["messages"]), 1)
        self.assertEqual(before["live"]["running_agents"], 1)
        self.assertNotIn("task_complete", [e["type"] for e in before["events"]])
        self.ingest("task_complete", at="2026-10-05T01:00:02Z", turn_id="request", last_agent_message=body)
        state = self.state()
        self.assertEqual(len(state["messages"]), 1)
        card = state["messages"][0]
        self.assertEqual(card["id"], before["messages"][0]["id"])
        self.assertEqual((card["from_agent"], card["to_agent"], card["recipient_kind"]), ("/root", "user", "user"))
        self.assertEqual(card["message"], body)
        self.assertEqual(card["record_count"], 2)
        self.assertEqual({r["record_kind"] for r in card["records"]}, {"assistant_final_answer", "task_complete"})
        self.assertTrue(all(r["sender_session_id"] == ROOT_SESSION and r["sender_turn_id"] == "request" for r in card["records"]))
        event = next(e for e in state["events"] if e["type"] == "task_complete")
        self.assertEqual(event["message"], body)
        self.assertEqual((event["agent_id"], event["session_id"], event["turn_id"], event["source"]),
                         ("/root", ROOT_SESSION, "request", "codex_session"))
        self.assertEqual(state["live"]["running_agents"], 0)

    def test_repeated_root_report_in_distinct_turns_remains_two_user_reports(self):
        for number in (1, 2):
            turn = f"request{number}"
            prefix = f"2026-10-05T0{number}:00:"
            self.ingest("task_started", at=prefix+"00Z", turn_id=turn, root_turn_id=turn)
            self.ingest("message", at=prefix+"01Z", row_type="response_item", id=f"final{number}",
                        role="assistant", phase="final_answer", content=[{"type": "output_text", "text": "Same report"}])
            self.ingest("task_complete", at=prefix+"02Z", turn_id=turn, last_agent_message="Same report")
        state = self.state()
        self.assertEqual(len(state["messages"]), 2)
        self.assertEqual([m["record_count"] for m in state["messages"]], [2, 2])
        self.assertEqual({m["sender_turn_id"] for m in state["messages"]}, {"request1", "request2"})
        self.assertEqual(len([e for e in state["events"] if e["type"] == "task_complete"]), 2)

    def test_actual_root_final_with_citation_and_completion_without_it_stay_one_report(self):
        body = "수정했습니다.\n\n검증 결과를 확인했습니다."
        final_body = body + "\n\n" + MessageGroupingChecks().citation()
        self.ingest("task_started", turn_id="request", root_turn_id="request")
        self.ingest("message", at="2026-10-05T01:00:01Z", row_type="response_item", id="final",
                    role="assistant", phase="final_answer", content=[{"type": "output_text", "text": final_body}])
        before = self.state()["messages"][0]
        self.ingest("task_complete", at="2026-10-05T01:00:02Z", turn_id="request", last_agent_message=body+"\n\n")
        state = self.state()
        self.assertEqual(len(state["messages"]), 1)
        card = state["messages"][0]
        self.assertEqual(card["message"], body)
        self.assertEqual(card["id"], before["id"])
        self.assertEqual(card["record_count"], 2)
        self.assertEqual([r["message"] for r in card["records"]], [final_body, body+"\n\n"])
        self.assertEqual(next(e for e in state["events"] if e["type"] == "task_complete")["message"], body+"\n\n")
        self.assertEqual(state["live"]["running_agents"], 0)

    def test_root_completion_only_report_is_visible_and_empty_completion_is_not_a_message(self):
        self.ingest("task_started", turn_id="one", root_turn_id="one")
        self.ingest("task_complete", at="2026-10-05T01:00:01Z", turn_id="one", last_agent_message="Legacy final")
        self.ingest("task_started", at="2026-10-05T02:00:00Z", turn_id="two", root_turn_id="two")
        self.ingest("task_complete", at="2026-10-05T02:00:01Z", turn_id="two")
        state = self.state()
        self.assertEqual([m["message"] for m in state["messages"]], ["Legacy final"])
        self.assertEqual(state["messages"][0]["to_agent"], "user")
        self.assertEqual(len([e for e in state["events"] if e["type"] == "task_complete"]), 2)

    def test_public_progress_is_visible_but_private_phases_and_encrypted_content_are_not(self):
        self.ingest("task_started", turn_id="request", root_turn_id="request")
        for phase in ("analysis", "summary", "commentary"):
            self.ingest("message", row_type="response_item", role="assistant", phase=phase,
                        content=[{"type": "output_text", "text": phase}])
        self.ingest("message", row_type="response_item", role="assistant", phase="final_answer",
                    content=[{"type": "encrypted_content", "encrypted_content": "gAAAAA"+"a"*100}])
        state = self.state()
        self.assertEqual([(m["type"], m["message"]) for m in state["messages"]], [("progress", "commentary")])
        self.assertEqual([e["type"] for e in state["events"]], ["progress", "task_started"])
        self.assertEqual(len([e for e in state["execution_events"] if e["type"] == "progress"]), 1)
        self.assertEqual(state["live"]["running_agents"], 1)
        self.assertFalse(any(a["detail"] in {"analysis", "summary"} for a in state["activity"]))

    def test_delayed_final_uses_its_metadata_turn_without_overwriting_current_progress(self):
        self.ingest("task_started", turn_id="old", root_turn_id="old")
        self.ingest("task_complete", at="2026-10-05T01:00:01Z", turn_id="old", last_agent_message="Old final")
        self.ingest("task_started", at="2026-10-05T02:00:00Z", turn_id="new", root_turn_id="new")
        self.ingest("message", at="2026-10-05T02:00:01Z", row_type="response_item", role="assistant",
                    phase="commentary", content=[{"type": "output_text", "text": "New progress"}])
        self.ingest("message", at="2026-10-05T02:00:02Z", row_type="response_item", role="assistant",
                    phase="final_answer", id="delayed-final", internal_chat_message_metadata_passthrough={"turn_id": "old"},
                    content=[{"type": "output_text", "text": "Old final"}])
        state = self.state()
        self.assertEqual(len([m for m in state["messages"] if m["type"] == "result"]), 1)
        self.assertEqual(next(m for m in state["messages"] if m["type"] == "result")["sender_turn_id"], "old")
        self.assertEqual(next(m for m in state["messages"] if m["type"] == "progress")["sender_turn_id"], "new")
        self.assertEqual(state["messages"][0]["record_count"], 2)
        self.assertEqual(state["live"]["agents"][0]["latest_activity"]["detail"], "New progress")
        self.assertEqual(state["nodes"][-1]["status"], "running")

    def test_other_selected_root_has_its_own_reports_and_runtime_events_without_current_journal(self):
        self.ingest("task_started", turn_id="current", root_turn_id="current")
        self.ingest("task_complete", at="2026-10-05T01:00:01Z", turn_id="current", last_agent_message="Current final")
        other = {**self.entry, "id": "other-root", "cwd": "/other/project", "current_turn_id": None,
                 "active_tools": {}, "call_turns": {}}
        self.collector.files["other"] = other
        for ordinal, kind, data in ((1, "task_started", {"turn_id": "other", "root_turn_id": "other"}),
                                   (2, "task_complete", {"turn_id": "other", "last_agent_message": "Other final"})):
            self.collector.ingest(other, {"type": "event_msg", "timestamp": f"2026-10-05T03:00:0{ordinal}Z",
                                          "ordinal": ordinal, "payload": {"type": kind, **data}})
        self.collector.root_session, self.collector.project = "other-root", "/other/project"
        state = self.state([{"seq": 1, "at": "2026-10-05T03:00:03Z", "type": "agent.message",
            "session_id": ROOT_SESSION, "from_agent": "/root", "to_agent": "/root/review", "message": "Current journal"}])
        self.assertEqual([m["message"] for m in state["messages"]], ["Other final"])
        self.assertEqual([e["type"] for e in state["events"]], ["task_started", "task_complete"])
        self.assertTrue(all(e["session_id"] == "other-root" for e in state["events"]))
        self.assertIsNone(state["registered_workflow"])

    def test_current_root_timeline_excludes_journal_records_from_another_session(self):
        self.ingest("task_started", turn_id="current", root_turn_id="current")
        foreign = {"seq": 1, "at": "2026-10-05T01:00:02Z", "type": "agent.message",
                   "session_id": "foreign-root", "from_agent": "/root", "to_agent": "/root/review",
                   "message": "Foreign report"}
        own = dict(foreign, seq=2, session_id=ROOT_SESSION, message="Current report")
        state = self.state([foreign, own])
        self.assertNotIn(foreign, state["events"])
        self.assertIn(own, state["events"])
        self.assertEqual([m["message"] for m in state["messages"]], ["Current report"])

    def test_followup_running_is_visible_after_registered_plan_completed(self):
        self.ingest("task_started", turn_id="first", root_turn_id="first")
        self.ingest("task_complete", turn_id="first", last_agent_message="Done")
        self.ingest("task_started", at="2026-10-05T02:00:00Z", turn_id="followup", root_turn_id="followup")
        self.ingest("message", row_type="response_item", role="user", content=[{"type": "input_text",
            "text": "<environment_context>app metadata</environment_context>\n## My request:\nFix live monitoring\n<image>attachment</image>"}])
        state = self.state()
        self.assertEqual(state["live"]["running_agents"], 1)
        self.assertEqual([n["status"] for n in state["nodes"]], ["completed", "running"])
        self.assertEqual(state["nodes"][-1]["title"], "Fix live monitoring")
        self.assertEqual(state["graph_kind"], "request_history")
        self.assertEqual(state["registered_workflow"]["nodes"][0]["status"], "completed")

    def test_request_node_exposes_role_separately_from_agent_name(self):
        self.ingest("task_started", turn_id="request", root_turn_id="request")
        child = {**self.entry, "id": "child", "agent_id": "/root/ui", "parent_id": ROOT_SESSION,
                 "nickname": "Galileo", "current_turn_id": None, "active_tools": {}, "call_turns": {}}
        self.collector.files["child"] = child
        self.collector.ingest(child, {"type": "event_msg", "timestamp": "2026-10-05T01:00:01Z", "ordinal": 1,
            "payload": {"type": "task_started", "turn_id": "ui-turn", "root_turn_id": "request"}})
        state = self.collector.augment({"revision": 1, "title": "plan", "run_id": ROOT_SESSION,
            "binding": {"project_path": PROJECT_PATH, "root_session_id": ROOT_SESSION},
            "started_at": "2026-10-05T01:00:00Z", "updated_at": "2026-10-05T01:00:00Z", "events": [],
            "nodes": [{"id": "ui", "role": "Frontend Developer", "agent_id": "/root/ui", "status": "running",
                       "started_at": "2026-10-05T01:00:00Z", "finished_at": None}]})
        node = next(n for n in state["nodes"] if n["session_id"] == "child")
        self.assertEqual(node["role"], "Frontend Developer")
        self.assertEqual(node["nickname"], "Galileo")
        self.assertEqual(node["role_source"], "workflow_assignment")
        # Selecting a different root must not carry the current app's workflow roles.
        self.entry["id"] = "another-root"
        child["parent_id"] = "another-root"
        self.collector.root_session = "another-root"
        node = next(n for n in self.state()["nodes"] if n["session_id"] == "child")
        self.assertIsNone(node["role"])
        self.assertEqual(node["nickname"], "Galileo")

    def test_app_selected_path_is_a_suggestion_and_named_sessions_keep_ids(self):
        self.ingest("task_started", turn_id="current", root_turn_id="current")
        self.collector.app_reader = SimpleNamespace(
            context={"status": "ready", "selected_project": {"id": "another-project",
                "name": "Another", "path": "/another/path", "paths": ["/another/path"]}},
            names={ROOT_SESSION: {"name": "Name visible in Codex", "name_source": "app_thread_name"}})
        state = self.state()
        self.assertEqual(state["app_context"]["selected_project"]["path"], "/another/path")
        self.assertEqual(state["project_path"], PROJECT_PATH)
        self.assertEqual(state["root_session_id"], ROOT_SESSION)
        self.assertEqual(state["available_runs"][0]["id"], ROOT_SESSION)
        self.assertEqual(state["available_runs"][0]["name"], "Name visible in Codex")
        self.assertEqual(state["root_session_name"], "Name visible in Codex")
        self.collector.app_reader.names.clear()
        self.assertEqual(self.state()["available_runs"][0]["name"], "이름 없는 세션")
    def test_tool_completion_does_not_complete_request_and_abort_clears_tools(self):
        self.ingest("task_started", turn_id="turn", root_turn_id="turn")
        self.ingest("function_call", row_type="response_item", name="exec", call_id="call", arguments="run checks")
        self.assertEqual(self.state()["live"]["active_tools"], 1)
        self.ingest("function_call_output", row_type="response_item", call_id="call", output="checks passed")
        state = self.state()
        self.assertEqual(state["live"]["active_tools"], 0)
        self.assertEqual(state["live"]["running_agents"], 1)
        self.ingest("function_call", row_type="response_item", name="exec", call_id="call2", arguments="more")
        self.ingest("turn_aborted", turn_id="turn", reason="User interrupted")
        self.assertEqual(self.state()["live"]["running_agents"], 0)
        self.assertEqual(self.state()["live"]["active_tools"], 0)
        self.assertEqual(self.state()["nodes"][0]["status"], "failed")

    def test_delayed_old_completion_does_not_stop_current_turn(self):
        self.ingest("task_started", turn_id="old", root_turn_id="old")
        self.ingest("function_call", row_type="response_item", name="exec", call_id="old-call", arguments="old")
        self.ingest("task_started", turn_id="new", root_turn_id="new")
        self.ingest("message", row_type="response_item", role="assistant", phase="commentary",
                    content=[{"type": "output_text", "text": "new progress"}])
        self.ingest("task_complete", turn_id="old", last_agent_message="old done")
        self.ingest("function_call_output", row_type="response_item", call_id="old-call", output="old result")
        state = self.state()
        self.assertEqual(state["live"]["running_agents"], 1)
        self.assertEqual(state["nodes"][-1]["status"], "running")
        self.assertNotIn("old done", state["nodes"][-1]["detail"])
        self.assertNotIn("old result", state["nodes"][-1]["detail"])
        self.assertEqual(state["live"]["agents"][0]["latest_activity"]["detail"], "new progress")
        self.assertEqual(next(x for x in state["activity"] if x["id"] == "output:old-call")["turn_id"], "old")

    def test_restamped_parent_bootstrap_is_not_a_child_request(self):
        self.entry.update(id="child", parent_id=ROOT_SESSION, agent_id="/root/ui",
                          created_at="2026-10-05T01:00:00.900Z")
        self.ingest("task_started", at="2026-10-05T01:00:00.900Z", turn_id="parent",
                    root_turn_id="parent", started_at=1791158400)
        self.assertFalse(self.collector.turns)
        self.ingest("task_complete", turn_id="parent", last_agent_message="parent result")
        self.assertEqual(self.entry["status"], "unknown")
        self.ingest("task_started", at="2026-10-05T01:00:00.950Z", turn_id="child-turn",
                    root_turn_id="parent", started_at=1791162000)
        self.assertEqual(len(self.collector.turns), 1)

    def test_unspecified_ids_with_file_offsets_preserve_multiple_progress_records(self):
        for offset in (100, 200):
            self.collector.ingest(self.entry, {"type": "response_item", "timestamp": "2026-10-05T01:00:00Z",
                "_offset": offset, "payload": {"type": "message", "role": "assistant", "phase": "commentary",
                                              "content": [{"type": "output_text", "text": "progress"}]}})
        self.assertEqual(len(self.collector.activity), 2)

    def test_ambient_context_is_not_a_request_and_analysis_is_not_public_activity(self):
        self.assertEqual(user_request("<environment_context>metadata</environment_context>"), "")
        self.ingest("message", row_type="response_item", role="assistant", phase="analysis",
                    content=[{"type": "output_text", "text": "private thoughts"}])
        self.assertFalse(self.collector.activity)

    def test_direct_followup_uses_observed_sender_instead_of_session_creation_parent(self):
        self.ingest("task_started", turn_id="request", root_turn_id="request")
        self.ingest("function_call", row_type="response_item", namespace="collaboration", name="followup_task",
                    call_id="dispatch", arguments=json.dumps({"target": "/root/worker/sub", "message": "Review"}))
        child = {**self.entry, "id": "sub", "agent_id": "/root/worker/sub", "parent_id": "worker",
                 "current_turn_id": None, "active_tools": {}, "call_turns": {}}
        self.collector.files["worker"] = {**self.entry, "id": "worker", "agent_id": "/root/worker",
                                           "parent_id": ROOT_SESSION, "status": "completed"}
        self.collector.files["sub"] = child
        self.collector.ingest(child, {"type": "event_msg", "ordinal": 1, "timestamp": "2026-10-05T01:00:01Z",
            "payload": {"type": "task_started", "turn_id": "sub-turn", "root_turn_id": "request"}})
        node = next(x for x in self.state()["nodes"] if x["session_id"] == "sub")
        self.assertEqual(node["parent_id"], f"{ROOT_SESSION}:request")
        self.assertEqual(node["parent_relation"], "observed_dispatch")
        self.collector.dispatches.clear()
        node = next(x for x in self.state()["nodes"] if x["session_id"] == "sub")
        self.assertIsNone(node["parent_id"])
        self.assertEqual(node["lineage_parent_session_id"], "worker")

    def test_nested_relative_followup_and_session_id_targets_are_preserved(self):
        self.ingest("task_started", turn_id="request", root_turn_id="request")
        worker = {**self.entry, "id": "worker", "agent_id": "/root/worker", "parent_id": ROOT_SESSION,
                  "current_turn_id": None, "active_tools": {}, "call_turns": {}}
        sub = {**worker, "id": "sub", "agent_id": "/root/worker/sub", "parent_id": "worker"}
        self.collector.files.update(worker=worker, sub=sub)
        self.collector.ingest(worker, {"type": "event_msg", "ordinal": 1, "timestamp": "2026-10-05T01:00:01Z",
            "payload": {"type": "task_started", "turn_id": "worker-turn", "root_turn_id": "request"}})
        self.collector.ingest(worker, {"type": "response_item", "ordinal": 2, "timestamp": "2026-10-05T01:00:02Z",
            "payload": {"type": "function_call", "namespace": "collaboration", "name": "followup_task", "call_id": "nested",
                        "arguments": json.dumps({"target": "sub", "message": "Review"})}})
        self.collector.ingest(sub, {"type": "event_msg", "ordinal": 3, "timestamp": "2026-10-05T01:00:03Z",
            "payload": {"type": "task_started", "turn_id": "sub-turn", "root_turn_id": "request"}})
        node = next(x for x in self.state()["nodes"] if x["session_id"] == "sub")
        self.assertEqual(node["parent_id"], "worker:worker-turn")
        self.ingest("function_call", row_type="response_item", namespace="collaboration", name="followup_task",
                    call_id="id-target", arguments=json.dumps({"target": ROOT_SESSION, "message": "Test"}))
        self.assertEqual(self.collector.dispatches["id-target"]["target"], ROOT_SESSION)


if __name__ == "__main__":
    unittest.main()
