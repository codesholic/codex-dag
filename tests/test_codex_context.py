import json
import sqlite3
from contextlib import closing
import tempfile
import unittest
from pathlib import Path

from codex_context import AppContextReader


class AppContextChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state_path = self.root / ".codex-global-state.json"
        self.index = self.root / "session_index.jsonl"
        self.reader = AppContextReader(self.root)
        self.project = self.root / "project"
        self.project.mkdir()
        self.state = {"selected-project": {"type": "local", "projectId": "project-id"},
                      "local-projects": {"project-id": {"name": "My project", "rootPaths": [str(self.project)]}}}
        self.write_state()

    def write_state(self):
        self.state_path.write_text(json.dumps(self.state), encoding="utf-8")

    def database(self, filename="state_5.sqlite", rows=(("session", "Shown in app", "Full initial request"),)):
        path = self.root / filename
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, name TEXT, title TEXT)")
            db.executemany("INSERT INTO threads VALUES (?,?,?)", rows)
        return path

    def test_selected_local_project_path_is_available_without_writing_app_state(self):
        before = self.state_path.read_bytes()
        self.assertTrue(self.reader.refresh(["session"]))
        self.assertEqual(self.reader.context["status"], "ready")
        self.assertEqual(self.reader.context["selected_project"]["path"], str(self.project))
        self.assertEqual(self.reader.context["selected_project"]["name"], "My project")
        self.assertFalse(self.reader.refresh(["session"]))
        self.assertEqual(self.state_path.read_bytes(), before)

    def test_project_selection_changes_are_detected(self):
        self.reader.refresh([])
        other = self.root / "other"; other.mkdir()
        self.state["local-projects"]["other"] = {"name": "Other", "rootPaths": [str(other)]}
        self.state["selected-project"]["projectId"] = "other"
        self.write_state()
        self.assertTrue(self.reader.refresh([]))
        self.assertEqual(self.reader.context["selected_project"]["path"], str(other))

    def test_selected_directory_removal_and_recreation_are_detected_without_metadata_changes(self):
        self.reader.refresh([])
        before = self.reader.stamp(self.state_path)
        self.project.rmdir()
        self.assertTrue(self.reader.refresh([]))
        self.assertEqual(self.reader.stamp(self.state_path), before)
        self.assertEqual(self.reader.context["status"], "invalid_path")
        self.assertIsNone(self.reader.context["selected_project"]["path"])
        self.project.mkdir()
        self.assertTrue(self.reader.refresh([]))
        self.assertEqual(self.reader.context["status"], "ready")
        self.assertEqual(self.reader.context["selected_project"]["path"], str(self.project))

    def test_remote_multiple_roots_and_missing_paths_do_not_guess_local_path(self):
        self.state["selected-project"] = {"type": "remote", "projectId": "remote-id"}
        self.write_state(); self.reader.refresh([])
        self.assertEqual(self.reader.context["status"], "remote")
        self.assertIsNone(self.reader.context["selected_project"])
        self.state["selected-project"] = {"type": "local", "projectId": "project-id"}
        self.state["local-projects"]["project-id"]["rootPaths"] = [str(self.project), str(self.root)]
        self.write_state(); self.reader.refresh([])
        self.assertEqual(self.reader.context["status"], "multiple_roots")
        self.assertIsNone(self.reader.context["selected_project"]["path"])
        for path in ("relative/path", str(self.root / "missing")):
            self.state["local-projects"]["project-id"]["rootPaths"] = [path]
            self.write_state(); self.reader.refresh([])
            self.assertEqual(self.reader.context["status"], "invalid_path")
            self.assertIsNone(self.reader.context["selected_project"]["path"])

    def test_invalid_or_missing_app_state_preserves_manual_fallback(self):
        self.state_path.write_text('{"selected-project":')
        self.reader.refresh([])
        self.assertEqual(self.reader.context["status"], "unavailable")
        self.state_path.unlink(); self.reader.refresh([])
        self.assertEqual(self.reader.context["status"], "unavailable")
        self.state_path.write_text('{}'); self.reader.refresh([])
        self.assertEqual(self.reader.context["status"], "no_project")

    def test_displayed_database_name_wins_over_prompt_and_old_index_title(self):
        path = self.database()
        self.index.write_text(json.dumps({"id": "session", "thread_name": "Old name"}) + "\n")
        before = path.read_bytes()
        self.reader.refresh(["session"])
        self.assertEqual(self.reader.names["session"], {"name": "Shown in app", "name_source": "app_thread_name"})
        self.assertEqual(path.read_bytes(), before)
        self.assertNotIn("Full initial request", str(self.reader.names))

    def test_rename_and_same_named_distinct_sessions_keep_original_ids(self):
        path = self.database(rows=(("first", "Same name", "prompt1"), ("second", "Same name", "prompt2")))
        self.reader.refresh(["first", "second"])
        self.assertEqual(set(self.reader.names), {"first", "second"})
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("UPDATE threads SET name='Renamed' WHERE id='first'")
        self.assertTrue(self.reader.refresh(["first", "second"]))
        self.assertEqual(self.reader.names["first"]["name"], "Renamed")
        self.assertEqual(self.reader.names["second"]["name"], "Same name")

    def test_wal_rename_is_visible_without_reopening_the_writer(self):
        path = self.database()
        db = sqlite3.connect(path); self.addCleanup(db.close)
        db.execute("PRAGMA journal_mode=WAL")
        self.reader.refresh(["session"])
        db.execute("UPDATE threads SET name='WAL rename' WHERE id='session'"); db.commit()
        self.assertTrue(self.reader.refresh(["session"]))
        self.assertEqual(self.reader.names["session"]["name"], "WAL rename")

    def test_active_rollback_journal_falls_back_and_retries_without_database_change(self):
        path = self.database()
        self.index.write_text(json.dumps({"id": "session", "thread_name": "Index fallback"}) + "\n")
        db = sqlite3.connect(path); self.addCleanup(db.close)
        db.execute("BEGIN EXCLUSIVE")
        # Snapshot reads never acquire source SQLite locks. An active rollback
        # journal, which can represent uncommitted main DB pages, must defer reads.
        journal = Path(str(path) + "-journal")
        journal.write_bytes(b"active recovery")
        before = self.reader.stamp(path)
        self.reader.refresh(["session"])
        self.assertEqual(self.reader.names["session"]["name"], "Index fallback")
        db.rollback()
        journal.unlink(missing_ok=True)
        self.assertEqual(self.reader.stamp(path), before)
        self.assertTrue(self.reader.refresh(["session"]))
        self.assertEqual(self.reader.names["session"]["name"], "Shown in app")
        self.assertFalse(self.reader.refresh(["session"]))

    def test_wal_mode_database_with_missing_sidecars_remains_exactly_read_only(self):
        path = self.database()
        with closing(sqlite3.connect(path)) as db:
            db.execute("PRAGMA journal_mode=WAL")
        self.assertFalse(Path(str(path) + "-wal").exists())
        self.assertFalse(Path(str(path) + "-shm").exists())
        before = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.reader.refresh(["session"])
        self.assertEqual(self.reader.names["session"]["name"], "Shown in app")
        after = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(after, before)

    def test_index_fallback_ignores_other_ids_and_incomplete_tail(self):
        self.index.write_text('\n'.join(json.dumps(v) for v in [
            {"id": "session", "thread_name": "Previous"}, {"id": "other", "thread_name": "Other"},
            {"id": "session", "thread_name": "Current"}]) + '\n' + json.dumps({"id": "session", "thread_name": "Incomplete"}))
        self.reader.refresh(["session"])
        self.assertEqual(self.reader.names, {"session": {"name": "Current", "name_source": "app_session_index"}})

    def test_incomplete_utf8_index_tail_does_not_interrupt_database_name_lookup(self):
        self.database()
        line = json.dumps({"id": "session", "thread_name": "이전 이름"}, ensure_ascii=False).encode() + b"\n"
        self.index.write_bytes(line + b'\xed\x95')
        self.reader.refresh(["session"])
        self.assertEqual(self.reader.names["session"]["name"], "Shown in app")
        (self.root / "state_5.sqlite").unlink()
        self.reader.refresh(["session"])
        self.assertEqual(self.reader.names["session"]["name"], "이전 이름")

    def test_newer_database_name_has_priority_and_legacy_missing_name_is_safe(self):
        self.database("state_4.sqlite", rows=(("session", "Old DB", "Raw prompt"),))
        self.database("state_5.sqlite", rows=(("session", "Current DB", "Raw prompt"),))
        with closing(sqlite3.connect(self.root / "state_6.sqlite")) as db, db:
            db.execute("CREATE TABLE threads(id TEXT,title TEXT)")
            db.execute("INSERT INTO threads VALUES ('session','Raw attachment instructions')")
        self.reader.refresh(["session"])
        self.assertEqual(self.reader.names["session"]["name"], "Current DB")

    def cli_database(self, filename="state_5.sqlite", rows=(("session", None, "현재 작업위치 확인해줘"),)):
        path = self.database(filename, rows)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("ALTER TABLE threads ADD COLUMN source TEXT")
            db.execute("UPDATE threads SET source='cli'")
        return path

    def test_cli_null_name_uses_stored_title_with_distinct_provenance(self):
        path = self.cli_database()
        before = path.read_bytes()
        self.reader.refresh(["session"])
        self.assertEqual(self.reader.names["session"],
                         {"name": "현재 작업위치 확인해줘", "name_source": "cli_thread_title"})
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(self.reader.refresh(["session"]))

    def test_explicit_database_name_wins_over_cli_prompt_and_cached_names(self):
        self.cli_database(rows=(("session", "App renamed", "CLI first prompt"),))
        self.index.write_text(json.dumps({"id": "session", "thread_name": "Index name"}) + "\n")
        self.reader.refresh(["session"])
        self.assertEqual(self.reader.names["session"], {"name": "App renamed", "name_source": "app_thread_name"})

    def test_saved_and_index_display_names_win_over_cli_title_fallback(self):
        self.cli_database()
        self.state["thread-titles"] = {"titles": {"session": "Saved app name"}}
        self.write_state()
        self.reader.refresh(["session"])
        self.assertEqual(self.reader.names["session"], {"name": "Saved app name", "name_source": "app_saved_title"})
        self.index.write_text(json.dumps({"id": "session", "thread_name": "Index renamed"}) + "\n")
        self.reader.refresh(["session"])
        self.assertEqual(self.reader.names["session"], {"name": "Index renamed", "name_source": "app_session_index"})

    def test_cli_wal_title_update_and_later_app_rename_are_live_and_read_only(self):
        path = self.cli_database()
        with closing(sqlite3.connect(path)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            self.reader.refresh(["session"])
            db.execute("UPDATE threads SET title='CLI updated title' WHERE id='session'"); db.commit()
            before = {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()}
            self.assertTrue(self.reader.refresh(["session"]))
            self.assertEqual(self.reader.names["session"]["name"], "CLI updated title")
            self.assertEqual({p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()}, before)
            db.execute("UPDATE threads SET name='Explicit app rename' WHERE id='session'"); db.commit()
            self.assertTrue(self.reader.refresh(["session"]))
            self.assertEqual(self.reader.names["session"],
                             {"name": "Explicit app rename", "name_source": "app_thread_name"})

    def test_non_cli_sources_do_not_expose_raw_prompt_titles(self):
        path = self.cli_database(rows=(("session", None, "Raw attachment/system instructions"),))
        for source in ("vscode", "subagent", None):
            with self.subTest(source=source):
                with closing(sqlite3.connect(path)) as db, db:
                    db.execute("UPDATE threads SET source=?", (source,))
                self.reader.refresh(["session"])
                self.assertNotIn("session", self.reader.names)

    def test_cli_empty_or_invalid_titles_keep_unnamed_fallback(self):
        self.cli_database(rows=(("empty", "  ", " \n "), ("null", None, None), ("number", None, 123),
                                ("blob", None, b"not a display string")))
        self.reader.refresh(["empty", "null", "number", "blob"])
        # SQLite TEXT affinity makes numeric titles strings; title 123 is the
        # actual metadata string, while NULL/blank/BLOB cannot be display names.
        self.assertEqual(self.reader.names, {"number": {"name": "123", "name_source": "cli_thread_title"}})

    def test_legacy_cli_schema_without_name_column_supports_title(self):
        with closing(sqlite3.connect(self.root / "state_5.sqlite")) as db, db:
            db.execute("CREATE TABLE threads(id TEXT,title TEXT,source TEXT)")
            db.execute("INSERT INTO threads VALUES ('session','CLI legacy title','cli')")
        self.reader.refresh(["session"])
        self.assertEqual(self.reader.names["session"], {"name": "CLI legacy title", "name_source": "cli_thread_title"})

    def test_cli_fallback_latest_database_priority_and_duplicate_names_keep_ids(self):
        self.cli_database("state_4.sqlite", rows=(("first", None, "Older CLI title"),))
        self.cli_database("state_5.sqlite", rows=(("first", None, "Same title"), ("second", None, "Same title")))
        self.reader.refresh(["first", "second"])
        self.assertEqual(set(self.reader.names), {"first", "second"})
        self.assertTrue(all(n["name"] == "Same title" for n in self.reader.names.values()))

    def test_cli_closed_wal_database_has_no_new_source_sidecars(self):
        path = self.cli_database()
        with closing(sqlite3.connect(path)) as db:
            db.execute("PRAGMA journal_mode=WAL")
        before = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.reader.refresh(["session"])
        self.assertEqual(self.reader.names["session"]["name"], "현재 작업위치 확인해줘")
        self.assertEqual({str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}, before)


if __name__ == "__main__":
    unittest.main()
