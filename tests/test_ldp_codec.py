import sqlite3
import unittest

from central.erg_central.canonical import Canonical
from central.erg_central.ldp_codec import (
    certify_prefix,
    checked_item,
    reconstruct_revision,
    unique_items,
)
from central.erg_central.receiver import Conflict, namespace
from erg import delivery_protocol as w

IDENTITY = dict(
    enrollment_id="synthetic-ldp", installation_id="fixture", destination_generation="g"
)


def make(ident="r", revision=1, body="synthetic", seq=1, kind="session_event"):
    return w.record_items(
        dict(record_id=ident, record_revision=revision, record_kind=kind, body=body), seq
    )


def row(item):
    return dict(
        IDENTITY,
        **{k: item[k] for k in w.IDENTITY + ("delivery_seq", "record_sha256")},
        item_json=w.encode(item).decode(),
    )


def prior(materialized=0, projected=0):
    return dict(materialized_through=materialized, projected_through=projected)


def fact(items):
    return reconstruct_revision([w.encode(i).decode() for i in items])


class LdpRevisionCodec(unittest.TestCase):
    def test_inline_and_cross_batch_chunks_match_reference_exactly(self):
        items = make() + make("chunked", body="x" * w.CHUNK_BYTES, seq=2)
        reference = Canonical(sqlite3.connect(":memory:"))
        for page in (items[:2], items[2:], items):
            reference.apply(dict(IDENTITY, items=page))
        for ident, group in (("r", items[:1]), ("chunked", items[1:])):
            result = fact(group)
            stored = reference.db.execute(
                "SELECT sha256,payload FROM canonical_revisions WHERE namespace=? AND id=?",
                (namespace(IDENTITY), ident),
            ).fetchone()
            self.assertEqual((result["record_sha256"], result["payload_json"].encode()), stored)

    def test_incomplete_chunk_does_not_advance_receipt(self):
        items = make(body="x" * w.CHUNK_BYTES)
        pending = fact(items[:1])
        self.assertEqual(pending["state"], "pending")
        self.assertEqual(
            certify_prefix(IDENTITY, prior(), [row(items[0])], {("r", 1): pending})[
                "materialized_through"
            ],
            0,
        )

    def test_repeated_delivery_dedupes_before_reconstruction(self):
        item = make()[0]
        self.assertEqual(unique_items([row(item)] * 3, IDENTITY), [item])
        self.assertEqual(fact([item, item])["state"], "conflict")

    def test_same_sequence_different_bytes_is_conflict(self):
        with self.assertRaises(Conflict):
            unique_items([row(make()[0]), row(make(body="changed")[0])], IDENTITY)

    def test_outer_identity_or_revision_cannot_disagree_with_wire(self):
        for key, value in (("installation_id", "foreign"), ("record_revision", 99)):
            with self.subTest(key=key), self.assertRaises(Conflict):
                checked_item(dict(row(make()[0]), **{key: value}), IDENTITY)

    def test_revision_archive_preserves_out_of_order_versions(self):
        archive = {}
        for revision in (3, 1, 2):
            archive["r", revision] = fact(make(revision=revision, body=str(revision)))
        self.assertEqual(len(archive), 3)
        self.assertEqual(w.decode(archive["r", 1]["payload_json"].encode())["body"], "1")
        self.assertNotEqual(archive["r", 1]["record_sha256"], archive["r", 3]["record_sha256"])

    def test_gap_and_unimplemented_projection_stop_independent_prefixes(self):
        items = make() + make("usage", seq=2, kind="host_metric") + make("after", seq=3)
        facts = {(i["record_id"], 1): fact([i]) for i in items}
        self.assertEqual(
            certify_prefix(IDENTITY, prior(), list(map(row, items)), facts),
            dict(materialized_through=3, projected_through=1),
        )
        self.assertEqual(
            certify_prefix(IDENTITY, prior(), [row(items[0]), row(items[2])], facts)[
                "materialized_through"
            ],
            1,
        )

    def test_crash_before_fact_commit_cannot_acknowledge_then_replay_recovers(self):
        item = make()[0]
        self.assertEqual(
            certify_prefix(IDENTITY, prior(), [row(item)], {})["materialized_through"], 0
        )
        facts = {("r", 1): fact([item])}
        self.assertEqual(
            certify_prefix(IDENTITY, prior(), [row(item)], facts)["projected_through"], 1
        )
        self.assertEqual(
            certify_prefix(IDENTITY, prior(1, 1), [row(item)], facts)["materialized_through"], 1
        )

    def test_conflicting_revision_blocks_publication(self):
        item = make()[0]
        with self.assertRaises(Conflict):
            certify_prefix(IDENTITY, prior(), [row(item)], {("r", 1): fact(make(body="other"))})

    def test_corrupt_payload_returns_sanitized_conflict(self):
        item = make()[0]
        item["payload_json"] += "sensitive-content"
        result = fact([item])
        self.assertEqual(
            result, dict(state="conflict", payload_json=None, record_sha256=None, disposition=None)
        )
        self.assertEqual(fact(make() * 129)["state"], "conflict")

    def test_projection_lag_is_independent_and_cannot_skip_prior_gap(self):
        items = make("unresolved", seq=2, kind="host_metric") + make("new", seq=3)
        facts = {(i["record_id"], 1): fact([i]) for i in items}
        with self.assertRaises(Conflict):
            certify_prefix(IDENTITY, prior(2, 1), [row(items[1])], facts)
        result = certify_prefix(IDENTITY, prior(2, 1), list(map(row, items)), facts)
        self.assertEqual(result, prior(3, 1))
        self.assertEqual(certify_prefix(IDENTITY, result, list(map(row, items)), facts), result)
        facts["unresolved", 1]["disposition"] = "qualified-projection-v1"
        self.assertEqual(
            certify_prefix(IDENTITY, result, list(map(row, items)), facts), prior(3, 3)
        )
        with self.assertRaises(Conflict):
            certify_prefix(IDENTITY, prior(1, 2), [], {})

    def test_null_malformed_and_unicode_groups_are_sanitized(self):
        for value in (None, "not-array", [None], [123], ["\ud800"]):
            with self.subTest(value_type=type(value).__name__):
                self.assertEqual(reconstruct_revision(value)["state"], "conflict")
