"""Managed checkpoints preserve committed data and pinned reader snapshots."""

import contextlib
import os
import tempfile
import unittest

from erg import store


class ManagedCheckpoint(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = os.path.join(temp.name, "ledger.db")
        self.db = store.connect(self.path, initialize=False, autocheckpoint=False)
        self.addCleanup(self.db.close)
        with self.db:
            self.db.execute("CREATE TABLE evidence(id INTEGER PRIMARY KEY,value TEXT)")
            self.db.execute("INSERT INTO evidence VALUES(1,'old')")
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def test_checkpoint_policy_keeps_full_durability_and_preserves_reader_snapshot(self):
        self.assertEqual(self.db.execute("PRAGMA wal_autocheckpoint").fetchone()[0], 0)
        self.assertEqual(self.db.execute("PRAGMA synchronous").fetchone()[0], 2)
        with contextlib.closing(store.open_readonly(self.path)) as reader:
            reader.execute("BEGIN")
            self.assertEqual(reader.execute("SELECT value FROM evidence").fetchone()[0], "old")
            with self.db:
                self.db.execute("UPDATE evidence SET value='new'")
            self.db.execute("PRAGMA busy_timeout=1234")
            partial = store.passive_checkpoint(self.db)
            self.assertLess(partial["checkpointed"], partial["frames"])
            self.assertEqual(self.db.execute("PRAGMA busy_timeout").fetchone()[0], 1234)
            self.assertEqual(reader.execute("SELECT value FROM evidence").fetchone()[0], "old")
            with contextlib.closing(store.connect(self.path, initialize=False)) as fresh:
                self.assertEqual(fresh.execute("SELECT value FROM evidence").fetchone()[0], "new")
                self.assertEqual(fresh.execute("PRAGMA wal_autocheckpoint").fetchone()[0], 1000)
        complete = store.passive_checkpoint(self.db)
        self.assertEqual(complete["checkpointed"], complete["frames"])
        with contextlib.closing(store.open_readonly(self.path)) as fresh:
            self.assertEqual(fresh.execute("SELECT value FROM evidence").fetchone()[0], "new")

    def test_checkpoint_does_not_wait_for_an_active_writer_or_change_timeout(self):
        with contextlib.closing(
            store.connect(self.path, initialize=False, autocheckpoint=False)
        ) as writer:
            writer.execute("BEGIN IMMEDIATE")
            writer.execute("UPDATE evidence SET value='pending'")
            trace = []
            self.db.set_trace_callback(trace.append)
            try:
                r = store.passive_checkpoint(self.db)
            finally:
                self.db.set_trace_callback(None)
            self.assertIn(r["state"], ("ready", "busy"))
            self.assertIn("PRAGMA busy_timeout=0", trace)
            self.assertEqual(self.db.execute("PRAGMA busy_timeout").fetchone()[0], 30000)
            self.assertEqual(self.db.execute("SELECT value FROM evidence").fetchone()[0], "old")
            writer.rollback()

    def test_checkpoint_refuses_an_open_transaction_without_committing_it(self):
        self.db.execute("BEGIN IMMEDIATE")
        self.db.execute("UPDATE evidence SET value='uncommitted'")
        with self.assertRaises(ValueError):
            store.passive_checkpoint(self.db)
        self.assertTrue(self.db.in_transaction)
        self.db.rollback()
        self.assertEqual(self.db.execute("SELECT value FROM evidence").fetchone()[0], "old")
