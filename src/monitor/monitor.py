#!/usr/bin/env python3
"""Portable Codex DAG journal, read-only collector and authenticated loopback viewer."""
import argparse
import hashlib
import hmac
import io
import json
import os
import secrets
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from collector import Collector
from storage import validate_storage
from platform_runtime import default_data_directory, locked_file, observe_parent
import journal

RESOURCE_DIR = Path(__file__).resolve().parent
DATA_DIR = default_data_directory()
LOG = DATA_DIR / "events.jsonl"
CODEX_DIR = None
STATUSES = {"pending", "running", "blocked", "completed", "failed", "skipped"}
TERMINAL = {"completed", "failed", "skipped"}
COLLECTOR = None


def configure_runtime(data_dir=None, codex_dir=None, resource_dir=None, journal_dir=None, shared_journal=False):
    """Select writable app data and read-only inputs; never creates Codex inputs."""
    global DATA_DIR, LOG, CODEX_DIR, RESOURCE_DIR
    candidate = Path(data_dir or default_data_directory()).expanduser().resolve()
    config = journal.load_config(candidate)
    effective_codex = codex_dir or config.get("codex_dir") or Path.home() / ".codex"
    data, codex, resources = validate_storage(candidate, effective_codex,
        resource_dir or Path(__file__).resolve().parent)
    # No directory or file writes happen before the read-only boundary check.
    effective_journal = journal.resolve_directory(data, config, journal_dir, shared_journal)
    validate_storage(effective_journal, codex, resources)
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    legacy_log = data / "events.jsonl"
    persist = journal_dir is not None or shared_journal or os.environ.get("CODEX_DAG_JOURNAL_DIR")
    if effective_journal != data and legacy_log.exists():
        # Keep the old writer lock through pointer publication. Waiting writers
        # resolve settings again after acquiring that lock and redirect safely.
        with journal.ledger_lock(legacy_log, exclusive=True):
            if journal.has_unpublished([legacy_log], effective_journal, read_events):
                journal.migrate([legacy_log], effective_journal, read_events, reduce_events)
            if persist:
                journal.persist_directory(data, effective_journal, config)
    elif persist:
        journal.persist_directory(data, effective_journal, config)
    DATA_DIR, LOG, RESOURCE_DIR = data, effective_journal / "events.jsonl", resources
    CODEX_DIR = codex
    return DATA_DIR


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def read_events(handle):
    handle.seek(0)
    data = handle.read()
    if data and not data.endswith("\n"):
        raise ValueError("Incomplete event-log tail; preserve and repair it before continuing")
    events = []
    for i, line in enumerate(data.splitlines(), 1):
        event = json.loads(line)
        if event.get("seq") != i:
            raise ValueError(f"Invalid sequence at line {i}")
        events.append(event)
    return events


def validate_plan(plan):
    if not isinstance(plan.get("title"), str) or not plan.get("run_id"):
        raise ValueError("Plan requires run_id and title")
    nodes = plan.get("nodes", [])
    ids = [n["id"] for n in nodes]
    if not ids or len(set(ids)) != len(ids):
        raise ValueError("Node IDs must be unique and nonempty")
    lookup = {n["id"]: n for n in nodes}
    for node in nodes:
        if not node.get("title") or not node.get("role"):
            raise ValueError("Each node requires title and role")
        deps = node.get("depends_on", [])
        if not isinstance(deps, list) or len(set(deps)) != len(deps):
            raise ValueError("Dependencies must be a unique list")
        if any(dep not in lookup or dep == node["id"] for dep in deps):
            raise ValueError("Unknown or self dependency")
    visited, visiting = set(), set()
    def visit(node_id):
        if node_id in visiting:
            raise ValueError("Workflow has a dependency cycle")
        if node_id in visited:
            return
        visiting.add(node_id)
        for dep in lookup[node_id].get("depends_on", []):
            visit(dep)
        visiting.remove(node_id)
        visited.add(node_id)
    for node_id in ids:
        visit(node_id)


def _reduce_workflow(events):
    if not events or events[0]["type"] != "workflow.created":
        raise ValueError("First event must create a workflow")
    first = events[0]
    plan = first["plan"]
    validate_plan(plan)
    def fresh_node(n):
        return {"id": n["id"], "title": n["title"], "role": n["role"],
              "depends_on": n.get("depends_on", []), "agent_id": None,
              "status": "pending", "detail": n.get("detail", "선행 작업 대기"),
              "started_at": None, "finished_at": None, "evidence": [],
              **{key: n[key] for key in ("execution_request_id", "task_session_id", "task_turn_id", "root_turn_id", "assignment_id", "collaboration_call_id") if key in n}}
    nodes = [fresh_node(n) for n in plan["nodes"]]
    lookup = {n["id"]: n for n in nodes}
    for event in events[1:]:
        if event["type"] == "workflow.extended":
            added = event.get("nodes", [])
            if not added:
                raise ValueError("Workflow extension requires new nodes")
            validate_plan({**plan, "nodes": nodes + added})
            new_nodes = [fresh_node(n) for n in added]
            nodes.extend(new_nodes)
            lookup.update({n["id"]: n for n in new_nodes})
        elif event["type"] == "node.updated":
            node = lookup.get(event.get("node_id"))
            if node is None:
                raise ValueError("Unknown node")
            status = event.get("status", node["status"])
            if status not in STATUSES:
                raise ValueError("Unknown status")
            if node["status"] in TERMINAL:
                raise ValueError("Terminal nodes are immutable; use a new node or run to retry")
            if status == "pending" and node["status"] != "pending":
                raise ValueError("Cannot return a started node to pending")
            if status == "running":
                unmet = [d for d in node["depends_on"] if lookup[d]["status"] != "completed"]
                if unmet:
                    raise ValueError(f"Dependencies not completed: {', '.join(unmet)}")
                if node["started_at"] is None:
                    node["started_at"] = event["at"]
            if status == "completed" and node["status"] != "running":
                raise ValueError("Completion requires a running node")
            if status == "completed" and not (event.get("evidence") or node["evidence"]):
                raise ValueError("Completion requires evidence")
            if status in {"blocked", "failed", "skipped"} and not event.get("message"):
                raise ValueError("Blocked/failed/skipped nodes require a reason")
            node["status"] = status
            if status in TERMINAL:
                node["finished_at"] = event["at"]
            for key in ("execution_request_id", "task_session_id", "task_turn_id", "root_turn_id", "assignment_id", "collaboration_call_id"):
                if key in event:
                    if node.get(key) not in (None, event[key]):
                        raise ValueError("Task execution binding is immutable; use a new task")
                    node[key] = event[key]
            if event.get("agent_id"):
                node["agent_id"] = event["agent_id"]
            if event.get("message"):
                node["detail"] = event["message"]
            node["evidence"].extend(e for e in event.get("evidence", []) if e not in node["evidence"])
        elif event["type"] == "agent.message":
            if not event.get("from_agent") or not event.get("to_agent") or not event.get("message"):
                raise ValueError("Agent messages require sender, recipient and content")
        else:
            raise ValueError(f"Unknown event type: {event['type']}")
    return {"run_id": plan["run_id"], "title": plan["title"],
            "started_at": first["at"], "updated_at": events[-1]["at"],
            "revision": events[-1]["seq"], "source": "coordinator_observed_events",
            "nodes": nodes, "events": [{k: v for k, v in e.items() if k != "plan"} for e in events]}


def empty_state():
    return {"run_id": None, "title": "프로젝트와 세션을 선택하세요", "started_at": None,
            "updated_at": None, "revision": 0, "source": "coordinator_observed_events",
            "nodes": [], "events": [], "binding": None, "workflows": []}


def reduce_events(events):
    """Validate a lossless ledger containing isolated workflows and public messages.

    Legacy ledgers remain unbound until an explicit workflow.bound event is added.
    New workflow IDs can never reuse completed work. Sequence IDs are global.
    """
    if not events:
        return empty_state()
    groups, current = {}, None
    bindings, node_ids = {}, set()
    for event in events:
        kind = event["type"]
        if kind in {"workflow.created", "workflow.extended"}:
            new_nodes = event["plan"].get("nodes", []) if kind == "workflow.created" else event.get("nodes", [])
            added_ids = [node["id"] for node in new_nodes]
            if len(added_ids) != len(set(added_ids)) or node_ids.intersection(added_ids):
                raise ValueError("Node IDs must be unique across preserved workflow history")
            node_ids.update(added_ids)
        if kind == "workflow.created":
            current = event["plan"]["run_id"]
            if current in groups:
                raise ValueError("Workflow run IDs must be unique; existing history is preserved")
            groups[current] = [event]
            bindings[current] = event.get("binding") or event["plan"].get("binding")
        elif kind == "workflow.bound":
            target = event.get("workflow_id", current)
            if target not in groups or not event.get("binding"):
                raise ValueError("Binding requires an existing workflow")
            if bindings.get(target) and bindings[target] != event["binding"]:
                raise ValueError("Workflow binding is immutable; create a new workflow")
            bindings[target] = event["binding"]
        elif kind == "agent.message":
            if not all(event.get(k) for k in ("from_agent", "to_agent", "message")):
                raise ValueError("Agent messages require sender, recipient and content")
        else:
            target = event.get("workflow_id", current)
            if target not in groups:
                raise ValueError("First workflow event must create a workflow")
            groups[target].append(event)
    workflows = []
    for identity, workflow_events in groups.items():
        binding = bindings[identity]
        if binding is not None and (not isinstance(binding, dict) or not binding.get("project_path")
                                    or not binding.get("root_session_id")):
            raise ValueError("Workflow binding requires project_path and root_session_id")
        workflows.append({**_reduce_workflow(workflow_events), "binding": binding})
    state = {**(workflows[-1] if workflows else empty_state()), "workflows": workflows,
             "events": [{k: v for k, v in e.items() if k != "plan"} for e in events],
             "revision": events[-1]["seq"], "updated_at": events[-1]["at"]}
    return state


def snapshot():
    if LOG.exists():
        with journal.ledger_lock(LOG), LOG.open("r", encoding="utf-8") as handle, locked_file(handle):
            state = reduce_events(read_events(handle))
            stat = os.fstat(handle.fileno())
            journal_version = f"{stat.st_ino}:{stat.st_mtime_ns}:{stat.st_size}"
    else:
        state = empty_state()
        journal_version = "empty"
    state = COLLECTOR.augment(state) if COLLECTOR else state
    state.setdefault("collection", {}).update(journal_dir=str(LOG.parent), journal_version=journal_version)
    return state


class SnapshotCache:
    """Share one computed state per journal/collector version across requests.

    Every collector state change advances its revision and every ledger write
    changes the ledger's stat, so an equal version means an equal snapshot.
    The key is read before computing; a concurrent change only makes the cached
    state newer and is recomputed on the next request.
    """
    def __init__(self):
        self.lock = threading.Lock()
        self.key, self.state, self.frame = None, None, None

    @staticmethod
    def version():
        try:
            stat = LOG.stat()
            ledger = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
        except FileNotFoundError:
            ledger = None
        return str(LOG), ledger, id(COLLECTOR), COLLECTOR.revision if COLLECTOR else None

    def _refresh(self, key):
        if key != self.key or self.state is None:
            self.state, self.frame, self.key = snapshot(), None, key
        return self.state

    def get(self):
        key = self.version()
        with self.lock:
            return self._refresh(key)

    def stream(self):
        """Return the state with its SSE frame, serialized once per version."""
        key = self.version()
        with self.lock:
            state = self._refresh(key)
            if self.frame is None:
                data = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
                self.frame = f"id: {state['revision']}\ndata: {data}\n\n".encode()
            return state, self.frame

    @staticmethod
    def current(state):
        """Requests report the collector's latest poll time, as before caching."""
        if not COLLECTOR:
            return state
        return {**state, "collection": {**state.get("collection", {}), "last_success_at": COLLECTOR.last_success_at}}


def summary(state=None):
    """Menu/tray projection; full provenance remains in state and SSE."""
    state = snapshot() if state is None else state
    workflow = state.get("registered_workflow")
    return {key: state.get(key) for key in (
        "revision", "project_path", "root_session_id", "root_session_name")} | {
        "live": {key: value for key, value in state.get("live", {}).items() if key != "agents"},
        "collection": {key: state.get("collection", {}).get(key) for key in ("error", "last_success_at")},
        "registered_workflow": {"node_count": len(workflow.get("nodes", []))} if workflow else None}


def append_event(event, initialize=False, data_dir=None, journal_dir=None):
    """Append public evidence. Optional data_dir avoids process-global configuration."""
    data = Path(data_dir).expanduser().resolve() if data_dir else DATA_DIR
    while True:
        config = journal.load_config(data)
        resolve = data_dir is not None or journal_dir is not None
        path = (journal.resolve_directory(data, config, journal_dir) / "events.jsonl" if resolve else LOG)
        codex = config.get("codex_dir") or CODEX_DIR or Path.home() / ".codex"
        validate_storage(data, codex, RESOURCE_DIR)
        validate_storage(path.parent, codex, RESOURCE_DIR)
        with journal.ledger_lock(path, exclusive=True):
            if resolve:
                current = journal.load_config(data)
                refreshed = journal.resolve_directory(data, current, journal_dir) / "events.jsonl"
                if refreshed != path:
                    continue
            with path.open("a+", encoding="utf-8") as handle, locked_file(handle, exclusive=True):
                events = read_events(handle)
                recorded = {"at": now(), "session_id": os.environ.get("CODEX_THREAD_ID"), "node_id": None,
                    "from_agent": None, "to_agent": None, "message": "", "evidence": [],
                    **event, "seq": len(events) + 1}
                if initialize and recorded["type"] != "workflow.created":
                    raise ValueError("Initialization requires workflow.created")
                if path.parent != data or journal_dir is not None or config.get("journal_dir") or os.environ.get("CODEX_DAG_JOURNAL_DIR"):
                    recorded = journal.identified(recorded, journal.writer_identity(path), path)
                state = reduce_events(events + [recorded])
                handle.seek(0, os.SEEK_END)
                handle.write(json.dumps(recorded, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
                return state

def import_data(source, data_dir=None):
    """Explicitly copy a valid ledger/config unchanged; refuse destructive merges.

    Repeating the import is allowed only if destination bytes already match.
    The source is never altered and its digest/location remain in import.json.
    """
    source = Path(source).expanduser().resolve()
    destination = Path(data_dir or DATA_DIR).expanduser().resolve()
    if source == destination:
        raise ValueError("Import source and destination must differ")
    with (source / "events.jsonl").open("rb") as handle, locked_file(handle):
        raw = handle.read()
        reduce_events(read_events(io.StringIO(raw.decode("utf-8"))))
    inputs = {"events.jsonl": raw}
    if (source / "config.json").exists():
        config_raw = (source / "config.json").read_bytes()
        if not isinstance(json.loads(config_raw), dict):
            raise ValueError("Config must be an object")
        inputs["config.json"] = config_raw
    imported_config = json.loads(inputs.get("config.json", b"{}"))
    validate_storage(destination, imported_config.get("codex_dir") or CODEX_DIR or Path.home() / ".codex", RESOURCE_DIR)
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name, content in inputs.items():
        target = destination / name
        if target.exists() and target.read_bytes() != content:
            raise ValueError(f"Import refuses to overwrite existing {name}")
    for name, content in inputs.items():
        target = destination / name
        if not target.exists():
            with target.open("xb") as handle:
                handle.write(content)
    manifest = {"source": str(source), "imported_at": now(), "files": {
        name: hashlib.sha256(content).hexdigest() for name, content in inputs.items()}}
    (destination / "import.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def handle(self):
        # A browser can close between SSE frames or while HTTP/1.1 awaits a request.
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True

    def log_request(self, code="-", size="-"):
        # Hosts poll these every few seconds; successful polls would push real
        # errors out of the rotated host log. Failures are still recorded.
        try:
            success = 200 <= int(code) < 300
        except (TypeError, ValueError):
            success = False
        if success and urlsplit(self.path).path in {"/api/state", "/api/summary", "/api/health"}:
            return
        super().log_request(code, size)

    def log_message(self, fmt, *args):
        # Never log the bootstrap query token or Authorization credentials.
        if args and str(args[0]).startswith("GET /api/stream"):
            return
        clean = tuple(str(v).split("?", 1)[0] if "token=" in str(v) else v for v in args)
        super().log_message(fmt, *clean)

    def reply(self, code, data, mime="application/json; charset=utf-8", headers=None):
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def authorize(self, bootstrap=False):
        origin = self.server.origin
        if self.headers.get("Host") != origin.split("://", 1)[1]:
            self.reply(403, b'{"error":"host rejected"}')
            return False
        if self.headers.get("Origin") not in {None, origin}:
            self.reply(403, b'{"error":"origin rejected"}')
            return False
        if getattr(self.server, "dev_no_auth", False):
            if self.headers.get("Sec-Fetch-Site") == "cross-site":
                self.reply(403, b'{"error":"cross-site request rejected"}')
                return False
            return True
        token = self.server.token
        supplied = self.headers.get("Authorization", "").removeprefix("Bearer ")
        try:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            supplied = supplied or (cookie["codex_dag_token"].value if "codex_dag_token" in cookie else "")
        except Exception:
            supplied = ""
        if bootstrap:
            supplied = parse_qs(urlsplit(self.path).query).get("token", [supplied])[0]
        if not hmac.compare_digest(supplied.encode("utf-8"), token.encode("utf-8")):
            self.reply(401, b'{"error":"authentication required"}')
            return False
        return True

    def do_GET(self):
        path = urlsplit(self.path).path
        stream_started = False
        try:
            if not self.authorize(bootstrap=path == "/"):
                return
            if path == "/":
                if "token" in parse_qs(urlsplit(self.path).query):
                    if self.server.dev_no_auth:
                        return self.reply(303, b"", headers={"Location": "/"})
                    return self.reply(303, b"", headers={"Location": "/", "Set-Cookie":
                        f"codex_dag_token={self.server.token}; Path=/; HttpOnly; SameSite=Strict"})
                return self.reply(200, (RESOURCE_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
            if path == "/api/health":
                return self.reply(200, json.dumps({"status": "ready", "pid": os.getpid(),
                    "started_at": self.server.started_at, "version": os.environ.get("CODEX_DAG_APP_VERSION", "0.1.1"),
                    "collection_error": COLLECTOR.error}).encode())
            if path == "/api/settings":
                return self.reply(200, json.dumps({**COLLECTOR.settings(), "data_dir": str(DATA_DIR), "journal_dir": str(LOG.parent)}, ensure_ascii=False).encode())
            snapshots = self.server.snapshots
            if path == "/api/state":
                return self.reply(200, json.dumps(snapshots.current(snapshots.get()), ensure_ascii=False).encode())
            if path == "/api/summary":
                return self.reply(200, json.dumps(summary(snapshots.current(snapshots.get())), ensure_ascii=False).encode())
            if path == "/api/projects":
                return self.reply(200, json.dumps(COLLECTOR.projects(), ensure_ascii=False).encode())
            if path != "/api/stream":
                return self.reply(404, b'{"error":"not found"}')
            state, frame = snapshots.stream()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            stream_started = True
            self.close_connection = True
            self.wfile.write(b"retry: 1500\n\n")
            last_revision, last_heartbeat = -1, time.monotonic()
            while not COLLECTOR.stop_event.is_set():
                version = (state["revision"], state.get("collection", {}).get("journal_version"))
                if version != last_revision:
                    self.wfile.write(frame)
                    self.wfile.flush()
                    last_revision = version
                if time.monotonic() - last_heartbeat > 10:
                    self.wfile.write(b": connection heartbeat\n\n")
                    self.wfile.flush()
                    last_heartbeat = time.monotonic()
                COLLECTOR.stop_event.wait(.5)
                state, frame = snapshots.stream()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True
        except Exception as exc:
            if stream_started:
                try:
                    self.wfile.write(f"event: source-error\ndata: {json.dumps({'error': str(exc)})}\n\n".encode())
                    self.wfile.flush()
                except OSError:
                    pass
                self.close_connection = True
            else:
                self.reply(500, json.dumps({"error": str(exc)}).encode())

    def do_POST(self):
        if not self.authorize():
            return
        if self.path not in {"/api/project", "/api/session", "/api/settings"}:
            return self.reply(404, b'{"error":"not found"}')
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 16384:
                raise ValueError("Invalid request size")
            value = json.loads(self.rfile.read(length))
            if self.path == "/api/project":
                result = {"project_path": COLLECTOR.set_project(value["path"])}
            elif self.path == "/api/session":
                result = {"root_session_id": COLLECTOR.set_session(value["id"])}
            else:
                validate_storage(LOG.parent, value["codex_dir"], RESOURCE_DIR)
                result = COLLECTOR.set_codex_directory(value["codex_dir"])
            return self.reply(200, json.dumps(result, ensure_ascii=False).encode())
        except (ValueError, KeyError, TypeError, OSError) as exc:
            return self.reply(400, json.dumps({"error": str(exc)}, ensure_ascii=False).encode())


def serve(args):
    if not (RESOURCE_DIR / "index.html").is_file():
        raise ValueError("Resource directory must contain index.html")
    with (DATA_DIR / ".service.lock").open("a+") as lock:
        try:
            with locked_file(lock, exclusive=True, nonblocking=True):
                _serve_locked(args)
        except BlockingIOError as exc:
            raise ValueError("Codex DAG is already running for this data directory") from exc


def _serve_locked(args):
    global COLLECTOR
    snapshot()  # Validate existing evidence before starting any collector.
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    httpd.daemon_threads = True
    info_path = DATA_DIR / ".server.json"
    parent = None
    parent_thread = None
    try:
        COLLECTOR = Collector(DATA_DIR, codex_directory=CODEX_DIR, resource_directory=RESOURCE_DIR)
        collector = COLLECTOR
        if args.parent_pid:
            parent = observe_parent(args.parent_pid)
        httpd.snapshots = SnapshotCache()
        httpd.dev_no_auth = args.dev_no_auth
        httpd.token = None if httpd.dev_no_auth else secrets.token_urlsafe(32)
        httpd.origin = f"http://127.0.0.1:{httpd.server_port}"
        httpd.started_at = now()
        info = {"pid": os.getpid(), "url": httpd.origin, "token": httpd.token, "started_at": httpd.started_at, "journal_dir": str(LOG.parent)}
        if httpd.dev_no_auth:
            info["auth_mode"] = "development"
        with open(info_path, "w", encoding="utf-8", opener=lambda path, flags: os.open(path, flags, 0o600)) as handle:
            os.chmod(info_path, 0o600)
            json.dump(info, handle)
        def stop(_signum, _frame):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, stop)
        print(json.dumps(info), flush=True)
        if httpd.dev_no_auth:
            print(f"Codex DAG 개발 화면: {httpd.origin}/ (토큰 없이 접속)", flush=True)
        if parent:
            def parent_watch():
                while not collector.stop_event.wait(.5):
                    try:
                        alive = parent.alive()
                    except OSError:
                        alive = False  # Observation failed; do not leave an orphan.
                    if not alive:
                        httpd.shutdown()
                        return
            parent_thread = threading.Thread(target=parent_watch, daemon=True)
            parent_thread.start()
        if args.control_stdin:
            def control_watch():
                try:
                    for line in sys.stdin:
                        if line.strip() == "shutdown":
                            break
                except (OSError, ValueError):
                    pass
                # Explicit shutdown or EOF both mean our owning host is done.
                # This is a private inherited pipe, never a network endpoint.
                httpd.shutdown()
            threading.Thread(target=control_watch, daemon=True).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
    finally:
        if COLLECTOR:
            COLLECTOR.stop_event.set()
            COLLECTOR.thread.join(timeout=3)
        if parent_thread:
            parent_thread.join(timeout=2)
        if parent:
            parent.close()
        httpd.server_close()
        if info_path.exists():
            info_path.unlink()
        COLLECTOR = None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--codex-dir", type=Path)
    parser.add_argument("--resource-dir", type=Path)
    parser.add_argument("--journal-dir", type=Path)
    parser.add_argument("--shared-journal", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("plan", type=Path)
    init.add_argument("--project", required=True)
    init.add_argument("--session", required=True)
    extend = commands.add_parser("extend")
    extend.add_argument("plan", type=Path)
    extend.add_argument("--workflow-id")
    bind = commands.add_parser("bind")
    bind.add_argument("--workflow-id", required=True)
    bind.add_argument("--project", required=True)
    bind.add_argument("--session", required=True)
    imported = commands.add_parser("import-data")
    imported.add_argument("source", type=Path)
    migration = commands.add_parser("migrate-journal")
    migration.add_argument("sources", type=Path, nargs="+")
    emit = commands.add_parser("emit")
    emit.add_argument("--node", required=True)
    emit.add_argument("--workflow-id")
    emit.add_argument("--status", choices=sorted(STATUSES))
    emit.add_argument("--agent")
    emit.add_argument("--message", default="")
    emit.add_argument("--evidence", action="append", default=[])
    for option, field in (("--task-session", "task_session_id"), ("--task-turn", "task_turn_id"),
                          ("--execution-request-id", "execution_request_id"), ("--root-turn", "root_turn_id")):
        emit.add_argument(option, dest=field)
    message = commands.add_parser("message")
    message.add_argument("--from-agent", required=True)
    message.add_argument("--to-agent", required=True)
    message.add_argument("--message", required=True)
    message.add_argument("--kind", choices=["message", "assignment", "progress", "result"], default="message")
    message.add_argument("--session", required=True, help="Recorder Codex session ID")
    message.add_argument("--project", required=True)
    message.add_argument("--at")
    for option, field in (("--role", "role"), ("--assignment-id", "assignment_id"),
        ("--collaboration-call-id", "collaboration_call_id"), ("--recipient-session", "recipient_session_id"),
        ("--recipient-turn", "recipient_turn_id"), ("--execution-request-id", "execution_request_id"),
        ("--sender-session", "sender_session_id"), ("--sender-turn", "sender_turn_id"), ("--root-turn", "root_turn_id")):
        message.add_argument(option, dest=field, help={
            "assignment_id": "Your own task ID; it binds a Codex call only if it equals that call_id",
            "collaboration_call_id": "The Codex collaboration call_id of this send; omit when unknown"}.get(field))
    commands.add_parser("state")
    server = commands.add_parser("serve")
    server.add_argument("--port", type=int, default=0)
    server.add_argument("--parent-pid", type=int)
    server.add_argument("--control-stdin", action="store_true", help="Owned host pipe: shutdown line or EOF stops the service")
    server.add_argument("--dev-no-auth", action="store_true", help="Source development only: local access without token")
    args = parser.parse_args()
    try:
        if args.command == "serve" and args.dev_no_auth and getattr(sys, "frozen", False):
            raise ValueError("Development mode is available only from source")
        if args.command == "migrate-journal":
            data = Path(args.data_dir or default_data_directory()).expanduser().resolve()
            config = journal.load_config(data)
            destination = journal.resolve_directory(data, config, args.journal_dir, args.shared_journal)
            validate_storage(destination, args.codex_dir or config.get("codex_dir") or Path.home() / ".codex", args.resource_dir or RESOURCE_DIR)
            print(json.dumps(journal.migrate(args.sources, destination, read_events, reduce_events), ensure_ascii=False))
            return
        configure_runtime(args.data_dir, args.codex_dir, args.resource_dir, args.journal_dir, args.shared_journal)
        if args.command == "serve":
            if not 0 <= args.port <= 65535:
                raise ValueError("Port must be between 0 and 65535")
            if args.parent_pid is not None and args.parent_pid <= 0:
                raise ValueError("Parent PID must be positive")
            serve(args)
            return
        if args.command == "state":
            print(json.dumps(snapshot(), ensure_ascii=False, indent=2))
            return
        if args.command == "import-data":
            print(json.dumps(import_data(args.source), ensure_ascii=False))
            return
        if args.command == "init":
            state = append_event({"type": "workflow.created", "plan": json.loads(args.plan.read_text(encoding="utf-8")),
                "binding": {"project_path": str(Path(args.project).expanduser().resolve()), "root_session_id": args.session}}, True)
        elif args.command == "bind":
            state = append_event({"type": "workflow.bound", "workflow_id": args.workflow_id,
                "binding": {"project_path": str(Path(args.project).expanduser().resolve()), "root_session_id": args.session}})
        elif args.command in {"extend", "emit"}:
            event = {"workflow_id": args.workflow_id} if args.workflow_id else {}
            if args.command == "extend":
                plan = json.loads(args.plan.read_text(encoding="utf-8"))
                event.update(type="workflow.extended", nodes=plan["nodes"], message=plan.get("title", "후속 작업 추가"))
            else:
                event.update(type="node.updated", node_id=args.node, agent_id=args.agent,
                             message=args.message, evidence=args.evidence)
                if args.status:
                    event["status"] = args.status
                for key in ("task_session_id", "task_turn_id", "execution_request_id", "root_turn_id"):
                    if getattr(args, key, None):
                        event[key] = getattr(args, key)
            state = append_event(event)
        else:
            event = {"type": "agent.message", "from_agent": args.from_agent, "to_agent": args.to_agent,
                     "message": args.message, "message_kind": args.kind, "session_id": args.session,
                     "project_path": str(Path(args.project).expanduser().resolve())}
            for key in ("role", "assignment_id", "collaboration_call_id", "recipient_session_id", "recipient_turn_id",
                        "execution_request_id", "sender_session_id", "sender_turn_id", "root_turn_id"):
                if getattr(args, key, None):
                    event[key] = getattr(args, key)
            if args.at:
                event["at"] = args.at
            state = append_event(event)
        print(json.dumps({"revision": state["revision"], "updated_at": state["updated_at"]}))
    except (ValueError, OSError, KeyError, TypeError) as exc:
        parser.exit(1, f"error: {exc}\n")


if __name__ == "__main__":
    main()
