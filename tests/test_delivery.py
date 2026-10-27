"""Delivery failure invariants use only synthetic private temporary records."""

import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from central.erg_central.canonical import Canonical
from central.erg_central.receiver import Conflict, FileStore, Forbidden, Receiver
from erg import delivery_journal as journal
from erg import delivery_protocol as wire
from erg import history, store

IDENTITY = dict(
    enrollment_id="fixture-device",
    installation_id="fixture-install",
    destination_generation="fixture-generation",
)


def payload(revision=1, text="synthetic fixture", kind="session_event"):
    return dict(
        record_id="fixture-record",
        record_revision=revision,
        record_kind=kind,
        tool="pi",
        session="fixture-session",
        body=None if kind == "content_tombstone" else dict(content=text),
    )


class Protocol(unittest.TestCase):
    def test_executable_fixture_and_digests(self):
        root = Path(__file__).parent / "fixtures" / "delivery"
        raw = (root / "batch.json").read_bytes()
        sha = (root / "batch.sha256").read_text().strip()
        value = wire.validate_batch(raw, sha)
        self.assertEqual(wire.reconstruct(value["items"]), payload())

    def test_noncanonical_json_bytes_are_preserved_with_their_digest(self):
        native = json.dumps(payload(), indent=2).encode()
        item = wire.record_items(payload(), 1)[0]
        item.update(payload_json=native.decode(), record_sha256=wire.digest(native))
        self.assertEqual(wire.reconstruct([item], as_bytes=True), native)
        self.assertEqual(wire.reconstruct([item]), payload())

    def test_chunk_reorder_loss_and_manifest_corruption(self):
        record = payload(text="世" * 300000)
        items = wire.record_items(record, 1)
        self.assertGreater(len(items), 1)
        self.assertEqual(wire.reconstruct(list(reversed(items))), record)
        with self.assertRaisesRegex(wire.ProtocolError, "incomplete"):
            wire.reconstruct(items[:-1])
        changed = copy.deepcopy(items)
        changed[0]["record_bytes"] += 1
        with self.assertRaises(wire.ProtocolError):
            wire.reconstruct(changed)
        changed = copy.deepcopy(items)
        changed[-1]["chunk_sha256"] = "0" * 64
        with self.assertRaises(wire.ProtocolError):
            wire.reconstruct(changed)

    def test_duplicate_keys_nonfinite_boolean_sequences_and_mixed_identity_rejected(self):
        for raw in (b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":1e999}'):
            with self.assertRaises(wire.ProtocolError):
                wire.decode(raw)
        item = wire.record_items(payload(), 1)[0]
        item["delivery_seq"] = True
        with self.assertRaises(wire.ProtocolError):
            wire.validate_item(item)
        item = wire.record_items(payload(), 1)[0]
        item["record_id"] = "other"
        with self.assertRaises(wire.ProtocolError):
            wire.validate_item(item)

    def test_invalid_digest_order_unknown_fields_and_payload_chunk_mix(self):
        items = wire.record_items(payload(), 1)
        raw = wire.batch(IDENTITY, items)
        with self.assertRaises(wire.ProtocolError):
            wire.validate_batch(raw, "0" * 64)
        items[0]["chunk_count"] = 1
        with self.assertRaises(wire.ProtocolError):
            wire.batch(IDENTITY, items)
        with self.assertRaises(wire.ProtocolError):
            wire.batch(IDENTITY, wire.record_items(payload(), 2) + wire.record_items(payload(2), 1))


class Delivery(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.database = str(self.path / "local.db")
        self.db = store.connect(self.database)
        self.addCleanup(lambda: self.db.close())
        self.receiver = Receiver(FileStore(self.path / "spool"))
        self.identity = dict(IDENTITY, active=True)
        central_db = sqlite3.connect(str(self.path / "canonical.db"))
        self.addCleanup(central_db.close)
        self.canonical = Canonical(central_db)

    def put(self, name="new", text="synthetic fixture"):
        return history.put(
            self.db,
            "pi",
            "fixture-session",
            "project",
            1000,
            "message",
            dict(type="message", message=dict(role="user", content=text)),
            name,
            machine="fixture-machine",
        )[0]

    def flush(self):
        while journal.prepare_one(self.db):
            pass
        return journal.next_batch(self.db)

    def test_default_disabled_and_boundary_is_forward_only_including_repairs(self):
        with self.db:
            old = self.put("old")
        self.assertEqual(journal.status(self.db)["state"], "disabled")
        journal.enroll(self.db, IDENTITY)
        with self.db:
            self.db.execute("UPDATE history_events SET project='repaired' WHERE event_id=?", (old,))
            self.put("old", "corrected pre-enrollment body")
        self.assertIsNone(self.flush())
        with self.db:
            self.put("new")
        self.assertIsNotNone(self.flush())
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM delivery_journal").fetchone()[0], 1)

    def test_capture_revision_and_outbox_rollback_atomically(self):
        journal.enroll(self.db, IDENTITY)
        try:
            with self.db:
                self.put()
                raise RuntimeError("abort")
        except RuntimeError:
            pass
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM delivery_journal").fetchone()[0], 0)
        with self.db:
            key = self.put()
        with self.db:
            self.db.execute(
                "UPDATE history_events SET project='corrected' WHERE event_id=?", (key,)
            )
        self.assertEqual(
            self.db.execute("SELECT revision FROM delivery_journal ORDER BY id").fetchall(),
            [(1,), (2,)],
        )
        self.assertEqual(
            self.db.execute("SELECT COUNT(DISTINCT source_seq) FROM delivery_journal").fetchone()[
                0
            ],
            1,
        )

    def test_restart_lost_ack_and_missed_notification_roundtrip(self):
        journal.enroll(self.db, IDENTITY)
        with self.db:
            self.put()
        batch_id, raw, sha = self.flush()
        receipt = self.receiver.accept(raw, sha, self.identity)
        self.db.close()
        self.db = store.connect(self.database)
        self.assertEqual(journal.next_batch(self.db), (batch_id, raw, sha))
        self.assertEqual(self.receiver.accept(raw, sha, self.identity), receipt)
        journal.accept(self.db, batch_id, receipt)
        self.assertEqual(journal.status(self.db)["accepted_through"], 1)
        self.assertEqual(journal.status(self.db)["materialized_through"], 0)
        # No notification is delivered: a fresh scan still finds the accepted body.
        fresh = Receiver(FileStore(self.path / "spool"))
        batches = list(fresh.batches())
        self.assertEqual(len(batches), 1)
        for value, _ in batches:
            self.canonical.apply(value)
            self.canonical.apply(value)
        self.assertEqual(
            self.canonical.db.execute("SELECT COUNT(*) FROM canonical_heads").fetchone()[0], 1
        )
        self.assertEqual(self.canonical.status(IDENTITY)["materialized_through"], 1)
        stored = wire.decode(
            self.canonical.db.execute("SELECT payload FROM canonical_heads").fetchone()[0]
        )
        self.assertEqual(stored["body"]["message"]["content"], "synthetic fixture")
        self.assertNotIn("reported_cost", stored)

    def test_receiver_crash_before_commit_cannot_ack_and_replay_recovers(self):
        raw = wire.batch(IDENTITY, wire.record_items(payload(), 1))
        sha = wire.digest(raw)
        backend = self.receiver.store
        original = backend.put_once

        def fail_marker(key, data):
            if key.endswith("accepted.json"):
                raise OSError("synthetic interruption")
            return original(key, data)

        backend.put_once = fail_marker
        with self.assertRaises(OSError):
            self.receiver.accept(raw, sha, self.identity)
        self.assertEqual(list(self.receiver.batches()), [])
        backend.put_once = original
        self.receiver.accept(raw, sha, self.identity)
        self.assertEqual(len(list(self.receiver.batches())), 1)

    def test_500_item_batch_uses_one_claim_write_and_replay_uses_none(self):
        items = []
        for seq in range(1, 501):
            record = payload()
            record["record_id"] = "fixture-" + str(seq)
            items.extend(wire.record_items(record, seq))
        raw = wire.batch(IDENTITY, items)
        self.receiver.accept(raw, wire.digest(raw), self.identity)
        self.assertEqual(self.receiver.metrics["claim_items"], 500)
        self.assertEqual(self.receiver.metrics["claim_writes"], 1)
        self.receiver.accept(raw, wire.digest(raw), self.identity)
        self.assertEqual(self.receiver.metrics["claim_writes"], 0)
        self.assertTrue(self.receiver.metrics["replay"])

    def test_cas_race_preserves_prior_claims_instead_of_overwriting(self):
        backend = self.receiver.store
        original = backend.compare_swap
        injected = []
        extra = wire.record_items(dict(payload(), record_id="other-record"), 512)[0]

        def compete(key, expected, raw):
            if not injected:
                injected.append(True)
                self.assertTrue(
                    original(key, expected, wire.encode({"512": wire.digest(wire.encode(extra))}))
                )
            return original(key, expected, raw)

        backend.compare_swap = compete
        raw = wire.batch(IDENTITY, wire.record_items(payload(), 1))
        self.receiver.accept(raw, wire.digest(raw), self.identity)
        self.assertEqual(self.receiver.metrics["claim_cas_conflicts"], 1)
        index = [
            key
            for key in backend.keys("v1/")
            if "/sequence-shards/" in key and key.endswith(".json")
        ][0]
        self.assertEqual(set(wire.decode(backend.get(index))), {"1", "512"})

    def test_conflicting_sequence_across_batches_is_not_accepted(self):
        first = wire.batch(IDENTITY, wire.record_items(payload(), 1))
        self.receiver.accept(first, wire.digest(first), self.identity)
        second = wire.batch(IDENTITY, wire.record_items(payload(2), 1))
        with self.assertRaises(Conflict):
            self.receiver.accept(second, wire.digest(second), self.identity)
        self.assertEqual(len(list(self.receiver.batches())), 1)

    def test_revocation_generation_binding_and_cross_enrollment_receipts(self):
        raw = wire.batch(IDENTITY, wire.record_items(payload(), 1))
        for identity in (
            dict(self.identity, active=False),
            dict(self.identity, installation_id="other"),
            dict(self.identity, destination_generation="other"),
        ):
            with self.assertRaises(Forbidden):
                self.receiver.accept(raw, wire.digest(raw), identity)
        self.receiver.accept(raw, wire.digest(raw), self.identity)
        self.assertEqual(
            list(self.receiver.receipts(dict(self.identity, enrollment_id="other"))), []
        )
        with self.assertRaises(ValueError):
            journal.enroll(
                self.db, dict(IDENTITY, installation_id="bad", destination_generation="")
            )
        journal.enroll(self.db, IDENTITY)
        with self.assertRaises(ValueError):
            journal.enroll(self.db, dict(IDENTITY, destination_generation="new"))

    def test_receipt_mismatch_does_not_advance_local_cursor(self):
        journal.enroll(self.db, IDENTITY)
        with self.db:
            self.put()
        batch_id, raw, sha = self.flush()
        receipt = self.receiver.accept(raw, sha, self.identity)
        for key, value in (
            ("batch_sha256", "bad"),
            ("enrollment_id", "other"),
            ("accepted_delivery_seqs", []),
        ):
            with self.assertRaises(wire.ProtocolError):
                journal.accept(self.db, batch_id, dict(receipt, **{key: value}))
            self.assertEqual(journal.status(self.db)["accepted_through"], 0)

    def test_pending_content_is_protected_from_expiration(self):
        journal.enroll(self.db, IDENTITY)
        with self.db:
            key = self.put()
        self.flush()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "pending materialization"):
            with self.db:
                self.db.execute(
                    "UPDATE history_events SET body=NULL,expired_at=2000 WHERE event_id=?", (key,)
                )

    def test_verified_status_allows_only_later_explicit_tombstone(self):
        journal.enroll(self.db, IDENTITY)
        with self.db:
            key = self.put()
        ident, raw, sha = self.flush()
        receipt = self.receiver.accept(raw, sha, self.identity)
        journal.accept(self.db, ident, receipt)
        value = wire.validate_batch(raw, sha)
        remote = self.canonical.apply(value)
        journal.reconcile_status(self.db, remote)
        with self.db:
            self.db.execute(
                "UPDATE history_events SET body=NULL,expired_at=2000 WHERE event_id=?", (key,)
            )
        self.assertTrue(journal.prepare_one(self.db))
        tombstone = wire.decode(
            self.db.execute(
                "SELECT item FROM delivery_items ORDER BY delivery_seq DESC LIMIT 1"
            ).fetchone()[0]
        )
        self.assertEqual(tombstone["record_revision"], 2)
        self.assertEqual(wire.reconstruct([tombstone])["record_kind"], "content_tombstone")
        self.assertIsNone(wire.reconstruct([tombstone])["body"])

    def test_chunk_completion_gaps_and_tombstone_revision_replay(self):
        items = wire.record_items(payload(text="x" * 900000), 2)

        def apply(values):
            raw = wire.batch(IDENTITY, values)
            return self.canonical.apply(wire.validate_batch(raw, wire.digest(raw)))

        apply([items[0]])
        self.assertEqual(
            self.canonical.db.execute("SELECT COUNT(*) FROM canonical_heads").fetchone()[0], 0
        )
        apply(items[1:])
        self.assertEqual(
            self.canonical.status(IDENTITY)["materialized_through"], 0
        )  # missing seq 1
        # Tombstone supersedes content even when an older accepted record arrives late.
        apply(wire.record_items(payload(2, "", "content_tombstone"), 4))
        apply(wire.record_items(payload(text="x" * 900000), 5))
        head = self.canonical.db.execute("SELECT revision,kind FROM canonical_heads").fetchone()
        self.assertEqual(head, (2, "content_tombstone"))

    def test_pending_revision_status_does_not_scan_completed_history(self):
        journal.enroll(self.db, IDENTITY)
        with self.db:
            self.db.executemany(
                "INSERT INTO delivery_journal(enrollment_id,installation_id,destination_generation,event_id,revision,source_seq,snapshot,captured_at,prepared) VALUES('fixture-device','fixture-install','fixture-generation',?,1,0,'{}',1,?)",
                [(str(n), 1) for n in range(20000)]
                + [("pending", 0), ("failed", -1), ("future", 2)],
            )
        ticks = []
        self.db.set_progress_handler(lambda: ticks.append(1) or 0, 100)
        try:
            status = journal.status(self.db)
        finally:
            self.db.set_progress_handler(None, 0)
        self.assertEqual(status["pending_revisions"], 3)
        self.assertEqual(status["blocked_revisions"], 1)
        self.assertLess(len(ticks), 100, "Status scanned completed journal history")

    def test_batch_selection_does_not_scan_delivered_history_or_other_enrollments(self):
        journal.enroll(self.db, IDENTITY)
        with self.db:
            self.db.executemany(
                "INSERT INTO delivery_items(enrollment_id,destination_generation,delivery_seq,journal_id,item,batch_id,accepted,materialized,projected) VALUES('fixture-device','fixture-generation',?,0,'{}','archived',1,1,1)",
                [(n,) for n in range(100, 20100)],
            )
            self.db.execute(
                "INSERT INTO delivery_items(enrollment_id,destination_generation,delivery_seq,journal_id,item) VALUES('other-device','fixture-generation',1,0,'{}')"
            )
            self.put()
        journal.prepare_one(self.db)
        ticks = []
        self.db.set_progress_handler(lambda: ticks.append(1) or 0, 100)
        try:
            batch = journal.next_batch(self.db)
        finally:
            self.db.set_progress_handler(None, 0)
        self.assertLess(len(ticks), 100, "Batch selection scanned delivered history")
        self.assertEqual([r["delivery_seq"] for r in wire.decode(batch[1])["items"]], [1])
        self.assertIsNone(
            self.db.execute(
                "SELECT batch_id FROM delivery_items WHERE enrollment_id='other-device'"
            ).fetchone()[0]
        )


if __name__ == "__main__":
    unittest.main()
