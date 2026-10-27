"""Prove idle polling avoids per-file writes without weakening progress atomicity."""

import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from erg import collect, history, history_projection, store, work


class CaptureEfficiency(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / "state"
        self.state.mkdir()
        for obj, key, value in (
            (collect, "HOME", str(self.root)),
            (collect, "STATE_DIR", str(self.state)),
        ):
            p = patch.object(obj, key, value)
            p.start()
            self.addCleanup(p.stop)
        self.db = store.connect(str(self.root / "db"))
        self.addCleanup(self.db.close)
        history.enroll(self.db)
        self.enrolled = int(time.time())
        self.logs = self.root / "logs"
        self.logs.mkdir()

    def line(self, path, value):
        with path.open("a") as stream:
            stream.write(json.dumps(value) + "\n")

    def header(self, path):
        self.line(
            path,
            dict(type="session_meta", timestamp=self.enrolled, payload=dict(id="s", cwd="/code/p")),
        )

    def scan(self):
        with patch.object(collect, "pi_roots", return_value=[str(self.logs)]), patch.object(
            collect, "claude_roots", return_value=[]
        ), patch.object(collect, "native_roots", return_value=[]), patch.object(
            collect, "opencode_roots", return_value=[]
        ):
            return history.scan(self.db)

    def baselines(self, n=128):
        for i in range(n):
            path = self.logs / (str(i) + ".jsonl")
            self.line(
                path,
                dict(type="session", id="s" + str(i), timestamp=self.enrolled - 100, cwd="/code/p"),
            )
            os.utime(path, (self.enrolled - 100, self.enrolled - 100))
        self.scan()

    def test_many_idle_sources_use_one_batched_heartbeat_then_no_source_writes(self):
        self.baselines()
        last_seen = self.db.execute("select max(last_seen) from history_sources").fetchone()[0]
        queries = []
        self.db.set_trace_callback(queries.append)
        with patch.object(history.time, "time", return_value=last_seen + 61):
            first = self.scan()
        self.assertEqual(first["heartbeat_writes"], 128)
        self.assertEqual(first["unchanged_files"], 128)
        self.assertLessEqual(sum(q.startswith("BEGIN") for q in queries), 4)
        queries.clear()
        with patch.object(history.time, "time", return_value=last_seen + 62):
            second = self.scan()
        self.assertEqual(second["heartbeat_writes"], 0)
        self.assertFalse(any(q.startswith("UPDATE history_sources") for q in queries))
        self.assertEqual(self.db.execute("select count(*) from history_events").fetchone()[0], 0)

    def test_new_append_is_captured_even_while_heartbeat_is_throttled(self):
        self.baselines(1)
        path = self.logs / "0.jsonl"
        self.line(
            path,
            dict(
                type="message",
                id="new",
                timestamp=self.enrolled + 1,
                message=dict(role="assistant", model="m", usage=dict(input=2, output=3)),
            ),
        )
        result = self.scan()
        self.assertEqual(result["events"], 1)
        self.assertEqual(self.db.execute("select fresh_in,out from calls").fetchone(), (2, 3))
        self.assertEqual(
            self.db.execute("select offset from history_sources").fetchone()[0], path.stat().st_size
        )

    def test_unchanged_parse_error_stays_visible_and_budget_does_not_skip_a_path(self):
        path = self.logs / "s.jsonl"
        self.line(path, dict(type="session", id="s", timestamp=self.enrolled + 1))
        self.line(
            path,
            dict(
                type="message",
                id="a",
                timestamp=self.enrolled + 1,
                message=dict(role="user", content="text"),
            ),
        )
        with path.open("a") as stream:
            stream.write("invalid-json\n")
        self.assertGreater(self.scan()["errors"], 0)
        self.assertGreater(self.scan()["errors"], 0)
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO history_config VALUES('scan_cursor','0')")
        with patch.object(collect, "pi_roots", return_value=[str(self.logs)]), patch.object(
            collect, "claude_roots", return_value=[]
        ), patch.object(collect, "native_roots", return_value=[]):
            result = history.scan(self.db, budget=0)
        self.assertEqual(result["checked_files"], 0)
        self.assertEqual(
            self.db.execute("select value from history_config where key='scan_cursor'").fetchone()[
                0
            ],
            "0",
        )

    def test_unchanged_work_context_does_not_open_transcript_or_write_transaction(self):
        path = self.root / "s.jsonl"
        self.header(path)
        with patch.object(work, "git_context", return_value={"repo": "github.com/a/r"}):
            work.capture_file(self.db, "codex", path)
        queries = []
        self.db.set_trace_callback(queries.append)
        with patch.object(
            collect, "transcript", side_effect=AssertionError("unchanged file was opened")
        ):
            self.assertEqual(work.capture_file(self.db, "codex", path), 0)
        self.assertFalse(any(q.startswith(("BEGIN", "INSERT", "UPDATE")) for q in queries))

    def test_git_inspection_is_outside_writer_transaction(self):
        path = self.root / "s.jsonl"
        self.header(path)

        def git(*args):
            self.assertFalse(self.db.in_transaction)
            return {"repo": "github.com/a/r"}

        with patch.object(work, "git_context", side_effect=git):
            self.assertGreater(work.capture_file(self.db, "codex", path), 0)

    def test_concurrent_progress_winner_prevents_stale_ingest(self):
        path = self.root / "s.jsonl"
        self.header(path)
        stat = path.stat()
        inode = "%s:%s" % (stat.st_dev, stat.st_ino)

        def git(*args):
            with self.db:
                self.db.execute(
                    "INSERT OR REPLACE INTO native_offsets VALUES(?,?,?,?,?)",
                    (str(path), inode, stat.st_size, 1, "{}"),
                )
            return {"repo": "github.com/a/r"}

        with patch.object(work, "git_context", side_effect=git):
            self.assertEqual(work.capture_file(self.db, "codex", path), 0)
        self.assertEqual(self.db.execute("select count(*) from observations").fetchone()[0], 0)
        self.assertEqual(
            self.db.execute("select offset from native_offsets").fetchone()[0], stat.st_size
        )

    def test_ingest_failure_preserves_progress_and_rolls_back_observations(self):
        path = self.root / "s.jsonl"
        self.header(path)
        original = store.ingest

        def fail(*args, **kwargs):
            original(*args, **kwargs)
            raise ValueError("injected")

        with patch.object(
            work, "git_context", return_value={"repo": "github.com/a/r"}
        ), patch.object(store, "ingest", side_effect=fail):
            with self.assertRaises(ValueError):
                work.capture_file(self.db, "codex", path)
        self.assertEqual(self.db.execute("select count(*) from observations").fetchone()[0], 0)
        self.assertEqual(self.db.execute("select count(*) from native_offsets").fetchone()[0], 0)


class IdleWriterExclusion(unittest.TestCase):
    """Idle capture must remain readable while another connection owns the writer."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "ledger"
        self.db = store.connect(str(self.path))
        self.addCleanup(self.db.close)

    def blocked_reader(self):
        other = sqlite3.connect(str(self.path), timeout=0)
        self.addCleanup(other.close)
        self.db.execute("BEGIN IMMEDIATE")
        return other

    def test_empty_projection_and_inference_linking_do_not_acquire_writer(self):
        other = self.blocked_reader()
        self.assertEqual(history_projection.run(other), dict(processed=0, errors=0, pending=0))
        history.link_inferences(other)
        self.assertFalse(other.in_transaction)

    def test_retry_only_projection_does_not_acquire_writer_or_clear_pending(self):
        with self.db:
            history.put(
                self.db,
                "pi",
                "s",
                "project",
                100,
                "message",
                dict(message=dict(role="user", content="retained")),
                "entry",
            )
            self.db.execute("UPDATE history_pending SET retry_at=?", (int(time.time()) + 3600,))
        other = self.blocked_reader()
        self.assertEqual(history_projection.run(other), dict(processed=0, errors=0, pending=1))
        self.assertEqual(other.execute("SELECT count(*) FROM history_pending").fetchone()[0], 1)
        self.assertFalse(other.in_transaction)

    def test_idle_calls_do_not_commit_a_callers_existing_transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        self.db.execute("INSERT INTO history_config VALUES('caller-uncommitted','1')")
        history_projection.run(self.db)
        history.link_inferences(self.db)
        self.assertTrue(self.db.in_transaction)
        self.db.rollback()
        self.assertIsNone(
            self.db.execute(
                "SELECT value FROM history_config WHERE key='caller-uncommitted'"
            ).fetchone()
        )

    def test_empty_projection_and_linking_work_on_a_read_only_connection(self):
        reader = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
        self.addCleanup(reader.close)
        self.assertEqual(history_projection.run(reader), dict(processed=0, errors=0, pending=0))
        history.link_inferences(reader)
