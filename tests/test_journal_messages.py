import copy
import unittest

from collector import group_message_records


class SenderJournalDisplayChecks(unittest.TestCase):
    def setUp(self):
        self.turns = {"sender:turn": {"session_id": "sender", "turn_id": "turn",
                      "started_at": "2026-10-06T01:00:00Z", "finished_at": "2026-10-06T01:10:00Z"}}
        common = {"from_agent": "/root", "to_agent": "/root/review", "project_path": "/project",
                  "sender_session_id": "sender", "sender_turn_id": "turn", "session_id": "sender"}
        self.journal = {**common, "id": "journal:48", "at": "2026-10-06T01:01:15.776+00:00",
                        "type": "message", "record_kind": "sender_journal", "source": "sender_journal",
                        "recorder_session_id": "sender", "encrypted": False,
                        "message": "Build succeeded.\n전체 공개 원문을 표시합니다."}
        self.call = {**common, "id": "call", "at": "2026-10-06T01:01:22.417Z", "type": "message",
                     "record_kind": "collaboration_call", "source": "codex_session", "encrypted": True,
                     "delivery_key": "opaque-token", "message": "본문 암호화"}
        self.receipt = {"id": "receipt", "at": "2026-10-06T01:01:22.426Z", "type": "message",
                        "record_kind": "agent_message", "source": "codex_session", "encrypted": True,
                        "delivery_key": "opaque-token", "message": "본문 암호화", "project_path": "/project",
                        "from_agent": "/root", "to_agent": "/root/review", "session_id": "recipient"}

    def group(self, *records):
        return group_message_records(list(records), self.turns)

    def test_journal_displays_body_and_preserves_three_originals_without_decryption(self):
        records = [self.call, self.receipt, self.journal]
        before = copy.deepcopy(records)
        cards = self.group(*records)
        self.assertEqual(len(cards), 1)
        card = cards[0]
        self.assertEqual(card["message"], self.journal["message"])
        self.assertFalse(card["encrypted"])
        self.assertEqual(card["message_source"], "sender_journal")
        self.assertEqual(card["message_source_record_id"], "journal:48")
        self.assertEqual(card["journal_match"], "inferred")
        self.assertIn("본문 일치 미검증", card["correlation_basis"])
        self.assertEqual(card["record_count"], 3)
        self.assertEqual(card["records"], [self.journal, self.call, self.receipt])
        self.assertEqual(records, before)
        self.assertEqual(sum(r["encrypted"] for r in card["records"]), 2)

    def test_receive_can_arrive_later_without_changing_journal_card_identity(self):
        early = self.group(self.journal, self.call)[0]
        late = self.group(self.receipt, self.call, self.journal)[0]
        self.assertEqual(early["id"], late["id"])
        self.assertEqual(early["message"], late["message"])
        self.assertEqual(early["at"], late["at"])
        self.assertEqual(early["record_count"], 2)
        self.assertEqual(late["record_count"], 3)

    def test_journal_can_arrive_in_snapshot_after_encrypted_pair(self):
        encrypted = self.group(self.call, self.receipt)[0]
        resolved = self.group(self.journal, self.call, self.receipt)[0]
        self.assertEqual(encrypted["id"], resolved["id"])
        self.assertTrue(encrypted["encrypted"])
        self.assertFalse(resolved["encrypted"])

    def test_two_journals_for_one_call_are_not_resolved_by_nearest_time(self):
        other = dict(self.journal, id="journal:49", at="2026-10-06T01:01:21Z", message="Different body")
        cards = self.group(self.journal, other, self.call, self.receipt)
        self.assertEqual(len(cards), 3)
        self.assertTrue(next(c for c in cards if c.get("delivery_key"))["encrypted"])
        self.assertEqual(sum(c["record_count"] for c in cards), 4)

    def test_one_journal_for_two_calls_is_ambiguous_even_with_plaintext_competitor(self):
        for encrypted in (True, False):
            with self.subTest(encrypted=encrypted):
                other = dict(self.call, id="other", at="2026-10-06T01:01:23Z", delivery_key="other-token",
                             encrypted=encrypted, message="Different public body" if not encrypted else "본문 암호화")
                cards = self.group(self.journal, self.call, self.receipt, other)
                self.assertEqual(len(cards), 3)
                self.assertFalse(any(c.get("message_source") for c in cards))

    def test_scope_and_sender_ownership_must_match(self):
        for changes in ({"project_path": "/other"}, {"project_path": None},
                        {"sender_session_id": "other"}, {"sender_turn_id": "later"},
                        {"sender_turn_id": None}, {"from_agent": "/other"}, {"to_agent": "/other"},
                        {"recorder_session_id": "coordinator"}, {"recorder_session_id": None},
                        {"type": "assignment"}):
            with self.subTest(changes=changes):
                cards = self.group(dict(self.journal, **changes), self.call, self.receipt)
                self.assertEqual(len(cards), 2)
                self.assertFalse(any(c.get("message_source") for c in cards))

    def test_assignment_journal_uses_outgoing_kind_even_when_receipt_is_message(self):
        cards = self.group(dict(self.journal, type="assignment"), dict(self.call, type="assignment"), self.receipt)
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["type"], "assignment")
        self.assertEqual(next(r for r in cards[0]["records"] if r["id"] == "receipt")["type"], "message")

    def test_preceding_window_and_observed_turn_boundaries_are_required(self):
        for at in ("2026-10-06T01:00:52Z", "2026-10-06T01:01:23Z", "invalid"):
            with self.subTest(at=at):
                self.assertEqual(len(self.group(dict(self.journal, at=at), self.call, self.receipt)), 2)
        for turn in ({}, {"started_at": "2026-10-06T01:01:16Z"},
                     {"started_at": "2026-10-06T01:00:00Z", "finished_at": "2026-10-06T01:01:20Z"},
                     {"started_at": "2026-10-06T01:00:00Z", "finished_at": "invalid"}):
            with self.subTest(turn=turn):
                self.turns = {"sender:turn": turn}
                self.assertEqual(len(self.group(self.journal, self.call, self.receipt)), 2)

    def test_no_sender_call_or_mismatched_delivery_token_cannot_claim_receipt(self):
        self.assertEqual(len(self.group(self.journal, self.receipt)), 2)
        cards = self.group(self.journal, self.call, dict(self.receipt, delivery_key="other-token"))
        self.assertEqual(len(cards), 2)
        self.assertEqual(next(c for c in cards if c.get("message_source"))["record_count"], 2)
        self.assertTrue(next(c for c in cards if c["id"] == "receipt")["encrypted"])

    def test_empty_and_redacted_journals_do_not_claim_readable_body(self):
        for body in ("", "   \n", "[키 숨김]", "[암호화된 본문]"):
            with self.subTest(body=body):
                cards = self.group(dict(self.journal, message=body), self.call, self.receipt)
                self.assertFalse(any(c.get("message_source") for c in cards))

    def test_separate_resends_keep_two_cards_even_with_identical_public_bodies(self):
        later = [dict(r, id="later:" + r["id"], at=r["at"].replace("01:01", "01:02"),
                      **({"delivery_key": "later-token"} if r.get("delivery_key") else {}))
                 for r in (self.journal, self.call, self.receipt)]
        cards = self.group(self.journal, self.call, self.receipt, *later)
        self.assertEqual(len(cards), 2)
        self.assertEqual([c["record_count"] for c in cards], [3, 3])
        self.assertNotEqual(cards[0]["id"], cards[1]["id"])

    def test_repeated_opaque_tokens_do_not_merge_retransmissions(self):
        repeat = dict(self.call, id="repeat", at="2026-10-06T01:01:23Z")
        cards = self.group(self.journal, self.call, repeat, self.receipt)
        self.assertEqual(len(cards), 4)
        self.assertEqual({r["id"] for c in cards for r in c["records"]}, {"journal:48", "call", "repeat", "receipt"})

    def test_explicit_result_journal_is_not_consumed_by_automatic_final_report(self):
        complete = dict(self.call, id="complete", at="2026-10-06T01:01:25Z", encrypted=False,
                        type="result", record_kind="task_complete", message=self.journal["message"])
        cards = self.group(dict(self.journal, type="result"), self.call, self.receipt, complete)
        self.assertEqual(len(cards), 2)
        sent = next(c for c in cards if c.get("message_source"))
        self.assertEqual(sent["type"], "result")
        self.assertEqual(sent["record_count"], 3)
        self.assertEqual(next(r for r in sent["records"] if r["id"] == "call")["type"], "message")
        self.assertEqual(next(c for c in cards if c["record_kind"] == "task_complete")["record_count"], 1)

    def test_ambiguous_result_send_journal_stays_separate_from_automatic_final(self):
        complete = dict(self.call, id="complete", encrypted=False, type="result",
                        record_kind="task_complete", message=self.journal["message"])
        repeat = dict(self.call, id="repeat", at="2026-10-06T01:01:23Z", delivery_key="other-token")
        cards = self.group(dict(self.journal, type="result"), self.call, repeat, self.receipt, complete)
        self.assertEqual(len(cards), 4)
        self.assertFalse(any(c.get("message_source") for c in cards))
        self.assertEqual(next(c for c in cards if c["id"] == "journal:48")["record_count"], 1)


class FinalJournalSummaryChecks(unittest.TestCase):
    def setUp(self):
        SenderJournalDisplayChecks.setUp(self)
        self.journal["type"] = "result"
        self.final = {**self.call, "id": "complete", "at": "2026-10-06T01:01:25Z", "encrypted": False,
                      "type": "result", "record_kind": "task_complete", "message": "Final full report\nConfirmed details.",
                      "recipient_session_id": "recipient"}
        self.final.pop("delivery_key")
        self.receipt.update(type="result", encrypted=False, message=self.final["message"])
        self.receipt["at"] = "2026-10-06T01:01:26Z"
        self.receipt.pop("delivery_key")
        self.turns["sender:turn"]["finished_at"] = self.final["at"]

    def group(self, *records):
        return group_message_records(list(records), self.turns)

    def test_different_pre_final_summary_is_preserved_inside_one_final_card(self):
        records = [self.journal, self.final, self.receipt]
        before = copy.deepcopy(records)
        cards = self.group(*records)
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["message"], self.final["message"])
        self.assertEqual(cards[0]["report_group_kind"], "completion_with_journal_summaries")
        self.assertEqual(cards[0]["related_journal_record_ids"], ["journal:48"])
        self.assertEqual(cards[0]["records"], records)
        self.assertEqual(records, before)
        self.assertIn("다른 본문은 출처에 보존", cards[0]["correlation_basis"])

    def test_final_card_identity_survives_summary_discovery(self):
        final = self.group(self.final, self.receipt)[0]
        combined = self.group(self.journal, self.final, self.receipt)[0]
        self.assertEqual(final["id"], combined["id"])
        self.assertEqual(final["message"], combined["message"])

    def test_explicit_result_send_and_final_stay_separate(self):
        cards = self.group(self.journal, self.call, self.final, self.receipt)
        self.assertEqual(len(cards), 2)
        final = next(c for c in cards if c["record_kind"] == "task_complete")
        self.assertNotIn("journal:48", [r["id"] for r in final["records"]])
        self.assertNotIn("report_group_kind", final)

    def test_delayed_explicit_send_outside_journal_window_still_blocks_final_summary(self):
        self.final["at"] = "2026-10-06T01:02:20Z"
        self.turns["sender:turn"]["finished_at"] = self.final["at"]
        call = dict(self.call, at="2026-10-06T01:02:00Z")
        cards = self.group(self.journal, call, self.final)
        self.assertEqual(len(cards), 3)
        self.assertFalse(any(c.get("report_group_kind") for c in cards))

    def test_send_to_another_recipient_also_blocks_pre_final_summary(self):
        other = dict(self.call, to_agent="/root/other")
        cards = self.group(self.journal, other, self.final)
        self.assertEqual(len(cards), 3)
        self.assertFalse(any(c.get("report_group_kind") for c in cards))

    def test_second_completion_before_journal_is_still_ambiguous(self):
        earlier = dict(self.final, id="earlier", at="2026-10-06T01:01:14Z", message="Earlier final body")
        self.assertEqual(len(self.group(self.journal, earlier, self.final)), 3)

    def test_repetition_check_includes_journal_already_attached_to_an_explicit_send(self):
        earlier_journal = dict(self.journal, id="earlier-journal", at="2026-10-06T01:00:15Z")
        earlier_call = dict(self.call, id="earlier-call", at="2026-10-06T01:00:22Z", delivery_key="earlier-token")
        cards = self.group(earlier_journal, earlier_call, self.journal, self.final)
        self.assertEqual(len(cards), 3)
        self.assertFalse(any(c.get("report_group_kind") for c in cards))

    def test_multiple_completions_are_ambiguous(self):
        other = dict(self.final, id="other-complete", at="2026-10-06T01:01:24Z", message="Other final body")
        cards = self.group(self.journal, self.final, other)
        self.assertEqual(len(cards), 3)
        self.assertFalse(any(c.get("report_group_kind") for c in cards))

    def test_repeated_same_journal_body_is_not_silently_removed(self):
        repeat = dict(self.journal, id="journal:49", at="2026-10-06T01:01:16Z")
        cards = self.group(self.journal, repeat, self.final, self.receipt)
        self.assertEqual(len(cards), 3)
        self.assertEqual(sum(c["record_count"] for c in cards), 4)

    def test_scope_sender_ownership_and_observed_completion_are_required(self):
        for changes in ({"project_path": "/other"}, {"project_path": None}, {"to_agent": "/other"},
                        {"sender_turn_id": "other"}, {"sender_session_id": "other"},
                        {"recorder_session_id": "coordinator"}, {"recorder_session_id": None}):
            with self.subTest(changes=changes):
                self.assertEqual(len(self.group(dict(self.journal, **changes), self.final, self.receipt)), 2)
        self.turns["sender:turn"]["finished_at"] = None
        self.assertEqual(len(self.group(self.journal, self.final, self.receipt)), 2)

    def test_late_old_or_redacted_summary_is_not_used(self):
        for changes in ({"at": "2026-10-06T01:01:26Z"}, {"at": "2026-10-06T00:59:24Z"},
                        {"message": "[키 숨김]"}, {"message": "   "}):
            with self.subTest(changes=changes):
                self.assertEqual(len(self.group(dict(self.journal, **changes), self.final, self.receipt)), 2)
        self.final["at"] = "2026-10-06T01:03:16Z"
        self.turns["sender:turn"]["finished_at"] = self.final["at"]
        self.assertEqual(len(self.group(self.journal, self.final)), 2)

    def test_different_actual_final_bodies_are_not_merged_by_turn_alone(self):
        other = dict(self.final, id="final-answer", record_kind="assistant_final_answer", message="Different actual final")
        self.assertEqual(len(self.group(other, self.final)), 2)


if __name__ == "__main__":
    unittest.main()
