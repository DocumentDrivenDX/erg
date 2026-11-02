"""Spool -> qualified Zerobus visibility -> declared Delta append source.

The private staging receipt/cache cannot publish canonical or projected progress.
LDP owns reconstruction; the publication coordinator owns delivery receipts.
"""

import concurrent.futures
import time
from pathlib import Path

from erg import delivery_protocol as wire

from .ldp_codec import checked_item
from .processor import Processor, atomic_control, await_visibility, cohort_values
from .receiver import Conflict, namespace
from .zerobus import table_name

FIELDS = wire.ENROLLMENT + (
    "batch_id",
    "delivery_seq",
    "record_id",
    "record_revision",
    "record_kind",
    "record_sha256",
    "item_json",
)
SCHEMA = "enrollment_id string,installation_id string,destination_generation string,batch_id string,delivery_seq long,record_id string,record_revision long,record_kind string,record_sha256 string,item_json string"


def bounded_insert(client, rows):
    pending = []
    size = 2
    for row in rows:
        cost = len(wire.encode(row)) + bool(pending)
        if pending and size + cost > wire.MAX_BATCH:
            client.insert(pending)
            pending = []
            size = 2
            cost = len(wire.encode(row))
        if size + cost > wire.MAX_BATCH:
            raise Conflict("Zerobus item exceeds bounded request")
        pending.append(row)
        size += cost
    if pending:
        client.insert(pending)


def publish_staging_receipts(control, receipts, status):
    def write(receipt):
        value = dict(status, batch_id=receipt["batch_id"], batch_sha256=receipt["batch_sha256"])
        atomic_control(
            Path(control) / namespace(value) / "batches" / (value["batch_id"] + ".json"), value
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for _ in pool.map(write, receipts):
            pass


class StageProcessor(Processor):
    def __init__(
        self,
        spark,
        zerobus,
        catalog,
        ingest,
        telemetry,
        control,
        source,
        identity,
        source_id,
        after_sequence=0,
    ):
        super().__init__(spark, zerobus, catalog, ingest, telemetry, control)
        self.source = table_name(*source.split("."))
        self.identity = identity
        self.source_id = source_id
        if type(after_sequence) is not int or not 0 <= after_sequence < 2**63:
            raise ValueError("valid completed legacy prefix required")
        self.after_sequence = after_sequence
        if Path(control).name != "ldp-staging":
            raise ValueError("separate ldp-staging control root required")

    def visible(self, value, wanted):
        from pyspark.sql import functions as F

        rows = (
            self._scope(self.raw, value)
            .where(F.col("delivery_seq").isin(list(wanted)))
            .select(*[key for key in FIELDS if key != "batch_id"])
            .dropDuplicates()
            .limit(len(wanted) + 1)
            .collect()
        )
        result = []
        for row in rows:
            data = row.asDict()
            checked_item(data, self.identity)
            result.append((data["delivery_seq"], data["item_json"]))
        return result

    def append(self, rows):
        detail = self.spark.sql("DESCRIBE DETAIL " + self.source).first()
        if (
            detail["id"] != self.source_id
            or detail["format"] != "delta"
            or detail["properties"].get("delta.appendOnly") != "true"
        ):
            raise Conflict("declared append source identity or policy changed")
        # Append visibility-confirmed bytes only. Replays after an uncertain
        # append remain safe in the checkpoint-backed exact-sequence processor.
        self.spark.createDataFrame(
            [tuple(row[key] for key in FIELDS) for row in rows], SCHEMA
        ).write.format("delta").mode("append").saveAsTable(self.source)

    def process_many(self, batches):
        value, receipts, origins = cohort_values(batches)
        if any(value[key] != self.identity[key] for key in wire.ENROLLMENT):
            raise Conflict("staging batch is outside declared LDP identity")
        legacy = sum(item["delivery_seq"] <= self.after_sequence for item in value["items"])
        value = dict(
            value,
            items=[item for item in value["items"] if item["delivery_seq"] > self.after_sequence],
        )
        wanted = {item["delivery_seq"]: wire.encode(item).decode() for item in value["items"]}
        rows = [
            dict(
                self.identity,
                batch_id=origins[item["delivery_seq"]],
                **{
                    key: item[key]
                    for key in (
                        "delivery_seq",
                        "record_id",
                        "record_revision",
                        "record_kind",
                        "record_sha256",
                    )
                },
                item_json=wanted[item["delivery_seq"]],
            )
            for item in value["items"]
        ]
        started = time.monotonic()
        if rows and not await_visibility(
            wanted,
            lambda: self.visible(value, wanted),
            lambda missing: bounded_insert(
                self.zerobus, [row for row in rows if row["delivery_seq"] in missing]
            ),
        ):
            return dict(
                state="awaiting_delta_visibility",
                cohort_items=len(rows),
                cohort_batches=len(receipts),
            )
        visibility = time.monotonic() - started
        append_started = time.monotonic()
        if rows:
            self.append(rows)
        append_seconds = time.monotonic() - append_started
        receipt_started = time.monotonic()
        publish_staging_receipts(
            self.control,
            receipts,
            dict(
                self.identity,
                state="raw_visible",
                items_staged=True,
                source_table=self.source,
                legacy_completed_through=self.after_sequence,
            ),
        )
        return dict(
            state="raw_visible",
            cohort_items=len(rows),
            legacy_covered_items=legacy,
            cohort_batches=len(receipts),
            cohort_source_bytes=sum(len(raw) for raw, _ in batches),
            phase_seconds=dict(
                zerobus_visibility=visibility,
                source_append=append_seconds,
                receipts=time.monotonic() - receipt_started,
            ),
        )
