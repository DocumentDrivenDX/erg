import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from erg import collect, history, service, store
from erg import source_audit as audit


class SourceAudit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = store.connect(str(self.root / "db"))
        self.addCleanup(self.db.close)

    def file(self, name="session.jsonl"):
        p = self.root / name
        p.write_text("SECRET CONTENT MUST NOT BE READ\n")
        return p

    def checkpoint(self, p, state="captured", errors=0):
        s = p.stat()
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO history_sources VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    str(p),
                    "pi",
                    str(s.st_dev) + ":" + str(s.st_ino),
                    s.st_size,
                    "{}",
                    s.st_size,
                    s.st_mtime,
                    1,
                    int(time.time()),
                    1,
                    errors,
                    state,
                ),
            )

    def test_current_baseline_and_uncheckpointed_files_are_distinct_without_body_reads(self):
        a = self.file()
        b = self.file("old.jsonl")
        c = self.file("new.jsonl")
        self.checkpoint(a)
        self.checkpoint(b, "baseline")
        original = Path.open

        def guarded(path, *args, **kwargs):
            if str(path).endswith(".jsonl"):
                raise AssertionError("transcript read")
            return original(path, *args, **kwargs)

        with patch.object(Path, "open", guarded):
            r = audit.compare(self.db, [("pi", str(p)) for p in (a, b, c)])
        self.assertEqual(
            r["counts"], dict(checkpoint_current=1, baseline_excluded=1, uncheckpointed=1)
        )
        self.assertEqual(r["current_state"], "pending")
        self.assertNotIn("SECRET", json.dumps(r))

    def test_appended_and_replaced_files_require_rechecking(self):
        p = self.file()
        self.checkpoint(p)
        p.write_text("changed content\n")
        r = audit.compare(self.db, [("pi", str(p))])
        self.assertEqual(r["counts"], dict(changed=1))
        self.assertEqual(r["issues"][0]["observed_bytes"], p.stat().st_size)
        self.assertIsNotNone(r["issues"][0]["checkpoint_offset"])
        self.checkpoint(p)
        p.unlink()
        p.write_text("replacement\n")
        self.assertEqual(audit.compare(self.db, [("pi", str(p))])["current_state"], "pending")

    def test_parse_errors_are_attention_and_budget_exhaustion_is_not_success(self):
        p = self.file()
        self.checkpoint(p, errors=1)
        self.assertEqual(audit.compare(self.db, [("pi", str(p))])["current_state"], "attention")
        r = audit.compare(self.db, [("pi", str(p))], budget=0)
        self.assertEqual(r["checked_paths"], 0)
        self.assertEqual(r["counts"], dict(deferred=1))
        self.assertEqual(r["current_state"], "pending")

    def test_pruning_consumption_and_existing_paths_outside_discovery_remain_distinct(self):
        p = self.file()
        q = self.file("outside.jsonl")
        c = self.file("consumed.jsonl")
        for x in (p, q):
            self.checkpoint(x)
        self.checkpoint(c, "consumed")
        p.unlink()
        c.unlink()
        r = audit.compare(self.db, [])
        self.assertEqual(
            r["tracked_outside_inventory"],
            dict(pruned_after_checkpoint=1, outside_current_discovery=1, consumed=1, deferred=0),
        )
        self.assertIn("last observed checkpoint", r["pruning_scope"])

    def test_discovery_errors_are_reported_even_with_no_candidates(self):
        r = audit.compare(
            self.db,
            [],
            discovery_errors=[
                dict(tool="pi", path="private-root", state="discovery_error", errno=13)
            ],
            discovery_error_count=1,
        )
        self.assertEqual((r["current_state"], r["error_paths"]), ("attention", 1))

    def test_live_walk_surfaces_permission_errors_without_exception_text(self):
        def walk(root, onerror=None):
            onerror(PermissionError(13, "SECRET MESSAGE", str(root)))
            return iter(())

        seen = []
        with patch.object(collect.os, "walk", side_effect=walk):
            self.assertEqual(
                list(
                    collect.log_paths(
                        [str(self.root / "unreadable")], on_error=lambda *args: seen.append(args)
                    )
                ),
                [],
            )
        self.assertEqual(seen, [(str(self.root / "unreadable"), "walk", 13)])
        self.assertNotIn("SECRET", str(seen))

    def test_record_is_cached_and_freshness_is_explicit(self):
        self.assertFalse(audit.status(self.db)["available"])
        p = self.file()
        self.checkpoint(p)
        before = self.db.execute("SELECT count(*) FROM observations").fetchone()[0]
        r = audit.record(self.db, paths=[("pi", str(p))])
        self.assertEqual(self.db.execute("SELECT count(*) FROM observations").fetchone()[0], before)
        self.assertFalse(audit.status(self.db)["stale"])
        with patch.object(audit.time, "time", return_value=r["observed_at"] + 700):
            self.assertTrue(audit.status(self.db)["stale"])
        r["observed_at"] = "invalid"
        with self.db:
            self.db.execute(
                "UPDATE history_config SET value=? WHERE key=?", (json.dumps(r), audit.KEY)
            )
        self.assertEqual(audit.status(self.db)["current_state"], "invalid")

    def test_consumed_outbox_removed_after_inventory_is_not_a_missing_capture(self):
        p = self.file()
        self.checkpoint(p, "consumed")
        p.unlink()
        r = audit.compare(self.db, [("pi", str(p))])
        self.assertEqual(r["counts"], dict(consumed=1))
        self.assertEqual(r["current_state"], "current")

    def test_full_scan_publishes_comparison_and_targeted_scan_keeps_it(self):
        p = self.root / "live.jsonl"
        p.write_text(
            json.dumps(dict(type="session", id="s", timestamp=int(time.time()), cwd="/fixture"))
            + "\n"
        )
        with patch.object(
            history, "config", return_value=dict(content=True, enrolled_at=int(time.time()) - 5)
        ), patch.object(collect, "STATE_DIR", str(self.root)), patch.object(
            collect, "pi_roots", return_value=[str(p)]
        ), patch.object(collect, "claude_roots", return_value=[]), patch.object(
            collect, "native_roots", return_value=[]
        ), patch.object(collect, "opencode_roots", return_value=[]), patch.object(
            collect, "retained_source_rows", return_value=[]
        ), patch.object(collect, "machine_name", return_value="host"):
            result = history.scan(
                self.db, budget=5, scan_sqlite=False, project_usage=False, project_work=False
            )
            self.assertEqual(result["source_inventory"]["counts"], dict(checkpoint_current=1))
            before = audit.status(self.db)["comparison_sha256"]
            fast = history.scan(
                self.db,
                paths=[("pi", str(p))],
                scan_sqlite=False,
                project_usage=False,
                project_work=False,
            )
        self.assertIsNone(fast["source_inventory"])
        self.assertEqual(audit.status(self.db)["comparison_sha256"], before)

    def test_discovery_health_is_not_cleared_by_a_recent_fast_capture(self):
        now = time.time()
        value = dict(
            heartbeat=now,
            started=now,
            poll=2,
            interval=900,
            history_enabled=True,
            source_discovery_enabled=True,
        )
        for worker in (
            "fast",
            "full",
            "sample",
            "context",
            "usage",
            "account_probe",
            "history",
            "discovery",
        ):
            value["last_" + worker] = now
        value["last_discovery_error"] = "source_discovery_gaps"
        p = self.root / "collector-status.json"
        p.write_text(json.dumps(value))
        with patch.object(collect, "STATE_DIR", str(self.root)):
            self.assertEqual(service.status()["capture_state"], "degraded")
            value["last_discovery_error"] = None
            value["last_discovery"] = now - 700
            p.write_text(json.dumps(value))
            self.assertEqual(service.status()["workers"]["discovery"]["state"], "stale")

    def test_sqlite_comparison_uses_database_and_wal_signature(self):
        import sqlite3

        from erg import history_opencode

        p = self.root / "opencode.db"
        source = sqlite3.connect(p)
        source.execute("CREATE TABLE fixture(id INTEGER)")
        source.close()
        with self.db:
            history_opencode.checkpoint(
                self.db, p, dict(signature=history_opencode.signature(p)), "captured"
            )
        self.assertEqual(
            audit.compare(self.db, [("opencode", str(p))])["counts"], dict(checkpoint_current=1)
        )
        Path(str(p) + "-wal").write_bytes(b"changed test WAL")
        self.assertEqual(audit.compare(self.db, [("opencode", str(p))])["counts"], dict(changed=1))

    def test_duplicate_candidates_and_conflicting_tool_ownership_are_explicit(self):
        p = self.file()
        self.checkpoint(p)
        self.assertEqual(
            audit.compare(self.db, [("pi", str(p)), ("pi", str(p))])["inventory_paths"], 1
        )
        r = audit.compare(self.db, [("claude", str(p))])
        self.assertEqual((r["current_state"], r["counts"]), ("attention", dict(tool_conflict=1)))

    def test_reading_cached_status_does_not_restat_sources(self):
        p = self.file()
        self.checkpoint(p)
        audit.record(self.db, paths=[("pi", str(p))])
        with patch.object(
            audit.os, "stat", side_effect=AssertionError("source rescan on reporting read")
        ):
            self.assertTrue(audit.status(self.db)["available"])
