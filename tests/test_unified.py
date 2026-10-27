import contextlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from erg import collect, databricks, store, unified


class Capture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)

    def capture(self, fn):
        out = io.StringIO()
        with patch.object(collect, "HOME", str(self.home)), contextlib.redirect_stdout(out):
            fn(0)
        return [json.loads(x) for x in out.getvalue().splitlines()]

    def write(self, rel, events, compact=False):
        p = self.home / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            "".join(
                json.dumps(e, separators=(",", ":") if compact else None) + "\n" for e in events
            )
        )
        return p

    def codex(self):
        stamp = "2026-10-17T00:00:00Z"
        return (
            [
                dict(
                    type="session_meta",
                    timestamp=stamp,
                    payload=dict(
                        id="s",
                        cwd="/code/p",
                        originator="Codex Desktop",
                        parent_thread_id="parent",
                        thread_source="guardian",
                    ),
                ),
                dict(type="turn_context", timestamp=stamp, payload=dict(model="m", turn_id="t")),
            ]
            + [
                dict(
                    type="response_item",
                    timestamp=stamp,
                    payload=dict(type="message", role="user", content=[dict(text=x)]),
                )
                for x in ["one secret prompt", "two secret prompts"]
            ]
            + [
                dict(
                    type="token_usage_record",
                    timestamp=stamp,
                    payload=dict(
                        response_id="r",
                        turn_id="t",
                        usage=dict(input_tokens=100, cached_input_tokens=20, output_tokens=n),
                    ),
                )
                for n in [5, 50, 7]
            ]
        )

    def test_codex_streamed_records_and_parentage(self):
        self.write(".codex/sessions/a.jsonl", self.codex())
        rows = self.capture(collect.collect_codex)
        call = [r for r in rows if r["t"] == "llm"][0]
        self.assertEqual((call["out"], call["fresh_in"], call["cache_read"]), (50, 80, 20))
        meta = [r for r in rows if r["t"] == "session"][0]
        self.assertEqual(
            (meta["parent_session"], meta["kind"], meta["surface"]),
            ("parent", "review", "Codex Desktop"),
        )
        self.assertNotIn("secret", json.dumps(rows))

    def test_codex_prompts_without_ordinal_do_not_collide(self):
        events = self.codex()
        events[0]["payload"].pop("parent_thread_id")
        events[0]["payload"]["thread_source"] = "user"
        self.write(".codex/sessions/a.jsonl", events)
        prompts = [r for r in self.capture(collect.collect_codex) if r["t"] == "prompt"]
        self.assertEqual(len({r["uid"] for r in prompts}), 2)

    @unittest.skipUnless(shutil.which("zstd"), "zstd archive fixture needs installed decoder")
    def test_codex_archive_replay_deduplicates(self):
        p = self.write(".codex/archived_sessions/a.jsonl", self.codex())
        r = subprocess.run(["zstd", "-q", "-c", str(p)], capture_output=True, check=True)
        p.with_suffix(".jsonl.zst").write_bytes(r.stdout)
        rows = self.capture(collect.collect_codex)
        with store.connect(str(self.home / "test.db")) as db:
            store.ingest(db, map(json.dumps, rows), machine="host")
            self.assertEqual(db.execute("SELECT COUNT(*),MAX(out) FROM llm").fetchone(), (1, 50))

    def test_claude_spaced_json_and_subagent_sessions(self):
        self.write(
            ".claude/projects/p/subagents/agent-child.jsonl",
            [
                dict(
                    type="assistant",
                    timestamp="2026-10-17T00:00:00Z",
                    uuid="u",
                    sessionId="parent",
                    agentId="child",
                    cwd="/code/p",
                    isSidechain=True,
                    entrypoint="claude-desktop",
                    message=dict(
                        id="msg",
                        model="claude-x",
                        usage=dict(input_tokens=5, output_tokens=6),
                        content=[],
                    ),
                )
            ],
        )
        rows = self.capture(collect.collect_claude)
        meta = [r for r in rows if r["t"] == "session"][0]
        self.assertEqual((meta["session"], meta["parent_session"]), ("child", "parent"))
        self.assertEqual(len([r for r in rows if r["t"] == "llm"]), 1)

    def test_copilot_capture_keeps_unknown_usage_unknown(self):
        self.write(
            ".copilot/session-state/s/events.jsonl",
            [
                dict(
                    type="session.start",
                    timestamp="2026-10-17T00:00:00Z",
                    data=dict(sessionId="s", context=dict(cwd="/code/p")),
                ),
                dict(
                    type="user.message",
                    timestamp="2026-10-17T00:01:00Z",
                    id="u",
                    data=dict(content="private prompt"),
                ),
            ],
        )
        rows = self.capture(collect.collect_copilot)
        self.assertEqual([r for r in rows if r["t"] == "session"][0]["usage_status"], "unknown")
        self.assertFalse(any(r["t"] == "llm" for r in rows))
        self.assertNotIn("private prompt", json.dumps(rows))

    def test_opencode_read_only_sqlite_projection(self):
        root = self.home / ".local/share/opencode"
        root.mkdir(parents=True)
        db = sqlite3.connect(str(root / "opencode.db"))
        db.executescript(
            "CREATE TABLE session(id TEXT,directory TEXT,parent_id TEXT,time_created INT,time_updated INT); CREATE TABLE message(id TEXT,session_id TEXT,data TEXT);"
        )
        db.execute(
            "INSERT INTO session VALUES(?,?,?,?,?)",
            ("s", "/code/p", None, 1792195200000, 1792195260000),
        )
        db.execute(
            "INSERT INTO message VALUES(?,?,?)",
            (
                "r",
                "s",
                json.dumps(
                    dict(
                        role="assistant",
                        modelID="m",
                        time=dict(created=1792195200000),
                        tokens=dict(input=10, output=3, cache=dict(read=5, write=2)),
                    )
                ),
            ),
        )
        db.commit()
        db.close()
        rows = self.capture(collect.collect_opencode)
        call = [r for r in rows if r["t"] == "llm"][0]
        self.assertEqual((call["fresh_in"], call["cache_read"], call["cache_write"]), (10, 5, 2))


class Journal(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = store.connect(os.path.join(self.tmp.name, "t.db"))
        self.addCleanup(self.db.close)

    def call(self, uid="a", **kw):
        return dict(
            dict(
                t="llm",
                uid=uid,
                ts=3600,
                tool="claude",
                session="s",
                project="p",
                model="m",
                kind="main",
                client="",
                fresh_in=10,
                cache_read=2,
                cache_write=3,
                out=5,
                reasoning=0,
            ),
            **kw,
        )

    def ingest(self, rows, machine="host"):
        return store.ingest(self.db, map(json.dumps, rows), machine=machine)

    def test_privacy_replay_revision_and_destination_cursors(self):
        event = self.call()
        event["prompt"] = "secret"
        event["credentials"] = "secret"
        self.ingest([event, event])
        rows = unified.pending(self.db, "a")
        self.assertEqual(len(rows), 1)
        self.assertNotIn("secret", rows[0][4])
        unified.acknowledge(self.db, "a", rows[-1][-1])
        self.assertEqual(unified.pending(self.db, "a"), [])
        self.assertEqual(len(unified.pending(self.db, "b")), 1)
        self.ingest([self.call(out=15)])
        new = unified.pending(self.db, "a")
        self.assertGreater(new[0][-1], rows[0][-1])
        self.assertEqual(new[0][0], rows[0][0])
        self.ingest([self.call(out=7)])
        self.assertEqual(json.loads(unified.pending(self.db, "a")[0][4])["out"], 15)

    def test_failed_ingestion_rolls_back_projection_and_journal(self):
        with self.assertRaises(ValueError):
            self.ingest([self.call(), self.call(uid="bad", out=float("nan"))])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM llm").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM observations").fetchone()[0], 0)

    def test_sync_failure_does_not_advance_cursor(self):
        self.ingest([self.call()])
        client = databricks.Client("test", "warehouse")
        client.api = lambda *args, **kw: {"table_id": "table1"}
        with patch.object(client, "sql", side_effect=RuntimeError("offline")):
            with self.assertRaises(RuntimeError):
                databricks.sync(self.db, client, "a.b")
        self.assertEqual(len(unified.pending(self.db, "databricks-table:table1")), 1)
        with patch.object(client, "sql", return_value=[]) as sql:
            self.assertEqual(databricks.sync(self.db, client, "a.b"), 1)
            self.assertIn("MERGE INTO", sql.call_args[0][0])
            self.assertIn(":batch", sql.call_args[0][0])
        self.assertEqual(unified.pending(self.db, "databricks-table:table1"), [])

    def test_recreated_central_table_gets_a_fresh_cursor(self):
        self.ingest([self.call()])
        client = databricks.Client("test", "warehouse")
        client.api = lambda *args, **kw: {"table_id": "table1"}
        with patch.object(client, "sql", return_value=[]):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(databricks.sync(self.db, client, "a.b"), 1)
                self.assertEqual(databricks.sync(self.db, client, "a.b"), 0)
                client.api = lambda *args, **kw: {"table_id": "table2"}
                self.assertEqual(databricks.sync(self.db, client, "a.b"), 1)
                self.assertEqual(databricks.sync(self.db, client, "a.b", replay=True), 1)

    def test_session_replay_cannot_retract_bounds_or_known_usage(self):
        latest = dict(
            t="session",
            uid="s",
            ts=3600,
            last_ts=7200,
            tool="codex",
            session="s",
            project="p",
            kind="main",
            usage_status="observed",
        )
        older = dict(latest, last_ts=4000, usage_status="unknown")
        self.ingest([latest])
        before = self.db.execute("SELECT MAX(seq) FROM observations").fetchone()[0]
        self.ingest([older])
        self.assertEqual(
            self.db.execute("SELECT last_ts,usage_status FROM sessions").fetchone(),
            (7200, "observed"),
        )
        self.assertEqual(self.db.execute("SELECT MAX(seq) FROM observations").fetchone()[0], before)

    def test_explicit_identity_reconciles_late_gateway_evidence(self):
        gateway = dict(self.call(uid="g", tool="gateway", request_id="req"), requester="u")
        store.ingest_gateway(self.db, [gateway], "databricks")
        self.ingest([self.call(request_id="req")])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM calls").fetchone()[0], 1)
        self.ingest([self.call(uid="different", request_id="other")])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM calls").fetchone()[0], 2)

    def test_cross_machine_call_and_commit_sightings_count_once(self):
        self.ingest([self.call()], machine="a")
        self.ingest([self.call()], machine="b")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM llm").fetchone()[0], 2)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM calls").fetchone()[0], 1)

    def test_sparse_active_sample_and_source_selection(self):
        rows = [dict(t="metric", src="sampler", m="active", ts=3600, iv=30, v=1)]
        rows += [
            dict(t="metric", src="istat", m="cpu", ts=3600 + i * 60, iv=60, v=10) for i in range(60)
        ]
        rows += [dict(t="metric", src="endpointcsv", m="cpu", ts=3600, iv=300, v=90)]
        self.ingest(rows)
        values = {r[2]: r for r in store.rollup_metrics(self.db)}
        self.assertEqual(values["active"][7], 30)
        self.assertEqual(values["cpu"][5], "istat")

    def test_span_projection_is_content_free_and_cache_inclusive(self):
        self.ingest([self.call()])
        span = unified.span(unified.pending(self.db, "x")[0])
        self.assertEqual(span["attributes"]["gen_ai.usage.input_tokens"], 15)
        self.assertEqual(len(span["trace_id"]), 32)
        self.assertIsNone(span["end_time_unix_nano"])

    def test_report_backend_binds_and_aggregates_remotely(self):
        client = databricks.Client("test", "warehouse")
        with patch.object(client, "sql", return_value=[]) as sql:
            list(
                databricks.ReportDB(client, "ai.telemetry").execute(
                    "SELECT ts/3600,COUNT(*) FROM calls WHERE ts>=? GROUP BY 1", (3600,)
                )
            )
            text, params = sql.call_args[0]
            self.assertIn("`ai`.`telemetry`.erg_calls", text)
            self.assertIn("floor(ts/3600)", text)
            self.assertEqual(params[0]["value"], "3600")
            self.assertEqual(params[0]["type"], "INT")
        with self.assertRaises(ValueError):
            databricks.namespace("ai.telemetry;DROP")


class Reporter(unittest.TestCase):
    def test_report_connection_cannot_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "db.sqlite")
            db = store.connect(path)
            db.close()
            ro = store.open_readonly(path)
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    ro.execute("INSERT INTO machines(machine) VALUES('x')")
            finally:
                ro.close()

    def test_partial_and_empty_reports_keep_unknowns(self):
        from erg.server import Reports

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "db.sqlite")
            db = store.connect(path)
            db.close()
            original = store.open_readonly
            with patch.object(store, "open_readonly", side_effect=lambda: original(path)):
                data = Reports().data(0)
            self.assertEqual(data["session_count"], 0)
            self.assertEqual(data["warnings"], [])
            self.assertEqual(data["reporting"]["cost_provenance"], "unknown")

    def test_collector_does_not_overlap_an_existing_enrollment(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(collect, "STATE_DIR", tmp):
            with collect.collector_lock():
                with self.assertRaises(RuntimeError):
                    with collect.collector_lock():
                        self.fail("overlapping collector acquired the lock")
