import copy
import unittest

from collector import execution_event_rows, group_message_records


class ExecutionEventChecks(unittest.TestCase):
    def setUp(self):
        common = {"type": "result", "message": "Final report\n검증 결과", "encrypted": False,
                  "from_agent": "/root", "to_agent": "user", "project_path": "/project",
                  "session_id": "root", "sender_session_id": "root", "sender_turn_id": "one"}
        self.journal = {**common, "id": "journal:1", "at": "2026-10-06T01:00:01Z",
                        "source": "sender_journal", "record_kind": "sender_journal", "recorder_session_id": "root"}
        self.complete = {**common, "id": "complete", "at": "2026-10-06T01:00:03Z",
                         "source": "codex_session", "record_kind": "task_complete"}
        self.turns = {"root:one": {"session_id": "root", "turn_id": "one", "root_turn_id": "one",
                                  "started_at": "2026-10-06T01:00:00Z", "finished_at": self.complete["at"]}}
        self.journal_event = {"seq": 1, "at": self.journal["at"], "type": "agent.message",
                              "session_id": "root", "from_agent": "/root", "to_agent": "user",
                              "message": self.journal["message"]}
        self.complete_event = {"id": "lifecycle:complete", "at": self.complete["at"], "type": "task_complete",
                               "source": "codex_session", "runtime": True, "agent_id": "/root",
                               "session_id": "root", "turn_id": "one", "message": self.complete["message"]}

    def rows(self, events=None, records=None):
        return execution_event_rows(events or [self.journal_event, self.complete_event],
                                    group_message_records(records or [self.journal, self.complete], self.turns))

    def test_journal_and_completion_have_one_flat_row_with_exact_originals(self):
        events = [self.journal_event, self.complete_event]
        records = [self.journal, self.complete]
        before = copy.deepcopy((events, records))
        rows = self.rows(events, records)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["type"], "task_complete")
        self.assertEqual(row["at"], self.complete_event["at"])
        self.assertEqual(row["message"], self.complete["message"])
        self.assertEqual((row["agent_id"], row["session_id"], row["turn_id"]), ("/root", "root", "one"))
        self.assertEqual((row["from_agent"], row["to_agent"]), ("/root", "user"))
        self.assertEqual(row["original_event_id"], "lifecycle:complete")
        self.assertEqual(row["source_event_count"], 2)
        self.assertEqual(row["source_events"], events)
        self.assertEqual(row["records"], records)
        self.assertEqual((events, records), before)

    def test_row_identity_and_original_ids_survive_completion_arrival(self):
        early = self.rows([self.journal_event], [self.journal])[0]
        late = self.rows()[0]
        self.assertEqual(early["id"], late["id"])
        self.assertEqual(early["type"], "agent.message")
        self.assertEqual(late["type"], "task_complete")
        self.assertIn(early["records"][0]["id"], [r["id"] for r in late["records"]])

    def test_lifecycle_and_registrations_remain_in_chronological_order(self):
        others = [{"id": kind, "at": f"2026-10-06T01:00:0{n}Z", "type": kind, "message": kind}
                  for n, kind in [(0, "task_started"), (2, "node.updated"), (4, "turn_aborted"), (5, "task_failed")]]
        rows = self.rows([others[3], self.complete_event, *others[:3], self.journal_event])
        self.assertEqual([r["type"] for r in rows],
                         ["task_started", "node.updated", "task_complete", "turn_aborted", "task_failed"])
        self.assertTrue(all(event in rows for event in others))

    def test_repeated_journal_deliveries_are_not_deduplicated_by_text(self):
        repeat = dict(self.journal, id="journal:2", type="message")
        first = dict(self.journal, type="message")
        event = dict(self.journal_event, seq=2)
        rows = self.rows([self.journal_event, event], [first, repeat])
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0]["id"], rows[1]["id"])

    def test_separate_turns_with_identical_final_bodies_remain_separate(self):
        second_journal = dict(self.journal, id="journal:2", sender_turn_id="two", at="2026-10-06T02:00:01Z")
        second_complete = dict(self.complete, id="complete-two", sender_turn_id="two", at="2026-10-06T02:00:03Z")
        events = [self.journal_event, self.complete_event,
                  dict(self.journal_event, seq=2, at=second_journal["at"]),
                  dict(self.complete_event, id="lifecycle:complete-two", turn_id="two", at=second_complete["at"])]
        rows = self.rows(events, [self.journal, self.complete, second_journal, second_complete])
        self.assertEqual(len(rows), 2)
        self.assertEqual([r["turn_id"] for r in rows], ["one", "two"])

    def test_duplicate_original_identity_is_ambiguous_and_keeps_all_events(self):
        repeat = dict(self.journal_event, at="2026-10-06T01:00:02Z")
        rows = self.rows([self.journal_event, repeat, self.complete_event])
        self.assertEqual(len(rows), 3)
        self.assertFalse(any(r.get("source_events") for r in rows))

    def test_unrelated_same_text_event_cannot_be_claimed(self):
        unrelated = dict(self.complete_event, id="lifecycle:other-complete", session_id="other")
        rows = self.rows([self.journal_event, unrelated])
        self.assertEqual(len(rows), 2)
        self.assertIn(unrelated, rows)

    def test_shared_source_identity_between_cards_keeps_all_raw_events(self):
        cards = group_message_records([self.journal, self.complete], self.turns)
        cards.append({**cards[0], "id": "other-delivery", "records": [self.journal]})
        events = [self.journal_event, self.complete_event]
        self.assertEqual(execution_event_rows(events, cards), events)

    def test_repeated_record_identity_inside_a_card_does_not_duplicate_an_event(self):
        cards = group_message_records([self.journal, self.complete], self.turns)
        cards[0]["records"].append(copy.deepcopy(self.journal))
        events = [self.journal_event, self.complete_event]
        self.assertEqual(execution_event_rows(events, cards), events)

    def test_explicit_result_send_is_distinct_from_automatic_completion(self):
        call = dict(self.complete, id="call", at="2026-10-06T01:00:02Z",
                    record_kind="collaboration_call", type="message")
        rows = self.rows(records=[self.journal, call, self.complete])
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["type"] for r in rows}, {"agent.message", "task_complete"})

    def test_different_pre_final_summary_is_replaced_only_in_presentation(self):
        summary = dict(self.journal, message="Short summary")
        event = dict(self.journal_event, message=summary["message"])
        rows = self.rows([event, self.complete_event], [summary, self.complete])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["message"], self.complete["message"])
        self.assertEqual(rows[0]["source_events"][0]["message"], "Short summary")

    def test_plain_raw_timeline_is_supported_without_message_cards(self):
        events = [self.complete_event, self.journal_event]
        self.assertEqual(execution_event_rows(events, []), list(reversed(events)))


if __name__ == "__main__":
    unittest.main()
