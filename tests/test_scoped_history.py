"""Universal hierarchy browsing keeps native session identity and append stability."""

import sqlite3
import unittest
from unittest.mock import patch

import test_projects as fixtures

from erg import history, project_scope


class ScopedHistory(unittest.TestCase):
    setUp = fixtures.ProjectAccounting.setUp
    rule = fixtures.ProjectAccounting.rule
    configure = fixtures.ProjectAccounting.configure
    context = fixtures.ProjectAccounting.context

    def put(self, uid, session="s", ts=100, tool="pi"):
        with self.db:
            history.put(
                self.db,
                tool,
                session,
                "folder-hint",
                ts,
                "message",
                dict(content="private body"),
                uid,
                machine="host",
                role="user",
            )

    def test_one_session_across_folder_moves_and_temporal_human_projects(self):
        self.configure(
            [
                self.rule(selectors={"directory": {"machine": "host", "root": "/work/app"}}),
                self.rule(
                    binding_id="q",
                    selectors={"directory": {"machine": "host", "root": "/work/other"}},
                    allocations=[dict(project_key="q", units=1000000)],
                ),
            ]
        )
        self.context(50)
        self.put("first", ts=100)
        self.context(120, "/work/other")
        self.put("later", ts=150)
        data = history.scoped_sessions(self.db, dict(start=1, end=200))
        self.assertEqual(len(data["sessions"]), 1)
        self.assertEqual(data["sessions"][0]["events"], 2)
        self.assertEqual(data["sessions"][0]["project_keys"], ["p", "q"])
        self.assertEqual(len(data["sessions"][0]["folder_keys"]), 2)
        for key, ts in (("p", 100), ("q", 150)):
            data = history.scoped_sessions(self.db, dict(start=1, end=200, project_key=key))
            self.assertEqual(data["sessions"][0]["events"], 1)
            self.assertEqual(data["sessions"][0]["first_ts"], ts)
        # Filtering did not rewrite source data or remove the other retained event.
        self.assertEqual(len(history.events(self.db, "pi", "s")["events"]), 2)

    def test_high_water_cursor_prevents_duplicate_sessions_during_appends(self):
        self.configure([])
        self.put("a", session="a")
        self.put("b", session="b")
        first = history.scoped_sessions(self.db, dict(start=1, end=200), limit=1)
        self.assertEqual(first["sessions"][0]["session"], "a")
        self.assertTrue(first["more"])
        self.put("new-a", session="a", ts=110)
        self.put("new-c", session="c", ts=120)
        second = history.scoped_sessions(
            self.db,
            dict(start=1, end=200),
            after=first["next_cursor"],
            through_seq=first["through_seq"],
            limit=1,
        )
        self.assertEqual(second["sessions"][0]["session"], "b")
        self.assertFalse(second["more"])
        self.assertEqual(second["through_seq"], first["through_seq"])
        self.assertEqual(
            history.scoped_sessions(self.db, dict(start=1, end=200))["sessions"][-1]["session"], "c"
        )

    def test_split_project_history_counts_physical_records_and_deduplicates_receipts(self):
        self.configure(
            [
                self.rule(
                    selectors={"session": {"machine": "host", "tool": "pi", "id": "s"}},
                    allocations=[dict(project_key="p", units=600000)],
                )
            ]
        )
        self.put("native")
        self.put("server", session="inference:i", tool="local")
        with self.db:
            self.db.execute(
                "INSERT INTO history_links VALUES('inference:i','pi','s','response',100)"
            )
        result = history.scoped_sessions(self.db, dict(start=1, end=200, project_key="p"))
        self.assertEqual(len(result["sessions"]), 1)
        self.assertEqual(result["sessions"][0]["events"], 2)
        self.assertEqual(result["sessions"][0]["tool"], "pi")
        self.assertEqual(result["observed_tools"], ["pi"])
        self.assertIn("not additive", result["count_basis"])

    def test_source_bodies_are_never_selected_and_snapshot_ownership_is_preserved(self):
        self.configure([])
        self.put("a")
        self.db.set_authorizer(
            lambda action, table, column, *unused: (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_READ and table == "history_events" and column == "body"
                else sqlite3.SQLITE_OK
            )
        )
        try:
            history.scoped_sessions(self.db, dict(start=1, end=200))
        finally:
            self.db.set_authorizer(lambda *args: sqlite3.SQLITE_OK)
        self.assertFalse(self.db.in_transaction)
        self.db.execute("BEGIN")
        history.scoped_sessions(self.db, dict(start=1, end=200))
        self.assertTrue(self.db.in_transaction)
        self.db.rollback()
        for args in (
            dict(after=-1),
            dict(through_seq=-1),
            dict(filters=dict(project_key="missing", start=1, end=200)),
        ):
            with self.assertRaises(ValueError):
                history.scoped_sessions(self.db, **args)

    def test_exact_session_filters_before_python_and_compacts_matching_metadata(self):
        from erg import report_scope

        self.configure([])
        for n in range(100):
            self.put("other" + str(n), session="other", tool="codex")
        for n in range(3):
            self.put("wanted" + str(n), session="target", ts=100)
        self.put("later", session="target", ts=101)
        accepted = []
        original = report_scope.Selection.accepts

        def observe(selection, row):
            accepted.append((row["tool"], row["session"], row["ts"]))
            return original(selection, row)

        with patch.object(report_scope.Selection, "accepts", observe):
            data = history.scoped_sessions(
                self.db, dict(tool="pi", session="target", start=1, end=200)
            )
        self.assertCountEqual(accepted, [("pi", "target", 100), ("pi", "target", 101)])
        self.assertEqual(data["sessions"][0]["events"], 4)
        self.assertEqual(data["observed_tools"], ["codex", "pi"])

    def test_native_identity_filter_includes_only_its_linked_server_receipts(self):
        self.configure([])
        self.put("native", session="target")
        self.put("server", session="inference:a", tool="local")
        self.put("unrelated", session="inference:b", tool="local")
        self.put("unlinked", session="target", tool="local")
        with self.db:
            self.db.execute("INSERT INTO history_links VALUES('inference:a','pi','target','a',100)")
            self.db.execute(
                "INSERT INTO history_links VALUES('inference:b','claude','other','b',100)"
            )
        data = history.scoped_sessions(self.db, dict(tool="pi", session="target", start=1, end=200))
        self.assertEqual(
            [(r["tool"], r["session"], r["events"]) for r in data["sessions"]],
            [("pi", "target", 2)],
        )
        self.assertEqual(data["observed_tools"], ["claude", "local", "pi"])
        self.assertEqual(
            history.scoped_sessions(
                self.db, dict(tool="local", session="inference:a", start=1, end=200)
            )["sessions"],
            [],
        )
        self.assertEqual(
            history.scoped_sessions(
                self.db, dict(tools="codex,pi", session="target", start=1, end=200)
            )["sessions"][0]["events"],
            2,
        )
        self.assertEqual(
            history.scoped_sessions(self.db, dict(tool="pi", tools="claude", start=1, end=200))[
                "sessions"
            ],
            [],
        )

    def test_compacted_metadata_preserves_expired_counts_and_cursor(self):
        self.configure([])
        self.put("one")
        self.put("two")
        self.put("three")
        last = self.db.execute("SELECT MAX(seq) FROM history_events").fetchone()[0]
        with self.db:
            self.db.execute(
                "UPDATE history_events SET body=NULL,expired_at=150 WHERE seq=?", (last,)
            )
        data = history.scoped_sessions(self.db, dict(tool="pi", session="s", start=1, end=200))
        row = data["sessions"][0]
        self.assertEqual((row["events"], row["expired_events"], row["cursor"]), (3, 1, last))

    def test_allocation_cache_respects_context_and_policy_edges(self):
        self.configure(
            [
                self.rule(
                    selectors={"directory": {"machine": "host", "root": "/work/app"}},
                    valid_from=100,
                    valid_to=150,
                )
            ]
        )
        self.context(50)
        for ts in (99, 100, 101, 149, 150, 151):
            self.put("time" + str(ts), ts=ts)
        self.context(151, "/work/other")
        original = project_scope.Scope.weight
        resolved = []

        def observe(scope, row, *args):
            resolved.append(row["ts"])
            return original(scope, row, *args)

        with patch.object(project_scope.Scope, "weight", observe):
            data = history.scoped_sessions(self.db, dict(start=1, end=200, project_key="p"))
        self.assertEqual(data["sessions"][0]["events"], 3)
        self.assertEqual(
            (data["sessions"][0]["first_ts"], data["sessions"][0]["last_ts"]), (100, 149)
        )
        self.assertEqual(resolved, [99, 100, 150, 151])
        # Cache is scoped to a request, including folder and human-project filters.
        other = history.scoped_sessions(self.db, dict(start=1, end=200, project_key="@unassigned"))
        self.assertEqual(other["sessions"][0]["events"], 3)
