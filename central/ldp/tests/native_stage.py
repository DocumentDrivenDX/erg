"""Native synthetic test for bounded visibility and append-source admission."""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--application-archive", required=True)
    parser.add_argument("--application-sha256", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--control", required=True)
    parser.add_argument("--expect-replay-regression", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"ergqual_[A-Za-z0-9_]{1,60}", args.namespace):
        raise ValueError("isolated synthetic namespace required")
    control = Path(args.control)
    if ".." in control.parts or not str(control).startswith(
        "/Volumes/erg/ingest/control/qualification/ergqual_"
    ):
        raise ValueError("private qualification control required")
    archive = Path("/Workspace" + args.application_archive)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != args.application_sha256:
        raise ValueError("application archive differs")
    sys.path.insert(0, str(archive))
    from erg_central.ldp_stage import FIELDS, SCHEMA, StageProcessor
    from erg_central.receiver import Conflict, namespace
    from pyspark.sql import SparkSession

    from erg import delivery_protocol as wire

    spark = SparkSession.builder.getOrCreate()
    identity = dict(
        enrollment_id="synthetic-native-stage",
        installation_id="fabricated",
        destination_generation="synthetic-only-v1",
    )
    items = [
        wire.record_items(
            dict(
                record_id="stage-" + str(n),
                record_revision=1,
                record_kind="session_event",
                body="fabricated",
            ),
            n + 1,
        )[0]
        for n in range(2)
    ]
    raw = wire.batch(identity, items, batch_id="00000000-0000-0000-0000-000000000001")
    receipt = dict(
        identity,
        batch_id=wire.decode(raw)["batch_id"],
        batch_sha256=wire.digest(raw),
        accepted_delivery_seqs=[1, 2],
    )
    raw_table = "erg.telemetry." + args.namespace + "_raw"
    source = "erg.telemetry." + args.namespace + "_source"
    for table in (raw_table, source):
        spark.sql(
            "CREATE TABLE IF NOT EXISTS "
            + table
            + " ("
            + SCHEMA
            + ") USING DELTA TBLPROPERTIES ('delta.appendOnly'='true')"
        )
    if not spark.table(raw_table).limit(1).count():
        rows = []
        for n in range(65):
            item = items[1] if n == 64 else items[0]
            row = dict(
                identity,
                **{key: item[key] for key in wire.IDENTITY + ("delivery_seq", "record_sha256")},
                batch_id="00000000-0000-0000-0000-" + str(n).zfill(12),
                item_json=wire.encode(item).decode(),
            )
            rows.append(tuple(row[key] for key in FIELDS))
        spark.createDataFrame(rows, SCHEMA).write.mode("append").saveAsTable(raw_table)

    class NoInsert:
        def insert(self, rows):
            raise AssertionError("identical cross-batch replays hid visible items")

    processor = StageProcessor(
        spark,
        NoInsert(),
        "erg",
        "ingest",
        "telemetry",
        control / "ldp-staging",
        source,
        identity,
        spark.sql("DESCRIBE DETAIL " + source).first()["id"],
    )
    processor.raw = raw_table
    wanted = {item["delivery_seq"]: wire.encode(item).decode() for item in items}
    visible = processor.visible(identity, wanted)
    if args.expect_replay_regression:
        assert len(visible) == 3, "negative control did not reproduce the visibility regression"
        print(json.dumps(dict(replay_regression_detected=True, no_source_append=True)))
        return
    assert len(visible) == 2 and dict(visible) == wanted
    result = processor.process_many([(raw, receipt)])
    assert result["state"] == "raw_visible"
    foreign = wire.decode(raw)
    foreign["installation_id"] = "other-fabricated-installation"
    foreign_raw = wire.encode(foreign)
    try:
        processor.process_many(
            [(foreign_raw, dict(receipt, batch_sha256=wire.digest(foreign_raw)))]
        )
    except Conflict as error:
        assert str(error) == "staging batch is outside declared LDP identity"
    else:
        raise AssertionError("foreign installation was admitted into the dedicated source")
    actual = spark.table(source).select("delivery_seq", "item_json").distinct().collect()
    assert {row["delivery_seq"]: row["item_json"] for row in actual} == wanted
    assert not (control / namespace(identity) / "status.json").exists()
    terminal = (
        control / "ldp-staging" / namespace(identity) / "batches" / (receipt["batch_id"] + ".json")
    )
    assert wire.decode(terminal.read_bytes())["items_staged"] is True
    print(
        json.dumps(
            dict(
                native_staging_passed=True,
                foreign_scope_rejected_before_append=True,
                unique_items=2,
                raw_rows=65,
                identical_replays=63,
                exact_wire_bytes=True,
                no_canonical_receipt=True,
            ),
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
