"""Session drill-down must preserve canonical accounting without a corpus report."""

import contextlib
import io
import json
import random
import sqlite3
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

from erg import accounting, collect, routes, server, session_usage, store, work


class SessionUsage(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "ledger"
        self.db = store.connect(str(self.path))
        self.addCleanup(self.db.close)

    def ingest(self, *events, machine="host"):
        store.ingest(self.db, map(json.dumps, events), machine=machine)

    def call(self, uid="call", **fields):
        return dict(
            dict(
                t="llm",
                uid=uid,
                ts=20,
                tool="codex",
                session="s",
                project="p",
                model="m",
                fresh_in=10,
                cache_read=2,
                cache_write=3,
                out=5,
                reasoning=1,
            ),
            **fields,
        )

    def expected(self, tool, session, columns):
        return list(
            self.db.execute(
                "SELECT "
                + ",".join(columns)
                + " FROM calls WHERE tool=? AND session=? ORDER BY ts",
                (tool, session),
            )
        )

    def test_targeted_calls_equal_global_oracle_across_replays_and_receipt_matches(self):
        columns = "machine uid ts tool session model fresh_in out capture_rank".split()
        for seed in range(12):
            rng = random.Random(seed)
            with self.db:
                self.db.execute("DELETE FROM llm")
                for i in range(300):
                    event = self.call(
                        str(rng.randrange(40)),
                        tool=rng.choice(("codex", "claude", "pi", "new-agent", "local", "gateway")),
                        session=rng.choice(("s", "other")),
                        ts=rng.randrange(10, 30),
                        out=rng.choice((0, 2, 5)),
                        usage_kind=rng.choice(
                            ("observed", "observed", "unknown", "synthetic_history")
                        ),
                        request_id=rng.choice(("", None, "request", "other")),
                        response_id=rng.choice(("", None, "response", "other")),
                        model=rng.choice(("m", "n", None)),
                    )
                    # Valid captured UIDs are unique per machine, regardless of tool.
                    store.ingest(
                        self.db,
                        [json.dumps(event)],
                        machine=rng.choice(("a", "b", "c")),
                        transaction=False,
                    )
            for tool in ("codex", "claude", "pi", "new-agent", "local", "gateway"):
                for sid in ("s", "other"):
                    actual = list(session_usage.calls(self.db, tool, sid, columns))
                    expected = self.expected(tool, sid, columns)
                    self.assertEqual(
                        sorted(actual, key=repr), sorted(expected, key=repr), (seed, tool, sid)
                    )

    def test_winner_in_another_session_is_not_resurrected(self):
        self.ingest(self.call(), machine="a")
        self.ingest(self.call(session="other", out=10), machine="b")
        self.assertEqual(list(session_usage.calls(self.db, "codex", "s")), [])
        self.assertEqual(len(list(session_usage.calls(self.db, "codex", "other"))), 1)
        self.ingest(self.call("synthetic", usage_kind="synthetic_history"))
        self.assertEqual(list(session_usage.calls(self.db, "codex", "s")), [])

    def test_unrelated_native_history_does_not_drive_query_work(self):
        self.ingest(self.call())
        columns = "uid ts tool session".split()
        steps = [0]

        def tick():
            steps[0] += 1
            return 0

        self.db.set_progress_handler(tick, 1)
        expected = list(session_usage.calls(self.db, "codex", "s", columns))
        before = steps[0]
        self.db.set_progress_handler(None, 0)
        with self.db:
            self.db.executemany(
                "INSERT INTO llm(machine,uid,ts,tool,session,fresh_in,out,usage_kind) VALUES('host',?,20,'codex','unrelated',10,1,'observed')",
                [("unrelated-" + str(i),) for i in range(5000)],
            )
        steps[0] = 0
        self.db.set_progress_handler(tick, 1)
        self.assertEqual(list(session_usage.calls(self.db, "codex", "s", columns)), expected)
        self.db.set_progress_handler(None, 0)
        # VM instructions, rather than timing, prove the index avoids an unrelated scan.
        self.assertLess(steps[0], before + 100)

    def test_session_metrics_equal_full_report_with_unknowns_and_route_changes(self):
        self.ingest(
            collect.session_record("codex", "s", 10),
            self.call(),
            self.call("later", ts=40, model="gpt-5", reported_cost_usd=0.25),
            collect.session_record("pi", "summary", 10, usage_status="summary_only", summary_in=50),
            routes.event(
                "codex",
                "s",
                30,
                dict(provider="custom", base_url="https://gateway.example/v1"),
                "lifecycle",
            ),
        )
        for host in ("a.example", "b.example"):
            self.ingest(
                routes.event(
                    "codex",
                    "s",
                    40,
                    dict(provider="custom", base_url="https://" + host + "/v1"),
                    "native",
                )
            )
        identity = dict(
            t="identity",
            uid="identity",
            ts=10,
            tool="codex",
            session="s",
            provider="openai",
            provenance="codex_creator_account",
            **accounting.identity_fields(dict(account_id="account", plan="dynamic-plan"), "openai"),
        )
        self.ingest(
            identity,
            dict(
                t="quota",
                uid="quota",
                tool="codex",
                session="s",
                ts=30,
                plan="observed-plan",
                event="snapshot",
            ),
        )
        expected = {(r["tool"], r["session"]): r for r in work.effectiveness(self.db)["sessions"]}
        for key, value in expected.items():
            self.assertEqual(session_usage.metrics(self.db, *key), value)
        self.assertIsNone(session_usage.metrics(self.db, "codex", "missing"))
        self.assertIsNone(expected[("codex", "s")]["actual_billed_usd"])
        self.assertIsNone(expected[("codex", "s")]["billing_account"])

    def test_accounting_order_and_cost_sightings_preserve_existing_report(self):
        for uid, ts, cost in [("a", 30, 0.1), ("b", 20, 0.2), ("c", 10, 0.3)]:
            self.ingest(self.call(uid, ts=ts, reported_cost_usd=cost))
        self.ingest(
            dict(
                t="session_cost",
                uid="cost",
                ts=10,
                tool="codex",
                session="s",
                usd=2,
                complete=1,
                provenance="agent_reported_estimate",
            ),
            machine="a",
        )
        self.ingest(
            dict(
                t="session_cost",
                uid="cost",
                ts=30,
                tool="codex",
                session="s",
                usd=1,
                complete=1,
                provenance="agent_reported_estimate",
            ),
            machine="b",
        )
        expected = work.effectiveness(self.db)["sessions"][0]
        actual = session_usage.metrics(self.db, "codex", "s")
        self.assertEqual(actual, expected)
        self.assertEqual(actual["reported_call_value"], 0.1 + 0.2 + 0.3)
        self.assertEqual(actual["reported_session_value"]["usd"], 2)

    def test_drilldown_does_not_build_or_invalidate_whole_effectiveness_report(self):
        self.ingest(self.call())
        reports = server.Reports()
        sentinel = (123, {"cached": "keep"})
        reports.work_cache = sentinel
        with patch.object(
            store,
            "open_readonly",
            side_effect=lambda: sqlite3.connect("file:" + str(self.path) + "?mode=ro", uri=True),
        ), patch.object(
            work, "effectiveness", side_effect=AssertionError("whole report requested")
        ):
            result = reports.session("codex", "s")
        self.assertEqual(result["metrics"]["total_tokens"], 20)
        self.assertEqual(len(result["calls"]), 1)
        self.assertFalse(result["calls_truncated"])
        self.assertIs(reports.work_cache, sentinel)

    def test_call_detail_and_metrics_share_one_snapshot(self):
        self.ingest(self.call())
        metrics = session_usage.metrics

        def concurrent_write(db, *args):
            self.ingest(self.call("concurrent", out=100))
            return metrics(db, *args)

        with patch.object(
            store,
            "open_readonly",
            side_effect=lambda: sqlite3.connect("file:" + str(self.path) + "?mode=ro", uri=True),
        ), patch.object(session_usage, "metrics", side_effect=concurrent_write):
            result = server.Reports().session("codex", "s")
        self.assertEqual(result["metrics"]["calls"], 1)
        self.assertEqual(len(result["calls"]), 1)
        self.assertEqual(session_usage.metrics(self.db, "codex", "s")["calls"], 2)

    def test_http_drilldown_is_complete_and_never_requests_pr_report(self):
        self.ingest(*(self.call("call-" + str(i)) for i in range(1001)))
        created = []
        ready = threading.Event()
        real_server = server.http.server.ThreadingHTTPServer

        def make(address, handler):
            instance = real_server(address, handler)
            created.append(instance)
            ready.set()
            return instance

        with patch.object(
            server.http.server, "ThreadingHTTPServer", side_effect=make
        ), patch.object(
            store,
            "open_readonly",
            side_effect=lambda: sqlite3.connect("file:" + str(self.path) + "?mode=ro", uri=True),
        ), patch.object(
            work, "effectiveness", side_effect=AssertionError("whole report requested")
        ), contextlib.redirect_stdout(io.StringIO()):
            worker = threading.Thread(target=server.serve, args=(0,), daemon=True)
            worker.start()
            try:
                self.assertTrue(ready.wait(3))
                url = "http://127.0.0.1:" + str(created[0].server_port)
                with urllib.request.urlopen(
                    url + "/api/session?tool=codex&session=s", timeout=5
                ) as response:
                    detail = json.load(response)
                self.assertEqual(len(detail["calls"]), 1000)
                self.assertTrue(detail["calls_truncated"])
                self.assertEqual(detail["metrics"]["calls"], 1001)
                self.assertEqual(detail["metrics"]["total_tokens"], 20020)
                self.assertIsNone(detail["metrics"]["actual_billed_usd"])
                with urllib.request.urlopen(url + "/healthz", timeout=5) as response:
                    health = json.load(response)
                self.assertNotIn("/api/effectiveness", health["request_cpu"]["routes"])
            finally:
                if created:
                    created[0].shutdown()
                worker.join(3)

    def test_dashboard_rollups_keep_one_snapshot_during_capture(self):
        self.ingest(collect.session_record("codex", "s", 20), self.call())
        session_rows = store.unified.session_rows

        def append_after_sessions(db, *args):
            result = session_rows(db, *args)
            self.ingest(
                collect.session_record("codex", "new", 30), self.call("new", session="new", ts=30)
            )
            return result

        with patch.object(
            store,
            "open_readonly",
            side_effect=lambda: sqlite3.connect("file:" + str(self.path) + "?mode=ro", uri=True),
        ), patch.object(store.unified, "session_rows", side_effect=append_after_sessions), patch(
            "erg.service.status", return_value={}
        ):
            result = server.Reports().data(0)
        self.assertEqual(result["session_count"], 1)
        self.assertEqual(len(result["sessions"]), 1)
        self.assertEqual(sum(row[11] for row in result["llm"]), 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM calls").fetchone()[0], 2)

    def test_work_report_keeps_one_snapshot_during_capture(self):
        self.ingest(self.call())
        load = work.rates.load

        def append_after_contexts():
            result = load()
            self.ingest(self.call("new", session="new", ts=30))
            return result

        with patch.object(
            store,
            "open_readonly",
            side_effect=lambda: sqlite3.connect("file:" + str(self.path) + "?mode=ro", uri=True),
        ), patch.object(work.rates, "load", side_effect=append_after_contexts):
            result = server.Reports().effectiveness()
        self.assertEqual(result["total_calls"], 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM calls").fetchone()[0], 2)
