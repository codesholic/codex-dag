"""Behavioral portability, scope and real subprocess lifecycle contracts."""
import copy
import hashlib
import http.client
import io
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import monitor
from collector import Collector

BACKEND = Path(monitor.__file__).resolve()
RESOURCES = BACKEND.parent


def plan(identity, project, root, role="Reviewer"):
    return {"type": "workflow.created", "plan": {"run_id": identity, "title": identity,
        "nodes": [{"id": identity + "-node", "title": identity + " task", "role": role}]},
        "binding": {"project_path": str(project), "root_session_id": root}}


def rollout(codex, project, identity, parent=None, role=None):
    # Date is deliberately outside the imported two-week discovery limit.
    folder = codex / "sessions/2020/01/01"
    folder.mkdir(parents=True, exist_ok=True)
    meta = {"id": identity, "cwd": str(project), "agent_nickname": "Test Name"}
    if parent:
        meta["source"] = {"subagent": {"thread_spawn": {"parent_thread_id": parent,
            "agent_path": "/root/review", "agent_role": role}}}
    rows = [{"timestamp": "2020-01-01T00:00:00Z", "type": "session_meta", "payload": meta},
            {"timestamp": "2020-01-01T00:00:01Z", "type": "event_msg", "payload": {
                "type": "task_started", "turn_id": identity + "-turn", "root_turn_id": (parent or identity) + "-turn"}}]
    path = folder / (identity + ".jsonl")
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return path


class PortableStorageChecks(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.base = Path(self.folder.name).resolve()
        self.home, self.codex, self.data = (self.base / n for n in ("home", "codex", "data"))
        self.home.mkdir(); self.codex.mkdir(); self.data.mkdir()
        self.original_runtime = (monitor.DATA_DIR, monitor.LOG, monitor.CODEX_DIR, monitor.RESOURCE_DIR, monitor.COLLECTOR)
        monitor.configure_runtime(self.data, self.codex, RESOURCES)
        monitor.COLLECTOR = None

    def tearDown(self):
        monitor.DATA_DIR, monitor.LOG, monitor.CODEX_DIR, monitor.RESOURCE_DIR, monitor.COLLECTOR = self.original_runtime
        self.folder.cleanup()

    def test_fresh_home_and_empty_source_create_no_fabricated_run(self):
        with patch.dict(os.environ, {"HOME": str(self.home)}):
            monitor.configure_runtime()
            self.assertEqual(monitor.DATA_DIR, self.home / "Library/Application Support/Codex DAG")
            collector = Collector(monitor.DATA_DIR, start_thread=False)
            collector.scan()
            state = collector.augment(monitor.snapshot())
        self.assertIsNone(state["run_id"])
        self.assertEqual(state["nodes"], [])
        self.assertEqual(state["messages"], [])
        self.assertEqual(state["events"], [])
        self.assertEqual(state["live"]["running_agents"], 0)
        self.assertIsNone(state["registered_workflow"])
        self.assertFalse((self.home / ".codex").exists())
        self.assertFalse(monitor.LOG.exists())

    def test_two_users_have_independent_paths_and_selected_settings(self):
        project = self.base / "project"; project.mkdir()
        first = Collector(self.data, self.codex, start_thread=False)
        first.set_project(str(project))
        second = Collector(self.base / "other-data", self.base / "other-codex", start_thread=False)
        self.assertIsNone(second.project)
        self.assertNotEqual(first.codex_directory, second.codex_directory)
        self.assertFalse(second.codex_directory.exists())
        self.assertEqual(Collector(self.data, start_thread=False).project, str(project))

    def test_explicit_import_preserves_original_bytes_and_refuses_overwrite(self):
        source = self.base / "source"; source.mkdir()
        # CRLF, Unicode and unusual JSON spacing must survive unchanged.
        event = {**plan("one", self.base, "root"), "seq": 1, "at": "2026-10-06T00:00:00Z"}
        raw = (json.dumps(event, ensure_ascii=False, indent=None, separators=(", ", ": ")) + "\r\n").encode()
        (source / "events.jsonl").write_bytes(raw)
        config = b'{"custom_setting":true,"project_path":null}\n'
        (source / "config.json").write_bytes(config)
        manifest = monitor.import_data(source)
        self.assertEqual(monitor.LOG.read_bytes(), raw)
        self.assertEqual((source / "events.jsonl").read_bytes(), raw)
        self.assertEqual((self.data / "config.json").read_bytes(), config)
        self.assertEqual(manifest["files"]["events.jsonl"], hashlib.sha256(raw).hexdigest())
        monitor.import_data(source)
        monitor.append_event({"type": "agent.message", "from_agent": "/root", "to_agent": "/root/review", "message": "new"})
        existing = monitor.LOG.read_bytes()
        with self.assertRaisesRegex(ValueError, "overwrite"):
            monitor.import_data(source)
        self.assertEqual(monitor.LOG.read_bytes(), existing)
        self.assertEqual((source / "events.jsonl").read_bytes(), raw)

    def test_import_incomplete_evidence_is_rejected_without_creating_destination_log(self):
        source = self.base / "broken"; source.mkdir()
        raw = b'{"seq":1'
        (source / "events.jsonl").write_bytes(raw)
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            monitor.import_data(source)
        self.assertFalse(monitor.LOG.exists())
        self.assertEqual((source / "events.jsonl").read_bytes(), raw)

    def test_public_journal_can_exist_without_creating_fake_workflow(self):
        monitor.append_event({"type": "agent.message", "from_agent": "/root/review", "to_agent": "/root",
                              "message": "public report", "session_id": "actual-child"})
        state = monitor.snapshot()
        self.assertEqual(state["nodes"], [])
        self.assertIsNone(state["run_id"])
        self.assertEqual(state["events"][0]["message"], "public report")

    def test_project_root_workflow_roles_and_journals_do_not_cross_selection(self):
        a, b = self.base / "project-a", self.base / "project-b"
        a.mkdir(); b.mkdir()
        rollout(self.codex, a, "root-a"); rollout(self.codex, a, "worker-a", "root-a")
        rollout(self.codex, b, "root-b"); rollout(self.codex, b, "worker-b", "root-b")
        for project, root, identity, role in ((a, "root-a", "a", "Backend"), (b, "root-b", "b", "Native")):
            monitor.append_event(plan(identity, project, root, role), initialize=True)
            monitor.append_event({"type": "node.updated", "node_id": identity + "-node", "status": "running",
                                  "agent_id": "/root/review", "at": "2020-01-01T00:00:01Z"})
            monitor.append_event({"type": "agent.message", "from_agent": "/root/review", "to_agent": "/root",
                                  "message": identity + " report", "session_id": root, "project_path": str(project)})
        collector = Collector(self.data, self.codex, start_thread=False); collector.scan()
        for project, root, identity, role in ((a, "root-a", "a", "Backend"), (b, "root-b", "b", "Native")):
            collector.set_project(str(project)); collector.set_session(root); collector.scan()
            before = monitor.LOG.read_bytes()
            state = collector.augment(monitor.snapshot())
            self.assertEqual(state["registered_workflow"]["run_id"], identity)
            self.assertEqual([m["message"] for m in state["messages"]], [identity + " report"])
            worker = next(n for n in state["nodes"] if n["session_id"] == "worker-" + identity)
            self.assertEqual(worker["role"], role)
            self.assertEqual(state["live"]["running_agents"], 2)
            self.assertEqual(monitor.LOG.read_bytes(), before)

    def test_unbound_workflow_and_sessionless_or_foreign_project_journal_stay_unassociated(self):
        project = self.base / "project"; project.mkdir()
        rollout(self.codex, project, "actual-root")
        legacy = plan("legacy", project, "actual-root"); legacy.pop("binding")
        monitor.append_event(legacy)
        for fields in ({"session_id": None}, {"session_id": "actual-root", "project_path": "/foreign"}):
            monitor.append_event({"type": "agent.message", "from_agent": "/root", "to_agent": "/root/review",
                                  "message": "unscoped", **fields})
        collector = Collector(self.data, self.codex, start_thread=False); collector.scan()
        collector.set_project(str(project)); collector.set_session("actual-root"); collector.scan()
        state = collector.augment(monitor.snapshot())
        self.assertIsNone(state["registered_workflow"])
        self.assertEqual(state["messages"], [])
        self.assertFalse(any(e.get("message") == "unscoped" for e in state["events"]))
        monitor.append_event({"type": "workflow.bound", "workflow_id": "legacy", "binding": {
            "project_path": str(project), "root_session_id": "actual-root"}})
        self.assertEqual(collector.augment(monitor.snapshot())["registered_workflow"]["run_id"], "legacy")
        with self.assertRaisesRegex(ValueError, "immutable"):
            monitor.append_event({"type": "workflow.bound", "workflow_id": "legacy", "binding": {
                "project_path": str(project), "root_session_id": "different-root"}})

    def test_completed_work_and_distinct_root_history_survive_restart_and_reuse_rejected(self):
        monitor.append_event(plan("one", self.base, "root-one"))
        monitor.append_event({"type": "node.updated", "node_id": "one-node", "status": "running"})
        monitor.append_event({"type": "node.updated", "node_id": "one-node", "status": "completed", "evidence": ["artifact"]})
        completed = monitor.LOG.read_bytes()
        monitor.append_event(plan("two", self.base, "root-two"))
        monitor.configure_runtime(self.data, self.codex)
        self.assertTrue(monitor.LOG.read_bytes().startswith(completed))
        self.assertEqual(monitor.snapshot()["workflows"][0]["nodes"][0]["status"], "completed")
        with self.assertRaisesRegex(ValueError, "Terminal"):
            monitor.append_event({"type": "node.updated", "workflow_id": "one", "node_id": "one-node", "status": "running"})
        with self.assertRaisesRegex(ValueError, "unique"):
            monitor.append_event(plan("one", self.base, "root-one"))
        reused = plan("third", self.base, "root-three")
        reused["plan"]["nodes"][0]["id"] = "one-node"
        with self.assertRaisesRegex(ValueError, "unique"):
            monitor.append_event(reused)

    def test_new_workflow_for_same_root_preserves_completed_registration_and_actual_count(self):
        project = self.base / "project"; project.mkdir()
        rollout(self.codex, project, "root")
        monitor.append_event(plan("previous", project, "root"))
        monitor.append_event({"type": "node.updated", "node_id": "previous-node", "status": "running"})
        monitor.append_event({"type": "node.updated", "node_id": "previous-node", "status": "completed", "evidence": ["artifact"]})
        monitor.append_event(plan("next", project, "root"))
        collector = Collector(self.data, self.codex, start_thread=False)
        collector.set_project(str(project)); collector.scan()
        state = collector.augment(monitor.snapshot())
        self.assertEqual(state["registered_workflow"]["workflow_runs"], ["previous", "next"])
        self.assertEqual([node["status"] for node in state["registered_workflow"]["nodes"]], ["completed", "pending"])
        self.assertEqual(state["live"]["running_agents"], 1)
        self.assertEqual(state["nodes"][0]["source"], "codex_turn")

    def test_custom_codex_directory_switch_clears_live_state_and_persists_unknown_settings(self):
        project = self.base / "project"; project.mkdir()
        source_path = rollout(self.codex, project, "old-root")
        before = source_path.read_bytes()
        (self.data / "config.json").write_text(json.dumps({"project_path": str(project), "custom_setting": "keep"}))
        collector = Collector(self.data, self.codex, start_thread=False); collector.scan()
        self.assertTrue(collector.files)
        other = self.base / "new-source"; other.mkdir()
        collector.set_codex_directory(str(other))
        self.assertEqual(collector.files, {})
        self.assertIsNone(collector.root_session)
        restarted = Collector(self.data, start_thread=False)
        self.assertEqual(restarted.codex_directory, other)
        self.assertEqual(restarted.config["custom_setting"], "keep")
        self.assertEqual(source_path.read_bytes(), before)

    def test_foreign_large_malformed_body_stays_unread_and_selection_catches_up(self):
        project, foreign = self.base / "project", self.base / "foreign"
        project.mkdir(); foreign.mkdir()
        own = rollout(self.codex, project, "own")
        other = rollout(self.codex, foreign, "other")
        with other.open("ab") as handle:
            handle.write(b"{malformed foreign public body" + b"x" * 1024 * 1024 + b"}\n")
        before = {p: p.read_bytes() for p in (own, other)}
        collector = Collector(self.data, self.codex, start_thread=False)
        collector.scan()
        self.assertEqual(collector.turns, {})
        other_entry = next(e for e in collector.files.values() if e["id"] == "other")
        header_offset = other_entry["offset"]
        collector.set_project(str(project)); collector.set_session("own"); collector.scan()
        self.assertEqual(collector.augment(monitor.snapshot())["live"]["running_agents"], 1)
        self.assertEqual(other_entry["offset"], header_offset)
        collector.set_project(str(foreign)); collector.set_session("other")
        with self.assertRaisesRegex(ValueError, "Malformed rollout"):
            collector.scan()
        # Returning to a healthy selection recovers without deleting either source.
        collector.set_project(str(project)); collector.set_session("own"); collector.scan()
        self.assertEqual(collector.augment(monitor.snapshot())["live"]["running_agents"], 1)
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)

    def test_foreign_header_source_variants_do_not_block_selected_project(self):
        project = self.base / "project"; project.mkdir()
        own = rollout(self.codex, project, "own")
        foreign = rollout(self.codex, self.base / "foreign", "foreign")
        rows = foreign.read_text().splitlines()
        header = json.loads(rows[0]); header["payload"]["source"] = {"subagent": "other internal kind"}
        foreign.write_text(json.dumps(header) + "\n" + "malformed foreign body\n")
        collector = Collector(self.data, self.codex, start_thread=False)
        collector.scan(); collector.set_project(str(project)); collector.set_session("own"); collector.scan()
        self.assertEqual(collector.augment(monitor.snapshot())["live"]["running_agents"], 1)

    def test_cli_and_library_overlap_rejection_happens_before_any_writes(self):
        protected = self.codex / "nested-data"
        with self.assertRaisesRegex(ValueError, "Codex input"):
            monitor.configure_runtime(protected, self.codex)
        self.assertFalse(protected.exists())
        # Effective saved and default paths receive the same preflight.
        self.data.joinpath("config.json").write_text(json.dumps({"codex_dir": str(self.base)}))
        before = self.data.joinpath("config.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "Codex input"):
            monitor.configure_runtime(self.data)
        self.assertEqual(self.data.joinpath("config.json").read_bytes(), before)
        with patch.dict(os.environ, {"HOME": str(self.home)}):
            with self.assertRaisesRegex(ValueError, "Codex input"):
                monitor.configure_runtime(self.home / ".codex/nested-data")
            self.assertFalse((self.home / ".codex").exists())
        with self.assertRaisesRegex(ValueError, "bundled resources"):
            monitor.configure_runtime(RESOURCES / "forbidden-data", self.codex)
        self.assertFalse((RESOURCES / "forbidden-data").exists())
        bundle_data = self.base / "Example.app/Contents/data"
        with self.assertRaisesRegex(ValueError, "application bundle"):
            monitor.configure_runtime(bundle_data, self.codex)
        self.assertFalse(bundle_data.exists())
        link = self.base / "codex-link"; link.symlink_to(self.codex, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "Codex input"):
            monitor.configure_runtime(link / "through-link", self.codex)
        self.assertFalse((self.codex / "through-link").exists())
        # The public library convenience must enforce the same boundary.
        with self.assertRaisesRegex(ValueError, "Codex input"):
            monitor.append_event({"type": "agent.message", "from_agent": "a", "to_agent": "b", "message": "m"},
                                 data_dir=self.codex)
        self.assertFalse((self.codex / "events.jsonl").exists())

    def test_rejected_codex_settings_overlap_preserves_settings_and_collection(self):
        project = self.base / "project"; project.mkdir()
        source = rollout(self.codex, project, "root")
        collector = Collector(self.data, self.codex, start_thread=False)
        collector.set_project(str(project)); collector.scan()
        before_config, before_source = collector.config_file.read_bytes(), source.read_bytes()
        before_files = copy.deepcopy(collector.files)
        for rejected in (self.data, self.base):
            with self.assertRaisesRegex(ValueError, "Codex input"):
                collector.set_codex_directory(str(rejected))
            self.assertEqual(collector.config_file.read_bytes(), before_config)
            self.assertEqual(collector.files, before_files)
            self.assertEqual(source.read_bytes(), before_source)



class ServiceChecks(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.base = Path(self.folder.name).resolve()
        self.data, self.codex = self.base / "data", self.base / "codex"
        self.codex.mkdir()
        self.children = []
        self.process, self.info = self.start(self.data)

    def tearDown(self):
        for child in self.children:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill(); child.wait(timeout=3)
            if child.stdout: child.stdout.close()
            if child.stderr: child.stderr.close()
        self.folder.cleanup()

    def command(self, data, *args):
        return [sys.executable, str(BACKEND), "--data-dir", str(data), "--codex-dir", str(self.codex),
                "--resource-dir", str(RESOURCES), "serve", *args]

    def start(self, data, *args):
        process = subprocess.Popen(self.command(data, *args), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.children.append(process)
        # readline ends on the required first stdout JSON; errors produce EOF.
        line = process.stdout.readline()
        self.assertTrue(line, process.stderr.read() if process.poll() is not None else "No startup handshake")
        info = json.loads(line)
        expected = {"pid", "url", "token", "started_at", "journal_dir"}
        if "--dev-no-auth" in args:
            expected.add("auth_mode")
        self.assertEqual(set(info), expected)
        self.assertEqual(info["journal_dir"], str(data))
        return process, info

    def request(self, path, method="GET", body=None, authorized=True, extra_headers=None, info=None):
        info = info or self.info
        url = info["url"].split(":")
        connection = http.client.HTTPConnection("127.0.0.1", int(url[-1]), timeout=3)
        headers = {"Authorization": "Bearer " + info["token"]} if authorized and info.get("token") else {}
        headers.update(extra_headers or {})
        connection.request(method, path, json.dumps(body) if body is not None else None, headers)
        response = connection.getresponse()
        data = response.read()
        result = response.status, dict(response.getheaders()), data
        connection.close()
        return result

    def test_fresh_service_handshake_readiness_and_empty_data_are_real(self):
        status, _, raw = self.request("/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["pid"], self.process.pid)
        state = json.loads(self.request("/api/state")[2])
        self.assertEqual(state["nodes"], [])
        self.assertEqual(state["events"], [])
        self.assertEqual(state["live"]["running_agents"], 0)
        self.assertFalse((self.data / "events.jsonl").exists())
        self.assertEqual((self.data / ".server.json").stat().st_mode & 0o777, 0o600)

    def test_bearer_cookie_host_and_origin_boundaries(self):
        self.assertEqual(self.request("/api/state", authorized=False)[0], 401)
        self.assertEqual(self.request("/api/health", extra_headers={"Authorization": "Bearer invalid"})[0], 401)
        self.assertEqual(self.request("/api/health", extra_headers={"Host": "attacker.example"})[0], 403)
        self.assertEqual(self.request("/api/health", extra_headers={"Origin": "https://attacker.example"})[0], 403)
        status, headers, _ = self.request("/?token=" + self.info["token"], authorized=False)
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/")
        cookie = headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie); self.assertIn("SameSite=Strict", cookie)
        self.assertEqual(self.request("/", authorized=False, extra_headers={"Cookie": cookie.split(";", 1)[0]})[0], 200)
        self.assertEqual(self.request("/api/state", authorized=False, extra_headers={"Cookie": cookie.split(";", 1)[0]})[0], 200)
        self.assertEqual(self.request("/api/settings", "POST", {"codex_dir": str(self.codex)},
                                      extra_headers={"Origin": "https://attacker.example"})[0], 403)

    def test_same_data_duplicate_is_rejected_without_disturbing_owned_server(self):
        other = subprocess.run(self.command(self.data), capture_output=True, text=True, timeout=5)
        self.assertNotEqual(other.returncode, 0)
        self.assertIn("already running", other.stderr)
        self.assertEqual(self.request("/api/health")[0], 200)
        self.assertEqual(json.loads((self.data / ".server.json").read_text())["pid"], self.process.pid)

    def test_port_conflict_does_not_kill_other_service(self):
        port = self.info["url"].split(":")[-1]
        other = subprocess.run(self.command(self.base / "other-data", "--port", port), capture_output=True, text=True, timeout=5)
        self.assertNotEqual(other.returncode, 0)
        self.assertEqual(self.request("/api/health")[0], 200)
        self.assertFalse((self.base / "other-data/.server.json").exists())

    def test_settings_and_project_persist_across_owned_restart(self):
        project = self.base / "project"; project.mkdir()
        other = self.base / "source-two"; other.mkdir()
        self.assertEqual(self.request("/api/project", "POST", {"path": str(project)})[0], 200)
        self.assertEqual(self.request("/api/settings", "POST", {"codex_dir": str(other)})[0], 200)
        self.assertEqual(json.loads(self.request("/api/settings")[2])["codex_dir"], str(other))
        self.process.terminate(); self.process.wait(timeout=5)
        self.assertFalse((self.data / ".server.json").exists())
        # Restart without CLI Codex override must use the saved setting.
        command = [sys.executable, str(BACKEND), "--data-dir", str(self.data), "serve"]
        restarted = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.children.append(restarted)
        info = json.loads(restarted.stdout.readline())
        settings = json.loads(self.request("/api/settings", info=info)[2])
        self.assertEqual(settings["codex_dir"], str(other))
        self.assertEqual(settings["project_path"], str(project))
        self.assertNotEqual(info["token"], self.info["token"])

    def test_sse_snapshot_disconnect_and_reconnect_leave_no_traceback(self):
        for attempt in range(2):
            conn = http.client.HTTPConnection("127.0.0.1", int(self.info["url"].split(":")[-1]), timeout=3)
            conn.request("GET", "/api/stream", headers={"Authorization": "Bearer " + self.info["token"]})
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            lines = []
            while True:
                line = response.fp.readline().decode()
                lines.append(line)
                if line.startswith("data: "):
                    self.assertEqual(json.loads(line[6:])["live"]["running_agents"], 0)
                    break
            response.close(); conn.close()
            monitor.append_event({"type": "agent.message", "from_agent": "/root", "to_agent": "/root/review",
                                  "message": str(attempt)}, data_dir=self.data)
            time.sleep(.6)
        self.process.terminate(); self.process.wait(timeout=5)
        stderr = self.process.stderr.read()
        self.assertNotIn("Traceback", stderr)
        self.assertNotIn(self.info["token"], stderr)

    def test_sigterm_removes_only_own_server_metadata_and_releases_lock(self):
        second, info = self.start(self.base / "other-data")
        self.process.terminate(); self.assertEqual(self.process.wait(timeout=5), 0)
        self.assertFalse((self.data / ".server.json").exists())
        self.assertEqual(self.request("/api/health", info=info)[0], 200)
        restarted, _ = self.start(self.data)
        self.assertIsNone(restarted.poll())

    def test_service_rejects_input_data_overlap_before_lock_or_config_write(self):
        data = self.codex / "forbidden"
        other = subprocess.run(self.command(data), capture_output=True, text=True, timeout=5)
        self.assertNotEqual(other.returncode, 0)
        self.assertIn("Codex input", other.stderr)
        self.assertFalse(data.exists())
        config_before = (self.data / "config.json").read_bytes()
        self.assertEqual(self.request("/api/settings", "POST", {"codex_dir": str(self.base)})[0], 400)
        self.assertEqual((self.data / "config.json").read_bytes(), config_before)
        self.assertEqual(self.request("/api/health")[0], 200)

    def test_parent_death_stops_child_without_touching_existing_service(self):
        data = self.base / "parent-data"
        # The parent prints the child's handshake, then waits; child owns no parent state.
        script = "import subprocess,sys,time,os; p=subprocess.Popen(sys.argv[1:]+['--parent-pid',str(os.getpid())],stdout=subprocess.PIPE,text=True); print(p.stdout.readline(),end='',flush=True); time.sleep(60)"
        parent = subprocess.Popen([sys.executable, "-c", script, *self.command(data)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.children.append(parent)
        info = json.loads(parent.stdout.readline())
        self.assertEqual(self.request("/api/health", info=info)[0], 200)
        parent.terminate(); parent.wait(timeout=5)
        deadline = time.monotonic() + 5
        while (data / ".server.json").exists() and time.monotonic() < deadline:
            time.sleep(.1)
        self.assertFalse((data / ".server.json").exists())
        with self.assertRaises((ConnectionRefusedError, ConnectionResetError, OSError)):
            self.request("/api/health", info=info)
        self.assertEqual(self.request("/api/health")[0], 200)

    def test_development_root_api_and_settings_work_without_token_or_cookie(self):
        process, info = self.start(self.base / "dev-data", "--dev-no-auth")
        self.assertIsNone(info["token"])
        self.assertEqual(info["auth_mode"], "development")
        for path in ("/", "/api/health", "/api/state", "/api/settings", "/api/projects"):
            self.assertEqual(self.request(path, info=info, authorized=False)[0], 200)
        self.assertEqual(self.request("/api/state", info=info, extra_headers={"Authorization": "Bearer obsolete"})[0], 200)
        project = self.base / "dev-project"; project.mkdir()
        self.assertEqual(self.request("/api/project", "POST", {"path": str(project)}, authorized=False, info=info,
                                      extra_headers={"Origin": info["url"]})[0], 200)
        status, headers, _ = self.request("/?token=obsolete", info=info, authorized=False)
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/")
        self.assertNotIn("Set-Cookie", headers)
        self.assertEqual(self.request("/api/state", authorized=False)[0], 401)

    def test_development_keeps_host_origin_and_cross_site_boundaries(self):
        _, info = self.start(self.base / "dev-boundary", "--dev-no-auth")
        for headers in ({"Host": "attacker.example"}, {"Origin": "https://attacker.example"},
                        {"Origin": "null"}, {"Sec-Fetch-Site": "cross-site"}):
            with self.subTest(headers=headers):
                self.assertEqual(self.request("/api/state", info=info, authorized=False, extra_headers=headers)[0], 403)
        self.assertEqual(self.request("/api/settings", "POST", {"codex_dir": str(self.codex)},
                                      info=info, authorized=False, extra_headers={"Origin": "https://attacker.example"})[0], 403)
        self.assertEqual(self.request("/api/state", info=info, authorized=False,
                                      extra_headers={"Origin": info["url"], "Sec-Fetch-Site": "same-origin"})[0], 200)

    def test_development_sse_reconnects_without_authentication(self):
        process, info = self.start(self.base / "dev-sse", "--dev-no-auth")
        for _ in range(2):
            conn = http.client.HTTPConnection("127.0.0.1", int(info["url"].split(":")[-1]), timeout=3)
            conn.request("GET", "/api/stream")
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            while True:
                line = response.fp.readline().decode()
                if line.startswith("data: "):
                    self.assertEqual(json.loads(line[6:])["live"]["running_agents"], 0)
                    break
            response.close(); conn.close()
        process.terminate(); process.wait(timeout=5)
        self.assertNotIn("Traceback", process.stderr.read())

    def test_development_mode_is_not_persisted_and_fixed_port_is_reused(self):
        data = self.base / "dev-restart"
        process, info = self.start(data, "--dev-no-auth")
        port = info["url"].split(":")[-1]
        process.terminate(); process.wait(timeout=5)
        restarted, second = self.start(data, "--dev-no-auth", "--port", port)
        self.assertEqual(second["url"], info["url"])
        self.assertEqual(self.request("/api/state", info=second, authorized=False)[0], 200)
        restarted.terminate(); restarted.wait(timeout=5)
        _, normal = self.start(data, "--port", port)
        self.assertNotIn("auth_mode", normal)
        self.assertTrue(normal["token"])
        self.assertEqual(self.request("/api/state", info=normal, authorized=False)[0], 401)
        self.assertNotIn("dev_no_auth", json.loads((data / "config.json").read_text()))

    def test_dev_script_custom_port_and_source_mode_are_real(self):
        data = self.base / "script-data"
        env = {**os.environ, "CODEX_DAG_DATA_DIR": str(data), "CODEX_DAG_SOURCE_DIR": str(self.codex),
               "CODEX_DAG_PYTHON": sys.executable, "CODEX_DAG_JOURNAL_DIR": str(self.base / "script-journal"), "CODEX_DAG_DEV_PORT": "0"}
        script = BACKEND.parents[2] / "scripts/start-dev.sh"
        process = subprocess.Popen([str(script)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.children.append(process)
        info = json.loads(process.stdout.readline())
        self.assertEqual(info["auth_mode"], "development")
        self.assertIsNone(info["token"])
        self.assertEqual(self.request("/api/state", info=info, authorized=False)[0], 200)
        self.assertEqual(json.loads(self.request("/api/settings", info=info, authorized=False)[2])["codex_dir"], str(self.codex))

    def test_frozen_backend_rejects_development_mode_before_data_writes(self):
        data = self.base / "frozen-forbidden"
        with patch.object(sys, "argv", [str(BACKEND), "--data-dir", str(data), "serve", "--dev-no-auth"]), \
                patch.object(sys, "frozen", True, create=True), patch("sys.stderr", new_callable=io.StringIO) as stderr:
            with self.assertRaises(SystemExit) as raised:
                monitor.main()
            self.assertEqual(raised.exception.code, 1)
            self.assertIn("only from source", stderr.getvalue())
        self.assertFalse(data.exists())

    def test_dev_script_argument_overrides_environment_port(self):
        data = self.base / "script-argument"
        env = {**os.environ, "CODEX_DAG_DATA_DIR": str(data), "CODEX_DAG_SOURCE_DIR": str(self.codex),
               "CODEX_DAG_PYTHON": sys.executable, "CODEX_DAG_JOURNAL_DIR": str(self.base / "script-journal-override"), "CODEX_DAG_DEV_PORT": self.info["url"].split(":")[-1]}
        process = subprocess.Popen([str(BACKEND.parents[2] / "scripts/start-dev.sh"), "0"], env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.children.append(process)
        info = json.loads(process.stdout.readline())
        self.assertNotEqual(info["url"], self.info["url"])
        self.assertEqual(self.request("/api/state", authorized=False, info=info)[0], 200)
        self.assertEqual(self.request("/api/health")[0], 200)

    def test_dev_source_override_rejects_overlap_before_bootstrap_writes(self):
        data = self.codex / "forbidden-dev-data"
        env = {**os.environ, "CODEX_DAG_DATA_DIR": str(data), "CODEX_DAG_SOURCE_DIR": str(self.codex),
               "CODEX_DAG_PYTHON": sys.executable, "CODEX_DAG_JOURNAL_DIR": str(self.base / "script-journal"), "CODEX_DAG_DEV_PORT": "0"}
        result = subprocess.run([str(BACKEND.parents[2] / "scripts/start-dev.sh")], env=env,
                                capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Codex input", result.stderr)
        self.assertFalse(data.exists())


if __name__ == "__main__":
    unittest.main()
