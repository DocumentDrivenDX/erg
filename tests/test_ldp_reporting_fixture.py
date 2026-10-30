import tempfile
import unittest

from central.ldp.reporting_fixture import payloads, qualify
from erg import unified


class LdpReportingFixture(unittest.TestCase):
    def test_metadata_is_accepted_by_real_observation_normalization(self):
        kinds = set()
        for record in payloads():
            body = record["body"]
            row = unified.normalize(body["machine"], dict(body["payload"], t=body["kind"]))
            self.assertEqual((row[0], row[5]), (body["event_id"], body["content_hash"]))
            kinds.add(record["record_kind"])
        self.assertEqual(kinds, {"usage_evidence", "work_evidence", "account_evidence"})

    def test_real_reporting_replay_and_project_allocations_match(self):
        with tempfile.TemporaryDirectory() as directory:
            expected = qualify(directory)["reports"]
        self.assertEqual(expected["costs"]["request_equivalents"], 3)
        self.assertEqual(expected["costs"]["tokens"], 390)
        self.assertEqual(expected["projects"]["synthetic-a"]["tokens"], 292.5)
        self.assertEqual(expected["projects"]["synthetic-b"]["tokens"], 97.5)
        for view in ("work", "accounts"):
            self.assertEqual(
                expected[view]["valuation"]["known_value_low_usd"],
                expected["costs"]["known_value_low_usd"],
            )
