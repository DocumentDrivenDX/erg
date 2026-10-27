import unittest

from central.erg_central.processor import await_visibility
from central.erg_central.receiver import Conflict


class ZerobusVisibilityTests(unittest.TestCase):
    def test_visible_retry_never_resubmits(self):
        writes = []
        self.assertTrue(await_visibility({1: "one"}, lambda: [(1, "one")], writes.append))
        self.assertEqual(writes, [])

    def test_only_missing_records_sent_and_ack_waits_for_visibility(self):
        reads = iter([[(1, "one")], [(1, "one")], [(1, "one"), (2, "two")]])
        writes = []
        sleeps = []
        self.assertTrue(
            await_visibility(
                {1: "one", 2: "two"},
                lambda: next(reads),
                writes.append,
                clock=lambda: 0,
                sleep=sleeps.append,
            )
        )
        self.assertEqual(writes, [{2}])
        self.assertEqual(sleeps, [2])

    def test_timeout_does_not_claim_materialization_or_resend(self):
        times = iter([0, 31])
        writes = []
        self.assertFalse(
            await_visibility({1: "one"}, lambda: [], writes.append, clock=lambda: next(times))
        )
        self.assertEqual(writes, [{1}])

    def test_conflict_rejected_before_writing(self):
        writes = []
        with self.assertRaises(Conflict):
            await_visibility({1: "one"}, lambda: [(1, "different")], writes.append)
        self.assertEqual(writes, [])
