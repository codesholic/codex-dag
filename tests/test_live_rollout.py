"""Live append contracts, including Windows writer metadata lag.

The source writer remains open throughout each test. Windows-specific tests
freeze the path metadata while real bytes grow, so they run on POSIX too.
"""
import copy
import json
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import monitor
from collector import Collector


def line(kind, payload, at="2026-10-06T01:00:01Z", ending=b"\n"):
    return json.dumps({"timestamp": at, "type": kind, "payload": payload},
                      ensure_ascii=False).encode("utf-8") + ending


def progress(identity, body="작업 진행 중입니다.", ending=b"\n"):
    return line("response_item", {"type": "message", "id": identity,
        "role": "assistant", "phase": "commentary",
        "content": [{"type": "output_text", "text": body}]}, ending=ending)


class LiveRolloutChecks(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.base = Path(self.folder.name).resolve()
        self.project, self.codex, self.data = (self.base / name for name in ("project", "codex", "data"))
        self.project.mkdir(); self.data.mkdir()
        self.path = self.codex / "sessions/2026/10/06/root.jsonl"
        self.path.parent.mkdir(parents=True)
        self.header = line("session_meta", {"id": "root", "cwd": str(self.project)},
                           at="2026-10-06T01:00:00Z")
        self.writer = self.path.open("wb")
        self.append(self.header + line("event_msg", {"type": "task_started", "turn_id": "one"}))
        (self.data / "config.json").write_text(json.dumps({"project_path": str(self.project),
                                                         "root_session_id": "root"}))
        self.collector = Collector(self.data, self.codex, start_thread=False)
        self.collector.scan()
        self.entry = self.collector.files[str(self.path)]

    def tearDown(self):
        self.writer.close()
        self.folder.cleanup()

    def append(self, raw):
        self.writer.write(raw)
        self.writer.flush()

    def state(self):
        return self.collector.augment(monitor.empty_state())

    @contextmanager
    def frozen_path_metadata(self, path=None):
        target = path or self.path
        frozen, real_stat = target.stat(), Path.stat

        def stat(path, *args, **kwargs):
            return frozen if path == target else real_stat(path, *args, **kwargs)

        with patch.object(Path, "stat", stat):
            yield frozen

    def preserve_mtime(self, metadata):
        os.utime(self.path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))

    def assert_progress(self, identity, body):
        state = self.state()
        messages = [item for item in state["messages"] if item["id"] == identity]
        self.assertEqual([item["message"] for item in messages], [body])
        # Public progress is present in the flat execution list with provenance.
        events = [item for item in state["execution_events"]
                  if any(source.get("id") == identity for source in item.get("source_events", []))]
        self.assertEqual([item["message"] for item in events], [body])

    def test_windows_reads_new_request_and_progress_with_both_metadata_fields_frozen(self):
        body = "파일을 닫기 전에 보이는 공개 진행 메시지"
        with patch.object(sys, "platform", "win32"), self.frozen_path_metadata():
            self.append(line("event_msg", {"type": "task_started", "turn_id": "two"}, at="2026-10-06T01:00:02Z")
                        + line("response_item", {"type": "message", "role": "user", "content": [
                            {"type": "input_text", "text": "새 요청을 실시간으로 확인해 줘"}]}, at="2026-10-06T01:00:03Z")
                        + progress("live-progress", body))
            source = self.path.read_bytes()
            before = self.collector.revision
            self.collector.scan()
            self.assertEqual(self.collector.turns["root:two"]["request"], "새 요청을 실시간으로 확인해 줘")
            self.assertGreater(self.collector.revision, before)
            self.assert_progress("live-progress", body)
            self.assertEqual(self.entry["offset"], len(source))
            stable = self.collector.revision
            for _ in range(3):
                self.collector.scan()  # Stale path size is now smaller than the consumed offset.
                self.assertEqual(self.collector.revision, stable)
            self.assertEqual(self.path.read_bytes(), source)

    def test_posix_size_growth_is_collected_even_with_same_mtime(self):
        metadata = self.path.stat()
        self.append(progress("posix-growth"))
        self.preserve_mtime(metadata)
        with patch.object(sys, "platform", "linux"):
            self.collector.scan()
        self.assert_progress("posix-growth", "작업 진행 중입니다.")
        self.assertEqual(self.entry["offset"], self.path.stat().st_size)

    def test_windows_incomplete_utf8_and_crlf_are_retried_from_exact_offset(self):
        for ending in (b"\n", b"\r\n"):
            identity, body = "split-" + str(len(ending)), "한글 메시지와 emoji 🧪"
            raw = progress(identity, body, ending)
            split = raw.index("한".encode()) + 1  # Cut inside a three-byte UTF-8 code point.
            offset, revision = self.entry["offset"], self.collector.revision
            with self.subTest(ending=ending), patch.object(sys, "platform", "win32"), self.frozen_path_metadata():
                for chunk in (raw[:split], raw[split:-1]):
                    self.append(chunk)
                    self.collector.scan()
                    self.assertEqual(self.entry["offset"], offset)
                    self.assertEqual(self.collector.revision, revision)
                    self.assertNotIn(identity, self.collector.messages)
                self.append(raw[-1:])
                self.collector.scan()
                self.assert_progress(identity, body)
                self.assertEqual(self.entry["offset"], offset + len(raw))
                stable = self.collector.revision
                self.collector.scan()
                self.assertEqual(self.collector.revision, stable)

    def test_posix_incomplete_tail_can_finish_without_mtime_change(self):
        metadata, offset = self.path.stat(), self.entry["offset"]
        raw = progress("posix-tail")
        with patch.object(sys, "platform", "linux"):
            self.append(raw[:-1]); self.preserve_mtime(metadata)
            self.collector.scan()
            self.assertEqual(self.entry["offset"], offset)
            before = self.collector.revision
            self.append(raw[-1:]); self.preserve_mtime(metadata)
            self.collector.scan()
            self.assertGreater(self.collector.revision, before)
            self.assert_progress("posix-tail", "작업 진행 중입니다.")

    def test_same_mtime_truncation_reports_error_and_keeps_prior_evidence(self):
        self.append(progress("kept")); self.collector.scan()
        metadata, offset = self.path.stat(), self.entry["offset"]
        prior = copy.deepcopy(self.collector.messages)
        self.writer.truncate(len(self.header)); self.writer.flush()
        self.preserve_mtime(metadata)
        with patch.object(sys, "platform", "linux"), self.assertRaisesRegex(ValueError, "truncated.*preserved"):
            self.collector.scan()
        self.assertEqual(self.entry["offset"], offset)
        self.assertEqual(self.collector.messages, prior)

    def test_windows_truncation_uses_open_handle_size_instead_of_stale_stat(self):
        self.append(progress("kept")); self.collector.scan()
        offset, prior = self.entry["offset"], copy.deepcopy(self.collector.messages)
        with patch.object(sys, "platform", "win32"), self.frozen_path_metadata():
            self.writer.truncate(len(self.header)); self.writer.flush()
            with self.assertRaisesRegex(ValueError, "truncated.*preserved"):
                self.collector.scan()
        self.assertEqual(self.entry["offset"], offset)
        self.assertEqual(self.collector.messages, prior)

    def test_unchanged_probe_and_metadata_only_touch_keep_revision_stable(self):
        for platform in ("win32", "linux"):
            with self.subTest(platform=platform), patch.object(sys, "platform", platform):
                before = self.collector.revision
                for _ in range(3):
                    self.collector.scan()
                    self.assertEqual(self.collector.revision, before)
                metadata = self.path.stat()
                os.utime(self.path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 1_000_000_000))
                self.collector.scan()
                self.assertEqual(self.collector.revision, before)

    def test_successful_lines_commit_offset_before_a_complete_malformed_record(self):
        offset = self.entry["offset"]
        good = progress("before-malformed")
        self.append(good + b"{malformed complete line}\n")
        for _ in range(2):
            with self.assertRaisesRegex(ValueError, "Malformed rollout JSON"):
                self.collector.scan()
            self.assertEqual(self.entry["offset"], offset + len(good))
            self.assert_progress("before-malformed", "작업 진행 중입니다.")

    def test_complete_invalid_utf8_is_an_error_not_an_incomplete_tail(self):
        offset = self.entry["offset"]
        self.append(b'{"invalid":"\xff"}\n')
        with self.assertRaisesRegex(ValueError, "Malformed rollout JSON"):
            self.collector.scan()
        self.assertEqual(self.entry["offset"], offset)

    def test_partial_header_waits_for_newline_before_selected_child_discovery(self):
        child = self.path.with_name("child.jsonl")
        header = line("session_meta", {"id": "child", "cwd": str(self.project), "agent_nickname": "작업자",
            "source": {"subagent": {"thread_spawn": {"parent_thread_id": "root", "agent_path": "/root/child"}}}},
            at="2026-10-06T01:00:00Z", ending=b"\r\n")
        with child.open("wb") as writer, patch.object(sys, "platform", "win32"):
            writer.write(header[:-1]); writer.flush()
            self.collector.scan()
            self.assertNotIn(str(child), self.collector.files)
            writer.write(header[-1:] + line("event_msg", {"type": "task_started", "turn_id": "child-turn"})
                         + progress("child-progress", "자식 공개 진행")); writer.flush()
            self.collector.scan()
            self.assertIn("child:child-turn", self.collector.turns)
            self.assertEqual(self.collector.messages["child-progress"]["message"], "자식 공개 진행")
            self.assertEqual(self.collector.files[str(child)]["offset"], child.stat().st_size)

    def test_windows_probe_does_not_read_foreign_bodies_or_collect_private_reasoning(self):
        foreign = self.path.with_name("foreign.jsonl")
        foreign.write_bytes(line("session_meta", {"id": "foreign", "cwd": str(self.base / "foreign")},
                                 at="2026-10-06T01:00:00Z") + b"{broken foreign body}\n")
        self.collector.scan()  # Only the foreign header is discovered.
        foreign_offset = self.collector.files[str(foreign)]["offset"]
        real_open = Path.open

        def open_selected(path, *args, **kwargs):
            if path == foreign:
                raise AssertionError("Foreign rollout body must stay unread")
            return real_open(path, *args, **kwargs)

        self.append(line("response_item", {"type": "reasoning", "content": "private reasoning"})
                    + progress("private", "private channel", ending=b"\n").replace(
                        b'"phase": "commentary"', b'"phase": "analysis"')
                    + progress("public", "선택한 공개 메시지"))
        with patch.object(sys, "platform", "win32"), patch.object(Path, "open", open_selected):
            self.collector.scan()
        self.assertEqual(self.collector.files[str(foreign)]["offset"], foreign_offset)
        self.assertNotIn("private", self.collector.messages)
        self.assertEqual(set(self.collector.messages), {"public"})


if __name__ == "__main__":
    unittest.main()
