"""Forward SQLite capture: bounded traversal, WAL, mutation and expiry safety."""

import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from erg import collect, history, history_opencode, history_projection, store


class OpenCodeHistory(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for obj, key, value in (
            (collect, "HOME", str(self.root)),
            (collect, "STATE_DIR", str(self.root / "state")),
            (collect, "machine_name", lambda: "host"),
        ):
            p = patch.object(obj, key, value)
            p.start()
            self.addCleanup(p.stop)
        self.db = store.connect(str(self.root / "erg.db"))
        self.addCleanup(self.db.close)
        self.enrolled = int(time.time())
        self.path = self.root / "opencode.db"
        self.source = sqlite3.connect(str(self.path))
        self.addCleanup(self.source.close)
        self.source.executescript("""PRAGMA journal_mode=WAL;
   CREATE TABLE session_v2(id TEXT PRIMARY KEY,directory TEXT,title TEXT);
   CREATE TABLE session_message(id TEXT PRIMARY KEY,session_id TEXT,type TEXT,seq INT,time_created INT,time_updated INT,data TEXT);
   INSERT INTO session_v2 VALUES('s','/code/project','Named OpenCode session');""")

    def row(self, rid, ts, content, role="user"):
        with self.source:
            self.source.execute(
                "INSERT INTO session_message VALUES(?,?,?,?,?,?,?)",
                (
                    rid,
                    "s",
                    "message",
                    1,
                    ts * 1000,
                    ts * 1000,
                    json.dumps(dict(role=role, content=content)),
                ),
            )

    def scan(self, limit=500):
        return history_opencode.scan_file(self.db, self.path, self.enrolled, limit)

    def test_baselines_old_bodies_and_pages_new_records_without_double_counting(self):
        self.row("a", self.enrolled - 100, "OLD")
        self.row("b", self.enrolled + 1, "NEW")
        self.row("c", self.enrolled + 2, "NEXT")
        first = self.scan(1)
        self.assertEqual(first["rows"], 1)
        self.assertEqual(first["events"], 0)
        self.assertEqual(
            self.db.execute("select state from history_sources").fetchone()[0], "pending"
        )
        for _ in range(3):
            self.scan(1)
        events = history.events(self.db, "opencode", "s")["events"]
        self.assertEqual(len(events), 2)
        self.assertNotIn("OLD", json.dumps(events))
        self.assertEqual(
            history.sessions(self.db)["sessions"][0]["title"], "Named OpenCode session"
        )
        self.assertEqual(self.scan()["rows"], 0)
        self.assertEqual(self.db.execute("select count(*) from calls").fetchone()[0], 0)

    def test_wal_only_changes_update_one_native_projection(self):
        self.row("a", self.enrolled + 1, "FIRST")
        self.scan()
        before = self.path.stat().st_mtime_ns
        with self.source:
            self.source.execute(
                "UPDATE session_message SET data=?,time_updated=? WHERE id=?",
                (
                    json.dumps({"role": "assistant", "content": "FINAL"}),
                    (self.enrolled + 2) * 1000,
                    "a",
                ),
            )
        self.assertEqual(self.path.stat().st_mtime_ns, before)
        self.scan()
        events = history.events(self.db, "opencode", "s")["events"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["body"]["data"]["content"], "FINAL")
        self.assertEqual(events[0]["ts"], self.enrolled + 2)

    def test_expiration_tombstones_survive_native_row_mutation(self):
        self.row("a", self.enrolled + 1, "PRIVATE")
        self.scan()
        history_projection.run(self.db, budget=10)
        history.expire(self.db, self.enrolled + 10, True)
        with self.source:
            self.source.execute(
                "UPDATE session_message SET data=?,time_updated=?",
                (json.dumps({"content": "NEW PRIVATE"}), (self.enrolled + 2) * 1000),
            )
        self.scan()
        self.assertIsNone(history.events(self.db, "opencode", "s")["events"][0]["body"])

    def test_failed_page_rolls_back_events_and_retry_does_not_skip_rows(self):
        self.row("a", self.enrolled + 1, "FIRST")
        self.row("b", self.enrolled + 2, "SECOND")
        original = history.put

        def fail_second(*args, **kwargs):
            if args[7] == "session_message:b":
                raise sqlite3.OperationalError("injected failure")
            return original(*args, **kwargs)

        with patch.object(history, "put", side_effect=fail_second):
            result = self.scan()
        self.assertEqual(result["errors"], 1)
        self.assertEqual(self.db.execute("select count(*) from history_events").fetchone()[0], 0)
        self.assertEqual(self.scan()["events"], 2)
        self.assertEqual(len(history.events(self.db, "opencode", "s")["events"]), 2)

    def test_unknown_schema_is_an_explicit_source_problem(self):
        with self.source:
            self.source.execute("DROP TABLE session_message")
        self.assertEqual(self.scan()["errors"], 1)
        self.assertEqual(history.reconciliation(self.db)["source_problem_count"], 1)

    def test_parts_are_preserved_separately_from_their_parent_message(self):
        self.row("a", self.enrolled + 1, "MESSAGE")
        with self.source:
            self.source.execute(
                "CREATE TABLE part(id TEXT PRIMARY KEY,session_id TEXT,time_created INT,data TEXT,message_id TEXT)"
            )
            self.source.execute(
                "INSERT INTO part VALUES(?,?,?,?,?)",
                (
                    "a",
                    "s",
                    (self.enrolled + 1) * 1000,
                    json.dumps(
                        {
                            "type": "tool",
                            "state": {"input": {"command": "pwd"}, "output": "/code/project"},
                        }
                    ),
                    "a",
                ),
            )
        self.scan()
        events = history.events(self.db, "opencode", "s")["events"]
        self.assertEqual(len(events), 2)
        self.assertEqual({r["body"]["table"] for r in events}, {"session_message", "part"})
        self.assertIn("pwd", json.dumps(events))
        part = next(r["body"] for r in events if r["body"]["table"] == "part")
        self.assertEqual(part["parent_message_id"], "a")

    def test_legacy_and_v2_tables_keep_their_correct_parent_sessions(self):
        self.row("v2", self.enrolled + 1, "V2")
        with self.source:
            self.source.execute("CREATE TABLE session(id TEXT PRIMARY KEY,directory TEXT)")
            self.source.execute("INSERT INTO session VALUES('legacy','/code/legacy')")
            self.source.execute(
                "CREATE TABLE message(id TEXT PRIMARY KEY,session_id TEXT,time_created INT,data TEXT)"
            )
            self.source.execute(
                "INSERT INTO message VALUES(?,?,?,?)",
                (
                    "v1",
                    "legacy",
                    (self.enrolled + 1) * 1000,
                    json.dumps({"role": "user", "content": "V1"}),
                ),
            )
        self.assertEqual(self.scan()["errors"], 0)
        self.assertEqual(
            {r["session"] for r in history.sessions(self.db)["sessions"]}, {"s", "legacy"}
        )

    def test_unchanged_sqlite_signature_skips_body_queries_and_repeated_heartbeats(self):
        self.row("a", self.enrolled + 1, "CONTENT")
        self.scan()
        with patch.object(
            history_opencode, "checkpoint", side_effect=AssertionError("idle checkpoint write")
        ), patch.object(
            history_opencode.sqlite3, "connect", side_effect=AssertionError("idle source reopened")
        ):
            result = self.scan()
        self.assertEqual((result["events"], result["rows"]), (0, 0))
        previous = self.db.execute(
            "SELECT last_seen FROM history_sources WHERE path=?", (str(self.path),)
        ).fetchone()[0]
        with patch.object(
            history_opencode.time, "time", return_value=previous + history.HEARTBEAT_INTERVAL + 1
        ):
            self.scan()
        self.assertGreater(
            self.db.execute(
                "SELECT last_seen FROM history_sources WHERE path=?", (str(self.path),)
            ).fetchone()[0],
            previous,
        )
