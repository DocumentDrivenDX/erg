"""Derived canonical winners; raw llm and the portable query remain authoritative.

Maintenance uses bounded indexed lookups under short writer transactions.
Actual inserts and updates invalidate cached winners sharing their destination
primary key, covering implicit REPLACE deletions without dirtying no-op upserts. Old groups
not yet cached are already pending or still ahead of the seed cursor. Readers
may use winners only in a pinned snapshot after seeding and invalidation drain.
Optional failures disable cached reads; raw ingestion and its observation journal
remain in their original atomic transaction.
"""

import hashlib
import json
import math
import re
import sqlite3
import time

from . import report_sqlite

VERSION = "2"
# An altered canonical rule needs a fresh audit; unknown rules use raw reporting.
AUDITED_DEFINITION = "3107d8d5436ba685856c10344e8760c9ab9177dc4ac9e705dfb69ac9ae06f8d5"
TABLE = "call_winner_rows"
TRIGGERS = ("erg_call_insert", "erg_call_delete", "erg_call_update")


def definition():
    from . import unified

    return hashlib.sha256(
        json.dumps([unified.CALLS_SELECT, report_sqlite.eligible()], separators=(",", ":")).encode()
    ).hexdigest()


def quoted(name):
    return '"' + name.replace('"', '""') + '"'


def columns(db):
    return [(r[1], r[2]) for r in db.execute("PRAGMA table_info(llm)")]


def signature(db, cols):
    schema = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='llm'"
    ).fetchone()
    return hashlib.sha256(
        json.dumps([cols, schema[0] if schema else None], separators=(",", ":")).encode()
    ).hexdigest()


def identity(prefix):
    p = prefix + "." if prefix else ""
    return "CASE WHEN {p}tool='local' AND COALESCE({p}response_id,'')<>'' THEN {p}response_id||':'||COALESCE({p}model,'') ELSE {p}uid END".format(
        p=p
    )


def key(prefix):
    tool = prefix + ".tool"
    group = identity(prefix)
    # quote() truncates strings at NUL; typed hex framing preserves opaque keys.
    return "typeof({t})||':'||hex({t})||'|'||typeof({g})||':'||hex({g})".format(t=tool, g=group)


def enqueue(prefix):
    return (
        "INSERT INTO call_winner_dirty SELECT "
        + key(prefix)
        + ","
        + prefix
        + ".tool,"
        + identity(prefix)
        + " WHERE 1 ON CONFLICT(key) DO NOTHING;"
    )


def affinity(value):
    value = value.upper()
    return (
        "INTEGER"
        if "INT" in value
        else "TEXT"
        if any(x in value for x in ("CHAR", "CLOB", "TEXT"))
        else "BLOB"
        if not value or "BLOB" in value
        else "REAL"
        if any(x in value for x in ("REAL", "FLOA", "DOUB"))
        else "NUMERIC"
    )


def winner_columns(db):
    return [(r[1], r[2]) for r in db.execute("PRAGMA table_xinfo(" + TABLE + ")")]


def expected_columns(cols):
    return [("key", "TEXT")] + [(c, affinity(t)) for c, t in cols]


def trigger_sql(cols):
    changed = " OR ".join(
        ["OLD.rowid IS NOT NEW.rowid"]
        + ["OLD." + quoted(c) + " IS NOT NEW." + quoted(c) for c, t in cols]
    )
    replaced = (
        "INSERT INTO call_winner_dirty SELECT w.key,w.tool,"
        + identity("w")
        + " FROM "
        + TABLE
        + " w "
        "WHERE w.machine=NEW.machine AND w.uid=NEW.uid AND w.key<>"
        + key("NEW")
        + " ON CONFLICT(key) DO NOTHING; "
    )
    return {
        TRIGGERS[0]: "CREATE TRIGGER "
        + TRIGGERS[0]
        + " AFTER INSERT ON llm BEGIN "
        + replaced
        + enqueue("NEW")
        + " END",
        TRIGGERS[1]: "CREATE TRIGGER "
        + TRIGGERS[1]
        + " AFTER DELETE ON llm BEGIN "
        + enqueue("OLD")
        + " END",
        TRIGGERS[2]: "CREATE TRIGGER "
        + TRIGGERS[2]
        + " AFTER UPDATE ON llm WHEN "
        + changed
        + " BEGIN "
        + replaced
        + enqueue("OLD")
        + enqueue("NEW")
        + " END",
    }


def indexes():
    return {
        "call_winner_order": "CREATE INDEX call_winner_order ON "
        + TABLE
        + "(tool,("
        + identity("")
        + "))",
        "call_winner_source": "CREATE INDEX call_winner_source ON " + TABLE + "(machine,uid)",
        "call_winner_request": "CREATE INDEX call_winner_request ON "
        + TABLE
        + "(request_id,model,tool)",
        "call_winner_response": "CREATE INDEX call_winner_response ON "
        + TABLE
        + "(machine,response_id,model,tool)",
        "call_winner_invalid": "CREATE INDEX call_winner_invalid ON "
        + TABLE
        + "(key) WHERE machine IS NULL OR uid IS NULL OR tool IS NULL",
        "llm_eligible_winner": "CREATE INDEX llm_eligible_winner ON llm(tool,uid,out DESC,ts DESC,machine) WHERE "
        + report_sqlite.eligible(),
        "call_winner_local_lookup": "CREATE INDEX call_winner_local_lookup ON llm(("
        + identity("")
        + "),out DESC,ts DESC,machine) WHERE tool='local' AND "
        + report_sqlite.eligible(),
    }


def initialize(db):
    """Caller commits; schema/trigger upgrades remain one atomic writer window."""
    own = not db.in_transaction
    if own:
        db.execute("BEGIN IMMEDIATE")
    try:
        return _initialize(db)
    except BaseException:
        if own:
            db.rollback()
        raise


def _initialize(db):
    """Repair a broken cache by reseeding, never by trusting it."""
    if definition() != AUDITED_DEFINITION:
        return
    cols = columns(db)
    if not cols or {"key", "capture_rank"} & {c for c, t in cols}:
        raise ValueError("unsupported call source schema")
    sqls = trigger_sql(cols)
    sig = signature(db, cols)
    db.execute("CREATE TABLE IF NOT EXISTS call_winner_state(key TEXT PRIMARY KEY,value TEXT)")
    state = dict(db.execute("SELECT key,value FROM call_winner_state"))
    existing = dict(
        db.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND name IN (?,?,?)", TRIGGERS
        )
    )
    have = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    try:
        cursor = int(state["seed_cursor"]) if state["seed_cursor"] is not None else None
        bound = int(state["seed_bound"])
        invalid_state = (
            state.get("seed_complete") not in ("0", "1") or cursor is not None and cursor > bound
        )
    except (KeyError, ValueError, TypeError):
        invalid_state = True
    reset = (
        state.get("version") != VERSION
        or state.get("definition_signature") != definition()
        or state.get("source_signature") != sig
        or existing != sqls
        or not {"call_winner_dirty", TABLE} <= have
        or winner_columns(db) != expected_columns(cols)
        or invalid_state
    )
    if reset:
        for name in TRIGGERS + ("erg_call_replace_before",):
            db.execute("DROP TRIGGER IF EXISTS " + name)
        db.execute("DROP TABLE IF EXISTS " + TABLE)
        db.execute("DROP TABLE IF EXISTS call_winner_dirty")
        db.execute("DELETE FROM call_winner_state")
        db.execute(
            "CREATE TABLE "
            + TABLE
            + "(key TEXT PRIMARY KEY,"
            + ",".join(quoted(c) + " " + affinity(t) for c, t in cols)
            + ")"
        )
        db.execute("CREATE TABLE call_winner_dirty(key TEXT PRIMARY KEY,tool TEXT,identity TEXT)")
        bound = db.execute("SELECT max(rowid) FROM llm").fetchone()[0]
        db.executemany(
            "INSERT INTO call_winner_state VALUES(?,?)",
            [
                (k, None if v is None else str(v))
                for k, v in dict(
                    version=VERSION,
                    definition_signature=definition(),
                    source_signature=sig,
                    seed_bound=bound if bound is not None else 0,
                    seed_cursor=None,
                    seed_complete=int(bound is None),
                ).items()
            ],
        )
        for sql in sqls.values():
            db.execute(sql)
    for name, sql in indexes().items():
        old = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (name,)
        ).fetchone()
        if old and old[0] != sql:
            db.execute("DROP INDEX " + name)
            old = None
        if not old:
            db.execute(sql)


def disarm(db):
    """Caller owns the transaction; absent capture triggers force raw fallback."""
    for name in TRIGGERS + ("erg_call_replace_before",):
        db.execute("DROP TRIGGER IF EXISTS " + name)


def safe_initialize(db):
    """A failed optional migration must preserve unrelated raw capture writes."""
    db.execute("SAVEPOINT erg_call_winner_setup")
    try:
        initialize(db)
        result = check(db, pending_limit=0)
        if not result.get("available") or result.get("reason") in (
            "unsupported_source_constraints",
            "unsupported_source_layout",
        ):
            disarm(db)
    except Exception as error:
        db.execute("ROLLBACK TO erg_call_winner_setup")
        disarm(db)
        result = dict(
            available=False, ready=False, reason="initialization_failed", error=type(error).__name__
        )
    db.execute("RELEASE erg_call_winner_setup")
    return result


def maintain(db, **kwargs):
    """Keep optional cache failures outside durable archive/usage capture."""
    try:
        result = run(db, **kwargs)
    except Exception as error:
        db.rollback()
        with db:
            if not db.in_transaction:
                db.execute("BEGIN IMMEDIATE")
            disarm(db)
        return dict(
            available=False, ready=False, reason="maintenance_failed", error=type(error).__name__
        )
    broken = (
        "cache_tables_unavailable",
        "source_triggers_changed",
        "source_schema_changed",
        "source_indexes_changed",
        "invalid_cache_state",
        "cache_definition_changed",
        "cache_schema_changed",
        "unsupported_source_constraints",
        "unsupported_source_layout",
        "canonical_definition_changed",
    )
    if result.get("reason") in broken:
        existing = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name IN (?,?,?) LIMIT 1", TRIGGERS
        ).fetchone()
        if existing:
            with db:
                if not db.in_transaction:
                    db.execute("BEGIN IMMEDIATE")
                disarm(db)
    return result


def write_calls(db, sql, rows):
    """Retry cache-specific trigger failures inside the raw ingest transaction.

    Disarming invalidation triggers also disables cached reads in the same commit.
    Primary-store failures propagate; unrelated writes/journal state stay atomic.
    """
    if not db.in_transaction:
        db.execute("BEGIN")
    db.execute("SAVEPOINT erg_call_winner_write")
    try:
        try:
            db.executemany(sql, rows)
        except sqlite3.Error as error:
            if not db.in_transaction:
                raise  # Preserve errors that rolled back the outer transaction.
            db.execute("ROLLBACK TO erg_call_winner_write")
            message = str(error)
            cache_error = bool(re.search(r"\bcall_winner_(?:dirty|rows|state)\b", message))
            if (
                not cache_error
                and isinstance(error, sqlite3.OperationalError)
                and message.startswith(("no such column:", "no such table:"))
            ):
                try:
                    cache_error = check(db, pending_limit=0).get("reason") in (
                        "cache_schema_changed",
                        "cache_tables_unavailable",
                    )
                except sqlite3.Error:
                    cache_error = False
            if not cache_error:
                raise
            disarm(db)
            db.executemany(sql, rows)
    except BaseException:
        if db.in_transaction:
            db.execute("ROLLBACK TO erg_call_winner_write")
            db.execute("RELEASE erg_call_winner_write")
        raise
    db.execute("RELEASE erg_call_winner_write")


def check(db, pending_limit=10000):
    """This result is meaningful for reads only while this snapshot stays pinned."""
    if definition() != AUDITED_DEFINITION:
        return dict(available=False, ready=False, reason="canonical_definition_changed")
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='call_winner_state'"
    ).fetchone():
        return dict(available=False, ready=False, reason="not_initialized")
    state = dict(db.execute("SELECT key,value FROM call_winner_state"))
    cols = columns(db)
    if state.get("definition_signature") != definition():
        return dict(available=True, ready=False, reason="cache_definition_changed")
    primary = sorted((r[5], r[1]) for r in db.execute("PRAGMA table_info(llm)") if r[5])
    extra_unique = any(r[2] and r[3] != "pk" for r in db.execute("PRAGMA index_list(llm)"))
    if primary != [(1, "machine"), (2, "uid")] or extra_unique:
        return dict(available=True, ready=False, reason="unsupported_source_constraints")
    schema = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='llm'"
    ).fetchone()[0]
    if (
        re.search(r"\bCOLLATE\b", schema, re.I)
        or any(r[6] for r in db.execute("PRAGMA table_xinfo(llm)"))
        or db.execute("PRAGMA encoding").fetchone()[0] != "UTF-8"
    ):
        return dict(available=True, ready=False, reason="unsupported_source_layout")
    if state.get("version") != VERSION or state.get("source_signature") != signature(db, cols):
        return dict(available=True, ready=False, reason="source_schema_changed")
    actual = dict(
        db.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND name IN (?,?,?)", TRIGGERS
        )
    )
    if actual != trigger_sql(cols):
        return dict(available=True, ready=False, reason="source_triggers_changed")
    have = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {TABLE, "call_winner_dirty"} <= have:
        return dict(available=True, ready=False, reason="cache_tables_unavailable")
    if winner_columns(db) != expected_columns(cols):
        return dict(available=True, ready=False, reason="cache_schema_changed")
    for name, sql in indexes().items():
        row = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (name,)
        ).fetchone()
        if not row or row[0] != sql:
            return dict(available=True, ready=False, reason="source_indexes_changed")
    try:
        cursor = int(state["seed_cursor"]) if state["seed_cursor"] is not None else None
        bound = int(state["seed_bound"])
        if state.get("seed_complete") not in ("0", "1") or cursor is not None and cursor > bound:
            raise ValueError("invalid cursor")
    except (KeyError, ValueError, TypeError):
        return dict(available=True, ready=False, reason="invalid_cache_state")
    if pending_limit is None:
        pending = db.execute("SELECT count(*) FROM call_winner_dirty").fetchone()[0]
        exact = True
    else:
        pending = db.execute(
            "SELECT count(*) FROM (SELECT 1 FROM call_winner_dirty LIMIT ?)", (pending_limit + 1,)
        ).fetchone()[0]
        exact = pending <= pending_limit
    invalid = bool(
        db.execute(
            "SELECT 1 FROM "
            + TABLE
            + " WHERE machine IS NULL OR uid IS NULL OR tool IS NULL LIMIT 1"
        ).fetchone()
    )
    complete = state.get("seed_complete") == "1"
    return dict(
        available=True,
        ready=bool(complete and not pending and not invalid),
        reason="seeding"
        if not complete
        else "pending"
        if pending
        else "unsupported_winner_identity"
        if invalid
        else "",
        pending=pending,
        pending_count_exact=exact,
        seed_cursor=cursor,
        seed_bound=bound,
        seed_complete=complete,
    )


def status(db, pending_limit=10000):
    if pending_limit is not None and (
        isinstance(pending_limit, bool) or not isinstance(pending_limit, int) or pending_limit < 0
    ):
        raise ValueError("invalid pending count bound")
    own = not db.in_transaction
    if own:
        db.execute("BEGIN")
    try:
        return check(db, pending_limit)
    finally:
        if own:
            db.rollback()


def project_batch(db, cols, limit):
    """Recompute a small current prefix atomically; the caller owns the writer.

    Identity stays affinity-free so a correlated comparison can seek the local
    expression index without adding a TEXT conversion to the indexed expression.
    """
    db.execute(
        "CREATE TEMP TABLE IF NOT EXISTS erg_call_winner_batch(key TEXT PRIMARY KEY,tool TEXT,identity)"
    )
    db.execute("DELETE FROM erg_call_winner_batch")
    db.execute(
        "INSERT INTO erg_call_winner_batch SELECT key,tool,identity FROM call_winner_dirty ORDER BY rowid LIMIT ?",
        (limit,),
    )
    count = db.execute("SELECT count(*) FROM erg_call_winner_batch").fetchone()[0]
    if not count:
        return 0
    fields = ",".join("l." + quoted(c) for c, t in cols)
    native = (
        "SELECT b.key," + fields + " FROM erg_call_winner_batch b JOIN llm l ON l.rowid=("
        "SELECT n.rowid FROM llm n WHERE n.tool IS b.tool AND n.uid IS b.identity AND "
        + report_sqlite.eligible("n")
        + " ORDER BY n.out DESC,n.ts DESC,n.machine LIMIT 1) WHERE b.tool IS NOT 'local'"
    )
    local = (
        "SELECT b.key," + fields + " FROM erg_call_winner_batch b JOIN llm l ON l.rowid=("
        "SELECT n.rowid FROM llm n WHERE n.tool='local' AND "
        + identity("n")
        + " IS b.identity AND "
        + report_sqlite.eligible("n")
        + " ORDER BY n.out DESC,n.ts DESC,n.machine LIMIT 1) WHERE b.tool='local'"
    )
    db.execute("DELETE FROM " + TABLE + " WHERE key IN (SELECT key FROM erg_call_winner_batch)")
    db.execute("INSERT INTO " + TABLE + " " + native + " UNION ALL " + local)
    db.execute("DELETE FROM call_winner_dirty WHERE key IN (SELECT key FROM erg_call_winner_batch)")
    return count


def run(db, seed_limit=256, limit=256, budget=0.002):
    for value in (seed_limit, limit):
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 10000:
            raise ValueError("invalid call projection limits")
    if (
        isinstance(budget, bool)
        or not isinstance(budget, (int, float))
        or not math.isfinite(budget)
        or budget < 0
    ):
        raise ValueError("invalid call projection budget")
    if db.in_transaction:
        raise ValueError("call projection requires an idle connection")
    started = time.thread_time()
    seeded = processed = 0
    before = status(db, pending_limit=seed_limit)
    if not before["available"] or before["reason"] in (
        "source_schema_changed",
        "source_triggers_changed",
        "cache_tables_unavailable",
        "source_indexes_changed",
        "unsupported_source_constraints",
        "unsupported_source_layout",
        "invalid_cache_state",
        "cache_definition_changed",
        "cache_schema_changed",
    ):
        return dict(before, seeded=0, processed=0)
    # Existing invalidations take priority. Seeding must not create a corpus-sized
    # dirty queue when a soft-budgeted worker drains fewer keys per poll.
    if not before["seed_complete"] and before["pending"] < seed_limit:
        with db:
            db.execute("BEGIN IMMEDIATE")
            # Recheck after acquiring the writer: source inserts can enqueue work
            # between the read-only status snapshot and this transaction.
            waiting = db.execute(
                "SELECT count(*) FROM (SELECT 1 FROM call_winner_dirty LIMIT ?)", (seed_limit,)
            ).fetchone()[0]
            allowance = seed_limit - waiting
            if allowance:
                state = dict(db.execute("SELECT key,value FROM call_winner_state"))
                cursor = int(state["seed_cursor"]) if state["seed_cursor"] is not None else None
                bound = int(state["seed_bound"])
                where = "rowid<=?" if cursor is None else "rowid>? AND rowid<=?"
                args = (bound,) if cursor is None else (cursor, bound)
                last, count = db.execute(
                    "SELECT max(rowid),count(*) FROM (SELECT rowid FROM llm WHERE "
                    + where
                    + " ORDER BY rowid LIMIT ?)",
                    args + (allowance,),
                ).fetchone()
                if count:
                    prefix = (last,) if cursor is None else (cursor, last)
                    db.execute(
                        "INSERT OR IGNORE INTO call_winner_dirty SELECT "
                        + key("l")
                        + ",l.tool,"
                        + identity("l")
                        + " FROM llm l WHERE "
                        + where,
                        prefix,
                    )
                    db.execute(
                        "UPDATE call_winner_state SET value=? WHERE key='seed_cursor'", (str(last),)
                    )
                    seeded = count
                else:
                    db.execute("UPDATE call_winner_state SET value='1' WHERE key='seed_complete'")
    # The lock contains indexed winner selection and local table bookkeeping only.
    if budget > 0 and db.execute("SELECT 1 FROM call_winner_dirty LIMIT 1").fetchone():
        cols = columns(db)
        with db:
            db.execute("BEGIN IMMEDIATE")
            while processed < limit and (not processed or time.thread_time() - started < budget):
                # One bounded batch must progress even when snapshot/schema checks
                # consumed the soft budget; one key per poll can take days to warm.
                width = min(32, limit - processed)
                count = project_batch(db, cols, width)
                if not count:
                    break
                processed += count
    return dict(
        status(db),
        seeded=seeded,
        processed=processed,
        cpu_seconds=round(time.thread_time() - started, 6),
    )


def cached_select(db, fields):
    """None requires portable fallback; never trust readiness across snapshots."""
    if (
        not isinstance(fields, (list, tuple))
        or not fields
        or any(not isinstance(f, str) or not re.fullmatch(r"[a-z_][a-z0-9_]*", f) for f in fields)
        or len(set(fields)) != len(fields)
    ):
        raise ValueError("explicit unique column names required")
    if not db.in_transaction or not check(db, pending_limit=0).get("ready"):
        return None
    selected = ",".join(
        "1 AS capture_rank" if f == "capture_rank" else "w." + quoted(f) for f in fields
    )
    return (
        "SELECT "
        + selected
        + """ FROM call_winner_rows w WHERE
 (tool<>'local' OR NOT (w.response_id IS NOT NULL AND w.response_id<>'' AND
 (SELECT count(*) FROM call_winner_rows n WHERE n.machine=w.machine AND n.response_id=w.response_id AND n.model=w.model AND n.tool NOT IN ('local','gateway'))=1 AND
 (SELECT count(*) FROM call_winner_rows l WHERE l.machine=w.machine AND l.response_id=w.response_id AND l.model=w.model AND l.tool='local')=1)) AND
 (tool<>'gateway' OR NOT (w.request_id IS NOT NULL AND w.request_id<>'' AND
 (SELECT count(*) FROM call_winner_rows g WHERE g.request_id=w.request_id AND g.model=w.model AND g.tool='gateway')=1 AND
 (SELECT count(*) FROM call_winner_rows n WHERE n.request_id=w.request_id AND n.model=w.model AND n.tool<>'gateway')=1))"""
        + " ORDER BY w.tool,"
        + identity("w")
    )
