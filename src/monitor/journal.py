"""Public journal resolution and lossless migration, independent of runtime settings.

Only the public ledger is shared. Config, service locks, credentials and Codex
inputs remain separate. Migration preserves raw source bytes plus per-event
origins and uses the same stable lock as writers before atomic publication.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import uuid

from platform_runtime import default_data_directory, locked_file
from storage import validate_storage


METADATA = {"event_id", "origins", "alias_event_ids"}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def load_config(data):
    path = Path(data) / "config.json"
    value = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if not isinstance(value, dict):
        raise ValueError("Settings must be an object")
    return value


def resolve_directory(data_dir, config=None, journal_dir=None, shared_journal=False):
    """Explicit journal > environment > persisted setting > shared default > data.

    An explicit data directory by itself stays isolated for API compatibility.
    Callers opt in with shared_journal, a path, environment or saved settings.
    """
    data = Path(data_dir).expanduser().resolve()
    config = load_config(data) if config is None else config
    selected = journal_dir or os.environ.get("CODEX_DAG_JOURNAL_DIR") or config.get("journal_dir")
    if selected is not None:
        if not str(selected).strip():
            raise ValueError("Journal directory must be a nonempty path")
        return Path(selected).expanduser().resolve()
    return (default_data_directory() / "public-journal").expanduser().resolve() if shared_journal else data


def atomic_write(path, raw):
    fd, name = tempfile.mkstemp(prefix=".journal-stage-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def persist_directory(data, directory, config=None):
    config = dict(load_config(data) if config is None else config)
    config["journal_dir"] = str(directory)
    atomic_write(Path(data) / "config.json", (canonical(config) + "\n").encode("utf-8"))


@contextmanager
def ledger_lock(path, exclusive=False):
    """Stable sidecar avoids locking a replaced inode during a migration."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (path.parent / ".journal.lock").open("a+b") as handle, locked_file(handle, exclusive=exclusive):
        yield


def source_identity(path, events):
    identity_path = Path(path).parent / ".journal-source.json"
    if identity_path.exists():
        value = json.loads(identity_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or not isinstance(value.get("source_id"), str) or not value["source_id"]:
            raise ValueError("Invalid journal source identity")
        return value["source_id"]
    # Legacy source files are never changed. Their common immutable first record
    # identifies clone lineage; each event hash also includes its original seq/at.
    seed = events[0] if events and events[0].get("type") == "workflow.created" else {"path": str(Path(path).resolve())}
    return "legacy:" + digest(canonical(seed).encode("utf-8"))


def writer_identity(path):
    identity_path = path.parent / ".journal-source.json"
    if not identity_path.exists():
        atomic_write(identity_path, (canonical({"source_id": "journal:" + str(uuid.uuid4())}) + "\n").encode())
    return source_identity(path, [])


def identified(event, source_id, path):
    result = dict(event)
    if result.get("event_id"):
        if not isinstance(result["event_id"], str) or not isinstance(result.get("origins"), list) or not result["origins"]:
            raise ValueError("Identified journal events require preserved origins")
        for origin in result["origins"]:
            if not all(key in origin for key in ("source_id", "source_seq", "original_event", "path")):
                raise ValueError("Journal origin is incomplete")
        return result
    result["event_id"] = "sha256:" + digest(canonical({"source_id": source_id, "original_event": event}).encode("utf-8"))
    result["origins"] = [{"source_id": source_id, "source_seq": event["seq"],
                          "original_event": dict(event), "path": str(Path(path).resolve())}]
    return result


def append_origins(target, incoming):
    previous = {canonical(item) for item in target["origins"]}
    target["origins"].extend(item for item in incoming["origins"] if canonical(item) not in previous)
    aliases = set(target.get("alias_event_ids", [])) | set(incoming.get("alias_event_ids", []))
    if incoming["event_id"] != target["event_id"]:
        aliases.add(incoming["event_id"])
    if aliases:
        target["alias_event_ids"] = sorted(aliases)


def payload(event):
    return {key: value for key, value in event.items() if key not in METADATA | {"seq", "at"}}


def moment(event):
    value = datetime.fromisoformat(event["at"].replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("Journal timestamps require a timezone")
    return value


def merge_streams(streams):
    """Merge exact clone identities; unique near copies only after a shared prefix.

    No event is coalesced within the same source. Multiple matching candidates
    remain separate. Conflicting workflow transitions then fail validation rather
    than silently changing completed history.
    """
    all_events, by_id = [], {}
    for stream in streams:
        for event in stream["identified"]:
            duplicate = by_id.get(event["event_id"])
            if duplicate is not None:
                # seq is local to the published ledger; all other event content
                # must remain identical for a reused stable identity.
                if event["event_id"] in duplicate.get("alias_event_ids", []):
                    originals = {canonical(origin["original_event"]) for origin in duplicate["origins"]}
                    if not all(canonical(origin["original_event"]) in originals for origin in event["origins"]):
                        raise ValueError("Stable journal alias identity has conflicting content")
                elif payload(duplicate) != payload(event) or duplicate["at"] != event["at"]:
                    raise ValueError("Stable journal event identity has conflicting content")
                append_origins(duplicate, event)
                continue
            copy = json.loads(canonical(event))
            all_events.append(copy)
            for identity in [copy["event_id"], *copy.get("alias_event_ids", [])]:
                if identity in by_id and by_id[identity] is not copy:
                    raise ValueError("Conflicting journal alias identity")
                by_id[identity] = copy
    for index, first in enumerate(streams):
        for second in streams[index + 1:]:
            if first["path"] == second["path"]:
                continue
            common = 0
            for left, right in zip(first["events"], second["events"]):
                if left != right:
                    break
                common += 1
            # Identical legacy workflow history confirms these are forked copies.
            # A coincidentally equal message alone does not establish clone lineage.
            if not common or not any(e["type"] == "workflow.created" for e in first["events"][:common]):
                continue
            lefts, rights = first["identified"][common:], second["identified"][common:]
            candidates = {}
            for left in lefts:
                matches = []
                for right in rights:
                    if left["event_id"] == right["event_id"]:
                        continue
                    if left.get("type") == "node.updated" and payload(left) == payload(right) and abs((moment(left) - moment(right)).total_seconds()) <= 1:
                        matches.append(right)
                candidates[left["event_id"]] = matches
            for left in lefts:
                matches = candidates[left["event_id"]]
                if len(matches) != 1:
                    continue
                right = matches[0]
                if sum(right["event_id"] in [r["event_id"] for r in matched] for matched in candidates.values()) != 1:
                    continue
                primary, alias = by_id[left["event_id"]], by_id[right["event_id"]]
                if primary is alias:
                    continue
                append_origins(primary, alias)
                all_events.remove(alias)
                for identity, event in list(by_id.items()):
                    if event is alias:
                        by_id[identity] = primary
    # Preserve each source's order even if its clock moved backwards. A stable
    # topological merge orders independent events by observed timestamp only.
    indices = {id(event): i for i, event in enumerate(all_events)}
    edges, incoming = {id(e): set() for e in all_events}, {id(e): 0 for e in all_events}
    for stream in streams:
        previous = None
        for event in stream["identified"]:
            current = by_id[event["event_id"]]
            if previous is not None and previous is not current and id(current) not in edges[id(previous)]:
                edges[id(previous)].add(id(current))
                incoming[id(current)] += 1
            previous = current
    by_object = {id(e): e for e in all_events}
    result = []
    ready = [e for e in all_events if not incoming[id(e)]]
    while ready:
        ready.sort(key=lambda e: (moment(e), indices[id(e)]))
        event = ready.pop(0)
        result.append(event)
        for next_id in edges[id(event)]:
            incoming[next_id] -= 1
            if not incoming[next_id]:
                ready.append(by_object[next_id])
    if len(result) != len(all_events):
        raise ValueError("Journal source orders conflict; original ledgers are preserved")
    return [{**event, "seq": index} for index, event in enumerate(result, 1)]


def has_unpublished(sources, destination, read_events):
    """Whether any source event is missing from the destination ledger.

    Preserved legacy ledgers stay in place after an upgrade. Re-importing them
    unchanged would stage another full audit copy on every start or write, so
    automatic upgrades migrate only when a source holds an unpublished event.
    Unreadable or invalid sources count as unpublished; migrate reports them.
    """
    target = Path(destination).expanduser().resolve() / "events.jsonl"
    if not target.exists():
        return bool(sources)
    with ledger_lock(target), target.open("rb") as handle, locked_file(handle):
        published = read_events(io.StringIO(handle.read().decode("utf-8")))
    identities = {identity for event in published
                  for identity in (event.get("event_id"), *event.get("alias_event_ids", [])) if identity}
    for source in sources:
        path = Path(source).expanduser().resolve()
        path = path / "events.jsonl" if path.is_dir() else path
        try:
            with path.open("rb") as handle, locked_file(handle):
                events = read_events(io.StringIO(handle.read().decode("utf-8")))
            source_id = source_identity(path, events)
            if any(identified(event, source_id, path)["event_id"] not in identities for event in events):
                return True
        except (OSError, ValueError, KeyError, TypeError):
            return True
    return False


def migrate(sources, destination, read_events, reduce_events):
    """Back up originals, stage/validate, atomically publish; repeated imports are safe."""
    destination = Path(destination).expanduser().resolve()
    target = destination / "events.jsonl"
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    with ledger_lock(target, exclusive=True):
        paths = []
        if target.exists():
            paths.append(target)
        for source in sources:
            source = Path(source).expanduser().resolve()
            source = source / "events.jsonl" if source.is_dir() else source
            if source not in paths:
                paths.append(source)
        streams = []
        for path in paths:
            with path.open("rb") as handle, locked_file(handle):
                raw = handle.read()
            events = read_events(io.StringIO(raw.decode("utf-8")))
            reduce_events(events)
            source_id = source_identity(path, events)
            identified_events, current_workflow = [], None
            for event in events:
                recorded = identified(event, source_id, path)
                if event["type"] == "workflow.created":
                    current_workflow = event["plan"]["run_id"]
                elif event["type"] in {"workflow.extended", "workflow.bound", "node.updated"} and "workflow_id" not in event:
                    # Legacy implicit workflow references are local to each
                    # source stream, never to the interleaved merged ordering.
                    recorded["workflow_id"] = current_workflow
                identified_events.append(recorded)
            streams.append({"path": str(path), "source_id": source_id, "raw": raw,
                "events": events, "identified": identified_events})
        migration_id = digest(canonical([{k: stream[k] for k in ("path", "source_id")} | {
            "sha256": digest(stream["raw"])} for stream in streams]).encode())
        audit = destination / "migrations" / migration_id
        audit.mkdir(parents=True, exist_ok=True, mode=0o700)
        source_manifest = []
        for index, stream in enumerate(streams):
            backup = audit / (str(index) + "-events.jsonl")
            if not backup.exists():
                atomic_write(backup, stream["raw"])
            elif backup.read_bytes() != stream["raw"]:
                raise ValueError("Migration backup content mismatch")
            config = Path(stream["path"]).parent / "config.json"
            entry = {"path": stream["path"], "source_id": stream["source_id"], "sha256": digest(stream["raw"]),
                     "records": len(stream["events"]), "backup": str(backup)}
            if config.exists():
                raw = config.read_bytes()
                config_backup = audit / (str(index) + "-config.json")
                if not config_backup.exists():
                    atomic_write(config_backup, raw)
                entry.update(config_sha256=digest(raw), config_backup=str(config_backup))
            source_manifest.append(entry)
        manifest = {"migration_id": migration_id, "sources": source_manifest, "status": "staged"}
        manifest_path = audit / "manifest.json"
        atomic_write(manifest_path, (canonical(manifest) + "\n").encode())
        try:
            result = merge_streams(streams)
            reduce_events(result)
            raw = ("".join(canonical(event) + "\n" for event in result)).encode("utf-8")
            # Validate the exact staged bytes, including full tail and sequences.
            reduce_events(read_events(io.StringIO(raw.decode("utf-8"))))
            if not target.exists() or target.read_bytes() != raw:
                atomic_write(target, raw)
            manifest.update(status="published", records=len(result), sha256=digest(raw),
                aliases=[{"event_id": event["event_id"], "alias_event_ids": event["alias_event_ids"]}
                         for event in result if event.get("alias_event_ids")])
        except Exception as exc:
            manifest.update(status="rejected", error=str(exc))
            atomic_write(manifest_path, (canonical(manifest) + "\n").encode())
            raise
        atomic_write(manifest_path, (canonical(manifest) + "\n").encode())
        return {**manifest, "manifest": str(manifest_path), "journal_dir": str(destination)}
