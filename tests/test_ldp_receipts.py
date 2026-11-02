import copy
import tempfile
import unittest
from pathlib import Path

from central.erg_central.ldp_driver import validate_config
from central.erg_central.ldp_publish import read_previous
from central.erg_central.receiver import Conflict
from erg import delivery_protocol as wire

IDENTITY = dict(
    enrollment_id="synthetic-receipt", installation_id="fixture", destination_generation="v1"
)


class PriorReceipt(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "status.json"
        self.admission = dict(
            materialized_through=20,
            projected_through=5,
            raw_version=7,
            source_table_id="source-id",
            raw_table_id="raw-id",
            raw_table="erg.ingest.raw",
        )
        self.config = dict(mode="production", admission=self.admission)

    def put(self, mat=20, proj=5, scoped=True):
        value = dict(materialized_through=mat, projected_through=proj)
        if scoped:
            value.update(
                enrollment_id=IDENTITY["enrollment_id"],
                destination_generation=IDENTITY["destination_generation"],
            )
        self.path.write_bytes(wire.encode(value))
        return value

    def test_missing_production_receipt_never_starts_a_replacement_zero_prefix(self):
        with self.assertRaisesRegex(Conflict, "unavailable"):
            read_previous(self.path, IDENTITY, self.config)
        self.assertFalse(self.path.exists())

    def test_declared_floor_preserves_independent_lag_and_allows_forward_progress(self):
        for mat, proj in ((20, 5), (21, 5), (21, 21)):
            expected = self.put(mat, proj)
            self.assertEqual(read_previous(self.path, IDENTITY, self.config), expected)
        for mat, proj in ((19, 5), (20, 4)):
            self.put(mat, proj)
            with self.assertRaisesRegex(Conflict, "migration boundary"):
                read_previous(self.path, IDENTITY, self.config)

    def test_unscoped_or_invalid_production_receipts_are_refused(self):
        self.put(scoped=False)
        with self.assertRaises(Conflict):
            read_previous(self.path, IDENTITY, self.config)
        for mat, proj in ((True, 0), (20, 21), (-1, 0)):
            self.put(mat, proj)
            with self.assertRaises(Conflict):
                read_previous(self.path, IDENTITY, self.config)

    def test_fresh_synthetic_prefix_can_start_at_zero(self):
        self.assertEqual(
            read_previous(self.path, IDENTITY, dict(mode="synthetic")),
            dict(materialized_through=0, projected_through=0),
        )

    def test_production_configuration_requires_pinned_legacy_admission(self):
        config = dict(
            version=1,
            mode="production",
            identity=IDENTITY,
            control="/Volumes/erg/ingest/control",
            source_table_id="source-id",
            admission=self.admission,
            code_manifest={},
            code_sha256=wire.digest(wire.encode({})),
        )
        validate_config(config)
        for change in (
            None,
            dict(self.admission, projected_through=21),
            dict(self.admission, raw_version=True),
            dict(self.admission, source_table_id="changed"),
        ):
            value = copy.deepcopy(config)
            value["admission"] = change or {}
            with self.assertRaises(Conflict):
                validate_config(value)
