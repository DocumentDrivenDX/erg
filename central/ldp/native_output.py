"""Read-only assertions over exact native LDP output snapshots."""

import json
import re

from central.ldp.qualification import (
    IDENTITY,
    capacity_items,
    compare_output,
    edge_items,
    expected_output,
    fixture,
)
from erg import delivery_protocol as wire

TABLE = re.compile(
    r"^erg\.telemetry\.(?:raw_|scoped_sequence_decisions_|revision_fact_events_)?ergqual_[A-Za-z0-9_]+$"
)
SEQUENCE = (
    "enrollment_id",
    "installation_id",
    "destination_generation",
    "event_type",
    "delivery_sequence",
    "record_id",
    "record_revision",
    "content_hash",
    "item_json",
    "reason_code",
    "evidence_json",
)
ARCHIVE = (
    "enrollment_id",
    "installation_id",
    "destination_generation",
    "event_type",
    "record_id",
    "record_revision",
    "record_sha256",
    "payload_json",
    "disposition",
    "reason_code",
    "evidence_json",
)


def targets(document):
    metadata = document["modules"][0]["extensions"]["tablespec.pipeline"]["metadata"]
    if metadata["source"]["identity_scope"] != IDENTITY:
        raise ValueError("native output test requires the exact synthetic identity")
    archive = metadata["metadata"]["ldp"]["archive"]
    result = dict(
        sequence=archive["sequence_decisions_target"],
        archive=archive["target"],
        latest=archive["latest_target"],
        source=metadata["source"]["table"],
    )
    if any(not TABLE.fullmatch(value) for value in result.values()):
        raise ValueError("native output test table is outside synthetic allowlist")
    return result


def snapshot_pin(client, table):
    if not TABLE.fullmatch(table):
        raise ValueError("synthetic table required")
    identity = client.api("GET", "/api/2.1/unity-catalog/tables/" + table)["table_id"]
    version = client.sql("DESCRIBE HISTORY " + table + " LIMIT 1")[0][0]
    if not isinstance(identity, str) or not identity or type(version) is not int or version < 0:
        raise AssertionError("invalid output Delta identity/version")
    return identity, version


def pinned_rows(client, table, columns, limit=1000):
    # Some SQL warehouses reject VERSION AS OF on new streaming tables. A current
    # read fenced by unchanged table identity and monotonically increasing Delta
    # version proves the same snapshot. This is read-only test evidence; the
    # production publisher uses native Spark versionAsOf snapshots.
    before = snapshot_pin(client, table)
    values = client.sql(
        "SELECT " + ",".join(columns) + " FROM " + table + " LIMIT " + str(limit + 1)
    )
    if snapshot_pin(client, table) != before:
        raise AssertionError("native output changed during snapshot read")
    if len(values) > limit:
        raise AssertionError("native output exceeds case bound")
    return [dict(zip(columns, row)) for row in values], before[1]


def assert_completed_update(client, pipeline_id, update_id):
    pipeline = client.api("GET", "/api/2.0/pipelines/" + pipeline_id)
    if (
        pipeline.get("state") != "IDLE"
        or not pipeline.get("latest_updates")
        or pipeline["latest_updates"][0]["update_id"] != update_id
    ):
        raise AssertionError("native output no longer belongs to the requested completed update")
    update = client.api("GET", "/api/2.0/pipelines/" + pipeline_id + "/updates/" + update_id)[
        "update"
    ]
    if update["state"] != "COMPLETED":
        raise AssertionError("native LDP update did not complete")
    return update


def qualify_output(client, document, pipeline_id, update_id, phase):
    assert_completed_update(client, pipeline_id, update_id)
    tables = targets(document)
    sequence, seq_version = pinned_rows(client, tables["sequence"], SEQUENCE)
    archive, archive_version = pinned_rows(client, tables["archive"], ARCHIVE)
    heads, head_version = pinned_rows(client, tables["latest"], ARCHIVE)
    items = fixture()
    pages = [items[:-1]] if phase == 1 else [items[:-1], items[-1:]]
    if phase >= 3:
        pages.append(items)
    if phase == 4:
        pages.append(edge_items())
    if phase not in (1, 2, 3, 4):
        raise ValueError("expected native phase 1, 2, 3 or 4")
    expected = expected_output(pages)
    result = compare_output(sequence, archive, heads, expected)
    offered = client.sql("SELECT count(*),sum(octet_length(item_json)) FROM " + tables["source"])[0]
    result.update(
        offered_items=int(offered[0]),
        offered_wire_bytes=int(offered[1]),
        versions=dict(sequence=seq_version, archive=archive_version, latest=head_version),
        phase=phase,
        native_update_id=update_id,
        worker_runtime_admission_passed=True,
        snapshot_mode="unchanged_table_identity_and_delta_version",
        scope="native synthetic LDP output; no production publication claim",
    )
    assert_completed_update(client, pipeline_id, update_id)
    return result, [
        wire.decode(row["payload_json"].encode())
        for row in archive
        if row["event_type"] == "revision"
    ]


def qualify_faults(client, document, pipeline_id, update_id, scope_faults=False):
    assert_completed_update(client, pipeline_id, update_id)
    tables = targets(document)
    sequence, _ = pinned_rows(client, tables["sequence"], SEQUENCE)
    archive, _ = pinned_rows(client, tables["archive"], ARCHIVE)
    reasons = {row["reason_code"] for row in sequence + archive if row["event_type"] == "conflict"}
    expected = {"DELIVERY_SEQUENCE_CONFLICT", "IMMUTABLE_REVISION_CONFLICT", "VALIDATION_REJECTED"}
    if not expected.issubset(reasons):
        raise AssertionError("native fault evidence missing")
    if scope_faults:
        rejected = {
            row["delivery_sequence"]
            for row in sequence
            if row["event_type"] == "conflict" and row["reason_code"] == "VALIDATION_REJECTED"
        }
        if not {1004, 1005}.issubset(rejected):
            raise AssertionError("native in-scope hash/envelope rejection missing")
        if any(row["delivery_sequence"] == 1003 for row in sequence):
            raise AssertionError("foreign installation crossed the declared source selector")
    original = fixture()[0]
    facts = [row for row in archive if row["event_type"] == "revision"]
    if (
        len(facts) != 1
        or facts[0]["payload_json"] != original["payload_json"]
        or facts[0]["record_sha256"] != original["record_sha256"]
    ):
        raise AssertionError("later conflicts changed previously appended immutable fact")
    for row in sequence + archive:
        evidence = row.get("evidence_json")
        if evidence and (
            "synthetic changed bytes" in evidence or "payload_json" in json.loads(evidence)
        ):
            raise AssertionError("conflict evidence contains source body")
    return dict(
        native_conflict_reasons=sorted(reasons),
        prior_revision_retained=True,
        full_scope_isolation_checked=scope_faults,
        publication_must_block=True,
    )


def qualify_capacity(client, document, pipeline_id, update_id, count=8398):
    assert_completed_update(client, pipeline_id, update_id)
    tables = targets(document)
    pins = {key: snapshot_pin(client, value) for key, value in tables.items()}
    items = capacity_items(count)
    expected = {item["delivery_seq"]: wire.digest(wire.encode(item)) for item in items}
    actual = client.sql(
        "SELECT delivery_sequence,sha2(encode(item_json,'UTF-8'),256) FROM "
        + tables["sequence"]
        + " WHERE event_type='accepted_item' ORDER BY delivery_sequence LIMIT "
        + str(count + 1)
    )
    if dict(actual) != expected or len(actual) != len(expected):
        raise AssertionError("native capacity sequence bytes differ")
    expected_facts = {item["record_id"]: item["record_sha256"] for item in items}
    actual = client.sql(
        "SELECT record_id,record_sha256,sha2(encode(payload_json,'UTF-8'),256) FROM "
        + tables["archive"]
        + " WHERE event_type='revision' LIMIT "
        + str(count + 1)
    )
    if (
        {ident: sha for ident, sha, payload_sha in actual} != expected_facts
        or len(actual) != len(expected_facts)
        or any(sha != payload_sha for ident, sha, payload_sha in actual)
    ):
        raise AssertionError("native capacity immutable facts differ")
    conflicts = client.sql(
        "SELECT count(*) FROM " + tables["archive"] + " WHERE event_type='conflict'"
    )[0][0]
    if conflicts:
        raise AssertionError("capacity fixture produced conflicts")
    if pins != {key: snapshot_pin(client, value) for key, value in tables.items()}:
        raise AssertionError("capacity output changed during snapshot read")
    assert_completed_update(client, pipeline_id, update_id)
    return dict(
        snapshot_mode="unchanged_table_identity_and_delta_version",
        unique_items=len(items),
        wire_bytes=sum(len(wire.encode(item)) for item in items),
        exact_sequence_and_fact_hash_parity=True,
    )
