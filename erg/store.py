"""SQLite store + ingest of collector NDJSON."""

import json
import os
import sqlite3
import time
from contextlib import closing, contextmanager, nullcontext

from . import titles, unified, work

DB_DIR = os.environ.get("ERG_HOME") or os.path.join(
    os.path.expanduser("~"), ".local", "share", "erg"
)
DB_PATH = os.environ.get("ERG_DATABASE") or os.path.join(DB_DIR, "erg-v2.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS llm(machine TEXT, uid TEXT, ts INT, tool TEXT, session TEXT, project TEXT, model TEXT,
  kind TEXT, client TEXT, fresh_in INT, cache_read INT, cache_write INT, out INT, reasoning INT, PRIMARY KEY(machine, uid));
CREATE TABLE IF NOT EXISTS edits(machine TEXT, uid TEXT, ts INT, tool TEXT, session TEXT, project TEXT, model TEXT,
  doc INT, added INT, removed INT, PRIMARY KEY(machine, uid));
CREATE TABLE IF NOT EXISTS prompts(machine TEXT, uid TEXT, ts INT, tool TEXT, session TEXT, project TEXT, model TEXT,
  chars INT, words INT, est_tokens INT, PRIMARY KEY(machine, uid));
CREATE TABLE IF NOT EXISTS commits(machine TEXT, uid TEXT, ts INT, repo TEXT, project TEXT, author TEXT,
  added_code INT, removed_code INT, added_doc INT, removed_doc INT, files INT, doc_files INT, agent TEXT, model TEXT,
  PRIMARY KEY(machine, uid));
CREATE TABLE IF NOT EXISTS perf(machine TEXT, ts INT, cpu REAL, mem_pct REAL, mem_mb INT, disk_r INT, disk_w INT,
  net_rx INT, net_tx INT, load1 REAL, idle_s REAL, PRIMARY KEY(machine, ts));
CREATE TABLE IF NOT EXISTS metrics(machine TEXT, src TEXT, m TEXT, ts INT, iv INT, v REAL, pk REAL, PRIMARY KEY(machine, src, m, ts, iv));
CREATE INDEX IF NOT EXISTS metrics_ts ON metrics(ts);
CREATE TABLE IF NOT EXISTS notes(machine TEXT, k TEXT, ts INT, text TEXT, PRIMARY KEY(machine, k, ts));
CREATE TABLE IF NOT EXISTS machines(machine TEXT PRIMARY KEY, os TEXT, ncpu INT, last_sync INT, last_perf INT);
CREATE INDEX IF NOT EXISTS llm_ts ON llm(ts); CREATE INDEX IF NOT EXISTS edits_ts ON edits(ts);
CREATE INDEX IF NOT EXISTS prompts_ts ON prompts(ts); CREATE INDEX IF NOT EXISTS commits_ts ON commits(ts);
"""

COLS = {
    "llm": [
        "uid",
        "ts",
        "tool",
        "session",
        "project",
        "model",
        "kind",
        "client",
        "fresh_in",
        "cache_read",
        "cache_write",
        "out",
        "reasoning",
    ],
    "edit": ["uid", "ts", "tool", "session", "project", "model", "doc", "added", "removed"],
    "prompt": [
        "uid",
        "ts",
        "tool",
        "session",
        "project",
        "model",
        "chars",
        "words",
        "est_tokens",
        "origin",
        "provenance",
        "source_uid",
    ],
    "commit": [
        "uid",
        "ts",
        "repo",
        "project",
        "author",
        "added_code",
        "removed_code",
        "added_doc",
        "removed_doc",
        "files",
        "doc_files",
        "agent",
        "model",
    ],
}
TABLE = {"llm": "llm", "edit": "edits", "prompt": "prompts", "commit": "commits"}
PERF_COLS = [
    "ts",
    "cpu",
    "mem_pct",
    "mem_mb",
    "disk_r",
    "disk_w",
    "net_rx",
    "net_tx",
    "load1",
    "idle_s",
]


SESSION_COLS = "uid ts last_ts tool session project kind parent_session forked_from surface agent_version provenance provider usage_status orchestrator conversation_id external_session summary_in summary_out summary_cache_read summary_cache_write".split()
SOURCE_COLS = "uid ts tool files errors installed status linked missing detail".split()
COLS.update(session=SESSION_COLS, source=SOURCE_COLS)
TABLE.update(session="sessions", source="sources")
COLS.update({kind: fields.split() for kind, fields in work.EVENTS.items()})
TABLE.update(work.TABLES)


@contextmanager
def schema_lock(path):
    # Separate from capture transactions; only schema initialization holds this.
    import fcntl

    fd = os.open(path + ".schema.lock", os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def connect(path=DB_PATH, initialize=True, autocheckpoint=True):
    path = os.fspath(path)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    with schema_lock(path) if initialize else nullcontext():
        existed = os.path.exists(path)
        db = sqlite3.connect(path, timeout=30)
        if not existed:
            os.chmod(path, 0o600)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=30000")
        if not autocheckpoint:
            db.execute("PRAGMA wal_autocheckpoint=0")
            # Deferred checkpoints must not defer commit durability.
            db.execute("PRAGMA synchronous=FULL")
        try:
            return initialize_db(db) if initialize else db
        except Exception:
            db.close()
            raise


def passive_checkpoint(db):
    """Maintenance owns checkpoint CPU; commits retain normal FULL durability.

    PASSIVE does not wait for readers/writers. Busy readers may retain WAL pages;
    the next paced maintenance attempt can advance after their snapshot closes.
    """
    if db.in_transaction:
        raise ValueError("checkpoint requires an idle connection")
    started = time.thread_time()
    timeout = db.execute("PRAGMA busy_timeout").fetchone()[0]
    try:
        db.execute("PRAGMA busy_timeout=0")
        busy, frames, done = db.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        return dict(
            state="busy" if busy else "ready",
            frames=frames,
            checkpointed=done,
            cpu_seconds=time.thread_time() - started,
        )
    except sqlite3.OperationalError as error:
        if not any(v in str(error).lower() for v in ("locked", "busy")):
            raise
        return dict(state="busy", cpu_seconds=time.thread_time() - started)
    finally:
        db.execute("PRAGMA busy_timeout=" + str(timeout))


def initialize_db(db):
    """Initialize an existing connection, including isolated in-memory audits."""
    db.executescript(SCHEMA)
    for table, cols in (("sessions", SESSION_COLS), ("sources", SOURCE_COLS)):
        numeric = {
            "ts",
            "last_ts",
            "files",
            "errors",
            "installed",
            "linked",
            "missing",
            "summary_in",
            "summary_out",
            "summary_cache_read",
            "summary_cache_write",
        }
        db.execute(
            "CREATE TABLE IF NOT EXISTS "
            + table
            + "(machine TEXT,"
            + ",".join(c + (" INT" if c in numeric else " TEXT") for c in cols)
            + ",PRIMARY KEY(machine,uid))"
        )
        have = {r[1] for r in db.execute("PRAGMA table_info(" + table + ")")}
        for col in cols:
            if col not in have:
                db.execute(
                    "ALTER TABLE "
                    + table
                    + " ADD COLUMN "
                    + col
                    + (" INT" if col in numeric else " TEXT")
                )
    db.execute("CREATE VIEW IF NOT EXISTS report_sessions AS " + unified.SESSIONS_SELECT)
    db.execute("CREATE INDEX IF NOT EXISTS sessions_last_activity ON sessions(machine,last_ts)")
    unified.initialize(db)
    work.initialize(db)
    from . import history

    history.initialize(db)
    from . import quota_projection

    quota_projection.initialize(db)
    from . import delivery_journal

    delivery_journal.initialize(db)
    from . import work_projection

    work_projection.initialize(db)
    titles.initialize(db)
    from . import projects

    projects.initialize(db)
    from . import history_browse

    history_browse.initialize(db)
    have_prompts = {r[1] for r in db.execute("PRAGMA table_info(prompts)")}
    for col in ("origin", "provenance", "source_uid"):
        if col not in have_prompts:
            db.execute("ALTER TABLE prompts ADD COLUMN " + col + " TEXT")
    db.execute("CREATE VIEW IF NOT EXISTS input_messages AS " + work.INPUTS_SELECT)
    have = {r[1] for r in db.execute("PRAGMA table_info(llm)")}
    for col, ddl in (
        ("requester", "TEXT DEFAULT ''"),
        ("dup", "INT DEFAULT 0"),
        ("request_id", "TEXT DEFAULT ''"),
        ("response_id", "TEXT DEFAULT ''"),
    ):
        if col not in have:
            db.execute("ALTER TABLE llm ADD COLUMN %s %s" % (col, ddl))
    extra = {
        "usage_kind": "TEXT",
        "provider": "TEXT",
        "reported_cost_usd": "REAL",
        "reported_cost_source": "TEXT",
        "cache_write_5m": "INT",
        "cache_write_1h": "INT",
    }
    for col, ddl in extra.items():
        if col not in have:
            db.execute("ALTER TABLE llm ADD COLUMN " + col + " " + ddl)
    COLS["llm"] = list(dict.fromkeys(COLS["llm"] + ["request_id", "response_id"] + list(extra)))
    db.execute("CREATE INDEX IF NOT EXISTS llm_request_id ON llm(request_id)")
    db.execute(
        "CREATE INDEX IF NOT EXISTS llm_response_identity ON llm(machine,response_id,model,tool,session) WHERE response_id<>''"
    )
    previous = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='view' AND name='calls'"
    ).fetchone()
    if previous and previous[0].split(" AS ", 1)[-1] != unified.CALLS_SELECT:
        db.execute("DROP VIEW calls")  # Derived projection only; source observations stay intact.
    db.execute("CREATE VIEW IF NOT EXISTS calls AS " + unified.CALLS_SELECT)
    from . import report_sqlite

    report_sqlite.initialize(db)
    db.execute("CREATE VIEW IF NOT EXISTS work_commits AS " + unified.WORK_SELECT)
    from . import metric_rollups

    metric_rollups.initialize(db)
    from . import call_projection

    call_projection.safe_initialize(db)
    db.commit()
    return db


def ingest_gateway(db, rows, machine):
    """Preserve evidence; only unambiguous shared request identities reconcile."""
    cols = COLS["llm"] + ["requester"]
    with db:
        db.executemany(
            "INSERT OR REPLACE INTO llm(machine,%s) VALUES(?,%s)"
            % (",".join(cols), ",".join("?" for _ in cols)),
            [
                (machine,)
                + tuple(
                    r.get(c, "") if c in ("request_id", "response_id", "requester") else r.get(c)
                    for c in cols
                )
                for r in rows
            ],
        )
        unified.record(db, machine, [dict(r, t="llm") for r in rows])
        db.execute(
            "INSERT INTO machines(machine,os,ncpu,last_sync) VALUES(?,?,?,?) ON CONFLICT(machine) DO UPDATE SET last_sync=excluded.last_sync",
            (machine, "databricks", None, int(time.time())),
        )
    return db.execute(
        "SELECT COUNT(*) FROM llm g WHERE g.tool='gateway' AND NOT EXISTS(SELECT 1 FROM calls c WHERE c.uid=g.uid AND c.machine=g.machine)"
    ).fetchone()[0]


def ingest(db, lines, machine=None, transaction=True):
    """Upsert NDJSON events. Returns (machine, counts)."""
    counts, batch, info = {}, {}, {}
    for ln in lines:
        ln = ln.strip()
        if not ln.startswith("{"):
            continue
        d = json.loads(ln)
        t = d.get("t")
        if t == "hello":
            info = d
            machine = machine or d["machine"]
        elif t in COLS:
            if t == "session":
                d.setdefault("last_ts", d["ts"])
                d.setdefault("usage_status", "unknown")
            batch.setdefault(t, []).append(d)
        elif t == "perf":  # own sampler -> canonical metrics
            for m, v in (
                ("cpu", d.get("cpu")),
                ("mem_pct", d.get("mem_pct")),
                ("mem_mb", d.get("mem_mb")),
                ("load1", d.get("load1")),
                ("disk_r", None if d.get("disk_r") is None else d["disk_r"] / 1024.0),
                ("disk_w", None if d.get("disk_w") is None else d["disk_w"] / 1024.0),
                ("net_down", None if d.get("net_rx") is None else d["net_rx"] / 1024.0),
                ("net_up", None if d.get("net_tx") is None else d["net_tx"] / 1024.0),
                (
                    "active",
                    None if d.get("idle_s") is None else (1.0 if d["idle_s"] < 120 else 0.0),
                ),
            ):
                if v is not None:
                    batch.setdefault("metric", []).append(
                        {"src": "sampler", "m": m, "ts": d["ts"], "iv": 30, "v": v, "pk": None}
                    )
        elif t == "metric":
            batch.setdefault("metric", []).append(d)
        elif t == "note":
            batch.setdefault("note", []).append(d)
    machine = machine or "local"
    with db if transaction else nullcontext():
        for t, rows in batch.items():
            if t == "note":
                db.executemany(
                    "INSERT OR REPLACE INTO notes(machine,k,ts,text) VALUES(?,?,?,?)",
                    [(machine, r["k"], r["ts"], r["text"]) for r in rows],
                )
            elif t == "metric":
                db.executemany(
                    "INSERT INTO metrics(machine,src,m,ts,iv,v,pk) VALUES(?,?,?,?,?,?,?) ON CONFLICT(machine,src,m,ts,iv) DO UPDATE SET v=excluded.v,pk=excluded.pk WHERE metrics.v IS NOT excluded.v OR metrics.pk IS NOT excluded.pk",
                    [
                        (machine, r["src"], r["m"], r["ts"], r["iv"], r["v"], r.get("pk"))
                        for r in rows
                    ],
                )
            else:
                cols = COLS[t]
                sql = "INSERT INTO %s(machine,%s) VALUES(?,%s)" % (
                    TABLE[t],
                    ",".join(cols),
                    ",".join("?" * len(cols)),
                )
                if (
                    t == "llm"
                ):  # streamed duplicates: keep the most complete (highest output) record
                    sql += (
                        " ON CONFLICT(machine,uid) DO UPDATE SET "
                        + ",".join(
                            "%s=COALESCE(excluded.%s,llm.%s)" % (c, c, c)
                            if c
                            in (
                                "provider",
                                "reported_cost_usd",
                                "reported_cost_source",
                                "cache_write_5m",
                                "cache_write_1h",
                            )
                            else "%s=excluded.%s" % (c, c)
                            for c in cols[1:]
                        )
                        + " WHERE excluded.out>=llm.out"
                    )
                elif t == "prompt":
                    sql += " ON CONFLICT(machine,uid) DO UPDATE SET " + ",".join(
                        c
                        + "="
                        + (
                            "CASE WHEN (prompts.origin IN ('human','automation') AND COALESCE(excluded.origin,'unknown')='unknown') OR (prompts.provenance IN ('codex_user_text','codex_user_event') AND COALESCE(excluded.provenance,'') NOT IN ('codex_user_text','codex_user_event')) THEN prompts."
                            + c
                            + " ELSE COALESCE(excluded."
                            + c
                            + ",prompts."
                            + c
                            + ") END"
                            if c in ("origin", "provenance")
                            else "COALESCE(excluded." + c + ",prompts." + c + ")"
                        )
                        for c in cols[1:]
                    )
                elif t == "session":
                    assignments = []
                    for c in cols[1:]:
                        value = (
                            "MIN(sessions.ts,excluded.ts)"
                            if c == "ts"
                            else "MAX(sessions.last_ts,excluded.last_ts)"
                            if c == "last_ts"
                            else "CASE WHEN sessions.usage_status='observed' OR excluded.usage_status='observed' THEN 'observed' WHEN sessions.usage_status='summary_only' OR excluded.usage_status='summary_only' THEN 'summary_only' ELSE 'unknown' END"
                            if c == "usage_status"
                            else "CASE WHEN sessions.provenance<>'omnigent_metadata' AND excluded.provenance='omnigent_metadata' THEN sessions."
                            + c
                            + " ELSE excluded."
                            + c
                            + " END"
                            if c
                            in ("project", "surface", "provenance", "provider", "agent_version")
                            else "COALESCE(NULLIF(excluded." + c + ",''),sessions." + c + ")"
                            if c
                            in (
                                "orchestrator",
                                "conversation_id",
                                "external_session",
                                "summary_in",
                                "summary_out",
                                "summary_cache_read",
                                "summary_cache_write",
                            )
                            else "excluded." + c
                        )
                        assignments.append(c + "=" + value)
                    sql += " ON CONFLICT(machine,uid) DO UPDATE SET " + ",".join(assignments)
                else:
                    sql = sql.replace("INSERT INTO", "INSERT OR REPLACE INTO")
                values = [(machine,) + tuple(r.get(c) for c in cols) for r in rows]
                if t == "llm":
                    from . import call_projection

                    call_projection.write_calls(db, sql, values)
                else:
                    db.executemany(sql, values)
            unified.record(db, machine, [dict(r, t=t) for r in rows])
            counts[t] = len(rows)
        db.execute(
            "INSERT INTO machines(machine,os,ncpu,last_sync) VALUES(?,?,?,?) ON CONFLICT(machine) DO UPDATE SET os=COALESCE(excluded.os,machines.os),ncpu=COALESCE(excluded.ncpu,machines.ncpu),last_sync=excluded.last_sync",
            (machine, info.get("os"), info.get("ncpu"), int(time.time())),
        )
    return machine, counts


def last_sync(db, machine):
    r = db.execute("SELECT last_sync FROM machines WHERE machine=?", (machine,)).fetchone()
    return r[0] if r else None


def max_perf_ts(db, machine):
    """Resume point. Minus an hour so ring-buffer tiers that were still filling get refreshed."""
    r = db.execute("SELECT MAX(ts) FROM metrics WHERE machine=?", (machine,)).fetchone()
    return max(0, (r[0] or 0) - 3600)


def dataset(db, since_ts=0, activity=True):
    """Hourly rollups, columnar, for the dashboard. hour = ts // 3600 (UTC)."""

    def q(sql, *a):
        return [list(r) for r in db.execute(sql, a)]

    activity_query = q if activity else lambda *args: []
    calls = "calls"
    if isinstance(db, sqlite3.Connection):
        from . import report_sqlite

        columns = "ts machine tool project model kind fresh_in cache_read cache_write out reasoning requester dup".split()
        calls = "(" + report_sqlite.calls_select(columns, db=db) + ")"
    data = {
        "generated": int(time.time()),
        "schema_version": unified.VERSION,
        "session_cols": [
            "machine",
            "tool",
            "session",
            "project",
            "kind",
            "first_ts",
            "last_ts",
            "parent_session",
            "surface",
            "agent_version",
            "usage_status",
            "orchestrator",
            "conversation_id",
            "external_session",
            "summary_in",
            "summary_out",
            "summary_cache_read",
            "summary_cache_write",
        ],
        "sessions": unified.session_rows(db, since_ts),
        "session_titles": titles.rows(db),
        "session_count": q(
            "SELECT COUNT(DISTINCT tool || ':' || session) FROM report_sessions WHERE last_ts>=?",
            since_ts,
        )[0][0],
        "orchestrator_counts": q(
            "SELECT machine,orchestrator,COUNT(*),SUM(CASE WHEN usage_status='observed' THEN 1 ELSE 0 END),SUM(CASE WHEN usage_status='summary_only' THEN 1 ELSE 0 END) FROM report_sessions WHERE orchestrator IS NOT NULL AND orchestrator<>'' GROUP BY machine,orchestrator"
        ),
        "capture_counts": q(
            "SELECT machine,tool,COUNT(*),SUM(CASE WHEN usage_status='observed' THEN 1 ELSE 0 END),SUM(CASE WHEN usage_status='summary_only' THEN 1 ELSE 0 END) FROM report_sessions GROUP BY machine,tool"
        ),
        "source_cols": ["machine"] + SOURCE_COLS,
        "sources": q("SELECT machine," + ",".join(SOURCE_COLS) + " FROM sources ORDER BY tool"),
        "machines": [r[0] for r in db.execute("SELECT machine FROM machines ORDER BY 1")],
        "llm_cols": [
            "hour",
            "machine",
            "tool",
            "project",
            "model",
            "kind",
            "fresh_in",
            "cache_read",
            "cache_write",
            "out",
            "reasoning",
            "calls",
            "requester",
        ],
        "llm": activity_query(
            """SELECT ts/3600,machine,tool,project,model,kind,SUM(fresh_in),SUM(cache_read),SUM(cache_write),SUM(out),SUM(reasoning),COUNT(*),requester
                    FROM {calls} WHERE ts>=? AND dup=0 GROUP BY 1,2,3,4,5,6,13""".format(
                calls=calls
            ),
            since_ts,
        ),
        "me": my_identities(),
        "edit_cols": [
            "hour",
            "machine",
            "tool",
            "project",
            "model",
            "doc",
            "added",
            "removed",
            "n",
        ],
        "edits": activity_query(
            """SELECT ts/3600,machine,tool,project,model,doc,SUM(added),SUM(removed),COUNT(*)
                      FROM edits WHERE ts>=? GROUP BY 1,2,3,4,5,6""",
            since_ts,
        ),
        "prompt_cols": [
            "hour",
            "machine",
            "tool",
            "project",
            "model",
            "n",
            "chars",
            "words",
            "est_tokens",
        ],
        "prompts": activity_query(
            """SELECT ts/3600,machine,tool,project,model,COUNT(*),SUM(chars),SUM(words),SUM(est_tokens)
                        FROM input_messages WHERE ts>=? GROUP BY 1,2,3,4,5""",
            since_ts,
        ),
        "commit_cols": [
            "hour",
            "machine",
            "project",
            "agent",
            "model",
            "n",
            "added_code",
            "removed_code",
            "added_doc",
            "removed_doc",
            "files",
            "doc_files",
        ],
        "commits": activity_query(
            """SELECT ts/3600,machine,project,agent,model,COUNT(*),SUM(added_code),SUM(removed_code),SUM(added_doc),SUM(removed_doc),SUM(files),SUM(doc_files)
                        FROM work_commits WHERE ts>=? GROUP BY 1,2,3,4,5""",
            since_ts,
        ),
        "metric_cols": ["hour", "machine", "m", "avg", "max", "src", "samples", "covered_seconds"],
        "metrics": rollup_metrics(db, since_ts),
        "note_cols": ["hour", "machine", "text"],
        "notes": q(
            "SELECT ts/3600,machine,text FROM notes WHERE k='top_apps' AND ts>=? GROUP BY 1,2 HAVING ts=MAX(ts)",
            since_ts,
        ),
    }

    return data


# When several sources report the same canonical metric, take the first that has data for that machine/metric.
PRIORITY = ["endpointcsv", "istat", "sampler"]
# per-metric overrides (iStat has real throughput; own sampler only has idle)
PRIORITY_BY = {
    "disk_r": ["istat", "sampler"],
    "disk_w": ["istat", "sampler"],
    "net_up": ["istat", "sampler"],
    "net_down": ["istat", "sampler"],
    "load1": ["istat", "endpointcsv", "sampler"],
}


def rollup_metrics(db, since_ts=0):
    """Hourly avg/max per (machine, metric). Ring-buffer tiers overlap, so per hour use the finest tier
    that covers >=80% of the hour, else the best-covered one."""
    import collections

    cells = collections.defaultdict(dict)
    from . import metric_rollups

    with closing(metric_rollups.tiers(db, since_ts)) as tiers:
        for mach, src, m, hour, iv, n, total, peak in tiers:
            cells[(mach, src, m, hour)][iv] = (n, total, peak)
    best = {}
    for (mach, src, m, hour), tiers in cells.items():

        def cov(iv):
            return min(3600, tiers[iv][0] * iv)

        ok = [iv for iv in tiers if cov(iv) >= 2880]
        iv = min(ok) if ok else max(tiers, key=cov)
        n, total, peak = tiers[iv]
        best.setdefault((mach, m, hour), {})[src] = (total / n, peak, n, cov(iv))
    out = []
    for (mach, m, hour), by in best.items():
        priority = PRIORITY_BY.get(m, PRIORITY)
        order = {src: i for i, src in enumerate(priority)}
        covering = [src for src in by if by[src][3] >= 2880]
        src = (
            min(covering, key=lambda x: order.get(x, 999))
            if covering
            else max(by, key=lambda x: (by[x][3], -order.get(x, 999)))
        )
        avg, peak, n, seconds = by[src]
        out.append([hour, mach, m, round(avg, 3), round(peak, 3), src, n, seconds])
    return out


def my_identities():
    """Emails that count as 'me' (git identity + Databricks gateway user)."""
    import subprocess

    me = []
    try:
        me.append(
            subprocess.run(
                ["git", "config", "--global", "user.email"], capture_output=True, text=True
            ).stdout.strip()
        )
    except OSError:
        pass
    try:
        with open(os.path.join(os.path.expanduser("~"), ".config", "erg", "gateway.json")) as f:
            me.append(json.load(f).get("me", ""))
    except (OSError, ValueError):
        pass
    return [m for m in dict.fromkeys(me) if m]


def open_readonly(path=DB_PATH):
    """Report reads never initialize or migrate a ledger."""
    from pathlib import Path

    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
    db.execute("PRAGMA query_only=ON")
    return db
