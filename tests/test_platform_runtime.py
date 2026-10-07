"""Windows boundary contracts plus real owned-host shutdown on this platform."""
import ctypes
import http.client
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import monitor
import platform_runtime as runtime

BACKEND = Path(monitor.__file__).resolve()


class SimulatedProfilePath(PurePosixPath):
    @classmethod
    def home(cls):
        return cls("/tmp/current-user")


class PlatformRuntimeTests(unittest.TestCase):
    def test_windows_default_uses_local_app_data_with_spaces_and_unicode(self):
        with patch.object(runtime.os, "name", "nt"), patch.object(runtime, "Path", SimulatedProfilePath), \
             patch.dict(os.environ, {"LOCALAPPDATA": "/tmp/사용자/Local AppData"}):
            self.assertEqual(str(runtime.default_data_directory()), "/tmp/사용자/Local AppData/Codex DAG")

    def test_windows_default_falls_back_to_the_current_users_profile(self):
        with patch.object(runtime.os, "name", "nt"), patch.object(runtime, "Path", SimulatedProfilePath), \
             patch.dict(os.environ, {}, clear=True):
            self.assertEqual(str(runtime.default_data_directory()), "/tmp/current-user/AppData/Local/Codex DAG")

    def test_posix_default_keeps_existing_macos_application_support(self):
        with patch.object(runtime.os, "name", "posix"), patch.object(runtime, "Path", SimulatedProfilePath):
            self.assertEqual(str(runtime.default_data_directory()), "/tmp/current-user/Library/Application Support/Codex DAG")

    def test_windows_shared_and_exclusive_locks_use_the_same_byte_without_writes(self):
        api = SimpleNamespace(LockFileEx=Mock(return_value=1), UnlockFileEx=Mock(return_value=1))
        handle = Mock()
        handle.fileno.return_value = 7
        with patch.object(runtime.os, "name", "nt"), patch.object(runtime, "_windows_api", return_value=api), \
             patch.dict(sys.modules, {"msvcrt": SimpleNamespace(get_osfhandle=lambda fd: 77)}):
            for exclusive, nonblocking, expected in ((False, False, 0), (True, False, 2), (True, True, 3)):
                with runtime.locked_file(handle, exclusive=exclusive, nonblocking=nonblocking):
                    self.assertEqual(api.LockFileEx.call_args.args[:5], (77, expected, 0, 1, 0))
                self.assertEqual(api.UnlockFileEx.call_args.args[:4], (77, 0, 1, 0))
            with self.assertRaisesRegex(ValueError, "preserved"):
                with runtime.locked_file(handle):
                    raise ValueError("preserved")
        self.assertEqual(api.UnlockFileEx.call_count, 4)
        handle.write.assert_not_called()

    def test_windows_nonblocking_lock_conflict_does_not_unlock_someone_elses_lock(self):
        api = SimpleNamespace(LockFileEx=Mock(return_value=0), UnlockFileEx=Mock())
        with patch.object(runtime.os, "name", "nt"), patch.object(runtime, "_windows_api", return_value=api), \
             patch.dict(sys.modules, {"msvcrt": SimpleNamespace(get_osfhandle=lambda fd: 77)}), \
             patch.object(ctypes, "get_last_error", return_value=33, create=True):
            with self.assertRaises(BlockingIOError):
                with runtime.locked_file(Mock(), exclusive=True, nonblocking=True):
                    self.fail("Conflicting lock was acquired")
        api.UnlockFileEx.assert_not_called()

    def test_windows_parent_uses_original_observation_handle_and_never_os_kill(self):
        api = SimpleNamespace(OpenProcess=Mock(return_value=987),
            WaitForSingleObject=Mock(side_effect=[258, 0]), CloseHandle=Mock())
        with patch.object(runtime.os, "name", "nt"), patch.object(runtime, "_windows_api", return_value=api), \
             patch.object(runtime.os, "kill", side_effect=AssertionError("Windows kill(pid, 0) is unsafe")):
            parent = runtime.observe_parent(123)
            self.assertTrue(parent.alive())
            self.assertFalse(parent.alive())
            parent.close()
            parent.close()
        api.OpenProcess.assert_called_once_with(0x00100000, False, 123)
        self.assertEqual(api.WaitForSingleObject.call_args_list[0].args, (987, 0))
        api.CloseHandle.assert_called_once_with(987)

    def test_windows_already_exited_parent_does_not_reopen_a_recycled_pid(self):
        api = SimpleNamespace(OpenProcess=Mock(return_value=None), WaitForSingleObject=Mock(), CloseHandle=Mock())
        with patch.object(runtime, "_windows_api", return_value=api), \
             patch.object(ctypes, "get_last_error", return_value=87, create=True):
            parent = runtime.WindowsParentProcess(123)
            self.assertFalse(parent.alive())
            parent.close()
        api.OpenProcess.assert_called_once()
        api.WaitForSingleObject.assert_not_called()

    def test_windows_parent_permission_failure_is_explicit_instead_of_pid_guessing(self):
        api = SimpleNamespace(OpenProcess=Mock(return_value=None))
        with patch.object(runtime, "_windows_api", return_value=api), \
             patch.object(ctypes, "get_last_error", return_value=5, create=True), \
             patch.object(ctypes, "WinError", side_effect=lambda code: OSError(code, "access denied"), create=True):
            with self.assertRaisesRegex(OSError, "access denied"):
                runtime.WindowsParentProcess(123)

    def test_summary_excludes_raw_messages_tools_and_agent_bodies_but_keeps_live_counts(self):
        state = {"revision": 19, "project_path": "/project", "root_session_id": "root", "root_session_name": "CLI 제목",
            "live": {"running_agents": 2, "completed_agents": 4, "active_tools": 1,
                "agents": [{"latest_activity": {"detail": "large tool body"}}]},
            "collection": {"error": None, "last_success_at": "2026-10-06", "encrypted_records": 9},
            "registered_workflow": {"nodes": [{"id": "one"}, {"id": "two"}]},
            "activity": [{"detail": "large tool body"}], "messages": [{"message": "private-to-ui source"}]}
        with patch.object(monitor, "snapshot", return_value=state):
            result = monitor.summary()
        self.assertEqual(result["live"], {"running_agents": 2, "completed_agents": 4, "active_tools": 1})
        self.assertEqual(result["registered_workflow"], {"node_count": 2})
        self.assertEqual(result["root_session_name"], "CLI 제목")
        self.assertNotIn("large tool body", json.dumps(result))
        self.assertIn("agents", state["live"])
        self.assertIn("messages", state)


class HostControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.codex = self.base / "codex-input"
        self.codex.mkdir()
        self.children = []

    def tearDown(self):
        for child in self.children:
            if child.poll() is None:
                if child.stdin and not child.stdin.closed:
                    child.stdin.close()
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    child.terminate()
                    child.wait(timeout=3)
            for stream in (child.stdin, child.stdout, child.stderr):
                if stream and not stream.closed:
                    stream.close()
        self.temp.cleanup()

    def start(self, name, control=True):
        data = self.base / name
        args = [sys.executable, str(BACKEND), "--data-dir", str(data), "--codex-dir", str(self.codex), "serve"]
        if control:
            args.append("--control-stdin")
        child = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.children.append(child)
        line = child.stdout.readline()
        self.assertTrue(line, child.stderr.read() if child.poll() is not None else "No handshake")
        return child, data, json.loads(line)

    def get(self, info, path, authorized=True):
        conn = http.client.HTTPConnection("127.0.0.1", int(info["url"].rsplit(":", 1)[1]), timeout=3)
        conn.request("GET", path, headers={"Authorization": "Bearer " + info["token"]} if authorized else {})
        response = conn.getresponse()
        result = response.status, response.read()
        conn.close()
        return result

    def test_shutdown_pipe_releases_metadata_and_lock_without_touching_other_services(self):
        child, data, info = self.start("data 한글")
        other, _, other_info = self.start("other")
        self.assertEqual(self.get(info, "/api/summary", authorized=False)[0], 401)
        status, body = self.get(info, "/api/summary")
        self.assertEqual(status, 200)
        self.assertLess(len(body), 1500)
        self.assertEqual(json.loads(body)["live"]["running_agents"], 0)
        child.stdin.write("shutdown\n")
        child.stdin.flush()
        self.assertEqual(child.wait(timeout=5), 0)
        self.assertFalse((data / ".server.json").exists())
        self.assertEqual(self.get(other_info, "/api/health")[0], 200)
        self.assertNotIn("Traceback", child.stderr.read())
        restarted, _, _ = self.start(data.name)
        self.assertIsNone(restarted.poll())

    def test_unknown_pipe_lines_do_not_control_runtime_and_eof_cleans_up(self):
        child, data, info = self.start("eof")
        child.stdin.write("delete logs\nSHUTDOWN\n")
        child.stdin.flush()
        time.sleep(.1)
        self.assertEqual(self.get(info, "/api/health")[0], 200)
        child.stdin.close()
        self.assertEqual(child.wait(timeout=5), 0)
        self.assertFalse((data / ".server.json").exists())

    def test_plain_serve_ignores_stdin_eof_as_before(self):
        child, _, info = self.start("plain", control=False)
        child.stdin.close()
        time.sleep(.1)
        self.assertEqual(self.get(info, "/api/health")[0], 200)
        child.terminate()
        child.wait(timeout=5)

    def test_utf8_config_and_journal_work_when_process_default_encoding_is_not_utf8(self):
        data = self.base / "config-data"
        data.mkdir()
        project = self.base / "한글 프로젝트"
        project.mkdir()
        (data / "config.json").write_text(json.dumps({"project_path": str(project),
            "codex_dir": str(self.codex)}, ensure_ascii=False), encoding="utf-8")
        code = """import json,locale,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import monitor
from collector import Collector
monitor.configure_runtime(sys.argv[2])
c=Collector(sys.argv[2],codex_directory=monitor.CODEX_DIR,start_thread=False)
assert c.project==json.loads((Path(sys.argv[2])/'config.json').read_text(encoding='utf-8'))['project_path']
monitor.append_event({'type':'agent.message','from_agent':'root','to_agent':'worker','message':'\\ud55c\\uae00'})
assert monitor.snapshot()['events'][0]['message']=='\\ud55c\\uae00'
print(json.dumps({'encoding':locale.getencoding(),'ok':True}))
"""
        env = {**os.environ, "LC_ALL": "C", "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0"}
        result = subprocess.run([sys.executable, "-c", code, str(BACKEND.parent), str(data)],
            env=env, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        evidence = json.loads(result.stdout)
        self.assertTrue(evidence["ok"])
        self.assertNotEqual(evidence["encoding"].replace("-", "").lower(), "utf8")


@unittest.skipUnless(os.name == "nt", "Native Windows API integration requires a Windows runner")
class NativeWindowsTests(unittest.TestCase):
    def test_native_shared_readers_and_exclusive_conflict_preserve_empty_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "empty-journal"
            path.touch()
            with path.open("r+") as first, path.open("r+") as second:
                with runtime.locked_file(first), runtime.locked_file(second):
                    self.assertEqual(path.read_bytes(), b"")
                with runtime.locked_file(first, exclusive=True):
                    with self.assertRaises(BlockingIOError):
                        with runtime.locked_file(second, exclusive=True, nonblocking=True):
                            self.fail("Native conflicting lock succeeded")
                with runtime.locked_file(second, exclusive=True, nonblocking=True):
                    pass
            self.assertEqual(path.read_bytes(), b"")

    def test_native_parent_handle_observes_exit_without_signalling_the_process(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        parent = runtime.observe_parent(child.pid)
        try:
            self.assertTrue(parent.alive())
            self.assertIsNone(child.poll())
            child.terminate()
            child.wait(timeout=3)
            self.assertFalse(parent.alive())
        finally:
            parent.close()
            if child.poll() is None:
                child.kill()
                child.wait(timeout=3)


if __name__ == "__main__":
    unittest.main()
