"""Shared public records, clone migration, provenance and concurrent publication."""
from concurrent.futures import ThreadPoolExecutor
import copy
import http.client
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import journal
import monitor
from collector import Collector


BACKEND = Path(monitor.__file__).resolve()


def workflow():
    return {"seq": 1, "at": "2026-10-07T00:00:00Z", "type": "workflow.created", "plan": {
        "run_id": "workflow-one", "title": "Public workflow", "nodes": [{"id": "task-one", "title": "Task", "role": "Reviewer"}]},
        "binding": {"project_path": "/public-project", "root_session_id": "root"}}


def message(seq=1, at="2026-10-07T00:00:01Z", body="Exact public request"):
    return {"seq": seq, "at": at, "type": "agent.message", "from_agent": "/root", "to_agent": "/root/review",
            "session_id": "root", "project_path": "/public-project", "message": body}


class SharedJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.codex = self.base / "codex"
        self.codex.mkdir()
        self.shared = self.base / "shared-public"
        self.original_runtime = monitor.DATA_DIR, monitor.LOG, monitor.CODEX_DIR, monitor.RESOURCE_DIR, monitor.COLLECTOR
        monitor.COLLECTOR = None
        self.env = patch.dict(os.environ, {"CODEX_DAG_JOURNAL_DIR": ""})
        self.env.start()

    def tearDown(self):
        monitor.DATA_DIR, monitor.LOG, monitor.CODEX_DIR, monitor.RESOURCE_DIR, monitor.COLLECTOR = self.original_runtime
        self.env.stop()
        self.temp.cleanup()

    def ledger(self, name, events):
        path = self.base / name
        path.mkdir()
        path.joinpath("events.jsonl").write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events), encoding="utf-8")
        return path

    def migrate(self, *sources):
        return journal.migrate(sources, self.shared, monitor.read_events, monitor.reduce_events)

    def read(self):
        with self.shared.joinpath("events.jsonl").open(encoding="utf-8") as handle:
            return monitor.read_events(handle)

    def test_two_runtimes_share_public_records_and_keep_selected_settings_separate(self):
        first, second = self.base / "dev", self.base / "installed"
        for data, selected in ((first, "one"), (second, "two")):
            data.mkdir()
            data.joinpath("config.json").write_text(json.dumps({"project_path": "/project-" + selected,
                "root_session_id": selected, "custom": selected, "codex_dir": str(self.codex)}))
            monitor.configure_runtime(data, journal_dir=self.shared)
            self.assertEqual(json.loads(data.joinpath("config.json").read_text())["custom"], selected)
            self.assertEqual(monitor.LOG.parent, self.shared)
        state = monitor.append_event(message(), data_dir=first)
        identity = state["events"][0]["event_id"]
        monitor.configure_runtime(second)
        self.assertEqual(monitor.snapshot()["events"][0]["event_id"], identity)
        self.assertEqual(monitor.snapshot()["collection"]["journal_dir"], str(self.shared))
        self.assertFalse(first.joinpath("events.jsonl").exists())
        self.assertFalse(second.joinpath("events.jsonl").exists())
        self.assertEqual(json.loads(first.joinpath("config.json").read_text())["root_session_id"], "one")
        collector = Collector(second, start_thread=False)
        self.assertEqual(collector.config["journal_dir"], str(self.shared))
        self.assertEqual(collector.root_session, "two")

    def test_explicit_data_remains_isolated_and_environment_and_config_have_documented_priority(self):
        data = self.base / "isolated"
        monitor.configure_runtime(data, self.codex)
        self.assertEqual(monitor.LOG, data / "events.jsonl")
        monitor.append_event(message(), data_dir=data)
        self.assertTrue(data.joinpath("events.jsonl").exists())
        data.joinpath("config.json").write_text(json.dumps({"journal_dir": str(self.shared), "codex_dir": str(self.codex)}))
        other = self.base / "environment"
        with patch.dict(os.environ, {"CODEX_DAG_JOURNAL_DIR": str(other)}):
            monitor.configure_runtime(data)
            self.assertEqual(monitor.LOG.parent, other)
            monitor.append_event(message(), data_dir=data, journal_dir=self.shared)
        self.assertTrue(self.shared.joinpath("events.jsonl").exists())
        self.assertTrue(other.joinpath("events.jsonl").exists())
        self.assertEqual(json.loads(other.joinpath("events.jsonl").read_text())["message"], "Exact public request")

    def test_shared_default_is_current_user_data_and_never_real_user_storage(self):
        data = self.base / "runtime"
        with patch.object(journal, "default_data_directory", return_value=self.base / "current-user-app-data"):
            monitor.configure_runtime(data, self.codex, shared_journal=True)
        self.assertEqual(monitor.LOG.parent, self.base / "current-user-app-data/public-journal")
        monitor.append_event(message())
        self.assertTrue(monitor.LOG.exists())

    def test_journal_boundary_rejects_codex_bundle_resource_and_symlink_before_config_changes(self):
        data = self.base / "runtime"; data.mkdir()
        data.joinpath("config.json").write_text('{"custom":"preserved"}')
        before = data.joinpath("config.json").read_bytes()
        bundle = self.base / "Fake.app"
        link = self.base / "codex-link"; link.symlink_to(self.codex)
        for forbidden in (self.codex / "journal", BACKEND.parent / "journal", bundle / "journal", link / "journal"):
            with self.subTest(path=forbidden), self.assertRaises(ValueError):
                monitor.configure_runtime(data, self.codex, journal_dir=forbidden)
            self.assertEqual(data.joinpath("config.json").read_bytes(), before)
            self.assertFalse(forbidden.exists())

    def test_clone_history_coalesces_exact_events_and_preserves_new_events_and_original_bytes(self):
        common = [workflow(), message(2)]
        first = self.ledger("first", common + [message(3, body="new dev record")])
        second = self.ledger("second", common + [message(3, body="new installed record")])
        before = {path: path.joinpath("events.jsonl").read_bytes() for path in (first, second)}
        report = self.migrate(first, second)
        rows = self.read()
        self.assertEqual(len(rows), 4)
        self.assertEqual(len(rows[1]["origins"]), 2)
        self.assertNotEqual(rows[2]["event_id"], rows[3]["event_id"])
        self.assertEqual({row["message"] for row in rows[2:]}, {"new dev record", "new installed record"})
        for path, raw in before.items():
            self.assertEqual(path.joinpath("events.jsonl").read_bytes(), raw)
        for source in report["sources"]:
            self.assertEqual(hashlib.sha256(Path(source["backup"]).read_bytes()).hexdigest(), source["sha256"])
        identities = [row["event_id"] for row in rows]
        stable = self.shared.joinpath("events.jsonl").read_bytes()
        self.migrate(first, second)
        self.assertEqual(self.shared.joinpath("events.jsonl").read_bytes(), stable)
        self.assertEqual([row["event_id"] for row in self.read()], identities)

    def test_near_copy_node_transitions_keep_both_origins_and_do_not_weaken_terminal_validation(self):
        running = {"seq": 2, "at": "2026-10-07T00:00:01Z", "type": "node.updated", "node_id": "task-one", "status": "running"}
        completed = {"seq": 3, "at": "2026-10-07T00:00:02Z", "type": "node.updated", "node_id": "task-one", "status": "completed", "evidence": ["result"]}
        source = self.ledger("dev", [workflow(), running, completed])
        clone = self.ledger("installed", [workflow(), {**running, "at": "2026-10-07T00:00:01.092Z"}, {**completed, "at": "2026-10-07T00:00:02.119Z"}])
        self.migrate(source, clone)
        rows = self.read()
        self.assertEqual(len(rows), 3)
        self.assertEqual(monitor.reduce_events(rows)["nodes"][0]["status"], "completed")
        self.assertEqual(len(rows[2]["origins"]), 2)
        self.assertEqual(rows[2]["origins"][1]["original_event"]["at"], "2026-10-07T00:00:02.119Z")
        self.assertEqual(len(rows[2]["alias_event_ids"]), 1)
        before = self.shared.joinpath("events.jsonl").read_bytes()
        self.migrate(source, clone)
        self.assertEqual(self.shared.joinpath("events.jsonl").read_bytes(), before)
        with self.assertRaisesRegex(ValueError, "immutable"):
            monitor.append_event({**running, "at": "2026-10-07T00:00:03Z"}, data_dir=self.base / "data", journal_dir=self.shared)
        self.assertEqual(self.shared.joinpath("events.jsonl").read_bytes(), before)

    def test_genuine_same_source_and_cross_source_retransmissions_remain_distinct(self):
        first = self.ledger("dev", [workflow(), message(2), message(3, at="2026-10-07T00:00:01.003Z")])
        second = self.ledger("installed", [workflow(), message(2, at="2026-10-07T00:00:01.005Z")])
        self.migrate(first, second)
        self.assertEqual(len(self.read()), 4)
        self.assertEqual(len({row["event_id"] for row in self.read()}), 4)
        self.assertEqual([len(row["origins"]) for row in self.read()[1:]], [1, 1, 1])

    def test_independent_identical_singleton_messages_are_not_a_confirmed_clone(self):
        first, second = self.ledger("one", [message()]), self.ledger("two", [message()])
        self.migrate(first, second)
        self.assertEqual(len(self.read()), 2)
        self.assertNotEqual(self.read()[0]["event_id"], self.read()[1]["event_id"])

    def test_conflict_preserves_destination_and_sources_and_leaves_rejected_manifest(self):
        running = {"seq": 2, "at": "2026-10-07T00:00:01Z", "type": "node.updated", "node_id": "task-one", "status": "running"}
        completed = {"seq": 3, "at": "2026-10-07T00:00:02Z", "type": "node.updated", "node_id": "task-one", "status": "completed", "evidence": ["result"]}
        first = self.ledger("dev", [workflow(), running, completed])
        self.migrate(first)
        before = self.shared.joinpath("events.jsonl").read_bytes()
        second = self.ledger("other", [workflow(), {**running, "at": "2026-10-07T00:00:03Z"}])
        original = second.joinpath("events.jsonl").read_bytes()
        with self.assertRaisesRegex(ValueError, "immutable"):
            self.migrate(first, second)
        self.assertEqual(self.shared.joinpath("events.jsonl").read_bytes(), before)
        self.assertEqual(second.joinpath("events.jsonl").read_bytes(), original)
        manifests = [json.loads(p.read_text()) for p in self.shared.glob("migrations/*/manifest.json")]
        self.assertIn("rejected", [m["status"] for m in manifests])

    def test_incomplete_tail_and_corrupt_sequence_are_never_published_or_repaired(self):
        source = self.ledger("source", [message()])
        for corrupt in ('{"seq":2', '{"seq":9}\n'):
            source.joinpath("events.jsonl").write_text(json.dumps(message()) + "\n" + corrupt)
            before = source.joinpath("events.jsonl").read_bytes()
            with self.assertRaises(ValueError):
                self.migrate(source)
            self.assertEqual(source.joinpath("events.jsonl").read_bytes(), before)
            self.assertFalse(self.shared.joinpath("events.jsonl").exists())

    def test_explicit_assignment_cli_preserves_role_and_execution_binding(self):
        data = self.base / "runtime"
        args = [sys.executable, str(BACKEND), "--data-dir", str(data), "--codex-dir", str(self.codex),
            "--journal-dir", str(self.shared), "message", "--from-agent", "/root", "--to-agent", "/root/review",
            "--session", "root", "--project", str(self.base), "--kind", "assignment", "--message", "Review exactly",
            "--role", "Reviewer", "--assignment-id", "assignment-one", "--collaboration-call-id", "call-one",
            "--recipient-session", "child", "--recipient-turn", "child-turn", "--sender-turn", "root-turn"]
        result = subprocess.run(args, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        row = self.read()[0]
        self.assertEqual(row["role"], "Reviewer")
        self.assertEqual(row["recipient_turn_id"], "child-turn")
        self.assertEqual(row["collaboration_call_id"], "call-one")
        self.assertEqual(row["origins"][0]["original_event"]["message"], "Review exactly")

    def test_workflow_node_explicit_execution_binding_survives_creation_and_updates(self):
        event = workflow()
        event["plan"]["nodes"][0].update(task_session_id="child", task_turn_id="child-turn", execution_request_id="child:child-turn")
        update = {"seq": 2, "at": "2026-10-07T00:00:01Z", "type": "node.updated", "node_id": "task-one", "status": "running", "root_turn_id": "root-turn"}
        node = monitor.reduce_events([event, update])["nodes"][0]
        self.assertEqual(node["execution_request_id"], "child:child-turn")
        self.assertEqual(node["root_turn_id"], "root-turn")
        with self.assertRaisesRegex(ValueError, "binding is immutable"):
            monitor.reduce_events([event, {**update, "task_turn_id": "different-turn"}])

    def test_upgrade_migrates_own_legacy_history_before_persisting_pointer_and_preserves_preferences(self):
        data = self.ledger("installed", [message()])
        config = {"project_path": "/chosen", "root_session_id": "chosen-root", "codex_dir": str(self.codex), "custom": True}
        data.joinpath("config.json").write_text(json.dumps(config))
        original = data.joinpath("events.jsonl").read_bytes()
        monitor.configure_runtime(data, journal_dir=self.shared)
        self.assertEqual(self.read()[0]["origins"][0]["original_event"], message())
        saved = json.loads(data.joinpath("config.json").read_text())
        self.assertEqual({k: saved[k] for k in config}, config)
        self.assertEqual(saved["journal_dir"], str(self.shared))
        self.assertEqual(data.joinpath("events.jsonl").read_bytes(), original)
        monitor.append_event({**message(), "message": "after upgrade"}, data_dir=data)
        identities = [row["event_id"] for row in self.read()]
        monitor.configure_runtime(data)
        self.assertEqual([row["event_id"] for row in self.read()], identities)
        self.assertEqual(data.joinpath("events.jsonl").read_bytes(), original)

    def test_upgrade_failure_preserves_original_config_and_does_not_connect_empty_shared_history(self):
        data = self.ledger("installed", [message()])
        data.joinpath("events.jsonl").write_text('{"seq":1')
        data.joinpath("config.json").write_text('{"custom":"preserved"}')
        before = data.joinpath("config.json").read_bytes()
        with self.assertRaises(ValueError):
            monitor.configure_runtime(data, self.codex, journal_dir=self.shared)
        self.assertEqual(data.joinpath("config.json").read_bytes(), before)
        self.assertEqual(data.joinpath("events.jsonl").read_text(), '{"seq":1')
        self.assertFalse(self.shared.joinpath("events.jsonl").exists())

    def test_migration_and_shared_writer_serialize_without_losing_late_records(self):
        source = self.ledger("legacy", [workflow(), message(2)])
        data = self.base / "runtime"
        monitor.configure_runtime(data, self.codex, journal_dir=self.shared)
        entered, release = threading.Event(), threading.Event()
        actual = journal.merge_streams
        def paused(streams):
            entered.set()
            if not release.wait(5):
                raise AssertionError("Migration release missing")
            return actual(streams)
        with ThreadPoolExecutor(max_workers=1) as pool, patch.object(journal, "merge_streams", side_effect=paused):
            migration = pool.submit(self.migrate, source)
            self.assertTrue(entered.wait(3))
            code = "import sys;sys.path.insert(0,sys.argv[1]);import monitor;monitor.append_event({'type':'agent.message','from_agent':'root','to_agent':'worker','message':'late writer'},data_dir=sys.argv[2])"
            writer = subprocess.Popen([sys.executable, "-c", code, str(BACKEND.parent), str(data)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                time.sleep(.1)
                self.assertIsNone(writer.poll(), "Writer bypassed migration lock")
                release.set()
                migration.result(timeout=5)
                _, err = writer.communicate(timeout=5)
                self.assertEqual(writer.returncode, 0, err)
            finally:
                release.set()
                if writer.poll() is None:
                    writer.terminate()
                    writer.communicate(timeout=3)
        self.assertEqual([row["seq"] for row in self.read()], [1, 2, 3])
        self.assertEqual(self.read()[-1]["message"], "late writer")

    def test_legacy_writer_waiting_during_upgrade_reresolves_the_new_pointer(self):
        data = self.ledger("installed", [message()])
        data.joinpath("config.json").write_text(json.dumps({"codex_dir": str(self.codex), "custom": True}))
        original = data.joinpath("events.jsonl").read_bytes()
        entered, release = threading.Event(), threading.Event()
        actual = journal.migrate
        def paused(*args):
            entered.set()
            if not release.wait(5):
                raise AssertionError("Upgrade release missing")
            return actual(*args)
        with ThreadPoolExecutor(max_workers=1) as pool, patch.object(journal, "migrate", side_effect=paused):
            upgrade = pool.submit(monitor.configure_runtime, data, self.codex, BACKEND.parent, self.shared)
            self.assertTrue(entered.wait(3))
            code = "import sys;sys.path.insert(0,sys.argv[1]);import monitor;monitor.append_event({'type':'agent.message','from_agent':'root','to_agent':'worker','message':'late legacy writer'},data_dir=sys.argv[2])"
            writer = subprocess.Popen([sys.executable, "-c", code, str(BACKEND.parent), str(data)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                time.sleep(.1)
                self.assertIsNone(writer.poll(), "Writer bypassed upgrade lock")
                release.set()
                upgrade.result(timeout=5)
                _, err = writer.communicate(timeout=5)
                self.assertEqual(writer.returncode, 0, err)
            finally:
                release.set()
                if writer.poll() is None:
                    writer.terminate(); writer.communicate(timeout=3)
        self.assertEqual(data.joinpath("events.jsonl").read_bytes(), original)
        self.assertEqual(len(self.read()), 2)
        self.assertEqual(self.read()[-1]["message"], "late legacy writer")

    def test_origin_only_migration_publishes_sse_even_when_sequence_count_is_unchanged(self):
        first = self.ledger("first", [workflow(), message(2)])
        second = self.ledger("clone", [workflow(), message(2)])
        self.migrate(first)
        child = subprocess.Popen([sys.executable, str(BACKEND), "--data-dir", str(self.base / "runtime"),
            "--codex-dir", str(self.codex), "--journal-dir", str(self.shared), "serve"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        conn = response = None
        try:
            info = json.loads(child.stdout.readline())
            conn = http.client.HTTPConnection("127.0.0.1", int(info["url"].rsplit(":", 1)[1]), timeout=4)
            conn.request("GET", "/api/stream", headers={"Authorization": "Bearer " + info["token"]})
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            before = None
            while before is None:
                line = response.fp.readline()
                if line.startswith(b"data: "):
                    before = json.loads(line[6:])
            self.migrate(first, second)
            self.assertEqual(len(self.read()), 2)
            self.assertEqual(len(self.read()[1]["origins"]), 2)
            end, updated = time.monotonic() + 4, None
            while time.monotonic() < end:
                line = response.fp.readline()
                if line.startswith(b"data: "):
                    state = json.loads(line[6:])
                    if state["collection"]["journal_version"] != before["collection"]["journal_version"]:
                        updated = state
                        break
            self.assertIsNotNone(updated, "Origin-only import was skipped by SSE sequence check")
            self.assertEqual(child.pid, info["pid"])
            self.assertIsNone(child.poll())
        finally:
            if response: response.close()
            if conn: conn.close()
            if child.poll() is None:
                child.terminate(); child.wait(timeout=5)
            self.assertNotIn("Traceback", child.stderr.read())
            child.stderr.close(); child.stdout.close()

    def test_two_running_services_publish_new_shared_message_over_both_sse_connections_without_restart(self):
        project = self.base / "project"; project.mkdir()
        session = self.codex / "sessions"; session.mkdir()
        rows = [{"timestamp": "2026-10-07T00:00:00Z", "type": "session_meta", "payload": {"id": "root", "cwd": str(project)}},
            {"timestamp": "2026-10-07T00:00:01Z", "type": "event_msg", "payload": {"type": "task_started", "turn_id": "root-turn"}}]
        session.joinpath("root.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
        children, streams, infos = [], [], []
        try:
            for name in ("development", "installed"):
                data = self.base / name; data.mkdir()
                data.joinpath("config.json").write_text(json.dumps({"project_path": str(project), "root_session_id": "root"}))
                child = subprocess.Popen([sys.executable, str(BACKEND), "--data-dir", str(data), "--codex-dir", str(self.codex),
                    "--journal-dir", str(self.shared), "serve"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                children.append(child)
                line = child.stdout.readline()
                self.assertTrue(line, child.stderr.read() if child.poll() is not None else "No handshake")
                info = json.loads(line); infos.append(info)
                conn = http.client.HTTPConnection("127.0.0.1", int(info["url"].rsplit(":", 1)[1]), timeout=4)
                conn.request("GET", "/api/stream", headers={"Authorization": "Bearer " + info["token"]})
                response = conn.getresponse(); streams.append((conn, response))
                self.assertEqual(response.status, 200)
                while not response.fp.readline().startswith(b"data: "):
                    pass
            monitor.append_event({"type": "agent.message", "from_agent": "/root", "to_agent": "user", "session_id": "root",
                "project_path": str(project), "message": "Shared live body", "message_kind": "message"}, data_dir=self.base / "development")
            for child, (conn, response), info in zip(children, streams, infos):
                end = time.monotonic() + 4
                found = False
                while time.monotonic() < end:
                    line = response.fp.readline()
                    if line.startswith(b"data: "):
                        state = json.loads(line[6:])
                        found = any(record.get("message") == "Shared live body" for record in state["message_records"])
                        if found:
                            self.assertEqual(state["collection"]["journal_dir"], str(self.shared))
                            break
                self.assertTrue(found, "New public body missing from SSE")
                self.assertEqual(child.pid, info["pid"])
                self.assertIsNone(child.poll())
        finally:
            for conn, response in streams:
                response.close(); conn.close()
            for child in children:
                if child.poll() is None:
                    child.terminate(); child.wait(timeout=5)
                if child.stderr:
                    self.assertNotIn("Traceback", child.stderr.read())
                    child.stderr.close()
                if child.stdout:
                    child.stdout.close()

    def test_independent_workflows_with_implicit_references_remain_bound_to_their_source(self):
        running = {"seq": 2, "at": "2026-10-07T00:00:02Z", "type": "node.updated", "node_id": "task-one", "status": "running"}
        other_workflow = copy.deepcopy(workflow())
        other_workflow["at"] = "2026-10-07T00:00:01Z"
        other_workflow["plan"]["run_id"] = "workflow-two"
        other_workflow["plan"]["nodes"][0]["id"] = "task-two"
        first = self.ledger("first", [workflow(), running])
        second = self.ledger("second", [other_workflow, {**running, "at": "2026-10-07T00:00:03Z", "node_id": "task-two"}])
        self.migrate(first, second)
        workflows = monitor.reduce_events(self.read())["workflows"]
        self.assertEqual([w["nodes"][0]["status"] for w in workflows], ["running", "running"])
        updates = [row for row in self.read() if row["type"] == "node.updated"]
        self.assertEqual([row["workflow_id"] for row in updates], ["workflow-one", "workflow-two"])
        self.assertNotIn("workflow_id", updates[0]["origins"][0]["original_event"])

    def test_two_writer_processes_keep_exact_public_bodies_unique_ids_and_contiguous_sequence(self):
        data = self.base / "runtime"
        monitor.configure_runtime(data, self.codex, journal_dir=self.shared)
        code = '''import sys
sys.path.insert(0,sys.argv[1])
import monitor
for n in range(8):
 monitor.append_event({'type':'agent.message','from_agent':'root','to_agent':'worker','message':sys.argv[3]+str(n)},data_dir=sys.argv[2])
'''
        writers = [subprocess.Popen([sys.executable, "-c", code, str(BACKEND.parent), str(data), prefix],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for prefix in ("A", "B")]
        for writer in writers:
            out, err = writer.communicate(timeout=10)
            self.assertEqual(writer.returncode, 0, err)
        rows = self.read()
        self.assertEqual([r["seq"] for r in rows], list(range(1, 17)))
        self.assertEqual(len({r["event_id"] for r in rows}), 16)
        self.assertEqual({r["message"] for r in rows}, {p + str(n) for p in ("A", "B") for n in range(8)})


if __name__ == "__main__":
    unittest.main()
