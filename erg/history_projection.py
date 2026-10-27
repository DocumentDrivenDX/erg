"""Durable bounded projection from retained events, independent of upstream logs."""

import hashlib
import json
import math
import time
import zlib

from . import accounting, collect, routes, store, work

VERSION = 2


def number(value):
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError("invalid_usage")
    return value


def text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(
        isinstance(x, dict) and x.get("type") == "text" for x in value
    ):
        return "\n".join(x.get("text", "") for x in value)
    return ""


def plan(db, e, d, state):
    tool, sid, ts = e["tool"], e["session"], e["ts"]
    kind = d.get("type") or e["kind"]
    p = d.get("payload") if tool == "codex" else d
    if not isinstance(p, dict):
        p = {}
    state["session"] = sid
    if kind in ("session_meta", "turn_context", "model_change", "session"):
        if p.get("cwd"):
            if kind == "turn_context" and state.get("cwd") and state["cwd"] != p["cwd"]:
                state.update(repo="", branch="", head_sha="")
            state["cwd"] = p["cwd"]
        if p.get("model") or d.get("modelId"):
            state.update(model=p.get("model") or d.get("modelId"), model_ts=ts)
        if p.get("turn_id") and p.get("model"):
            turns = state.setdefault("turns", {})
            turns[p["turn_id"]] = p["model"]
            while len(turns) > 128:
                turns.pop(next(iter(turns)))
        if tool == "codex" and kind == "session_meta":
            source = p.get("source")
            spawn = (
                source.get("subagent", {}).get("thread_spawn", {})
                if isinstance(source, dict)
                else {}
            )
            parent = p.get("parent_thread_id") or spawn.get("parent_thread_id") or ""
            thread_source = p.get("thread_source") or (
                "subagent" if isinstance(source, dict) else "user"
            )
            native_kind = (
                "review"
                if "guardian" in str(thread_source)
                else "subagent"
                if parent or thread_source != "user"
                else "main"
            )
            old = db.execute(
                "SELECT kind FROM sessions WHERE machine=? AND uid=?",
                (e["machine"], tool + ":" + sid),
            ).fetchone()
            if e["capture_version"] < 2 and not p.get("thread_source") and old:
                native_kind = old[0] or native_kind
            state.update(parent_session=parent, kind=native_kind, started=ts)
            git = p.get("git") or {}
            state.update(
                repo=work.repository(git.get("repository_url")),
                branch=git.get("branch") or "",
                head_sha=git.get("commit_hash") or "",
            )
    if tool == "codex" and kind == "session_meta":
        if p.get("cli_version"):
            state["agent_version"] = p["cli_version"]
        if p.get("forked_from_id"):
            state["forked_from"] = p["forked_from_id"]
        if p.get("originator"):
            state["surface"] = p["originator"]
        if p.get("model_provider"):
            state["provider"] = p["model_provider"]
    if tool == "pi" and kind == "session":
        if d.get("version") is not None:
            state["agent_version"] = str(d["version"])
        if d.get("parentSession"):
            state["parent_session"] = d["parentSession"]
    if tool == "claude" and d.get("version"):
        state["agent_version"] = d["version"]
    if tool == "claude":
        if d.get("cwd"):
            state["cwd"] = d["cwd"]
        if d.get("gitBranch"):
            state["branch"] = d["gitBranch"]
        if d.get("isSidechain"):
            state["kind"] = "subagent"
            state["parent_session"] = d.get("sessionId") or ""
    if tool == "pi" and kind == "model_change":
        state["provider"] = d.get("provider") or ""
    rows = []
    reason = ""
    call = None
    prompt = None
    base = dict(tool=tool, session=sid, project=e["project"], ts=ts)

    def model(explicit="", turn=None):
        return (
            explicit
            or state.get("turns", {}).get(turn)
            or (state.get("model") if state.get("model_ts", 0) <= ts else "")
            or "unknown"
        )

    def usage(uid, u, fields, mdl, **extra):
        # Missing usage stays unknown; an assistant record alone is not a call.
        nonlocal reason
        if not isinstance(u, dict):
            return None
        if fields["fresh_in"] not in u or fields["out"] not in u:
            reason = "usage_fields_incomplete"
            return None
        values = {name: number(u.get(key, 0)) for name, key in fields.items()}
        return dict(
            base,
            t="llm",
            uid=uid,
            model=mdl,
            kind=state.get("kind", "main"),
            client=tool,
            usage_kind="observed",
            **values,
            **extra,
        )

    if tool == "pi" and kind == "message":
        m = d.get("message") or {}
        ident = d.get("id")
        if m.get("role") == "assistant" and isinstance(m.get("usage"), dict):
            if ident:
                call = usage(
                    "pi:" + sid + ":" + str(ident),
                    m["usage"],
                    dict(
                        fresh_in="input",
                        out="output",
                        cache_read="cacheRead",
                        cache_write="cacheWrite",
                        reasoning="reasoning",
                    ),
                    model(m.get("model")),
                    provider=m.get("provider") or state.get("provider", ""),
                    response_id=m.get("responseId") or "",
                    request_id=m.get("requestId") or "",
                    reported_cost_usd=collect.reported_cost(m["usage"]),
                    reported_cost_source="agent_reported_estimate"
                    if isinstance(m["usage"].get("cost"), dict)
                    else None,
                )
                if call:
                    call["usage_kind"] = collect.pi_usage_kind(m)
            else:
                reason = "native_message_id_missing"
        elif (
            m.get("role") == "user"
            and m.get("timestamp") != 0
            and m.get("usageProvenance") != "synthetic_history"
        ):
            if ident:
                stats = m.get("inputStats")
                if isinstance(stats, dict):
                    prompt = dict(
                        base,
                        t="prompt",
                        uid="pi:" + sid + ":" + str(ident),
                        model=model(),
                        **{
                            k: stats.get(k)
                            for k in ("chars", "words", "est_tokens", "origin", "provenance")
                        },
                    )
                else:
                    prompt = (
                        "pi:" + sid + ":" + str(ident),
                        text(m.get("content")),
                        "unknown",
                        "native_user_message",
                        "",
                    )
            else:
                reason = "native_input_id_missing"
    elif tool == "claude" and kind == "assistant":
        m = d.get("message") or {}
        ident = m.get("id") or (
            d.get("requestId") or d.get("uuid") if e["capture_version"] >= 2 else None
        )
        if isinstance(m.get("usage"), dict) and not str(m.get("model", "")).startswith("<"):
            if ident:
                uid = "c:" + str(ident)
                cached = db.execute(
                    "SELECT value FROM history_projection_usage WHERE machine=? AND tool=? AND uid=?",
                    (e["machine"], tool, uid),
                ).fetchone()
                prior = json.loads(cached[0]) if cached else {"values": {}, "events": []}
                merged = collect.merge_claude_usage(prior["values"], m["usage"])
                pending_refs = prior.get("events", [])
                if "fresh_in" in merged and "out" in merged:
                    call = dict(
                        base,
                        t="llm",
                        uid=uid,
                        model=model(m.get("model")),
                        kind=state.get("kind", "main"),
                        client=tool,
                        usage_kind="observed",
                        response_id=m.get("id") or "",
                        request_id=d.get("requestId") or "",
                        **{
                            k: merged.get(k, 0)
                            for k in ("fresh_in", "out", "cache_read", "cache_write", "reasoning")
                        },
                        **{k: merged.get(k) for k in ("cache_write_5m", "cache_write_1h")},
                    )
                    for ref in pending_refs:
                        db.execute(
                            "UPDATE history_projection_state SET state='projected',reason='' WHERE event_id=? AND reason='usage_fields_incomplete'",
                            (ref,),
                        )
                    pending_refs = []
                else:
                    reason = "usage_fields_incomplete"
                    pending_refs = (pending_refs + [e["event_id"]])[-128:]
                db.execute(
                    "INSERT OR REPLACE INTO history_projection_usage VALUES(?,?,?,?)",
                    (
                        e["machine"],
                        tool,
                        uid,
                        json.dumps({"values": merged, "events": pending_refs}),
                    ),
                )
            else:
                reason = "native_message_id_not_retained"
    elif tool == "claude" and kind == "user" and not d.get("isMeta") and not d.get("isSidechain"):
        if e["capture_version"] < 2:
            reason = "input_flags_not_retained"
        elif d.get("uuid"):
            prompt = (
                "c:" + d["uuid"],
                text((d.get("message") or {}).get("content")),
                "unknown",
                "native_user_message",
                "",
            )
    elif tool == "codex" and kind == "token_usage_record":
        rid = p.get("response_id")
        if rid:
            u = p.get("usage") or {}
            cached = number(u.get("cached_input_tokens", 0))
            written = number(u.get("cache_write_input_tokens", 0))
            call = usage(
                "x:" + rid,
                u,
                dict(
                    fresh_in="input_tokens",
                    out="output_tokens",
                    reasoning="reasoning_output_tokens",
                ),
                model(p.get("model"), p.get("turn_id")),
                response_id=rid,
                cache_read=cached,
                cache_write=written,
            )
            if call:
                call["fresh_in"] = max(0, call["fresh_in"] - cached - written)
        else:
            reason = "response_id_missing"
    elif tool == "codex" and kind == "event_msg" and p.get("type") == "token_count":
        total = (p.get("info") or {}).get("total_token_usage")
        if isinstance(total, dict):
            values = [
                number(total[k]) if total.get(k) is not None else None
                for k in (
                    "input_tokens",
                    "output_tokens",
                    "cached_input_tokens",
                    "cache_write_input_tokens",
                    "total_tokens",
                )
            ]
            db.execute(
                "INSERT OR REPLACE INTO history_usage_snapshots VALUES(?,?,?,?,?,?,?,?,?,?)",
                (e["event_id"], e["machine"], tool, sid, ts, *values),
            )
    elif tool == "codex" and (
        (kind == "response_item" and p.get("role") == "user")
        or (kind == "event_msg" and p.get("type") == "user_message")
    ):
        ident = p.get("id") if kind == "response_item" else p.get("message_id")
        origin = "unknown"
        provenance = "codex_user_event"
        value = p.get("message", "")
        if kind == "response_item":
            meta = p.get("internal_chat_message_metadata_passthrough") or {}
            kinds = meta.get("content_item_kinds", [])
            if not any(isinstance(k, str) and k.startswith("user.") for k in kinds):
                value = ""
            elif (
                isinstance(meta.get("create_time"), (int, float))
                and meta["create_time"] < state.get("started", 0) - 5
            ):
                value = ""
            else:
                value = "\n".join(
                    c.get("text", "") for c in p.get("content", []) if isinstance(c, dict)
                )
                origin = "automation" if state.get("kind") == "subagent" else "human"
                provenance = "codex_user_text"
        if value:
            # Without a native ID or original ordinal, the native parser's line
            # fallback cannot be recreated safely from a copied/pruned corpus.
            if ident:
                prompt = ("corpus-input:" + e["event_id"], value, origin, provenance, ident)
            elif d.get("ordinal") is not None:
                prompt = (
                    "x:%s:%s:%s"
                    % (
                        sid,
                        d["ordinal"],
                        hashlib.sha256(work.user_text(value).encode()).hexdigest()[:12],
                    ),
                    value,
                    origin,
                    provenance,
                    "",
                )
            else:
                reason = "native_input_identity_not_retained"
    elif tool == "copilot":
        data = d.get("data") or {}
        ident = d.get("id")
        if kind == "assistant.usage":
            ident = data.get("apiCallId") or ident
            if ident:
                u = dict(data)
                if "inputTokens" in u:
                    u["inputTokens"] = max(
                        0,
                        number(u["inputTokens"])
                        - number(u.get("cacheReadTokens", 0))
                        - number(u.get("cacheWriteTokens", 0)),
                    )
                call = usage(
                    "copilot:" + str(ident),
                    u,
                    dict(
                        fresh_in="inputTokens",
                        out="outputTokens",
                        cache_read="cacheReadTokens",
                        cache_write="cacheWriteTokens",
                        reasoning="reasoningTokens",
                    ),
                    model(data.get("model")),
                    response_id=data.get("apiCallId") or "",
                    request_id=data.get("serviceRequestId") or "",
                )
        elif kind == "user.message" and ident:
            prompt = (
                "copilot:" + ident,
                text(data.get("content")),
                "unknown",
                "native_user_message",
                "",
            )
    elif tool == "opencode" and kind == "sqlite_record":
        m = d.get("data") or {}
        if (
            d.get("table") in ("message", "session_message")
            and isinstance(m, dict)
            and m.get("role") == "assistant"
        ):
            if isinstance(m.get("tokens"), dict):
                u = m["tokens"]
                cache = u.get("cache") or {}
                tm = m.get("time") or {}
                if tm.get("completed") or m.get("finish"):
                    call = usage(
                        "opencode:" + d["id"],
                        u,
                        dict(fresh_in="input", out="output", reasoning="reasoning"),
                        model(m.get("modelID")),
                        cache_read=number(cache.get("read", 0)),
                        cache_write=number(cache.get("write", 0)),
                        response_id=m.get("responseId") or m.get("response_id") or "",
                        provider=m.get("providerID") or "",
                    )
                    if call:
                        call["ts"] = (
                            collect.parse_ts(tm.get("completed") or tm.get("created")) or ts
                        )
        if (
            d.get("table") in ("message", "session_message")
            and isinstance(m, dict)
            and m.get("role") in ("user", "assistant")
        ):
            db.execute(
                "INSERT OR REPLACE INTO history_projection_messages VALUES(?,?,?,?,?,?)",
                (e["machine"], tool, sid, d["id"], m["role"], ts),
            )
            parent_id = d["id"]
            db.execute(
                "UPDATE history_projection_state SET state='projected',reason='' WHERE reason='parent_metadata_pending' AND event_id IN (SELECT event_id FROM history_projection_parts WHERE machine=? AND tool=? AND session=? AND parent_id=?)",
                (e["machine"], tool, sid, parent_id),
            )
        elif (
            d.get("table") == "part"
            and isinstance(m, dict)
            and m.get("type") == "text"
            and d.get("parent_message_id")
        ):
            parent_id = d["parent_message_id"]
            known_parent = db.execute(
                "SELECT role FROM history_projection_messages WHERE machine=? AND tool=? AND session=? AND native_id=?",
                (e["machine"], tool, sid, parent_id),
            ).fetchone()
            if not known_parent or known_parent[0] == "user":
                value = work.user_text(m.get("text", "")) if not m.get("synthetic") else ""
                db.execute(
                    "INSERT OR REPLACE INTO history_projection_parts VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        e["machine"],
                        tool,
                        sid,
                        parent_id,
                        d["id"],
                        e["event_id"],
                        len(value),
                        len(value.split()),
                        (len(value) + 3) // 4,
                    ),
                )
        else:
            parent_id = None
        if parent_id:
            parent = db.execute(
                "SELECT role,ts FROM history_projection_messages WHERE machine=? AND tool=? AND session=? AND native_id=?",
                (e["machine"], tool, sid, parent_id),
            ).fetchone()
            if parent and parent[0] == "user":
                counts = db.execute(
                    "SELECT sum(chars),sum(words),sum(est_tokens) FROM history_projection_parts WHERE machine=? AND tool=? AND session=? AND parent_id=?",
                    (e["machine"], tool, sid, parent_id),
                ).fetchone()
                if counts[0]:
                    prompt = dict(
                        dict(base, ts=parent[1]),
                        t="prompt",
                        uid="opencode:input:" + parent_id,
                        model=model(),
                        chars=counts[0],
                        words=counts[1],
                        est_tokens=counts[2],
                        origin="unknown",
                        provenance="native_user_message",
                        source_uid=parent_id,
                    )
            elif not parent:
                reason = "parent_metadata_pending"
    elif tool == "local" and kind == "inference_response":
        cur = db.execute(
            "SELECT * FROM local_inferences WHERE response_event_id=?", (e["event_id"],)
        )
        v = cur.fetchone()
        if v:
            r = dict(zip([c[0] for c in cur.description], v))
            if r["usage_known"] and r["state"] == "complete":
                call = dict(
                    dict(base, ts=r["event_time"]),
                    t="llm",
                    uid="local:" + r["invocation_id"],
                    model=r["model"],
                    provider="local",
                    client="local_proxy",
                    kind="main",
                    usage_kind="observed",
                    response_id=r["response_id"],
                    request_id="",
                    fresh_in=r["input_tokens"],
                    out=r["output_tokens"],
                    cache_read=r["cache_read"],
                    cache_write=r["cache_write"],
                )
    if call:
        old = db.execute(
            "SELECT model,kind,client FROM llm WHERE machine=? AND uid=?",
            (e["machine"], call["uid"]),
        ).fetchone()
        if old and call["model"] == "unknown":
            call["model"] = old[0] or "unknown"
        if old and not state.get("kind"):
            call["kind"] = old[1] or "main"
        rows.append(call)
    if isinstance(prompt, tuple):
        uid, value, origin, provenance, source_uid = prompt
        value = work.user_text(value)
        if value:
            prompt = dict(
                base,
                t="prompt",
                uid=uid,
                model=model(),
                chars=len(value),
                words=len(value.split()),
                est_tokens=(len(value) + 3) // 4,
                origin=origin,
                provenance=provenance,
                source_uid=source_uid,
            )
        else:
            prompt = None
    if isinstance(prompt, dict):
        rows.append(prompt)
    if tool == "claude" and kind == "assistant":
        rows.extend(collect.claude_edit_events(d, sid, e["project"], ts))
    if tool == "codex" and kind == "response_item":
        subtype = p.get("type", "")
        if e.get("projection_hint") is None:
            from . import history

            db.execute(
                "UPDATE history_events SET projection_hint=? WHERE event_id=? AND content_hash=?",
                (history.projection_hint(tool, kind, d), e["event_id"], e["content_hash"]),
            )
        if subtype in ("custom_tool_call", "function_call"):
            from . import edit_projection

            rows.extend(
                edit_projection.preserve_models(
                    db, collect.codex_edit_events(p, sid, e["project"], ts, model()), e["machine"]
                )
            )
    if tool in ("claude", "codex", "pi"):
        rows.extend(routes.native(tool, d, p, ts, state))
        rows.extend(accounting.observe(tool, d, p, state, ts, e["source"]))
        if tool == "claude":
            cost = accounting.reported_cost(d, ts)
            if cost:
                rows.append(cost)
    if tool != "local" or kind == "inference_response":
        cursor = db.execute(
            "SELECT * FROM sessions WHERE machine=? AND uid=?", (e["machine"], tool + ":" + sid)
        )
        record = cursor.fetchone()
        session_row = dict(zip([c[0] for c in cursor.description], record)) if record else {}
        session_row.pop("machine", None)
        session_row.update(
            base,
            t="session",
            uid=tool + ":" + sid,
            last_ts=ts,
            kind=state.get("kind") or session_row.get("kind") or "main",
            surface=session_row.get("surface")
            or state.get("surface")
            or d.get("entrypoint")
            or "retained_corpus",
            parent_session=state.get("parent_session") or session_row.get("parent_session") or "",
            agent_version=state.get("agent_version") or session_row.get("agent_version") or "",
            forked_from=state.get("forked_from") or session_row.get("forked_from") or "",
            provider=state.get("provider")
            or session_row.get("provider")
            or (call or {}).get("provider")
            or "",
            provenance=session_row.get("provenance") or "retained_corpus",
            usage_status="observed"
            if call and call.get("usage_kind") == "observed"
            else session_row.get("usage_status") or "unknown",
        )
        rows.append(session_row)
    return rows, reason


def requeue(db, tool="", session=""):
    where = ["body IS NOT NULL"]
    params = []
    for name, value in (("tool", tool), ("session", session)):
        if value:
            where.append(name + "=?")
            params.append(value)
    with db:
        db.execute(
            "INSERT INTO history_pending(event_id,content_hash,enqueued_at) SELECT event_id,content_hash,? FROM history_events WHERE "
            + " AND ".join(where)
            + " ON CONFLICT(event_id) DO UPDATE SET content_hash=excluded.content_hash,retry_at=0,attempts=0",
            [int(time.time())] + params,
        )
    return dict(
        queued=db.execute("SELECT count(*) FROM history_pending").fetchone()[0],
        scope="Retained corpus only; upstream logs are not read",
    )


def candidate_cursor(db, limit, now):
    """Seek an ordered queue prefix before reading/decompressing event bodies."""
    cols = "event_id machine tool session project ts kind role source capture_version content_hash body bytes expired_at projection_hint".split()
    select = [
        (
            "CASE WHEN e.tool='local' OR (e.tool='codex' AND e.kind='response_item' AND e.role<>'user' AND e.projection_hint<>'codex_patch') THEN NULL ELSE e.body END AS body"
            if c == "body"
            else "e." + c
        )
        for c in cols
    ]
    return cols, db.execute(
        "SELECT "
        + ",".join(select)
        + " FROM history_pending q INDEXED BY history_pending_sequence CROSS JOIN history_events e ON e.event_id=q.event_id WHERE coalesce(q.retry_at,0)<=? ORDER BY q.seq LIMIT ?",
        (now, limit),
    )


def run(db, limit=500, budget=1, cpu_budget=False):
    clock = time.thread_time if cpu_budget else time.monotonic
    deadline = clock() + budget
    now = int(time.time())
    processed = errors = processed_bytes = 0
    cols, cursor = candidate_cursor(db, limit, now)
    candidates = []
    size = 0
    for values in cursor:
        item = dict(zip(cols, values))
        size += item["bytes"] or 0
        candidates.append(item)
        if size > 8 * 1024 * 1024:
            break
    cursor.close()
    if not candidates:
        return dict(
            processed=0,
            errors=0,
            pending=db.execute("SELECT count(*) FROM history_pending").fetchone()[0],
        )
    decoded = []
    contexts = {}
    for e in candidates:
        try:
            body = e.pop("body")
            d = (
                None
                if e["expired_at"]
                else json.loads(zlib.decompress(body))
                if body
                else {"type": e["kind"]}
            )
            decoded.append((e, d, None))
        except (ValueError, zlib.error):
            decoded.append((e, None, "invalid_corpus_body"))
    with db:
        if not db.in_transaction:
            db.execute("BEGIN IMMEDIATE")
        for e, d, error in decoded:
            if clock() > deadline and processed:
                break
            current = db.execute(
                "SELECT content_hash FROM history_pending WHERE event_id=?", (e["event_id"],)
            ).fetchone()
            if not current or current[0] != e["content_hash"]:
                continue
            key = (e["machine"], e["tool"], e["session"])
            if key not in contexts:
                record = db.execute(
                    "SELECT value FROM history_projection_context WHERE machine=? AND tool=? AND session=?",
                    key,
                ).fetchone()
                contexts[key] = json.loads(record[0]) if record else {}
            state = contexts[key]
            db.execute("SAVEPOINT history_projection_record")
            try:
                if error:
                    raise ValueError(error)
                rows, reason = (
                    plan(db, e, d or {}, state)
                    if d is not None
                    else ([], "content_expired_before_projection")
                )
                store.ingest(db, map(json.dumps, rows), machine=e["machine"], transaction=False)
                db.execute(
                    "INSERT OR REPLACE INTO history_projection_context VALUES(?,?,?,?)",
                    key + (json.dumps(state),),
                )
                db.execute(
                    "INSERT OR REPLACE INTO history_projection_state VALUES(?,?,?,?,?,?,?)",
                    (
                        e["event_id"],
                        e["content_hash"],
                        VERSION,
                        "gap" if reason else "projected",
                        reason,
                        len(rows),
                        now,
                    ),
                )
                db.execute(
                    "DELETE FROM history_pending WHERE event_id=? AND content_hash=?",
                    (e["event_id"], e["content_hash"]),
                )
                db.execute("DELETE FROM history_edit_pending WHERE event_id=?", (e["event_id"],))
                db.execute("RELEASE history_projection_record")
                processed += 1
                processed_bytes += e["bytes"] or 0
            except (ValueError, TypeError, KeyError, AttributeError):
                errors += 1
                db.execute("ROLLBACK TO history_projection_record")
                db.execute("RELEASE history_projection_record")
                contexts.pop(key, None)  # A failed plan may have mutated its state.
                db.execute(
                    "INSERT OR REPLACE INTO history_projection_state VALUES(?,?,?,?,?,?,?)",
                    (
                        e["event_id"],
                        e["content_hash"],
                        VERSION,
                        "error",
                        error or "invalid_native_record",
                        0,
                        now,
                    ),
                )
                db.execute(
                    "UPDATE history_pending SET attempts=coalesce(attempts,0)+1,retry_at=? WHERE event_id=?",
                    (now + 60, e["event_id"]),
                )
    return dict(
        processed=processed,
        processed_bytes=processed_bytes,
        errors=errors,
        pending=db.execute("SELECT count(*) FROM history_pending").fetchone()[0],
    )


def latest_counter(db, tool, session):
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='history_usage_snapshots'"
    ).fetchone():
        return None
    row = db.execute(
        "SELECT ts,input_tokens,output_tokens,cache_read,cache_write,total_tokens FROM history_usage_snapshots WHERE tool=? AND session=? ORDER BY ts DESC,rowid DESC LIMIT 1",
        (tool, session),
    ).fetchone()
    if not row:
        return None
    return dict(
        zip(
            ("ts", "input_tokens", "output_tokens", "cache_read", "cache_write", "total_tokens"),
            row,
        ),
        scope="native_cumulative_snapshot",
        input_tokens_include_cache=True,
        request_count=None,
        coverage="Native counter window may start before corpus enrollment; not added to request totals",
    )


def status(db):
    return dict(
        pending=db.execute("SELECT count(*) FROM history_pending").fetchone()[0],
        edit_pending=db.execute("SELECT count(*) FROM history_edit_pending").fetchone()[0],
        edit_retrying=db.execute(
            "SELECT count(*) FROM history_edit_pending WHERE attempts>0"
        ).fetchone()[0],
        edit_unavailable=db.execute("SELECT count(*) FROM history_edit_errors").fetchone()[0],
        states=dict(
            db.execute("SELECT state,count(*) FROM history_projection_state GROUP BY state")
        ),
        gaps=dict(
            db.execute(
                "SELECT reason,count(*) FROM history_projection_state WHERE state IN ('gap','error') GROUP BY reason"
            )
        ),
    )
