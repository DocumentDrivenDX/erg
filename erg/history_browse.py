"""Transactional invalidation and bounded background tiles for session browsing.

Tiles contain allocated metadata only. Readers never write or accept a dirty,
wrong-policy, partial-hour or too-new tile in place of raw physical evidence.
"""

import contextlib
import json
import time

from . import project_scope, projects, report_scope, store

VERSION = 1


def initialize(db):
    db.executescript("""
    CREATE TABLE IF NOT EXISTS history_browse_dirty(hour INTEGER PRIMARY KEY,serial INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS history_browse_heads(hour INTEGER PRIMARY KEY,revision INTEGER,upper_seq INTEGER,data TEXT);
    CREATE TABLE IF NOT EXISTS history_browse_context(machine TEXT,tool TEXT,session TEXT,serial INTEGER,PRIMARY KEY(machine,tool,session));
    CREATE TABLE IF NOT EXISTS history_browse_dependencies(hour INTEGER,machine TEXT,tool TEXT,session TEXT,PRIMARY KEY(hour,machine,tool,session));
    CREATE INDEX IF NOT EXISTS history_browse_dependency_session ON history_browse_dependencies(machine,tool,session,hour);
    """)

    def queue(select):
        return (
            "INSERT INTO history_browse_dirty "
            + select
            + " ON CONFLICT(hour) DO UPDATE SET serial=serial+1; "
        )

    def trigger(name, event, body):
        sql = "CREATE TRIGGER " + name + " " + event + " BEGIN " + body + " END"
        old = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,)
        ).fetchone()
        if old and old[0] != sql:
            db.execute("DROP TRIGGER " + name)
            old = None
        if not old:
            db.execute(sql)

    columns = ("machine", "tool", "session", "project", "ts", "expired_at", "seq")
    for event, aliases in (("INSERT", ("NEW",)), ("UPDATE", ("OLD", "NEW")), ("DELETE", ("OLD",))):
        when = (
            " WHEN " + " OR ".join("OLD." + c + " IS NOT NEW." + c for c in columns)
            if event == "UPDATE"
            else ""
        )
        trigger(
            "erg_history_browse_events_" + event.lower(),
            "AFTER " + event + " ON history_events" + when,
            "".join(queue("SELECT CAST(" + a + ".ts/3600 AS INT),1 WHERE 1") for a in aliases),
        )
    trigger(
        "erg_history_browse_replace",
        "BEFORE INSERT ON history_events",
        queue(
            "SELECT CAST(ts/3600 AS INT),1 FROM history_events WHERE event_id=NEW.event_id AND (machine IS NOT NEW.machine OR tool IS NOT NEW.tool OR session IS NOT NEW.session OR project IS NOT NEW.project OR ts IS NOT NEW.ts)"
        ),
    )
    for table, extra in (
        ("project_contexts", ("cwd", "repo")),
        ("work_contexts", ("repo", "worktree")),
    ):
        cols = ("machine", "tool", "session", "ts") + extra
        identity = "source_uid" if table == "project_contexts" else "uid"
        prior = "prior.machine=NEW.machine AND prior." + identity + "=NEW." + identity
        different = (
            " WHEN NOT EXISTS(SELECT 1 FROM "
            + table
            + " prior WHERE "
            + prior
            + " AND "
            + " AND ".join("prior." + c + " IS NEW." + c for c in cols)
            + ")"
        )
        # BEFORE INSERT also sees the displaced row of INSERT OR REPLACE, whose
        # implicit delete does not invoke delete triggers with SQLite defaults.
        old = (
            "INSERT INTO history_browse_context SELECT prior.machine,prior.tool,prior.session,1 FROM "
            + table
            + " prior WHERE "
            + prior
            + " ON CONFLICT(machine,tool,session) DO UPDATE SET serial=serial+1; "
        )
        old += queue(
            "SELECT d.hour,1 FROM history_browse_dependencies d JOIN "
            + table
            + " prior ON "
            + prior
            + " WHERE d.machine=prior.machine AND d.tool=prior.tool AND d.session=prior.session AND (d.hour+1)*3600>prior.ts"
        )
        for event, aliases in (
            ("INSERT", ("NEW",)),
            ("UPDATE", ("OLD", "NEW")),
            ("DELETE", ("OLD",)),
        ):
            when = (
                different
                if event == "INSERT"
                else " WHEN " + " OR ".join("OLD." + c + " IS NOT NEW." + c for c in cols)
                if event == "UPDATE"
                else ""
            )
            body = old if event == "INSERT" else ""
            for a in aliases:
                body += (
                    "INSERT INTO history_browse_context VALUES("
                    + ",".join(a + "." + c for c in ("machine", "tool", "session"))
                    + ",1) ON CONFLICT(machine,tool,session) DO UPDATE SET serial=serial+1; "
                )
                body += queue(
                    "SELECT hour,1 FROM history_browse_dependencies WHERE machine="
                    + a
                    + ".machine AND tool="
                    + a
                    + ".tool AND session="
                    + a
                    + ".session AND (hour+1)*3600>"
                    + a
                    + ".ts"
                )
            trigger(
                "erg_history_browse_" + table + "_" + event.lower(),
                ("BEFORE" if event == "INSERT" else "AFTER") + " " + event + " ON " + table + when,
                body,
            )
    db.execute("INSERT OR IGNORE INTO history_config VALUES('browse_links_version','0')")
    # Server/native relinking is rare; invalidate tiles without scanning raw bodies.
    for event in ("INSERT", "UPDATE", "DELETE"):
        trigger(
            "erg_history_browse_links_" + event.lower(),
            "AFTER " + event + " ON history_links",
            queue("SELECT hour,1 FROM history_browse_heads WHERE 1")
            + "UPDATE history_config SET value=CAST(value AS INT)+1 WHERE key='browse_links_version';",
        )
    if not db.execute("SELECT 1 FROM history_config WHERE key='browse_seed_v1'").fetchone():
        db.execute(queue("SELECT DISTINCT CAST(ts/3600 AS INT),1 FROM history_events WHERE 1"))
        db.execute("INSERT INTO history_config VALUES('browse_seed_v1','1')")


def context_versions(db):
    return {
        (m, t, s): n
        for m, t, s, n in db.execute(
            "SELECT machine,tool,session,serial FROM history_browse_context"
        )
    }


def status(db):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='history_browse_heads'").fetchone():
        return dict(available=False)
    revision = projects.snapshot(db)["revision"]
    pending = {
        r[0]
        for r in db.execute(
            "SELECT hour FROM history_browse_dirty UNION SELECT hour FROM history_browse_heads WHERE revision<>?",
            (revision,),
        )
    }
    return dict(
        pending_closed_hours=sum(h < int(time.time()) // 3600 for h in pending),
        available=True,
        ready_hours=db.execute(
            "SELECT count(*) FROM history_browse_heads WHERE revision=? AND hour NOT IN (SELECT hour FROM history_browse_dirty)",
            (revision,),
        ).fetchone()[0],
        pending_hours=db.execute(
            "SELECT count(*) FROM (SELECT hour FROM history_browse_dirty UNION SELECT hour FROM history_browse_heads WHERE revision<>?)",
            (revision,),
        ).fetchone()[0],
        version=VERSION,
    )


def build(source, hour, revision):
    from . import history

    selection = report_scope.Selection(dict(start=hour * 3600, end=(hour + 1) * 3600))
    upper = source.execute("SELECT coalesce(max(seq),0) FROM history_events").fetchone()[0]
    links = {
        r[0]: (r[1], r[2])
        for r in source.execute("SELECT local_session,tool,session FROM history_links")
    }
    compact = {}
    dependencies = set()
    # All raw identities matter, including receipt sessions before they are linked.
    for m, t, s in source.execute(
        "SELECT DISTINCT machine,tool,session FROM history_events WHERE ts>=? AND ts<?",
        (hour * 3600, (hour + 1) * 3600),
    ):
        dependencies.add((m, t, s))
        if t == "local" and s in links:
            dependencies.add((m,) + links[s])
    hierarchy = project_scope.Scope(source, selection.values["end"], identities=dependencies)
    assert hierarchy.config["revision"] == revision
    for row, allocation in history._allocated_metadata(source, selection, upper, hierarchy, links):
        # Scope's folder registry keeps the first matching exemplar. Retain the
        # per-fact context so narrower identity filters select their own exemplar.
        context = project_scope.context_at(hierarchy.contexts, row)
        folder = dict(
            allocation["folder"],
            observed_project=row.get("project") or "",
            cwd=context.get("cwd") or None,
            repository=context.get("repo") or None,
            project_keys=sorted(p["project_key"] for p in allocation["allocations"]),
        )
        allocation = dict(allocation, folder=folder)
        key = (
            row["machine"],
            row["tool"],
            row["session"],
            row.get("project"),
            folder["folder_key"],
            folder["cwd"],
            folder["repository"],
            tuple((p["project_key"], p["units"]) for p in allocation["allocations"]),
        )
        item = compact.get(key)
        if item is None:
            item = dict(row, allocation=allocation)
            compact[key] = item
        else:
            item["ts"] = min(item["ts"], row["ts"])
            item["last_ts"] = max(item["last_ts"], row["last_ts"])
            item["count"] += row["count"]
            item["expired"] += row["expired"]
            item["seq"] = max(item["seq"], row["seq"])
    return dict(
        version=VERSION, rows=list(compact.values()), dependencies=sorted(dependencies)
    ), upper


def run(database, max_hours=1, tick=None):
    """Build a finite batch off the request path; release writer between hours."""

    processed = 0
    with contextlib.closing(
        store.connect(database, initialize=False, autocheckpoint=False)
    ) as writer:
        for unused in range(max(1, min(int(max_hours), 16))):
            with contextlib.closing(store.open_readonly(database)) as source:
                source.execute("BEGIN")
                revision = projects.snapshot(source)["revision"]
                # Alternate oldest and newest pending work by completed tile count.
                candidates = source.execute(
                    "SELECT hour FROM history_browse_dirty UNION SELECT hour FROM history_browse_heads WHERE revision<>? ORDER BY hour",
                    (revision,),
                ).fetchall()
                if not candidates:
                    break
                turn = source.execute(
                    "SELECT value FROM history_config WHERE key='browse_turn_v1'"
                ).fetchone()
                turn = int(turn[0]) if turn else 0
                hour = candidates[0 if turn % 3 == 2 else -1][0]
                link_version = source.execute(
                    "SELECT value FROM history_config WHERE key='browse_links_version'"
                ).fetchone()[0]
                row = source.execute(
                    "SELECT serial FROM history_browse_dirty WHERE hour=?", (hour,)
                ).fetchone()
                serial = row[0] if row else None
                versions = context_versions(source)
                data, upper = build(source, hour, revision)
                encoded = json.dumps(data, separators=(",", ":"))
            with writer:
                writer.execute("BEGIN IMMEDIATE")
                current = writer.execute(
                    "SELECT serial FROM history_browse_dirty WHERE hour=?", (hour,)
                ).fetchone()
                writer.execute(
                    "INSERT INTO history_config VALUES('browse_turn_v1',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(turn + 1),),
                )
                unchanged = (
                    (current[0] if current else None) == serial
                    and projects.snapshot(writer)["revision"] == revision
                    and writer.execute(
                        "SELECT value FROM history_config WHERE key='browse_links_version'"
                    ).fetchone()[0]
                    == link_version
                )
                if unchanged:
                    for key in data["dependencies"]:
                        value = writer.execute(
                            "SELECT serial FROM history_browse_context WHERE machine=? AND tool=? AND session=?",
                            key,
                        ).fetchone()
                        if (value[0] if value else 0) != versions.get(tuple(key), 0):
                            unchanged = False
                            break
                if unchanged:
                    writer.execute(
                        "INSERT OR REPLACE INTO history_browse_heads VALUES(?,?,?,?)",
                        (hour, revision, upper, encoded),
                    )
                    writer.execute("DELETE FROM history_browse_dependencies WHERE hour=?", (hour,))
                    writer.executemany(
                        "INSERT INTO history_browse_dependencies VALUES(?,?,?,?)",
                        [(hour,) + tuple(k) for k in data["dependencies"]],
                    )
                    writer.execute("DELETE FROM history_browse_dirty WHERE hour=?", (hour,))
                    processed += 1
            if tick:
                tick()
        return dict(status(writer), processed=processed)


def metadata(db, selection, upper, hierarchy, links, stats):
    """Mix valid complete tiles with exact raw fallback inside one read snapshot."""
    from . import history

    start, end = selection.values["start"], selection.values["end"]
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='history_browse_heads'").fetchone():
        stats["raw_hours"] = None
        yield from history._allocated_metadata(db, selection, upper, hierarchy, links)
        return
    heads = {
        h: (r, u, data)
        for h, r, u, data in db.execute(
            "SELECT hour,revision,upper_seq,data FROM history_browse_heads WHERE hour>=? AND hour<=?",
            (start // 3600, (end - 1) // 3600),
        )
    }
    dirty = {
        h
        for (h,) in db.execute(
            "SELECT hour FROM history_browse_dirty WHERE hour>=? AND hour<=?",
            (start // 3600, (end - 1) // 3600),
        )
    }
    # Enumerate occupied hours from the time index, avoiding a calendar-sized loop.
    hours = [
        h
        for (h,) in db.execute(
            "SELECT DISTINCT CAST(ts/3600 AS INT) FROM history_events WHERE ts>=? AND ts<? AND seq<=?",
            (start, end, upper),
        )
    ]
    for hour in hours:
        head = heads.get(hour)
        data = None
        if (
            head
            and hour not in dirty
            and head[0] == hierarchy.config["revision"]
            and head[1] <= upper
            and start <= hour * 3600
            and end >= (hour + 1) * 3600
        ):
            try:
                candidate = json.loads(head[2])
                if (
                    isinstance(candidate, dict)
                    and candidate.get("version") == VERSION
                    and isinstance(candidate.get("rows"), list)
                ):
                    data = candidate
            except (ValueError, TypeError):
                pass
        if data is None:
            stats["raw_hours"] += 1
            values = dict(
                selection.values, start=max(start, hour * 3600), end=min(end, (hour + 1) * 3600)
            )
            yield from history._allocated_metadata(
                db, report_scope.Selection(values), upper, hierarchy, links
            )
        else:
            stats["indexed_hours"] += 1
            for item in data["rows"]:
                if not selection.accepts(item):
                    continue
                allocation = item["allocation"]
                folder = allocation["folder"]
                key = folder["folder_key"]
                portions = {p["project_key"] for p in allocation["allocations"]}
                existing = hierarchy.folders.setdefault(key, dict(folder, project_keys=[]))
                existing["project_keys"] = sorted(set(existing["project_keys"]) | portions)
                target = selection.values["folder_key"]
                if target and (
                    folder["kind"] != "unobserved" if target == "@unobserved" else key != target
                ):
                    continue
                if selection.values["project_key"] and not any(
                    p["project_key"] == selection.values["project_key"] and p["units"]
                    for p in allocation["allocations"]
                ):
                    continue
                yield item, allocation
