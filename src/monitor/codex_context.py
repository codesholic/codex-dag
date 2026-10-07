"""Read Codex's local project selection and displayed chat names.

No writes to Codex state or databases. These local storage formats are adapters,
not a public API; unavailable metadata leaves manual selection usable.
"""
import json
import sqlite3
import shutil
import tempfile
from contextlib import closing
from pathlib import Path


class AppContextReader:
    def __init__(self, codex_directory=None):
        self.directory = Path(codex_directory or Path.home() / ".codex")
        self.context = {"status": "unavailable", "selected_project": None,
                        "detail": "앱 선택 정보를 읽지 못했습니다. 경로를 직접 입력할 수 있습니다."}
        self.names = {}
        self.signature = None

    @staticmethod
    def stamp(path):
        try:
            stat = path.stat()
            return stat.st_mtime_ns, stat.st_size
        except OSError:
            return None

    @staticmethod
    def name(value):
        return value.strip() if isinstance(value, str) and value.strip() else None

    def selected_project(self, state):
        selected = state.get("selected-project")
        if selected is None:
            return {"status": "no_project", "selected_project": None,
                    "detail": "앱에서 선택한 로컬 프로젝트가 없습니다. 경로를 직접 입력할 수 있습니다."}
        if not isinstance(selected, dict) or selected.get("type") != "local":
            return {"status": "remote", "selected_project": None,
                    "detail": "앱 선택이 로컬 프로젝트가 아닙니다. 로컬 경로를 직접 입력하세요."}
        projects = state.get("local-projects", {})
        project = projects.get(selected.get("projectId")) if isinstance(projects, dict) else None
        if not isinstance(project, dict):
            return {"status": "unavailable", "selected_project": None,
                    "detail": "선택한 프로젝트의 경로 정보를 읽지 못했습니다."}
        paths = project.get("rootPaths", [])
        paths = list(dict.fromkeys(p for p in paths if isinstance(p, str) and p.strip())) if isinstance(paths, list) else []
        value = {"id": selected.get("projectId"), "name": self.name(project.get("name")) or "선택한 프로젝트",
                 "paths": paths, "path": None}
        if len(paths) != 1:
            return {"status": "multiple_roots" if len(paths) > 1 else "invalid_path", "selected_project": value,
                    "detail": "프로젝트에 여러 경로가 있습니다. 관찰할 경로를 직접 선택하세요." if paths else "선택한 프로젝트에 로컬 경로가 없습니다."}
        path = Path(paths[0]).expanduser()
        if not path.is_absolute() or not path.is_dir():
            return {"status": "invalid_path", "selected_project": value,
                    "detail": "선택한 프로젝트 경로에 접근할 수 없습니다. 경로를 직접 입력하세요."}
        value["path"] = str(path)
        return {"status": "ready", "selected_project": value,
                "detail": "앱에서 선택한 프로젝트 경로를 자동으로 채웁니다."}

    def path_signature(self):
        project = self.context.get("selected_project") or {}
        paths = project.get("paths", [])
        if len(paths) != 1:
            return None
        path = Path(paths[0]).expanduser()
        try:
            return str(path), path.is_absolute() and path.is_dir()
        except OSError:
            return str(path), False

    def database_snapshot(self, path, ids):
        """Query an app-owned snapshot; opening source SQLite can create sidecars.

        Include committed WAL frames. If rollback recovery or concurrent copying
        is observed, use metadata/index fallback and retry on the next poll.
        No SQLite connection, locks or sidecar files touch the Codex directory.
        """
        wal = Path(str(path) + "-wal")
        journal = Path(str(path) + "-journal")
        if journal.exists() and journal.stat().st_size:
            raise OSError("Codex database has an active rollback journal")
        sources = [path, wal]
        before = tuple(self.stamp(source) for source in sources)
        with tempfile.TemporaryDirectory(prefix="codex-dag-metadata-") as temporary:
            copy = Path(temporary) / path.name
            shutil.copyfile(path, copy)
            if before[1] is not None:
                shutil.copyfile(wal, Path(str(copy) + "-wal"))
            after = tuple(self.stamp(source) for source in sources)
            if before != after or (journal.exists() and journal.stat().st_size):
                raise OSError("Codex database changed while copying metadata")
            names = {}
            with closing(sqlite3.connect(copy.as_uri() + "?mode=ro", uri=True, timeout=.2)) as db:
                db.execute("PRAGMA query_only=ON")
                columns = {row[1] for row in db.execute("PRAGMA table_info(threads)")}
                if "id" not in columns:
                    return names
                selected = [column for column in ("name", "title", "source") if column in columns]
                if not selected:
                    return names
                for start in range(0, len(ids), 250):
                    batch = ids[start:start + 250]
                    placeholders = ",".join("?" for _ in batch)
                    for row in db.execute(f"SELECT id,{','.join(selected)} FROM threads WHERE id IN ({placeholders})", batch):
                        session_id, values = row[0], dict(zip(selected, row[1:]))
                        name = self.name(values.get("name"))
                        if name:
                            names[session_id] = {"name": name, "name_source": "app_thread_name"}
                        elif values.get("source") == "cli":
                            title = self.name(values.get("title"))
                            if title:
                                names[session_id] = {"name": title, "name_source": "cli_thread_title"}
            return names

    def refresh(self, session_ids):
        ids = sorted(set(session_ids))
        global_file = self.directory / ".codex-global-state.json"
        index_file = self.directory / "session_index.jsonl"
        databases = sorted(self.directory.glob("state_[0-9]*.sqlite"),
                           key=lambda p: int(p.stem.split("_")[-1]) if p.stem.split("_")[-1].isdigit() else -1,
                           reverse=True)
        sources = [global_file, index_file] + [p for db in databases for p in (db, Path(str(db) + "-wal"), Path(str(db) + "-journal"))]
        # Directory removal/recreation need not change Codex's metadata files.
        signature = (tuple(ids), tuple((str(p), self.stamp(p)) for p in sources), self.path_signature())
        if signature == self.signature:
            return False
        retry = False
        context, names = self.context, {}
        try:
            state = json.loads(global_file.read_text(encoding="utf-8"))
            if not isinstance(state, dict):
                raise ValueError("Invalid app metadata")
            context = self.selected_project(state)
            saved = state.get("thread-titles")
            cached = saved.get("titles", {}) if isinstance(saved, dict) else {}
            if isinstance(cached, dict):
                for session_id in ids:
                    name = self.name(cached.get(session_id))
                    if name:
                        names[session_id] = {"name": name, "name_source": "app_saved_title"}
        except (OSError, ValueError, TypeError, AttributeError):
            context = {"status": "unavailable", "selected_project": None,
                       "detail": "앱 선택 정보를 읽지 못했습니다. 경로를 직접 입력할 수 있습니다."}
        try:
            accepted = set(ids)
            # Ignore an in-progress final line before decoding: it can stop in
            # the middle of a UTF-8 character while the app appends the index.
            for line in index_file.read_bytes().splitlines(keepends=True):
                if not line.endswith(b"\n"):
                    continue
                try:
                    item = json.loads(line)
                    name = self.name(item.get("thread_name"))
                    if item.get("id") in accepted and name:
                        names[item["id"]] = {"name": name, "name_source": "app_session_index"}
                except (ValueError, TypeError, AttributeError):
                    continue
        except OSError:
            pass
        # App display names stay authoritative. CLI threads can have NULL name
        # and only a title; use that fallback below explicit saved/index names.
        database_names, cli_titles = {}, {}
        for path in databases:
            try:
                for session_id, value in self.database_snapshot(path, ids).items():
                    target = cli_titles if value["name_source"] == "cli_thread_title" else database_names
                    target.setdefault(session_id, value)
            except (sqlite3.Error, OSError):
                # Locks/recovery or concurrent writes can disappear without a
                # metadata signature change. Retry instead of caching a failure.
                retry = True
                continue
        for session_id, value in cli_titles.items():
            names.setdefault(session_id, value)
        names.update(database_names)
        changed = context != self.context or names != self.names
        self.context, self.names = context, names
        self.signature = None if retry else signature
        return changed
