"""SQLite store + ingest of collector NDJSON."""
import json, os, sqlite3, time

DB_DIR = os.environ.get("USAGE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share", "usage")
DB_PATH = os.path.join(DB_DIR, "usage.db")

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
    "llm": ["uid", "ts", "tool", "session", "project", "model", "kind", "client", "fresh_in", "cache_read", "cache_write", "out", "reasoning"],
    "edit": ["uid", "ts", "tool", "session", "project", "model", "doc", "added", "removed"],
    "prompt": ["uid", "ts", "tool", "session", "project", "model", "chars", "words", "est_tokens"],
    "commit": ["uid", "ts", "repo", "project", "author", "added_code", "removed_code", "added_doc", "removed_doc", "files", "doc_files", "agent", "model"],
}
TABLE = {"llm": "llm", "edit": "edits", "prompt": "prompts", "commit": "commits"}
PERF_COLS = ["ts", "cpu", "mem_pct", "mem_mb", "disk_r", "disk_w", "net_rx", "net_tx", "load1", "idle_s"]


def connect(path=DB_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    have = {r[1] for r in db.execute("PRAGMA table_info(llm)")}
    for col, ddl in (("requester", "TEXT DEFAULT ''"), ("dup", "INT DEFAULT 0")):
        if col not in have:
            db.execute("ALTER TABLE llm ADD COLUMN %s %s" % (col, ddl))
    db.commit()
    return db


def ingest_gateway(db, rows, machine):
    """Upsert gateway usage rows, then mark rows that exactly match a local Claude/Codex call (same output
    tokens + cache reads within 2 minutes, coding-agent traffic only) as duplicates so totals don't double-count."""
    cols = ["uid", "ts", "tool", "session", "project", "model", "kind", "client", "fresh_in", "cache_read", "cache_write", "out", "reasoning", "requester"]
    with db:
        db.executemany("INSERT OR REPLACE INTO llm(machine,%s,dup) VALUES(?,%s,0)" % (",".join(cols), ",".join("?" * len(cols))),
                       [(machine,) + tuple(r[c] for c in cols) for r in rows])
        db.execute("INSERT INTO machines(machine,os,ncpu,last_sync) VALUES(?,?,?,?) ON CONFLICT(machine) DO UPDATE SET last_sync=excluded.last_sync",
                   (machine, "databricks", None, int(time.time())))
        local = {}
        for ts, out, cr in db.execute("SELECT ts,out,cache_read FROM llm WHERE machine<>? AND tool IN ('claude','codex')", (machine,)):
            local.setdefault((out, cr), []).append(ts)
        dups = [(r["uid"],) for r in rows if r["client"].startswith(("claude", "codex")) and
                any(abs(t - r["ts"]) <= 120 for t in local.get((r["out"], r["cache_read"]), ()))]
        db.executemany("UPDATE llm SET dup=1 WHERE machine=? AND uid=?", [(machine, u[0]) for u in dups])
    return len(dups)


def ingest(db, lines, machine=None):
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
            batch.setdefault(t, []).append(d)
        elif t == "perf":  # own sampler -> canonical metrics
            for m, v in (("cpu", d.get("cpu")), ("mem_pct", d.get("mem_pct")), ("mem_mb", d.get("mem_mb")), ("load1", d.get("load1")),
                         ("disk_r", None if d.get("disk_r") is None else d["disk_r"] / 1024.0),
                         ("disk_w", None if d.get("disk_w") is None else d["disk_w"] / 1024.0),
                         ("net_down", None if d.get("net_rx") is None else d["net_rx"] / 1024.0),
                         ("net_up", None if d.get("net_tx") is None else d["net_tx"] / 1024.0),
                         ("active", None if d.get("idle_s") is None else (1.0 if d["idle_s"] < 120 else 0.0))):
                if v is not None:
                    batch.setdefault("metric", []).append({"src": "sampler", "m": m, "ts": d["ts"], "iv": 30, "v": v, "pk": None})
        elif t == "metric":
            batch.setdefault("metric", []).append(d)
        elif t == "note":
            batch.setdefault("note", []).append(d)
    machine = machine or "local"
    with db:
        for t, rows in batch.items():
            if t == "note":
                db.executemany("INSERT OR REPLACE INTO notes(machine,k,ts,text) VALUES(?,?,?,?)",
                               [(machine, r["k"], r["ts"], r["text"]) for r in rows])
            elif t == "metric":
                db.executemany("INSERT OR REPLACE INTO metrics(machine,src,m,ts,iv,v,pk) VALUES(?,?,?,?,?,?,?)",
                               [(machine, r["src"], r["m"], r["ts"], r["iv"], r["v"], r.get("pk")) for r in rows])
            else:
                cols = COLS[t]
                sql = "INSERT INTO %s(machine,%s) VALUES(?,%s)" % (TABLE[t], ",".join(cols), ",".join("?" * len(cols)))
                if t == "llm":  # streamed duplicates: keep the most complete (highest output) record
                    sql += " ON CONFLICT(machine,uid) DO UPDATE SET " + ",".join("%s=excluded.%s" % (c, c) for c in cols[1:]) + " WHERE excluded.out>=llm.out"
                else:
                    sql = sql.replace("INSERT INTO", "INSERT OR REPLACE INTO")
                db.executemany(sql, [(machine,) + tuple(r.get(c) for c in cols) for r in rows])
            counts[t] = len(rows)
        db.execute("INSERT INTO machines(machine,os,ncpu,last_sync) VALUES(?,?,?,?) ON CONFLICT(machine) DO UPDATE SET os=excluded.os,ncpu=excluded.ncpu,last_sync=excluded.last_sync",
                   (machine, info.get("os"), info.get("ncpu"), int(time.time())))
    return machine, counts


def last_sync(db, machine):
    r = db.execute("SELECT last_sync FROM machines WHERE machine=?", (machine,)).fetchone()
    return r[0] if r else None


def max_perf_ts(db, machine):
    """Resume point. Minus an hour so ring-buffer tiers that were still filling get refreshed."""
    r = db.execute("SELECT MAX(ts) FROM metrics WHERE machine=?", (machine,)).fetchone()
    return max(0, (r[0] or 0) - 3600)


def dataset(db, since_ts=0):
    """Hourly rollups, columnar, for the dashboard. hour = ts // 3600 (UTC)."""
    q = lambda sql, *a: [list(r) for r in db.execute(sql, a)]
    return {
        "generated": int(time.time()),
        "machines": [r[0] for r in db.execute("SELECT machine FROM machines ORDER BY 1")],
        "llm_cols": ["hour", "machine", "tool", "project", "model", "kind", "fresh_in", "cache_read", "cache_write", "out", "reasoning", "calls", "requester"],
        "llm": q("""SELECT ts/3600,machine,tool,project,model,kind,SUM(fresh_in),SUM(cache_read),SUM(cache_write),SUM(out),SUM(reasoning),COUNT(*),requester
                    FROM llm WHERE ts>=? AND dup=0 GROUP BY 1,2,3,4,5,6,13""", since_ts),
        "me": my_identities(),
        "edit_cols": ["hour", "machine", "tool", "project", "model", "doc", "added", "removed", "n"],
        "edits": q("""SELECT ts/3600,machine,tool,project,model,doc,SUM(added),SUM(removed),COUNT(*)
                      FROM edits WHERE ts>=? GROUP BY 1,2,3,4,5,6""", since_ts),
        "prompt_cols": ["hour", "machine", "tool", "project", "model", "n", "chars", "words", "est_tokens"],
        "prompts": q("""SELECT ts/3600,machine,tool,project,model,COUNT(*),SUM(chars),SUM(words),SUM(est_tokens)
                        FROM prompts WHERE ts>=? GROUP BY 1,2,3,4,5""", since_ts),
        "commit_cols": ["hour", "machine", "project", "agent", "model", "n", "added_code", "removed_code", "added_doc", "removed_doc", "files", "doc_files"],
        "commits": q("""SELECT ts/3600,machine,project,agent,model,COUNT(*),SUM(added_code),SUM(removed_code),SUM(added_doc),SUM(removed_doc),SUM(files),SUM(doc_files)
                        FROM commits WHERE ts>=? GROUP BY 1,2,3,4,5""", since_ts),
        "metric_cols": ["hour", "machine", "m", "avg", "max", "src"],
        "metrics": rollup_metrics(db, since_ts),
        "note_cols": ["hour", "machine", "text"],
        "notes": q("SELECT ts/3600,machine,text FROM notes WHERE k='top_apps' AND ts>=? GROUP BY 1,2 HAVING ts=MAX(ts)", since_ts),
    }


# When several sources report the same canonical metric, take the first that has data for that machine/metric.
PRIORITY = ["endpointcsv", "istat", "sampler"]
# per-metric overrides (iStat has real throughput; own sampler only has idle)
PRIORITY_BY = {"disk_r": ["istat", "sampler"], "disk_w": ["istat", "sampler"], "net_up": ["istat", "sampler"],
               "net_down": ["istat", "sampler"], "load1": ["istat", "endpointcsv", "sampler"]}


def rollup_metrics(db, since_ts=0):
    """Hourly avg/max per (machine, metric). Ring-buffer tiers overlap, so per hour use the finest tier
    that covers >=80% of the hour, else the best-covered one."""
    import collections
    cells = collections.defaultdict(lambda: collections.defaultdict(list))  # (mach,src,m,hour) -> iv -> [(v,pk)]
    for mach, src, m, ts, iv, v, pk in db.execute("SELECT machine,src,m,ts,iv,v,pk FROM metrics WHERE ts>=?", (since_ts,)):
        cells[(mach, src, m, ts // 3600)][iv].append((v, pk if pk is not None else v))
    best = {}  # (mach,m,hour) -> {src: (avg,max)}
    for (mach, src, m, hour), tiers in cells.items():
        cov = lambda iv: min(1.0, len(tiers[iv]) * iv / 3600.0)
        ok = [iv for iv in tiers if cov(iv) >= 0.8]
        iv = min(ok) if ok else max(tiers, key=cov)
        vals = tiers[iv]
        best.setdefault((mach, m, hour), {})[src] = (sum(v for v, _ in vals) / len(vals), max(p for _, p in vals))
    out = []
    for (mach, m, hour), by in best.items():
        for src in PRIORITY_BY.get(m, PRIORITY):
            if src in by:
                out.append([hour, mach, m, round(by[src][0], 3), round(by[src][1], 3), src])
                break
    return out


def my_identities():
    """Emails that count as 'me' (git identity + Databricks gateway user)."""
    import subprocess
    me = []
    try:
        me.append(subprocess.run(["git", "config", "--global", "user.email"], capture_output=True, text=True).stdout.strip())
    except OSError:
        pass
    try:
        with open(os.path.join(os.path.expanduser("~"), ".config", "usage", "gateway.json")) as f:
            me.append(json.load(f).get("me", ""))
    except (OSError, ValueError):
        pass
    return [m for m in dict.fromkeys(me) if m]
