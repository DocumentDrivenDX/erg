"""Approved content-free reporting evidence in the existing delivery journal.

Only post-boundary activity from forward sessions is eligible. Current context,
PR and account headers may annotate that work; old session bodies are never read.
"""

import json
import time
import zlib

from . import delivery_journal as journal
from . import delivery_protocol as wire
from . import unified

SCHEMA = """
CREATE TABLE IF NOT EXISTS delivery_report_progress(id INTEGER PRIMARY KEY CHECK(id=1),source_seq INTEGER,scope_seq INTEGER);
INSERT OR IGNORE INTO delivery_report_progress(id,source_seq,scope_seq) VALUES(1,0,0);
CREATE TABLE IF NOT EXISTS delivery_report_scope(machine TEXT,tool TEXT,session TEXT,seeded INTEGER DEFAULT 0,PRIMARY KEY(machine,tool,session));
CREATE TABLE IF NOT EXISTS delivery_report_seed(event_id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS delivery_report_sent(event_id TEXT PRIMARY KEY,source_seq INTEGER);
CREATE TABLE IF NOT EXISTS delivery_report_errors(event_id TEXT PRIMARY KEY,source_seq INTEGER,reason TEXT);
CREATE TABLE IF NOT EXISTS delivery_report_global_scope(id INTEGER PRIMARY KEY CHECK(id=1),digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS delivery_context_scope(machine TEXT,tool TEXT,session TEXT,PRIMARY KEY(machine,tool,session));
CREATE TABLE IF NOT EXISTS delivery_context_dirty(machine TEXT,source_uid TEXT,PRIMARY KEY(machine,source_uid));
CREATE TRIGGER IF NOT EXISTS delivery_context_insert AFTER INSERT ON project_contexts
 WHEN EXISTS(SELECT 1 FROM delivery_report_scope s WHERE s.machine=NEW.machine AND s.tool=NEW.tool AND s.session=NEW.session)
 BEGIN INSERT OR IGNORE INTO delivery_context_dirty VALUES(NEW.machine,NEW.source_uid); END;
CREATE TRIGGER IF NOT EXISTS delivery_context_update AFTER UPDATE ON project_contexts
 WHEN (OLD.ts IS NOT NEW.ts OR OLD.cwd IS NOT NEW.cwd OR OLD.repo IS NOT NEW.repo)
 AND EXISTS(SELECT 1 FROM delivery_report_scope s WHERE s.machine=NEW.machine AND s.tool=NEW.tool AND s.session=NEW.session)
 BEGIN INSERT OR IGNORE INTO delivery_context_dirty VALUES(NEW.machine,NEW.source_uid); END;

"""
SESSION = frozenset(
    (
        "project_context",
        "llm",
        "prompt",
        "edit",
        "session",
        "work_context",
        "work_evidence",
        "work_link",
        "identity",
        "session_route",
        "quota",
        "session_cost",
        "account_binding",
    )
)
ACTIVITY = frozenset(("llm", "prompt", "edit"))
CONTEXT = frozenset(
    (
        "project_context",
        "work_context",
        "work_link",
        "work_evidence",
        "pull_request",
        "pr_commit",
        "work_commit",
        "commit",
    )
)
ACCOUNT = frozenset(
    ("identity", "session_route", "quota", "session_cost", "account_binding", "subscription_charge")
)


def session_kinds():
    return ",".join("'" + kind + "'" for kind in sorted(SESSION))


def initialize(db):
    db.executescript(SCHEMA)
    if "seeded" not in {r[1] for r in db.execute("PRAGMA table_info(delivery_report_scope)")}:
        db.execute("ALTER TABLE delivery_report_scope ADD COLUMN seeded INTEGER DEFAULT 0")
    if "turn" not in {r[1] for r in db.execute("PRAGMA table_info(delivery_report_progress)")}:
        db.execute(
            "ALTER TABLE delivery_report_progress ADD COLUMN turn INTEGER NOT NULL DEFAULT 0"
        )
    if "source_seq" not in {r[1] for r in db.execute("PRAGMA table_info(delivery_report_seed)")}:
        db.execute("ALTER TABLE delivery_report_seed ADD COLUMN source_seq INTEGER")
    with db:
        db.execute(
            "UPDATE delivery_report_seed SET source_seq=(SELECT seq FROM observations o WHERE o.event_id=delivery_report_seed.event_id) WHERE source_seq IS NULL"
        )
    db.execute(
        "CREATE INDEX IF NOT EXISTS delivery_report_seed_order ON delivery_report_seed(source_seq,event_id)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS delivery_snapshot_scope ON delivery_snapshots(machine,tool,session,ts)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS delivery_identity_scope ON identity_events(machine,tool,session,account_key) WHERE account_key<>''"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS delivery_route_scope ON session_route_events(machine,tool,session,account_key) WHERE account_key<>''"
    )
    # Exclude host samples and other non-session observations from this index.
    # TEXT affinity must match the enrolled identity columns, or SQLite uses
    # only the machine prefix and scans every session on that machine.
    sql = (
        "CREATE INDEX delivery_observation_scope ON observations(machine,CAST(json_extract(payload,'$.tool') AS TEXT),CAST(json_extract(payload,'$.session') AS TEXT),kind,ts) WHERE kind IN ("
        + session_kinds()
        + ")"
    )
    previous = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name='delivery_observation_scope'"
    ).fetchone()
    if previous and previous[0] != sql:
        db.execute("DROP INDEX delivery_observation_scope")
        previous = None
    if not previous:
        db.execute(sql)


def retry_errors(db, limit=64):
    """Explicit bounded recovery after a schema/runtime update, before publication.

    Recovered rows still pass the ordinary forward/scope selection. Malformed
    observations remain quarantined and cannot hold up native uploads.
    """
    recovered = 0
    with db:
        for (event_id,) in db.execute(
            "SELECT event_id FROM delivery_report_errors LIMIT ?", (limit,)
        ).fetchall():
            row = db.execute(
                "SELECT machine,kind,payload,content_hash,seq FROM observations WHERE event_id=?",
                (event_id,),
            ).fetchone()
            if not row:
                continue
            try:
                normal = unified.normalize(row[0], dict(json.loads(row[2]), t=row[1]))
            except (ValueError, TypeError):
                continue
            if normal[0] != event_id or normal[5] != row[3]:
                continue
            ident = wire.digest(("erg-report:" + event_id).encode())
            prior = db.execute(
                "SELECT body FROM delivery_journal WHERE event_id=? AND revision=?", (ident, row[4])
            ).fetchone()
            if prior:
                delivered = wire.decode(zlib.decompress(prior[0]))
                if (
                    delivered.get("event_id") == event_id
                    and delivered.get("content_hash") == row[3]
                ):
                    db.execute("DELETE FROM delivery_report_errors WHERE event_id=?", (event_id,))
                    recovered += 1
                    continue
            db.execute(
                "INSERT OR IGNORE INTO delivery_report_seed(event_id,source_seq) VALUES(?,?)",
                (event_id, row[4]),
            )
            recovered += 1
    return recovered


def enqueue(db, config, observation):
    event_id, machine, kind, ts, payload, sha, seq = observation
    value = json.loads(payload)
    # Normalize through the allowlist again; no arbitrary fields/credentials pass.
    normalized = unified.normalize(machine, dict(value, t=kind))
    if normalized[0] != event_id or normalized[5] != sha:
        raise ValueError("noncanonical reporting observation")
    body = dict(
        schema_version=1,
        event_id=event_id,
        machine=machine,
        kind=kind,
        ts=ts,
        payload=value,
        content_hash=sha,
        observation_sequence=seq,
    )
    raw = wire.encode(body)
    ident = wire.digest(("erg-report:" + event_id).encode())
    now = int(time.time())
    snapshot = {k: None for k in journal.FIELDS if k != "body"}
    snapshot.update(
        event_id=ident,
        machine=machine,
        tool=value.get("tool") or "erg",
        session=value.get("session") or "",
        project=value.get("project") or "",
        ts=now,
        kind="account_evidence"
        if kind in ACCOUNT
        else "work_evidence"
        if kind in CONTEXT
        else "usage_evidence",
        role=kind,
        origin="derived_reporting",
        source="erg:observations",
        content_hash=wire.digest(raw),
        bytes=len(raw),
        capture_version=3,
    )
    db.execute("DELETE FROM delivery_report_errors WHERE event_id=?", (event_id,))
    row = db.execute(
        "INSERT OR IGNORE INTO delivery_journal(enrollment_id,installation_id,destination_generation,event_id,revision,source_seq,snapshot,body,captured_at) VALUES(?,?,?,?,?,0,'v1',?,?)",
        config[:3] + (ident, seq, zlib.compress(raw, 1), now),
    )
    db.execute(
        "INSERT INTO delivery_report_sent VALUES(?,?) ON CONFLICT(event_id) DO UPDATE SET source_seq=MAX(source_seq,excluded.source_seq)",
        (event_id, seq),
    )
    if not row.rowcount:
        return 0
    cols = list(snapshot)
    db.execute(
        "INSERT INTO delivery_snapshots(journal_id,"
        + ",".join(cols)
        + ") VALUES("
        + ",".join("?" for _ in range(len(cols) + 1))
        + ")",
        [row.lastrowid] + [snapshot[k] for k in cols],
    )
    return 1


def publish_contexts(db, config, limit=64):
    """Scoped context anchors and changes, without native history/body reads."""
    from . import unified

    new = db.execute(
        "SELECT machine,tool,session FROM delivery_report_scope s WHERE NOT EXISTS(SELECT 1 FROM delivery_context_scope c WHERE c.machine=s.machine AND c.tool=s.tool AND c.session=s.session) LIMIT ?",
        (limit,),
    ).fetchall()
    queued = 0
    with db:
        for machine, tool, session in new:
            first = db.execute(
                "SELECT min(ts) FROM delivery_snapshots WHERE machine=? AND tool=? AND session=? AND COALESCE(origin,'')<>'derived_reporting'",
                (machine, tool, session),
            ).fetchone()[0]
            # Keep all tied anchors: conflicting context stays ambiguous in Scope.
            if first is not None:
                db.execute(
                    "INSERT OR IGNORE INTO delivery_context_dirty SELECT machine,source_uid FROM project_contexts WHERE machine=? AND tool=? AND session=? AND ts=(SELECT max(ts) FROM project_contexts WHERE machine=? AND tool=? AND session=? AND ts<=?)",
                    (machine, tool, session, machine, tool, session, first),
                )
            db.execute(
                "INSERT OR IGNORE INTO delivery_context_scope VALUES(?,?,?)",
                (machine, tool, session),
            )
        rows = db.execute(
            "SELECT p.machine,p.source_uid,p.tool,p.session,p.ts,p.cwd,p.repo FROM delivery_context_dirty d JOIN project_contexts p ON p.machine=d.machine AND p.source_uid=d.source_uid LIMIT ?",
            (limit,),
        ).fetchall()
        for machine, uid, tool, session, ts, cwd, repo in rows:
            event = dict(
                t="project_context", uid=uid, tool=tool, session=session, ts=ts, cwd=cwd, repo=repo
            )
            normalized = unified.normalize(machine, event)
            unified.record(db, machine, [event])
            row = db.execute(
                "SELECT event_id,machine,kind,ts,payload,content_hash,seq FROM observations WHERE event_id=?",
                (normalized[0],),
            ).fetchone()
            queued += enqueue(db, config, row)
            db.execute(
                "DELETE FROM delivery_context_dirty WHERE machine=? AND source_uid=?",
                (machine, uid),
            )
    return queued


def publish(db, limit=4000, budget=0.35):
    config = db.execute(
        "SELECT enrollment_id,installation_id,destination_generation,forward_ts FROM delivery_config"
    ).fetchone()
    if not config or not config[3]:
        return dict(scanned=0, queued=0)
    cursor, scope_seq, turn = db.execute(
        "SELECT source_seq,scope_seq,turn FROM delivery_report_progress"
    ).fetchone()
    latest = db.execute("SELECT max(id) FROM delivery_journal").fetchone()[0] or 0
    with db:
        db.execute(
            "INSERT OR IGNORE INTO delivery_report_scope(machine,tool,session) SELECT DISTINCT machine,tool,session FROM delivery_snapshots WHERE journal_id>? AND journal_id<=? AND COALESCE(origin,'')<>'derived_reporting' AND session<>''",
            (scope_seq, latest),
        )
        db.execute("UPDATE delivery_report_progress SET scope_seq=? WHERE id=1", (latest,))
    context_queued = publish_contexts(db, config)
    scope = set(db.execute("SELECT machine,tool,session FROM delivery_report_scope"))
    repos = {
        r[0]
        for r in db.execute(
            "SELECT DISTINCT c.repo FROM delivery_report_scope s CROSS JOIN work_contexts c INDEXED BY work_context_session ON c.machine=s.machine AND c.tool=s.tool AND c.session=s.session WHERE c.repo<>''"
        )
    }
    prs = {url for repo, url in db.execute("SELECT repo,url FROM pull_requests") if repo in repos}
    accounts = {
        r[0]
        for r in db.execute(
            "SELECT DISTINCT i.account_key FROM delivery_report_scope s CROSS JOIN identity_events i INDEXED BY delivery_identity_scope ON i.machine=s.machine AND i.tool=s.tool AND i.session=s.session WHERE i.account_key<>''"
        )
    }
    accounts.update(
        r[0]
        for r in db.execute(
            "SELECT DISTINCT i.account_key FROM delivery_report_scope s CROSS JOIN session_route_events i INDEXED BY delivery_route_scope ON i.machine=s.machine AND i.tool=s.tool AND i.session=s.session WHERE i.account_key<>'' AND i.account_provider<>'' AND i.scope<>'configuration'"
        )
    )
    high = db.execute("SELECT seq FROM journal_clock WHERE id=1").fetchone()[0]
    # Global headers are reconsidered when eligibility changes, including a
    # late route/account/repository for an already-enrolled session. A new
    # session using the same keys must not rescan unrelated retained headers.
    digest = wire.digest(
        wire.encode([sorted(repos), sorted(prs, key=lambda v: json.dumps(v)), sorted(accounts)])
    )
    previous = db.execute("SELECT digest FROM delivery_report_global_scope WHERE id=1").fetchone()
    global_changed = previous is None or previous[0] != digest
    new_scopes = bool(
        db.execute("SELECT 1 FROM delivery_report_scope WHERE seeded=0 LIMIT 1").fetchone()
    )
    ids = []
    unsent = "NOT EXISTS(SELECT 1 FROM delivery_report_sent sent WHERE sent.event_id=e.event_id AND sent.source_seq>=e.seq)"
    if new_scopes:
        # Seek only new session identities; never scan all retained kinds or
        # recent host samples to find their content-free reporting evidence.
        ids = db.execute(
            "SELECT e.event_id,e.seq FROM delivery_report_scope s CROSS JOIN observations e INDEXED BY delivery_observation_scope ON e.machine=s.machine AND CAST(json_extract(e.payload,'$.tool') AS TEXT)=s.tool AND CAST(json_extract(e.payload,'$.session') AS TEXT)=s.session WHERE s.seeded=0 AND e.kind IN ("
            + session_kinds()
            + ") AND e.seq<=? AND (e.kind NOT IN ('llm','prompt','edit','session','session_cost','quota') OR e.ts>=?) AND "
            + unsent,
            (high, config[3]),
        ).fetchall()
    if global_changed:
        global_ids = db.execute(
            "SELECT event_id,payload,kind,seq FROM observations e INDEXED BY observations_kind WHERE kind IN ('pull_request','pr_commit','work_commit','subscription_charge','identity','account_binding') AND seq<=? AND "
            + unsent,
            (high,),
        )
        for eid, encoded, kind, source_seq in global_ids:
            value = json.loads(encoded)
            relevant = (
                value.get("repo") in repos
                if kind in ("pull_request", "work_commit")
                else value.get("pr_url") in prs
                if kind == "pr_commit"
                else (kind == "subscription_charge" or not value.get("session"))
                and bool(value.get("account_key"))
                and value["account_key"] in accounts
            )
            if relevant:
                ids.append((eid, source_seq))
        ids += db.execute(
            "SELECT event_id,seq FROM observations e INDEXED BY observations_ts WHERE kind IN ('commit','quota') AND ts>=? AND seq<=? AND "
            + unsent,
            (config[3], high),
        ).fetchall()
    if new_scopes or global_changed:
        with db:
            db.executemany(
                "INSERT OR IGNORE INTO delivery_report_seed(event_id,source_seq) VALUES(?,?)", ids
            )
            if new_scopes:
                db.execute("UPDATE delivery_report_scope SET seeded=1")
            if global_changed:
                db.execute(
                    "INSERT INTO delivery_report_global_scope VALUES(1,?) ON CONFLICT(id) DO UPDATE SET digest=excluded.digest",
                    (digest,),
                )
            if cursor == 0:
                db.execute("UPDATE delivery_report_progress SET source_seq=? WHERE id=1", (high,))
                cursor = high
    with db:
        db.execute(
            "DELETE FROM delivery_report_seed WHERE event_id IN (SELECT s.event_id FROM (SELECT event_id FROM delivery_report_seed ORDER BY source_seq LIMIT 4000) s JOIN delivery_report_sent sent ON s.event_id=sent.event_id JOIN observations o ON o.event_id=s.event_id WHERE sent.source_seq>=o.seq LIMIT 4000)"
        )

    def seed_rows():
        return db.execute(
            "SELECT o.event_id,o.machine,o.kind,o.ts,o.payload,o.content_hash,o.seq FROM delivery_report_seed s INDEXED BY delivery_report_seed_order JOIN observations o ON o.event_id=s.event_id WHERE NOT EXISTS(SELECT 1 FROM delivery_report_sent sent WHERE sent.event_id=o.event_id AND sent.source_seq>=o.seq) ORDER BY s.source_seq LIMIT ?",
            (limit,),
        ).fetchall()

    def delta_rows():
        return db.execute(
            "SELECT event_id,machine,kind,ts,payload,content_hash,seq FROM observations WHERE seq>? AND seq<=? AND kind NOT IN ('metric','note','source') AND (kind NOT IN ('llm','prompt','edit','commit') OR ts>=?) ORDER BY seq LIMIT ?",
            (cursor, high, config[3], limit),
        ).fetchall()

    # Never put ongoing PR/account/usage changes behind bootstrap candidates.
    # Alternate productive lanes, falling back immediately when either is empty.
    is_seed = turn % 2 == 0
    rows = seed_rows() if is_seed else delta_rows()
    if not rows:
        is_seed = not is_seed
        rows = seed_rows() if is_seed else delta_rows()
    started = time.monotonic()
    pending = []
    last = cursor
    scanned = 0
    seen = []
    for row in rows:
        if scanned and time.monotonic() - started >= budget:
            break
        event_id, machine, kind, ts, payload, sha, seq = row
        last = cursor if is_seed else seq
        scanned += 1
        seen.append((event_id,))
        value = json.loads(payload)
        selected = False
        if kind in SESSION:
            selected = (machine, value.get("tool"), value.get("session")) in scope
            if not selected and kind in ("identity", "quota", "account_binding"):
                selected = (
                    not value.get("session")
                    and bool(value.get("account_key"))
                    and value["account_key"] in accounts
                )
        elif kind in ("pull_request", "work_commit"):
            selected = value.get("repo") in repos
        elif kind == "pr_commit":
            selected = value.get("pr_url") in prs
        elif kind == "commit":
            selected = value.get("repo") in repos
        elif kind == "subscription_charge":
            selected = bool(value.get("account_key")) and value["account_key"] in accounts
        # Recheck at publication, including durable seeds made by earlier runtimes.
        # Old session summaries/costs are activity, not harmless context headers.
        if kind in ACTIVITY | {"session", "commit", "quota", "session_cost"} and ts < config[3]:
            selected = False
        if selected:
            pending.append(row)
        if len(pending) >= 512:
            break
    if not rows:
        last = high
    queued = context_queued
    with db:
        for row in pending:
            try:
                queued += enqueue(db, config, row)
            except (ValueError, TypeError, wire.ProtocolError) as error:
                db.execute(
                    "INSERT OR REPLACE INTO delivery_report_errors VALUES(?,?,?)",
                    (row[0], row[-1], type(error).__name__),
                )
        db.executemany("DELETE FROM delivery_report_seed WHERE event_id=?", seen)
        if not is_seed:
            db.execute("UPDATE delivery_report_progress SET source_seq=? WHERE id=1", (last,))
        db.execute("UPDATE delivery_report_progress SET turn=turn+1 WHERE id=1")
    return dict(
        scanned=scanned,
        queued=queued,
        source_seq=last,
        source_high_water=high,
        seed_pending=db.execute("SELECT count(*) FROM delivery_report_seed").fetchone()[0],
        errors=db.execute("SELECT count(*) FROM delivery_report_errors").fetchone()[0],
    )


def apply(db, payload, forward_ts=0):
    """Allowlisted content-free observations, never record-supplied SQL tables."""
    if payload.get("origin") != "derived_reporting" or payload.get("kind") not in (
        "usage_evidence",
        "work_evidence",
        "account_evidence",
    ):
        return False
    value = payload["body"]
    if value.get("schema_version") != 1 or value.get("kind") not in SESSION | CONTEXT | ACCOUNT:
        raise ValueError("unknown reporting evidence schema")
    if payload.get("record_kind") != payload.get("kind"):
        raise ValueError("reporting record kind mismatch")
    event = dict(value["payload"], t=value["kind"])
    row = unified.normalize(value["machine"], event)
    if row[0] != value["event_id"] or row[5] != value["content_hash"]:
        raise ValueError("reporting observation digest mismatch")
    # Older approved headers may exist in Delta; they cannot introduce pre-cutoff
    # activity or cumulative session counters into a forward-only report.
    if (
        value["kind"] in ACTIVITY | {"session", "commit", "quota", "session_cost"}
        and row[3] < forward_ts
    ):
        return True
    from . import store

    if value["kind"] == "project_context":
        from . import projects

        projects.observe_context(
            db,
            value["machine"],
            event.get("tool"),
            event.get("session"),
            event["ts"],
            event["uid"],
            event.get("cwd"),
            event.get("repo"),
        )
        unified.record(db, value["machine"], [event])
        return True
    store.ingest(db, [json.dumps(event)], machine=value["machine"], transaction=False)
    return True
