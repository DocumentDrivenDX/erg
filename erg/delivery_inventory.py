"""Read-only cost planning inventory. Never exports bodies or reads upstream logs."""

import collections
import sqlite3
import time
import zlib
from pathlib import Path

from . import delivery_protocol as wire


def inventory(path, batch_rows=5000, sample_limit=128, sample_bytes=8 * 1024 * 1024):
    if (
        not 1 <= batch_rows <= 10000
        or not 0 <= sample_limit <= 1024
        or not 0 <= sample_bytes <= 32 * 1024 * 1024
    ):
        raise ValueError("inventory bound exceeded")
    started = time.monotonic()
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    db.execute("PRAGMA query_only=ON")
    try:
        ceiling = db.execute("SELECT COALESCE(MAX(seq),0) FROM history_events").fetchone()[0]
        counts = collections.Counter()
        tools = {}
        sessions = set()
        body_sessions = set()
        candidates = {}
        position = 0
        while position < ceiling:
            rows = db.execute(
                """SELECT seq,machine,tool,session,bytes,length(body),expired_at
              FROM history_events WHERE seq>? AND seq<=? ORDER BY seq LIMIT ?""",
                (position, ceiling, batch_rows),
            ).fetchall()
            if not rows:
                break
            for seq, machine, tool, session, size, compressed, expired in rows:
                counts["events"] += 1
                counts["retained_body_events"] += compressed is not None
                counts["expired_events"] += expired is not None
                if compressed is not None:
                    counts["raw_retained_body_bytes"] += size or 0
                    counts["sqlite_compressed_body_bytes"] += compressed
                    body_sessions.add((machine, tool, session))
                    # Bounded stratified reservoir by tool and uncompressed size class.
                    group = (tool, (size or 0).bit_length())
                    candidates.setdefault(group, (seq, size or 0))
                sessions.add((machine, tool, session))
                tools[tool] = tools.get(tool, 0) + 1
            position = rows[-1][0]
        counts["history_sessions"] = len(sessions)
        counts["history_sessions_with_bodies"] = len(body_sessions)
        counts["discovered_session_rows"] = db.execute("SELECT COUNT(*) FROM sessions").fetchone()[
            0
        ]
        counts["discovered_sessions_without_history_rows"] = sum(
            (m, t, s) not in sessions
            for m, t, s in db.execute("SELECT DISTINCT machine,tool,session FROM sessions")
        )
        measured_raw = measured_payload = measured_wire = sampled = skipped = 0
        envelope = dict(
            enrollment_id="estimate-enrollment",
            installation_id="estimate-installation",
            destination_generation="estimate-generation",
        )
        for seq, size in list(candidates.values())[:sample_limit]:
            if size > sample_bytes - measured_raw or size > wire.MAX_RECORD:
                skipped += 1
                continue
            row = db.execute(
                "SELECT event_id,tool,session,body FROM history_events WHERE seq=?", (seq,)
            ).fetchone()
            if row is None or row[3] is None:
                continue
            decoder = zlib.decompressobj()
            raw = decoder.decompress(row[3], min(sample_bytes - measured_raw, wire.MAX_RECORD) + 1)
            if not decoder.eof or decoder.unconsumed_tail or len(raw) > sample_bytes - measured_raw:
                skipped += 1
                continue
            payload = dict(
                record_id=row[0],
                record_revision=1,
                record_kind="session_event",
                tool=row[1],
                session=row[2],
                source_seq=seq,
                body=wire.decode(raw),
            )
            try:
                items = wire.record_items(payload, 1)
            except wire.ProtocolError:
                skipped += 1
                continue
            # Conservative one-item batches include envelope overhead per chunk.
            measured_wire += sum(len(wire.batch(envelope, [item])) for item in items)
            measured_payload += len(wire.encode(payload))
            measured_raw += len(raw)
            sampled += 1
        ratio = measured_wire / measured_raw if measured_raw else None
        return dict(
            mode="estimate-only; no upload or historical import authorized",
            consistency="bounded sequence scan; concurrent revisions/expiration may change metadata",
            ceiling_seq=ceiling,
            counts=dict(counts),
            events_by_tool=tools,
            sample=dict(
                strategy="first retained event per tool/log2-size stratum; not a random population estimate",
                events=sampled,
                skipped=skipped,
                decoded_body_bytes=measured_raw,
                payload_bytes=measured_payload,
                encoded_contract_bytes=measured_wire,
                wire_per_raw_byte=ratio,
            ),
            estimated_initial_contract_bytes=(
                int(counts["raw_retained_body_bytes"] * ratio) if ratio else None
            ),
            zerobus_metered_bytes=None,
            daily_new_capture_bytes=None,
            costs=dict(
                currency="USD",
                fixed_monthly=None,
                variable_transfer=None,
                storage=None,
                zerobus=None,
                materialization=None,
                reporting=None,
            ),
            assumptions=[
                "sample is illustrative, not a guaranteed compression/amplification bound",
                "wire excludes HTTPS/TLS, retries and future revisions",
                "Zerobus record bytes and prices require service measurement",
                "event timestamps are not capture-arrival timestamps",
                "retained local bodies exclude already pruned upstream history",
                "storage grows without a selected retention policy; spool/canonical copies add storage",
            ],
            elapsed_seconds=time.monotonic() - started,
        )
    finally:
        db.close()
