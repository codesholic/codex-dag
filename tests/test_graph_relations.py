"""Public routing must connect execution turns without guessing reused tasks."""
import copy
import json
import threading
import unittest

from collector import Collector, graph_relations, group_message_records


class GraphRelationChecks(unittest.TestCase):
    def setUp(self):
        self.entries = [self.entry("root", "/root", "Coordinator"),
                        self.entry("sender", "/root/backend", "Bernoulli"),
                        self.entry("recipient", "/root/review", "Boyle")]
        self.nodes = [self.node("root", "/root", "request", 0, 50),
                      self.node("sender", "/root/backend", "one", 2, 20, "root:request"),
                      self.node("recipient", "/root/review", "first", 3, 25, "root:request"),
                      self.node("recipient", "/root/review", "later", 30, 40, "root:request")]
        self.nodes[1]["assignment_call_id"] = "assign-sender"
        self.nodes[2]["assignment_call_id"] = "assign-recipient"

    @staticmethod
    def at(seconds):
        return f"2026-10-07T01:00:{seconds:02d}Z"

    @staticmethod
    def entry(identity, agent, nickname):
        return {"id": identity, "agent_id": agent, "nickname": nickname}

    def node(self, session, agent, turn, start, end, parent=None, request="request", wave=0):
        return {"id": session + ":" + turn, "session_id": session, "turn_id": turn, "agent_id": agent,
                "root_turn_id": request, "wave": wave, "parent_id": parent,
                "started_at": self.at(start), "finished_at": self.at(end) if end is not None else None,
                "status": "completed" if end is not None else "running"}

    def record(self, identity="call", **changes):
        return {"id": identity, "at": self.at(10), "type": "message", "from_agent": "/root/backend",
                "to_agent": "/root/review", "message": "Public result", "encrypted": False,
                "record_kind": "collaboration_call", "source": "codex_session", "project_path": "/project",
                "session_id": "sender", "sender_session_id": "sender", "sender_turn_id": "one", **changes}

    def card(self, identity="delivery", *records, **changes):
        records = records or (self.record(**changes),)
        return {**records[0], "id": identity, "records": list(records), "record_count": len(records), **changes}

    def project(self, *cards, nodes=None, dispatches=()):
        return graph_relations(nodes if nodes is not None else self.nodes, self.entries, "root", list(cards), dispatches)

    def routed(self, relations, kind="message"):
        return [relation for relation in relations if relation["kind"] == kind]

    def test_secondary_receipt_turn_connects_first_recipient_after_later_turn_exists(self):
        journal = self.record("journal", record_kind="sender_journal", at=self.at(9), type="result")
        call = self.record("call", encrypted=True, message="cipher marker", delivery_key="opaque-key")
        receipt = self.record("receipt", session_id="recipient", record_kind="agent_message", encrypted=True,
                              at=self.at(11), sender_session_id=None, sender_turn_id=None,
                              recipient_turn_id="first", delivery_key="opaque-key")
        cards = group_message_records([journal, call, receipt], {node["id"]: node for node in self.nodes})
        # The old journal needs a direct recorder binding to correlate to call.
        journal["recorder_session_id"] = "sender"
        cards = group_message_records([journal, call, receipt], {node["id"]: node for node in self.nodes})
        relations, endpoints = self.project(*cards)
        result = self.routed(relations, "result")[0]
        self.assertEqual((result["from_node_id"], result["to_node_id"]), ("sender:one", "recipient:first"))
        self.assertEqual(result["record_ids"], ["call", "journal", "receipt"])
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["resolution"], "resolved")
        self.assertFalse(endpoints)
        self.assertTrue(any(ref.get("id") == "receipt" and ref.get("side") == "recipient" for ref in result["refs"]))

    def test_explicit_receipt_turn_does_not_depend_on_delivery_time_or_latest_turn(self):
        receipt = self.record("receipt", at=self.at(35), record_kind="agent_message", session_id="recipient",
                              recipient_turn_id="first", sender_session_id=None, sender_turn_id=None)
        relations, _ = self.project(self.card("delivery", self.record(), receipt))
        self.assertEqual(self.routed(relations)[0]["to_node_id"], "recipient:first")

    def test_conflicting_recipient_turns_preserve_relation_to_unresolved_endpoint(self):
        records = [self.record("first", recipient_session_id="recipient", recipient_turn_id="first"),
                   self.record("later", record_kind="agent_message", session_id="recipient", sender_session_id=None,
                               sender_turn_id=None, recipient_turn_id="later")]
        relations, endpoints = self.project(self.card("conflict", *records))
        relation = self.routed(relations)[0]
        self.assertEqual(relation["resolution"], "conflicting")
        self.assertEqual(endpoints[0]["kind"], "unresolved")
        self.assertEqual(relation["to_node_id"], endpoints[0]["id"])
        self.assertNotEqual(relation["to_node_id"], "recipient:later")
        self.assertEqual(relation["record_ids"], ["first", "later"])

    def test_journal_original_conflict_is_not_hidden_by_canonical_metadata(self):
        record = self.record(recipient_session_id="recipient", recipient_turn_id="first", journal_origins=[{
            "source_id": "legacy", "source_seq": 7, "original_event": {
                "recipient_session_id": "recipient", "recipient_turn_id": "later"}}])
        relations, endpoints = self.project(self.card("conflict", record))
        self.assertEqual(self.routed(relations)[0]["resolution"], "conflicting")
        self.assertTrue(any(ref.get("id") == "journal-origin:legacy:7" for ref in self.routed(relations)[0]["refs"]))
        self.assertEqual(endpoints[0]["kind"], "unresolved")

    def test_unknown_explicit_turn_never_falls_back_to_active_recipient(self):
        relations, endpoints = self.project(self.card(recipient_session_id="recipient", recipient_turn_id="missing"))
        self.assertEqual(self.routed(relations)[0]["resolution"], "unresolved")
        self.assertEqual(endpoints[0]["candidate_node_ids"], ["recipient:first", "recipient:later"])

    def test_missing_turn_uses_only_unique_observed_execution_interval(self):
        relations, endpoints = self.project(self.card())
        self.assertEqual(self.routed(relations)[0]["to_node_id"], "recipient:first")
        self.assertEqual(self.routed(relations)[0]["resolution"], "inferred")
        self.assertFalse(endpoints)

    def test_journal_sender_turn_inferred_from_interval_is_not_called_explicit(self):
        record = self.record(record_kind="sender_journal", sender_turn_inferred=True,
                             recipient_session_id="recipient", recipient_turn_id="first")
        relations, endpoints = self.project(self.card("journal", record))
        self.assertEqual(self.routed(relations)[0]["resolution"], "inferred")
        self.assertTrue(any(ref.get("side") == "sender" and ref.get("basis") == "unique_execution_interval"
                            for ref in self.routed(relations)[0]["refs"]))
        self.assertFalse(endpoints)

    def test_simultaneous_intervals_remain_unresolved(self):
        nodes = copy.deepcopy(self.nodes)
        nodes[-1].update(started_at=self.at(3), finished_at=self.at(25))
        relations, endpoints = self.project(self.card(), nodes=nodes)
        self.assertEqual(self.routed(relations)[0]["resolution"], "unresolved")
        self.assertEqual(set(endpoints[0]["candidate_node_ids"]), {"recipient:first", "recipient:later"})

    def test_adjacent_turns_at_exact_boundary_are_ambiguous(self):
        nodes = copy.deepcopy(self.nodes)
        nodes[2]["finished_at"], nodes[3]["started_at"] = self.at(10), self.at(10)
        relations, endpoints = self.project(self.card(), nodes=nodes)
        self.assertEqual(self.routed(relations)[0]["resolution"], "unresolved")
        self.assertTrue(endpoints)

    def test_gap_does_not_select_previous_or_next_turn(self):
        relations, endpoints = self.project(self.card(at=self.at(27)))
        self.assertEqual(self.routed(relations)[0]["resolution"], "unresolved")
        self.assertTrue(endpoints)

    def test_external_counterpart_preserves_route_without_fake_execution_node(self):
        relations, endpoints = self.project(self.card(to_agent="/outside/unknown"))
        relation = self.routed(relations)[0]
        self.assertEqual(relation["from_node_id"], "sender:one")
        self.assertEqual(endpoints[0]["agent_id"], "/outside/unknown")
        self.assertEqual(endpoints[0]["node_kind"], "relation_endpoint")
        self.assertFalse(any(node["id"] == endpoints[0]["id"] for node in self.nodes))

    def test_session_agent_mismatch_is_conflicting_not_silently_corrected(self):
        relations, endpoints = self.project(self.card(recipient_session_id="sender", recipient_turn_id="first"))
        self.assertEqual(self.routed(relations)[0]["resolution"], "conflicting")
        self.assertTrue(endpoints)

    def test_routing_conflict_in_secondary_record_is_preserved(self):
        relation, endpoints = self.project(self.card("delivery", self.record(), self.record("wrong", to_agent="/root")))
        self.assertEqual(self.routed(relation)[0]["resolution"], "conflicting")
        self.assertTrue(endpoints)

    def test_parent_assignment_augments_delegation_without_second_line(self):
        record = self.record("assign-recipient", type="assignment", from_agent="/root", session_id="root",
                             sender_session_id="root", sender_turn_id="request", recipient_session_id="recipient")
        relations, endpoints = self.project(self.card("assignment", record))
        target = [r for r in self.routed(relations, "delegation") if r["to_node_id"] == "recipient:first"][0]
        self.assertEqual(target["count"], 1)
        self.assertEqual(target["message_ids"], ["assignment"])
        self.assertIn("assign-recipient", target["record_ids"])
        self.assertFalse(self.routed(relations, "assignment"))
        self.assertFalse(endpoints)

    def test_peer_assignment_stays_distinct_from_parent_delegation(self):
        relations, endpoints = self.project(self.card(type="assignment", recipient_session_id="recipient", recipient_turn_id="first"))
        peer = self.routed(relations, "assignment")[0]
        self.assertEqual((peer["from_node_id"], peer["to_node_id"]), ("sender:one", "recipient:first"))
        self.assertTrue(any(r["to_node_id"] == "recipient:first" for r in self.routed(relations, "delegation")))
        self.assertFalse(endpoints)

    def test_assignment_with_conflicting_root_request_cannot_claim_observed_turn(self):
        relations, endpoints = self.project(self.card(type="assignment", recipient_session_id="recipient",
            recipient_turn_id="first", root_turn_id="unrelated-request"))
        relation = self.routed(relations, "assignment")[0]
        self.assertEqual(relation["resolution"], "conflicting")
        self.assertTrue(endpoints)
        self.assertFalse(any(r["message_ids"] for r in self.routed(relations, "delegation")))

    def test_delegation_survives_missing_assignment_body(self):
        relations, endpoints = self.project(dispatches=[{"id": "assign-recipient", "at": self.at(1)}])
        relation = next(r for r in relations if r["to_node_id"] == "recipient:first")
        self.assertEqual(relation["kind"], "delegation")
        self.assertEqual(relation["count"], 0)
        self.assertEqual(relation["record_ids"], ["assign-recipient"])
        self.assertFalse(endpoints)

    def test_reverse_direction_and_self_message_are_separate_relations(self):
        forward = self.card("forward", recipient_session_id="recipient", recipient_turn_id="first")
        reverse = self.card("reverse", self.record("reverse-call", from_agent="/root/review", to_agent="/root/backend",
            session_id="recipient", sender_session_id="recipient", sender_turn_id="first",
            recipient_session_id="sender", recipient_turn_id="one"))
        self_send = self.card("self", to_agent="/root/backend", recipient_session_id="sender", recipient_turn_id="one")
        relations, endpoints = self.project(forward, reverse, self_send)
        self.assertEqual({(r["from_node_id"], r["to_node_id"]) for r in self.routed(relations)}, {
            ("sender:one", "recipient:first"), ("recipient:first", "sender:one"), ("sender:one", "sender:one")})
        self.assertFalse(endpoints)

    def test_cipher_only_delivery_is_retained_without_decoding_body(self):
        call = self.record("call", encrypted=True, delivery_key="opaque", message="cipher placeholder")
        receipt = self.record("receipt", encrypted=True, delivery_key="opaque", message="cipher placeholder",
            record_kind="agent_message", session_id="recipient", sender_session_id=None, sender_turn_id=None,
            recipient_turn_id="first")
        cards = group_message_records([call, receipt], {node["id"]: node for node in self.nodes})
        relations, endpoints = self.project(*cards)
        relation = self.routed(relations)[0]
        self.assertEqual(relation["to_node_id"], "recipient:first")
        self.assertEqual(relation["count"], 1)
        self.assertTrue(relation["deliveries"][0]["encrypted"])
        self.assertFalse(endpoints)

    def test_duplicate_presentation_card_does_not_count_as_retransmission(self):
        card = self.card(recipient_session_id="recipient", recipient_turn_id="first")
        relations, _ = self.project(card, copy.deepcopy(card))
        self.assertEqual(self.routed(relations)[0]["count"], 1)

    def test_actual_retransmissions_at_same_timestamp_preserve_original_ids_and_count(self):
        first = self.card("first", self.record("call1", recipient_session_id="recipient", recipient_turn_id="first"))
        second = self.card("second", self.record("call2", recipient_session_id="recipient", recipient_turn_id="first"))
        relations, endpoints = self.project(first, second)
        relation = self.routed(relations)[0]
        self.assertEqual(relation["count"], 2)
        self.assertEqual(relation["message_ids"], ["first", "second"])
        self.assertEqual(relation["record_ids"], ["call1", "call2"])
        self.assertEqual(relation["first_at"], relation["last_at"])
        self.assertFalse(endpoints)

    def test_same_agent_next_turn_gets_distinct_pair(self):
        first = self.card("first", recipient_session_id="recipient", recipient_turn_id="first")
        later = self.card("later", self.record("later-call", recipient_session_id="recipient", recipient_turn_id="later"))
        relations, _ = self.project(first, later)
        self.assertEqual({r["to_node_id"] for r in self.routed(relations)}, {"recipient:first", "recipient:later"})

    def test_user_request_progress_and_final_share_wave_endpoint_without_execution_count(self):
        request = self.card("request", self.record("user-source", type="request", from_agent="user", to_agent="/root",
            record_kind="user_request", session_id="root", sender_session_id=None, sender_turn_id=None,
            recipient_session_id="root", recipient_turn_id="request"))
        progress = self.card("progress", self.record("progress-source", type="progress", from_agent="/root", to_agent="user",
            record_kind="assistant_progress", session_id="root", sender_session_id="root", sender_turn_id="request"))
        result = self.card("result", self.record("final-source", type="result", from_agent="/root", to_agent="user",
            record_kind="task_complete", session_id="root", sender_session_id="root", sender_turn_id="request"))
        original_nodes = copy.deepcopy(self.nodes)
        relations, endpoints = self.project(request, progress, result)
        self.assertEqual(len(endpoints), 1)
        self.assertEqual(endpoints[0]["kind"], "user")
        self.assertEqual({r["kind"] for r in relations}, {"delegation", "request", "progress", "result"})
        self.assertEqual(self.nodes, original_nodes)
        self.assertEqual(self.routed(relations, "request")[0]["from_node_id"], endpoints[0]["id"])
        self.assertEqual(self.routed(relations, "result")[0]["to_node_id"], endpoints[0]["id"])

    def test_user_endpoints_are_separate_per_root_request_wave(self):
        nodes = self.nodes + [self.node("root", "/root", "next", 51, 59, request="next", wave=1)]
        first = self.card("first", from_agent="/root", to_agent="user", session_id="root", sender_session_id="root", sender_turn_id="request")
        second = self.card("second", self.record("second-record", from_agent="/root", to_agent="user", session_id="root",
            sender_session_id="root", sender_turn_id="next", at=self.at(55)))
        relations, endpoints = self.project(first, second, nodes=nodes)
        self.assertEqual({endpoint["wave"] for endpoint in endpoints}, {0, 1})
        self.assertEqual(len({r["to_node_id"] for r in self.routed(relations)}), 2)

    def test_projection_is_stable_and_does_not_mutate_inputs(self):
        cards = [self.card("receipt", self.record(recipient_session_id="recipient", recipient_turn_id="first"))]
        original = copy.deepcopy((cards, self.nodes, self.entries))
        self.assertEqual(self.project(*cards), self.project(*cards))
        self.assertEqual((cards, self.nodes, self.entries), original)

    def test_progress_journal_call_receipt_is_one_typed_delivery(self):
        journal = self.record("progress-journal", type="progress", record_kind="sender_journal",
                              recorder_session_id="sender", at=self.at(9))
        call = self.record("progress-call", encrypted=True, message="opaque progress", delivery_key="progress-key")
        receipt = self.record("progress-receipt", record_kind="agent_message", encrypted=True,
            message="opaque progress", delivery_key="progress-key", session_id="recipient", sender_session_id=None,
            sender_turn_id=None, recipient_turn_id="first", at=self.at(11))
        cards = group_message_records([journal, call, receipt], {node["id"]: node for node in self.nodes})
        self.assertEqual(len(cards), 1)
        self.assertEqual((cards[0]["type"], cards[0]["record_count"]), ("progress", 3))
        self.assertEqual(cards[0]["message"], journal["message"])
        relations, _ = self.project(*cards)
        relation = self.routed(relations, "progress")[0]
        self.assertEqual(relation["count"], 1)
        self.assertEqual(set(relation["record_ids"]), {journal["id"], call["id"], receipt["id"]})
        self.assertEqual(relation["to_node_id"], "recipient:first")

    def test_verified_plaintext_call_keeps_declared_progress_and_result_kind(self):
        for kind in ["progress", "result"]:
            with self.subTest(kind=kind):
                journal = self.record("journal", type=kind, record_kind="sender_journal",
                    recorder_session_id="sender", at=self.at(9), collaboration_call_id="call")
                call = self.record("call")
                cards = group_message_records([journal, call], {node["id"]: node for node in self.nodes})
                self.assertEqual(len(cards), 1)
                self.assertEqual((cards[0]["type"], cards[0]["record_count"]), (kind, 2))
                self.assertEqual(cards[0]["message"], call["message"])

    def test_competing_plaintext_call_prevents_progress_journal_guess(self):
        journal = self.record("journal", type="progress", record_kind="sender_journal",
                              recorder_session_id="sender", at=self.at(9))
        plain = self.record("plaintext", at=self.at(10))
        call = self.record("opaque-call", encrypted=True, delivery_key="opaque-key", message="cipher", at=self.at(11))
        receipt = self.record("opaque-receipt", record_kind="agent_message", encrypted=True,
            delivery_key="opaque-key", message="cipher", session_id="recipient", sender_session_id=None,
            sender_turn_id=None, recipient_turn_id="first", at=self.at(12))
        cards = group_message_records([journal, plain, call, receipt], {node["id"]: node for node in self.nodes})
        self.assertEqual(len(cards), 3)
        self.assertEqual(next(c for c in cards if c["type"] == "progress")["record_count"], 1)
        self.assertEqual({r["id"] for c in cards for r in c["records"]}, {"journal", "plaintext", "opaque-call", "opaque-receipt"})

    def test_same_progress_body_retransmissions_remain_two_deliveries(self):
        self.nodes = self.nodes[:3]
        self.nodes[1]["finished_at"] = self.nodes[2]["finished_at"] = None
        records = []
        for index, seconds in enumerate([9, 45]):
            records.extend([
                self.record(f"journal-{index}", type="progress", record_kind="sender_journal",
                    recorder_session_id="sender", at=self.at(seconds)),
                self.record(f"call-{index}", encrypted=True, message="opaque", delivery_key=f"key-{index}", at=self.at(seconds+1)),
                self.record(f"receipt-{index}", record_kind="agent_message", encrypted=True, message="opaque",
                    delivery_key=f"key-{index}", session_id="recipient", sender_session_id=None,
                    sender_turn_id=None, recipient_turn_id="first", at=self.at(seconds+2))])
        cards = group_message_records(records, {node["id"]: node for node in self.nodes})
        self.assertEqual(len(cards), 2)
        self.assertTrue(all(c["type"] == "progress" and c["record_count"] == 3 for c in cards))
        relations, _ = self.project(*cards)
        relation = self.routed(relations, "progress")[0]
        self.assertEqual((relation["count"], len(relation["record_ids"])), (2, 6))

    def test_user_commentary_is_not_a_peer_progress_journal(self):
        journal = self.record("journal", type="progress", record_kind="sender_journal",
                              recorder_session_id="sender", at=self.at(9))
        commentary = self.record("commentary", type="progress", record_kind="assistant_progress", to_agent="user", at=self.at(9))
        call = self.record("call", encrypted=True, message="cipher", delivery_key="key")
        receipt = self.record("receipt", record_kind="agent_message", encrypted=True, message="cipher",
            delivery_key="key", session_id="recipient", sender_session_id=None, sender_turn_id=None,
            recipient_turn_id="first", at=self.at(11))
        cards = group_message_records([journal, commentary, call, receipt], {node["id"]: node for node in self.nodes})
        self.assertEqual(len(cards), 2)
        self.assertEqual(next(c for c in cards if c["to_agent"] == "user")["records"][0]["id"], "commentary")
        self.assertEqual(next(c for c in cards if c["to_agent"] == "/root/review")["record_count"], 3)


class GraphRelationIntegrationChecks(unittest.TestCase):
    def test_public_user_request_gets_original_card_and_separate_endpoint(self):
        collector = Collector.__new__(Collector)
        collector.lock = threading.RLock()
        collector.messages, collector.activity, collector.turns, collector.dispatches = {}, {}, {}, {}
        collector.revision, collector.project, collector.root_session = 0, "/project", "root"
        collector.error, collector.last_success_at = None, None
        root = {"id": "root", "cwd": "/project", "agent_id": "/root", "nickname": "Coordinator", "parent_id": None,
                "created_at": "2026-10-07T00:59:00Z", "status": "unknown", "last_activity_at": "2026-10-07T00:59:00Z", "path": "root.jsonl"}
        collector.files = {"root": root}
        collector.ingest(root, {"timestamp": "2026-10-07T01:00:00Z", "type": "event_msg", "ordinal": 1,
                               "payload": {"type": "task_started", "turn_id": "one", "root_turn_id": "one"}})
        collector.ingest(root, {"timestamp": "2026-10-07T01:00:01Z", "type": "response_item", "ordinal": 2,
            "payload": {"id": "user-public-request", "type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "<environment_context>metadata</environment_context>\n## My request:\nShow all relations"}]}})
        state = collector.augment({"events": [], "nodes": [], "revision": 1, "title": "Plan", "run_id": "plan",
            "started_at": None, "updated_at": None})
        self.assertEqual(len(state["nodes"]), 1)
        self.assertEqual(state["live"]["running_agents"], 1)
        self.assertEqual(state["messages"], [])
        self.assertEqual(state["graph_requests"][0]["id"], "user-public-request")
        self.assertEqual(state["graph_requests"][0]["message"], "Show all relations")
        self.assertIn("<environment_context>metadata</environment_context>", state["graph_requests"][0]["records"][0]["source_body"])
        self.assertEqual(state["graph_endpoints"][0]["kind"], "user")
        self.assertEqual(state["graph_relations"][0]["to_node_id"], "root:one")
        self.assertEqual(state["graph_relations"][0]["record_ids"], ["user-public-request"])


if __name__ == "__main__":
    unittest.main()
