import unittest

from central.erg_central.ldp_prefix import prove_window
from central.erg_central.receiver import Conflict


def row(seq, disposition="ready", complete=True):
    return dict(
        delivery_seq=seq,
        record_sha256="a" * 64,
        revision_sha256="a" * 64 if complete else None,
        projection_disposition=disposition,
    )


class PrefixProof(unittest.TestCase):
    def test_complete_prefix_preserves_independent_projection_lag(self):
        rows = [row(1), row(2, disposition=None), row(3)]
        self.assertEqual(
            prove_window(dict(materialized_through=0, projected_through=0), rows, rows),
            dict(materialized_through=3, projected_through=1),
        )

    def test_missing_sequence_or_chunk_stops_before_later_ready_facts(self):
        for rows in ([row(1), row(3)], [row(1), row(2, complete=False), row(3)]):
            self.assertEqual(
                prove_window(dict(materialized_through=0, projected_through=0), rows, rows)[
                    "materialized_through"
                ],
                1,
            )

    def test_new_materialization_cannot_skip_old_projected_backlog(self):
        self.assertEqual(
            prove_window(
                dict(materialized_through=10, projected_through=5),
                [row(11)],
                [row(6), row(7, disposition=None), row(8)],
            ),
            dict(materialized_through=11, projected_through=6),
        )

    def test_conflicting_hash_duplicate_sequence_and_invalid_cursors_refuse(self):
        bad = dict(row(1), revision_sha256="b" * 64)
        for rows in ([bad], [row(1), row(1)]):
            with self.assertRaises(Conflict):
                prove_window(dict(materialized_through=0, projected_through=0), rows, rows)
        for previous in (
            dict(materialized_through=0, projected_through=1),
            dict(materialized_through=True, projected_through=0),
        ):
            with self.assertRaises(Conflict):
                prove_window(previous, [], [])

    def test_crash_before_durable_facts_cannot_advance_then_restart_recovers(self):
        previous = dict(materialized_through=0, projected_through=0)
        self.assertEqual(prove_window(previous, [row(1, complete=False)], []), previous)
        first = prove_window(previous, [row(1)], [row(1)])
        self.assertEqual(prove_window(first, [], []), first)
