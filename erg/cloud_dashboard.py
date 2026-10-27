"""Incremental private cache of verified Delta items, using shared local reports.

Only Databricks delivery items populate history. Operator project policy is a
separately labelled local configuration overlay. Nothing here captures or uploads.
"""

import contextlib
import threading
import time
from pathlib import Path

from . import collect, history, projects, store
from . import delivery_protocol as wire

SCHEMA = """
CREATE TABLE IF NOT EXISTS cloud_config(id INTEGER PRIMARY KEY CHECK(id=1),identity TEXT,cursor INTEGER DEFAULT 0,last_pull REAL,error TEXT);
CREATE TABLE IF NOT EXISTS cloud_item_digests(seq INTEGER PRIMARY KEY,digest TEXT);
CREATE TABLE IF NOT EXISTS cloud_chunks(record_id TEXT,revision INTEGER,chunk INTEGER,item TEXT,PRIMARY KEY(record_id,revision,chunk));
CREATE TABLE IF NOT EXISTS cloud_heads(record_id TEXT PRIMARY KEY,revision INTEGER,digest TEXT);
"""


def path():
    return Path(collect.STATE_DIR) / "databricks-reporting" / "report.db"


def initialize(db, identity, forward_ts=None):
    db.executescript(SCHEMA)
    encoded = wire.encode(identity).decode()
    if "forward_ts" not in {r[1] for r in db.execute("PRAGMA table_info(cloud_config)")}:
        db.execute("ALTER TABLE cloud_config ADD COLUMN forward_ts INTEGER NOT NULL DEFAULT 0")
    if "metadata_cursor" not in {r[1] for r in db.execute("PRAGMA table_info(cloud_config)")}:
        db.execute("ALTER TABLE cloud_config ADD COLUMN metadata_cursor INTEGER NOT NULL DEFAULT 0")
    old = db.execute("SELECT identity FROM cloud_config WHERE id=1").fetchone()
    if old and old[0] != encoded:
        raise ValueError("cloud cache identity changed; separate cache required")
    with db:
        db.execute("INSERT OR IGNORE INTO cloud_config(id,identity) VALUES(1,?)", (encoded,))
        if forward_ts is not None:
            previous = db.execute("SELECT forward_ts FROM cloud_config WHERE id=1").fetchone()[0]
            if previous and previous != forward_ts:
                raise ValueError("cloud forward boundary changed")
            db.execute("UPDATE cloud_config SET forward_ts=? WHERE id=1", (forward_ts,))


def apply_page(db, rows, metadata=False):
    """Atomic trace prefix or sparse approved headers; identities dedupe both lanes."""
    column = "metadata_cursor" if metadata else "cursor"
    cursor, forward_ts = db.execute(
        "SELECT " + column + ",forward_ts FROM cloud_config WHERE id=1"
    ).fetchone()
    applied = 0
    with db:
        for seq, raw in rows:
            item = wire.decode(raw.encode())
            wire.validate_item(item)
            if item["delivery_seq"] != seq:
                raise ValueError("cloud sequence mismatch")
            if metadata and item["record_kind"] not in ("work_evidence", "account_evidence"):
                raise ValueError("non-header in metadata lane")
            sha = wire.digest(wire.encode(item))
            prior = db.execute(
                "SELECT digest FROM cloud_item_digests WHERE seq=?", (seq,)
            ).fetchone()
            if prior and prior[0] != sha:
                raise ValueError("immutable cloud item changed")
            if seq <= cursor:
                if not prior:
                    raise ValueError("missing cloud item digest")
                continue
            if not metadata and seq != cursor + 1:
                break  # Later out-of-order Delta visibility cannot skip gaps.
            if prior:
                cursor = seq
                applied += 1
                continue
            ident, revision = item["record_id"], item["record_revision"]
            index = item.get("chunk_index", 0)
            db.execute(
                "INSERT INTO cloud_chunks VALUES(?,?,?,?)",
                (ident, revision, index, wire.encode(item).decode()),
            )
            chunks = [
                wire.decode(r[0].encode())
                for r in db.execute(
                    "SELECT item FROM cloud_chunks WHERE record_id=? AND revision=? ORDER BY chunk",
                    (ident, revision),
                )
            ]
            expected = item.get("chunk_count", 1)
            if len(chunks) == expected:
                payload = wire.reconstruct(chunks)
                digest = wire.digest(wire.encode(payload))
                if digest != item["record_sha256"]:
                    raise ValueError("cloud payload digest mismatch")
                head = db.execute(
                    "SELECT revision,digest FROM cloud_heads WHERE record_id=?", (ident,)
                ).fetchone()
                if head and head[0] == revision and head[1] != digest:
                    raise ValueError("immutable revision changed")
                if not head or revision > head[0]:
                    from . import report_delivery

                    imported = report_delivery.apply(db, payload, forward_ts=forward_ts)
                    if metadata and not imported:
                        raise ValueError("invalid metadata envelope")
                    if not imported and payload["record_kind"] in (
                        "session_event",
                        "content_tombstone",
                    ):
                        if payload.get("event_id") != ident:
                            raise ValueError("native identity mismatch")
                        if payload["record_kind"] == "content_tombstone":
                            db.execute(
                                "UPDATE history_events SET body=NULL,expired_at=? WHERE event_id=?",
                                (payload.get("expired_at") or int(time.time()), ident),
                            )
                        else:
                            history.put(
                                db,
                                **{
                                    k: payload.get(k)
                                    for k in (
                                        "tool",
                                        "session",
                                        "project",
                                        "ts",
                                        "kind",
                                        "body",
                                        "role",
                                        "origin",
                                        "source",
                                        "machine",
                                        "source_offset",
                                        "source_inode",
                                        "source_ts",
                                    )
                                },
                                entry_id="",
                                event_id=ident,
                            )
                    db.execute(
                        "INSERT INTO cloud_heads VALUES(?,?,?) ON CONFLICT(record_id) DO UPDATE SET revision=excluded.revision,digest=excluded.digest",
                        (ident, revision, digest),
                    )
                db.execute(
                    "DELETE FROM cloud_chunks WHERE record_id=? AND revision=?", (ident, revision)
                )
            db.execute("INSERT INTO cloud_item_digests VALUES(?,?)", (seq, sha))
            cursor = seq
            applied += 1
        db.execute(
            "UPDATE cloud_config SET " + column + "=?,last_pull=?,error=NULL WHERE id=1",
            (cursor, time.time()),
        )
    return applied


def fetch_page(db, client, ns, identity):
    from .databricks import parameter

    cursor = db.execute("SELECT cursor FROM cloud_config WHERE id=1").fetchone()[0]
    # Bound escaped result JSON BEFORE materializing it in the CLI/Python process.
    # Count JSON string escaping plus conservative row framing, rather than
    # treating raw item bytes as the SQL response size. Keep payloads small enough for the authenticated CLI transport.
    sql = (
        """WITH page AS (SELECT delivery_seq,item_json FROM %s.delivery_items
      WHERE enrollment_id=:e AND destination_generation=:g AND materialized=true AND delivery_seq>:after
      ORDER BY delivery_seq LIMIT 10000), bounded AS (
      SELECT *,SUM(octet_length(to_json(array(item_json)))+64) OVER(ORDER BY delivery_seq ROWS UNBOUNDED PRECEDING) AS bytes FROM page)
      SELECT delivery_seq,item_json FROM bounded WHERE bytes<=8388608 ORDER BY delivery_seq"""
        % ns
    )
    rows = client.sql(
        sql,
        [
            parameter("e", identity["enrollment_id"]),
            parameter("g", identity["destination_generation"]),
            parameter("after", cursor),
        ],
    )
    return apply_page(db, rows)


def fetch_metadata(db, client, ns, identity, known):
    """Small approved headers through a signed contiguous materialization receipt.

    This cursor covers a filtered stream; it never advances the native trace prefix.
    Later trace replay checks the same item digest and cannot re-ingest a header.
    """
    from .databricks import parameter

    cursor = db.execute("SELECT metadata_cursor FROM cloud_config WHERE id=1").fetchone()[0]
    if known is None or cursor >= known:
        return 0
    sql = (
        """WITH page AS (SELECT delivery_seq,item_json FROM %s.delivery_items
      WHERE enrollment_id=:e AND destination_generation=:g AND materialized=true
      AND delivery_seq>:after AND delivery_seq<=:through
      AND get_json_object(item_json,'$.record_kind') IN ('work_evidence','account_evidence')
      ORDER BY delivery_seq LIMIT 4000), bounded AS (
      SELECT *,SUM(octet_length(to_json(array(item_json)))+64) OVER(ORDER BY delivery_seq ROWS UNBOUNDED PRECEDING) AS bytes FROM page)
      SELECT delivery_seq,item_json FROM bounded WHERE bytes<=2097152 ORDER BY delivery_seq"""
        % ns
    )
    rows = client.sql(
        sql,
        [
            parameter("e", identity["enrollment_id"]),
            parameter("g", identity["destination_generation"]),
            parameter("after", cursor),
            parameter("through", known),
        ],
    )
    count = apply_page(db, rows, metadata=True)
    if not rows:
        with db:
            db.execute("UPDATE cloud_config SET metadata_cursor=? WHERE id=1", (known,))
    return count


def sync_policy(db, source=store.DB_PATH):
    """Same current operator policy; no usage/body rows copied from local ledger."""
    tables = (
        "project_config",
        "project_catalog",
        "project_bindings",
        "project_aliases",
        "project_config_history",
    )
    with contextlib.closing(store.open_readonly(source)) as local:
        local.execute("BEGIN")
        policy = projects.snapshot(local)
        if projects.snapshot(db) == policy:
            return False
        values = {table: local.execute("SELECT * FROM " + table).fetchall() for table in tables}
    with db:
        for table, rows in values.items():
            db.execute("DELETE FROM " + table)
            if rows:
                db.executemany(
                    "INSERT INTO " + table + " VALUES(" + ",".join("?" for _ in rows[0]) + ")", rows
                )
    return True


def forward_boundary():
    with contextlib.closing(store.open_readonly()) as db:
        return db.execute("SELECT forward_ts FROM delivery_config").fetchone()[0]


def receipt_cursor():
    """Local verified delivery receipt: a cheap hint, not native session data."""
    try:
        with contextlib.closing(store.open_readonly()) as db:
            row = db.execute(
                "SELECT delivery_seq FROM delivery_items WHERE materialized=1 ORDER BY delivery_seq DESC LIMIT 1"
            ).fetchone()
            return row[0] if row else 0
    except Exception:
        return None


def status(database=None):
    try:
        with contextlib.closing(store.open_readonly(database or path())) as db:
            row = db.execute(
                "SELECT cursor,last_pull,error FROM cloud_config WHERE id=1"
            ).fetchone()
            count = db.execute("SELECT count(*) FROM cloud_heads").fetchone()[0]
            from . import project_rollups

            r = project_rollups.status(db)
            known = receipt_cursor()
            newest = db.execute("SELECT ts FROM history_events ORDER BY ts DESC LIMIT 1").fetchone()
            return dict(
                metadata_cursor=db.execute(
                    "SELECT metadata_cursor FROM cloud_config WHERE id=1"
                ).fetchone()[0],
                central_receipt_cursor=known,
                pending_items=max(0, known - row[0]) if known is not None else None,
                latest_native_event_at=newest[0] if newest else None,
                backend="databricks",
                cache=True,
                delivery_cursor=row[0],
                last_pull=row[1],
                error=row[2],
                records=count,
                project_revision=projects.snapshot(db)["revision"],
                published_project_revision=r.get("project_revision"),
                scope="Only verified Databricks forward corpus; shared operator project policy; missing evidence remains unknown",
            )
    except Exception as error:
        return dict(backend="databricks", state="warming", error=type(error).__name__)


def worker(database, stop):
    import fcntl

    from . import (
        call_projection,
        cloud_history,
        edit_projection,
        history_projection,
        project_rollups,
        work_projection,
    )

    lock = open(str(database) + ".cloud.lock", "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        return
    try:
        client, ns, cfg = cloud_history.connection()
        with contextlib.closing(store.connect(database)) as db:
            initialize(
                db,
                dict(
                    identity=cfg["identity"],
                    namespace=cfg["cloud"]["namespace"],
                    host=cfg["cloud"]["host"],
                ),
                forward_ts=forward_boundary(),
            )
            with db:
                project_rollups.initialize(db)
                project_rollups.put(db, enabled=1)
            while not stop.is_set():
                try:
                    sync_policy(db)
                    from . import rate_catalog

                    rate_catalog.refresh(client, cfg["cloud"]["namespace"], cfg["cloud"]["host"])
                    known = receipt_cursor()
                    cursor = db.execute("SELECT cursor FROM cloud_config WHERE id=1").fetchone()[0]
                    metadata_count = fetch_metadata(db, client, ns, cfg["identity"], known)
                    count = metadata_count or (
                        fetch_page(db, client, ns, cfg["identity"])
                        if known is None or cursor < known
                        else 0
                    )
                    # Drain bounded productive work, then publish with the SAME rollup code.
                    deadline = time.monotonic() + 30
                    while time.monotonic() < deadline and not stop.is_set():
                        usage = history_projection.run(db, limit=256, budget=0.1)
                        edits = edit_projection.run(db, limit=64, budget=0.05)
                        work = work_projection.run(db, limit=256, budget=0.1)
                        winners = call_projection.run(db, limit=512, budget=0.1)
                        if not any(
                            x.get("processed") or x.get("pending")
                            for x in (usage, edits, work, winners)
                        ):
                            break
                        if not any(x.get("processed") for x in (usage, edits, work, winners)):
                            break
                    project_rollups.capture_progress(db, usage, work)
                    from . import quota_projection

                    quota = quota_projection.run(db)
                    if quota.get("pending"):
                        stop.wait(0.1)
                        continue
                    publication = project_rollups.rebuild(database, max_hours=4)
                    project_rollups.gc(db)
                    from . import history_browse

                    browse = history_browse.run(database, max_hours=1)
                    pending = project_rollups.status(db)
                    local_debt = publication.get("state") == "published" and (
                        pending.get("pending_hours") or pending.get("pending_metadata")
                    )
                    if not count and not local_debt and not browse.get("pending_hours"):
                        stop.wait(60)
                except Exception as error:
                    db.rollback()
                    with db:
                        db.execute(
                            "UPDATE cloud_config SET error=? WHERE id=1", (type(error).__name__,)
                        )
                    stop.wait(30)
    finally:
        lock.close()


_instances = []


def start():
    from .server import Reports

    database = path()
    database.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Initialize before any HTTP read; missing auth never turns into local fallback.
    from . import cloud_history

    _, _, cfg = cloud_history.connection()
    with contextlib.closing(store.connect(database)) as db:
        initialize(
            db,
            dict(
                identity=cfg["identity"],
                namespace=cfg["cloud"]["namespace"],
                host=cfg["cloud"]["host"],
            ),
            forward_ts=forward_boundary(),
        )
    stop = threading.Event()
    thread = threading.Thread(
        target=worker, args=(database, stop), daemon=True, name="databricks-report-cache"
    )
    thread.start()
    _instances.append((stop, thread))
    return Reports(
        database=database, source_backend="databricks", source_status=lambda: status(database)
    )
