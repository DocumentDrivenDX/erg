"""Forward capture, reconstructed history and safe local inference integration."""

import contextlib
import http.server
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

from erg import collect, history, history_projection, local_proxy, store


class Foundation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        for obj, key, value in [
            (collect, "STATE_DIR", str(self.state)),
            (collect, "HOME", str(self.root)),
            (store, "DB_PATH", str(self.root / "ledger.db")),
        ]:
            p = patch.object(obj, key, value)
            p.start()
            self.addCleanup(p.stop)
        self.db = store.connect(str(self.root / "ledger.db"))
        self.addCleanup(self.db.close)
        p = patch.dict(
            os.environ,
            {"ERG_DATABASE": str(self.root / "ledger.db"), "ERG_STATE_DIR": str(self.state)},
            clear=True,
        )
        p.start()
        self.addCleanup(p.stop)
        history.enroll(self.db)
        self.enrolled = int(time.time())

    def line(self, path, d):
        with path.open("a") as f:
            f.write(json.dumps(d) + "\n")

    def stamp(self, offset=1):
        return self.enrolled + offset

    def test_existing_history_baselines_then_only_new_events_are_archived(self):
        p = self.root / "s.jsonl"
        self.line(p, dict(type="session", id="s", timestamp=self.stamp(-100), cwd="/code/p"))
        self.line(
            p,
            dict(
                type="message",
                id="old",
                timestamp=self.stamp(-90),
                message=dict(role="user", content="OLD"),
            ),
        )
        os.utime(p, (self.enrolled - 60, self.enrolled - 60))
        history.scan_file(self.db, "pi", str(p), self.enrolled)
        self.assertEqual(self.db.execute("select count(*) from history_events").fetchone()[0], 0)
        self.line(
            p,
            dict(
                type="message",
                id="new",
                timestamp=self.stamp(),
                message=dict(role="user", content="NEW"),
            ),
        )
        history.scan_file(self.db, "pi", str(p), self.enrolled)
        history.scan_file(self.db, "pi", str(p), self.enrolled)
        result = history.events(self.db, "pi", "s")
        self.assertEqual(len(result["events"]), 1)
        self.assertEqual(result["events"][0]["body"]["message"]["content"], "NEW")

    def test_native_and_lifecycle_mirrors_preserve_full_content_once(self):
        with self.db:
            history.put(
                self.db,
                "pi",
                "s",
                "p",
                self.stamp(),
                "message",
                dict(message=dict(role="assistant", content="FULL", usage=dict(input=1))),
                "a",
            )
            history.put(
                self.db,
                "pi",
                "s",
                "p",
                self.stamp(),
                "message",
                dict(message=dict(role="assistant", usage=dict(input=1))),
                "a",
            )
        rows = history.events(self.db, "pi", "s")["events"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["body"]["message"]["content"], "FULL")

    def test_expiration_is_previewable_and_replay_cannot_resurrect_content(self):
        with self.db:
            history.put(
                self.db, "pi", "s", "p", self.stamp(), "message", dict(content="PRIVATE"), "a"
            )
        self.assertEqual(history.expire(self.db, self.stamp(5))["events"], 1)
        self.assertIsNotNone(history.events(self.db, "pi", "s")["events"][0]["body"])
        history_projection.run(self.db, budget=10)
        history.expire(self.db, self.stamp(5), True)
        with self.db:
            history.put(
                self.db, "pi", "s", "p", self.stamp(), "message", dict(content="PRIVATE"), "a"
            )
        self.assertIsNone(history.events(self.db, "pi", "s")["events"][0]["body"])

    def test_partial_trailing_record_does_not_advance_checkpoint(self):
        p = self.root / "s.jsonl"
        self.line(p, dict(type="session", id="s", timestamp=self.stamp(), cwd="/code/p"))
        with p.open("a") as f:
            f.write('{"type":"message","id":"a",')
        history.scan_file(self.db, "pi", str(p), self.enrolled)
        offset = self.db.execute("select offset from history_sources").fetchone()[0]
        self.assertLess(offset, p.stat().st_size)
        with p.open("a") as f:
            f.write(
                '"timestamp":'
                + str(self.stamp())
                + ',"message":{"role":"user","content":"DONE"}}\n'
            )
        history.scan_file(self.db, "pi", str(p), self.enrolled)
        self.assertEqual(len(history.events(self.db, "pi", "s")["events"]), 2)

    def test_filters_and_pagination_do_not_duplicate_sessions(self):
        with self.db:
            for i in range(4):
                history.put(self.db, "pi", "s" + str(i), "p", self.stamp(i), "message", {}, str(i))
        first = history.sessions(self.db, project="p", limit=2)
        second = history.sessions(self.db, after=first["next_cursor"], limit=2)
        self.assertFalse(
            set(r["session"] for r in first["sessions"])
            & set(r["session"] for r in second["sessions"])
        )
        self.assertEqual(len(history.sessions(self.db, session="s0")["sessions"]), 1)

    def test_synthetic_and_unknown_pi_records_never_become_metered_calls(self):
        rows = []
        for sid, m in [
            ("synthetic", dict(timestamp=0, usage=dict(input=0, output=0))),
            ("unknown", dict(usage=dict(input=0, output=0))),
            ("real", dict(usage=dict(input=2, output=3))),
        ]:
            rows.append(
                dict(
                    t="llm",
                    uid=sid,
                    tool="pi",
                    session="s",
                    ts=self.stamp(),
                    fresh_in=m["usage"]["input"],
                    out=m["usage"]["output"],
                    usage_kind=collect.pi_usage_kind(m),
                )
            )
        store.ingest(self.db, map(json.dumps, rows), machine="host")
        self.assertEqual(self.db.execute("select count(*) from llm").fetchone()[0], 3)
        self.assertEqual(
            self.db.execute("select count(*),sum(fresh_in+out) from calls").fetchone(), (1, 5)
        )

    def test_exact_server_response_match_counts_once_and_ambiguity_remains(self):
        rows = [
            dict(
                t="llm",
                uid="a",
                ts=self.stamp(),
                tool="pi",
                session="s",
                model="m",
                response_id="r",
                fresh_in=5,
                out=2,
            ),
            dict(
                t="llm",
                uid="b",
                ts=self.stamp(),
                tool="local",
                session="i",
                model="m",
                response_id="r",
                fresh_in=5,
                out=2,
            ),
        ]
        store.ingest(self.db, map(json.dumps, rows), machine="host")
        self.assertEqual(
            self.db.execute("select count(*),sum(fresh_in+out) from calls").fetchone(), (1, 7)
        )
        rows[0]["uid"] = "c"
        store.ingest(self.db, [json.dumps(rows[0])], machine="host")
        self.assertEqual(self.db.execute("select count(*) from calls").fetchone()[0], 3)

    def test_usage_parsing_requires_both_buckets_and_preserves_cache_subsets(self):
        response = json.dumps(
            dict(
                id="r",
                model="m",
                usage=dict(
                    prompt_tokens=100,
                    completion_tokens=3,
                    prompt_tokens_details=dict(cached_tokens=20, cache_write_tokens=70),
                ),
            )
        ).encode()
        u = local_proxy.usage_from(response)
        self.assertEqual(
            (u["fresh_in"], u["cache_read"], u["cache_write"], u["out"]), (10, 20, 70, 3)
        )
        self.assertFalse(local_proxy.usage_from(b'{"usage":{"input_tokens":5}}')["known"])

    def test_loopback_proxy_archives_request_response_and_usage(self):
        dbpath = str(self.root / "ledger.db")

        class Upstream(http.server.BaseHTTPRequestHandler):
            def do_POST(s):
                s.rfile.read(int(s.headers["Content-Length"]))
                body = json.dumps(
                    dict(
                        id="actual",
                        model="m",
                        choices=[dict(message=dict(content="HELLO"))],
                        usage=dict(prompt_tokens=5, completion_tokens=2),
                    )
                ).encode()
                s.send_response(200)
                s.send_header("Content-Length", str(len(body)))
                s.end_headers()
                s.wfile.write(body)

            def log_message(s, *a):
                pass

        upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        self.addCleanup(upstream.server_close)
        self.addCleanup(upstream.shutdown)
        # The default connect argument was bound at import, so patch the module's
        # factory rather than relying on an environment-variable change after import.
        original = store.connect
        with patch.object(
            store,
            "connect",
            side_effect=lambda *args, **kw: original(dbpath, initialize=kw.get("initialize", True)),
        ):
            proxy = local_proxy.make_server("http://127.0.0.1:" + str(upstream.server_port))
            threading.Thread(target=proxy.serve_forever, daemon=True).start()
            try:
                req = urllib.request.Request(
                    "http://127.0.0.1:" + str(proxy.server_port) + "/v1/chat/completions",
                    data=b'{"model":"m","messages":[{"role":"user","content":"PRIVATE"}]}',
                    headers={
                        "Content-Type": "application/json",
                        "X-Erg-Session-Id": "direct-s",
                        "Authorization": "SECRET",
                    },
                )
                with urllib.request.urlopen(req) as r:
                    self.assertEqual(json.load(r)["id"], "actual")
            finally:
                proxy.shutdown()
                proxy.server_close()
        self.assertEqual(
            self.db.execute(
                "select usage_known,state,input_tokens,output_tokens from local_inferences"
            ).fetchone(),
            (1, "complete", 5, 2),
        )
        self.assertEqual(
            self.db.execute("select distinct machine from llm where tool='local'").fetchall(),
            [(collect.machine_name(),)],
        )
        bodies = json.dumps(history.events(self.db, "local", "direct-s"))
        self.assertIn("PRIVATE", bodies)
        self.assertNotIn("SECRET", bodies)
        self.assertEqual(
            self.db.execute("select count(*) from calls where tool='local'").fetchone()[0], 1
        )

    def test_stream_capture_initializes_once_and_releases_writer_during_inference(self):
        dbpath = str(self.root / "ledger.db")
        checks = []
        final = dict(
            id="stream-response", model="m", usage=dict(prompt_tokens=6, completion_tokens=3)
        )
        chunks = [
            b'data: {"id":"stream-response","choices":[]}\n\n',
            b"data: " + json.dumps(final).encode() + b"\n\ndata: [DONE]\n\n",
        ]

        class Upstream(http.server.BaseHTTPRequestHandler):
            def do_POST(s):
                s.rfile.read(int(s.headers["Content-Length"]))
                # The committed request must be visible and no writer may span inference.
                with contextlib.closing(sqlite3.connect(dbpath, timeout=0)) as other:
                    other.execute("BEGIN IMMEDIATE")
                    checks.append(
                        other.execute(
                            "SELECT count(*) FROM local_inferences WHERE state='pending'"
                        ).fetchone()[0]
                    )
                    other.rollback()
                s.send_response(200)
                s.send_header("Content-Type", "text/event-stream")
                s.send_header("Connection", "close")
                s.end_headers()
                s.wfile.write(chunks[0])
                s.wfile.flush()
                time.sleep(1.05)
                s.wfile.write(chunks[1])
                s.wfile.flush()
                s.close_connection = True

            def log_message(s, *args):
                pass

        upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        self.addCleanup(upstream.server_close)
        self.addCleanup(upstream.shutdown)
        original = store.connect
        opened = []

        def connect(*args, **kw):
            opened.append(kw.get("initialize", True))
            return original(dbpath, initialize=kw.get("initialize", True))

        with patch.object(store, "connect", side_effect=connect):
            proxy = local_proxy.make_server("http://127.0.0.1:" + str(upstream.server_port))
            threading.Thread(target=proxy.serve_forever, daemon=True).start()
            try:
                req = urllib.request.Request(
                    "http://127.0.0.1:" + str(proxy.server_port) + "/v1/chat/completions",
                    data=b'{"model":"m","stream":true}',
                    headers={"Content-Type": "application/json", "X-Erg-Session-Id": "stream-s"},
                )
                with urllib.request.urlopen(req, timeout=10) as response:
                    self.assertEqual(response.read(), b"".join(chunks))
            finally:
                proxy.shutdown()
                proxy.server_close()
        self.assertEqual(checks, [1])
        self.assertEqual(opened, [True, False])
        events = history.events(self.db, "local", "stream-s")["events"]
        self.assertTrue(any(e["kind"] == "inference_stream_checkpoint" for e in events))
        self.assertEqual(
            self.db.execute(
                "SELECT state,usage_known,input_tokens,output_tokens FROM local_inferences"
            ).fetchone(),
            ("complete", 1, 6, 3),
        )
        history_projection.run(self.db, budget=10)
        self.assertEqual(
            self.db.execute(
                "SELECT count(*),sum(fresh_in+out) FROM calls WHERE tool='local'"
            ).fetchone(),
            (1, 9),
        )

    def test_local_proxy_rejects_nonlocal_upstreams_and_public_binding(self):
        for url in ("https://example.com", "http://user:secret@127.0.0.1", "file:///etc/passwd"):
            with self.assertRaises(ValueError):
                local_proxy.make_server(url)
        with self.assertRaises(ValueError):
            local_proxy.make_server("http://127.0.0.1", host="0.0.0.0")

    def test_local_proxy_rejects_self_loop(self):
        with self.assertRaises(ValueError):
            local_proxy.make_server("http://127.0.0.1:18000", 18000)

    def test_nested_server_receipts_are_metered_once(self):
        rows = [
            dict(
                t="llm",
                uid=uid,
                ts=self.stamp(),
                tool="local",
                session=uid,
                model="m",
                response_id="r",
                fresh_in=5,
                out=2,
            )
            for uid in ("inner", "outer")
        ]
        store.ingest(self.db, map(json.dumps, rows), machine="host")
        self.assertEqual(
            self.db.execute("select count(*),sum(fresh_in+out) from calls").fetchone(), (1, 7)
        )

    def test_link_is_retracted_when_native_identity_becomes_ambiguous(self):
        with self.db:
            local_proxy.record_request(self.db, "inv", {"model": "m"}, "ds4")
            self.db.execute("update local_inferences set response_id='r' where invocation_id='inv'")
        rows = [
            dict(
                t="llm",
                uid="a",
                ts=self.stamp(),
                tool="pi",
                session="a",
                model="m",
                response_id="r",
                fresh_in=5,
                out=2,
            )
        ]
        store.ingest(self.db, map(json.dumps, rows), machine=collect.machine_name())
        history.link_inferences(self.db)
        self.assertEqual(self.db.execute("select count(*) from history_links").fetchone()[0], 1)
        rows[0].update(uid="b", session="b")
        store.ingest(self.db, map(json.dumps, rows), machine=collect.machine_name())
        history.link_inferences(self.db)
        self.assertEqual(self.db.execute("select count(*) from history_links").fetchone()[0], 0)

    def test_owned_content_outbox_is_removed_only_after_successful_ingest(self):
        outbox = self.state / "pi-content-outbox"
        outbox.mkdir()
        p = outbox / "batch.jsonl"
        self.line(p, dict(type="session", id="s", timestamp=self.stamp(), cwd="p"))
        self.line(
            p,
            dict(
                type="message",
                id="a",
                timestamp=self.stamp(),
                message=dict(role="user", content="DURABLE"),
            ),
        )
        history.consume_outbox(self.db)
        self.assertTrue(p.exists())
        history.scan_file(self.db, "pi", str(p), self.enrolled)
        history.consume_outbox(self.db)
        self.assertFalse(p.exists())
        self.assertIn("DURABLE", json.dumps(history.events(self.db, "pi", "s")))

    def test_reconciliation_exposes_metadata_only_and_partial_checkpoint(self):
        p = self.root / "partial.jsonl"
        self.line(p, dict(type="session", id="s", timestamp=self.stamp(), cwd="p"))
        history.scan_file(self.db, "pi", str(p), self.enrolled)
        r = history.reconciliation(self.db)
        self.assertEqual(r["counts"], {"metadata_only": 1})
        with p.open("a") as f:
            f.write('{"type":')
        history.scan_file(self.db, "pi", str(p), self.enrolled)
        r = history.reconciliation(self.db)
        self.assertEqual(r["counts"], {"source_needs_attention": 1})
        self.assertEqual(r["source_problem_count"], 1)
        self.assertEqual(history.overview(self.db)["sources_needing_attention"], 1)

    def test_linked_receipt_is_not_a_second_unmatched_session(self):
        with self.db:
            history.put(
                self.db,
                "pi",
                "s",
                "p",
                self.stamp(),
                "message",
                {"content": "native"},
                "native",
                role="assistant",
            )
            history.put(
                self.db,
                "local",
                "inference:i",
                "p",
                self.stamp(),
                "inference_response",
                {"content": "server"},
                "server",
            )
            self.db.execute(
                "insert into history_links values('inference:i','pi','s','r',?)", (self.stamp(),)
            )
        r = history.reconciliation(self.db)
        self.assertEqual(r["total"], 1)
        self.assertEqual(r["sessions"][0]["archived_events"], 2)

    def test_reconciliation_expired_content_is_explicit(self):
        with self.db:
            history.put(
                self.db,
                "pi",
                "s",
                "p",
                self.stamp(),
                "message",
                {"content": "expired"},
                "a",
                role="user",
            )
        history_projection.run(self.db, budget=10)
        history.expire(self.db, self.stamp(5), True)
        r = history.reconciliation(self.db)
        self.assertEqual(r["counts"], {"archived_with_expired_content": 1})
        self.assertEqual(r["sessions"][0]["expired_events"], 1)

    def test_reconciliation_holds_one_snapshot_across_concurrent_usage_insert(self):
        from erg import call_projection

        with self.db:
            history.put(
                self.db,
                "pi",
                "s",
                "p",
                self.stamp(),
                "message",
                {"content": "retained"},
                "native",
                role="assistant",
            )
            self.db.execute(
                "INSERT INTO llm(machine,uid,tool,session,ts,model,out,usage_kind) VALUES('host','first','pi','s',?,'m',1,'observed')",
                (self.stamp(),),
            )
        for _ in range(20):
            if call_projection.maintain(self.db, budget=1)["ready"]:
                break
        reader = store.open_readonly(self.root / "ledger.db")
        self.addCleanup(reader.close)
        injected = []
        failures = []

        def change(sql):
            if "SELECT DISTINCT source,tool,session FROM history_events" in sql and not injected:
                injected.append(True)
                try:
                    with self.db:
                        self.db.execute(
                            "INSERT INTO llm(machine,uid,tool,session,ts,model,out,usage_kind) VALUES('host','second','pi','s',?,'m',2,'observed')",
                            (self.stamp(),),
                        )
                except Exception as error:
                    failures.append(error)

        reader.set_trace_callback(change)
        try:
            result = history.reconciliation(reader)
        finally:
            reader.set_trace_callback(None)
        self.assertEqual(failures, [])
        self.assertEqual(injected, [True])
        self.assertFalse(reader.in_transaction)
        self.assertEqual(result["sessions"][0]["metered_calls"], 1)
        self.assertEqual(reader.execute("SELECT count(*) FROM calls").fetchone()[0], 2)
        reader.execute("BEGIN")
        history.reconciliation(reader)
        self.assertTrue(reader.in_transaction)
        reader.rollback()

    def test_reconciliation_failure_releases_only_its_own_read_snapshot(self):
        with patch.object(history, "_reconciliation", side_effect=RuntimeError("failed coverage")):
            with self.assertRaises(RuntimeError):
                history.reconciliation(self.db)
            self.assertFalse(self.db.in_transaction)
            self.db.execute("BEGIN")
            with self.assertRaises(RuntimeError):
                history.reconciliation(self.db)
            self.assertTrue(self.db.in_transaction)
            self.db.rollback()

    def test_copilot_forward_trace_preserves_messages_tools_and_unknown_usage(self):
        root = self.root / ".copilot/session-state/copilot-s"
        root.mkdir(parents=True)
        p = root / "events.jsonl"
        self.line(
            p,
            dict(
                type="session.start",
                id="start",
                timestamp=self.stamp(),
                data={"sessionId": "copilot-s", "context": {"cwd": "/code/p"}},
            ),
        )
        self.line(
            p,
            dict(
                type="user.message",
                id="u",
                timestamp=self.stamp(),
                data={"content": "REQUEST", "authorization": "SECRET"},
            ),
        )
        self.line(
            p,
            dict(
                type="tool.execution_start",
                id="t",
                timestamp=self.stamp(),
                data={"toolCallId": "call", "toolName": "shell", "arguments": {"command": "pwd"}},
            ),
        )
        self.line(
            p,
            dict(
                type="tool.execution_complete",
                id="r",
                timestamp=self.stamp(),
                data={"toolCallId": "call", "result": "/code/p"},
            ),
        )
        self.line(
            p,
            dict(
                type="assistant.message",
                id="a",
                timestamp=self.stamp(),
                data={"content": "RESPONSE"},
            ),
        )
        history.scan(self.db)
        events = history.events(self.db, "copilot", "copilot-s")["events"]
        self.assertEqual(len(events), 5)
        self.assertEqual([r["role"] for r in events if r["role"]], ["user", "assistant"])
        self.assertIn("REQUEST", json.dumps(events))
        self.assertIn("pwd", json.dumps(events))
        self.assertNotIn("SECRET", json.dumps(events))
        self.assertEqual(history.reconciliation(self.db)["sessions"][0]["metered_calls"], 0)
        self.assertEqual(self.db.execute("select count(*) from llm").fetchone()[0], 0)
