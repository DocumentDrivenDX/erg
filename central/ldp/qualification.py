"""Synthetic LDP fixtures and exact output comparisons against the SQLite oracle.

No cloud access, production identities, credentials or handwritten Spark graph.
"""

import sqlite3

from central.erg_central.canonical import Canonical
from central.erg_central.disposition import raw_disposition
from central.erg_central.receiver import namespace
from central.ldp.reporting_fixture import payloads
from erg import delivery_protocol as wire

IDENTITY = dict(
    enrollment_id="synthetic-ldp-qualification",
    installation_id="fabricated",
    destination_generation="synthetic-only-v1",
)


def fixture():
    items = []
    for n in range(96):
        payload = dict(
            record_id="synthetic-session-" + str(n),
            record_revision=1,
            record_kind="session_event",
            tool=("claude", "codex", "pi")[n % 3],
            session="fabricated-" + str(n // 3),
            body=dict(text="synthetic fixture", ordinal=n),
        )
        items.extend(wire.record_items(payload, len(items) + 1))
    for revision in (3, 1, 2):
        items.extend(
            wire.record_items(
                dict(
                    record_id="out-of-order",
                    record_revision=revision,
                    record_kind="session_event",
                    body=dict(revision=revision),
                ),
                len(items) + 1,
            )
        )
    for payload in payloads():
        items.extend(wire.record_items(payload, len(items) + 1))
    items.extend(
        wire.record_items(
            dict(
                record_id="chunked",
                record_revision=1,
                record_kind="session_event",
                body="synthetic " * (wire.CHUNK_BYTES // 8),
            ),
            len(items) + 1,
        )
    )
    return items


def raw_rows(items):
    return [
        dict(
            IDENTITY,
            **{k: i[k] for k in wire.IDENTITY + ("delivery_seq", "record_sha256")},
            batch_id="00000000-0000-0000-0000-000000000001",
            item_json=wire.encode(i).decode(),
        )
        for i in items
    ]


def expected_output(pages):
    """Reference facts/heads/bytes over immutable, scoped synthetic updates."""
    reference = Canonical(sqlite3.connect(":memory:"))
    unique = {}
    try:
        for items in pages:
            reference.apply(dict(IDENTITY, items=items))
            for item in items:
                previous = unique.get(item["delivery_seq"])
                if previous is not None and previous != item:
                    raise AssertionError("oracle input sequence conflict")
                unique[item["delivery_seq"]] = item
        facts = {}
        heads = {}
        for ident, revision, sha, raw in reference.db.execute(
            "SELECT id,revision,sha256,payload FROM canonical_revisions WHERE namespace=? ORDER BY id,revision",
            (namespace(IDENTITY),),
        ):
            payload = wire.decode(raw)
            facts[ident, revision] = dict(
                record_sha256=sha, payload_json=raw.decode(), disposition=raw_disposition(payload)
            )
            if ident not in heads or revision > heads[ident][0]:
                heads[ident] = (revision, facts[ident, revision])
        return dict(
            items=unique,
            facts=facts,
            heads={(ident, revision): value for ident, (revision, value) in heads.items()},
        )
    finally:
        reference.db.close()


def compare_output(sequence, archive, latest, expected):
    """Reject extras, duplicates, scope drift, payload loss and latest-only archives."""
    for row in sequence + archive + latest:
        if any(row.get(key) != value for key, value in IDENTITY.items()):
            raise AssertionError("native output scope differs")
    if any(row["event_type"] == "conflict" for row in sequence + archive):
        raise AssertionError("healthy fixture produced conflict evidence")
    decisions = {}
    for row in sequence:
        if row["event_type"] != "accepted_item" or row["delivery_sequence"] in decisions:
            raise AssertionError("duplicate or unexpected sequence decision")
        decisions[row["delivery_sequence"]] = row["item_json"]
    if decisions != {seq: wire.encode(item).decode() for seq, item in expected["items"].items()}:
        raise AssertionError("native sequence bytes differ")

    def facts(rows, allow_pending):
        result = {}
        for row in rows:
            if allow_pending and row["event_type"] == "pending":
                continue
            key = row["record_id"], row["record_revision"]
            if row["event_type"] != "revision" or key in result:
                raise AssertionError("duplicate or unexpected native revision event")
            result[key] = {
                name: row[name] for name in ("record_sha256", "payload_json", "disposition")
            }
        return result

    if facts(archive, True) != expected["facts"]:
        raise AssertionError("native immutable archive differs")
    if facts(latest, False) != expected["heads"]:
        raise AssertionError("native current heads differ")
    return dict(
        unique_items=len(decisions),
        immutable_revisions=len(expected["facts"]),
        latest_heads=len(expected["heads"]),
        exact_wire_payload_hash_disposition=True,
    )


def fault_items():
    """Conflicts are separate from healthy checkpoints and preserve earlier facts."""
    original = fixture()[0]
    replacement = wire.record_items(
        dict(
            record_id=original["record_id"],
            record_revision=1,
            record_kind="session_event",
            body="synthetic changed bytes",
        ),
        1,
    )[0]
    revision_conflict = dict(replacement, delivery_seq=1001)
    malformed = dict(original, delivery_seq=1002, payload_json="invalid fabricated JSON")
    return dict(
        sequence_conflict=replacement, revision_conflict=revision_conflict, malformed=malformed
    )


def capacity_items(count=8398, body_bytes=3600):
    """Comparable item count and source bytes; never copied production bodies."""
    if not 1 <= count <= 64000 or not 0 <= body_bytes <= 8192:
        raise ValueError("synthetic capacity fixture exceeds bounds")
    return [
        wire.record_items(
            dict(
                record_id="capacity-" + str(n),
                record_revision=1,
                record_kind="session_event",
                body="x" * body_bytes,
            ),
            n + 1,
        )[0]
        for n in range(count)
    ]


def edge_items():
    """Large integer identity, late aliases/chunks, and an unknown projection."""
    items = fixture()
    seq = len(items) + 1
    unknown = wire.record_items(
        dict(
            record_id="unknown-projection",
            record_revision=1,
            record_kind="usage_evidence",
            body=dict(schema_version=999),
        ),
        seq,
    )
    alias = dict(items[0], delivery_seq=seq + 1)
    duplicate_chunk = dict(items[-1], delivery_seq=seq + 2)
    wide = wire.record_items(
        dict(
            record_id="wide-integer",
            record_revision=2**53 + 3,
            record_kind="session_event",
            body="fabricated wide identity",
        ),
        2**53 + 1,
    )
    return unknown + [alias, duplicate_chunk] + wide


def scope_fault_rows():
    """Foreign installation, corrupt chunk digest and outer identity mismatch."""
    items = fixture()
    foreign = dict(items[0], delivery_seq=1003)
    corrupt = dict(items[-1], delivery_seq=1004, chunk_sha256="0" * 64)
    envelope = dict(items[0], delivery_seq=1005)
    rows = raw_rows([foreign, corrupt, envelope])
    rows[0]["installation_id"] = "other-fabricated-installation"
    rows[2]["record_revision"] = 2
    return rows
