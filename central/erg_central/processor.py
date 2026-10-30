"""Finite Delta processor; no implicit OAuth, deletion or alternative ingest sink.

Server-only Spark/Unity Catalog adapter. Its deployment remains gated on B2/B5
production synthetic qualification; local tests exercise the shared wire/reducer.
"""

import os
import time
import uuid
from pathlib import Path

from erg import delivery_protocol as wire

from .disposition import raw_disposition
from .receiver import Conflict, namespace
from .zerobus import table_name


def merge(spark, table, rows, schema, keys, update=False):
    if not rows:
        return
    view = "_erg_" + uuid.uuid4().hex
    spark.createDataFrame(rows, schema).createOrReplaceTempView(view)
    condition = " AND ".join("t.%s=s.%s" % (key, key) for key in keys)
    try:
        spark.sql(
            "MERGE INTO "
            + table
            + " t USING "
            + view
            + " s ON "
            + condition
            + (" WHEN MATCHED THEN UPDATE SET *" if update else "")
            + " WHEN NOT MATCHED THEN INSERT *"
        )
    finally:
        spark.catalog.dropTempView(view)


def atomic_control(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex)
    try:
        temporary.write_bytes(wire.encode(value))
        os.replace(str(temporary), str(path))
    finally:
        if temporary.exists():
            temporary.unlink()


def await_visibility(
    wanted, read_visible, write_missing, timeout=30, clock=time.monotonic, sleep=time.sleep
):
    """Inspect before retrying; HTTP acceptance alone never advances cursors.

    Zerobus is at least once. Visible identical records need no resubmission;
    a lost response can still duplicate raw rows, which canonical MERGEs dedupe.
    """

    def observed():
        result = {}
        for seq, encoded in read_visible():
            if seq not in wanted or wanted[seq] != encoded:
                raise Conflict("staging sequence conflict")
            result[seq] = encoded
        return result

    seen = observed()
    if set(seen) == set(wanted):
        return True
    write_missing(set(wanted) - set(seen))
    deadline = clock() + timeout
    while True:
        if set(observed()) == set(wanted):
            return True
        remaining = deadline - clock()
        if remaining <= 0:
            return False
        sleep(min(2, remaining))


MAX_COHORT_BATCHES = 256
MAX_COHORT_ITEMS = 8000
MAX_COHORT_BYTES = 32 * 1024 * 1024


class Processor:
    def __init__(self, spark, zerobus, catalog, ingest, telemetry, control):
        self.spark = spark
        self.zerobus = zerobus
        self.raw = table_name(catalog, ingest, "delivery_items_raw")
        self.items = table_name(catalog, telemetry, "delivery_items")
        self.records = table_name(catalog, telemetry, "records")
        self.control = Path(control)

    def _scope(self, table, value):
        from pyspark.sql import functions as F

        return self.spark.table(table).where(
            (F.col("enrollment_id") == value["enrollment_id"])
            & (F.col("destination_generation") == value["destination_generation"])
        )

    def process(self, raw, receipt):
        return self.process_many([(raw, receipt)])

    def process_many(self, batches):
        from pyspark.sql import functions as F

        value, receipts, origins = cohort_values(batches)
        wanted = {i["delivery_seq"]: wire.encode(i).decode() for i in value["items"]}

        def read_visible():
            staged = (
                self._scope(self.raw, value)
                .where(F.col("delivery_seq").isin(list(wanted)))
                .select("delivery_seq", "item_json")
                .distinct()
                .limit(len(wanted) + 1)
                .collect()
            )
            return [(row["delivery_seq"], row["item_json"]) for row in staged]

        def write_missing(missing):
            rows = []
            encoded_size = 2
            # Keep every REST request bounded including double-escaped item_json.
            for item in value["items"]:
                if item["delivery_seq"] not in missing:
                    continue
                row = {k: value[k] for k in wire.ENROLLMENT}
                row.update(
                    batch_id=origins[item["delivery_seq"]],
                    **{
                        k: item[k]
                        for k in (
                            "delivery_seq",
                            "record_id",
                            "record_revision",
                            "record_kind",
                            "record_sha256",
                        )
                    },
                    item_json=wanted[item["delivery_seq"]],
                )
                encoded = wire.encode(row)
                cost = len(encoded) + (1 if rows else 0)
                if rows and encoded_size + cost > wire.MAX_BATCH:
                    self.zerobus.insert(rows)
                    rows = []
                    encoded_size = 2
                    cost = len(encoded)
                if encoded_size + cost > wire.MAX_BATCH:
                    raise ValueError("Zerobus item exceeds bounded request")
                rows.append(row)
                encoded_size += cost
            if rows:
                self.zerobus.insert(rows)

        if not await_visibility(wanted, read_visible, write_missing):
            return dict(state="awaiting_delta_visibility", materialized=False)
        old = (
            self._scope(self.items, value)
            .where(F.col("delivery_seq").isin(list(wanted)))
            .select("delivery_seq", "item_json")
            .collect()
        )
        if any(wanted[r["delivery_seq"]] != r["item_json"] for r in old):
            raise Conflict("canonical sequence conflict")
        schema = "enrollment_id string,destination_generation string,delivery_seq long,record_id string,record_revision long,item_json string,materialized boolean,projection_disposition string"
        records = [
            (
                value["enrollment_id"],
                value["destination_generation"],
                i["delivery_seq"],
                i["record_id"],
                i["record_revision"],
                wanted[i["delivery_seq"]],
                False,
                None,
            )
            for i in value["items"]
        ]
        merge(
            self.spark,
            self.items,
            records,
            schema,
            ("enrollment_id", "destination_generation", "delivery_seq"),
        )
        # Resolve all records in this batch with shared Spark queries and two
        # MERGEs. Never launch Spark/Delta jobs separately for each native event.
        keys = sorted({(i["record_id"], i["record_revision"]) for i in value["items"]})
        key_frame = self.spark.createDataFrame(keys, "record_id string,record_revision long")
        group = self._scope(self.items, value).join(
            F.broadcast(key_frame), ["record_id", "record_revision"], "inner"
        )
        bounds = (
            group.groupBy("record_id", "record_revision")
            .agg(
                F.count("*").alias("n"),
                F.sum(F.length(F.encode("item_json", "UTF-8"))).alias("bytes"),
            )
            .collect()
        )
        if any(r["n"] > 128 for r in bounds) or sum(r["bytes"] for r in bounds) > 192 * 1024 * 1024:
            raise Conflict("bounded reconstruction manifest exceeded")
        grouped = {}
        for row in group.select("record_id", "record_revision", "item_json").collect():
            grouped.setdefault((row["record_id"], row["record_revision"]), []).append(
                wire.decode(row["item_json"].encode())
            )
        prior_rows = (
            self._scope(self.records, value)
            .join(F.broadcast(key_frame), ["record_id", "record_revision"], "inner")
            .select("record_id", "record_revision", "record_sha256")
            .limit(len(keys) + 1)
            .collect()
        )
        if len(prior_rows) > len(keys):
            raise Conflict("canonical key multiplicity exceeded")
        prior = {(r["record_id"], r["record_revision"]): r["record_sha256"] for r in prior_rows}
        facts = []
        completed = []
        for (ident, revision), chunks in grouped.items():
            try:
                raw_payload = wire.reconstruct(chunks, as_bytes=True)
            except wire.ProtocolError as error:
                if str(error) == "incomplete record":
                    continue
                raise Conflict("canonical record conflict") from None
            payload = wire.decode(raw_payload)
            sha = wire.digest(raw_payload)
            if (ident, revision) in prior and prior[ident, revision] != sha:
                raise Conflict("revision digest conflict")
            facts.append(
                (
                    value["enrollment_id"],
                    value["installation_id"],
                    value["destination_generation"],
                    ident,
                    revision,
                    payload["record_kind"],
                    sha,
                    raw_payload.decode(),
                )
            )
            disposition = raw_disposition(payload)
            completed.extend(
                (
                    value["enrollment_id"],
                    value["destination_generation"],
                    i["delivery_seq"],
                    ident,
                    revision,
                    wire.encode(i).decode(),
                    True,
                    disposition,
                )
                for i in chunks
            )
        merge(
            self.spark,
            self.records,
            facts,
            "enrollment_id string,installation_id string,destination_generation string,record_id string,record_revision long,record_kind string,record_sha256 string,payload_json string",
            ("enrollment_id", "destination_generation", "record_id", "record_revision"),
        )
        merge(
            self.spark,
            self.items,
            completed,
            schema,
            ("enrollment_id", "destination_generation", "delivery_seq"),
            update=True,
        )
        current = self._scope(self.items, value)
        maximum = current.agg(F.max("delivery_seq")).first()[0] or 0
        # Missing sequence IDs count as gaps, not merely existing unmaterialized rows.
        complete = current.where(F.col("materialized")).select("delivery_seq")
        projected = current.where(
            F.col("materialized") & F.col("projection_disposition").isNotNull()
        ).select("delivery_seq")
        status_path = self.control / namespace(value) / "status.json"
        previous_status = wire.decode(status_path.read_bytes()) if status_path.exists() else {}
        cursors = {}
        for name, frame in (("materialized_through", complete), ("projected_through", projected)):
            cursor = previous_status.get(name, 0)
            if type(cursor) is not int or cursor < 0:
                raise Conflict("invalid control cursor")
            # Bounded contiguous advance; never re-sort or collect the full corpus.
            for page in range(64):
                rows = (
                    frame.where(F.col("delivery_seq") > cursor)
                    .orderBy("delivery_seq")
                    .limit(4096)
                    .collect()
                )
                gap = False
                for row in rows:
                    if row[0] != cursor + 1:
                        gap = True
                        break
                    cursor = row[0]
                if gap or len(rows) < 4096:
                    break
            cursors[name] = cursor
        atomic_control(
            status_path,
            dict(
                enrollment_id=value["enrollment_id"],
                destination_generation=value["destination_generation"],
                **cursors,
            ),
        )
        for receipt in receipts:
            result = dict(
                enrollment_id=value["enrollment_id"],
                destination_generation=value["destination_generation"],
                batch_id=receipt["batch_id"],
                batch_sha256=receipt["batch_sha256"],
                state="canonical",
                items_staged=True,
                observed_high_water=maximum,
                **cursors,
            )
            atomic_control(
                self.control / namespace(value) / "batches" / (receipt["batch_id"] + ".json"),
                result,
            )
        return dict(result, cohort_batches=len(receipts), cohort_items=len(value["items"]))


def cohort_values(batches):
    """Bounded internal cohort; original accepted envelopes remain authoritative."""
    if (
        not 1 <= len(batches) <= MAX_COHORT_BATCHES
        or sum(len(raw) for raw, _ in batches) > MAX_COHORT_BYTES
    ):
        raise wire.ProtocolError("invalid processing cohort size")
    first = None
    items = {}
    origins = {}
    receipts = []
    for raw, receipt in batches:
        value = wire.validate_batch(raw, receipt["batch_sha256"])
        if receipt["batch_id"] != value["batch_id"]:
            raise Conflict("batch receipt mismatch")
        if first is None:
            first = value
        if any(value[k] != first[k] for k in wire.ENROLLMENT):
            raise Conflict("processing cohort crosses enrollment")
        for item in value["items"]:
            seq = item["delivery_seq"]
            if seq in items and items[seq] != item:
                raise Conflict("cohort sequence conflict")
            items[seq] = item
            origins.setdefault(seq, value["batch_id"])
        if len(items) > MAX_COHORT_ITEMS:
            raise wire.ProtocolError("processing cohort item budget exceeded")
        receipts.append(receipt)
    return dict(first, items=[items[k] for k in sorted(items)]), receipts, origins


def batch_already_staged(previous, items):
    """Out-of-order durable staging must not wait for global prefix progress.

    Later chunks reconstruct earlier staged items. Global receipts still advance
    only after materialization; this flag merely avoids starving later batches.
    """
    return previous.get("items_staged") is True or previous["materialized_through"] >= max(
        i["delivery_seq"] for i in items
    )


def staged_index(control):
    """Cache terminal processor receipts, never acceptance or mere high water.

    The private control receipt remains the authority. A bounded parallel initial
    read replaces repeated serial remote stats/reads of every completed batch.
    Batch IDs are immutable in the receiver, so completed directories can then
    be pruned before opening any accepted marker or body.
    """
    import concurrent.futures

    path = Path(control) / "processor-staged-index.json"
    if path.exists():
        if path.stat().st_size > 16 * 1024 * 1024:
            raise Conflict("staged index exceeds bound")
        result = wire.decode(path.read_bytes())
        if (
            not isinstance(result, dict)
            or result.get("version") != 1
            or not isinstance(result.get("scopes"), dict)
        ):
            raise Conflict("invalid staged index")
        for space, batches in result["scopes"].items():
            if (
                not isinstance(space, str)
                or len(space) != 67
                or not space.startswith("v1/")
                or not wire.HEX.fullmatch(space[3:])
            ):
                raise Conflict("invalid staged index scope")
            if not isinstance(batches, dict) or any(
                not isinstance(k, str)
                or "/" in k
                or not k
                or not isinstance(v, str)
                or not wire.HEX.fullmatch(v)
                for k, v in batches.items()
            ):
                raise Conflict("invalid staged index batch")
        from .staging_index import load_shards

        return load_shards(control, result)
    result = dict(version=1, scopes={})
    receipts = Path(control).glob("v1/*/batches/*.json")

    def read(path):
        if path.stat().st_size > 65536:
            raise Conflict("oversized control receipt")
        value = wire.decode(path.read_bytes())
        if value.get("items_staged") is not True:
            return None
        sha = value.get("batch_sha256")
        if not isinstance(sha, str) or not wire.HEX.fullmatch(sha):
            raise Conflict("invalid terminal receipt digest")
        return path.parent.parent.relative_to(control).as_posix(), path.stem, sha

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        # executor.map eagerly submits its input on supported Python versions;
        # cap the migration inventory rather than building an unbounded queue.
        import itertools

        batch = list(itertools.islice(receipts, 50001))
        if len(batch) > 50000:
            raise Conflict("staged index migration requires sharding")
        for value in pool.map(read, batch):
            if value:
                space, ident, sha = value
                result["scopes"].setdefault(space, {})[ident] = sha
    atomic_control(path, result)
    from .staging_index import load_shards

    return load_shards(control, result)


def index_staged(index, control, receipts):
    from .staging_index import persist

    persist(index, control, receipts, atomic_control)


def pending_markers(root, control, limit, started, max_seconds, staged=None):
    """Read small receipts before bodies; oldest sequence first within bounded sweep.

    Durable staging already validates immutable body bytes. Rechecking those large
    bodies on every tick wastes remote IO without improving the acknowledgment.
    """
    pending = []
    for directory, children, names in os.walk(str(root)):
        children.sort()
        relative = Path(directory).relative_to(root).as_posix()
        if staged is not None and relative.endswith("/batches"):
            completed = staged["scopes"].get(relative[: -len("/batches")], {})
            children[:] = [child for child in children if child not in completed]
        if time.monotonic() - started >= max_seconds or len(pending) >= limit:
            break
        if "accepted.json" not in names:
            continue
        marker = Path(directory) / "accepted.json"
        if marker.stat().st_size > 65536:
            raise Conflict("oversized spool object")
        receipt = wire.decode(marker.read_bytes())
        output = Path(control) / namespace(receipt) / "batches" / (receipt["batch_id"] + ".json")
        if output.exists():
            previous = wire.decode(output.read_bytes())
            if previous["batch_sha256"] != receipt["batch_sha256"]:
                raise Conflict("control receipt conflict")
            if previous.get("items_staged") is True:
                continue
        quarantine = (
            Path(control) / namespace(receipt) / "quarantine" / (receipt["batch_id"] + ".json")
        )
        if quarantine.exists():
            continue
        pending.append((min(receipt["accepted_delivery_seqs"]), marker, receipt))
    return [
        (marker, receipt)
        for _, marker, receipt in sorted(pending, key=lambda row: (row[0], str(row[1])))
    ]


def run(
    spark,
    zerobus,
    spool,
    control,
    catalog,
    ingest="ingest",
    telemetry="telemetry",
    max_batches=512,
    max_seconds=240,
    processor_factory=None,
):
    """Finite reconciliation sweep, independent of notification delivery.

    This initial qualification scanner rescans marker metadata. Large-fleet indexing
    and measured 10x central capacity remain acceptance gates, not claimed here.
    """
    processor = (processor_factory or Processor)(
        spark, zerobus, catalog, ingest, telemetry, control
    )
    started = time.monotonic()
    attempted = 0
    results = []
    root = Path(spool)
    discovery_started = time.monotonic()
    completed = staged_index(control)
    cohort = []
    scope = None
    weight = dict(items=0, bytes=0)

    def quarantine_batch(value, receipt):
        quarantine = Path(control) / namespace(value) / "quarantine" / (value["batch_id"] + ".json")
        atomic_control(
            quarantine,
            dict(
                batch_id=value["batch_id"],
                batch_sha256=receipt["batch_sha256"],
                state="unresolved",
                reason="identity, schema or digest conflict; explicit repair required",
            ),
        )

    def flush():
        if not cohort:
            return
        try:
            result = processor.process_many(cohort)
            if result.get("state") in ("canonical", "raw_visible"):
                index_staged(completed, control, [receipt for _, receipt in cohort])
            results.append(
                {
                    k: result.get(k)
                    for k in (
                        "state",
                        "materialized_through",
                        "projected_through",
                        "cohort_batches",
                        "cohort_items",
                    )
                }
            )
        except (Conflict, wire.ProtocolError):
            # Isolate a bad member; successful immutable rows remain replay-safe.
            for raw, receipt in cohort:
                value = wire.validate_batch(raw, receipt["batch_sha256"])
                try:
                    individual = processor.process(raw, receipt)
                    if individual.get("state") in ("canonical", "raw_visible"):
                        index_staged(completed, control, [receipt])
                except (Conflict, wire.ProtocolError):
                    quarantine_batch(value, receipt)
        cohort.clear()
        weight.update(items=0, bytes=0)

    discovery_metrics = None
    if root.as_posix().startswith("/Volumes/"):
        from .discovery import paged_markers

        candidates, discovery_metrics = paged_markers(
            root,
            control,
            max_batches,
            time.monotonic(),
            min(60, max_seconds / 4),
            completed,
            atomic_control,
        )
    else:
        candidates = pending_markers(
            root, control, max_batches, time.monotonic(), min(60, max_seconds / 4), staged=completed
        )
    discovery_seconds = time.monotonic() - discovery_started
    # Discovery has its own bound; already-found work gets a processing budget.
    processing_started = time.monotonic()
    for marker, receipt in candidates:
        if time.monotonic() - processing_started >= max_seconds or attempted >= max_batches:
            break
        body = marker.parent / "body.json"
        if body.stat().st_size > wire.MAX_BATCH:
            raise Conflict("oversized spool object")
        raw = body.read_bytes()
        identity = wire.validate_batch(raw, receipt["batch_sha256"])
        output = Path(control) / namespace(identity) / "batches" / (identity["batch_id"] + ".json")
        if output.exists():
            previous = wire.decode(output.read_bytes())
            if previous["batch_sha256"] != receipt["batch_sha256"]:
                raise Conflict("control receipt conflict")
            if batch_already_staged(previous, identity["items"]):
                continue
        this_scope = tuple(identity[k] for k in wire.ENROLLMENT)
        if cohort and (
            this_scope != scope
            or len(cohort) >= MAX_COHORT_BATCHES
            or weight["items"] + len(identity["items"]) > MAX_COHORT_ITEMS
            or weight["bytes"] + len(raw) > MAX_COHORT_BYTES
        ):
            flush()
        if time.monotonic() - processing_started >= max_seconds:
            break
        scope = this_scope
        cohort.append((raw, receipt))
        attempted += 1
        weight["items"] += len(identity["items"])
        weight["bytes"] += len(raw)
        if len(cohort) >= MAX_COHORT_BATCHES or weight["items"] >= MAX_COHORT_ITEMS:
            flush()
    flush()
    return dict(
        attempted_batches=attempted,
        discovery_seconds=discovery_seconds,
        discovery=discovery_metrics,
        indexed_staged_batches=sum(len(v) for v in completed["scopes"].values()),
        elapsed_seconds=time.monotonic() - started,
        outcomes=results,
        zerobus_requests=dict(zerobus.metrics),
        zerobus_metered_bytes=None,
        databricks_dbu=None,
        reported_cloud_charge=None,
    )
