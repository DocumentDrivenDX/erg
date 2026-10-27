"""Bounded independent synthetic corpus/hash/schema verification in Databricks."""

from pathlib import Path

from erg import delivery_protocol as wire

from .processor import atomic_control
from .receiver import namespace
from .zerobus import table_name

CORE = {
    "event_time": "timestamp",
    "request_id": "string",
    "invocation_id": "string",
    "request": "string",
    "response": "string",
    "destination_type": "string",
    "destination_name": "string",
    "destination_model": "string",
    "requester": "string",
    "schema_version": "string",
    "url": "string",
    "api_type": "string",
    "request_tags": "map<string,string>",
    "status_code": "int",
    "sampling_fraction": "double",
    "latency_ms": "bigint",
    "time_to_first_byte_ms": "bigint",
    "logging_error_codes": "array<string>",
}


def compare(manifest, rows):
    if (
        manifest.get("kind") != "erg-synthetic-qualification-v1"
        or manifest.get("actual_session_data") is not False
    ):
        raise ValueError("synthetic manifest required")
    expected = manifest["expected_revisions"]
    if not 1 <= len(expected) <= 500:
        raise ValueError("bounded expected corpus required")
    wanted = {(r["record_id"], r["record_revision"]): r["record_sha256"] for r in expected}
    if len(wanted) != len(expected):
        raise ValueError("duplicate expected revision")
    observed = {}
    source_order = {}
    tools = set()
    for row in rows:
        key = (row["record_id"], row["record_revision"])
        if key in observed:
            raise ValueError("duplicate canonical revision")
        if key not in wanted or row["record_sha256"] != wanted[key]:
            raise ValueError("unexpected canonical revision or digest")
        raw = row["payload_json"].encode("utf-8")
        if wire.digest(raw) != wanted[key]:
            raise ValueError("body round-trip digest mismatch")
        payload = wire.decode(raw)
        if any(payload.get(k) != row[k] for k in ("record_id", "record_revision", "record_kind")):
            raise ValueError("canonical payload identity mismatch")
        source_order.setdefault(key[0], set()).add(payload["source_seq"])
        tools.add(payload["tool"])
        observed[key] = row["record_sha256"]
    if observed != wanted:
        raise ValueError("missing canonical revisions")
    if any(len(v) != 1 for v in source_order.values()):
        raise ValueError("revision changed native source order")
    return dict(
        verified=True,
        canonical_revisions=len(observed),
        expected_heads=len(source_order),
        tools=sorted(tools),
        exact_payload_hashes=True,
        source_order_preserved=True,
        no_duplicate_canonical_revisions=True,
        actual_session_data=False,
    )


def verify(spark, manifest_path, catalog, ingest, telemetry, control):
    from pyspark.sql import functions as F

    path = Path(manifest_path)
    if not str(path).startswith(str(Path(control)) + "/") or ".." in path.parts:
        raise ValueError("verification manifest must be under owned control volume")
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("verification manifest too large")
    manifest = wire.decode(path.read_bytes())
    identity = manifest["identity"]
    if manifest.get("actual_session_data") is not False:
        raise ValueError("synthetic corpus required")

    def scope(frame):
        return frame.where(
            (F.col("enrollment_id") == identity["enrollment_id"])
            & (F.col("destination_generation") == identity["destination_generation"])
        )

    revisions = scope(spark.table(table_name(catalog, telemetry, "records")))
    rows = [r.asDict() for r in revisions.limit(len(manifest["expected_revisions"]) + 1).collect()]
    result = compare(manifest, rows)
    heads = scope(spark.table(table_name(catalog, telemetry, "session_events"))).count()
    if heads != result["expected_heads"]:
        raise ValueError("latest-session projection count mismatch")
    items = scope(spark.table(table_name(catalog, telemetry, "delivery_items")))
    count = items.count()
    distinct = items.select("delivery_seq").distinct().count()
    if count != manifest["delivery_items"] or distinct != count:
        raise ValueError("delivery item coverage or dedup mismatch")
    if items.where(~F.col("materialized")).count():
        raise ValueError("unmaterialized synthetic items")
    schema = {
        field.name: field.dataType.simpleString()
        for field in spark.table(table_name(catalog, telemetry, "inference_calls")).schema.fields
    }
    if any(schema.get(name) != dtype for name, dtype in CORE.items()):
        raise ValueError("inference core schema mismatch")
    result.update(
        delivery_items=count,
        latest_session_heads=heads,
        inference_core_columns_verified=len(CORE),
        usage_projection="not claimed; this proof verifies raw session delivery",
        zerobus_metered_bytes=None,
        reported_cloud_charge=None,
    )
    output = Path(control) / namespace(identity) / "qualification" / "verification.json"
    atomic_control(output, result)
    return result
