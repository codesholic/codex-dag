import copy
import threading
import unittest

import monitor
from collector import Collector, readable_workflow_rows


class PublicProgressChecks(unittest.TestCase):
    def setUp(self):
        self.collector = Collector.__new__(Collector)
        self.collector.messages, self.collector.activity, self.collector.turns, self.collector.dispatches = {}, {}, {}, {}
        self.collector.lock = threading.RLock()
        self.collector.revision, self.collector.error, self.collector.last_success_at = 0, None, None
        self.collector.project, self.collector.root_session = "/project", "root"
        self.entry = {"id": "root", "agent_id": "/root", "cwd": "/project", "parent_id": None,
                      "created_at": "2026-10-06T00:00:00Z", "nickname": "Coordinator", "status": "unknown",
                      "last_activity_at": "2026-10-06T00:00:00Z"}
        self.collector.files = {"root": self.entry}
        self.ordinal = 0
        self.ingest({"type": "task_started", "turn_id": "one"}, row_type="event_msg", at="2026-10-06T01:00:00Z")

    def ingest(self, payload, row_type="response_item", at="2026-10-06T01:00:01Z", entry=None):
        self.ordinal += 1
        self.collector.ingest(entry or self.entry,
                              {"type": row_type, "timestamp": at, "ordinal": self.ordinal, "payload": payload})

    def progress(self, identity="progress", body="확인 중입니다.", **extra):
        return {"type": "message", "id": identity, "role": "assistant", "phase": "commentary",
                "content": [{"type": "output_text", "text": body}], **extra}

    def state(self):
        return self.collector.augment(monitor.empty_state())

    def test_public_progress_is_visible_in_both_lists_before_completion(self):
        body = "작업을 확인하겠습니다.\n" + "전체 공개 진행 문장을 보존합니다. " * 60
        payload = self.progress(body=body)
        before = copy.deepcopy(payload)
        self.ingest(payload)
        state = self.state()
        message = state["messages"][0]
        self.assertEqual((message["type"], message["from_agent"], message["to_agent"]), ("progress", "/root", "user"))
        self.assertEqual((message["message"], message["sender_turn_id"], message["record_kind"]),
                         (body, "one", "assistant_progress"))
        self.assertEqual(message["records"][0]["id"], "progress")
        row = next(e for e in state["execution_events"] if e["type"] == "progress")
        self.assertEqual(row["message"], body)
        self.assertEqual(row["source_event_count"], 1)
        self.assertEqual(row["source_events"][0]["id"], "progress")
        self.assertEqual(payload, before)
        self.assertEqual(state["live"]["running_agents"], 1)
        self.assertEqual(state["live"]["completed_agents"], 0)

    def test_private_phases_channels_reasoning_and_ciphertext_are_not_public_progress(self):
        for payload in [self.progress(phase="analysis"), self.progress(phase="summary"),
                        self.progress(channel="analysis"), self.progress(channel="summary"),
                        {"type": "reasoning", "id": "private", "content": "private reasoning"},
                        self.progress(content=[{"type": "encrypted_content", "encrypted_content": "gAAAAA"+"a"*100}]),
                        self.progress(body=("gAAAAA"+"a"*100)+"\n"+("gAAAAA"+"b"*100)),
                        self.progress(content=[{"type": "output_text", "text": "gAAAAA"+"a"*100},
                                               {"type": "output_text", "text": "gAAAAA"+"b"*100}]),
                        self.progress(body="sk-"+"a"*30),
                        self.progress(body="gAAAAA"+"a"*100), self.progress(body=""), self.progress(phase=None)]:
            self.ingest(payload)
        state = self.state()
        self.assertFalse(state["messages"])
        self.assertFalse(any(e["type"] == "progress" for e in state["events"]))

    def test_child_commentary_is_public_transcript_not_inferred_parent_delivery(self):
        child = {**self.entry, "id": "child", "agent_id": "/root/worker", "parent_id": "root", "nickname": "Worker"}
        self.collector.files["child"] = child
        self.ingest({"type": "task_started", "turn_id": "child-turn", "root_turn_id": "one"}, row_type="event_msg", entry=child)
        self.ingest(self.progress(identity="child-progress"), entry=child)
        message = self.state()["messages"][0]
        self.assertEqual((message["from_agent"], message["to_agent"], message["recipient_kind"]),
                         ("/root/worker", "user", "user"))
        self.assertEqual(message["sender_turn_id"], "child-turn")

    def test_foreign_root_public_messages_do_not_leak_into_selected_lists(self):
        foreign = {**self.entry, "id": "other-root", "current_turn_id": None}
        self.collector.files["other"] = foreign
        self.ingest(self.progress(identity="foreign-progress", body="Other root body"), entry=foreign)
        self.ingest(self.progress(identity="own-progress", body="Selected root body"))
        state = self.state()
        self.assertEqual([m["message"] for m in state["messages"]], ["Selected root body"])
        self.assertEqual([e["message"] for e in state["events"] if e["type"] == "progress"], ["Selected root body"])

    def test_repeated_public_messages_keep_distinct_original_ids(self):
        self.ingest(self.progress(identity="first"))
        self.ingest(self.progress(identity="second"), at="2026-10-06T01:00:02Z")
        state = self.state()
        self.assertEqual(len(state["messages"]), 2)
        self.assertEqual(len([e for e in state["execution_events"] if e["type"] == "progress"]), 2)
        self.assertEqual({r["id"] for r in state["message_records"]}, {"first", "second"})

    def test_public_message_update_keeps_identity_and_does_not_duplicate(self):
        self.ingest(self.progress(body="First text"))
        first = next(e["id"] for e in self.state()["execution_events"] if e["type"] == "progress")
        self.ingest(self.progress(body="Completed public sentence"))
        state = self.state()
        self.assertEqual(len(state["messages"]), 1)
        row = next(e for e in state["execution_events"] if e["type"] == "progress")
        self.assertEqual(row["id"], first)
        self.assertEqual(row["message"], "Completed public sentence")

    def test_progress_and_final_are_distinct_and_completion_ends_execution(self):
        self.ingest(self.progress(body="Same visible sentence"))
        self.ingest({**self.progress(identity="final", body="Same visible sentence"), "phase": "final_answer"},
                    at="2026-10-06T01:00:02Z")
        self.ingest({"type": "task_complete", "turn_id": "one", "last_agent_message": "Same visible sentence"},
                    row_type="event_msg", at="2026-10-06T01:00:03Z")
        state = self.state()
        self.assertEqual([m["type"] for m in state["messages"]], ["progress", "result"])
        self.assertEqual(len([e for e in state["execution_events"] if e["type"] in {"progress", "task_complete"}]), 2)
        self.assertEqual(state["live"]["running_agents"], 0)


class ReadableWorkflowChecks(unittest.TestCase):
    def setUp(self):
        self.nodes = [{"id": "check", "title": "CLI 세션 이름 확인", "status": "completed"}]
        self.event = {"seq": 1, "at": "2026-10-06T01:00:00Z", "type": "node.updated", "node_id": "check",
                      "status": "running", "message": "", "evidence": []}

    def test_blank_update_uses_title_and_original_status_without_rewriting_event(self):
        before = copy.deepcopy(self.event)
        row = readable_workflow_rows([self.event], self.nodes)[0]
        self.assertEqual(row["display_type"], "등록 작업 상태")
        self.assertEqual(row["display_message"], "CLI 세션 이름 확인 · 진행 중")
        self.assertEqual(row["message"], "")
        self.assertEqual(row["type"], "node.updated")
        self.assertEqual(row["source_events"], [before])
        self.assertEqual(self.event, before)

    def test_explicit_update_and_completion_evidence_are_visible_in_full(self):
        event = dict(self.event, status="completed", message="이름 표시 확인 완료", evidence=["150 tests passed", {"file": "report.json"}])
        row = readable_workflow_rows([event], self.nodes)[0]
        self.assertIn("CLI 세션 이름 확인 · 완료\n이름 표시 확인 완료", row["display_message"])
        self.assertIn('검증 근거 · 150 tests passed\n{"file": "report.json"}', row["display_message"])
        self.assertEqual(row["source_events"], [event])

    def test_statusless_update_does_not_borrow_latest_completed_status(self):
        event = {k: v for k, v in self.event.items() if k != "status"}
        event["message"] = "추가 확인 중"
        row = readable_workflow_rows([event], self.nodes)[0]
        self.assertEqual(row["display_message"], "CLI 세션 이름 확인 · 진행 내용 갱신\n추가 확인 중")

    def test_workflow_creation_and_extension_have_truthful_titles(self):
        created = {"seq": 4, "type": "workflow.created", "message": ""}
        extended = {"seq": 5, "type": "workflow.extended", "message": "후속 작업 추가",
                    "nodes": [{"id": "new", "title": "공개 진행 표시"}]}
        rows = readable_workflow_rows([created, extended], self.nodes, {4: "CLI 이름 수정"})
        self.assertEqual(rows[0]["display_message"], "CLI 이름 수정")
        self.assertEqual(rows[1]["display_message"], "후속 작업 추가\n공개 진행 표시")

    def test_unknown_node_falls_back_without_inventing_title_or_changing_other_events(self):
        event = dict(self.event, node_id="missing")
        started = {"type": "task_started", "message": "original lifecycle"}
        rows = readable_workflow_rows([event, started], self.nodes)
        self.assertEqual(rows[0]["display_message"], "등록 작업 · 진행 중")
        self.assertIs(rows[1], started)


if __name__ == "__main__":
    unittest.main()
