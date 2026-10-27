"""Public read models must remain identical with ready, dirty or disabled winners."""

import sqlite3
import unittest
from unittest.mock import patch

import test_effectiveness as fixtures

from erg import (
    account_reports,
    history,
    project_reports,
    report_sqlite,
    session_usage,
    store,
    work,
)
from erg import (
    call_projection as cache,
)


class CachedReports(unittest.TestCase):
    setUp = fixtures.Effectiveness.setUp
    ingest = fixtures.Effectiveness.ingest
    call = fixtures.Effectiveness.call
    ctx = fixtures.Effectiveness.ctx
    pr = fixtures.Effectiveness.pr

    def drain(self):
        for _ in range(100):
            result = cache.maintain(self.db, budget=1)
            if result.get("ready"):
                return
        self.fail(str(result))

    def calculate(self):
        self.db.execute("BEGIN")
        try:
            return dict(
                usage=store.dataset(self.db),
                work=work.effectiveness(self.db),
                accounts=account_reports.report(self.db),
                costs=project_reports.report(
                    self.db,
                    start=1,
                    end=1000,
                    canonical_select=lambda columns: report_sqlite.calls_select(
                        columns, db=self.db
                    ),
                ),
                native_session=session_usage.metrics(self.db, "codex", "s"),
                local_session=session_usage.metrics(self.db, "local", "receipt"),
                local_detail=list(session_usage.calls(self.db, "local", "receipt", ["uid", "out"])),
                coverage=history.reconciliation(self.db),
            )
        finally:
            self.db.rollback()

    def test_public_usage_work_accounts_costs_and_session_metrics_match_raw_fallback(self):
        self.pr()
        self.ingest(
            self.ctx(),
            self.call(uid="z", out=3),
            self.call(uid="a", ts=21, out=7),
            self.call(uid="no-context", session="unknown", ts=22),
            self.call(
                uid="local", tool="local", session="receipt", ts=23, response_id="local-response"
            ),
            self.call(uid="gateway", tool="gateway", session="gateway", ts=24, request_id="q"),
        )
        self.drain()
        with patch.object(work.time, "time", return_value=10000):
            with patch.object(cache, "cached_select", return_value=None):
                raw = self.calculate()
            statements = []
            self.db.set_trace_callback(statements.append)
            try:
                actual = self.calculate()
            finally:
                self.db.set_trace_callback(None)
            self.assertEqual(actual, raw)
            self.assertTrue(any("FROM call_winner_rows w" in q for q in statements))
            self.ingest(self.call(uid="new", ts=25))
            with patch.object(cache, "cached_select", return_value=None):
                raw = self.calculate()
            self.assertEqual(self.calculate(), raw)
            self.drain()
            self.assertEqual(self.calculate(), raw)
            with self.db:
                cache.disarm(self.db)
            self.assertEqual(self.calculate(), raw)

    def test_streaming_store_upserts_queue_idempotently_without_trigger_conflict_errors(self):
        for output in (1, 2, 7, 7, 9):
            self.ingest(self.call(out=output))
        self.assertEqual(self.db.execute("SELECT out FROM llm").fetchone()[0], 9)
        self.drain()
        self.ingest(self.call(out=11))
        self.drain()
        self.db.execute("BEGIN")
        try:
            sql = report_sqlite.calls_select(["uid", "out"], db=self.db)
            self.assertEqual(self.db.execute(sql).fetchall(), [("call", 11)])
        finally:
            self.db.rollback()

    def test_raw_ingestion_recovers_from_broken_cache_triggers_before_maintenance(self):
        self.ingest(self.call())
        self.drain()
        with self.db:
            self.db.execute("DROP TABLE call_winner_rows")
        self.ingest(self.call(out=7), self.call(uid="new", out=8))
        self.assertEqual(
            self.db.execute("SELECT uid,out FROM llm ORDER BY uid").fetchall(),
            [("call", 7), ("new", 8)],
        )
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM observations WHERE kind='llm'").fetchone()[0], 2
        )
        self.assertFalse(cache.status(self.db)["ready"])
        cache.safe_initialize(self.db)
        self.db.commit()
        self.drain()

    def test_cache_recovery_and_raw_observation_journal_rollback_together(self):
        from erg import unified

        self.ingest(self.call())
        self.drain()
        with self.db:
            self.db.execute("DROP TABLE call_winner_dirty")
        with patch.object(unified, "record", side_effect=RuntimeError("journal failed")):
            with self.assertRaises(RuntimeError):
                self.ingest(self.call(out=9))
        self.assertEqual(self.db.execute("SELECT out FROM llm").fetchone()[0], 5)
        self.assertFalse(self.db.in_transaction)
        # The failed outer transaction restored the trigger schema too; next ingest
        # recovers again rather than committing unjournaled source mutations.
        self.ingest(self.call(out=11))
        self.assertEqual(self.db.execute("SELECT out FROM llm").fetchone()[0], 11)

    def test_primary_store_failures_are_not_hidden_as_cache_problems(self):
        before = self.db.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'erg_call_%'"
        ).fetchone()[0]
        with self.assertRaises(sqlite3.OperationalError):
            cache.write_calls(self.db, "INSERT INTO missing_primary_table VALUES(?)", [(1,)])
        self.db.rollback()
        self.assertEqual(
            self.db.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'erg_call_%'"
            ).fetchone()[0],
            before,
        )

    def test_primary_trigger_rollback_preserves_the_original_failure(self):
        with self.db:
            self.db.execute(
                "CREATE TRIGGER primary_failure BEFORE INSERT ON llm BEGIN SELECT RAISE(ROLLBACK,'primary rollback signal'); END"
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "primary rollback signal"):
            self.ingest(self.call())
        self.assertFalse(self.db.in_transaction)
        self.assertEqual(self.db.execute("SELECT count(*) FROM llm").fetchone()[0], 0)
