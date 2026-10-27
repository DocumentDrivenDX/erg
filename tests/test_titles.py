"""Display metadata must not change identities, accounting or PR membership."""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from erg import collect, history, store, titles, work


class Titles(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        for obj, key, value in (
            (collect, "HOME", str(self.home)),
            (collect, "machine_name", lambda: "host"),
        ):
            p = patch.object(obj, key, value)
            p.start()
            self.addCleanup(p.stop)
        self.db = store.connect(str(self.home / "erg.db"))
        self.addCleanup(self.db.close)

    def test_explicit_names_and_renames_win_without_creating_calls(self):
        with self.db:
            titles.put(self.db, "codex", "s", "Generated", "metadata", 20, 100)
            titles.put(self.db, "codex", "s", "Named session", "index", 30, 200)
            titles.put(self.db, "codex", "s", "Older name", "index", 30, 150)
            titles.put(self.db, "codex", "s", "Fallback", "omnigent", 10, 300)
        self.assertEqual(titles.rows(self.db)[0]["title"], "Named session")
        self.assertEqual(self.db.execute("select count(*) from llm").fetchone()[0], 0)

    def test_current_codex_metadata_and_index_titles_are_discovered(self):
        home = self.home / ".codex"
        home.mkdir()
        with sqlite3.connect(str(home / "state_5.sqlite")) as source:
            source.execute("CREATE TABLE threads(id TEXT,title TEXT,name TEXT,updated_at INT)")
            source.execute("INSERT INTO threads VALUES(?,?,?,?)", ("s", "Generated", "Custom", 100))
        (home / "session_index.jsonl").write_text(
            json.dumps(dict(id="s", thread_name="Index", updated_at=200)) + "\n"
        )
        self.assertEqual(titles.refresh(self.db)["errors"], 0)
        self.assertEqual(titles.rows(self.db)[0]["title"], "Custom")

    def test_native_titles_reach_history_and_renames_keep_session_identity(self):
        with self.db:
            history.put(
                self.db, "pi", "s", "p", 100, "message", {"content": "body"}, "m", role="user"
            )
            titles.native(
                self.db, "pi", "s", dict(type="session_info", name="First", timestamp=100)
            )
            titles.native(
                self.db, "pi", "s", dict(type="session_info", name="Renamed", timestamp=200)
            )
        rows = history.sessions(self.db)["sessions"]
        self.assertEqual([(r["session"], r["title"]) for r in rows], [("s", "Renamed")])

    def test_unchanged_pr_title_refresh_preserves_commit_snapshot(self):
        url = "https://github.com/acme/r/pull/1"
        rows = [
            dict(
                t="pull_request",
                uid=url,
                ts=100,
                url=url,
                repo="github.com/acme/r",
                number=1,
                state="open",
                remote_updated_ts=200,
            ),
            dict(t="pr_commit", uid=url + ":sha", ts=100, pr_url=url, sha="abc"),
        ]
        store.ingest(self.db, map(json.dumps, rows), machine="host")
        with patch.object(
            work, "gh_json", return_value=[dict(number=1, title="Readable PR", updated_at=200)]
        ), patch.object(work, "evidence_pulls", return_value=([], [])):
            report = work.refresh(self.db, ["github.com/acme/r"])
        self.assertEqual(report["errors"], [])
        self.assertEqual(
            self.db.execute("select title,ts from pull_requests").fetchone(), ("Readable PR", 100)
        )
        self.assertEqual(self.db.execute("select sha,ts from pr_commits").fetchone(), ("abc", 100))
        self.assertEqual(work.effectiveness(self.db)["prs"][0]["title"], "Readable PR")

    def test_unchanged_sources_skip_open_and_writes_but_changes_and_deadlines_refresh(self):
        home = self.home / ".codex"
        home.mkdir()
        path = home / "state_5.sqlite"
        with sqlite3.connect(str(path)) as source:
            source.execute("CREATE TABLE threads(id TEXT,title TEXT,updated_at INT)")
            source.execute("INSERT INTO threads VALUES('s','First',100)")
        cache = {}
        with patch.object(titles.time, "monotonic", return_value=10):
            self.assertEqual(titles.refresh(self.db, cache)["read_sources"], 1)
        changes = self.db.total_changes
        with patch.object(titles.time, "monotonic", return_value=20), patch.object(
            titles.sqlite3, "connect", side_effect=AssertionError("opened unchanged source")
        ):
            self.assertEqual(titles.refresh(self.db, cache)["unchanged_sources"], 1)
        self.assertEqual(self.db.total_changes, changes)
        with sqlite3.connect(str(path)) as source:
            source.execute("UPDATE threads SET title='Second',updated_at=200")
        with patch.object(titles.time, "monotonic", return_value=30):
            self.assertEqual(titles.refresh(self.db, cache)["read_sources"], 1)
        self.assertEqual(titles.rows(self.db)[0]["title"], "Second")
        changes = self.db.total_changes
        with patch.object(titles.time, "monotonic", return_value=331):
            self.assertEqual(titles.refresh(self.db, cache)["read_sources"], 1)
        self.assertEqual(self.db.total_changes, changes)

    def test_source_errors_retry_and_failed_destination_does_not_cache_success(self):
        home = self.home / ".codex"
        home.mkdir()
        path = home / "state_5.sqlite"
        with sqlite3.connect(str(path)) as source:
            source.execute("CREATE TABLE threads(id TEXT,title TEXT,updated_at INT)")
            source.execute("INSERT INTO threads VALUES('s','First',100)")
        cache = {}
        db = self.db

        class FailedCommit:
            def execute(self, *args):
                return db.execute(*args)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                db.rollback()
                raise sqlite3.OperationalError("destination failed")

        with self.assertRaises(sqlite3.OperationalError):
            titles.refresh(FailedCommit(), cache)
        self.assertEqual(cache, {})
        self.assertEqual(titles.rows(db), [])
        self.assertEqual(titles.refresh(db, cache)["read_sources"], 1)
        path.write_bytes(b"invalid source database")
        self.assertEqual(titles.refresh(db, cache)["errors"], 1)
        self.assertEqual(titles.refresh(db, cache)["errors"], 1)

    def test_incremental_database_read_is_bounded_and_safety_scan_is_not_starved(self):
        home = self.home / ".codex"
        home.mkdir()
        path = home / "state_5.sqlite"
        with sqlite3.connect(str(path)) as source:
            source.execute(
                "CREATE TABLE threads(id TEXT,title TEXT,updated_at INT,updated_at_ms INT)"
            )
            source.executemany(
                "INSERT INTO threads VALUES(?,?,?,?)",
                [(str(i), "Title " + str(i), 100 + i, 100000 + i) for i in range(1000)],
            )
        cache = {}
        with patch.object(titles.time, "monotonic", return_value=10):
            first = titles.refresh(self.db, cache)
        self.assertEqual(first["read_rows"], 1000)
        for clock in (70, 130, 190, 250):
            with sqlite3.connect(str(path)) as source:
                source.execute(
                    "UPDATE threads SET title='Active',updated_at_ms=updated_at_ms+1 WHERE id='999'"
                )
            with patch.object(titles.time, "monotonic", return_value=clock):
                result = titles.refresh(self.db, cache)
            self.assertEqual(result["incremental_sources"], 1)
            self.assertEqual(result["read_rows"], 1)
        # Backdated edits must be found even if unrelated activity keeps changing WAL.
        with sqlite3.connect(str(path)) as source:
            source.execute("UPDATE threads SET title='Backdated' WHERE id='0'")
        with patch.object(titles.time, "monotonic", return_value=311):
            result = titles.refresh(self.db, cache)
        self.assertEqual(result["incremental_sources"], 0)
        self.assertEqual(result["read_rows"], 1000)
        self.assertEqual(
            self.db.execute("SELECT title FROM session_titles WHERE session='0'").fetchone()[0],
            "Backdated",
        )

    def test_title_index_tails_appends_and_detects_replacements_and_rewrites(self):
        home = self.home / ".codex"
        home.mkdir()
        path = home / "session_index.jsonl"

        def row(sid, title):
            return json.dumps(dict(id=sid, thread_name=title, updated_at=200)) + "\n"

        path.write_text("".join(row(str(i), "First") for i in range(100)))
        cache = {}
        titles.refresh(self.db, cache)
        with path.open("a") as stream:
            stream.write(row("new", "New"))
        result = titles.refresh(self.db, cache)
        self.assertEqual(result["read_rows"], 1)
        self.assertEqual(result["incremental_sources"], 1)
        # Replacing the inode must reset the source checkpoint.
        replacement = path.with_suffix(".new")
        replacement.write_text(row("replacement", "Replaced"))
        replacement.replace(path)
        self.assertEqual(titles.refresh(self.db, cache)["incremental_sources"], 0)
        self.assertEqual(
            self.db.execute(
                "SELECT title FROM session_titles WHERE session='replacement'"
            ).fetchone()[0],
            "Replaced",
        )
        # A same-size rewrite is a full read, not an empty tail.
        path.write_text(row("replacement", "Renamed!"))
        self.assertEqual(titles.refresh(self.db, cache)["incremental_sources"], 0)
        self.assertEqual(
            self.db.execute(
                "SELECT title FROM session_titles WHERE session='replacement'"
            ).fetchone()[0],
            "Renamed!",
        )
        self.assertNotIn("Renamed!", str(cache))

    def test_source_reads_never_hold_the_destination_writer_and_bad_append_retries(self):
        home = self.home / ".codex"
        home.mkdir()
        path = home / "session_index.jsonl"
        path.write_text(json.dumps(dict(id="s", thread_name="Title", updated_at=100)) + "\n")
        with sqlite3.connect(str(home / "state_5.sqlite")) as source:
            source.execute("CREATE TABLE threads(id TEXT,title TEXT,updated_at INT)")
            source.execute("INSERT INTO threads VALUES('database-title','Database title',100)")
        cache = {}
        original = Path.open

        def opened(path, *args, **kwargs):
            self.assertFalse(self.db.in_transaction)
            return original(path, *args, **kwargs)

        with patch.object(Path, "open", opened):
            titles.refresh(self.db, cache)
        before = dict(cache)
        with path.open("a") as stream:
            stream.write('{"id":"new",')
        self.assertEqual(titles.refresh(self.db, cache)["errors"], 1)
        self.assertEqual(cache, before)
        with path.open("a") as stream:
            stream.write('"thread_name":"Finished"}\n')
        self.assertEqual(titles.refresh(self.db, cache)["errors"], 0)
        self.assertEqual(
            self.db.execute("SELECT title FROM session_titles WHERE session='new'").fetchone()[0],
            "Finished",
        )

    def test_non_numeric_schema_falls_back_without_missing_equal_timestamp_rename(self):
        home = self.home / ".codex"
        home.mkdir()
        path = home / "state_5.sqlite"
        with sqlite3.connect(str(path)) as source:
            source.execute("CREATE TABLE threads(id TEXT,title TEXT,updated_at TEXT)")
            source.execute("INSERT INTO threads VALUES('s','First','2026-10-21T00:00:00Z')")
        cache = {}
        titles.refresh(self.db, cache)
        with sqlite3.connect(str(path)) as source:
            source.execute("UPDATE threads SET title='Second'")
        result = titles.refresh(self.db, cache)
        self.assertEqual(result["incremental_sources"], 0)
        self.assertEqual(titles.rows(self.db)[0]["title"], "Second")
        with sqlite3.connect(str(path)) as source:
            source.execute("UPDATE threads SET title='Boundary'")
        result = titles.refresh(self.db, cache)
        self.assertEqual(result["read_rows"], 1)
        self.assertEqual(titles.rows(self.db)[0]["title"], "Boundary")

    def test_numeric_timestamp_boundary_is_revisited(self):
        home = self.home / ".codex"
        home.mkdir()
        path = home / "state_5.sqlite"
        with sqlite3.connect(str(path)) as source:
            source.execute("CREATE TABLE threads(id TEXT,title TEXT,updated_at INT)")
            source.executemany(
                "INSERT INTO threads VALUES(?,?,?)",
                [("a", "First", 100), ("b", "Second", 100), ("old", "Old", 50)],
            )
        cache = {}
        titles.refresh(self.db, cache)
        with sqlite3.connect(str(path)) as source:
            source.execute("UPDATE threads SET title='Renamed' WHERE id='a'")
        result = titles.refresh(self.db, cache)
        self.assertEqual(result["incremental_sources"], 1)
        self.assertEqual(result["read_rows"], 2)
        self.assertEqual(
            self.db.execute("SELECT title FROM session_titles WHERE session='a'").fetchone()[0],
            "Renamed",
        )
