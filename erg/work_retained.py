"""Bounded repair of work evidence from the retained corpus, never upstream logs.

Native collection remains authoritative while this path is verified. Repairs only
insert absent work rows, never change usage, ownership, billing or existing facts.
"""

import collections
import hashlib
import json
import math
import time
import zlib

from . import collect, store, work

KINDS = ("work_context", "work_evidence", "work_commit")
SOURCE_FIELDS = (
    "event_id content_hash expired_at present ts kind source capture_version bytes".split()
)


def sources(db, machine, tool, session, limit):
    return db.execute(
        "SELECT event_id,content_hash,expired_at,body IS NOT NULL,ts,kind,source,capture_version,bytes FROM history_events WHERE machine=? AND tool=? AND session=? ORDER BY seq LIMIT ?",
        (machine, tool, session, limit),
    ).fetchall()


def contexts(db, machine, tool, session, limit):
    cursor = db.execute(
        "SELECT * FROM work_contexts WHERE machine=? AND tool=? AND session=? AND capture_version>=3 AND provenance IN ('native_git','tool_workdir','tool_checkout') ORDER BY ts,uid LIMIT ?",
        (machine, tool, session, limit),
    )
    names = [c[0] for c in cursor.description]
    return [dict(zip(names, row)) for row in cursor]


def existing(db, machine, row):
    fields = work.EVENTS[row["t"]].split()
    return db.execute(
        "SELECT "
        + ",".join(fields)
        + " FROM "
        + work.TABLES[row["t"]]
        + " WHERE machine=? AND uid=?",
        (machine, row["uid"]),
    ).fetchone()


def compatible(old, row):
    fields = work.EVENTS[row["t"]].split()
    expected = tuple(row.get(field) for field in fields)
    if old == expected:
        return True
    if row["t"] == "work_commit":
        index = fields.index("provenance")
        if old[index] in ("git_object", "git_history") and row.get("provenance") in (
            "git_object",
            "git_history",
        ):
            return all(a == b for i, (a, b) in enumerate(zip(old, expected)) if i != index)
    return False


def tool_pairs(tool, body):
    calls = []
    results = []
    if tool == "codex":
        payload = body.get("payload") or {}
        if isinstance(payload, dict) and body.get("type") == "response_item":
            kind = payload.get("type")
            ident = payload.get("call_id")
            if isinstance(ident, str) and ident:
                if kind in ("function_call", "custom_tool_call"):
                    calls.append(ident)
                elif kind in ("function_call_output", "custom_tool_call_output"):
                    results.append(ident)
    else:
        content = (body.get("message") or {}).get("content")
        for item in content if isinstance(content, list) else []:
            if not isinstance(item, dict):
                continue
            ident = (
                item.get("id")
                if item.get("type") == "tool_use"
                else item.get("tool_use_id")
                if item.get("type") == "tool_result"
                else None
            )
            if isinstance(ident, str) and ident:
                (calls if item.get("type") == "tool_use" else results).append(ident)
    return calls, results


def replay(
    db,
    tool,
    session,
    machine=None,
    apply=False,
    max_events=5000,
    budget=10,
    max_bytes=32 * 1024 * 1024,
):
    if tool not in ("claude", "codex") or not session:
        raise ValueError("select one Claude or Codex session")
    if (
        isinstance(max_events, bool)
        or not isinstance(max_events, int)
        or not 1 <= max_events <= 100000
        or not isinstance(budget, (int, float))
        or isinstance(budget, bool)
        or not math.isfinite(budget)
        or budget <= 0
        or not isinstance(max_bytes, int)
        or isinstance(max_bytes, bool)
        or max_bytes <= 0
    ):
        raise ValueError("invalid replay bounds")
    if db.in_transaction:
        raise ValueError("retained work replay requires an idle connection")
    machine = machine or collect.machine_name()
    deadline = time.monotonic() + budget
    result = dict(
        machine=machine,
        tool=tool,
        session=session,
        source_events=0,
        candidates={},
        insertable=0,
        already_present=0,
        conflicts=0,
        gaps={},
        can_apply=False,
        applied=False,
        inserted=0,
        scope="One already-retained session; no upstream transcript reads, usage/account/billing changes or replacement of existing work evidence",
    )
    gaps = collections.Counter()
    rows = {}
    state = dict(session=session)
    blocked = False
    calls_seen = set()
    results_seen = set()
    orphan_results = set()
    db.execute("BEGIN")
    try:
        snapshot = sources(db, machine, tool, session, max_events + 1)
        known_contexts = contexts(db, machine, tool, session, max_events + 1)
        result["source_events"] = len(snapshot)
        result["source_events_complete"] = len(snapshot) <= max_events
        if not snapshot:
            gaps["session_not_retained"] += 1
            blocked = True
        elif len(snapshot) > max_events:
            gaps["event_limit_exceeded"] += 1
            blocked = True
        elif sum(row[-1] or 0 for row in snapshot) > max_bytes:
            gaps["body_byte_limit_exceeded"] += 1
            blocked = True
        if len(known_contexts) > max_events:
            gaps["context_limit_exceeded"] += 1
            blocked = True
        if not blocked:
            decoded = []
            for record in snapshot:
                if time.monotonic() > deadline:
                    gaps["time_budget_exceeded"] += 1
                    blocked = True
                    break
                event = dict(zip(SOURCE_FIELDS, record))
                if event["expired_at"] or not event["present"]:
                    gaps["body_unavailable"] += 1
                    blocked = True
                    continue
                if (event["capture_version"] or 0) < 2:
                    gaps["native_flags_not_retained"] += 1
                    blocked = True
                    continue
                try:
                    packed = db.execute(
                        "SELECT body FROM history_events WHERE event_id=?", (event["event_id"],)
                    ).fetchone()[0]
                    body = json.loads(zlib.decompress(packed))
                    if not isinstance(body, dict):
                        raise ValueError("not an object")
                    decoded.append((event, body))
                except (ValueError, zlib.error):
                    gaps["invalid_retained_body"] += 1
                    blocked = True
            if tool == "codex" and (not decoded or decoded[0][0]["kind"] != "session_meta"):
                gaps["session_header_not_retained"] += 1
                blocked = True
            elif tool == "codex":
                payload = decoded[0][1].get("payload") or {}
                if (
                    not isinstance(payload, dict)
                    or (payload.get("id") or payload.get("session_id")) != session
                ):
                    gaps["native_session_identity_conflict"] += 1
                    blocked = True
            if not blocked and tool == "claude" and decoded:
                first, body = decoded[0]
                cwd = body.get("cwd")
                hash_cwd = hashlib.sha256((cwd or "").encode()).hexdigest()[:16]
                prior = [
                    c for c in known_contexts if c["ts"] < first["ts"] and c["worktree"] == hash_cwd
                ]
                if prior:
                    stamp = max(c["ts"] for c in prior)
                    prior = [c for c in prior if c["ts"] == stamp]
                    signatures = {
                        (c["repo"], c["branch"], c["head_sha"], c["task_id"], c["scope_id"])
                        for c in prior
                    }
                    if len(signatures) != 1:
                        gaps["ambiguous_prior_work_context"] += 1
                        blocked = True
                    else:
                        context = prior[-1]
                        state.update(
                            {k: context[k] or "" for k in ("repo", "branch", "head_sha", "task_id")}
                        )
                        state.update(cwd=cwd, _native_cwd=cwd, _scope_id=context["scope_id"])
                        state["_scope_key"] = json.dumps(
                            [session, cwd, state["repo"], state["branch"], state["task_id"]]
                        )
                        ctx = {k: state[k] for k in ("repo", "branch", "head_sha", "task_id")}
                        ctx["scope_id"] = context["scope_id"]
                        state["_signature"] = json.dumps([session, cwd, ctx], sort_keys=True)
            if not blocked:
                for event, body in decoded:
                    if time.monotonic() > deadline:
                        gaps["time_budget_exceeded"] += 1
                        blocked = True
                        break
                    if not collect.parse_ts(body.get("timestamp")):
                        gaps["native_timestamp_not_retained"] += 1
                        blocked = True
                        break
                    if collect.parse_ts(body.get("timestamp")) != event["ts"]:
                        gaps["native_timestamp_conflict"] += 1
                        blocked = True
                        break

                    def observed_repo(cwd):
                        key = hashlib.sha256((cwd or "").encode()).hexdigest()[:16]
                        candidates = [
                            c
                            for c in known_contexts
                            if c["worktree"] == key and c["ts"] <= event["ts"] and c["repo"]
                        ]
                        if not candidates:
                            return {}
                        stamp = max(c["ts"] for c in candidates)
                        repos = {c["repo"] for c in candidates if c["ts"] == stamp}
                        return (
                            dict(repo=next(iter(repos)))
                            if len(repos) == 1
                            else dict(ambiguous=True)
                        )

                    try:
                        call_ids, result_ids = tool_pairs(tool, body)
                        calls_seen.update(call_ids)
                        orphan_results.update(
                            ident for ident in result_ids if ident not in calls_seen
                        )
                        results_seen.update(result_ids)
                        projected = work.record_events(
                            tool,
                            body,
                            state,
                            event["source"] or "",
                            0,
                            include_metadata=False,
                            include_prompts=False,
                            repo_context=observed_repo,
                        )
                    except (ValueError, TypeError, KeyError, AttributeError):
                        gaps["invalid_work_record"] += 1
                        blocked = True
                        break
                    for row in projected:
                        if row["t"] not in KINDS:
                            continue
                        if row["t"] != "work_commit" and (
                            row.get("tool") != tool or row.get("session") != session
                        ):
                            gaps["native_session_identity_conflict"] += 1
                            blocked = True
                            continue
                        if row["t"] == "work_context" and not row.get("repo"):
                            gaps["historical_repository_not_recorded"] += 1
                            continue
                        key = (row["t"], row["uid"])
                        if key in rows and rows[key] != row:
                            gaps["conflicting_derived_rows"] += 1
                            blocked = True
                        rows[key] = row
        if orphan_results:
            gaps["tool_call_not_retained"] += len(orphan_results)
        if calls_seen - results_seen:
            gaps["tool_result_not_retained"] += len(calls_seen - results_seen)
        counts = collections.Counter(row["t"] for row in rows.values())
        result["candidates"] = dict(counts)
        additions = []
        for row in rows.values():
            old = existing(db, machine, row)
            if old is None:
                additions.append(row)
            elif compatible(old, row):
                result["already_present"] += 1
            else:
                result["conflicts"] += 1
        result["insertable"] = len(additions)
    finally:
        db.rollback()
    result["gaps"] = dict(gaps)
    result["can_apply"] = not blocked and not result["conflicts"]
    result["coverage"] = (
        "conflicting"
        if result["conflicts"]
        else "blocked"
        if blocked
        else "partial"
        if gaps
        else "observed_retained_records"
    )
    if apply and result["can_apply"]:
        with db:
            db.execute("BEGIN IMMEDIATE")
            if (
                sources(db, machine, tool, session, max_events + 1) != snapshot
                or contexts(db, machine, tool, session, max_events + 1) != known_contexts
            ):
                raise RuntimeError("retained evidence changed during replay; retry")
            for row in rows.values():
                old = existing(db, machine, row)
                if old is not None and not compatible(old, row):
                    raise RuntimeError("work evidence changed during replay; retry")
            fresh = [row for row in rows.values() if existing(db, machine, row) is None]
            if fresh:
                store.ingest(db, map(json.dumps, fresh), machine=machine, transaction=False)
            result.update(applied=True, inserted=len(fresh))
    return result
