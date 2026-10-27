"""Bounded edit-only replay for retained bodies captured before projector v2.

Never replay account, route or usage state to recover edit metrics. New bodies
use the main projection queue; this migration queue cannot block live usage.
"""

import json
import time
import zlib

from . import collect, store


def preserve_models(db, result, machine):
    for row in result:
        if row["model"] == "unknown":
            old = db.execute(
                "SELECT model FROM edits WHERE machine=? AND uid=?", (machine, row["uid"])
            ).fetchone()
            if old and old[0]:
                row["model"] = old[0]
    return result


def rows(db, event, body):
    if not isinstance(body, dict):
        raise ValueError("invalid_edit_body")
    if event["tool"] == "claude":
        return collect.claude_edit_events(body, event["session"], event["project"], event["ts"])
    payload = body.get("payload") or {}
    if not isinstance(payload, dict) or payload.get("type") not in (
        "custom_tool_call",
        "function_call",
    ):
        return []
    text = payload.get("input") or payload.get("arguments") or ""
    if not isinstance(text, str) or "Begin Patch" not in text:
        return []
    model = "unknown"
    context = db.execute(
        "SELECT body FROM history_events WHERE tool='codex' AND session=? AND machine=? AND kind='turn_context' AND seq<? AND ts<=? AND body IS NOT NULL ORDER BY seq DESC LIMIT 1",
        (event["session"], event["machine"], event["seq"], event["ts"]),
    ).fetchone()
    if context:
        record = json.loads(zlib.decompress(context[0]))
        value = (record.get("payload") or {}).get("model")
        if isinstance(value, str) and value:
            model = value
    result = collect.codex_edit_events(
        payload, event["session"], event["project"], event["ts"], model
    )
    return preserve_models(db, result, event["machine"])


def run(db, limit=32, budget=0.005):
    start = time.thread_time()
    now = int(time.time())
    processed = errors = attempted = 0
    columns = "event_id machine tool session project ts seq content_hash body expired_at projection_hint bytes".split()
    cursor = db.execute(
        "SELECT "
        + ",".join("e." + c for c in columns)
        + ",q.content_hash FROM history_edit_pending q CROSS JOIN history_events e ON e.event_id=q.event_id WHERE q.retry_at<=? AND NOT EXISTS(SELECT 1 FROM history_pending p WHERE p.event_id=e.event_id) ORDER BY q.seq LIMIT ?",
        (now, limit),
    )
    candidates = []
    size = 0
    for record in cursor:
        event = dict(zip(columns, record[:-1]))
        event["queued_hash"] = record[-1]
        candidates.append(event)
        size += event["bytes"] or 0
        if size >= 4 * 1024 * 1024:
            break
    cursor.close()
    for event in candidates:
        if attempted and time.thread_time() - start >= budget:
            break
        attempted += 1
        try:
            missing = bool(event["expired_at"] or not event["body"])
            body = json.loads(zlib.decompress(event["body"])) if not missing else None
            edits = rows(db, event, body) if not missing else []
            with db:
                if not db.in_transaction:
                    db.execute("BEGIN IMMEDIATE")
                current = db.execute(
                    "SELECT content_hash,expired_at FROM history_events WHERE event_id=?",
                    (event["event_id"],),
                ).fetchone()
                queued = db.execute(
                    "SELECT content_hash FROM history_edit_pending WHERE event_id=?",
                    (event["event_id"],),
                ).fetchone()
                if (
                    not current
                    or current != (event["content_hash"], event["expired_at"])
                    or not queued
                    or queued[0] != event["queued_hash"]
                ):
                    continue
                if db.execute(
                    "SELECT 1 FROM history_pending WHERE event_id=?", (event["event_id"],)
                ).fetchone():
                    continue
                if missing:
                    db.execute(
                        "INSERT OR REPLACE INTO history_edit_errors VALUES(?,?)",
                        (event["event_id"], "body_unavailable_for_edit_rebuild"),
                    )
                elif edits:
                    store.ingest(
                        db, map(json.dumps, edits), machine=event["machine"], transaction=False
                    )
                if body is not None and event["projection_hint"] is None:
                    from . import history

                    db.execute(
                        "UPDATE history_events SET projection_hint=? WHERE event_id=?",
                        (
                            history.projection_hint(
                                event["tool"],
                                "response_item" if event["tool"] == "codex" else "assistant",
                                body,
                            ),
                            event["event_id"],
                        ),
                    )
                db.execute(
                    "DELETE FROM history_edit_pending WHERE event_id=? AND content_hash=?",
                    (event["event_id"], event["queued_hash"]),
                )
                if not missing:
                    db.execute(
                        "DELETE FROM history_edit_errors WHERE event_id=?", (event["event_id"],)
                    )
                processed += 1
        except (ValueError, TypeError, AttributeError, zlib.error):
            errors += 1
            with db:
                db.execute(
                    "UPDATE history_edit_pending SET attempts=attempts+1,retry_at=? WHERE event_id=?",
                    (now + 60, event["event_id"]),
                )
    return dict(
        processed=processed,
        errors=errors,
        pending=db.execute("SELECT count(*) FROM history_edit_pending").fetchone()[0],
        cpu_seconds=round(time.thread_time() - start, 6),
    )
