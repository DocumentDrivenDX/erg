"""Incremental quota summaries; source history remains authoritative and intact."""

import json
import sqlite3
import time

EVENTS = (
    "snapshot",
    "window_exhausted",
    "limit_reached",
    "reset_observed",
    "reset_credit_used",
    "reset_credit_granted",
    "reset_credit_purchased",
)


def initialize(db):
    db.executescript("""
    CREATE INDEX IF NOT EXISTS quota_source_uid ON quota_events(uid,ts DESC);
    CREATE TABLE IF NOT EXISTS quota_summary_pending(uid TEXT PRIMARY KEY);
    CREATE TABLE IF NOT EXISTS quota_summary_rows(uid TEXT PRIMARY KEY,machine TEXT,source_rowid INTEGER,ordinal INTEGER,ts INTEGER,group_key TEXT,event TEXT,window TEXT,occurred_at INTEGER);
    CREATE INDEX IF NOT EXISTS quota_summary_group_time ON quota_summary_rows(group_key,ts DESC,ordinal);
    CREATE INDEX IF NOT EXISTS quota_summary_group_order ON quota_summary_rows(group_key,ordinal);
    CREATE INDEX IF NOT EXISTS quota_summary_resets ON quota_summary_rows(window,occurred_at DESC,ordinal);
    CREATE TABLE IF NOT EXISTS quota_summary_groups(group_key TEXT PRIMARY KEY,uid TEXT,first_ordinal INTEGER);
    CREATE TABLE IF NOT EXISTS quota_summary_counts(event TEXT PRIMARY KEY,n INTEGER NOT NULL);
    """)
    for event, aliases in (("INSERT", ("NEW",)), ("UPDATE", ("OLD", "NEW")), ("DELETE", ("OLD",))):
        body = "".join(
            "INSERT OR IGNORE INTO quota_summary_pending VALUES(" + a + ".uid); " for a in aliases
        )
        db.execute(
            "CREATE TRIGGER IF NOT EXISTS quota_summary_"
            + event.lower()
            + " AFTER "
            + event
            + " ON quota_events BEGIN "
            + body
            + "END"
        )
    if not db.execute("SELECT 1 FROM history_config WHERE key='quota_summary_seed_v1'").fetchone():
        db.execute(
            "INSERT OR IGNORE INTO quota_summary_pending SELECT DISTINCT uid FROM quota_events"
        )
        db.execute("INSERT INTO history_config VALUES('quota_summary_seed_v1','1')")


def available(db):
    return isinstance(db, sqlite3.Connection) and bool(
        db.execute("SELECT 1 FROM sqlite_master WHERE name='quota_summary_pending'").fetchone()
    )


def schema_current(db):
    stored = db.execute(
        "SELECT value FROM history_config WHERE key='quota_summary_schema_v1'"
    ).fetchone()
    return bool(stored and stored[0] == str(db.execute("PRAGMA schema_version").fetchone()[0]))


def ready(db):
    return (
        available(db)
        and bool(
            db.execute("SELECT 1 FROM history_config WHERE key='quota_summary_seed_v1'").fetchone()
        )
        and schema_current(db)
        and not db.execute("SELECT 1 FROM quota_summary_pending LIMIT 1").fetchone()
    )


def status(db):
    present = available(db)
    return dict(
        available=present,
        ready=ready(db) if present else False,
        pending=db.execute("SELECT count(*) FROM quota_summary_pending").fetchone()[0]
        if present
        else None,
    )


def run(db, limit=4096, budget=0.1):
    """One bounded transaction; all source mutations enqueue in that transaction."""
    if not available(db):
        return dict(processed=0, pending=None)
    if db.in_transaction:
        raise ValueError("quota projection requires its own transaction")
    if not schema_current(db):
        # VACUUM/schema migration may change physical row order without source
        # DML triggers. Requalify pointers and deterministic timestamp ties.
        with db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO quota_summary_pending SELECT uid FROM quota_events")
            db.execute(
                "INSERT OR IGNORE INTO quota_summary_pending SELECT uid FROM quota_summary_rows"
            )
            revision = str(db.execute("PRAGMA schema_version").fetchone()[0])
            db.execute(
                "INSERT INTO history_config VALUES('quota_summary_schema_v1',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (revision,),
            )
    if not db.execute("SELECT 1 FROM quota_summary_pending LIMIT 1").fetchone():
        return dict(processed=0, pending=0)
    processed = 0
    changed = False
    groups = set()
    deadline = time.monotonic() + max(0.001, budget)
    with db:
        db.execute("BEGIN IMMEDIATE")
        for (uid,) in db.execute(
            "SELECT uid FROM quota_summary_pending ORDER BY uid LIMIT ?",
            (max(1, min(int(limit), 16384)),),
        ).fetchall():
            old = db.execute("SELECT * FROM quota_summary_rows WHERE uid=?", (uid,)).fetchone()
            cursor = db.execute(
                "SELECT rowid,* FROM quota_events WHERE uid=? ORDER BY ts DESC,rowid LIMIT 1",
                (uid,),
            )
            keys = [c[0] for c in cursor.description]
            values = cursor.fetchone()
            new = None
            if values:
                row = dict(zip(keys, values))
                ordinal = db.execute(
                    "SELECT min(rowid) FROM quota_events WHERE uid=?", (uid,)
                ).fetchone()[0]
                group = json.dumps(
                    [
                        row["account_key"] or row["tool"] + ":" + row["session"],
                        row["limit_id"],
                        row["window"],
                    ],
                    separators=(",", ":"),
                )
                new = (
                    uid,
                    row["machine"],
                    row["rowid"],
                    ordinal,
                    row["ts"],
                    group,
                    row["event"],
                    row["window"],
                    row.get("occurred_at"),
                )
            if old != new:
                changed = True
                for record, delta in ((old, -1), (new, 1)):
                    if record:
                        groups.add(record[5])
                        if record[6] in EVENTS and (record[7] != "reset_history" or record[8]):
                            db.execute(
                                "INSERT INTO quota_summary_counts VALUES(?,?) ON CONFLICT(event) DO UPDATE SET n=n+excluded.n",
                                (record[6], delta),
                            )
                if new:
                    db.execute(
                        "INSERT OR REPLACE INTO quota_summary_rows VALUES(?,?,?,?,?,?,?,?,?)", new
                    )
                else:
                    db.execute("DELETE FROM quota_summary_rows WHERE uid=?", (uid,))
            db.execute("DELETE FROM quota_summary_pending WHERE uid=?", (uid,))
            processed += 1
            if time.monotonic() >= deadline:
                break
        for group in groups:
            winner = db.execute(
                "SELECT uid FROM quota_summary_rows WHERE group_key=? ORDER BY ts DESC,ordinal LIMIT 1",
                (group,),
            ).fetchone()
            if winner:
                ordinal = db.execute(
                    "SELECT min(ordinal) FROM quota_summary_rows WHERE group_key=?", (group,)
                ).fetchone()[0]
                db.execute(
                    "INSERT OR REPLACE INTO quota_summary_groups VALUES(?,?,?)",
                    (group, winner[0], ordinal),
                )
            else:
                db.execute("DELETE FROM quota_summary_groups WHERE group_key=?", (group,))
        if (
            changed
            and db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='project_rollup_state'"
            ).fetchone()
        ):
            # A REPLACE can change a timestamp tie's winner without changing its
            # payload; make that source-order repair visible to the publisher.
            db.execute(
                "INSERT INTO project_rollup_state VALUES('evidence_serial','1') ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INT)+1"
            )
    return dict(
        processed=processed,
        pending=db.execute("SELECT count(*) FROM quota_summary_pending").fetchone()[0],
    )


def summary(db, root):
    """None means use raw history, never a stale or partial summary."""
    if not ready(db):
        return None
    cursor = db.execute(
        "SELECT q.*,r.ordinal,g.first_ordinal FROM quota_summary_groups g JOIN quota_summary_rows r ON r.uid=g.uid JOIN quota_events q ON q.machine=r.machine AND q.uid=r.uid WHERE r.window IS NOT 'reset_history'"
    )
    keys = [c[0] for c in cursor.description]
    latest = {}
    for values in cursor:
        row = dict(zip(keys, values))
        ordinal = row.pop("ordinal")
        first = row.pop("first_ordinal")
        key = (
            root(row["account_key"]) if row["account_key"] else row["tool"] + ":" + row["session"],
            row["limit_id"],
            row["window"],
        )
        previous = latest.get(key)
        if previous is None:
            latest[key] = (row, ordinal, first)
        else:
            old, old_order, old_first = previous
            latest[key] = (
                (row, ordinal, min(first, old_first))
                if (row["ts"], -ordinal) > (old["ts"], -old_order)
                else (old, old_order, min(first, old_first))
            )
    cursor = db.execute(
        "SELECT q.* FROM quota_summary_rows r JOIN quota_events q ON q.machine=r.machine AND q.uid=r.uid WHERE r.window='reset_history' AND r.occurred_at IS NOT NULL AND r.occurred_at<>0 ORDER BY r.occurred_at DESC,r.ordinal LIMIT 200"
    )
    keys = [c[0] for c in cursor.description]
    history = [dict(zip(keys, values)) for values in cursor]
    counts = dict(db.execute("SELECT event,n FROM quota_summary_counts"))
    return dict(
        quota_latest=[
            r
            for r, unused, first in sorted(latest.values(), key=lambda v: (-v[0]["ts"], v[2]))[:200]
        ],
        reset_history=history,
        quota_counts={event: counts.get(event, 0) for event in EVENTS},
    )
