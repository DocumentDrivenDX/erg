import unittest

from central.ldp.native_output import pinned_rows


class Client:
    def __init__(self, change=None):
        self.change = change
        self.read = False

    def api(self, method, path):
        return dict(table_id="replacement" if self.read and self.change == "identity" else "fixed")

    def sql(self, statement):
        if statement.startswith("DESCRIBE HISTORY"):
            return [[2 if self.read and self.change == "version" else 1]]
        self.read = True
        return [[7]]


class NativeOutputSnapshots(unittest.TestCase):
    def test_stable_current_read_is_fenced_by_table_identity_and_delta_version(self):
        rows, version = pinned_rows(
            Client(), "erg.telemetry.ergqual_fixture", ["delivery_sequence"]
        )
        self.assertEqual(rows, [dict(delivery_sequence=7)])
        self.assertEqual(version, 1)

    def test_concurrent_append_or_replaced_table_invalidates_output_evidence(self):
        for change in ("identity", "version"):
            with self.subTest(change=change), self.assertRaisesRegex(AssertionError, "changed"):
                pinned_rows(Client(change), "erg.telemetry.ergqual_fixture", ["delivery_sequence"])

    def test_generated_synthetic_prefixes_are_allowed_but_production_is_refused(self):
        for prefix in ("", "raw_", "scoped_sequence_decisions_", "revision_fact_events_"):
            pinned_rows(Client(), "erg.telemetry." + prefix + "ergqual_fixture", ["value"])
        with self.assertRaises(ValueError):
            pinned_rows(Client(), "erg.telemetry.records", ["value"])
