import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from central.erg_central.ldp_stage import StageProcessor, bounded_insert
from central.erg_central.receiver import Conflict, namespace
from erg import delivery_protocol as wire

IDENTITY = dict(
    enrollment_id="synthetic-ldp-stage", installation_id="fixture", destination_generation="gen"
)


def batch():
    item = wire.record_items(
        dict(record_id="fixture", record_revision=1, record_kind="session_event", body="synthetic"),
        1,
    )[0]
    value = dict(
        IDENTITY, protocol_version=1, batch_id="00000000-0000-0000-0000-000000000001", items=[item]
    )
    raw = wire.encode(value)
    return raw, dict(
        IDENTITY,
        batch_id=value["batch_id"],
        batch_sha256=wire.digest(raw),
        accepted_delivery_seqs=[1],
    )


class Client:
    def __init__(self):
        self.requests = []
        self.metrics = {}

    def insert(self, rows):
        self.requests.append(rows)


class StageOnly(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.client = Client()
        self.processor = StageProcessor(
            None,
            self.client,
            "erg",
            "ingest",
            "telemetry",
            self.root / "ldp-staging",
            "erg.telemetry.synthetic_source",
            IDENTITY,
            "source-id",
        )
        self.appended = []
        self.processor.append = lambda rows: self.appended.extend(rows)
        self.processor.visible = lambda value, wanted: list(wanted.items())

    def test_raw_visibility_and_append_cannot_write_broker_progress(self):
        raw, receipt = batch()
        result = self.processor.process_many([(raw, receipt)])
        self.assertEqual(result["state"], "raw_visible")
        self.assertNotIn("materialized_through", result)
        self.assertEqual(len(self.appended), 1)
        self.assertEqual(self.client.requests, [])
        self.assertFalse((self.root / namespace(IDENTITY) / "status.json").exists())
        private = (
            self.root
            / "ldp-staging"
            / namespace(IDENTITY)
            / "batches"
            / (receipt["batch_id"] + ".json")
        )
        self.assertEqual(wire.decode(private.read_bytes())["state"], "raw_visible")

    def test_completed_legacy_prefix_is_cached_without_replaying_source_rows(self):
        self.processor.after_sequence = 1
        self.processor.visible = lambda *_: self.fail("legacy data must not be queried again")
        raw, receipt = batch()
        result = self.processor.process_many([(raw, receipt)])
        self.assertEqual(result["cohort_items"], 0)
        self.assertEqual(result["legacy_covered_items"], 1)
        self.assertEqual(self.appended, [])
        self.assertEqual(self.client.requests, [])
        private = (
            self.processor.control
            / namespace(IDENTITY)
            / "batches"
            / (receipt["batch_id"] + ".json")
        )
        self.assertEqual(wire.decode(private.read_bytes())["legacy_completed_through"], 1)
        self.assertFalse((self.root / namespace(IDENTITY) / "status.json").exists())

    def test_batch_crossing_boundary_admits_only_new_items(self):
        raw, receipt = batch()
        value = wire.decode(raw)
        value["items"] += wire.record_items(
            dict(
                record_id="second", record_revision=1, record_kind="session_event", body="synthetic"
            ),
            2,
        )
        raw = wire.encode(value)
        receipt.update(batch_sha256=wire.digest(raw), accepted_delivery_seqs=[1, 2])
        self.processor.after_sequence = 1
        result = self.processor.process_many([(raw, receipt)])
        self.assertEqual(result["legacy_covered_items"], 1)
        self.assertEqual([row["delivery_seq"] for row in self.appended], [2])

    def test_missing_visibility_and_failed_append_do_not_complete_staging(self):
        with patch("central.erg_central.ldp_stage.await_visibility", return_value=False):
            self.assertEqual(
                self.processor.process_many([batch()])["state"], "awaiting_delta_visibility"
            )
        self.assertEqual(self.appended, [])

        def failed(_):
            raise RuntimeError("append failed")

        self.processor.append = failed
        with self.assertRaises(RuntimeError):
            self.processor.process_many([batch()])
        self.assertFalse((self.root / "ldp-staging" / namespace(IDENTITY) / "batches").exists())

    def test_cross_scope_and_nonprivate_control_root_are_refused(self):
        self.processor.identity = dict(IDENTITY, installation_id="foreign")
        with self.assertRaises(Conflict):
            self.processor.process_many([batch()])
        with self.assertRaises(ValueError):
            StageProcessor(
                None,
                self.client,
                "erg",
                "ingest",
                "telemetry",
                self.root,
                "erg.telemetry.source",
                IDENTITY,
                "source-id",
            )

    def test_insert_chunks_account_for_encoded_json_expansion(self):
        rows = [dict(text='"' * 300000) for _ in range(5)]
        bounded_insert(self.client, rows)
        self.assertGreater(len(self.client.requests), 1)
        self.assertEqual(sum(len(v) for v in self.client.requests), len(rows))
        for request in self.client.requests:
            self.assertLessEqual(len(wire.encode(dict(records=request))), wire.MAX_BATCH)


class StagingFactory(unittest.TestCase):
    def test_finite_sweep_uses_explicit_processor_factory(self):
        from central.erg_central.processor import run

        with tempfile.TemporaryDirectory() as directory:
            factory = Mock()
            result = run(
                None,
                Client(),
                Path(directory) / "spool",
                Path(directory) / "control",
                "erg",
                max_seconds=0,
                processor_factory=factory,
            )
            factory.assert_called_once()
            self.assertEqual(result["attempted_batches"], 0)
