"""Task-scoped assignments and dialogs survive shared-journal import/reuse."""
import copy
import json
import threading
import tempfile
import unittest
from pathlib import Path

from collector import Collector, group_message_records, journal_record_id


class CollaborationBindingChecks(unittest.TestCase):
    def setUp(self):
        self.collector = Collector.__new__(Collector)
        self.collector.lock = threading.RLock()
        self.collector.messages, self.collector.activity, self.collector.turns, self.collector.dispatches = {}, {}, {}, {}
        self.collector.revision = 0
        self.collector.project, self.collector.root_session = "/project", "root"
        self.collector.error, self.collector.last_success_at = None, None
        self.root = self.entry("root", "/root", "Coordinator")
        self.worker = self.entry("worker", "/root/review", "Mill", "root")
        self.collector.files = {"root": self.root, "worker": self.worker}
        self.ordinal = 0
        self.ingest(self.root, "task_started", "2026-10-07T01:00:00Z", turn_id="request", root_turn_id="request")

    def entry(self, identity, agent, nickname, parent=None):
        return {"id": identity, "cwd": "/project", "agent_id": agent, "nickname": nickname,
                "parent_id": parent, "created_at": "2026-10-07T00:59:00Z", "status": "unknown",
                "last_activity_at": "2026-10-07T00:59:00Z", "path": identity + ".jsonl"}

    def ingest(self, entry, kind, at, row_type="event_msg", **payload):
        self.ordinal += 1
        self.collector.ingest(entry, {"timestamp": at, "type": row_type, "ordinal": self.ordinal,
                                     "payload": {"type": kind, **payload}})

    def dispatch(self, identity="assign", at="2026-10-07T01:00:01Z", body="Review the change"):
        self.ingest(self.root, "function_call", at, row_type="response_item", namespace="collaboration",
                    name="followup_task", call_id=identity,
                    arguments=json.dumps({"target": "/root/review", "message": body}))

    def start_worker(self, identity="first", at="2026-10-07T01:00:02Z", request="request"):
        self.ingest(self.worker, "task_started", at, turn_id=identity, root_turn_id=request)
        return self.collector.turns["worker:" + identity]

    def state(self, events=(), nodes=()):
        return self.collector.augment({"events": list(events), "nodes": list(nodes), "revision": 1,
            "title": "Plan", "run_id": "plan", "started_at": "2026-10-07T01:00:00Z",
            "updated_at": "2026-10-07T01:00:00Z",
            "binding": {"project_path": "/project", "root_session_id": "root"}})

    def assignment(self, **changes):
        event = {"seq": 1, "event_id": "sha256:assignment", "type": "agent.message",
                 "at": "2026-10-07T01:00:00.900Z", "message_kind": "assignment",
                 "from_agent": "/root", "to_agent": "/root/review", "session_id": "root",
                 "project_path": "/project", "message": "Review the change", "role": "Reviewer",
                 "sender_session_id": "root", "sender_turn_id": "request",
                 "recipient_session_id": "worker", "recipient_turn_id": "first", "root_turn_id": "request"}
        return {**event, **changes}

    def worker_node(self, state, identity="first"):
        return next(node for node in state["nodes"] if node["id"] == "worker:" + identity)

    def user_message(self, entry, text, at):
        self.ingest(entry, "message", at, row_type="response_item", role="user",
                    content=[{"type": "input_text", "text": text}])

    def test_subagent_instructions_do_not_replace_name_or_role(self):
        self.dispatch();turn=self.start_worker();instructions="# AGENTS.md instructions for /project\n<INSTRUCTIONS>Preserve original evidence.</INSTRUCTIONS>"
        self.user_message(self.worker,instructions,"2026-10-07T01:00:03Z")
        self.ingest(self.worker,"task_complete","2026-10-07T01:01:00Z",turn_id="first")
        original=copy.deepcopy(turn);node=self.worker_node(self.state([self.assignment()]))
        self.assertEqual(node["title"],"Mill · 에이전트 작업")
        self.assertEqual(node["nickname"],"Mill")
        self.assertEqual(node["role"],"Reviewer")
        # Injected instructions are kept verbatim with their source ID, not as the request.
        self.assertEqual(node["request"],"")
        self.assertEqual([(r["record_kind"],r["body"],r["at"]) for r in node["instruction_records"]],
                         [("codex_instructions",instructions,"2026-10-07T01:00:03Z")])
        self.assertEqual(node["instruction_records"],original["instruction_records"])
        self.assertEqual(node["request_title"],original["title"])
        self.assertEqual((node["id"],node["status"],node["finished_at"]),
                         (original["id"],"completed",original["finished_at"]))
        self.assertEqual(turn,original)

    def test_subagent_real_task_prompt_also_preserves_identity_title(self):
        self.start_worker();body="Review backend result and its tests"
        self.user_message(self.worker,body,"2026-10-07T01:00:03Z")
        node=self.worker_node(self.state())
        self.assertEqual(node["title"],"Mill · 에이전트 작업")
        self.assertEqual((node["request_title"],node["request"]),(body,body))

    def test_root_request_title_remains_visible_even_when_named_like_instructions(self):
        for body in ["Fix the collaboration graph","# AGENTS.md instructions for /project"]:
            with self.subTest(body=body):
                self.collector.turns["root:request"]["request"]=""
                self.user_message(self.root,body,"2026-10-07T01:00:01Z")
                node=next(n for n in self.state()["nodes"] if n["id"]=="root:request")
                self.assertEqual((node["title"],node["request_title"],node["request"]),(body,body,body))
                self.assertEqual(node["role"],"Coordinator")

    def test_followup_and_nested_subagent_titles_keep_names_per_turn(self):
        self.start_worker();self.user_message(self.worker,"Initial work","2026-10-07T01:00:03Z")
        self.ingest(self.worker,"task_complete","2026-10-07T01:01:00Z",turn_id="first")
        self.start_worker("second","2026-10-07T01:02:00Z")
        self.user_message(self.worker,"Followup prompt","2026-10-07T01:02:01Z")
        nested=self.entry("nested","/root/review/nested","Nested","worker");self.collector.files["nested"]=nested
        self.ingest(nested,"task_started","2026-10-07T01:02:02Z",turn_id="nested-turn",root_turn_id="request")
        self.user_message(nested,"# AGENTS.md instructions for /project","2026-10-07T01:02:03Z")
        nodes=self.state()["nodes"]
        self.assertEqual([n["title"] for n in nodes if n["session_id"]=="worker"],["Mill · 에이전트 작업"]*2)
        self.assertEqual(next(n["title"] for n in nodes if n["session_id"]=="nested"),"Nested · 에이전트 작업")
        self.assertEqual(next(n["status"] for n in nodes if n["id"]=="worker:first"),"completed")

    def test_missing_message_does_not_hide_running_worker_dialog(self):
        self.start_worker()
        state = self.state()
        self.assertFalse(state["messages"])
        conversation = state["conversations"][0]
        self.assertEqual((conversation["from_agent"], conversation["to_agent"]), ("/root", "/root/review"))
        self.assertEqual(conversation["status"], "running")
        self.assertEqual(conversation["source"], "session_lineage")
        self.assertEqual(conversation["refs"][0]["recipient_session_id"], "worker")

    def test_running_root_does_not_revive_completed_worker_dialog(self):
        self.start_worker()
        self.ingest(self.worker, "task_complete", "2026-10-07T01:02:00Z", turn_id="first")
        state = self.state()
        self.assertEqual(state["live"]["running_agents"], 1)
        self.assertEqual(state["conversations"][0]["status"], "completed")

    def test_registered_only_agent_does_not_become_live_dialog(self):
        state = self.state(nodes=[{"id": "manual", "agent_id": "/root/not-observed", "role": "Developer",
                                  "status": "pending", "started_at": None, "finished_at": None}])
        self.assertFalse(any(item["to_agent"] == "/root/not-observed" for item in state["conversations"]))

    def test_direct_dispatch_relation_is_distinct_from_creation_lineage(self):
        second = self.entry("second", "/root/worker/sub", "Second", "worker")
        self.collector.files["second"] = second
        self.ingest(self.root, "function_call", "2026-10-07T01:00:01Z", row_type="response_item",
                    namespace="collaboration", name="followup_task", call_id="nested",
                    arguments=json.dumps({"target": "second", "message": "Inspect"}))
        dialogs = self.state()["conversations"]
        self.assertTrue(any(d["from_agent"] == "/root/review" and d["to_agent"] == "/root/worker/sub" for d in dialogs))
        self.assertTrue(any(d["from_agent"] == "/root" and d["to_agent"] == "/root/worker/sub" for d in dialogs))

    def test_assignment_node_exposes_actual_body_and_shared_originals(self):
        self.dispatch(body="gAAAAA" + "x" * 80)
        self.start_worker()
        original = {"seq": 9, "at": "2026-10-07T01:00:00.900Z", "message": "Review the change"}
        event = self.assignment(journal_origins=[{"source_id": "legacy", "source_seq": 9, "original_event": original}])
        state = self.state([event])
        node = self.worker_node(state)
        self.assertEqual(node["role"], "Reviewer")
        self.assertEqual(node["role_source"], "journal_assignment")
        self.assertEqual(node["role_refs"], ["journal:sha256:assignment"])
        self.assertEqual(node["assignment"]["message"], "Review the change")
        self.assertEqual(node["assignment"]["id"], "journal:sha256:assignment")
        self.assertEqual(len(node["assignment_records"]), 2)
        self.assertEqual(node["assignment_records"][0]["journal_origins"][0]["original_event"], original)
        self.assertEqual(node["parent_id"], "root:request")
        self.assertEqual(node["assignment_call_id"], "assign")
        self.assertEqual(state["messages"][0]["journal_match"], "inferred")

    def test_explicit_assignment_role_overrides_session_spawn_default(self):
        self.worker["assigned_role"] = "Native"
        self.start_worker()
        self.assertEqual(self.worker_node(self.state([self.assignment()]))["role"], "Reviewer")
        self.assertEqual(self.worker_node(self.state())["role"], "Native")

    def test_workflow_exact_turn_role_overrides_session_default(self):
        self.worker["assigned_role"] = "Native"
        self.start_worker()
        node = {"id": "review", "agent_id": "/root/review", "role": "Reviewer", "status": "completed",
                "started_at": "2026-10-07T00:00:00Z", "finished_at": "2026-10-07T00:01:00Z",
                "execution_request_id": "worker:first"}
        resolved = self.worker_node(self.state(nodes=[node]))
        self.assertEqual(resolved["role"], "Reviewer")
        self.assertEqual(resolved["role_refs"], ["review"])

    def test_workflow_matching_session_and_turn_binding_is_supported(self):
        self.start_worker()
        node = {"id": "review", "agent_id": "worker", "role": "Reviewer", "status": "completed",
                "started_at": None, "finished_at": None, "task_session_id": "worker", "task_turn_id": "first"}
        self.assertEqual(self.worker_node(self.state(nodes=[node]))["role"], "Reviewer")

    def test_exact_target_can_bind_role_without_redundant_agent_path(self):
        self.start_worker()
        stage = {"id": "explicit", "role": "Reviewer", "status": "running", "started_at": None,
                 "finished_at": None, "execution_request_id": "worker:first"}
        state = self.state(nodes=[stage])
        self.assertEqual(self.worker_node(state)["role"], "Reviewer")
        self.assertEqual(state["registered_workflow"]["nodes"][0]["execution_request_id"], "worker:first")

    def test_contradictory_explicit_target_metadata_cannot_claim_role(self):
        self.start_worker()
        stage = {"id": "explicit", "agent_id": "worker", "role": "Reviewer", "status": "running",
                 "started_at": "2026-10-07T01:00:01Z", "finished_at": None,
                 "execution_request_id": "worker:first", "task_session_id": "worker", "task_turn_id": "second"}
        state = self.state(nodes=[stage])
        self.assertIsNone(self.worker_node(state)["role"])
        self.assertEqual(state["registered_workflow"]["nodes"][0]["status"], "unconfirmed")
        event = self.assignment(execution_request_id="worker:first", recipient_turn_id="second")
        self.assertIsNone(self.worker_node(self.state([event]))["role"])

    def test_exact_running_stage_binding_keeps_original_execution_on_reuse(self):
        self.start_worker()
        self.ingest(self.worker, "task_complete", "2026-10-07T01:02:00Z", turn_id="first")
        self.start_worker("second", "2026-10-07T01:03:00Z")
        stage = {"id": "first-stage", "agent_id": "/root/review", "role": "Reviewer", "status": "running",
                 "started_at": "2026-10-07T01:03:30Z", "finished_at": None, "execution_request_id": "worker:first"}
        state = self.state(nodes=[stage])
        projected = state["registered_workflow"]["nodes"][0]
        self.assertEqual(projected["execution_request_id"], "worker:first")
        self.assertEqual(projected["status"], "awaiting_confirmation")
        self.assertEqual(self.worker_node(state)["role"], "Reviewer")
        self.assertIsNone(self.worker_node(state, "second")["role"])

    def test_invalid_explicit_binding_cannot_fall_back_to_active_worker(self):
        self.start_worker()
        stage = {"id": "invalid", "agent_id": "/root/review", "role": "Reviewer", "status": "running",
                 "started_at": "2026-10-07T01:00:01Z", "finished_at": None,
                 "execution_request_id": "worker:not-observed"}
        state = self.state(nodes=[stage])
        self.assertEqual(state["registered_workflow"]["nodes"][0]["status"], "unconfirmed")
        self.assertIsNone(self.worker_node(state)["role"])

    def test_recorder_session_is_not_an_explicit_worker_binding(self):
        self.start_worker()
        stage = {"id": "legacy", "agent_id": "/root/review", "role": "Reviewer", "status": "completed",
                 "started_at": "2026-10-07T01:00:01Z", "finished_at": "2026-10-07T01:01:00Z",
                 "session_id": "root", "turn_id": "request"}
        self.assertEqual(self.worker_node(self.state(nodes=[stage]))["role"], "Reviewer")

    def test_conflicting_explicit_roles_remain_unknown_with_both_refs(self):
        self.start_worker()
        events = [self.assignment(), self.assignment(seq=2, event_id="sha256:other", role="Developer")]
        resolved = self.worker_node(self.state(events))
        self.assertIsNone(resolved["role"])
        self.assertEqual(resolved["role_source"], "conflicting_assignments")
        self.assertEqual(len(resolved["role_refs"]), 2)

    def test_explicit_assignment_to_other_turn_or_root_is_not_reused(self):
        self.start_worker()
        for change in ({"recipient_turn_id": "second"}, {"root_turn_id": "unrelated"},
                       {"recipient_session_id": "unrelated"}, {"project_path": "/other"},
                       {"sender_session_id": "proxy"}, {"sender_turn_id": "not-observed"}):
            with self.subTest(change=change):
                node = self.worker_node(self.state([self.assignment(**change)]))
                self.assertIsNone(node["role"])
                self.assertIsNone(node["assignment"])

    def test_legacy_finished_role_and_spawn_role_do_not_leak_to_reused_turn(self):
        self.worker["assigned_role"] = "Native"
        self.start_worker()
        self.ingest(self.worker, "task_complete", "2026-10-07T01:02:00Z", turn_id="first")
        self.start_worker("second", "2026-10-07T01:03:00Z")
        stage = {"id": "old", "agent_id": "/root/review", "role": "Reviewer", "status": "completed",
                 "started_at": "2026-10-07T01:00:01Z", "finished_at": "2026-10-07T01:02:00Z"}
        resolved = self.worker_node(self.state(nodes=[stage]), "second")
        self.assertIsNone(resolved["role"])

    def test_legacy_role_from_other_parent_request_does_not_overlap(self):
        self.start_worker()
        self.ingest(self.worker, "task_complete", "2026-10-07T01:02:00Z", turn_id="first")
        self.ingest(self.root, "task_complete", "2026-10-07T01:04:00Z", turn_id="request")
        self.ingest(self.root, "task_started", "2026-10-07T01:05:00Z", turn_id="new", root_turn_id="new")
        self.start_worker("second", "2026-10-07T01:06:00Z", "new")
        stage = {"id": "old", "agent_id": "/root/review", "role": "Reviewer", "status": "running",
                 "started_at": "2026-10-07T01:00:01Z", "finished_at": None}
        self.assertIsNone(self.worker_node(self.state(nodes=[stage]), "second")["role"])

    def test_original_turn_late_registration_preserves_unique_early_role(self):
        self.start_worker()
        self.ingest(self.worker, "task_complete", "2026-10-07T01:02:00Z", turn_id="first")
        stage = {"id": "late", "agent_id": "/root/review", "role": "Reviewer", "status": "completed",
                 "started_at": "2026-10-07T01:03:00Z", "finished_at": "2026-10-07T01:04:00Z"}
        self.assertEqual(self.worker_node(self.state(nodes=[stage]))["role"], "Reviewer")

    def test_dispatch_body_belongs_only_to_first_recipient_turn(self):
        self.dispatch()
        self.start_worker()
        self.ingest(self.worker, "task_complete", "2026-10-07T01:02:00Z", turn_id="first")
        self.start_worker("second", "2026-10-07T01:03:00Z")
        state = self.state()
        self.assertEqual(self.worker_node(state)["assignment"]["message"], "Review the change")
        self.assertIsNone(self.worker_node(state, "second")["assignment"])
        self.assertIsNone(self.worker_node(state, "second")["parent_id"])

    def test_shared_event_id_survives_ledger_reordering_and_preserves_origins(self):
        self.start_worker()
        event = self.assignment(journal_origins=[{"source_id": "legacy", "source_seq": 8,
                                                "original_event": {"seq": 8, "message": "Review the change"}}])
        before = copy.deepcopy(event)
        first = self.state([event])
        second = self.state([{**event, "seq": 100}])
        self.assertEqual(first["message_records"][0]["id"], second["message_records"][0]["id"])
        self.assertEqual(first["execution_events"][-1]["id"], second["execution_events"][-1]["id"])
        self.assertEqual(first["message_records"][0]["journal_origins"], before["journal_origins"])
        self.assertEqual(event, before)

    INSTRUCTIONS = "# AGENTS.md instructions for /project\n\n<INSTRUCTIONS>\nKeep evidence.\n</INSTRUCTIONS>"

    def codex_payload(self, entry, at, *texts):
        self.ingest(entry, "message", at, row_type="response_item", role="user",
                    content=[{"type": "input_text", "text": text} for text in texts])

    def root_node(self, state):
        return next(node for node in state["nodes"] if node["id"] == "root:request")

    def test_injected_instructions_are_not_the_root_request_title_or_request_line(self):
        # Codex sends AGENTS.md together with its environment context before the user's words.
        self.codex_payload(self.root, "2026-10-07T01:00:00.100Z", self.INSTRUCTIONS,
                           "<environment_context>\n  <cwd>/project</cwd>\n</environment_context>")
        self.codex_payload(self.root, "2026-10-07T01:00:00.200Z", "Create a reviewer and report back")
        state = self.state()
        node = self.root_node(state)
        self.assertEqual((node["title"], node["request_title"], node["request"]),
                         ("Create a reviewer and report back",) * 3)
        self.assertEqual([(r["record_kind"], r["body"]) for r in node["instruction_records"]],
                         [("codex_instructions", self.INSTRUCTIONS)])
        self.assertEqual([card["message"] for card in state["graph_requests"]], ["Create a reviewer and report back"])
        requests = [r for r in state["graph_relations"] if r["kind"] == "request"]
        self.assertEqual([(r["to_node_id"], r["count"]) for r in requests], [("root:request", 1)])

    def test_instruction_wrapper_followed_by_other_text_stays_the_request(self):
        mixed = self.INSTRUCTIONS + "\n\nAlso fix the graph"
        self.codex_payload(self.root, "2026-10-07T01:00:00.100Z", mixed)
        node = self.root_node(self.state())
        self.assertEqual(node["request"], mixed)
        self.assertNotIn("instruction_records", node)

    def test_task_id_on_an_assignment_journal_still_binds_the_observed_spawn(self):
        cipher = "gAAAAA" + "x" * 64
        journal = {"seq": 1, "event_id": "sha256:task-id", "type": "agent.message", "at": "2026-10-07T01:00:05Z",
                   "message_kind": "assignment", "from_agent": "/root", "to_agent": "/root/review",
                   "session_id": "root", "sender_session_id": "root", "project_path": "/project",
                   "message": "Review the documentation", "role": "Reviewer", "assignment_id": "doc-check-task-1"}
        self.ingest(self.root, "function_call", "2026-10-07T01:00:10Z", row_type="response_item",
                    namespace="collaboration", name="spawn_agent", call_id="spawn",
                    arguments=json.dumps({"task_name": "review", "message": cipher}))
        self.start_worker(at="2026-10-07T01:00:11Z")
        self.ingest(self.worker, "agent_message", "2026-10-07T01:00:11.500Z", row_type="response_item",
                    author="/root", recipient="/root/review",
                    content=[{"type": "encrypted_content", "encrypted_content": cipher}])
        self.ingest(self.worker, "task_complete", "2026-10-07T01:00:30Z", turn_id="first")
        state = self.state([journal])
        worker = self.worker_node(state)
        self.assertEqual((worker["role"], worker["role_source"]), ("Reviewer", "journal_assignment"))
        self.assertEqual(worker["assignment"]["message"], "Review the documentation")
        self.assertEqual(worker["assignment_source"], "sender_journal")
        card = next(m for m in state["messages"] if m["type"] == "assignment")
        self.assertEqual((card["journal_match"], card["assignment_id"]), ("inferred", "doc-check-task-1"))
        self.assertEqual({r["record_kind"] for r in card["records"]},
                         {"collaboration_call", "agent_message", "sender_journal"})
        line = next(r for r in state["graph_relations"] if card["id"] in r["message_ids"])
        self.assertEqual((line["kind"], line["from_node_id"], line["to_node_id"]),
                         ("delegation", "root:request", "worker:first"))
        self.assertFalse([e for e in state["graph_endpoints"] if e["kind"] == "unresolved"])

    def test_equal_legacy_seq_with_distinct_shared_id_is_not_merged(self):
        events = [self.assignment(), self.assignment(event_id="sha256:other", message="Different assignment")]
        state = self.state(events)
        self.assertEqual(len(state["message_records"]), 2)
        self.assertEqual(len({m["id"] for m in state["message_records"]}), 2)
        self.assertEqual(len([e for e in state["execution_events"] if e["type"] == "agent.message"]), 2)
        self.assertEqual(journal_record_id({"seq": 9}), "journal:9")


class ExplicitCallIdentityChecks(unittest.TestCase):
    def test_explicit_call_reference_can_be_recorded_later_without_time_guess(self):
        common = {"project_path": "/project", "from_agent": "/root", "to_agent": "/root/review",
                  "sender_session_id": "root", "sender_turn_id": "turn", "type": "assignment"}
        call = {**common, "id": "call", "at": "2026-10-07T01:00:02Z", "record_kind": "collaboration_call",
                "message": "encrypted", "encrypted": True, "delivery_key": "opaque"}
        journal = {**common, "id": "journal:stable", "at": "2026-10-07T01:02:00Z",
                   "record_kind": "sender_journal", "recorder_session_id": "root", "message": "Review",
                   "encrypted": False, "collaboration_call_id": "call", "role": "Reviewer"}
        turns = {"root:turn": {"started_at": "2026-10-07T01:00:00Z", "finished_at": "2026-10-07T01:03:00Z"}}
        card = group_message_records([journal, call], turns)[0]
        self.assertEqual(card["journal_match"], "explicit")
        self.assertEqual(card["role"], "Reviewer")
        self.assertEqual(card["record_count"], 2)
        self.assertEqual(len(group_message_records([{**journal, "collaboration_call_id": "different"}, call], turns)), 2)
        self.assertEqual(len(group_message_records([{**journal, "recorder_session_id": "proxy"}, call], turns)), 2)

    def test_assignment_id_binds_only_when_it_is_an_observed_call_id(self):
        common = {"project_path": "/project", "from_agent": "/root", "to_agent": "/root/review",
                  "sender_session_id": "root", "sender_turn_id": "turn", "type": "assignment"}
        call = {**common, "id": "call", "at": "2026-10-07T01:00:10Z", "record_kind": "collaboration_call",
                "message": "encrypted", "encrypted": True, "delivery_key": "opaque"}
        journal = {**common, "id": "journal:stable", "record_kind": "sender_journal", "recorder_session_id": "root",
                   "message": "Review", "encrypted": False}
        turns = {"root:turn": {"started_at": "2026-10-07T01:00:00Z", "finished_at": "2026-10-07T01:05:00Z"}}
        late = "2026-10-07T01:02:00Z"
        # An observed call ID keeps the explicit binding, even outside the inference window.
        cards = group_message_records([{**journal, "at": late, "assignment_id": "call"}, call], turns)
        self.assertEqual([(card["record_count"], card.get("journal_match")) for card in cards], [(2, "explicit")])
        # A writer's own task ID is not a call identity: no explicit binding, so timing rules apply.
        self.assertEqual(len(group_message_records([{**journal, "at": late, "assignment_id": "task-1"}, call], turns)), 2)
        cards = group_message_records([{**journal, "at": "2026-10-07T01:00:05Z", "assignment_id": "task-1"}, call], turns)
        self.assertEqual([(card["record_count"], card.get("journal_match")) for card in cards], [(2, "inferred")])

    def test_explicit_identity_does_not_deduplicate_actual_resends(self):
        common = {"project_path": "/project", "from_agent": "/root", "to_agent": "/root/review",
                  "sender_session_id": "root", "sender_turn_id": "turn", "type": "assignment"}
        call = {**common, "id": "call", "at": "2026-10-07T01:00:02Z", "record_kind": "collaboration_call",
                "message": "Review", "encrypted": False}
        journal = {**common, "id": "journal:stable", "at": "2026-10-07T01:00:01Z",
                   "record_kind": "sender_journal", "recorder_session_id": "root", "message": "Review",
                   "encrypted": False, "collaboration_call_id": "call"}
        turns = {"root:turn": {"started_at": "2026-10-07T01:00:00Z", "finished_at": None}}
        cards = group_message_records([journal, call, {**call, "id": "resend"}], turns)
        self.assertEqual(len(cards), 2)
        self.assertEqual(sum(card["record_count"] for card in cards), 3)
        self.assertEqual(len(group_message_records([journal, {**journal, "id": "journal:second"}, call], turns)), 3)


class SharedCollectorBoundaryChecks(unittest.TestCase):
    def test_codex_switch_rejects_shared_journal_inside_new_input_without_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            data, old_codex, new_codex = base / "data", base / "old-codex", base / "new-codex"
            data.mkdir()
            old_codex.mkdir()
            new_codex.mkdir()
            journal = new_codex / "public-journal"
            data.joinpath("config.json").write_text(json.dumps({"journal_dir": str(journal),
                "codex_dir": str(old_codex), "root_session_id": "saved-root"}))
            collector = Collector(data, start_thread=False)
            before = data.joinpath("config.json").read_bytes()
            with self.assertRaisesRegex(ValueError, "Codex input"):
                collector.set_codex_directory(str(new_codex))
            self.assertEqual(collector.codex_directory, old_codex)
            self.assertEqual(collector.root_session, "saved-root")
            self.assertEqual(data.joinpath("config.json").read_bytes(), before)
            self.assertFalse(journal.exists())


if __name__ == "__main__":
    unittest.main()
