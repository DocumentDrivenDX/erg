"""Fixed Delta snapshots and bounded metadata collection for LDP publication."""

from erg import delivery_protocol as wire

from .ldp_prefix import MAX_METADATA_BYTES
from .receiver import Conflict
from .zerobus import table_name


def name(value):
    return table_name(*value.split("."))


def scope(frame, identity):
    from pyspark.sql import functions as F

    for key in ("enrollment_id", "destination_generation"):
        value = identity[key]
        if key in frame.columns:
            frame = frame.where(F.col(key) == F.lit(value))
    return frame


def pin(spark, tables, identity):
    pins, fingerprints, frames, ids = {}, {}, {}, {}
    for table in sorted(set(tables)):
        table = name(table)
        detail = spark.sql("DESCRIBE DETAIL " + table).first()
        version = spark.sql("DESCRIBE HISTORY " + table + " LIMIT 1").first()["version"]
        if detail["format"] != "delta" or type(version) is not int or version < 0:
            raise Conflict("publication dependency is not a pinned Delta table")
        pins[table] = version
        ids[table] = detail["id"]
        fingerprints[table] = wire.digest(
            wire.encode(
                dict(table=table, table_id=detail["id"], version=version, identity=identity)
            )
        )
        frames[table] = scope(spark.read.option("versionAsOf", version).table(table), identity)
    return dict(versions=pins, fingerprints=fingerprints, frames=frames, table_ids=ids)


def bounded_metadata(frame, limit=16000):
    from pyspark.sql import Window
    from pyspark.sql import functions as F

    frame = frame.orderBy("delivery_seq").limit(limit)
    cost = F.length(F.encode(F.to_json(F.struct(*[F.col(c) for c in frame.columns])), "UTF-8")) + 64
    window = Window.orderBy("delivery_seq").rowsBetween(
        Window.unboundedPreceding, Window.currentRow
    )
    result = (
        frame.withColumn("_proof_bytes", F.sum(cost).over(window))
        .where(F.col("_proof_bytes") <= MAX_METADATA_BYTES // 2)
        .drop("_proof_bytes")
    )
    return [row.asDict() for row in result.collect()]


def reject_rows(frame, reason):
    if frame.limit(1).count():
        raise Conflict(reason)


def validate_outputs(sequence, archive, records, items, identity):
    from pyspark.sql import functions as F

    reject_rows(
        sequence.where(F.col("event_type") == "conflict"),
        "LDP sequence conflict blocks publication",
    )
    reject_rows(
        archive.where(F.col("event_type") == "conflict"), "LDP revision conflict blocks publication"
    )
    for frame in (sequence, archive):
        reject_rows(
            frame.where(
                F.col("installation_id").isNull()
                | (F.col("installation_id") != F.lit(identity["installation_id"]))
            ),
            "LDP output installation identity differs",
        )
    accepted = sequence.where(F.col("event_type") == "accepted_item")
    facts = archive.where(F.col("event_type") == "revision")
    reject_rows(
        accepted.groupBy("delivery_sequence").count().where(F.col("count") != 1),
        "duplicate LDP sequence decision",
    )
    reject_rows(
        facts.groupBy("record_id", "record_revision").count().where(F.col("count") != 1),
        "duplicate LDP immutable revision",
    )
    old = records.select("record_id", "record_revision", "record_sha256", "installation_id").alias(
        "old"
    )
    new = facts.alias("new")
    conflicts = new.join(old, ["record_id", "record_revision"], "inner").where(
        (F.col("new.record_sha256") != F.col("old.record_sha256"))
        | (F.col("old.installation_id") != F.lit(identity["installation_id"]))
    )
    reject_rows(conflicts, "LDP revision conflicts with immutable legacy history")
    old = items.select(F.col("delivery_seq").alias("delivery_sequence"), "item_json").alias("old")
    new = accepted.alias("new")
    reject_rows(
        new.join(old, ["delivery_sequence"], "inner").where(
            F.col("new.item_json") != F.col("old.item_json")
        ),
        "LDP sequence conflicts with immutable legacy history",
    )
    return accepted, facts


def ready_items(sequence, facts):
    from pyspark.sql import functions as F

    decisions = sequence.select(
        F.col("delivery_sequence").alias("delivery_seq"),
        "record_id",
        "record_revision",
        "item_json",
        F.col("content_hash").alias("record_sha256"),
    )
    revisions = facts.select(
        "record_id",
        "record_revision",
        F.col("record_sha256").alias("revision_sha256"),
        F.col("disposition").alias("projection_disposition"),
        F.length(F.encode("payload_json", "UTF-8")).cast("long").alias("payload_bytes"),
    )
    return decisions.join(revisions, ["record_id", "record_revision"], "left")
