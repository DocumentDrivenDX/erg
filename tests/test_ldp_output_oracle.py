import copy
import unittest

from central.ldp.qualification import IDENTITY, compare_output, expected_output, fixture
from erg import delivery_protocol as wire


class OutputOracle(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.items = fixture()
        cls.expected = expected_output([cls.items[:-1], cls.items[-1:], cls.items])

    def rows(self):
        expected = self.expected
        sequence = [
            dict(
                IDENTITY,
                event_type="accepted_item",
                delivery_sequence=seq,
                item_json=wire.encode(item).decode(),
            )
            for seq, item in expected["items"].items()
        ]
        archive = [
            dict(
                IDENTITY, event_type="revision", record_id=ident, record_revision=revision, **value
            )
            for (ident, revision), value in expected["facts"].items()
        ]
        heads = [
            dict(
                IDENTITY, event_type="revision", record_id=ident, record_revision=revision, **value
            )
            for (ident, revision), value in expected["heads"].items()
        ]
        return sequence, archive, heads

    def test_complete_multi_update_reference_keeps_every_revision(self):
        result = compare_output(*self.rows(), self.expected)
        self.assertEqual(result["unique_items"], len(self.items))
        self.assertGreater(result["immutable_revisions"], result["latest_heads"])
        self.assertTrue(result["exact_wire_payload_hash_disposition"])

    def test_dropped_old_revision_and_duplicate_fact_are_detected(self):
        for change in (
            lambda rows: rows[1].pop(),
            lambda rows: rows[1].append(copy.deepcopy(rows[1][0])),
        ):
            rows = self.rows()
            change(rows)
            with self.assertRaises(AssertionError):
                compare_output(*rows, self.expected)

    def test_corrupt_payload_hash_disposition_or_scope_is_detected(self):
        for key, value in (
            ("payload_json", "corrupt"),
            ("record_sha256", "b" * 64),
            ("disposition", None),
            ("installation_id", "foreign"),
        ):
            rows = self.rows()
            rows[1][0][key] = value
            with self.subTest(key=key), self.assertRaises(AssertionError):
                compare_output(*rows, self.expected)

    def test_extra_sequence_changed_wire_and_wrong_latest_head_are_detected(self):
        rows = self.rows()
        rows[0][0]["item_json"] += " "
        with self.assertRaises(AssertionError):
            compare_output(*rows, self.expected)
        rows = self.rows()
        rows[0].append(copy.deepcopy(rows[0][0]))
        with self.assertRaises(AssertionError):
            compare_output(*rows, self.expected)
        rows = self.rows()
        rows[2][0]["record_revision"] += 1
        with self.assertRaises(AssertionError):
            compare_output(*rows, self.expected)

    def test_pending_chunk_is_absent_from_facts_until_complete(self):
        initial = expected_output([self.items[:-1]])
        final = self.expected
        self.assertEqual(len(final["facts"]), len(initial["facts"]) + 1)
        self.assertNotIn((self.items[-1]["record_id"], 1), initial["facts"])
        self.assertIn((self.items[-1]["record_id"], 1), final["facts"])
