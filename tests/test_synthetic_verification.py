import unittest

from central.erg_central.verify import compare
from erg import delivery_protocol as wire


class SyntheticVerification(unittest.TestCase):
    def fixture(self):
        rows = []
        expected = []
        for revision in (1, 2):
            payload = dict(
                record_id="synthetic",
                record_revision=revision,
                record_kind="session_event",
                source_seq=1,
                tool="pi",
                body={"content": "synthetic-" + str(revision)},
            )
            raw = wire.encode(payload)
            sha = wire.digest(raw)
            rows.append(
                dict(
                    record_id="synthetic",
                    record_revision=revision,
                    record_kind="session_event",
                    payload_json=raw.decode(),
                    record_sha256=sha,
                )
            )
            expected.append(
                dict(record_id="synthetic", record_revision=revision, record_sha256=sha)
            )
        return dict(
            kind="erg-synthetic-qualification-v1",
            actual_session_data=False,
            expected_revisions=expected,
        ), rows

    def test_exact_revision_hashes_and_native_order_are_verified(self):
        manifest, rows = self.fixture()
        result = compare(manifest, rows)
        self.assertTrue(result["verified"])
        self.assertEqual(result["canonical_revisions"], 2)
        self.assertEqual(result["expected_heads"], 1)

    def test_missing_duplicate_and_corrupt_records_remain_failures(self):
        manifest, rows = self.fixture()
        for bad in (rows[:-1], rows + [rows[0]], [dict(rows[0], payload_json="{}"), rows[1]]):
            with self.assertRaises(ValueError):
                compare(manifest, bad)
        with self.assertRaises(ValueError):
            compare(dict(manifest, actual_session_data=True), rows)
