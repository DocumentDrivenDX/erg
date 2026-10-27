"""Durable hourly reporting, rebuilt off the request path from canonical winners.

Each dirty batch has one pinned source snapshot. Staging writes are short; the
whole batch becomes visible through one atomic head swap. Capture can continue
while a paced reader builds. Exact partial-hour costs use already priced facts.
"""

import contextlib
import hashlib
import json
import math
import re
import sqlite3
import time
import uuid
from typing import Callable, Union

from . import (
    call_projection,
    project_dataset,
    project_reports,
    project_scope,
    projects,
    rates,
    report_scope,
    store,
)

VERSION = "3"
DIM = "human machine tool session folder reason binding_refs".split()
NUM = "distinct_calls request_equivalents tokens priced_equivalents unpriced_equivalents known_value_low_usd known_value_high_usd reported_equivalents known_reported_usd".split()
HEAD = "version hour ts human machine tool session folder reason model kind requester binding_refs".split()
FIELDS = HEAD + NUM + ["first_ts", "last_ts"]


def initialize(db):
    db.execute("CREATE TABLE IF NOT EXISTS project_rollup_state(key TEXT PRIMARY KEY,value TEXT)")
    db.execute(
        "INSERT INTO project_rollup_state VALUES('_dirty_cause','') ON CONFLICT(key) DO NOTHING"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS project_rollup_dirty(hour INTEGER PRIMARY KEY,serial INTEGER NOT NULL)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS project_rollup_invalidations(source TEXT PRIMARY KEY,queued_rows INTEGER,max_batch INTEGER,last_at INTEGER)"
    )
    columns = {r[1] for r in db.execute("PRAGMA table_info(project_rollup_invalidations)")}
    for column in ("historical_rows", "oldest_hour"):
        if column not in columns:
            db.execute(
                "ALTER TABLE project_rollup_invalidations ADD COLUMN "
                + column
                + (" INTEGER" if column == "oldest_hour" else " INTEGER DEFAULT 0")
            )
    for event in ("INSERT", "UPDATE"):
        db.execute(
            "CREATE TRIGGER IF NOT EXISTS project_rollup_dirty_diagnostic_"
            + event.lower()
            + " AFTER "
            + event
            + " ON project_rollup_dirty BEGIN INSERT INTO project_rollup_invalidations(source,queued_rows,max_batch,last_at,historical_rows,oldest_hour) SELECT COALESCE(NULLIF((SELECT value FROM project_rollup_state WHERE key='_dirty_cause'),''),'unattributed'),1,1,CAST(strftime('%s','now') AS INT),CASE WHEN NEW.hour<CAST(strftime('%s','now') AS INT)/3600 THEN 1 ELSE 0 END,NEW.hour ON CONFLICT(source) DO UPDATE SET queued_rows=queued_rows+1,last_at=excluded.last_at,historical_rows=coalesce(historical_rows,0)+excluded.historical_rows,oldest_hour=MIN(coalesce(oldest_hour,excluded.oldest_hour),excluded.oldest_hour); END"
        )
    db.execute(
        "CREATE TABLE IF NOT EXISTS project_rollup_watermarks(hour INTEGER PRIMARY KEY,version TEXT,source_at REAL)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS project_rollup_capture(id INTEGER PRIMARY KEY CHECK(id=1),data TEXT)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS project_rollup_heads(hour INTEGER PRIMARY KEY,version TEXT NOT NULL)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS project_rollup_head_version ON project_rollup_heads(version,hour)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS project_rollup_payload(version TEXT,hour INTEGER,data TEXT,meta TEXT,PRIMARY KEY(version,hour))"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS project_rollup_gc(version TEXT,hour INTEGER,PRIMARY KEY(version,hour))"
    )
    ddl = ",".join(
        n
        + (
            " INTEGER"
            if n in ("hour", "ts")
            else " REAL"
            if n in NUM + ["first_ts", "last_ts"]
            else " TEXT"
        )
        for n in FIELDS
    )
    for name in ("fine", "hour"):
        table = "project_rollup_cost_" + name
        db.execute("CREATE TABLE IF NOT EXISTS " + table + "(" + ddl + ")")
        db.execute(
            "CREATE INDEX IF NOT EXISTS " + table + "_version ON " + table + "(version,hour)"
        )
        db.execute("CREATE INDEX IF NOT EXISTS " + table + "_scope ON " + table + "(human,hour)")

    # Only derived canonical winners trigger usage invalidation. Their explicit
    # deletes cover implicit source REPLACE; shared response/request peers cover
    # suppression changes even when the peer winner itself did not change.
    def queue(select):
        return (
            "INSERT INTO project_rollup_dirty "
            + select
            + " ON CONFLICT(hour) DO UPDATE SET serial=serial+1; "
        )

    def hour(expr):
        return queue("SELECT " + expr + "/3600,1 WHERE " + expr + " IS NOT NULL")

    def peers(alias):
        # Own old/new hours already cover UID winner movement. Only mirrored
        # identities have peers; both probes use existing covering indexes.
        return queue(
            "SELECT DISTINCT ts/3600,1 FROM call_winner_rows WHERE "
            + alias
            + ".response_id IS NOT NULL AND "
            + alias
            + ".response_id<>'' AND machine="
            + alias
            + ".machine AND response_id="
            + alias
            + ".response_id AND model="
            + alias
            + ".model"
        ) + queue(
            "SELECT DISTINCT ts/3600,1 FROM call_winner_rows WHERE "
            + alias
            + ".request_id IS NOT NULL AND "
            + alias
            + ".request_id<>'' AND request_id="
            + alias
            + ".request_id AND model="
            + alias
            + ".model"
        )

    for event, alias in [("INSERT", "NEW"), ("DELETE", "OLD")]:
        trigger(
            db,
            "winner_" + event.lower(),
            "AFTER " + event + " ON call_winner_rows",
            hour(alias + ".ts") + peers(alias),
        )
    trigger(
        db,
        "winner_update",
        "AFTER UPDATE ON call_winner_rows",
        hour("OLD.ts") + hour("NEW.ts") + peers("OLD") + peers("NEW"),
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS prompts_project_rollup_identity ON prompts(tool,COALESCE(NULLIF(source_uid,''),uid),ts)"
    )
    db.execute("CREATE INDEX IF NOT EXISTS commits_project_rollup_identity ON commits(uid,machine)")
    for table in ("edits", "prompts", "commits"):
        db.execute(
            "CREATE INDEX IF NOT EXISTS " + table + "_project_rollup_time ON " + table + "(ts)"
        )
        if table != "commits":
            db.execute(
                "CREATE INDEX IF NOT EXISTS "
                + table
                + "_project_rollup_session ON "
                + table
                + "(machine,tool,session,ts)"
            )
        for event in ("INSERT", "UPDATE", "DELETE"):
            aliases = (
                ("NEW",) if event == "INSERT" else ("OLD",) if event == "DELETE" else ("OLD", "NEW")
            )
            body = ""
            if event == "INSERT":
                body += queue(
                    "SELECT ts/3600,1 FROM " + table + " WHERE machine=NEW.machine AND uid=NEW.uid"
                )
                if table == "prompts":
                    body += queue(
                        "SELECT DISTINCT p.ts/3600,1 FROM prompts p JOIN prompts prior ON prior.machine=NEW.machine AND prior.uid=NEW.uid WHERE p.tool=prior.tool AND COALESCE(NULLIF(p.source_uid,''),p.uid)=COALESCE(NULLIF(prior.source_uid,''),prior.uid)"
                    )
            for alias in aliases:
                body += hour(alias + ".ts")
                if table == "commits":
                    body += queue(
                        "SELECT DISTINCT ts/3600,1 FROM commits WHERE uid=" + alias + ".uid"
                    )
                if table == "prompts":
                    body += queue(
                        "SELECT DISTINCT ts/3600,1 FROM prompts WHERE tool="
                        + alias
                        + ".tool AND COALESCE(NULLIF(source_uid,''),uid)=COALESCE(NULLIF("
                        + alias
                        + ".source_uid,''),"
                        + alias
                        + ".uid)"
                    )
            when = ""
            if event == "INSERT":
                cols = [r[1] for r in db.execute("PRAGMA table_info(" + table + ")")]
                when = (
                    " WHEN NOT EXISTS(SELECT 1 FROM "
                    + table
                    + " prior WHERE prior.machine=NEW.machine AND prior.uid=NEW.uid AND "
                    + " AND ".join("prior." + c + " IS NEW." + c for c in cols)
                    + ")"
                )
            if event == "UPDATE":
                cols = [r[1] for r in db.execute("PRAGMA table_info(" + table + ")")]
                when = " WHEN " + " OR ".join("OLD." + c + " IS NOT NEW." + c for c in cols)
            trigger(
                db,
                table + "_" + event.lower(),
                ("BEFORE INSERT" if event == "INSERT" else "AFTER " + event)
                + " ON "
                + table
                + when,
                body,
            )
    db.execute("CREATE INDEX IF NOT EXISTS call_winner_project_rollup_time ON call_winner_rows(ts)")
    db.execute(
        "CREATE INDEX IF NOT EXISTS llm_project_rollup_session ON llm(machine,tool,session,ts)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS work_context_project_rollup_session ON work_contexts(machine,tool,session,ts)"
    )
    for table in ("project_contexts", "work_contexts"):
        cols = ["machine", "tool", "session", "ts"] + (
            ["cwd", "repo"] if table == "project_contexts" else ["repo", "worktree"]
        )
        body = ""
        identity = "source_uid" if table == "project_contexts" else "uid"
        for source in ("llm", "edits", "prompts"):
            body += queue(
                "SELECT DISTINCT r.ts/3600,1 FROM "
                + source
                + " r JOIN "
                + table
                + " prior ON prior.machine=NEW.machine AND prior."
                + identity
                + "=NEW."
                + identity
                + " WHERE r.machine=prior.machine AND r.tool=prior.tool AND r.session=prior.session AND r.ts>=prior.ts"
            )
        for source in ("llm", "edits", "prompts"):
            body += queue(
                "SELECT DISTINCT ts/3600,1 FROM "
                + source
                + " WHERE machine=NEW.machine AND tool=NEW.tool AND session=NEW.session AND ts>=NEW.ts"
            )
        when = (
            " WHEN NOT EXISTS(SELECT 1 FROM "
            + table
            + " prior WHERE prior.machine=NEW.machine AND prior."
            + identity
            + "=NEW."
            + identity
            + " AND "
            + " AND ".join("prior." + c + " IS NEW." + c for c in cols)
            + ")"
        )
        trigger(db, table + "_replace", "BEFORE INSERT ON " + table + when, body)
        db.execute("DROP TRIGGER IF EXISTS erg_project_rollup_" + table + "_insert")
        for event in ("UPDATE", "DELETE"):
            body = ""
            for alias in (
                ("NEW",) if event == "INSERT" else ("OLD",) if event == "DELETE" else ("OLD", "NEW")
            ):
                for source in ("llm", "edits", "prompts"):
                    body += queue(
                        "SELECT DISTINCT ts/3600,1 FROM "
                        + source
                        + " WHERE machine="
                        + alias
                        + ".machine AND tool="
                        + alias
                        + ".tool AND session="
                        + alias
                        + ".session AND ts>="
                        + alias
                        + ".ts"
                    )
            when = (
                " WHEN " + " OR ".join("OLD." + c + " IS NOT NEW." + c for c in cols)
                if event == "UPDATE"
                else ""
            )
            trigger(db, table + "_" + event.lower(), "AFTER " + event + " ON " + table + when, body)

    from . import report_evidence

    report_evidence.initialize(db, trigger)
    from . import rollup_dependencies

    rollup_dependencies.initialize(db, instrument_dirty)


def instrument_dirty(body, name):
    # Writer transactions serialize the temporary cause label; dirty-table hooks
    # count exact affected hours, including updates of already-queued debt.
    before = "UPDATE project_rollup_state SET value='" + name + "' WHERE key='_dirty_cause'; "
    after = " UPDATE project_rollup_state SET value='' WHERE key='_dirty_cause'; "
    return re.sub(
        r"(INSERT INTO project_rollup_dirty[^;]*;)", lambda m: before + m[0] + after, body
    )


def trigger(db, name, event, body):
    name = "erg_project_rollup_" + name
    body = instrument_dirty(body, name)
    sql = "CREATE TRIGGER " + name + " " + event + " BEGIN " + body + "END"
    old = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,)
    ).fetchone()
    if old and old[0] != sql:
        db.execute("DROP TRIGGER " + name)
        old = None
    if not old:
        db.execute(sql)


def state(db):
    return dict(db.execute("SELECT key,value FROM project_rollup_state"))


def put(db, **values):
    db.executemany(
        "INSERT INTO project_rollup_state VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        [(k, str(v)) for k, v in values.items()],
    )


def signature(config, card):
    clean = {k: v for k, v in card.items() if not k.startswith("_")}
    return hashlib.sha256(
        json.dumps(
            [VERSION, call_projection.definition(), config, clean],
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def capture_progress(db, usage, work):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='project_rollup_capture'").fetchone():
        return
    newest = db.execute("SELECT seq,ts FROM history_events ORDER BY seq DESC LIMIT 1").fetchone()
    oldest = db.execute("SELECT enqueued_at FROM history_pending ORDER BY seq LIMIT 1").fetchone()
    progress = dict(
        sampled_at=time.time(),
        captured_seq=newest[0] if newest else 0,
        latest_event_at=newest[1] if newest else None,
        usage_pending=usage["pending"],
        work_pending=work["pending"],
        usage_oldest_enqueued_at=oldest[0] if oldest else None,
    )
    with db:
        db.execute(
            "INSERT OR REPLACE INTO project_rollup_capture VALUES(1,?)", (json.dumps(progress),)
        )


def freshness(db):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='project_rollup_capture'").fetchone():
        return {}
    raw = db.execute("SELECT data FROM project_rollup_capture WHERE id=1").fetchone()
    latest = db.execute(
        "SELECT w.source_at FROM project_rollup_watermarks w JOIN project_rollup_heads h ON w.hour=h.hour AND w.version=h.version ORDER BY h.hour DESC LIMIT 1"
    ).fetchone()
    progress = json.loads(raw[0]) if raw else {}
    return dict(
        capture=progress,
        recent_usage_source_at=latest[0] if latest else None,
        freshness_basis="Pinned source time of newest published hour; queue counts sampled by collector. Event times are not completeness watermarks.",
    )


def status(db):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='project_rollup_state'").fetchone():
        return dict(enabled=False, ready=False, state="not_installed")
    s = dict(
        db.execute(
            "SELECT key,value FROM project_rollup_state WHERE key NOT IN ('catalog','evidence_metadata')"
        )
    )
    from . import quota_projection

    quota_pending = quota_projection.status(db)
    target = projects.snapshot(db)["revision"]
    stale = bool(s.get("signature") and int(s.get("revision", 0)) != target)
    return dict(
        **freshness(db),
        quota_projection=quota_pending,
        pending_closed_hours=db.execute(
            "SELECT count(*) FROM project_rollup_dirty WHERE hour<?", (int(time.time()) // 3600,)
        ).fetchone()[0],
        pending_historical_hours=db.execute(
            "SELECT count(*) FROM project_rollup_dirty WHERE hour<?",
            (int(time.time()) // 3600 - 1,),
        ).fetchone()[0],
        migration_pending=s.get("dependency_bootstrap") == "1",
        target_project_revision=target,
        stale_policy=stale,
        enabled=s.get("enabled") == "1",
        ready=bool(s.get("signature")),
        state="reallocating" if stale else "ready" if s.get("signature") else "warming",
        project_revision=int(s.get("revision", 0)),
        updated_at=float(s.get("updated_at", 0)),
        pending_metadata=s.get("evidence_serial", "0") != s.get("evidence_published_serial", ""),
        pending_hours=db.execute("SELECT count(*) FROM project_rollup_dirty").fetchone()[0],
        last_error=s.get("error") or None,
        building=s.get("building") == "1",
        completed_hours=int(s.get("completed_hours", 0)),
        batch_hours=int(s.get("batch_hours", 0)),
    )


class Pacer:
    """Average worker CPU allowance that can follow live projection priority."""

    def __init__(self, stop=None, duty: Union[float, Callable[[], float]] = 0.02):
        self.stop = stop
        self.duty = duty
        self.allowance()
        self.cpu = time.thread_time()
        self.wall = time.monotonic()
        self.n = 0

    def allowance(self):
        value = self.duty() if callable(self.duty) else self.duty
        if not 0 < value <= 1:
            raise ValueError("rollup duty must be in (0, 1]")
        return value

    def __call__(self, force=False):
        self.n += 1
        if not force and self.n % 32:
            return
        delay = (time.thread_time() - self.cpu) / self.allowance() - (time.monotonic() - self.wall)
        while delay > 0:
            if self.stop is not None:
                if self.stop.wait(min(0.25, delay)):
                    raise InterruptedError("rollup stopped")
            else:
                time.sleep(min(0.25, delay))
            delay = (time.thread_time() - self.cpu) / self.allowance() - (
                time.monotonic() - self.wall
            )
        if self.stop is not None and self.stop.is_set():
            raise InterruptedError("rollup stopped")


def gc(db, limit=256):
    row = db.execute("SELECT version,hour FROM project_rollup_gc LIMIT 1").fetchone()
    if not row:
        return
    with db:
        for table in (
            "project_rollup_cost_fine",
            "project_rollup_cost_hour",
            "project_rollup_evidence_fine",
            "project_rollup_evidence_hour",
        ):
            db.execute(
                "DELETE FROM "
                + table
                + " WHERE rowid IN (SELECT rowid FROM "
                + table
                + " WHERE version=? AND hour=? LIMIT ?)",
                (*row, limit),
            )
            if db.execute(
                "SELECT 1 FROM " + table + " WHERE version=? AND hour=? LIMIT 1", row
            ).fetchone():
                return
        db.execute("DELETE FROM project_rollup_payload WHERE version=? AND hour=?", row)
        db.execute("DELETE FROM project_rollup_gc WHERE version=? AND hour=?", row)


def rebuild(database, tick=None, progress=None, max_hours=None, preparation_cache=None):
    """One dirty batch, one source snapshot, one atomic publication; no imports."""
    tick = tick or (lambda **kwargs: None)
    with contextlib.closing(store.open_readonly(database)) as source, contextlib.closing(
        store.connect(path=database, initialize=False, autocheckpoint=False)
    ) as writer:
        source_at = time.time()
        source.execute("BEGIN")
        if not call_projection.check(source, pending_limit=0).get("ready"):
            return dict(state="waiting_for_canonical_winners")
        config = projects.snapshot(source)
        card = rates.load(source)
        sig = signature(config, card)
        current = state(source)
        if current.get("signature") != sig:
            hours = {r[0] for r in source.execute("SELECT hour FROM project_rollup_heads")}
            for table in ("call_winner_rows", "edits", "prompts", "commits"):
                hours.update(
                    r[0]
                    for r in source.execute(
                        "SELECT DISTINCT ts/3600 FROM " + table + " WHERE ts IS NOT NULL"
                    )
                )
            with writer:
                writer.executemany(
                    "INSERT INTO project_rollup_dirty VALUES(?,1) ON CONFLICT(hour) DO NOTHING",
                    [(h,) for h in hours],
                )
            # Pin dirty serials after initialization, with the same source state.
            # Source triggers active before this snapshot enqueue later changes.
            dirty = dict(source.execute("SELECT hour,serial FROM project_rollup_dirty"))
            for h in hours:
                dirty.setdefault(h, 1)
        else:
            dirty = dict(source.execute("SELECT hour,serial FROM project_rollup_dirty"))
        if (
            max_hours
            and current.get("dependency_bootstrap") != "1"
            and current.get("signature") == sig
            and len(dirty) > max_hours
        ):
            # Reserve one slot for the oldest debt; keep recent activity moving.
            chosen = (
                ([min(dirty)] if int(current.get("batch_turn", "0")) % 3 == 2 else [max(dirty)])
                if max_hours == 1
                else sorted(dirty, reverse=True)[: max_hours - 1] + [min(dirty)]
            )
            from . import rollup_dependencies

            chosen = rollup_dependencies.cohort(source, chosen)
            dirty = {h: dirty[h] for h in chosen if h in dirty}
        dependencies = [
            (a, b, serial)
            for a, b, serial in source.execute("SELECT a,b,serial FROM project_rollup_dependencies")
            if a in dirty and b in dirty
        ]
        from . import report_evidence

        metadata_changed = current.get("evidence_published_serial", "") != current.get(
            "evidence_serial", "0"
        )
        work_changed = current.get("evidence_work_serial", "0") != current.get(
            "evidence_work_published_serial", ""
        )
        prepared = (
            report_evidence.prepare(
                source,
                tick,
                json.loads(current["evidence_metadata"])
                if not dirty and not work_changed and current.get("evidence_metadata")
                else None,
                cache=preparation_cache,
                versions=current,
            )
            if dirty or metadata_changed or not current.get("evidence_metadata")
            else None
        )
        metadata = (
            json.dumps(prepared[3], separators=(",", ":"))
            if prepared
            else current.get("evidence_metadata")
        )
        if not dirty:
            if current.get("dependency_bootstrap") == "1":
                with writer:
                    put(writer, dependency_bootstrap=0)
            if prepared:
                with writer:
                    put(
                        writer,
                        evidence_metadata=metadata,
                        evidence_published_serial=current.get("evidence_serial", "0"),
                        evidence_work_published_serial=current.get("evidence_work_serial", "0"),
                        updated_at=time.time(),
                    )
            if not current.get("signature"):
                with writer:
                    put(
                        writer,
                        signature=sig,
                        revision=config["revision"],
                        catalog=json.dumps(config["projects"]),
                        binding_count=len(config["bindings"]),
                        updated_at=time.time(),
                        building=0,
                    )
            return dict(state="idle", hours=0)
        job = uuid.uuid4().hex
        with writer:
            put(writer, building=1, completed_hours=0, batch_hours=len(dirty), error="")
        all_heads = []
        for number, (hour, serial) in enumerate(sorted(dirty.items(), reverse=True), 1):
            lower = hour * 3600
            upper = lower + 3600
            identities = set()
            for table in ("call_winner_rows", "edits", "prompts"):
                identities.update(
                    source.execute(
                        "SELECT DISTINCT machine,tool,session FROM "
                        + table
                        + " WHERE ts>=? AND ts<?",
                        (lower, upper),
                    )
                )
            scope = project_scope.Scope(source, upper, config, identities)
            fine = []
            cells = {}
            evidence = report_evidence.Builder(prepared, job, hour, writer)
            with writer:
                writer.execute("INSERT OR IGNORE INTO project_rollup_gc VALUES(?,?)", (job, hour))
            insert = (
                "INSERT INTO project_rollup_cost_fine VALUES(" + ",".join("?" for _ in FIELDS) + ")"
            )

            def flush():
                if fine:
                    with writer:
                        writer.executemany(insert, fine)
                    fine.clear()

            def on_call(call, allocation, value):
                evidence.add(call, allocation, value)
                reason = allocation["reason"] or ""
                parts = [dict(project_key="", units=projects.UNITS)] + allocation["allocations"]
                for part in parts:
                    weight = part["units"] / projects.UNITS
                    reported = call.get("reported_cost_usd")
                    known = (
                        isinstance(reported, (int, float))
                        and not isinstance(reported, bool)
                        and math.isfinite(reported)
                        and reported >= 0
                    )
                    dims = [
                        part["project_key"],
                        call["machine"],
                        call["tool"],
                        call["session"],
                        allocation["folder"]["folder_key"],
                        reason,
                        call.get("model"),
                        call.get("kind"),
                        call.get("requester"),
                        json.dumps(allocation["binding_refs"]),
                    ]
                    metrics = [
                        1,
                        weight,
                        sum(
                            call.get(k) or 0
                            for k in ("fresh_in", "cache_read", "cache_write", "out")
                        )
                        * weight,
                        weight if value else 0,
                        0 if value else weight,
                        (value["low_usd"] if value else 0) * weight,
                        (value["high_usd"] if value else 0) * weight,
                        weight if known else 0,
                        (reported if known else 0) * weight,
                    ]
                    fine.append([job, hour, call["ts"]] + dims + metrics + [call["ts"], call["ts"]])
                    key = tuple(dims)
                    if key not in cells:
                        cells[key] = [0.0] * len(NUM) + [call["ts"], call["ts"]]
                    cell = cells[key]
                    for i, v in enumerate(metrics):
                        cell[i] += v
                    cell[-2] = min(cell[-2], call["ts"])
                    cell[-1] = max(cell[-1], call["ts"])
                if len(fine) >= 256:
                    flush()

            data = project_dataset.build(
                source,
                lower,
                upper,
                scope,
                card,
                on_call,
                tick,
                lambda row, allocation: evidence.add(row, allocation, is_input=True),
            )
            evidence.finish()
            flush()
            tick(force=True)
            meta = {
                k: v
                for k, v in data.items()
                if k not in ("llm", "edits", "prompts", "commits", "session_allocations")
            }
            with writer:
                writer.executemany(
                    "INSERT INTO project_rollup_cost_hour VALUES("
                    + ",".join("?" for _ in FIELDS)
                    + ")",
                    (
                        [job, hour, hour * 3600] + list(key) + values
                        for key, values in cells.items()
                    ),
                )
                writer.execute(
                    "INSERT INTO project_rollup_payload VALUES(?,?,?,?)",
                    (
                        job,
                        hour,
                        json.dumps(data, separators=(",", ":")),
                        json.dumps(meta, separators=(",", ":")),
                    ),
                )
                put(writer, completed_hours=number)
            all_heads.append((hour, serial))
            tick(force=True)
            if progress:
                progress(dict(state="building", completed_hours=number, batch_hours=len(dirty)))
        # Source changes remain queued by serial. No half-batch is observable.
        with writer:
            writer.execute("BEGIN IMMEDIATE")
            for hour, serial in all_heads:
                writer.execute(
                    "INSERT OR IGNORE INTO project_rollup_gc SELECT version,hour FROM project_rollup_heads WHERE hour=?",
                    (hour,),
                )
                writer.execute(
                    "INSERT INTO project_rollup_heads VALUES(?,?) ON CONFLICT(hour) DO UPDATE SET version=excluded.version",
                    (hour, job),
                )
                writer.execute(
                    "DELETE FROM project_rollup_gc WHERE version=? AND hour=?", (job, hour)
                )
                writer.execute(
                    "DELETE FROM project_rollup_dirty WHERE hour=? AND serial=?", (hour, serial)
                )
                writer.execute(
                    "INSERT OR REPLACE INTO project_rollup_watermarks VALUES(?,?,?)",
                    (hour, job, source_at),
                )
            writer.executemany(
                "DELETE FROM project_rollup_dependencies WHERE a=? AND b=? AND serial=?",
                dependencies,
            )
            put(
                writer,
                signature=sig,
                revision=config["revision"],
                catalog=json.dumps(config["projects"]),
                binding_count=len(config["bindings"]),
                updated_at=time.time(),
                building=0,
                error="",
                evidence_metadata=metadata,
                evidence_published_serial=current.get("evidence_serial", "0"),
                evidence_work_published_serial=current.get("evidence_work_serial", "0"),
                batch_turn=int(current.get("batch_turn", "0")) + 1,
                dependency_bootstrap=0,
            )
        return dict(state="published", hours=len(all_heads), project_revision=config["revision"])


def dataset(db, since=0, piece_cache=None):
    s = status(db)
    if not s["enabled"]:
        return None
    result = dict(
        llm=[],
        edits=[],
        prompts=[],
        commits=[],
        folder_catalog=[],
        session_allocations=[],
        observed_tools=[],
        project_catalog=json.loads(state(db).get("catalog", "[]")),
        project_revision=s["project_revision"],
        rate_card_dates=[],
        rate_sources=[],
        rollup_status=s,
    )
    folders = {}
    sessions = {}
    sets = {k: set() for k in ("observed_tools", "rate_card_dates", "rate_sources")}
    if piece_cache is None:
        pieces = (
            json.loads(raw)
            for (raw,) in db.execute(
                "SELECT d.data FROM project_rollup_payload d JOIN project_rollup_heads h ON h.hour=d.hour AND h.version=d.version WHERE d.hour>=? ORDER BY d.hour",
                (since // 3600,),
            )
        )
    else:
        heads = list(
            db.execute(
                "SELECT hour,version FROM project_rollup_heads WHERE hour>=? ORDER BY hour",
                (since // 3600,),
            )
        )
        keep = {(version, hour) for hour, version in heads[-1024:]}
        for key in list(piece_cache):
            if key not in keep:
                del piece_cache[key]

        def decoded():
            for hour, version in heads:
                key = (version, hour)
                data = piece_cache.get(key)
                if data is None:
                    data = json.loads(
                        db.execute(
                            "SELECT data FROM project_rollup_payload WHERE version=? AND hour=?",
                            key,
                        ).fetchone()[0]
                    )
                    if key in keep:
                        piece_cache[key] = data
                yield data

        pieces = decoded()
    for data in pieces:
        for name in ("llm", "edits", "prompts", "commits"):
            result[name].extend(data[name])
        for key in data:
            if key.endswith("_cols") or key in ("project_allocation_scope", "cost_basis"):
                result[key] = data[key]
        for k in sets:
            sets[k].update(data[k])
        for f in data["folder_catalog"]:
            old = folders.setdefault(f["folder_key"], dict(f))
            old["project_keys"] = sorted(set(old["project_keys"]) | set(f["project_keys"]))
        for row in data["session_allocations"]:
            key = (row["machine"], row["tool"], row["session"])
            old = sessions.setdefault(key, dict(row))
            for name in ("project_keys", "folder_keys"):
                old[name] = sorted(set(old[name]) | set(row[name]))
    for kind, cols in [
        ("llm", project_dataset.LLM_COLS),
        ("edit", project_dataset.EDIT_COLS),
        ("prompt", project_dataset.PROMPT_COLS),
        ("commit", project_dataset.COMMIT_COLS),
    ]:
        result.setdefault(kind + "_cols", cols)
    result.update(
        folder_catalog=list(folders.values()),
        session_allocations=list(sessions.values()),
        **{k: sorted(v) for k, v in sets.items()},
    )
    return result


def query(db, selection, table="cost", dimensions=None, metrics=None, parent_only=False):
    dimensions = DIM if dimensions is None else dimensions
    metrics = NUM if metrics is None else metrics
    v = selection.values
    lower = (v["start"] + 3599) // 3600
    upper = v["end"] // 3600
    fields = (
        FIELDS
        if table == "cost"
        else ["version", "hour", "ts"] + dimensions + metrics + ["first_ts", "last_ts"]
    )
    cols = ",".join("c." + c for c in fields)
    relation = (
        "SELECT "
        + cols
        + " FROM project_rollup_"
        + table
        + "_hour c JOIN project_rollup_heads h ON c.hour=h.hour AND c.version=h.version WHERE c.hour>=? AND c.hour<? UNION ALL SELECT "
        + cols
        + " FROM project_rollup_"
        + table
        + "_fine c JOIN project_rollup_heads h ON c.hour=h.hour AND c.version=h.version WHERE h.hour IN (?,?) AND c.ts>=? AND c.ts<? AND (c.hour<? OR c.hour>=?)"
    )
    params = [
        lower,
        upper,
        v["start"] // 3600,
        v["end"] // 3600,
        v["start"],
        v["end"],
        lower,
        upper,
    ]
    where = []
    for field in ("machine", "tool", "session"):
        if v[field]:
            where.append(field + "=?")
            params.append(v[field])
    for field, values in [("tool", selection.tools), ("kind", selection.kinds)]:
        if values is not None:
            predicate = field + " IN (" + ",".join("?" for _ in values) + ")" if values else "0"
            if field == "kind":
                predicate = "(kind IS NULL OR " + predicate + ")"
            where.append(predicate)
            params.extend(sorted(values))
    if v["folder_key"]:
        where.append(
            "folder LIKE 'unobserved:%'" if v["folder_key"] == "@unobserved" else "folder=?"
        )
        if v["folder_key"] != "@unobserved":
            params.append(v["folder_key"])
    if v["requester"] not in ("", "all"):
        if v["requester"] == "me":
            ids = sorted(selection.identities)
            where.append(
                "(requester IS NULL OR requester=''"
                + (" OR requester IN (" + ",".join("?" for _ in ids) + ")" if ids else "")
                + ")"
            )
            params.extend(ids)
        else:
            where.append("requester=?")
            params.append(v["requester"])
    if v["project_key"]:
        where.append("human=?")
        params.append(v["project_key"])
    if parent_only and not v["project_key"]:
        where.append("human=''")
    sql = (
        "SELECT "
        + ",".join(dimensions)
        + ","
        + ",".join("SUM(" + c + ")" for c in metrics)
        + ",MIN(first_ts),MAX(last_ts) FROM ("
        + relation
        + ")"
        + (" WHERE " + " AND ".join(where) if where else "")
        + " GROUP BY "
        + ",".join(dimensions)
    )
    return db.execute(sql, params)


def dataset_metadata(db):
    folders = {}
    price_dates = set()
    price_sources = set()
    for (raw,) in db.execute(
        "SELECT d.meta FROM project_rollup_payload d JOIN project_rollup_heads h ON h.hour=d.hour AND h.version=d.version"
    ):
        metadata = json.loads(raw)
        price_dates.update(metadata["rate_card_dates"])
        price_sources.update(metadata["rate_sources"])
        for f in metadata["folder_catalog"]:
            old = folders.setdefault(f["folder_key"], dict(f))
            old["project_keys"] = sorted(set(old["project_keys"]) | set(f["project_keys"]))
    return dict(
        folder_catalog=list(folders.values()),
        rate_card_dates=sorted(price_dates),
        rate_sources=sorted(price_sources),
    )


def costs(db, **filters):
    s = status(db)
    if not s["enabled"]:
        return None
    if not s["ready"]:
        return dict(
            available=False, reason="Usage rollups are warming in the background", rollup_status=s
        )
    selection = report_scope.Selection(filters, store.my_identities())
    v = selection.values
    catalog = json.loads(state(db)["catalog"])
    names = {p["project_key"]: p["name"] for p in catalog}
    if v["project_key"] and v["project_key"] != "@unassigned" and v["project_key"] not in names:
        raise ValueError("Unknown accounting project")
    metadata = dataset_metadata(db)
    folders = {f["folder_key"]: f for f in metadata["folder_catalog"]}
    price_dates = metadata["rate_card_dates"]
    price_sources = metadata["rate_sources"]

    def empty():
        return {key: 0.0 for key in NUM}

    totals = empty()
    buckets = {}
    fb = {}
    sessions = {}
    reasons = {}
    tools = set()
    memberships = {}

    def add(target, row):
        for c in NUM:
            target[c] += row[c]

    for values in query(db, selection):
        row = dict(zip(DIM + NUM + ["first_ts", "last_ts"], values))
        human = row["human"]
        selected = human == v["project_key"] if v["project_key"] else human == ""
        if selected:
            add(totals, row)
            tools.add(row["tool"])
            if row["reason"]:
                reasons[row["reason"]] = reasons.get(row["reason"], 0) + row["distinct_calls"]
            key = (row["machine"], row["tool"], row["session"])
            sr = sessions.setdefault(
                key,
                dict(
                    empty(),
                    machine=key[0],
                    tool=key[1],
                    session=key[2],
                    projects=set(),
                    folders=set(),
                    reasons=set(),
                    binding_refs=set(),
                    first_ts=row["first_ts"],
                    last_ts=row["last_ts"],
                ),
            )
            add(sr, row)
            sr["first_ts"] = min(sr["first_ts"], row["first_ts"])
            sr["last_ts"] = max(sr["last_ts"], row["last_ts"])
            sr["folders"].add(row["folder"])
            if row["reason"]:
                sr["reasons"].add(row["reason"])
            sr["binding_refs"].update(json.loads(row["binding_refs"]))
        if not human:
            continue
        if human not in buckets:
            buckets[human] = dict(empty(), project_key=human, name=names.get(human, "Unassigned"))
        add(buckets[human], row)
        key = (human, row["folder"])
        if key not in fb:
            fb[key] = dict(empty(), **folders[row["folder"]], project_key=human, sessions=set())
        add(fb[key], row)
        fb[key]["sessions"].add((row["machine"], row["tool"], row["session"]))
        sk = (row["machine"], row["tool"], row["session"])
        memberships.setdefault(sk, set()).add(human)
    titles = {
        (r[0], r[1], r[2]): r[3]
        for r in db.execute("SELECT machine,tool,session,title FROM session_titles")
    }
    output = []
    # Project IDs sort before the all-project empty key is encountered only by
    # SQLite collation; derive memberships independently from folder metadata.
    for key, row in sorted(sessions.items(), key=lambda p: -p[1]["tokens"])[:100]:
        row = dict(row, title=titles.get(key))
        row["projects"] = sorted(memberships.get(key, set()))
        for field in ("folders", "reasons", "binding_refs"):
            row[field] = sorted(row[field])
        output.append(project_reports.finish(row))
    folder_rows = []
    for row in fb.values():
        row = dict(row)
        row["session_count"] = len(row.pop("sessions"))
        folder_rows.append(project_reports.finish(row))
    overlap = ("local" in tools and any(t not in ("local", "gateway") for t in tools)) or (
        "gateway" in tools and len(tools) > 1
    )
    return dict(
        available=True,
        generated=int(time.time()),
        revision=s["project_revision"],
        catalog=catalog,
        binding_count=int(state(db).get("binding_count", 0)),
        filters=v,
        totals=project_reports.finish(totals),
        projects=sorted(
            (project_reports.finish(r) for r in buckets.values()), key=lambda r: -r["tokens"]
        ),
        folders=sorted(folder_rows, key=lambda r: -r["tokens"]),
        folder_catalog=list(folders.values()),
        observed_tools=sorted(tools),
        sessions=output,
        session_count=len(sessions),
        sessions_truncated=len(sessions) > 100,
        unassigned_reasons=reasons,
        rate_card_dates=sorted(price_dates),
        rate_sources=sorted(price_sources),
        warnings=[
            "Unreconciled agent/server source observations may overlap; this is not a deduplicated physical-spend total."
        ]
        if overlap
        else [],
        monetary_basis="Reference token value and native reported estimates are separate; actual billed and allocated subscription expense are unknown",
        context_scope="Recorded context; durable incremental hourly reporting",
        rollup_status=s,
    )


# A few records between capture/projection ticks are normal, not catch-up debt.
CORPUS_PRESSURE_ROWS = 1024
RECENT_HOUR_COALESCE_SECONDS = 5


def worker_allowance(db, duty):
    for table in ("history_pending", "history_edit_pending", "history_work_pending"):
        if (
            db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            and db.execute(
                "SELECT 1 FROM " + table + " LIMIT 1 OFFSET ?", (CORPUS_PRESSURE_ROWS,)
            ).fetchone()
        ):
            return min(0.10, duty)
    return duty


def worker_schedule(db, now=None):
    now = time.time() if now is None else now
    hour = int(now) // 3600
    cold = db.execute(
        "SELECT 1 FROM project_rollup_dirty WHERE hour<? OR hour>? LIMIT 1", (hour - 1, hour)
    ).fetchone()
    if cold:
        return dict(publish=True, max_hours=4, delay=0.05, mode="catchup")
    hot = db.execute(
        "SELECT 1 FROM project_rollup_dirty WHERE hour BETWEEN ? AND ? LIMIT 1", (hour - 1, hour)
    ).fetchone()
    stamp = db.execute("SELECT value FROM project_rollup_state WHERE key='updated_at'").fetchone()
    age = now - float(stamp[0]) if stamp else RECENT_HOUR_COALESCE_SECONDS
    if hot and 0 <= age < RECENT_HOUR_COALESCE_SECONDS:
        return dict(
            publish=False,
            max_hours=2,
            delay=min(2, RECENT_HOUR_COALESCE_SECONDS - age),
            mode="coalescing",
        )
    return dict(
        publish=True, max_hours=2 if hot else 1, delay=2, mode="recent" if hot else "current"
    )


def run_worker(database, stop, update=None, duty=0.90, measure=None):
    import fcntl

    lock = open(str(database) + ".project-rollups.lock", "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        return
    worker_cpu_start = time.thread_time()
    try:
        with contextlib.closing(
            store.connect(path=database, initialize=False, autocheckpoint=False)
        ) as db:
            while not stop.is_set():
                try:
                    with db:
                        db.execute("BEGIN IMMEDIATE")
                        initialize(db)
                        put(db, enabled=1, building=0)
                    break
                except sqlite3.OperationalError as error:
                    if "locked" not in str(error).lower():
                        raise
                    stop.wait(2)
            pressure = dict(at=float("-inf"), value=duty)

            def allowance():
                now = time.monotonic()
                if now - pressure["at"] >= 0.25:
                    pressure.update(at=now, value=worker_allowance(db, duty))
                return pressure["value"]

            from . import report_evidence

            preparation_cache = report_evidence.PreparationCache()
            pacer = Pacer(stop, allowance)
            last_checkpoint = 0
            checkpoint = None
            while not stop.is_set():
                result = {}
                try:
                    if not db.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='erg_project_rollup_winner_insert'"
                    ).fetchone():
                        with db:
                            initialize(db)
                            put(db, signature="")
                    from . import quota_projection

                    quota = (
                        measure("project_rollups", quota_projection.run, db)
                        if measure
                        else quota_projection.run(db)
                    )
                    if quota.get("pending"):
                        if update:
                            update(dict(status(db), quota_projection=quota))
                        stop.wait(0.1)
                        continue
                    schedule = worker_schedule(db)
                    if not schedule["publish"]:
                        if update:
                            update(
                                dict(
                                    status(db),
                                    scheduler_mode=schedule["mode"],
                                    worker_cpu_seconds=time.thread_time() - worker_cpu_start,
                                )
                            )
                        stop.wait(schedule["delay"])
                        pacer = Pacer(stop, allowance)
                        continue

                    def progress(value):
                        return (
                            update(
                                dict(
                                    value, worker_cpu_seconds=time.thread_time() - worker_cpu_start
                                )
                            )
                            if update
                            else None
                        )

                    result = (
                        measure(
                            "project_rollups",
                            rebuild,
                            database,
                            pacer,
                            progress,
                            max_hours=schedule["max_hours"],
                            preparation_cache=preparation_cache,
                        )
                        if measure
                        else rebuild(
                            database,
                            pacer,
                            progress,
                            max_hours=schedule["max_hours"],
                            preparation_cache=preparation_cache,
                        )
                    )
                    # Drain retired versions, not one chunk per publication:
                    # busy-hour rewrites can retire thousands of facts at once.
                    # Each indexed chunk commits before the duty-cycle sleep.
                    from . import rollup_dependencies

                    rollup_dependencies.cleanup(db)
                    cleanup_cpu = time.thread_time()
                    for _ in range(128):
                        if time.thread_time() - cleanup_cpu >= 0.1:
                            break
                        if not db.execute("SELECT 1 FROM project_rollup_gc LIMIT 1").fetchone():
                            break
                        gc(db)
                        pacer(force=True)

                except InterruptedError:
                    break
                except Exception as error:
                    with db:
                        put(db, building=0, error=type(error).__name__)
                    if update:
                        update(dict(status(db)))
                if time.monotonic() - last_checkpoint >= 30:
                    last_checkpoint = time.monotonic()
                    try:
                        checkpoint = store.passive_checkpoint(db)
                    except Exception as error:
                        checkpoint = dict(state="error", error=type(error).__name__)
                if update:
                    update(
                        dict(
                            status(db),
                            checkpoint=checkpoint,
                            worker_cpu_seconds=time.thread_time() - worker_cpu_start,
                            preparation_cache=preparation_cache.stats(),
                        )
                    )
                try:
                    pacer(force=True)
                except InterruptedError:
                    break
                # Fresh pacing window prevents idle credits permitting a burst.
                delay = (
                    0.25
                    if result.get("state") == "waiting_for_canonical_winners"
                    else worker_schedule(db)["delay"]
                )
                stop.wait(delay)
                pacer = Pacer(stop, allowance)
    finally:
        lock.close()
