"""Incremental read-only adapter for local Codex rollout records.

Public messages, tool inputs/results and lifecycle records only. Private reasoning
and encrypted bodies are excluded. A separate journal captures original messages
when the coordinator/agent sends them.
"""
import hashlib
import json
import re
import threading
import time
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from codex_context import AppContextReader
from storage import validate_storage
from journal import resolve_directory as resolve_journal_directory


def redact(text):
    text = re.sub(r"gAAAAA[A-Za-z0-9_=-]{40,}", "[암호화된 본문]", str(text))
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{16,}\b", "[키 숨김]", text)
    return text


def content_text(content):
    if isinstance(content, str):
        return redact(content)
    if isinstance(content, list):
        return "\n".join(redact(c.get("text", "")) for c in content
                         if isinstance(c, dict) and c.get("type") != "encrypted_content")
    return ""


def user_request(text):
    """Extract the human request, excluding app metadata and attachment markup."""
    if "## My request:" in text:
        text = text.split("## My request:", 1)[1]
    for tag in ("environment_context", "in-app-browser-context", "external_codex_apps_open_page",
                "send_user_message_question_reply", "image"):
        text = re.sub(rf"<{tag}\b[^>]*>.*?</{tag}>", "", text, flags=re.S)
    text = re.sub(r"<image\b[^>]*>", "", text)
    return redact(text).strip()


def codex_instructions(request):
    """Whether a user payload is only Codex's injected AGENTS.md wrapper.

    Codex sends project instructions as a user message in this fixed shape. Only
    the whole generated wrapper counts; a heading alone or a wrapper followed by
    other text stays a user request.
    """
    return bool(re.fullmatch(r"# AGENTS\.md instructions for [^\n]+\n+<INSTRUCTIONS>.*</INSTRUCTIONS>",
                             request, flags=re.S))


def timestamp(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, AttributeError):
        return None


def journal_record_id(event):
    """Shared imports retain an identity independent of rewritten ledger seq."""
    return "journal:" + str(event.get("event_id") or event["seq"])


def final_report_body(text):
    """Compare/display public final text while keeping source records untouched.

    Codex completion events omit the final answer's trailing memory citation
    metadata. Only a complete, standalone suffix outside a Markdown code fence
    is omitted here; different substantive text is never matched approximately.
    """
    suffix = re.search(
        r"^<oai-mem-citation>[ \t]*\r?\n<citation_entries>[^<]*</citation_entries>[ \t]*\r?\n"
        r"<rollout_ids>[^<]*</rollout_ids>[ \t]*\r?\n</oai-mem-citation>[ \t]*(?:\r?\n[ \t]*)*\Z",
        text, flags=re.M | re.S)
    if suffix and text[:suffix.start()].strip():
        fence = None
        for line in text[:suffix.start()].splitlines():
            marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
            if not marker:
                continue
            run, tail = marker.groups()
            if fence is None:
                fence = run
            elif run[0] == fence[0] and len(run) >= len(fence) and not tail.strip():
                fence = None
        if fence is None:
            text = text[:suffix.start()]
    return re.sub(r"(?:\r?\n[ \t]*)+\Z", "", text)


def group_message_records(records, turns):
    """Build presentation cards without discarding the original source records.

    A final report has one sender turn, at most one record from each source,
    and at most one matching receipt. Repeated or ambiguous records stay apart.
    Text alone is never a delivery identity.
    """
    groups, used = [], set()
    ordered = sorted(records, key=lambda r: (timestamp(r.get("at")) or 0, r["id"]))

    def same_final_report(a, b):
        return (a["from_agent"], a["to_agent"], final_report_body(a["message"])) == (
            b["from_agent"], b["to_agent"], final_report_body(b["message"]))

    def readable(r):
        return not r.get("encrypted") and not any(
            marker in r.get("message", "") for marker in ("[키 숨김]", "[암호화된 본문]"))

    # Old journals have no shared call ID. Use a deliberately narrow, mutually
    # unique candidate window, and expose this as an inference, not decryption.
    # Count plaintext calls as competitors too: an opaque call must not steal
    # a journal belonging to a nearby readable send.
    journals = [r for r in ordered if r.get("record_kind") == "sender_journal"
                and readable(r) and r.get("message", "").strip()]
    calls = [r for r in ordered if r.get("record_kind") == "collaboration_call"]
    call_ids = {call["id"] for call in calls}

    def explicit_call(journal):
        # collaboration_call_id names a Codex call. assignment_id is the writer's
        # own task ID and identifies a call only when it equals an observed one.
        if journal.get("collaboration_call_id"):
            return journal["collaboration_call_id"]
        return journal["assignment_id"] if journal.get("assignment_id") in call_ids else None

    journal_calls, call_journals = {}, {}
    for journal in journals:
        for call in calls:
            session, turn_id = call.get("sender_session_id"), call.get("sender_turn_id")
            if (not session or not turn_id or not call.get("project_path")
                    or not call.get("from_agent") or not call.get("to_agent")
                    or journal.get("recorder_session_id") != session
                    or any(journal.get(key) != call.get(key) for key in (
                        "project_path", "sender_session_id", "sender_turn_id", "from_agent", "to_agent"))):
                continue
            if journal.get("type") not in ({"assignment"} if call.get("type") == "assignment"
                                           else {"message", "result", "progress"}):
                continue
            explicit_id = explicit_call(journal)
            if explicit_id and explicit_id != call["id"]:
                continue
            recorded, sent = timestamp(journal.get("at")), timestamp(call.get("at"))
            turn = turns.get(f"{session}:{turn_id}", {})
            start, end = timestamp(turn.get("started_at")), timestamp(turn.get("finished_at"))
            if (recorded is None or sent is None or start is None
                    or (explicit_id and not start <= recorded)
                    or (not explicit_id and (not start <= recorded <= sent or sent - recorded > 30))
                    or not start <= sent
                    or (turn.get("finished_at") and (end is None or recorded > end))
                    or (turn.get("finished_at") and (end is None or sent > end))):
                continue
            journal_calls.setdefault(journal["id"], []).append(call)
            call_journals.setdefault(call["id"], []).append(journal)
    journal_matches = {call_id: items[0] for call_id, items in call_journals.items()
                       if len(items) == 1 and len(journal_calls[items[0]["id"]]) == 1
                       and (next(c for c in journal_calls[items[0]["id"]]
                                 if c["id"] == call_id).get("encrypted")
                            or explicit_call(items[0]))}

    def card(items, identity, basis, primary=None):
        items = sorted(items, key=lambda r: (timestamp(r.get("at")) or 0, r["id"]))
        used.update(r["id"] for r in items)
        groups.append({**(primary or items[0]), "id": identity, "at": items[0]["at"],
                       "last_record_at": items[-1]["at"], "records": items,
                       "record_count": len(items), "correlation_basis": basis})

    finals = {}
    for r in ordered:
        # Even an ambiguous explicit-send journal must not become an automatic
        # final-report anchor merely because its text matches task_complete.
        if (r["id"] not in journal_calls and r["type"] == "result" and readable(r) and r.get("sender_turn_id")
                and r.get("sender_session_id") and r.get("record_kind") in {
                    "sender_journal", "task_complete", "assistant_final_answer"}):
            if r["record_kind"] == "sender_journal" and any(
                    x.get("record_kind") == "collaboration_call" and readable(x) and same_final_report(r, x)
                    and x.get("sender_session_id") == r["sender_session_id"]
                    and x.get("sender_turn_id") == r["sender_turn_id"] for x in ordered):
                continue  # An explicit send is distinct from the automatic final report.
            key = (r["sender_session_id"], r["sender_turn_id"], r["from_agent"], r["to_agent"],
                   final_report_body(r["message"]))
            finals.setdefault(key, []).append(r)

    # Only unambiguous one-per-source anchors are eligible for correlation.
    anchors = {key: items for key, items in finals.items() if all(
        sum(r["record_kind"] == kind for r in items) <= 1
        for kind in ("sender_journal", "task_complete", "assistant_final_answer"))}
    receipts = {}
    for r in ordered:
        if r.get("record_kind") != "agent_message" or r["type"] != "result" or not readable(r):
            continue
        candidates = []
        for key, items in anchors.items():
            complete = next((x for x in items if x["record_kind"] == "task_complete"), None)
            if not complete or not same_final_report(complete, r):
                continue
            finished, received = timestamp(complete["at"]), timestamp(r["at"])
            if finished is None or received is None or not 0 <= received - finished <= 120:
                continue
            if complete.get("recipient_session_id") != r.get("session_id"):
                continue
            turn = turns.get(f"{key[0]}:{key[1]}", {})
            recipient_turn = turns.get(f"{r.get('session_id')}:{r.get('recipient_turn_id')}", {})
            if (turn.get("root_turn_id") and recipient_turn.get("root_turn_id")
                    and turn["root_turn_id"] != recipient_turn["root_turn_id"]):
                continue
            # Never assign a delayed report to an already-started subsequent turn.
            later = [timestamp(t["started_at"]) for t in turns.values()
                     if t["session_id"] == key[0] and timestamp(t["started_at"]) is not None
                     and timestamp(t["started_at"]) > timestamp(turn.get("started_at", complete["at"]))]
            if later and received >= min(later):
                continue
            candidates.append(key)
        if len(candidates) == 1:
            receipts.setdefault(candidates[0], []).append(r)

    for key, items in anchors.items():
        matches = receipts.get(key, [])
        # Multiple FINAL_ANSWER receipts can represent actual repeated deliveries.
        # Keep those receipts separate instead of guessing which one was original.
        linked = items + (matches if len(matches) == 1 else [])
        digest = hashlib.sha256(json.dumps(key[2:], ensure_ascii=False).encode()).hexdigest()
        basis = ("동일 작업 회차의 최종 보고 · 공개 본문·방향·수신 구간 일치" if len(matches) == 1
                 else "동일 송신 회차·공개 본문·방향 확인" if len(items) > 1 else "송신 작업 회차 확인 · 수신 연결 미확인")
        primary = next((r for r in items if r["record_kind"] == "task_complete"), items[0])
        card(linked, f"delivery:result:{key[0]}:{key[1]}:{digest}", basis,
             {**primary, "message": key[4]})

    # Ciphertext is never displayed. Its opaque delivery token can connect a
    # single outgoing call to a single received record, preserving both IDs.
    encrypted = {}
    for r in ordered:
        if r["id"] not in used and r.get("encrypted") and r.get("delivery_key"):
            key = (r["delivery_key"], r["from_agent"], r["to_agent"])
            encrypted.setdefault(key, []).append(r)
    for key, items in encrypted.items():
        kinds = [r.get("record_kind") for r in items]
        paired = len(items) == 2 and set(kinds) == {"collaboration_call", "agent_message"}
        if not paired and not (len(items) == 1 and kinds == ["collaboration_call"]):
            continue
        outgoing = next(r for r in items if r["record_kind"] == "collaboration_call")
        journal = journal_matches.get(outgoing["id"])
        digest = hashlib.sha256(json.dumps(key).encode()).hexdigest()
        if journal:
            explicit = bool(explicit_call(journal))
            primary = {**outgoing, "message": journal["message"], "type": journal["type"],
                       "encrypted": False, "message_source": "sender_journal",
                       "message_source_record_id": journal["id"],
                       "journal_match": "explicit" if explicit else "inferred"}
            primary.update({key: journal[key] for key in (
                "assignment_id", "collaboration_call_id", "recipient_session_id", "recipient_turn_id",
                "execution_request_id", "root_turn_id", "role") if key in journal})
            basis = ("송신 원문 저널 명시 호출 ID 연결 · 본문 일치 미검증" if explicit else
                     "송신 원문 저널 자동 연결 · 동일 프로젝트·송신 세션·회차·방향의 30초 이내 유일 후보 · 본문 일치 미검증")
            if paired:
                basis += " · 송수신은 동일 암호화 전달 식별자"
            card([*items, journal], "delivery:encrypted:" + digest, basis, primary)
        elif paired:
            card(items, "delivery:encrypted:" + digest, "동일 암호화 전달 식별자", outgoing)
    for call in calls:
        journal = journal_matches.get(call["id"])
        if (journal and call["id"] not in used and journal["id"] not in used
                and not call.get("encrypted") and call.get("message") == journal.get("message")):
            card([journal, call], "delivery:call:" + call["id"], "명시 호출 ID·공개 본문·방향 확인",
                 {**call, "type": journal["type"]})
    for r in ordered:
        if r["id"] not in used:
            card([r], r["id"], "원본 기록 · 다른 전달과의 연결 미확인")
    groups = group_result_journal_summaries(groups, turns, calls)
    return sorted(groups, key=lambda r: (timestamp(r.get("at")) or 0, r["id"]))


def group_result_journal_summaries(cards, turns, calls):
    """Present a pre-completion journal summary with its final report.

    These can be different public bodies, not identical deliveries. Only direct,
    standalone result journals in one observed completed turn are eligible. Any
    intervening explicit send, repeated journal or multiple completion prevents
    this presentation grouping. Keep every body in the original records.
    """
    journals = [c for c in cards if c["type"] == "result" and not c.get("encrypted")
                and c["record_count"] == 1 and c["records"][0].get("record_kind") == "sender_journal"]
    original_journals = [r for c in cards for r in c["records"]
                         if r.get("record_kind") == "sender_journal" and r.get("type") == "result"]
    completions = [(c, r) for c in cards for r in c["records"] if r.get("record_kind") == "task_complete"]
    additions, attached = {}, set()
    for journal_card in journals:
        journal = journal_card["records"][0]
        session, turn_id = journal.get("sender_session_id"), journal.get("sender_turn_id")
        if (not session or not turn_id or not journal.get("project_path")
                or journal.get("recorder_session_id") != session
                or not journal.get("message", "").strip()
                or any(marker in journal["message"] for marker in ("[키 숨김]", "[암호화된 본문]"))):
            continue
        context = ("project_path", "sender_session_id", "sender_turn_id", "from_agent", "to_agent")
        same_context = lambda r: all(r.get(k) == journal.get(k) for k in context)
        repeated = [r for r in original_journals if same_context(r)
                    and final_report_body(r["message"]) == final_report_body(journal["message"])]
        if len(repeated) != 1:
            continue
        recorded = timestamp(journal.get("at"))
        turn = turns.get(f"{session}:{turn_id}", {})
        start, end = timestamp(turn.get("started_at")), timestamp(turn.get("finished_at"))
        if recorded is None or start is None or end is None or not start <= recorded <= end:
            continue
        matching_completions = [(c, r) for c, r in completions if same_context(r)]
        if len(matching_completions) != 1:
            continue
        candidates = []
        for final_card, complete in matching_completions:
            finished = timestamp(complete.get("at"))
            if (not same_context(complete) or final_card.get("encrypted") or finished is None
                    or not recorded <= finished <= end or finished - recorded > 120):
                continue
            if any(all(call.get(k) == journal.get(k) for k in context if k != "to_agent")
                   and timestamp(call.get("at")) is not None
                   and recorded <= timestamp(call["at"]) <= finished for call in calls):
                continue
            candidates.append(final_card)
        if len(candidates) == 1:
            additions.setdefault(candidates[0]["id"], []).append(journal)
            attached.add(journal_card["id"])
    result = []
    for card in cards:
        if card["id"] in attached:
            continue
        summaries = additions.get(card["id"], [])
        if summaries:
            records = sorted([*card["records"], *summaries], key=lambda r: (timestamp(r.get("at")) or 0, r["id"]))
            card = {**card, "records": records, "record_count": len(records), "at": records[0]["at"],
                    "report_group_kind": "completion_with_journal_summaries",
                    "related_journal_record_ids": [r["id"] for r in summaries],
                    "correlation_basis": card["correlation_basis"] +
                        " · 동일 완료 회차의 전송 없는 사전 저널 요약 묶음 · 다른 본문은 출처에 보존"}
        result.append(card)
    return result


def graph_relations(nodes, selected, root_id, cards, dispatches=()):
    """Project all public routing onto execution turns, without inventing tasks.

    Grouped cards are logical deliveries; their originals carry the reliable
    sender/receipt turn metadata. Uncertain routing gets a separate endpoint,
    never the newest turn of an agent. These endpoints are not execution nodes.
    """
    by_id = {node["id"]: node for node in nodes}
    entries = {entry["id"]: entry for entry in selected}
    paths = {entry["id"]: entry["agent_id"] for entry in selected}
    roots = {node.get("root_turn_id", node.get("turn_id")): node for node in nodes
             if node.get("session_id", node["id"]) == root_id}
    endpoints, relations = {}, {}
    dispatches = {dispatch["id"]: dispatch for dispatch in dispatches}

    def digest(value):
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def path(value):
        return paths.get(value, value)

    def candidates(agent, sessions=()):
        return [node for node in nodes if path(node.get("agent_id")) == path(agent)
                and (not sessions or node.get("session_id", node["id"]) in sessions)]

    def refs(records, side, basis):
        return [{"id": record["id"], "source": record.get("record_kind", record.get("source")),
                 "side": side, "basis": basis,
                 **{key: record[key] for key in ("at", "session_id", "sender_session_id", "sender_turn_id",
                     "recipient_session_id", "recipient_turn_id", "execution_request_id", "root_turn_id")
                    if record.get(key) is not None}} for record in records]

    def resolve(agent, records, side, card):
        if agent == "user":
            return {"user": True, "resolution": "resolved", "refs": refs(records, side, "user_routing")}
        originals = [dict(origin["original_event"], id="journal-origin:" + str(origin.get("source_id")) +
                          ":" + str(origin.get("source_seq")), record_kind="journal_origin")
                     for record in records for origin in record.get("journal_origins", [])
                     if isinstance(origin.get("original_event"), dict)]
        records = [*records, *originals]
        sessions, turns, execution_ids, root_turns = set(), set(), set(), set()
        agent_key = "from_agent" if side == "sender" else "to_agent"
        mismatched_routing = any(record.get(agent_key) and path(record[agent_key]) != path(agent) for record in records)
        for record in records:
            if record.get(side + "_session_id"):
                sessions.add(record[side + "_session_id"])
            if record.get(side + "_turn_id"):
                turns.add(record[side + "_turn_id"])
            if side == "recipient" and record.get("record_kind") in {"agent_message", "user_request"}:
                if record.get("session_id"):
                    sessions.add(record["session_id"])
            if side == "sender" and record.get("record_kind") in {
                    "collaboration_call", "assistant_progress", "assistant_final_answer", "task_complete"}:
                if record.get("session_id"):
                    sessions.add(record["session_id"])
            if side == "recipient" and record.get("execution_request_id") and card.get("type") == "assignment":
                execution_ids.add(record["execution_request_id"])
            if card.get("type") == "assignment" and record.get("root_turn_id"):
                root_turns.add(record["root_turn_id"])
        available = candidates(agent)
        base = {"refs": refs(records, side, "explicit_turn"), "candidate_node_ids": [n["id"] for n in available]}
        if mismatched_routing or len(sessions) > 1 or len(turns) > 1 or len(execution_ids) > 1 or len(root_turns) > 1:
            return {**base, "resolution": "conflicting", "basis": "conflicting_session_or_turn"}
        if sessions and any(session not in entries or path(entries[session]["agent_id"]) != path(agent)
                            for session in sessions):
            return {**base, "resolution": "conflicting" if available else "unresolved",
                    "basis": "session_agent_mismatch"}
        available = candidates(agent, sessions)
        base["candidate_node_ids"] = [n["id"] for n in available]
        if root_turns:
            matched = [node for node in available if node.get("root_turn_id") in root_turns]
            if available and not matched:
                return {**base, "resolution": "conflicting", "basis": "conflicting_root_request"}
            available = matched
        if turns or execution_ids:
            exact = [node for node in available if (not turns or node.get("turn_id") in turns)
                     and (not execution_ids or node["id"] in execution_ids)]
            if len(exact) == 1:
                sender_claims = [record for record in records if record.get("sender_turn_id")]
                inferred = side == "sender" and sender_claims and all(record.get("sender_turn_inferred") for record in sender_claims)
                basis = "unique_execution_interval" if inferred else "explicit_turn"
                return {**base, "node": exact[0], "resolution": "inferred" if inferred else "resolved", "basis": basis,
                        "refs": refs(records, side, basis)}
            return {**base, "resolution": "conflicting" if turns and execution_ids else "unresolved",
                    "basis": "explicit_turn_not_observed" if not exact else "ambiguous_explicit_turn"}
        if side == "recipient" and card.get("type") == "assignment":
            record_ids = {record["id"] for record in records}
            dispatched = [node for node in available if node.get("assignment_call_id") in record_ids]
            if len(dispatched) == 1:
                return {**base, "node": dispatched[0], "resolution": "resolved", "basis": "observed_dispatch",
                        "refs": refs(records, side, "observed_dispatch")}
            if dispatched:
                return {**base, "resolution": "unresolved", "basis": "ambiguous_dispatch"}
        # Prefer physical source/receipt time over a pre-send journal timestamp.
        timed = [record for record in records if record.get("record_kind") in (
            {"agent_message", "user_request"} if side == "recipient" else
            {"collaboration_call", "task_complete", "assistant_final_answer", "assistant_progress"})]
        times = {timestamp(record.get("at")) for record in timed or records} - {None}
        possible = [node for node in available if times and all(
            timestamp(node.get("started_at")) is not None and timestamp(node["started_at"]) <= at
            and (not node.get("finished_at") or (timestamp(node["finished_at"]) is not None
                 and at <= timestamp(node["finished_at"]))) for at in times)]
        if len(possible) == 1:
            return {**base, "node": possible[0], "resolution": "inferred", "basis": "unique_execution_interval",
                    "refs": refs(timed or records, side, "unique_execution_interval")}
        return {**base, "resolution": "unresolved",
                "basis": "ambiguous_execution_interval" if possible else "execution_interval_not_observed"}

    def endpoint(agent, side, other, card):
        if side.get("node"):
            return side["node"]["id"]
        root_turn = (other.get("node", {}).get("root_turn_id") or card.get("root_turn_id")
                     or next((r.get("root_turn_id") for r in card.get("records", []) if r.get("root_turn_id")), None))
        root = roots.get(root_turn, {})
        wave = root.get("wave", other.get("node", {}).get("wave", len(roots)))
        kind = "user" if side.get("user") else "unresolved"
        identity = "relation:" + kind + ":" + digest([root_id, root_turn, path(agent), kind,
            None if kind == "user" else [side["basis"], sorted(side.get("candidate_node_ids", []))]])
        if identity not in endpoints:
            known = [entry for entry in selected if path(entry["agent_id"]) == path(agent)]
            nickname = "사용자" if kind == "user" else known[0].get("nickname", path(agent)) if len(known) == 1 else path(agent)
            reasons = {"conflicting_session_or_turn": "송수신 세션·회차 근거가 충돌합니다",
                "session_agent_mismatch": "세션과 에이전트의 연결이 확인되지 않았습니다",
                "explicit_turn_not_observed": "지정된 작업 회차를 관찰하지 못했습니다",
                "ambiguous_explicit_turn": "지정된 회차에 여러 작업이 대응합니다",
                "ambiguous_dispatch": "위임 호출에 여러 작업 회차가 대응합니다",
                "conflicting_root_request": "위임의 사용자 요청 회차와 실행 회차가 충돌합니다",
                "ambiguous_execution_interval": "동일 시점에 여러 작업 회차가 대응합니다",
                "execution_interval_not_observed": "전달 시점의 작업 회차가 확인되지 않았습니다"}
            endpoints[identity] = {"id": identity, "node_kind": "relation_endpoint", "kind": kind,
                "agent_id": path(agent), "nickname": nickname,
                "title": "사용자" if kind == "user" else f"{nickname} · 회차 미확인",
                "detail": "사용자 요청·공개 진행·최종 보고의 전달 상대" if kind == "user" else reasons.get(side["basis"], side["basis"]),
                "wave": wave, "root_turn_id": root_turn, "resolution": side["resolution"],
                "candidate_node_ids": side.get("candidate_node_ids", [])}
        return identity

    def add(kind, source_id, target_id, source_agent, target_agent, card=None, resolution="resolved", evidence=()):
        key = (kind, source_id, target_id)
        relation = relations.setdefault(key, {"id": "relation:" + digest(key), "kind": kind,
            "from_node_id": source_id, "to_node_id": target_id,
            "from_agent": path(source_agent), "to_agent": path(target_agent), "message_ids": [],
            "record_ids": [], "count": 0, "first_at": None, "last_at": None,
            "resolution": resolution, "refs": [], "deliveries": []})
        severity = {"resolved": 0, "inferred": 1, "unresolved": 2, "conflicting": 3}
        if severity[resolution] > severity[relation["resolution"]]:
            relation["resolution"] = resolution
        if card and card["id"] not in relation["message_ids"]:
            relation["message_ids"].append(card["id"])
            records = card.get("records", [card])
            ids = [record["id"] for record in records]
            relation["record_ids"] = sorted(set(relation["record_ids"] + ids))
            relation["count"] += 1
            times = [record.get("at") for record in records if timestamp(record.get("at")) is not None]
            times += [value for value in (relation["first_at"], relation["last_at"]) if value]
            if times:
                relation["first_at"], relation["last_at"] = min(times, key=timestamp), max(times, key=timestamp)
            relation["deliveries"].append({"message_id": card["id"], "record_ids": ids,
                "encrypted": bool(card.get("encrypted")), "correlation_basis": card.get("correlation_basis"),
                "resolution": resolution, "refs": list(evidence)})
        for ref in evidence:
            if ref not in relation["refs"]:
                relation["refs"].append(ref)
        return relation

    # Parent/dispatch lines remain even if their assignment body is unavailable.
    for node in nodes:
        parent = by_id.get(node.get("parent_id"))
        if parent:
            call_id = node.get("assignment_call_id")
            evidence = [{"source": node.get("parent_relation") or "session_lineage",
                         "id": call_id or node["id"], "basis": "parent_id"}]
            relation = add("delegation", parent["id"], node["id"], parent.get("agent_id"), node.get("agent_id"), evidence=evidence)
            if call_id in dispatches:
                relation["record_ids"].append(call_id)
                relation["first_at"] = relation["last_at"] = dispatches[call_id]["at"]
    seen = set()
    for card in cards:
        if card["id"] in seen:
            continue  # A repeated presentation card is not a retransmission.
        seen.add(card["id"])
        records = card.get("records", [card])
        sender, recipient = card.get("from_agent"), card.get("to_agent")
        source, target = resolve(sender, records, "sender", card), resolve(recipient, records, "recipient", card)
        source_id, target_id = endpoint(sender, source, target, card), endpoint(recipient, target, source, card)
        severity = {"resolved": 0, "inferred": 1, "unresolved": 2, "conflicting": 3}
        resolution = max((source["resolution"], target["resolution"]), key=severity.get)
        kind = card.get("type") if card.get("type") in {"assignment", "message", "result", "progress", "request"} else "message"
        if kind == "assignment" and ("delegation", source_id, target_id) in relations:
            kind = "delegation"
        evidence = source["refs"] + target["refs"]
        evidence += [{"source": "endpoint_resolution", "side": side, "basis": value.get("basis", "user_routing"),
                      "resolution": value["resolution"], "node_id": identity,
                      "candidate_node_ids": value.get("candidate_node_ids", [])}
                     for side, value, identity in (("sender", source, source_id), ("recipient", target, target_id))]
        add(kind, source_id, target_id, sender, recipient, card, resolution, evidence)
    return (sorted(relations.values(), key=lambda relation: (relation["from_node_id"], relation["to_node_id"], relation["kind"])),
            sorted(endpoints.values(), key=lambda node: (node["wave"], node["kind"], node["id"])))


def execution_event_rows(events, messages):
    """Project known delivery sources into a flat timeline, preserving raw events.

    Reuse the audited message-card identities; never deduplicate by body/time.
    Completion remains a lifecycle event at its observed completion timestamp.
    """
    by_record = {}
    for index, event in enumerate(events):
        identity = None
        if event.get("type") == "agent.message" and event.get("seq") is not None:
            identity = journal_record_id(event)
        elif (event.get("type") == "task_complete" and event.get("source") == "codex_session"
              and str(event.get("id", "")).startswith("lifecycle:")):
            identity = event["id"][len("lifecycle:"):]
        elif event.get("type") == "progress" and event.get("source") == "codex_session":
            identity = event.get("id")
        if identity:
            by_record.setdefault(identity, []).append((index, event))
    owners = {}
    for card_index, message in enumerate(messages):
        for identity in {r["id"] for r in message.get("records", []) if r["id"] in by_record}:
            owners.setdefault(identity, set()).add(card_index)
    claimed, rows = set(), []
    for message in messages:
        if message.get("encrypted") or not message.get("message", "").strip():
            continue
        records = message.get("records", [])
        identities = [r["id"] for r in records]
        if (len(identities) != len(set(identities))
                or any(len(owners[identity]) != 1 for identity in identities if identity in owners)):
            continue
        matches = [by_record[r["id"]] for r in records if r["id"] in by_record]
        if not matches or any(len(items) != 1 for items in matches):
            continue  # Ambiguous original identities must not lose an event.
        sources = [items[0] for items in matches]
        if any(index in claimed for index, event in sources):
            continue
        primary = next((event for index, event in sources if event["type"] == "task_complete"), sources[0][1])
        claimed.update(index for index, event in sources)
        rows.append({**primary, "id": "execution:" + message["id"],
                     "original_event_id": primary.get("id"), "delivery_id": message["id"],
                     "message": message["message"], "from_agent": message["from_agent"],
                     "to_agent": message["to_agent"], "message_kind": message["type"],
                     "source_events": [event for index, event in sources], "source_event_count": len(sources),
                     "records": records, "correlation_basis": message.get("correlation_basis")})
    rows.extend(event for index, event in enumerate(events) if index not in claimed)
    return sorted(rows, key=lambda e: (timestamp(e.get("at")) or 0, str(e.get("id", e.get("seq", "")))))


def readable_workflow_rows(events, nodes=(), created_titles=None):
    """Add display text without rewriting workflow journal events or history."""
    by_id = {node["id"]: node for node in nodes}
    statuses = {"pending": "대기", "running": "진행 중", "completed": "완료",
                "blocked": "차단", "failed": "실패", "skipped": "건너뜀"}
    result = []
    for event in events:
        kind = event.get("type")
        if kind == "node.updated":
            title = by_id.get(event.get("node_id"), {}).get("title") or "등록 작업"
            status = statuses.get(event.get("status"), "진행 내용 갱신")
            display_type, lines = "등록 작업 상태", [f"{title} · {status}"]
            if event.get("message"):
                lines.append(event["message"])
            evidence = event.get("evidence") or []
            if evidence:
                lines.append("검증 근거 · " + "\n".join(
                    item if isinstance(item, str) else json.dumps(item, ensure_ascii=False) for item in evidence))
        elif kind == "workflow.created":
            display_type = "워크플로우 등록"
            title = (created_titles or {}).get(event.get("seq"))
            lines = [title or event.get("message") or display_type]
        elif kind == "workflow.extended":
            display_type = "작업 추가"
            titles = [node["title"] for node in event.get("nodes", []) if node.get("title")]
            lines = [event.get("message") or display_type, *titles]
        else:
            result.append(event)
            continue
        result.append({**event, "display_type": display_type, "display_message": "\n".join(lines),
                       "source_events": [event], "source_event_count": 1})
    return result


class Collector:
    def __init__(self, directory, codex_directory=None, start_thread=True, resource_directory=None):
        self.directory = Path(directory).expanduser().resolve()
        self.config_file = self.directory / "config.json"
        self.lock = threading.RLock()
        self.files, self.messages, self.activity, self.turns, self.dispatches = {}, {}, {}, {}, {}
        self.revision, self.error, self.last_success_at = 0, None, None
        self.config = json.loads(self.config_file.read_text(encoding="utf-8")) if self.config_file.exists() else {}
        if not isinstance(self.config, dict):
            raise ValueError("Settings must be an object")
        self.project = self.config.get("project_path")
        self.root_session = self.config.get("root_session_id")
        self.codex_directory = Path(codex_directory or self.config.get("codex_dir") or Path.home() / ".codex").expanduser().resolve()
        self.resource_directory = Path(resource_directory or Path(__file__).resolve().parent).expanduser().resolve()
        validate_storage(self.directory, self.codex_directory, self.resource_directory)
        validate_storage(resolve_journal_directory(self.directory, self.config), self.codex_directory, self.resource_directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.session_root = self.codex_directory / "sessions"
        self.app_reader = AppContextReader(self.codex_directory)
        self.config.update(codex_dir=str(self.codex_directory), project_path=self.project, root_session_id=self.root_session)
        self.save_config()
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.loop, daemon=True)
        if start_thread:
            self.thread.start()

    def save_config(self):
        self.config.update(project_path=self.project, root_session_id=self.root_session,
                           codex_dir=str(self.codex_directory))
        fd, temporary = tempfile.mkstemp(prefix=".config-", dir=self.directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self.config, handle, ensure_ascii=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.config_file)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def settings(self):
        with self.lock:
            return {"project_path": self.project, "root_session_id": self.root_session,
                    "codex_dir": str(self.codex_directory)}

    def set_codex_directory(self, value):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Codex directory must be a nonempty path")
        path = Path(value).expanduser().resolve()
        if not path.is_dir():
            raise ValueError("Codex directory must exist")
        validate_storage(self.directory, path, self.resource_directory)
        validate_storage(resolve_journal_directory(self.directory, self.config), path, self.resource_directory)
        with self.lock:
            self.codex_directory, self.session_root = path, path / "sessions"
            self.app_reader = AppContextReader(path)
            self.root_session = None
            self.files, self.messages, self.activity, self.turns, self.dispatches = {}, {}, {}, {}, {}
            self.error, self.last_success_at = None, None
            self.save_config()
            self.revision += 1
        return self.settings()

    def set_project(self, value):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("존재하는 개발 프로젝트 디렉터리를 지정하세요")
        path = Path(value).expanduser().resolve()
        if not path.is_dir():
            raise ValueError("존재하는 개발 프로젝트 디렉터리를 지정하세요")
        with self.lock:
            self.project = str(path)
            self.root_session = None
            self.save_config()
            self.revision += 1
        return self.project

    def candidates(self):
        # Older roots remain selectable; directory names are not a lifecycle filter.
        return sorted(self.session_root.glob("**/*.jsonl"))

    def add(self, bucket, item):
        if bucket.get(item["id"]) != item:
            bucket[item["id"]] = item
            self.revision += 1

    def publish_activity(self, entry, item):
        item.setdefault("turn_id", entry.get("current_turn_id"))
        self.add(self.activity, item)
        if item["turn_id"] == entry.get("current_turn_id"):
            entry["latest_activity"] = {k: item[k] for k in ("title", "detail", "at", "type")}
            entry["last_activity_at"] = item["at"]
        turn = self.turns.get(f"{entry['id']}:{item['turn_id']}")
        if turn:
            turn["latest_activity_at"] = item["at"]
            turn["detail"] = item["title"] + " · " + item["detail"][:240]

    def publish_report(self, entry, identity, at, text, record_kind, turn_id):
        parent = entry.get("parent_id")
        self.add(self.messages, {"id": identity, "at": at, "type": "result",
            "from_agent": entry["agent_id"], "to_agent": parent or "user",
            "recipient_kind": "agent" if parent else "user",
            "message": redact(text), "source": "codex_session", "encrypted": False,
            "project_path": entry["cwd"], "session_id": entry["id"],
            "record_kind": record_kind, "sender_session_id": entry["id"],
            "sender_turn_id": turn_id, "recipient_session_id": parent})

    def ingest(self, entry, row):
        at = row.get("timestamp", "")
        if at < entry["created_at"]:
            return  # Full-history forks contain earlier parent records.
        p = row.get("payload", {})
        kind = p.get("type", "")
        agent = entry["agent_id"]
        # Older files may omit ordinal and public message IDs.
        ordinal = row.get("ordinal", row.get("_offset", at))
        identity = p.get("id") or p.get("call_id") or f"{entry['id']}:{ordinal}:{kind}"
        if not hasattr(self, "turns"):
            self.turns = {}
        if not hasattr(self, "dispatches"):
            self.dispatches = {}
        active_tools = entry.setdefault("active_tools", {})
        call_turns = entry.setdefault("call_turns", {})
        if row.get("type") == "event_msg":
            turn_id = p.get("turn_id") or entry.get("current_turn_id")
            # Fork bootstrap records are re-stamped at child creation. Their
            # original started_at still identifies the inherited parent turn.
            if kind == "task_started" and entry.get("parent_id") and p.get("started_at") is not None:
                created = int(datetime.fromisoformat(entry["created_at"].replace("Z", "+00:00")).timestamp())
                if p["started_at"] < created:
                    return
            if kind == "task_started":
                turn_id = turn_id or identity
                entry["status"] = "running"
                entry["finished_at"] = None
                entry["current_turn_id"] = turn_id
                active_tools.clear()
                key = f"{entry['id']}:{turn_id}"
                self.turns[key] = {"id": key, "turn_id": turn_id, "session_id": entry["id"],
                    "root_turn_id": p.get("root_turn_id") or turn_id, "agent_id": agent,
                    "status": "running", "started_at": at, "finished_at": None,
                    "latest_activity_at": at, "request": "", "title": "", "detail": "작업 시작",
                    "evidence": [f"Codex task_started · {at}"]}
            elif kind in {"task_complete", "turn_aborted", "task_failed"}:
                status = "completed" if kind == "task_complete" else "failed"
                turn = self.turns.get(f"{entry['id']}:{turn_id}")
                if entry.get("parent_id") and p.get("turn_id") and turn is None:
                    return  # Completion of a fork's ignored parent bootstrap.
                if turn:
                    turn.update(status=status, finished_at=at, latest_activity_at=at,
                                detail=redact(p.get("last_agent_message") or p.get("reason") or "작업 중단")[:500])
                    turn["evidence"].append(f"Codex {kind} · {at}")
                # A delayed completion of an earlier turn cannot stop a newer turn.
                if not entry.get("current_turn_id") or entry["current_turn_id"] == turn_id:
                    entry["status"] = status
                    entry["finished_at"] = at
                    active_tools.clear()
                text = p.get("last_agent_message", "")
                if kind == "task_complete" and text:
                    self.publish_report(entry, identity, at, text, "task_complete", turn_id)
            else:
                return
            self.publish_activity(entry, {"id": "lifecycle:" + identity, "at": at,
                "agent_id": agent, "type": kind, "turn_id": turn_id,
                "title": {"task_started": "작업 시작", "task_complete": "작업 완료",
                          "turn_aborted": "작업 중단", "task_failed": "작업 실패"}[kind],
                "detail": redact(p.get("last_agent_message") or p.get("reason") or turn_id or ""),
                "source": "codex_session", "project_path": entry["cwd"], "session_id": entry["id"]})
            return
        if row.get("type") != "response_item":
            return
        if kind == "message" and p.get("role") == "user":
            turn = self.turns.get(f"{entry['id']}:{entry.get('current_turn_id')}")
            source_body = content_text(p.get("content", []))
            request = user_request(source_body)
            if turn and codex_instructions(request):
                # Injected project instructions are not the user's request. Keep
                # their exact body and source ID apart from the request title.
                turn.setdefault("instruction_records", []).append({"id": identity, "at": at,
                    "source": "codex_session", "record_kind": "codex_instructions", "body": request})
                return
            if turn and request and not turn["request"]:
                turn["request"] = request
                turn["title"] = re.sub(r"\s+", " ", request)[:76]
            if turn and request and not entry.get("parent_id"):
                turn.setdefault("request_records", []).append({"id": identity, "at": at, "type": "request",
                    "from_agent": "user", "to_agent": agent, "recipient_kind": "agent", "message": request,
                    "source_body": source_body,
                    "source": "codex_session", "record_kind": "user_request", "encrypted": False,
                    "project_path": entry["cwd"], "session_id": entry["id"],
                    "recipient_session_id": entry["id"], "recipient_turn_id": turn["turn_id"],
                    "root_turn_id": turn["root_turn_id"]})
            return
        if kind in {"function_call", "custom_tool_call"}:
            name = p.get("name", "tool")
            raw = p.get("arguments", p.get("input", ""))
            if p.get("namespace") == "collaboration" and name in {"spawn_agent", "send_message", "followup_task"}:
                try:
                    args = json.loads(raw)
                except (ValueError, TypeError):
                    args = {}
                target = args.get("target") or args.get("task_name", "unknown")
                known_ids = {e["id"] for e in getattr(self, "files", {}).values()}
                is_session_id = bool(re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", target))
                if not target.startswith("/") and target not in known_ids and not is_session_id:
                    target = agent.rstrip("/") + "/" + target
                if name in {"spawn_agent", "followup_task"}:
                    turn = self.turns.get(f"{entry['id']}:{entry.get('current_turn_id')}")
                    self.add(self.dispatches, {"id": identity, "at": at, "session_id": entry["id"],
                        "turn_id": entry.get("current_turn_id"), "root_turn_id": turn["root_turn_id"] if turn else None,
                        "target": target, "kind": name})
                text = args.get("message", "")
                encrypted = text.startswith("gAAAAA")
                delivery_key = "collab:" + hashlib.sha256(text.encode()).hexdigest() if encrypted else None
                self.add(self.messages, {"id": identity, "at": at,
                    "type": "assignment" if name in {"spawn_agent", "followup_task"} else "message",
                    "from_agent": agent, "to_agent": target,
                    "message": "본문이 암호화되어 로컬 로그에서 읽을 수 없습니다. 송수신 기록 연결이 필요합니다." if encrypted else redact(text),
                    "source": "codex_session", "encrypted": encrypted,
                    "project_path": entry["cwd"], "session_id": entry["id"],
                    "record_kind": "collaboration_call", "delivery_key": delivery_key,
                    "sender_session_id": entry["id"], "sender_turn_id": entry.get("current_turn_id")})
            title = (p.get("namespace", "") + "." + name).lstrip(".")
            call_key = p.get("call_id") or identity
            call_turns[call_key] = entry.get("current_turn_id")
            active_tools[call_key] = {"id": identity, "title": title, "at": at}
            self.publish_activity(entry, {"id": identity, "at": at, "agent_id": agent,
                "type": "tool_call", "title": title,
                "detail": redact(raw), "source": "codex_session", "project_path": entry["cwd"],
                "session_id": entry["id"], "call_id": p.get("call_id")})
        elif kind in {"function_call_output", "custom_tool_call_output"}:
            active_tools.pop(p.get("call_id"), None)
            self.publish_activity(entry, {"id": "output:" + identity, "at": at, "agent_id": agent,
                "turn_id": call_turns.get(p.get("call_id")),
                "type": "tool_result", "title": "도구 실행 결과", "detail": content_text(p.get("output", "")),
                "source": "codex_session", "project_path": entry["cwd"], "session_id": entry["id"],
                "call_id": p.get("call_id")})
        elif (kind == "message" and p.get("role") == "assistant"
              and p.get("phase") not in {"analysis", "summary"}
              and p.get("channel") not in {"analysis", "summary"}):
            text = content_text(p.get("content", []))
            metadata = p.get("internal_chat_message_metadata_passthrough") or {}
            turn_id = p.get("turn_id") or (metadata.get("turn_id") if isinstance(metadata, dict) else None) or entry.get("current_turn_id")
            if p.get("phase") == "final_answer" and not entry.get("parent_id") and text:
                self.publish_report(entry, identity, at, text, "assistant_final_answer", turn_id)
            elif (p.get("phase") == "commentary"
                  and re.sub(r"\[(?:암호화된 본문|키 숨김)\]", "", text).strip()):
                # Public transcript commentary is visible to the user. It is
                # not an inferred delivery to a parent agent or a final answer.
                self.add(self.messages, {"id": identity, "at": at, "type": "progress",
                    "from_agent": agent, "to_agent": "user", "recipient_kind": "user",
                    "message": text, "source": "codex_session", "encrypted": False,
                    "project_path": entry["cwd"], "session_id": entry["id"],
                    "record_kind": "assistant_progress", "sender_session_id": entry["id"],
                    "sender_turn_id": turn_id, "phase": "commentary"})
            self.publish_activity(entry, {"id": identity, "at": at, "agent_id": agent,
                "turn_id": turn_id,
                "type": "result" if p.get("phase") == "final_answer" else "progress",
                "title": "최종 보고" if p.get("phase") == "final_answer" else "작업 진행 보고",
                "detail": text, "source": "codex_session",
                "project_path": entry["cwd"], "session_id": entry["id"]})
        elif kind == "agent_message":
            text = content_text(p.get("content", []))
            parts = p.get("content", [])
            ciphertext = next((c.get("encrypted_content") for c in parts if c.get("type") == "encrypted_content"), None)
            message_type = re.search(r"Message Type: (\w+)", text)
            body = text.split("Payload:\n", 1)[-1] if "Payload:\n" in text else text
            delivery_key = "collab:" + hashlib.sha256(ciphertext.encode()).hexdigest() if ciphertext else None
            metadata = p.get("internal_chat_message_metadata_passthrough") or {}
            self.add(self.messages, {"id": identity, "at": at, "type": "result" if message_type and message_type.group(1) == "FINAL_ANSWER" else "message",
                "from_agent": p.get("author", agent), "to_agent": p.get("recipient", agent),
                "message": "본문이 암호화되어 로컬 로그에서 읽을 수 없습니다. 송수신 기록 연결이 필요합니다." if ciphertext else redact(body),
                "source": "codex_session", "encrypted": bool(ciphertext), "project_path": entry["cwd"], "session_id": entry["id"],
                "record_kind": "agent_message", "delivery_key": delivery_key,
                "recipient_turn_id": metadata.get("turn_id") if isinstance(metadata, dict) else None})
        # Never ingest reasoning/raw_content or inherited user/system instructions.

    def scan(self):
        # Read metadata headers for discovery, then only public bodies belonging
        # to the selected root lineage. Unrelated malformed bodies stay unread.
        paths = self.candidates()
        existing = {str(path) for path in paths}
        retained = {key: entry for key, entry in self.files.items() if key in existing}
        if len(retained) != len(self.files):
            self.revision += 1  # A removed rollout changes the observed sessions.
        self.files = retained
        for path in paths:
            key = str(path)
            if key not in self.files:
                try:
                    with path.open("rb") as handle:
                        first = handle.readline()
                except OSError:
                    continue  # Removed or unreadable since discovery; retry next pass.
                if not first.endswith(b"\n"):
                    continue  # A live writer may still be completing its header.
                try:
                    row = json.loads(first)
                    if not isinstance(row, dict):
                        continue
                    meta, created = row.get("payload", {}), row.get("timestamp") or ""
                except ValueError:
                    continue
                if not isinstance(meta, dict) or not meta.get("id") or not meta.get("cwd"):
                    continue
                source = meta.get("source", {})
                subagent = source.get("subagent", {}) if isinstance(source, dict) else {}
                spawn = subagent.get("thread_spawn", {}) if isinstance(subagent, dict) else {}
                if not isinstance(spawn, dict):
                    spawn = {}
                self.files[key] = {"id": meta["id"], "cwd": str(Path(meta["cwd"]).expanduser().resolve()),
                    "source_cwd": meta["cwd"], "path": key,
                    "agent_id": spawn.get("agent_path", "/root"), "nickname": meta.get("agent_nickname", "Coordinator"),
                    "assigned_role": spawn.get("agent_role") or meta.get("agent_role"),
                    "parent_id": spawn.get("parent_thread_id"), "created_at": created,
                    "last_activity_at": created, "status": "unknown", "current_turn_id": None,
                    "latest_activity": None, "active_tools": {}, "call_turns": {}, "offset": len(first),
                    "mtime": 0, "size": None}
                self.revision += 1
        roots = sorted((entry for entry in self.files.values() if entry["cwd"] == self.project
                        and not entry["parent_id"]), key=lambda entry: entry["created_at"], reverse=True)
        root_id = self.root_session or (roots[0]["id"] if roots else None)
        accepted = {root_id} if root_id else set()
        while True:
            added = {entry["id"] for entry in self.files.values() if entry["parent_id"] in accepted} - accepted
            if not added:
                break
            accepted.update(added)
        # Each selected rollout is read independently. A truncated or malformed
        # file stays an error without stopping the rest of the lineage; a sole
        # error keeps its own type and message.
        errors = []
        for key, entry in self.files.items():
            if entry["id"] not in accepted:
                continue
            try:
                self.read_rollout(Path(key), entry)
            except Exception as exc:
                errors.append(exc)
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise ValueError("; ".join(str(error) for error in errors)) from errors[0]

    def read_rollout(self, path, entry):
        stat = path.stat()
        # Windows path metadata can lag behind writes until the writer's
        # handle closes. Probe the selected lineage's tail through a fresh
        # read-only handle even if the reported timestamp and size match.
        # POSIX retains its shortcut, but size changes matter independently
        # of timestamps (including truncation with an unchanged timestamp).
        if sys.platform != "win32":
            if stat.st_size < entry["offset"]:
                raise ValueError(f"Selected rollout was truncated; prior evidence is preserved: {path.name}")
            if stat.st_mtime_ns == entry["mtime"] and stat.st_size == entry.get("size"):
                return
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            if size < entry["offset"]:
                raise ValueError(f"Selected rollout was truncated; prior evidence is preserved: {path.name}")
            handle.seek(entry["offset"])
            previous_offset = entry["offset"]
            try:
                while True:
                    position = handle.tell()
                    line = handle.readline()
                    if not line or not line.endswith(b"\n"):
                        break  # Retry an incomplete JSON/UTF-8/CRLF tail later.
                    try:
                        row = json.loads(line)
                    except ValueError as exc:
                        raise ValueError(f"Malformed rollout JSON: {path.name}, byte {position}: {exc}") from exc
                    row["_offset"] = position
                    self.ingest(entry, row)
                    entry["offset"] = handle.tell()
            finally:
                # Commit each successful line before any later malformed
                # record. Empty probes and partial tails must not cause SSE
                # revisions (nor re-ingest completed records on retry).
                if entry["offset"] != previous_offset:
                    self.revision += 1
        entry["mtime"] = stat.st_mtime_ns
        entry["size"] = size

    def poll(self):
        """One collection pass; a rollout error still refreshes app names."""
        with self.lock:
            try:
                self.scan()
                error = None
            except Exception as exc:
                error = str(exc)
            if self.app_reader.refresh(e["id"] for e in self.files.values() if not e["parent_id"]):
                self.revision += 1
            if error is None:
                self.last_success_at = datetime.now().astimezone().isoformat()
            if self.error != error:
                self.revision += 1
            self.error = error

    def loop(self):
        while not self.stop_event.is_set():
            try:
                self.poll()
            except Exception as exc:
                with self.lock:
                    new_error = str(exc)
                    if self.error != new_error:
                        self.revision += 1
                    self.error = new_error
            self.stop_event.wait(1)

    def projects(self):
        with self.lock:
            counts = {}
            for e in self.files.values():
                counts[e["cwd"]] = counts.get(e["cwd"], 0) + 1
            return {"projects": [{"path": p, "session_count": c} for p, c in sorted(counts.items())]}

    def set_session(self, value):
        with self.lock:
            matches = [e for e in self.files.values() if e["id"] == value and e["cwd"] == self.project and not e["parent_id"]]
            if not matches:
                raise ValueError("선택한 프로젝트의 루트 세션을 지정하세요")
            self.root_session = value
            self.save_config()
            self.revision += 1
        return value

    def task_role(self, entry, turn, root_id, workflow_nodes, assignments=()):
        """Resolve an assigned responsibility independently of the nickname."""
        def resolved(candidates, source):
            roles = {candidate["role"] for candidate in candidates if candidate.get("role")}
            return {"role": next(iter(roles)) if len(roles) == 1 else None,
                    "role_source": source if len(roles) == 1 else "conflicting_assignments",
                    "role_node_ids": [candidate["id"] for candidate in candidates] if source == "workflow_assignment" else [],
                    "role_refs": [candidate["id"] for candidate in candidates], "role_scope": "task"}

        def exact_task(candidate):
            target_session = candidate.get("task_session_id")
            target_turn = candidate.get("task_turn_id")
            if any(candidate.get(key) and candidate[key] != expected for key, expected in (
                    ("execution_request_id", turn.get("id")), ("task_session_id", entry["id"]),
                    ("task_turn_id", turn.get("turn_id")))):
                return False
            return bool(candidate.get("execution_request_id") or (target_session and target_turn))

        explicit = [assignment for assignment in assignments if assignment.get("role")]
        if explicit:
            return resolved(explicit, "journal_assignment")
        assigned = [n for n in workflow_nodes if (n.get("agent_id") in {entry["agent_id"], entry["id"]}
                    or (not n.get("agent_id") and exact_task(n)))
                    and n.get("role")]
        exact = [node for node in assigned if exact_task(node)
                 and (not node.get("root_turn_id") or node["root_turn_id"] == turn.get("root_turn_id"))]
        if exact:
            return resolved(exact, "workflow_assignment")
        if entry["id"] == root_id:
            return {"role": "Coordinator", "role_source": "root_coordinator", "role_node_ids": [],
                    "role_refs": [], "role_scope": "session"}
        session_turns = sorted((t for t in getattr(self, "turns", {}).values() if t["session_id"] == entry["id"]),
                               key=lambda t: t["started_at"])
        # Spawn metadata describes the original assignment. An explicit later
        # task overrides it, and a reused worker does not inherit that old role.
        if entry.get("assigned_role") and (not session_turns or turn.get("id") == session_turns[0]["id"]):
            return {"role": entry["assigned_role"], "role_source": "session_metadata", "role_node_ids": [],
                    "role_refs": [entry["id"]], "role_scope": "initial_assignment"}
        assigned = [node for node in assigned if not any(node.get(key) for key in (
            "execution_request_id", "task_session_id", "task_turn_id"))
            and (not node.get("root_turn_id") or node["root_turn_id"] == turn.get("root_turn_id"))]
        started = timestamp(turn.get("started_at"))
        finished = timestamp(turn.get("finished_at"))
        # All legacy time-based roles must belong to this actual parent request.
        if turn.get("root_turn_id"):
            root_turns = [t for t in getattr(self, "turns", {}).values() if t["session_id"] == root_id]
            def same_request(node):
                at = timestamp(node.get("started_at"))
                contexts = [t for t in root_turns if at is not None
                            and timestamp(t.get("started_at")) is not None
                            and timestamp(t["started_at"]) <= at
                            and (not t.get("finished_at") or (timestamp(t["finished_at"]) is not None
                                 and at < timestamp(t["finished_at"])))]
                return len(contexts) == 1 and contexts[0]["root_turn_id"] == turn["root_turn_id"]
            assigned = [node for node in assigned if same_request(node)]
        overlapping = [n for n in assigned if started is not None and timestamp(n.get("started_at")) is not None
                       and (finished is None or timestamp(n["started_at"]) < finished)
                       and (not n.get("finished_at") or timestamp(n["finished_at"]) > started)]
        if overlapping:
            return resolved(overlapping, "workflow_assignment")
        # Preserve late registration for an original early turn only. Finished
        # assignments can never supply a role to an unrelated subsequent turn.
        early = [n for n in assigned if finished is not None and timestamp(n.get("started_at")) is not None
                 and finished <= timestamp(n["started_at"])]
        if early and (not session_turns or turn.get("id") == session_turns[0]["id"]):
            return resolved(early, "workflow_assignment")
        return {"role": None, "role_source": "unregistered", "role_node_ids": [], "role_refs": [], "role_scope": None}

    def turn_dispatch(self, entry, turn):
        """A dispatch owns its first observed recipient turn in that request."""
        candidates = []
        for dispatch in getattr(self, "dispatches", {}).values():
            if (dispatch["target"] not in {entry["agent_id"], entry["id"]}
                    or dispatch["root_turn_id"] != turn["root_turn_id"] or dispatch["at"] > turn["started_at"]):
                continue
            following = sorted((t for t in self.turns.values() if t["session_id"] == entry["id"]
                                and t["root_turn_id"] == turn["root_turn_id"] and t["started_at"] >= dispatch["at"]),
                               key=lambda t: t["started_at"])
            if following and following[0]["id"] == turn["id"] and sum(
                    t["started_at"] == following[0]["started_at"] for t in following) == 1:
                candidates.append(dispatch)
        candidates.sort(key=lambda d: d["at"], reverse=True)
        return candidates[0] if candidates and (len(candidates) == 1 or candidates[0]["at"] != candidates[1]["at"]) else None

    def turn_assignments(self, entry, turn, messages, dispatch):
        candidates = []
        for message in messages:
            if message.get("type") != "assignment":
                continue
            records = message.get("records", [message])
            for record in records:
                if record.get("type") != "assignment" or record.get("encrypted"):
                    continue
                if (record.get("record_kind") == "sender_journal"
                        and (not record.get("sender_session_id")
                             or record.get("recorder_session_id") != record["sender_session_id"])):
                    continue  # A proxy's copy is not a direct work assignment.
                if record.get("sender_turn_id") and not self.turns.get(
                        f"{record.get('sender_session_id')}:{record['sender_turn_id']}"):
                    continue
                if record.get("to_agent") not in {entry["agent_id"], entry["id"]}:
                    continue
                if record.get("recipient_session_id") and record["recipient_session_id"] != entry["id"]:
                    continue
                if (record.get("recipient_turn_id") and record["recipient_turn_id"] != turn["turn_id"]
                        or record.get("execution_request_id") and record["execution_request_id"] != turn["id"]):
                    continue
                if record.get("root_turn_id") and record["root_turn_id"] != turn["root_turn_id"]:
                    continue
                explicit = (record.get("execution_request_id") == turn["id"] or
                            (record.get("recipient_session_id") == entry["id"]
                             and record.get("recipient_turn_id") == turn["turn_id"]))
                has_task_binding = record.get("execution_request_id") or record.get("recipient_turn_id")
                linked_call = dispatch and any(r["id"] == dispatch["id"] for r in records)
                if not explicit and (has_task_binding or not linked_call):
                    continue
                candidates.append(record)
        return candidates

    def session_name(self, session_id):
        reader = getattr(self, "app_reader", None)
        return reader.names.get(session_id, {"name": "이름 없는 세션", "name_source": "unregistered"}) if reader else {
            "name": "이름 없는 세션", "name_source": "unregistered"}

    def request_nodes(self, selected, root_id, workflow_nodes=(), messages=()):
        entries = {e["id"]: e for e in selected}
        turns = [t for t in self.turns.values() if t["session_id"] in entries]
        roots = sorted([t for t in turns if t["session_id"] == root_id], key=lambda t: t["started_at"])
        waves = {t["turn_id"]: wave for wave, t in enumerate(roots)}
        nodes = []
        for t in sorted(turns, key=lambda x: (waves.get(x["root_turn_id"], len(roots)), x["started_at"])):
            e = entries[t["session_id"]]
            dispatch = self.turn_dispatch(e, t)
            parent = (self.turns.get(f"{dispatch['session_id']}:{dispatch['turn_id']}") if dispatch else None)
            assignments = self.turn_assignments(e, t, messages, dispatch)
            bodies = {record["message"] for record in assignments if record.get("message")}
            assignment = None
            if len(bodies) == 1:
                primary = next((r for r in assignments if r.get("record_kind") == "sender_journal"), assignments[0])
                assignment = {key: primary.get(key) for key in ("id", "at", "from_agent", "to_agent", "source")}
                assignment["message"] = next(iter(bodies))
                assignment["correlation_basis"] = next((m.get("correlation_basis") for m in messages
                    if any(r["id"] == primary["id"] for r in m.get("records", []))), None)
            ids = {record["id"] for record in assignments}
            provenance = {r["id"]: r for message in messages if any(r["id"] in ids for r in message.get("records", []))
                          for r in message.get("records", [])}
            nodes.append({**t, "title": (t["title"] or f"{e['nickname']} · 에이전트 작업")
                          if e["id"] == root_id else f"{e['nickname']} · 에이전트 작업",
                "request_title": t["title"],
                "nickname": e["nickname"], **self.task_role(e, t, root_id, workflow_nodes, assignments),
                "wave": waves.get(t["root_turn_id"], len(roots)),
                "parent_id": parent["id"] if parent else None,
                "parent_relation": "observed_dispatch" if parent else None,
                "assignment": assignment, "assignment_records": list(provenance.values()),
                "assignment_source": ("sender_journal" if any(r.get("record_kind") == "sender_journal"
                                          for r in assignments) else "collaboration_call") if assignment else None,
                "assignment_call_id": dispatch["id"] if dispatch else None,
                "lineage_parent_session_id": e.get("parent_id"), "depends_on": [], "source": "codex_turn"})
        return nodes

    def conversations(self, selected, root_id, messages):
        """Observed lineage/routing establishes a dialog before public text exists."""
        paths = {entry["id"]: entry["agent_id"] for entry in selected}
        by_path = {}
        for entry in selected:
            by_path.setdefault(entry["agent_id"], []).append(entry)
        relations = {}
        def add(sender, recipient, source, ref, sender_session=None, recipient_session=None):
            sender, recipient = paths.get(sender, sender), paths.get(recipient, recipient)
            if not sender or not recipient or sender == recipient:
                return
            key = tuple(sorted((sender, recipient)))
            recipient_entries = ([entry for entry in selected if entry["id"] == recipient_session]
                                 if recipient_session else by_path.get(recipient, []))
            sender_entries = ([entry for entry in selected if entry["id"] == sender_session]
                              if sender_session else by_path.get(sender, []))
            # Root activity must not revive every previously completed dialog.
            workers = [entry for entry in recipient_entries + sender_entries if entry["id"] != root_id]
            subjects = workers or recipient_entries or sender_entries
            status = subjects[0]["status"] if len({entry["id"] for entry in subjects}) == 1 else "unknown"
            relation = relations.setdefault(key, {"id": "conversation:" + hashlib.sha256(
                json.dumps(key).encode()).hexdigest(), "from_agent": sender, "to_agent": recipient,
                "status": status, "source": source, "refs": []})
            relation["refs"].append({"source": source, "id": ref, "sender_session_id": sender_session,
                                     "recipient_session_id": recipient_session})
        for entry in selected:
            parent = entry.get("parent_id")
            if parent in paths:
                add(paths[parent], entry["agent_id"], "session_lineage", entry["id"], parent, entry["id"])
        for dispatch in getattr(self, "dispatches", {}).values():
            if dispatch.get("session_id") not in paths:
                continue
            targets = [entry for entry in selected if dispatch.get("target") in {entry["id"], entry["agent_id"]}]
            if len(targets) == 1:
                add(paths[dispatch["session_id"]], targets[0]["agent_id"], "observed_dispatch", dispatch["id"],
                    dispatch["session_id"], targets[0]["id"])
        for record in messages:
            add(record.get("from_agent"), record.get("to_agent"), "message_routing", record["id"],
                record.get("sender_session_id"), record.get("recipient_session_id"))
        return sorted(relations.values(), key=lambda relation: (relation["from_agent"], relation["to_agent"]))

    def workflow_execution(self, workflow, selected, root_id):
        """Project manual stages onto their own observed execution, without closing them.

        A worker finishing is evidence of execution ending, not approval of the
        registered stage. Keep the journal's status and require explicit stage
        completion. Bind at stage start so a later reuse cannot revive old work.
        """
        projected = []
        for original in workflow["nodes"]:
            node = {**original, "recorded_status": original["status"]}
            projected.append(node)
            if node["status"] != "running":
                continue
            started = timestamp(node.get("started_at"))
            entries = [e for e in selected if node.get("agent_id") in {e["id"], e["agent_id"]}
                       or (not node.get("agent_id") and (node.get("task_session_id") == e["id"]
                           or any(t["session_id"] == e["id"] and t["id"] == node.get("execution_request_id")
                                  for t in self.turns.values())))]
            root_turns = [t for t in self.turns.values() if t["session_id"] == root_id
                          and started is not None and timestamp(t["started_at"]) <= started
                          and (not t.get("finished_at") or timestamp(t["finished_at"]) > started)]
            context = root_turns[0] if len(root_turns) == 1 else None
            candidates = []
            explicit_binding = node.get("execution_request_id") or node.get("task_turn_id") or node.get("task_session_id")
            if explicit_binding:
                for turn in self.turns.values():
                    if turn["session_id"] not in {entry["id"] for entry in entries}:
                        continue
                    matches = (bool(node.get("execution_request_id") or
                                    (node.get("task_session_id") and node.get("task_turn_id")))
                               and not any(node.get(key) and node[key] != expected for key, expected in (
                                    ("execution_request_id", turn["id"]), ("task_session_id", turn["session_id"]),
                                    ("task_turn_id", turn["turn_id"]))))
                    if (matches and (not node.get("root_turn_id") or node["root_turn_id"] == turn["root_turn_id"])):
                        candidates.append(turn)
            elif started is not None and len(entries) == 1 and context:
                # A later stage assigned to the same worker bounds this dispatch.
                next_start = min((timestamp(n.get("started_at")) for n in workflow["nodes"]
                                  if n.get("agent_id") in {entries[0]["id"], entries[0]["agent_id"]}
                                  and timestamp(n.get("started_at")) is not None
                                  and timestamp(n["started_at"]) > started), default=float("inf"))
                previous_stages = [(timestamp(n["started_at"]), timestamp(n.get("finished_at")))
                                   for n in workflow["nodes"]
                                   if n.get("agent_id") in {entries[0]["id"], entries[0]["agent_id"]}
                                   and timestamp(n.get("started_at")) is not None
                                   and timestamp(n["started_at"]) < started]
                dispatch_at = min((timestamp(d["at"]) for d in getattr(self, "dispatches", {}).values()
                                   if d["target"] in {entries[0]["id"], entries[0]["agent_id"]}
                                   and d["root_turn_id"] == context["root_turn_id"]
                                   and timestamp(d["at"]) is not None
                                   and started <= timestamp(d["at"]) < next_start), default=None)
                for turn in self.turns.values():
                    at, end = timestamp(turn["started_at"]), timestamp(turn.get("finished_at"))
                    if turn["session_id"] != entries[0]["id"] or at is None:
                        continue
                    if turn["root_turn_id"] != context["root_turn_id"]:
                        continue
                    if dispatch_at is not None and at < dispatch_at:
                        continue  # A queued follow-up cannot reuse the currently busy turn.
                    overlaps_start = at <= started and (end is None or end > started)
                    if entries[0]["id"] != root_id and overlaps_start and any(
                            (end is None or previous < end) and (finish is None or finish > at)
                            for previous, finish in previous_stages):
                        continue  # Already assigned to an earlier worker stage.
                    follows_dispatch = started <= at < next_start
                    if overlaps_start or follows_dispatch:
                        candidates.append(turn)
            candidates.sort(key=lambda t: t["started_at"])
            # Simultaneous ambiguous records are not a reliable assignment.
            turn = candidates[0] if candidates and (len(candidates) == 1 or
                    candidates[0]["started_at"] != candidates[1]["started_at"]) else None
            node["execution_status"] = turn["status"] if turn else "unknown"
            if not turn:
                node.update(status="unconfirmed", detail="실행 미확인 · 배정된 실제 작업 회차의 시작 기록을 확인하지 못했습니다")
                continue
            node.update(execution_request_id=turn["id"], execution_started_at=turn["started_at"],
                        execution_finished_at=turn.get("finished_at"), execution_detail=turn.get("detail", ""))
            node["evidence"] = list(node.get("evidence", [])) + [turn["id"]] + list(turn.get("evidence", []))
            if turn["status"] != "running":
                outcome = "완료" if turn["status"] == "completed" else "실패·중단"
                node.update(status="awaiting_confirmation",
                            detail=f"완료 확인 대기 · 에이전트 작업 {outcome}. 워크플로우 결과 확인과 완료 등록이 필요합니다")
        return {**workflow, "nodes": projected}

    def augment(self, state):
        with self.lock:
            ledger_events = state["events"]
            selected = [e for e in self.files.values() if e["cwd"] == self.project]
            roots = sorted([e for e in selected if not e["parent_id"]], key=lambda e: e["created_at"], reverse=True)
            root_id = self.root_session or (roots[0]["id"] if roots else None)
            if root_id and self.root_session is None:
                self.root_session = root_id
                self.save_config()
            accepted = {root_id} if root_id else set()
            while True:
                added = {e["id"] for e in self.files.values() if e["parent_id"] in accepted} - accepted
                if not added:
                    break
                accepted.update(added)
            selected = [e for e in self.files.values() if e["id"] in accepted]
            sessions = [{k: v for k, v in e.items() if k not in {"mtime", "size", "offset", "active_tools", "call_turns"}} for e in selected]
            messages = sorted((m for m in self.messages.values() if m["session_id"] in accepted), key=lambda x: x["at"])
            agent_paths = {e["id"]: e["agent_id"] for e in self.files.values()}
            messages = [{**m, "from_agent": agent_paths.get(m["from_agent"], m["from_agent"]),
                         "to_agent": agent_paths.get(m["to_agent"], m["to_agent"])} for m in messages]
            activity = sorted((m for m in self.activity.values() if m["session_id"] in accepted), key=lambda x: x["at"])
            for e in state["events"]:
                if (e["type"] == "agent.message" and e.get("session_id") in accepted
                        and e.get("project_path", self.project) == self.project):
                    messages.append({"id": journal_record_id(e), "at": e["at"], "type": e.get("message_kind", "message"),
                        "from_agent": agent_paths.get(e["from_agent"], e["from_agent"]),
                        "to_agent": agent_paths.get(e["to_agent"], e["to_agent"]), "message": e["message"],
                        "source": "sender_journal", "encrypted": False, "project_path": self.project,
                        "record_kind": "sender_journal", "session_id": e.get("session_id"),
                        "recorder_session_id": e.get("session_id"),
                        "journal_event_id": e.get("event_id"), "journal_seq": e["seq"],
                        "journal_origins": e.get("journal_origins", e.get("origins", [])),
                        "journal_alias_event_ids": e.get("alias_event_ids", []),
                        **{key: e[key] for key in ("assignment_id", "collaboration_call_id", "sender_session_id",
                            "sender_turn_id", "recipient_session_id", "recipient_turn_id", "execution_request_id",
                            "root_turn_id", "role") if key in e}})
            for record in messages:
                if record.get("record_kind") == "sender_journal":
                    at = timestamp(record["at"])
                    # The coordinator can journal a child's original report.
                    # Keep the recorder session and resolve the declared sender
                    # only when its path identifies one accepted session.
                    senders = [e for e in selected if e["agent_id"] == record["from_agent"]]
                    sender = senders[0] if len(senders) == 1 else None
                    if sender and record.get("sender_session_id") in (None, sender["id"]):
                        record["sender_session_id"] = sender["id"]
                    eligible = [t for t in self.turns.values() if sender
                                and record.get("sender_session_id") == sender["id"]
                                and t["session_id"] == sender["id"] and at is not None
                                and timestamp(t["started_at"]) is not None and timestamp(t["started_at"]) <= at
                                and (not t.get("finished_at") or at <= timestamp(t["finished_at"]))]
                    if len(eligible) == 1 and not record.get("sender_turn_id"):
                        record["sender_turn_id"] = eligible[0]["turn_id"]
                        record["sender_turn_inferred"] = True
            message_records = sorted(messages, key=lambda r: (timestamp(r.get("at")) or 0, r["id"]))
            messages = group_message_records(message_records, self.turns)
            workflows = state.get("workflows", [state] if state.get("binding") else [])
            matching = [w for w in workflows if w.get("binding") == {
                "project_path": self.project, "root_session_id": root_id} and root_id]
            registered = ({**matching[-1], "nodes": [node for workflow in matching for node in workflow["nodes"]],
                           "events": [event for workflow in matching for event in workflow.get("events", [])],
                           "started_at": matching[0]["started_at"],
                           "workflow_runs": [workflow["run_id"] for workflow in matching]} if matching else None)
            registered_events = {event["seq"] for event in registered.get("events", [])
                                 if event.get("type") != "agent.message"} if registered else set()
            state = {k: v for k, v in state.items() if k != "workflows"}
            if registered:
                registered = self.workflow_execution(registered, selected, root_id)
            workflow_nodes = registered["nodes"] if registered else []
            request_nodes = self.request_nodes(selected, root_id, workflow_nodes, messages)
            root = next((e for e in sessions if e["id"] == root_id), None)
            if request_nodes:
                state = {**state, "run_id": root_id, "started_at": root["created_at"] if root else None,
                    "updated_at": max((e["last_activity_at"] for e in sessions), default=None),
                    "graph_kind": "request_history", "title": "개발 에이전트 · 실제 작업 요청", "nodes": request_nodes}
            elif registered:
                state = {**state, **{k: registered[k] for k in ("title", "nodes", "started_at", "updated_at", "run_id")}}
            else:
                root = next((e for e in sessions if e["id"] == root_id), None)
                state = {**state, "run_id": root_id, "started_at": root["created_at"] if root else None,
                    "updated_at": max((e["last_activity_at"] for e in sessions), default=None),
                    "graph_kind": "agent_tree", "title": (Path(self.project).name + " · 에이전트 실행 기록") if self.project else "프로젝트와 세션을 선택하세요", "nodes": [
                    {"id": e["id"], "title": e["agent_id"].split("/")[-1], "nickname": e["nickname"],
                     **self.task_role(e, {}, root_id, workflow_nodes),
                     "agent_id": e["id"], "status": e["status"] if e["status"] in {"running", "completed"} else "pending",
                     "detail": "작업 시작 이벤트를 아직 관찰하지 못했습니다", "depends_on": [], "parent_id": e["parent_id"],
                     "started_at": e["created_at"], "finished_at": e.get("finished_at"), "evidence": [e["path"]]} for e in sessions], "events": []}
            agents = [{"id": e["id"], "agent_id": e["agent_id"], "nickname": e["nickname"],
                       "status": e["status"], "current_turn_id": e.get("current_turn_id"),
                       "latest_activity": e.get("latest_activity"), "latest_activity_at": e["last_activity_at"],
                       "active_tools": list(e.get("active_tools", {}).values()) if e["status"] == "running" else []}
                      for e in selected]
            live = {"running_agents": sum(e["status"] == "running" for e in agents),
                    "completed_agents": sum(e["status"] == "completed" for e in agents),
                    "unknown_agents": sum(e["status"] == "unknown" for e in agents),
                    "failed_agents": sum(e["status"] == "failed" for e in agents),
                    "active_tools": sum(len(e["active_tools"]) for e in agents),
                    "latest_activity_at": max((e["latest_activity_at"] for e in agents), default=None),
                    "agents": agents}
            # Runtime lifecycle events belong in the execution-events view too.
            # Keep coordinator registrations alongside them only for their root.
            progress_ids = {r["id"] for r in message_records if r.get("record_kind") == "assistant_progress"}
            runtime_events = [{**item, "message": item["detail"], "runtime": True}
                              for item in activity if item["type"] in {
                                  "task_started", "task_complete", "turn_aborted", "task_failed"}
                              or (item["type"] == "progress" and item["id"] in progress_ids)]
            manual_events = [event for event in ledger_events
                             if (event.get("type") == "agent.message" and event.get("session_id") in accepted
                                 and event.get("project_path", self.project) == self.project)
                             or event.get("seq") in registered_events]
            events = sorted([*manual_events, *runtime_events],
                            key=lambda e: (timestamp(e.get("at")) or 0, str(e.get("id", e.get("seq", "")))))
            created_titles = {w["events"][0]["seq"]: w["title"] for w in matching
                              if w.get("events") and w["events"][0].get("type") == "workflow.created"}
            execution_events = readable_workflow_rows(execution_event_rows(events, messages), workflow_nodes, created_titles)
            request_records = [record for node in request_nodes for record in node.get("request_records", [])]
            request_cards = group_message_records(request_records, self.turns)
            relations, endpoints = graph_relations(state["nodes"], selected, root_id,
                [*messages, *request_cards], getattr(self, "dispatches", {}).values())
            return {**state, "revision": state["revision"] + self.revision, "messages": messages,
                "graph_relations": relations, "graph_endpoints": endpoints, "graph_requests": request_cards,
                "events": events, "execution_events": execution_events,
                "message_records": message_records,
                "registered_workflow": registered, "live": live,
                "conversations": self.conversations(selected, root_id, message_records),
                "activity": activity, "sessions": sessions, "project_path": self.project,
                "root_session_id": root_id, "root_session_name": self.session_name(root_id)["name"],
                "app_context": self.app_reader.context if getattr(self, "app_reader", None) else {
                    "status": "unavailable", "selected_project": None, "detail": "앱 선택 정보 없음"},
                "available_runs": [{"id": e["id"], **self.session_name(e["id"]),
                                    "started_at": e["created_at"], "status": e["status"]} for e in roots],
                "graph_kind": state.get("graph_kind", "workflow_dag"),
                "collection": {"poll_ms": 1000, "encrypted_messages": sum(m["encrypted"] for m in messages),
                    "journal_backed_messages": sum(m.get("message_source") == "sender_journal" for m in messages),
                    "encrypted_records": sum(r["encrypted"] for r in message_records),
                    "message_records": len(message_records), "message_cards": len(messages),
                    "last_success_at": self.last_success_at,
                    "source": "Codex 로컬 실행 기록 + 송수신 원문 기록", "error": self.error}}
