"""Derived host rollups must equal raw reporting under edits and expiration."""

import json
import random
import sqlite3
import tempfile
import unittest
from pathlib import Path

from erg import metric_rollups, store


class RawReader:
    def __init__(self, db):
        self.db = db

    def execute(self, *args):
        return self.db.execute(*args)


class MetricRollups(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "ledger"
        self.db = store.connect(self.path)
        self.addCleanup(self.db.close)

    def drain(self, db=None, **kwargs):
        db = db or self.db
        for _ in range(1000):
            result = metric_rollups.run(db, budget=1, **kwargs)
            if result["seed_complete"] and not result["pending"]:
                return result
        self.fail("derived metric maintenance did not drain")

    def parity(self, db=None):
        db = db or self.db
        for since in (
            -8000,
            -7200,
            -7199,
            -3700,
            -3600,
            -3599,
            -100,
            0,
            1,
            3599,
            3600,
            3601,
            7200,
            9001,
        ):
            self.assertEqual(
                store.rollup_metrics(db, since), store.rollup_metrics(RawReader(db), since), since
            )

    def test_cached_rollups_equal_raw_across_windows_tiers_peaks_and_nullable_dimensions(self):
        rng = random.Random(20261021)
        with self.db:
            for _ in range(500):
                value = rng.random() * 100
                self.db.execute(
                    "INSERT OR REPLACE INTO metrics VALUES(?,?,?,?,?,?,?)",
                    (
                        rng.choice(("host", None, "a,'b")),
                        rng.choice(("sampler", "istat", "other")),
                        rng.choice(("cpu", "load1")),
                        rng.randrange(-8000, 9000),
                        rng.choice((30, 60, 3600)),
                        value,
                        None if rng.randrange(2) else value + 2,
                    ),
                )
        self.parity()
        self.drain()
        self.parity()
        self.assertGreater(
            self.db.execute("SELECT count(*) FROM metric_hour_tiers").fetchone()[0], 0
        )
        with self.db:
            self.db.execute("UPDATE metrics SET v=99,pk=105 WHERE m='cpu'")
            self.db.execute("UPDATE metrics SET ts=ts+7200,src='changed' WHERE m='load1'")
            self.db.execute("DELETE FROM metrics WHERE src='istat'")
        self.parity()
        self.drain()
        self.parity()

    def test_existing_store_seed_is_bounded_and_concurrent_new_rows_are_not_lost(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.executescript(store.SCHEMA)
        db.executemany(
            "INSERT INTO metrics VALUES('host','sampler','cpu',?,30,?,NULL)",
            [(i * 3600, i) for i in range(20)],
        )
        db.commit()
        metric_rollups.initialize(db)
        db.commit()
        first = metric_rollups.run(db, seed_limit=3, limit=1, budget=1)
        self.assertEqual(first["seeded"], 3)
        self.assertFalse(first["seed_complete"])
        self.parity(db)
        with db:
            db.execute("INSERT INTO metrics VALUES('host','sampler','cpu',20,30,90,NULL)")
        self.drain(db, seed_limit=3, limit=2)
        self.parity(db)
        self.assertEqual(db.execute("SELECT sum(n) FROM metric_hour_tiers").fetchone()[0], 21)

    def test_failed_rebuild_preserves_dirty_marker_and_cache_rolls_back(self):
        with self.db:
            self.db.execute("INSERT INTO metrics VALUES('host','sampler','cpu',10,30,1,NULL)")
        self.drain()
        with self.db:
            self.db.execute("UPDATE metrics SET v=9")
        db = self.db

        class FailAck:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return db.__exit__(*args)

            @property
            def in_transaction(self):
                return db.in_transaction

            def execute(self, sql, *args):
                if sql.startswith("DELETE FROM metric_rollup_dirty"):
                    raise sqlite3.OperationalError("injected")
                return db.execute(sql, *args)

        with self.assertRaises(sqlite3.OperationalError):
            metric_rollups.run(FailAck(), budget=1)
        self.assertEqual(db.execute("SELECT total FROM metric_hour_tiers").fetchone()[0], 1)
        self.assertEqual(db.execute("SELECT count(*) FROM metric_rollup_dirty").fetchone()[0], 1)
        self.parity()
        self.drain()
        self.assertEqual(db.execute("SELECT total FROM metric_hour_tiers").fetchone()[0], 9)

    def test_unchanged_ingest_does_not_dirty_cache_and_idle_maintenance_takes_no_writer(self):
        event = dict(t="metric", src="sampler", m="cpu", ts=10, iv=30, v=1, pk=None)
        store.ingest(self.db, [json.dumps(event)], machine="host")
        self.drain()
        store.ingest(self.db, [json.dumps(event)], machine="host")
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM metric_rollup_dirty").fetchone()[0], 0
        )
        queries = []
        self.db.set_trace_callback(queries.append)
        metric_rollups.run(self.db)
        self.db.set_trace_callback(None)
        self.assertFalse(any(sql.startswith("BEGIN") for sql in queries))
        event["pk"] = 9
        store.ingest(self.db, [json.dumps(event)], machine="host")
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM metric_rollup_dirty").fetchone()[0], 1
        )
        self.parity()
        self.drain()
        self.parity()

    def test_expired_raw_bucket_is_removed_and_stale_cache_is_never_used(self):
        with self.db:
            self.db.execute("INSERT INTO metrics VALUES('host','sampler','cpu',10,30,1,NULL)")
        self.drain()
        with self.db:
            self.db.execute("DELETE FROM metrics")
        self.assertEqual(store.rollup_metrics(self.db), [])
        self.drain()
        self.assertEqual(self.db.execute("SELECT count(*) FROM metric_hour_tiers").fetchone()[0], 0)

    def test_cache_checks_and_reads_share_one_snapshot_under_concurrent_append(self):
        with self.db:
            self.db.execute("INSERT INTO metrics VALUES('host','sampler','cpu',3610,30,1,NULL)")
        self.drain()
        writer = store.connect(self.path, initialize=False)
        self.addCleanup(writer.close)
        fired = [False]

        def trace(sql):
            if "FROM metric_hour_tiers" in sql and not fired[0]:
                fired[0] = True
                with writer:
                    writer.execute(
                        "INSERT INTO metrics VALUES('host','sampler','cpu',3640,30,9,NULL)"
                    )

        self.db.set_trace_callback(trace)
        first = store.rollup_metrics(self.db, 3600)
        self.db.set_trace_callback(None)
        self.assertTrue(fired[0])
        self.assertEqual(first[0][3], 1)
        self.assertFalse(self.db.in_transaction)
        self.assertEqual(store.rollup_metrics(self.db, 3600)[0][3], 5)
        self.parity()
        self.drain()
        self.parity()

    def test_fractional_source_timestamp_uses_explicit_raw_fallback(self):
        with self.db:
            self.db.execute("INSERT INTO metrics VALUES('host','sampler','cpu',3601.5,30,1,NULL)")
        result = self.drain()
        self.assertFalse(result["compatible"])
        self.parity()
        with self.db:
            self.db.execute("UPDATE metrics SET ts=3602")
        result = self.drain()
        self.assertTrue(result["compatible"])
        self.parity()
        with self.db:
            self.db.execute(
                "INSERT INTO metrics VALUES('host','sampler','cpu','unsupported-time',30,9,NULL)"
            )
        self.parity()
        self.assertFalse(self.drain()["compatible"])
        with self.db:
            self.db.execute("DELETE FROM metrics WHERE typeof(ts)='text'")
        self.assertTrue(self.drain()["compatible"])
        self.parity()

    def test_full_dataset_equals_raw_backend_with_derived_metric_tiers(self):
        with self.db:
            self.db.executemany(
                "INSERT INTO metrics VALUES('host','sampler','cpu',?,30,?,NULL)",
                [(3600 + i * 30, i) for i in range(100)],
            )
        self.drain()
        from unittest.mock import patch

        with patch.object(store.time, "time", return_value=100000), patch.object(
            store, "my_identities", return_value=[]
        ):
            self.assertEqual(store.dataset(self.db, 3650), store.dataset(RawReader(self.db), 3650))

    def test_seed_progress_and_dirty_markers_roll_back_on_failed_commit(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.executescript(store.SCHEMA)
        db.execute("INSERT INTO metrics VALUES('host','sampler','cpu',10,30,1,NULL)")
        db.commit()
        metric_rollups.initialize(db)
        db.commit()

        class FailedCommit:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                db.rollback()
                raise sqlite3.OperationalError("injected commit")

            @property
            def in_transaction(self):
                return db.in_transaction

            def execute(self, *args):
                return db.execute(*args)

        with self.assertRaises(sqlite3.OperationalError):
            metric_rollups.run(FailedCommit())
        self.assertEqual(
            db.execute("SELECT value FROM metric_rollup_state WHERE key='seed_cursor'").fetchone()[
                0
            ],
            0,
        )
        self.assertEqual(db.execute("SELECT count(*) FROM metric_rollup_dirty").fetchone()[0], 0)
        self.drain(db)
        self.parity(db)

    def test_finite_dirty_batch_uses_one_commit_without_per_bucket_writer_churn(self):
        with self.db:
            self.db.executemany(
                "INSERT INTO metrics VALUES('host','sampler','cpu',?,30,1,NULL)",
                [(3600 * i,) for i in range(1, 9)],
            )
        queries = []
        self.db.set_trace_callback(queries.append)
        result = metric_rollups.run(self.db, limit=4, budget=1)
        self.db.set_trace_callback(None)
        self.assertEqual(result["processed"], 4)
        self.assertEqual(result["pending"], 4)
        self.assertEqual(sum(q.startswith("BEGIN") for q in queries), 1)
        self.assertEqual(sum(q == "COMMIT" for q in queries), 1)
        self.parity()
        self.drain()
        self.parity()
