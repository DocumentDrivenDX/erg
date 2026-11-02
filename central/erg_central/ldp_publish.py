"""Publish only proven generated LDP facts through existing Delta interfaces.

There is no reconstruction graph here. Writes are idempotent compatibility
copies from fixed generated snapshots; receipts follow durable verification.
"""

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from erg import delivery_protocol as wire

from . import ldp_snapshots as snapshots
from .ldp_prefix import prove_materialized, prove_window
from .processor import atomic_control
from .receiver import Conflict, namespace


def copy_frame(spark, table, frame, keys, update=None):
    view = "_erg_ldp_" + uuid.uuid4().hex
    frame.createOrReplaceTempView(view)
    condition = " AND ".join("t.%s=s.%s" % (key, key) for key in keys)
    matched = (
        " WHEN MATCHED THEN UPDATE SET " + ",".join("t.%s=s.%s" % (key, key) for key in update)
        if update
        else ""
    )
    try:
        spark.sql(
            "MERGE INTO "
            + snapshots.name(table)
            + " t USING "
            + view
            + " s ON "
            + condition
            + matched
            + " WHEN NOT MATCHED THEN INSERT *"
        )
    finally:
        spark.catalog.dropTempView(view)


def durable_copy(spark, ready, facts, cursors, previous, identity, records_table, items_table):
    from pyspark.sql import functions as F

    selected = ready.where(
        (F.col("delivery_seq") > previous["materialized_through"])
        & (F.col("delivery_seq") <= cursors["materialized_through"])
    )
    selected_facts = facts.join(
        selected.select("record_id", "record_revision").distinct(),
        ["record_id", "record_revision"],
        "inner",
    )
    records = selected_facts.select(
        "enrollment_id",
        "installation_id",
        "destination_generation",
        "record_id",
        "record_revision",
        F.get_json_object("payload_json", "$.record_kind").alias("record_kind"),
        "record_sha256",
        "payload_json",
    )
    copy_frame(
        spark,
        records_table,
        records,
        ("enrollment_id", "destination_generation", "record_id", "record_revision"),
    )
    items = selected.select(
        "delivery_seq",
        "record_id",
        "record_revision",
        "item_json",
        F.lit(True).alias("materialized"),
        "projection_disposition",
    )
    for key in ("enrollment_id", "destination_generation"):
        items = items.withColumn(key, F.lit(identity[key]))
    copy_frame(
        spark,
        items_table,
        items,
        ("enrollment_id", "destination_generation", "delivery_seq"),
        ("materialized", "projection_disposition"),
    )
    # Re-read durable tables independently before publishing a prefix. Full wire
    # bytes and revision hash, not a row count, must match the selected snapshot.
    confirmed = snapshots.pin(spark, [records_table, items_table], identity)
    actual = confirmed["frames"][items_table].alias("actual")
    wanted = selected.alias("wanted")
    reject = wanted.join(
        actual, F.col("wanted.delivery_seq") == F.col("actual.delivery_seq"), "left"
    ).where(
        F.col("actual.delivery_seq").isNull()
        | ~F.col("actual.materialized")
        | (F.col("actual.item_json") != F.col("wanted.item_json"))
    )
    snapshots.reject_rows(reject, "durable canonical item copy did not verify")
    actual = confirmed["frames"][records_table].alias("actual")
    wanted = records.alias("wanted")
    joined = wanted.join(actual, ["record_id", "record_revision"], "left")
    snapshots.reject_rows(
        joined.where(
            F.col("actual.record_sha256").isNull()
            | (F.col("actual.record_sha256") != F.col("wanted.record_sha256"))
            | (F.col("actual.payload_json") != F.col("wanted.payload_json"))
        ),
        "durable canonical revision copy did not verify",
    )
    return confirmed


def read_previous(path, identity, config):
    """A migrated production writer must never invent a replacement zero prefix."""
    production = config.get("mode") == "production"
    if production and not path.is_file():
        raise Conflict("prior production receipt is unavailable")
    previous = (
        wire.decode(path.read_bytes())
        if path.exists()
        else dict(materialized_through=0, projected_through=0)
    )
    for key in ("enrollment_id", "destination_generation"):
        if previous.get(key, None if production else identity[key]) != identity[key]:
            raise Conflict("prior publication scope differs")
    mat, proj = previous.get("materialized_through"), previous.get("projected_through")
    if type(mat) is not int or type(proj) is not int or not 0 <= proj <= mat:
        raise Conflict("invalid prior publication cursors")
    if production and any(
        previous[key] < config["admission"][key]
        for key in ("materialized_through", "projected_through")
    ):
        raise Conflict("prior receipt precedes the declared migration boundary")
    return previous


def publish(spark, capability, config, authority, recheck, failpoint=None):
    from pyspark.sql import functions as F
    from tablespec.ldp import CoordinatorEvidence, assess_publication_readiness

    identity = capability.source.identity_scope.model_dump(mode="python")
    path = Path(config["control"]) / namespace(identity) / "status.json"
    previous = read_previous(path, identity, config)
    records_table = snapshots.name(config["records_table"])
    items_table = snapshots.name(config["items_table"])
    archive = capability.archive
    required = sorted(
        set(capability.required_dependencies)
        | {
            archive.raw_target,
            archive.sequence_decisions_target,
            archive.target,
            archive.fact_events_target,
            archive.latest_target,
        }
    )
    if not {records_table, items_table, capability.source.table}.issubset(required):
        raise Conflict("canonical compatibility and source dependencies must be declared")
    pinned = snapshots.pin(spark, required, identity)
    if pinned["table_ids"][capability.source.table] != config["source_table_id"]:
        raise Conflict("publication source Delta identity changed")
    frame = pinned["frames"]
    accepted, facts = snapshots.validate_outputs(
        frame[archive.sequence_decisions_target],
        frame[archive.target],
        frame[records_table],
        frame[items_table],
        identity,
    )
    ready = snapshots.ready_items(accepted, facts)
    metadata = snapshots.bounded_metadata(
        ready.where(F.col("delivery_seq") > previous["materialized_through"]).select(
            "delivery_seq",
            "record_id",
            "record_revision",
            "record_sha256",
            "revision_sha256",
            "projection_disposition",
            "payload_bytes",
        )
    )
    materialized = prove_materialized(previous["materialized_through"], metadata)
    old = (
        frame[items_table]
        .where(F.col("materialized"))
        .where(
            (F.col("delivery_seq") > previous["projected_through"])
            & (F.col("delivery_seq") <= previous["materialized_through"])
        )
        .select("delivery_seq", "projection_disposition")
    )
    new = ready.where(
        (F.col("delivery_seq") > previous["materialized_through"])
        & (F.col("delivery_seq") <= materialized)
    ).select("delivery_seq", "projection_disposition")
    projection = snapshots.bounded_metadata(old.unionByName(new))
    cursors = prove_window(previous, metadata, projection)
    now = datetime.now(timezone.utc)
    snapshot = dict(
        pins=pinned["versions"],
        table_ids=pinned["table_ids"],
        previous={k: previous[k] for k in cursors},
        cursors=cursors,
    )
    snapshot_sha = wire.digest(wire.encode(snapshot))
    evidence = CoordinatorEvidence(
        snapshot_id=snapshot_sha,
        snapshot_sha256=snapshot_sha,
        source_table=capability.source.table,
        declared_starting_version=capability.source.starting_version,
        current_source_version=pinned["versions"][capability.source.table],
        transform_id=capability.transform.transform_id,
        transform_version=capability.transform.version,
        transform_sha256=capability.transform.sha256,
        required_dependency_ids=required,
        validated_dependency_ids=required,
        dependency_pins=pinned["versions"],
        dependency_snapshot_sha256=pinned["fingerprints"],
        conflict_check="clear",
        conflict_query_sha256=wire.digest(b"Erg LDP global/legacy integrity checks v1"),
        evaluated_at=now,
        evidence_expires_at=now + timedelta(minutes=5),
        execution_authority=authority(),
        **cursors,
    )
    report = assess_publication_readiness(
        "erg_revisions", capability, evidence, publication_policy="allow_serialized_job_execution"
    )
    if report.status != "ready":
        raise Conflict("LDP publication readiness is blocked")
    if cursors["materialized_through"] > previous["materialized_through"]:
        confirmed = durable_copy(
            spark, ready, facts, cursors, previous, identity, records_table, items_table
        )
        for field in ("versions", "fingerprints", "table_ids"):
            pinned[field].update(confirmed[field])
    if failpoint:
        failpoint("after_durable_copy")
    recheck()
    # Repeat the real backend observation after potentially slow Delta writes.
    snapshot["pins"] = pinned["versions"]
    snapshot["table_ids"] = pinned["table_ids"]
    snapshot_sha = wire.digest(wire.encode(snapshot))
    now = datetime.now(timezone.utc)
    data = evidence.model_dump()
    data.update(
        snapshot_id=snapshot_sha,
        snapshot_sha256=snapshot_sha,
        dependency_pins=pinned["versions"],
        dependency_snapshot_sha256=pinned["fingerprints"],
        execution_authority=authority(),
        evaluated_at=now,
        evidence_expires_at=now + timedelta(minutes=5),
    )
    evidence = CoordinatorEvidence.model_validate(data)
    report = assess_publication_readiness(
        "erg_revisions", capability, evidence, publication_policy="allow_serialized_job_execution"
    )
    if report.status != "ready":
        raise Conflict("LDP publication authority expired before receipt")
    current = wire.decode(path.read_bytes()) if path.exists() else previous
    if any(current.get(key) != previous[key] for key in cursors):
        raise Conflict("publication cursor changed during evaluation")
    atomic_control(
        Path(config["control"])
        / "ldp-publications"
        / (snapshot_sha + "-" + wire.digest(wire.encode(report.model_dump(mode="json"))) + ".json"),
        dict(snapshot=snapshot, readiness=report.model_dump(mode="json")),
    )
    atomic_control(
        path,
        dict(
            enrollment_id=identity["enrollment_id"],
            destination_generation=identity["destination_generation"],
            **cursors,
        ),
    )
    return dict(
        state="published",
        authority_kind=report.authority_kind,
        snapshot_sha256=snapshot_sha,
        **cursors,
    )
