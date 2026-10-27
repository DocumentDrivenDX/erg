"""Derived winners cannot bypass invalidation, replacement or snapshot safety."""

import random
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from erg import call_projection as cache
from erg import report_sqlite, store, unified


class CallProjection(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        store.initialize_db(self.db)
        cache.initialize(self.db)
        self.db.commit()

    def put(self, uid="u", tool="codex", machine="host", **kw):
        values = dict(
            machine=machine,
            uid=uid,
            tool=tool,
            ts=10,
            session="s",
            model="m",
            fresh_in=1,
            out=1,
            usage_kind="observed",
        )
        values.update(kw)
        self.db.execute(
            "INSERT OR REPLACE INTO llm("
            + ",".join(values)
            + ") VALUES("
            + ",".join("?" for _ in values)
            + ")",
            tuple(values.values()),
        )
        self.db.commit()

    def drain(self, db=None, **kw):
        db = db or self.db
        for _ in range(1000):
            result = cache.run(db, budget=1, **kw)
            if result.get("seed_complete") and not result.get("pending"):
                return result
        self.fail("cache did not drain")

    def parity(self, db=None, ready=True):
        db = db or self.db
        db.execute("BEGIN")
        try:
            fields = [r[1] for r in db.execute("PRAGMA table_info(calls)")]
            query = cache.cached_select(db, fields)
            self.assertEqual(query is not None, ready)
            oracle = list(db.execute(unified.CALLS_SELECT))
            actual = list(db.execute(query or report_sqlite.calls_select(fields)))
            self.assertEqual(actual, oracle)
        finally:
            db.rollback()

    def test_updates_replacements_and_deletes_invalidate_both_old_and_new_partitions(self):
        self.put("shared", machine="a", out=2)
        self.put("shared", machine="b", out=1)
        self.drain()
        self.parity()
        self.assertEqual(self.db.execute("PRAGMA recursive_triggers").fetchone()[0], 0)
        self.put("shared", tool="claude", machine="a", out=9)
        self.assertEqual(cache.status(self.db)["pending"], 2)
        self.parity(ready=False)
        self.drain()
        self.parity()
        self.assertEqual(
            set(self.db.execute("SELECT tool,machine FROM call_winner_rows")),
            {("codex", "b"), ("claude", "a")},
        )
        with self.db:
            self.db.execute(
                "UPDATE llm SET tool='pi',uid='changed',usage_kind='synthetic_history' WHERE machine='a'"
            )
        self.parity(ready=False)
        self.drain()
        self.parity()
        with self.db:
            self.db.execute("DELETE FROM llm WHERE machine='b'")
        self.drain()
        self.parity()
        self.assertEqual(self.db.execute("SELECT count(*) FROM call_winner_rows").fetchone()[0], 0)

    def test_unchanged_update_and_upsert_do_not_dirty_ready_cache(self):
        self.put()
        self.drain()
        with self.db:
            self.db.execute("UPDATE llm SET out=out")
            self.db.execute(
                "INSERT INTO llm(machine,uid,out) VALUES('host','u',1) ON CONFLICT(machine,uid) DO UPDATE SET out=excluded.out"
            )
        self.assertEqual(cache.status(self.db)["pending"], 0)
        self.parity()

    def test_bounded_existing_seed_and_new_rows_survive_restart_and_partial_progress(self):
        self.db.execute("DROP TRIGGER erg_call_insert")
        self.db.commit()
        for i in range(10):
            self.put(str(i))
        cache.initialize(self.db)
        self.db.commit()
        first = cache.run(self.db, seed_limit=3, limit=1, budget=1)
        self.assertEqual(first["seeded"], 3)
        self.assertEqual(first["processed"], 1)
        self.assertFalse(first["seed_complete"])
        self.parity(ready=False)
        self.put("new")
        self.drain(seed_limit=2, limit=2)
        self.parity()
        cache.initialize(self.db)
        self.db.commit()
        self.assertTrue(cache.status(self.db)["ready"])
        self.assertEqual(self.db.execute("SELECT count(*) FROM call_winner_rows").fetchone()[0], 11)

    def test_local_response_group_changes_and_gateway_counts_match_the_global_oracle(self):
        self.put("native", response_id="r", request_id="request")
        self.put("local", "local", response_id="r", request_id="request")
        self.put("gateway", "gateway", request_id="request")
        self.drain()
        self.parity()
        self.put("local", "local", response_id="different", request_id="request")
        self.parity(ready=False)
        self.drain()
        self.parity()
        self.put("local-2", "local", response_id="r:m", model="")
        self.put("local-3", "local", response_id="r", model="m:")
        self.drain()
        self.parity()

    def test_null_winner_identity_uses_portable_fallback_and_recovers_after_deletion(self):
        self.put(uid=None)
        self.drain()
        self.assertEqual(cache.status(self.db)["reason"], "unsupported_winner_identity")
        self.parity(ready=False)
        with self.db:
            self.db.execute("DELETE FROM llm")
        self.drain()
        self.parity()

    def test_opaque_keys_preserve_embedded_nuls_separators_and_null_vs_empty(self):
        for uid in ("a", "a\x00b", "a\x00c", "a'|text:", ""):
            self.put(uid)
        self.drain()
        self.parity()
        self.assertEqual(self.db.execute("SELECT count(*) FROM call_winner_rows").fetchone()[0], 5)

    def test_random_mutations_preserve_all_columns_and_incomplete_cache_falls_back(self):
        rng = random.Random(42)
        for batch in range(12):
            for _ in range(30):
                self.put(
                    str(rng.randrange(25)),
                    rng.choice(("codex", "pi", "local", "gateway")),
                    rng.choice(("a", "b")),
                    out=rng.choice((None, 0, 1, 5)),
                    model=rng.choice((None, "", "m", "m:n")),
                    response_id=rng.choice((None, "", "r", "r:m")),
                    request_id=rng.choice((None, "", "q")),
                    usage_kind=rng.choice((None, "observed", "unknown", "synthetic_history")),
                )
            self.parity(ready=False)
            self.drain(limit=7)
            self.parity()

    def test_schema_or_trigger_repair_reseeds_instead_of_trusting_stale_rows(self):
        self.put()
        self.drain()
        self.parity()
        self.db.execute("DROP TRIGGER erg_call_insert")
        self.db.commit()
        self.put("missed")
        self.assertEqual(cache.status(self.db)["reason"], "source_triggers_changed")
        self.parity(ready=False)
        cache.initialize(self.db)
        self.db.commit()
        self.drain()
        self.parity()
        with self.db:
            self.db.execute("ALTER TABLE llm ADD COLUMN extra_value TEXT")
        self.assertEqual(cache.status(self.db)["reason"], "source_schema_changed")
        self.parity(ready=False)
        cache.initialize(self.db)
        self.db.commit()
        self.drain()
        self.parity()

    def test_missing_lookup_index_disables_cache_and_is_repaired(self):
        self.put()
        self.drain()
        self.db.execute("DROP INDEX call_winner_local_lookup")
        self.db.commit()
        self.assertEqual(cache.status(self.db)["reason"], "source_indexes_changed")
        self.assertEqual(cache.run(self.db)["processed"], 0)
        self.parity(ready=False)
        cache.initialize(self.db)
        self.db.commit()
        self.parity()

    def test_failure_rolls_back_winner_changes_and_queue_acknowledgement(self):
        self.put()
        self.drain()
        self.put(out=9)
        before = self.db.execute("SELECT * FROM call_winner_rows").fetchall()
        pending = self.db.execute("SELECT * FROM call_winner_dirty").fetchall()
        db = self.db

        class FailAck:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return db.__exit__(*a)

            @property
            def in_transaction(self):
                return db.in_transaction

            def rollback(self):
                return db.rollback()

            def execute(self, sql, *a):
                if sql.startswith("DELETE FROM call_winner_dirty"):
                    raise sqlite3.OperationalError("injected")
                return db.execute(sql, *a)

        with self.assertRaises(sqlite3.OperationalError):
            cache.run(FailAck(), budget=1)
        self.assertFalse(db.in_transaction)
        self.assertEqual(db.execute("SELECT * FROM call_winner_rows").fetchall(), before)
        self.assertEqual(db.execute("SELECT * FROM call_winner_dirty").fetchall(), pending)
        self.drain()
        self.parity()

    def test_idle_maintenance_has_no_writer_and_fields_and_bounds_are_validated(self):
        self.put()
        self.drain()
        queries = []
        self.db.set_trace_callback(queries.append)
        cache.run(self.db)
        self.db.set_trace_callback(None)
        self.assertFalse(any("BEGIN IMMEDIATE" in q for q in queries))
        self.assertIsNone(cache.cached_select(self.db, ["uid"]))
        for fields in ([], ["uid", "uid"], ["uid; DROP TABLE llm"], [None]):
            with self.assertRaises(ValueError):
                cache.cached_select(self.db, fields)
        for kw in (
            dict(limit=True),
            dict(seed_limit=0),
            dict(budget=float("nan")),
            dict(budget=-1),
        ):
            with self.assertRaises(ValueError):
                cache.run(self.db, **kw)
        self.db.execute("BEGIN")
        try:
            with self.assertRaises(ValueError):
                cache.run(self.db)
        finally:
            self.db.rollback()

    def test_pinned_reader_remains_consistent_across_source_append_and_rebuild(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger"
            writer = store.connect(path)
            cache.initialize(writer)
            writer.commit()
            try:
                reader = store.open_readonly(path)
                try:
                    reader.execute("BEGIN")
                    fields = [r[1] for r in reader.execute("PRAGMA table_info(calls)")]
                    query = cache.cached_select(reader, fields)
                    self.assertIsNotNone(query)
                    with writer:
                        writer.execute(
                            "INSERT INTO llm(machine,uid,tool,ts,out) VALUES('host','new','codex',1,1)"
                        )
                    self.drain(writer)
                    self.assertEqual(
                        list(reader.execute(query)), list(reader.execute(unified.CALLS_SELECT))
                    )
                    self.assertEqual(list(reader.execute(query)), [])
                    reader.rollback()
                    self.parity(reader)
                finally:
                    reader.close()
            finally:
                writer.close()

    def test_replacement_with_pending_or_unseeded_old_winner_keeps_all_partitions_correct(self):
        self.put("u", machine="a")
        self.drain()
        self.put("u", machine="b", out=9)  # New winner is pending; cached copy is still a.
        self.put("u", machine="b", tool="claude", out=10)
        self.drain()
        self.parity()
        self.db.execute("DROP TRIGGER erg_call_insert")
        self.db.commit()
        self.put("unseeded")
        cache.initialize(self.db)
        self.db.commit()
        cache.run(self.db, seed_limit=1, budget=0)
        self.put("unseeded", tool="pi")
        self.drain(seed_limit=1)
        self.parity()

    def test_nonstandard_unique_constraints_require_raw_fallback(self):
        self.put()
        self.drain()
        self.db.execute("CREATE UNIQUE INDEX custom_session ON llm(session)")
        self.db.commit()
        self.assertEqual(cache.status(self.db)["reason"], "unsupported_source_constraints")
        self.assertEqual(cache.run(self.db)["processed"], 0)
        self.parity(ready=False)

    def test_source_layout_extensions_require_portable_fallback(self):
        self.put()
        self.drain()
        with self.db:
            self.db.execute("ALTER TABLE llm ADD COLUMN extra_value TEXT COLLATE NOCASE")
        self.assertEqual(cache.status(self.db)["reason"], "unsupported_source_layout")
        self.parity(ready=False)
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.execute("PRAGMA encoding='UTF-16'")
        store.initialize_db(db)
        cache.initialize(db)
        db.commit()
        self.assertEqual(cache.status(db)["reason"], "unsupported_source_layout")
        self.assertEqual(cache.run(db)["processed"], 0)
        self.parity(db, ready=False)

    def test_actual_maintenance_uses_indexed_ordered_winner_lookups(self):
        self.put("native")
        self.put("receipt", "local", response_id="r")
        queries = []
        self.db.set_trace_callback(queries.append)
        try:
            self.drain()
        finally:
            self.db.set_trace_callback(None)
        lookups = [q for q in queries if q.startswith("INSERT INTO call_winner_rows SELECT")]
        self.assertEqual(len(lookups), 1)
        plan = str(self.db.execute("EXPLAIN QUERY PLAN " + lookups[0]).fetchall())
        self.assertIn("llm_eligible_winner", plan)
        self.assertIn("call_winner_local_lookup", plan)
        self.assertNotIn("TEMP B-TREE", plan)

    def test_partial_seed_and_dirty_queue_survive_actual_connection_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger"
            db = store.connect(path)
            db.execute("DROP TRIGGER erg_call_insert")
            db.commit()
            with db:
                db.executemany(
                    "INSERT INTO llm(machine,uid,tool,ts,out) VALUES('host',?,'codex',1,1)",
                    [(str(i),) for i in range(20)],
                )
            cache.initialize(db)
            db.commit()
            first = cache.run(db, seed_limit=3, limit=1, budget=1)
            self.assertEqual(first["seed_cursor"], 3)
            self.assertEqual(first["pending"], 2)
            db.close()
            reopened = store.connect(path, initialize=False)
            try:
                self.assertEqual(cache.status(reopened)["seed_cursor"], 3)
                self.parity(reopened, ready=False)
                self.drain(reopened, seed_limit=3, limit=2)
                self.parity(reopened)
                self.assertEqual(
                    reopened.execute("SELECT count(*) FROM call_winner_rows").fetchone()[0], 20
                )
            finally:
                reopened.close()

    def test_nonpositive_rowids_and_moves_behind_seed_cursor_are_not_lost(self):
        self.db.execute("DROP TRIGGER erg_call_insert")
        self.db.commit()
        for i in range(5):
            self.put(str(i))
        with self.db:
            self.db.execute("UPDATE llm SET rowid=-9223372036854775808 WHERE uid='0'")
        cache.initialize(self.db)
        self.assertTrue(self.db.in_transaction)
        self.db.commit()
        first = cache.run(self.db, seed_limit=2, limit=1, budget=1)
        self.assertFalse(first["seed_complete"])
        with self.db:
            self.db.execute("UPDATE llm SET rowid=-10 WHERE uid='4'")
        self.drain(seed_limit=1)
        self.parity()
        self.assertEqual(self.db.execute("SELECT count(*) FROM call_winner_rows").fetchone()[0], 5)

    def test_corrupted_seed_metadata_disables_cache_without_hiding_raw_calls(self):
        self.put()
        self.drain()
        with self.db:
            self.db.execute("UPDATE call_winner_state SET value='invalid' WHERE key='seed_cursor'")
        self.assertEqual(cache.status(self.db)["reason"], "invalid_cache_state")
        self.assertEqual(cache.run(self.db)["processed"], 0)
        self.parity(ready=False)
        cache.initialize(self.db)
        self.db.commit()
        self.drain()
        self.parity()

    def test_generated_source_columns_require_portable_fallback(self):
        if sqlite3.sqlite_version_info < (3, 31, 0):
            self.skipTest("generated columns unavailable")
        self.put()
        self.drain()
        with self.db:
            self.db.execute(
                "ALTER TABLE llm ADD COLUMN generated_value INT GENERATED ALWAYS AS (out+1) VIRTUAL"
            )
        self.assertEqual(cache.status(self.db)["reason"], "unsupported_source_layout")
        self.parity(ready=False)

    def test_pending_counts_are_explicit_lower_bounds_and_cannot_starve_maintenance(self):
        for i in range(10):
            self.put(str(i))
        bounded = cache.status(self.db, pending_limit=3)
        self.assertEqual(bounded["pending"], 4)
        self.assertFalse(bounded["pending_count_exact"])
        full = cache.status(self.db, pending_limit=None)
        self.assertEqual(full["pending"], 10)
        self.assertTrue(full["pending_count_exact"])
        with patch.object(cache.time, "thread_time", side_effect=[0] + [100] * 10):
            result = cache.run(self.db, budget=0.002)
        self.assertEqual(result["processed"], 10)
        self.drain()
        self.parity()

    def test_exhausted_preflight_budget_advances_one_bounded_batch_and_honors_limit(self):
        for i in range(100):
            self.put(str(i))
        with patch.object(cache.time, "thread_time", side_effect=[0] + [100] * 10):
            result = cache.run(self.db, budget=0.002)
        self.assertEqual(result["processed"], 32)
        self.assertEqual(result["pending"], 68)
        self.parity(ready=False)
        with patch.object(cache.time, "thread_time", side_effect=[0] + [100] * 10):
            result = cache.run(self.db, limit=7, budget=0.002)
        self.assertEqual(result["processed"], 7)
        self.assertEqual(result["pending"], 61)
        self.drain()
        self.parity()

    def test_seed_backpressure_bounds_queue_and_resumes_without_skipping_rows(self):
        self.db.execute("DROP TRIGGER erg_call_insert")
        self.db.commit()
        for i in range(200):
            self.put(str(i))
        cache.initialize(self.db)
        self.db.commit()

        def small_run():
            with patch.object(cache.time, "thread_time", side_effect=[0] + [100] * 10):
                return cache.run(self.db, seed_limit=40, limit=7, budget=0.002)

        first = small_run()
        self.assertEqual(first["seeded"], 40)
        self.assertEqual(first["pending"], 33)
        second = small_run()
        self.assertEqual(second["seeded"], 7)
        self.assertEqual(second["seed_cursor"], 47)
        self.assertEqual(second["pending"], 33)
        self.put("new-during-seed", tool="pi")
        self.parity(ready=False)
        for _ in range(100):
            result = small_run()
            self.assertLessEqual(result["pending"], 40)
            if result["ready"]:
                break
        self.assertTrue(result["ready"])
        self.parity()
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM call_winner_rows").fetchone()[0], 201
        )

    def test_external_invalidations_above_seed_watermark_are_drained_before_seeding(self):
        self.db.execute("DROP TRIGGER erg_call_insert")
        self.db.commit()
        for i in range(20):
            self.put(str(i))
        cache.initialize(self.db)
        self.db.commit()
        for i in range(20, 40):
            self.put(str(i))
        with patch.object(cache.time, "thread_time", side_effect=[0] + [100] * 10):
            result = cache.run(self.db, seed_limit=8, limit=3, budget=0.002)
        self.assertEqual(result["seeded"], 0)
        self.assertIsNone(result["seed_cursor"])
        self.assertEqual(result["processed"], 3)
        self.assertEqual(result["pending"], 17)
        self.drain(seed_limit=8, limit=3)
        self.parity()

    def test_seed_allowance_rechecks_new_invalidations_after_status_snapshot(self):
        self.db.execute("DROP TRIGGER erg_call_insert")
        self.db.commit()
        for i in range(20):
            self.put(str(i))
        cache.initialize(self.db)
        self.db.commit()
        db = self.db
        owner = self

        class BeforeWriter:
            def __init__(self):
                self.injected = False

            def __getattr__(self, key):
                return getattr(db, key)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return db.__exit__(*args)

            def execute(self, sql, *args):
                if sql == "BEGIN IMMEDIATE" and not self.injected:
                    self.injected = True
                    for i in range(20, 28):
                        owner.put(str(i))
                return db.execute(sql, *args)

        with patch.object(cache.time, "thread_time", side_effect=[0] + [100] * 10):
            result = cache.run(BeforeWriter(), seed_limit=8, limit=3, budget=0.002)
        self.assertEqual(result["seeded"], 0)
        self.assertIsNone(result["seed_cursor"])
        self.assertFalse(result["seed_complete"])
        self.assertEqual(result["pending"], 5)
        self.drain(seed_limit=8, limit=3)
        self.parity()

    def test_changed_canonical_rules_or_cache_definition_cannot_reuse_old_winners(self):
        self.put()
        self.drain()
        with patch.object(unified, "CALLS_SELECT", unified.CALLS_SELECT + " "):
            self.assertEqual(cache.status(self.db)["reason"], "canonical_definition_changed")
            self.assertEqual(cache.run(self.db)["processed"], 0)
            self.db.execute("BEGIN")
            try:
                self.assertIsNone(cache.cached_select(self.db, ["uid"]))
            finally:
                self.db.rollback()
        with self.db:
            self.db.execute(
                "UPDATE call_winner_state SET value='older-definition' WHERE key='definition_signature'"
            )
        self.assertEqual(cache.status(self.db)["reason"], "cache_definition_changed")
        self.parity(ready=False)
        cache.initialize(self.db)
        self.db.commit()
        self.drain()
        self.parity()

    def test_seed_checkpoint_failure_rolls_back_the_whole_prefix(self):
        self.db.execute("DROP TRIGGER erg_call_insert")
        self.db.commit()
        for i in range(5):
            self.put(str(i))
        cache.initialize(self.db)
        self.db.commit()
        before = cache.status(self.db)
        db = self.db

        class FailSeed:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return db.__exit__(*a)

            @property
            def in_transaction(self):
                return db.in_transaction

            def rollback(self):
                return db.rollback()

            def execute(self, sql, *a):
                if sql.startswith("UPDATE call_winner_state SET value=?"):
                    raise sqlite3.OperationalError("injected seed failure")
                return db.execute(sql, *a)

        with self.assertRaises(sqlite3.OperationalError):
            cache.run(FailSeed(), seed_limit=2, budget=1)
        self.assertEqual(cache.status(db), before)
        self.assertFalse(db.in_transaction)
        self.drain()
        self.parity()

    def test_initializer_failure_rolls_back_derived_schema_and_keeps_raw_rows(self):
        self.put()
        self.drain()
        self.db.execute("DROP TRIGGER erg_call_insert")
        self.db.commit()
        db = self.db
        before = {
            name: db.execute("SELECT * FROM " + name).fetchall()
            for name in ("llm", "call_winner_rows", "call_winner_state")
        }

        class FailInit:
            @property
            def in_transaction(self):
                return db.in_transaction

            def rollback(self):
                return db.rollback()

            def executemany(self, *a):
                return db.executemany(*a)

            def execute(self, sql, *a):
                if sql.startswith("CREATE TRIGGER erg_call_insert"):
                    raise sqlite3.OperationalError("injected initialization failure")
                return db.execute(sql, *a)

        with self.assertRaises(sqlite3.OperationalError):
            cache.initialize(FailInit())
        self.assertFalse(db.in_transaction)
        self.assertEqual(
            {name: db.execute("SELECT * FROM " + name).fetchall() for name in before}, before
        )
        cache.initialize(db)
        db.commit()
        self.drain()
        self.parity()

    def test_source_append_between_preflight_and_writer_is_not_acknowledged_away(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger"
            first = store.connect(path)
            cache.initialize(first)
            first.commit()
            second = store.connect(path, initialize=False)
            fired = []
            errors = []
            try:
                with first:
                    first.execute(
                        "INSERT INTO llm(machine,uid,tool,ts,out) VALUES('host','old','codex',1,1)"
                    )

                def trace(sql):
                    if sql == "BEGIN IMMEDIATE" and not fired:
                        try:
                            with second:
                                second.execute(
                                    "INSERT INTO llm(machine,uid,tool,ts,out) VALUES('host','new','codex',2,2)"
                                )
                            fired.append(True)
                        except Exception as error:
                            errors.append(type(error).__name__)

                first.set_trace_callback(trace)
                try:
                    result = cache.run(first, budget=1)
                finally:
                    first.set_trace_callback(None)
                self.assertEqual(errors, [])
                self.assertEqual(fired, [True])
                self.assertEqual(result["pending"], 0)
                self.parity(first)
                self.assertEqual(
                    first.execute("SELECT count(*) FROM call_winner_rows").fetchone()[0], 2
                )
            finally:
                first.close()
                second.close()

    def test_optional_setup_failure_preserves_raw_writes_and_disarms_cache_triggers(self):
        self.put()
        self.drain()
        self.db.execute("BEGIN")
        self.db.execute(
            "INSERT INTO llm(machine,uid,tool,ts,out) VALUES('host','before','codex',1,1)"
        )
        before = self.db.execute("SELECT * FROM call_winner_rows").fetchall()

        def fail(db):
            db.execute("DELETE FROM call_winner_rows")
            raise RuntimeError("PRIVATE setup detail")

        with patch.object(cache, "_initialize", side_effect=fail):
            result = cache.safe_initialize(self.db)
        self.assertTrue(self.db.in_transaction)
        self.db.commit()
        self.assertEqual(result["error"], "RuntimeError")
        self.assertNotIn("PRIVATE", str(result))
        self.assertEqual(self.db.execute("SELECT * FROM call_winner_rows").fetchall(), before)
        self.put("after")
        self.parity(ready=False)
        self.assertEqual(self.db.execute("SELECT count(*) FROM llm").fetchone()[0], 3)

    def test_optional_maintenance_failure_does_not_stop_subsequent_raw_ingest(self):
        self.put()
        self.drain()
        self.put(out=9)
        with patch.object(cache, "project_batch", side_effect=sqlite3.OperationalError("PRIVATE")):
            result = cache.maintain(self.db, budget=1)
        self.assertEqual(result["reason"], "maintenance_failed")
        self.assertNotIn("PRIVATE", str(result))
        self.assertFalse(self.db.in_transaction)
        self.put("new")
        self.parity(ready=False)
        cache.safe_initialize(self.db)
        self.db.commit()
        self.drain()
        self.parity()

    def test_broken_derived_table_is_disarmed_and_raw_factory_falls_back(self):
        self.put()
        self.drain()
        with self.db:
            self.db.execute("DROP TABLE call_winner_rows")
        result = cache.maintain(self.db)
        self.assertEqual(result["reason"], "cache_tables_unavailable")
        self.put("new")
        self.parity(ready=False)
        cache.safe_initialize(self.db)
        self.db.commit()
        self.drain()
        self.parity()

    def test_changed_derived_schema_is_not_exposed_to_reports(self):
        self.put()
        self.drain()
        with self.db:
            self.db.execute("ALTER TABLE call_winner_rows RENAME COLUMN out TO wrong_out")
        self.assertEqual(cache.status(self.db)["reason"], "cache_schema_changed")
        self.parity(ready=False)
        cache.safe_initialize(self.db)
        self.db.commit()
        self.drain()
        self.parity()

    def test_report_factory_only_uses_ready_same_snapshot_cache_and_survives_metadata_errors(self):
        self.put()
        self.drain()
        fields = ["uid", "tool", "session", "out"]
        self.assertNotIn("FROM call_winner_rows", report_sqlite.calls_select(fields, db=self.db))
        self.db.execute("BEGIN")
        try:
            query = report_sqlite.calls_select(fields, db=self.db)
            self.assertIn("FROM call_winner_rows", query)
            self.assertEqual(self.db.execute(query).fetchall(), [("u", "codex", "s", 1)])
            with patch.object(
                cache, "cached_select", side_effect=sqlite3.OperationalError("metadata unavailable")
            ):
                query = report_sqlite.calls_select(fields, db=self.db)
                self.assertNotIn("FROM call_winner_rows", query)
        finally:
            self.db.rollback()

    def test_update_or_replace_invalidates_the_implicitly_removed_destination_winner(self):
        self.put("source", tool="codex", machine="a")
        self.put("destination", tool="claude", machine="b")
        self.drain()
        with self.db:
            self.db.execute(
                "UPDATE OR REPLACE llm SET machine='b',uid='destination',tool='pi' WHERE machine='a' AND uid='source'"
            )
        self.drain()
        self.parity()
        self.assertEqual(self.db.execute("SELECT tool FROM call_winner_rows").fetchall(), [("pi",)])
