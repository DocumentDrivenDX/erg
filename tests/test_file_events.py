"""Kernel notifications are hints with bounded, fair, independently acknowledged queues."""

import concurrent.futures
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from erg import collect, file_events, history, store


class FileEvents(unittest.TestCase):
    def test_slow_discovery_does_not_block_status_or_queue_concurrent_refreshes(self):
        import threading

        entered = threading.Event()
        release = threading.Event()
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            collect, "HOME", temporary
        ), patch.object(collect, "STATE_DIR", temporary + "/state"), patch.object(
            file_events, "MacWatch"
        ):
            feed = file_events.Feed()
            self.addCleanup(feed.close)
            feed.drain("history")
            original = feed.sources.refresh

            def slow():
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("test discovery timeout")
                return original()

            with patch.object(
                feed.sources, "refresh", side_effect=slow
            ) as refresh, concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                blocked = pool.submit(feed.refresh)
                try:
                    self.assertTrue(entered.wait(2))
                    status = pool.submit(feed.status)
                    self.assertIn("mode", status.result(timeout=2))
                    duplicate = pool.submit(feed.refresh)
                    duplicate.result(timeout=2)
                    self.assertEqual(refresh.call_count, 1)
                    feed.put(temporary + "/.pi/agent/sessions/live.jsonl")
                    self.assertTrue(feed.drain("history")[0])
                finally:
                    release.set()
                    blocked.result(timeout=5)
                self.assertTrue(
                    feed.drain("history")[1], "coalesced changes need another discovery pass"
                )

    def test_unrelated_event_storm_cannot_overflow_capture_queues(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(collect, "HOME", temporary), patch.object(
                collect, "STATE_DIR", temporary + "/state"
            ), patch.object(file_events, "MacWatch"):
                feed = file_events.Feed()
                self.addCleanup(feed.close)
                for name in ("history", "context", "fast"):
                    feed.drain(name)
                for i in range(20000):
                    feed.put(temporary + "/.codex/plugins/file-" + str(i) + ".js")
                status = feed.status()
                self.assertEqual(status["overflow_count"], 0)
                self.assertEqual(status["ignored_unrelated_events"], 20000)
                self.assertFalse(any(status["queued"].values()))
                transcript = temporary + "/.pi/agent/sessions/new.jsonl"
                feed.put(transcript)
                self.assertIn(transcript, feed.drain("history")[0])
                feed.put(temporary + "/.codex/plugins/unrelated.js", file_events.RESCAN_FLAGS)
                self.assertTrue(feed.drain("history")[1])
                feed.put(feed.discovery_path)
                self.assertTrue(feed.drain("history")[1])
                bridge = temporary + "/.omnigent/codex-native/new/state.json"
                feed.put(bridge)
                self.assertIn(bridge, feed.drain("history")[0])

    def test_symlinked_discovery_watches_target_and_link_directories(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            config = root / "config"
            master = root / "master"
            config.mkdir()
            master.mkdir()
            target = master / "discovery.json"
            target.write_text("{}")
            link = config / "discovery.json"
            link.symlink_to(target)
            with patch.object(collect, "HOME", str(root)), patch.object(
                collect, "STATE_DIR", str(root / "state")
            ), patch.dict(os.environ, {"ERG_DISCOVERY_FILE": str(link)}), patch.object(
                file_events, "MacWatch"
            ):
                feed = file_events.Feed()
                self.addCleanup(feed.close)
                self.assertIn(str(master), feed.roots)
                self.assertIn(str(config), feed.roots)
                feed.drain("history")
                feed.put(str(target))
                self.assertTrue(feed.drain("history")[1])
                other = root / "other"
                other.mkdir()
                replacement = other / "discovery.json"
                replacement.write_text("{}")
                link.unlink()
                link.symlink_to(replacement)
                feed.put(str(link))
                self.assertTrue(feed.drain("history")[1])
                feed.refresh()
                self.assertEqual(feed.discovery_path, str(replacement))
                self.assertIn(str(other), feed.roots)

    def test_consumers_coalesce_independently_and_retry_fairly(self):
        box = file_events.Mailbox(limit=3)
        box.subscribe("a")
        box.subscribe("b")
        box.put("z")
        box.put("a")
        box.put("z")
        self.assertEqual(box.drain("a", 1), (["z"], False))
        box.retry("a", ["z"])
        self.assertEqual(box.drain("a", 1), (["a"], False))
        self.assertEqual(box.drain("b"), (["z", "a"], False))

    def test_overflow_and_kernel_loss_require_reconciliation(self):
        box = file_events.Mailbox(limit=1)
        box.subscribe("a")
        box.put("first")
        box.put("second")
        paths, rescan = box.drain("a")
        self.assertEqual(paths, ["first"])
        self.assertTrue(rescan)
        self.assertEqual(box.status()["overflow_count"], 1)
        box.put("/", file_events.RESCAN_FLAGS)
        self.assertTrue(box.drain("a")[1])

    @unittest.skipUnless(sys.platform == "darwin", "FSEvents requires macOS")
    def test_real_file_events_drive_capture_without_inventory_walk(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            state = root / "state"
            state.mkdir()
            logs = root / "logs"
            logs.mkdir()
            with patch.object(collect, "HOME", str(root)), patch.object(
                collect, "STATE_DIR", str(state)
            ):
                db = store.connect(str(root / "db"))
                self.addCleanup(db.close)
                history.enroll(db)
                box = file_events.Mailbox()
                box.subscribe("history")
                watch = file_events.MacWatch([str(logs)], box)
                try:
                    path = logs / "session.jsonl"
                    now = int(time.time()) + 1
                    path.write_text(
                        json.dumps(dict(type="session", id="s", timestamp=now, cwd="/owned"))
                        + "\n"
                        + json.dumps(
                            dict(
                                type="message",
                                id="a",
                                timestamp=now,
                                message=dict(
                                    role="assistant", model="m", usage=dict(input=2, output=3)
                                ),
                            )
                        )
                        + "\n"
                    )
                    deadline = time.monotonic() + 5
                    seen = []
                    while time.monotonic() < deadline:
                        paths, rescan = box.drain("history")
                        seen.extend(paths)
                        if str(path) in seen:
                            break
                        time.sleep(0.05)
                    self.assertIn(str(path), seen)
                    with patch.object(
                        collect, "log_paths", side_effect=AssertionError("whole inventory walked")
                    ):
                        result = history.scan(db, paths=[("pi", str(path))], scan_sqlite=False)
                    self.assertEqual(result["events"], 2)
                    self.assertFalse(result["reconciled"])
                    self.assertEqual(
                        db.execute("select fresh_in,out from calls").fetchone(), (2, 3)
                    )
                finally:
                    watch.close()

    def test_targeted_budget_preserves_deferred_paths_and_global_error_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            state.mkdir()
            path = root / "s.jsonl"
            path.write_text("{}\n")
            with patch.object(collect, "STATE_DIR", str(state)):
                with store.connect(str(root / "db")) as db:
                    history.enroll(db)
                    with db:
                        db.execute(
                            "INSERT INTO history_sources(path,tool,state,errors) VALUES('unvisited','pi','parse_error',2)"
                        )
                    result = history.scan(
                        db, budget=0, paths=[("pi", str(path))], scan_sqlite=False
                    )
                    self.assertEqual(result["deferred_paths"], [("pi", str(path))])
                    self.assertEqual(result["source_error_count"], 2)

    def test_concurrent_schema_initialization_is_serialized(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = str(Path(temporary) / "db")

            def connect(_):
                db = store.connect(path)
                try:
                    return db.execute("pragma table_info(prompts)").fetchall()
                finally:
                    db.close()

            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
                results = list(pool.map(connect, range(6)))
            self.assertTrue(
                all(sum(row[1] == "source_uid" for row in result) == 1 for result in results)
            )

    def test_receipt_matching_uses_the_exact_response_index(self):
        with tempfile.TemporaryDirectory() as temporary:
            with store.connect(str(Path(temporary) / "db")) as db:
                plan = db.execute(
                    "EXPLAIN QUERY PLAN SELECT DISTINCT tool,session FROM llm WHERE machine=? AND tool NOT IN ('local','gateway') AND response_id=? AND model=? AND response_id<>''",
                    ("host", "response", "model"),
                ).fetchall()
                self.assertIn("llm_response_identity", str(plan))
                self.assertNotIn("SCAN llm", str(plan))

    def test_open_writer_append_captures_without_a_native_notification(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            state.mkdir()
            path = root / "session.jsonl"
            with patch.object(collect, "HOME", str(root)), patch.object(
                collect, "STATE_DIR", str(state)
            ):
                with store.connect(str(root / "db")) as db, path.open("w") as writer:
                    history.enroll(db)
                    stamp = int(time.time()) + 1

                    def append(uid):
                        writer.write(
                            json.dumps(
                                dict(
                                    type="message",
                                    id=uid,
                                    timestamp=stamp,
                                    message=dict(
                                        role="assistant", model="m", usage=dict(input=2, output=3)
                                    ),
                                )
                            )
                            + "\n"
                        )
                        writer.flush()

                    writer.write(
                        json.dumps(dict(type="session", id="s", timestamp=stamp, cwd="/fixture"))
                        + "\n"
                    )
                    append("first")
                    history.scan_file(db, "pi", str(path), stamp - 1)

                    class CurrentSources:
                        def classify(self, p):
                            return ("pi", p) if p == str(path) else None

                    append("second")
                    with patch.object(
                        collect, "log_paths", side_effect=AssertionError("whole inventory walked")
                    ):
                        active = file_events.active_sources(db, CurrentSources())
                        self.assertEqual(active, [("pi", str(path))])
                        result = history.scan(
                            db, paths=active, scan_sqlite=False, project_work=False
                        )
                    self.assertFalse(result["reconciled"])
                    self.assertEqual(result["events"], 1)
                    self.assertEqual(
                        db.execute("SELECT count(*),sum(fresh_in),sum(out) FROM calls").fetchone(),
                        (2, 4, 6),
                    )

    def test_active_source_fallback_is_bounded_and_respects_current_discovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with store.connect(str(root / "db")) as db:
                paths = []
                for i in range(5):
                    path = root / (str(i) + ".jsonl")
                    path.write_text("not read")
                    paths.append(str(path))
                    db.execute(
                        "INSERT INTO history_sources(path,tool,mtime) VALUES(?,'pi',?)",
                        (str(path), i),
                    )
                db.commit()

                class CurrentSources:
                    def classify(self, p):
                        return ("pi", p) if p != paths[-1] else None

                with patch.object(
                    collect, "log_paths", side_effect=AssertionError("whole inventory walked")
                ):
                    self.assertEqual(
                        file_events.active_sources(db, CurrentSources(), limit=2),
                        [("pi", paths[-2])],
                    )
                    self.assertEqual(file_events.active_sources(db, CurrentSources(), limit=0), [])
