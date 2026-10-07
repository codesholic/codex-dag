"""Runtime load contracts: shared snapshots, skipped re-imports, isolated rollout errors, quiet polls."""
import http.client
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import monitor
from collector import Collector

BACKEND = Path(monitor.__file__).resolve()
RESOURCES = BACKEND.parent


def row(kind, payload, at):
    return json.dumps({"timestamp": at, "type": kind, "payload": payload}, ensure_ascii=False) + "\n"


def header(identity, project, parent=None):
    meta = {"id": identity, "cwd": str(project), "agent_nickname": identity}
    if parent:
        meta["source"] = {"subagent": {"thread_spawn": {"parent_thread_id": parent, "agent_path": "/root/" + identity}}}
    return row("session_meta", meta, "2026-10-07T00:00:00Z")


def started(turn, at):
    return row("event_msg", {"type": "task_started", "turn_id": turn}, at)


def progress(identity, text, at):
    return row("response_item", {"type": "message", "id": identity, "role": "assistant", "phase": "commentary",
                                 "content": [{"type": "output_text", "text": text}]}, at)


class RuntimeFixture(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.base = Path(self.folder.name).resolve()
        self.project, self.codex, self.data = (self.base / name for name in ("project", "codex", "data"))
        self.project.mkdir(); self.data.mkdir()
        self.sessions = self.codex / "sessions/2026/10/07"
        self.sessions.mkdir(parents=True)
        self.original_runtime = (monitor.DATA_DIR, monitor.LOG, monitor.CODEX_DIR, monitor.RESOURCE_DIR, monitor.COLLECTOR)

    def tearDown(self):
        monitor.DATA_DIR, monitor.LOG, monitor.CODEX_DIR, monitor.RESOURCE_DIR, monitor.COLLECTOR = self.original_runtime
        self.folder.cleanup()

    def write(self, name, *lines):
        path = self.sessions / name
        with path.open("a", encoding="utf-8") as handle:
            handle.write("".join(lines))
        return path

    def selected_collector(self):
        collector = Collector(self.data, self.codex, start_thread=False, resource_directory=RESOURCES)
        collector.set_project(str(self.project))
        collector.scan()
        return collector


class SharedSnapshotChecks(RuntimeFixture):
    def setUp(self):
        super().setUp()
        monitor.configure_runtime(self.data, self.codex, RESOURCES)
        self.write("root.jsonl", header("root", self.project), started("one", "2026-10-07T00:00:01Z"))
        self.collector = monitor.COLLECTOR = self.selected_collector()
        self.cache = monitor.SnapshotCache()

    def test_unchanged_version_shares_one_computation_and_one_stream_frame(self):
        with patch.object(self.collector, "augment", wraps=self.collector.augment) as augment:
            state = self.cache.get()
            streamed, frame = self.cache.stream()
            again, same_frame = self.cache.stream()
            self.assertIs(streamed, state)
            self.assertIs(again, state)
            self.assertIs(same_frame, frame)
            self.assertEqual(augment.call_count, 1)
        head, data = frame.decode("utf-8").split("\n", 1)
        self.assertEqual(head, f"id: {state['revision']}")
        self.assertTrue(data.startswith("data: ") and data.endswith("\n\n"))
        self.assertEqual(json.loads(data[len("data: "):]), json.loads(json.dumps(state)))
        # Caching changes only how often the state is computed, not its content.
        self.assertEqual(json.loads(json.dumps(monitor.snapshot())), json.loads(json.dumps(state)))

    def test_rollout_and_ledger_changes_recompute_the_shared_state(self):
        with patch.object(self.collector, "augment", wraps=self.collector.augment) as augment:
            before = self.cache.get()
            self.write("root.jsonl", progress("progress-1", "공개 진행", "2026-10-07T00:00:02Z"))
            self.collector.scan()
            after_rollout = self.cache.get()
            self.assertEqual(augment.call_count, 2)
            self.assertGreater(after_rollout["revision"], before["revision"])
            self.assertIn("progress-1", {record["id"] for record in after_rollout["message_records"]})
            monitor.append_event({"type": "agent.message", "from_agent": "/root", "to_agent": "user",
                                  "message": "저널 원문", "session_id": "root", "project_path": str(self.project)})
            after_ledger = self.cache.get()
            self.assertEqual(augment.call_count, 3)
            self.assertIn("저널 원문", [event.get("message") for event in after_ledger["events"]])
            self.cache.get()
            self.assertEqual(augment.call_count, 3)

    def test_removed_rollout_advances_revision_and_leaves_the_shared_state(self):
        child = self.write("child.jsonl", header("child", self.project, parent="root"),
                           started("child-turn", "2026-10-07T00:00:03Z"))
        self.collector.scan()
        self.assertIn("child", {session["id"] for session in self.cache.get()["sessions"]})
        revision = self.collector.revision
        child.unlink()
        self.collector.scan()
        self.assertGreater(self.collector.revision, revision)
        self.assertNotIn("child", {session["id"] for session in self.cache.get()["sessions"]})

    def test_responses_report_the_latest_poll_time_without_mutating_the_shared_state(self):
        state = self.cache.get()
        self.collector.last_success_at = "2026-10-07T09:00:00+09:00"
        current = monitor.SnapshotCache.current(self.cache.get())
        self.assertEqual(current["collection"]["last_success_at"], "2026-10-07T09:00:00+09:00")
        self.assertIsNone(state["collection"]["last_success_at"])
        self.assertEqual(current["collection"]["journal_version"], state["collection"]["journal_version"])
        self.assertEqual(monitor.summary(current)["collection"]["last_success_at"], "2026-10-07T09:00:00+09:00")


class LiveServiceChecks(RuntimeFixture):
    def test_streams_share_new_frames_and_only_successful_polls_leave_the_log(self):
        self.write("root.jsonl", header("root", self.project), started("one", "2026-10-07T00:00:01Z"))
        (self.data / "config.json").write_text(json.dumps({"project_path": str(self.project), "root_session_id": "root"}))
        server = subprocess.Popen([sys.executable, str(BACKEND), "--data-dir", str(self.data), "--codex-dir", str(self.codex),
                                   "--resource-dir", str(RESOURCES), "serve"],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        connections = []
        try:
            info = json.loads(server.stdout.readline())
            port = int(info["url"].rsplit(":", 1)[1])
            auth = {"Authorization": "Bearer " + info["token"]}

            def request(path, headers=auth):
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                connection.request("GET", path, headers=headers)
                response = connection.getresponse()
                body = response.read()
                connection.close()
                return response.status, body

            def frames(response):
                while True:
                    line = response.fp.readline()
                    self.assertTrue(line, "SSE stream closed")
                    if line.startswith(b"data: "):
                        yield line

            streams = []
            for _ in range(2):
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                connection.request("GET", "/api/stream", headers=auth)
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                connections.append(connection)
                streams.append(frames(response))
            for stream in streams:
                # Wait until the collector has observed the selected root.
                while not any(s["id"] == "root" for s in json.loads(next(stream)[6:])["sessions"]):
                    pass
            monitor.append_event({"type": "agent.message", "from_agent": "/root", "to_agent": "user",
                                  "message": "공유 프레임 확인", "session_id": "root",
                                  "project_path": str(self.project)}, data_dir=self.data)
            received = []
            for stream in streams:
                line = next(stream)
                while "공유 프레임 확인".encode() not in line:
                    line = next(stream)
                received.append(line)
            self.assertEqual(received[0], received[1])

            status, raw = request("/api/state")
            state = json.loads(raw)
            self.assertEqual(status, 200)
            status, raw = request("/api/summary")
            self.assertEqual(status, 200)
            summary = json.loads(raw)
            self.assertEqual(summary["revision"], state["revision"])
            self.assertEqual(summary["live"]["running_agents"], state["live"]["running_agents"])
            self.assertEqual(summary["root_session_id"], "root")
            self.assertEqual(request("/api/health")[0], 200)
            self.assertEqual(request("/api/state", headers={})[0], 401)
            self.assertEqual(request("/api/missing")[0], 404)
        finally:
            for connection in connections:
                connection.close()
            server.terminate()
            _, log = server.communicate(timeout=10)
        self.assertNotIn('"GET /api/summary', log)
        self.assertNotIn('"GET /api/health', log)
        self.assertNotIn('"GET /api/stream', log)
        self.assertEqual(log.count('"GET /api/state HTTP/1.1" 401'), 1)
        self.assertEqual(log.count('"GET /api/state HTTP/1.1" 200'), 0)
        self.assertEqual(log.count('"GET /api/missing HTTP/1.1" 404'), 1)


class JournalUpgradeChecks(RuntimeFixture):
    def setUp(self):
        super().setUp()
        self.shared = self.base / "shared"
        monitor.configure_runtime(self.data, self.codex, RESOURCES)
        monitor.append_event({"type": "workflow.created", "plan": {"run_id": "run", "title": "기존 작업",
            "nodes": [{"id": "node", "title": "작업", "role": "Developer"}]}}, True)
        self.legacy = self.data / "events.jsonl"
        self.legacy_bytes = self.legacy.read_bytes()

    def audits(self):
        return sorted((self.shared / "migrations").iterdir())

    def shared_events(self):
        return monitor.read_events(io.StringIO((self.shared / "events.jsonl").read_text(encoding="utf-8")))

    def upgrade_and_write(self, message):
        monitor.configure_runtime(self.data, self.codex, RESOURCES, self.shared)
        monitor.append_event({"type": "agent.message", "from_agent": "/root", "to_agent": "/root/worker", "message": message})

    def test_repeated_upgrades_and_writes_stage_one_audit_and_keep_the_legacy_bytes(self):
        for index in range(4):
            self.upgrade_and_write(f"메시지 {index}")
        self.assertEqual(len(self.audits()), 1)
        self.assertEqual(self.legacy.read_bytes(), self.legacy_bytes)
        events = self.shared_events()
        self.assertEqual([event["type"] for event in events], ["workflow.created"] + ["agent.message"] * 4)
        self.assertEqual(len({event["event_id"] for event in events}), 5)
        self.assertFalse(monitor.journal.has_unpublished([self.legacy], self.shared, monitor.read_events))

    def test_a_late_legacy_event_is_still_migrated_with_a_new_audit(self):
        self.upgrade_and_write("공유 기록")
        # An older writer appends to the preserved legacy ledger after the upgrade.
        count = len(monitor.read_events(io.StringIO(self.legacy.read_text(encoding="utf-8"))))
        late = {"at": monitor.now(), "session_id": None, "node_id": None, "from_agent": "/root",
                "to_agent": "/root/worker", "message": "늦은 이전 기록", "evidence": [],
                "type": "agent.message", "seq": count + 1}
        with self.legacy.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(late, ensure_ascii=False) + "\n")
        self.assertTrue(monitor.journal.has_unpublished([self.legacy], self.shared, monitor.read_events))
        monitor.configure_runtime(self.data, self.codex, RESOURCES, self.shared)
        self.assertEqual(len(self.audits()), 2)
        self.assertIn("늦은 이전 기록", [event.get("message") for event in self.shared_events()])
        self.assertFalse(monitor.journal.has_unpublished([self.legacy], self.shared, monitor.read_events))

    def test_a_damaged_legacy_ledger_is_still_reported_and_preserved(self):
        self.upgrade_and_write("공유 기록")
        with self.legacy.open("ab") as handle:
            handle.write(b'{"incomplete":')
        damaged = self.legacy.read_bytes()
        self.assertTrue(monitor.journal.has_unpublished([self.legacy], self.shared, monitor.read_events))
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            monitor.configure_runtime(self.data, self.codex, RESOURCES, self.shared)
        self.assertEqual(self.legacy.read_bytes(), damaged)
        self.assertEqual(len(self.audits()), 1)


class RolloutIsolationChecks(RuntimeFixture):
    def setUp(self):
        super().setUp()
        self.root = self.write("a-root.jsonl", header("root", self.project), started("one", "2026-10-07T00:00:01Z"))
        self.child = self.write("b-child.jsonl", header("child", self.project, parent="root"))
        self.collector = self.selected_collector()

    def test_malformed_root_line_stays_an_error_without_blocking_the_child(self):
        self.write("a-root.jsonl", "{not json}\n")
        offset = self.collector.files[str(self.root)]["offset"]
        self.write("b-child.jsonl", started("child-turn", "2026-10-07T00:00:05Z"))
        for _ in range(2):
            with self.assertRaisesRegex(ValueError, "^Malformed rollout JSON: a-root.jsonl"):
                self.collector.scan()
            self.assertEqual(self.collector.files[str(self.root)]["offset"], offset)
        self.assertIn("child:child-turn", self.collector.turns)
        self.assertEqual(self.root.read_bytes().count(b"{not json}"), 1)

    def test_every_failing_rollout_is_reported(self):
        self.write("a-root.jsonl", "{not json}\n")
        self.write("b-child.jsonl", "[broken\n")
        with self.assertRaises(ValueError) as caught:
            self.collector.scan()
        self.assertIn("a-root.jsonl", str(caught.exception))
        self.assertIn("b-child.jsonl", str(caught.exception))

    def test_poll_reports_the_error_and_still_refreshes_app_names(self):
        self.collector.poll()
        self.assertIsNone(self.collector.error)
        succeeded = self.collector.last_success_at
        self.assertIsNotNone(succeeded)
        self.write("a-root.jsonl", "{not json}\n")
        revision = self.collector.revision
        with patch.object(self.collector.app_reader, "refresh", return_value=True) as refresh:
            self.collector.poll()
        refresh.assert_called_once()
        self.assertIn("Malformed rollout JSON", self.collector.error)
        self.assertEqual(self.collector.last_success_at, succeeded)
        self.assertGreater(self.collector.revision, revision)

    def test_rollout_removed_before_its_header_is_read_is_retried_later(self):
        late = self.write("c-late.jsonl", header("late", self.project, parent="root"))
        real_open = Path.open

        def vanished(path, *args, **kwargs):
            if path == late:
                raise FileNotFoundError(str(path))
            return real_open(path, *args, **kwargs)

        with patch.object(Path, "open", vanished):
            self.collector.scan()
        self.assertNotIn(str(late), self.collector.files)
        self.collector.scan()
        self.assertIn(str(late), self.collector.files)


if __name__ == "__main__":
    unittest.main()
