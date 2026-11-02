import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from central.erg_central import discovery, processor
from central.erg_central.receiver import Conflict, namespace


class PagedDiscovery(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "spool"
        self.control = Path(self.temporary.name) / "control"
        self.index = dict(version=1, scopes={})
        self.calls = []

    def marker(self, ident, seq):
        receipt = dict(
            enrollment_id="test",
            destination_generation="gen",
            batch_id=ident,
            batch_sha256="a" * 64,
            accepted_delivery_seqs=[seq],
        )
        path = self.root / namespace(receipt) / "batches" / ident / "accepted.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(receipt))
        return receipt

    def page(self, directory, token):
        self.calls.append((str(directory), token))
        entries = sorted(directory.iterdir())
        offset = int(token or 0)
        return dict(
            contents=[
                dict(name=p.name, is_directory=p.is_dir()) for p in entries[offset : offset + 2]
            ],
            next_page_token=str(offset + 2) if offset + 2 < len(entries) else None,
        )

    def scan(self, limit=2, seconds=30, page=None):
        return discovery.paged_markers(
            self.root,
            self.control,
            limit,
            time.monotonic(),
            seconds,
            self.index,
            processor.atomic_control,
            page or self.page,
        )

    def finish(self, rows):
        processor.index_staged(self.index, self.control, [r for _, r in rows])

    def test_resumes_inside_wide_page_and_retries_unacknowledged_work(self):
        for seq in range(7):
            self.marker(str(seq), seq + 1)
        with patch.object(processor.os, "walk", side_effect=AssertionError("FUSE enumeration")):
            first, stats = self.scan(limit=1)
            self.assertEqual(first[0][1]["batch_id"], "0")
            self.assertFalse(stats["sweep_complete"])
            retry, _ = self.scan(limit=1)
            self.assertEqual(retry, first)
            self.finish(retry)
            seen = ["0"]
            for _ in range(6):
                rows, _ = self.scan(limit=1)
                seen.extend(r["batch_id"] for _, r in rows)
                self.finish(rows)
            self.assertEqual(seen, list("0123456"))
            empty, stats = self.scan()
            self.assertEqual(empty, [])
            self.assertTrue(stats["sweep_complete"])
            self.assertTrue(any(token for _, token in self.calls))

    def test_budget_before_first_marker_persists_directory_page_progress(self):
        self.marker("last", 1)
        clock = [0]

        def slow_page(directory, token):
            result = self.page(directory, token)
            clock[0] += 5
            return result

        with patch.object(discovery.time, "monotonic", side_effect=lambda: clock[0]):
            rows, stats = discovery.paged_markers(
                self.root, self.control, 2, 0, 4, self.index, processor.atomic_control, slow_page
            )
        self.assertEqual(rows, [])
        self.assertTrue(stats["budget_exhausted"])
        state = json.loads((self.control / "processor-discovery-pages.json").read_text())
        self.assertGreater(len(state["stack"]), 1)
        rows, _ = self.scan()
        self.assertEqual(rows[0][1]["batch_id"], "last")

    def test_completed_batch_is_pruned_without_opening_marker_or_body(self):
        receipt = self.marker("done", 1)
        self.finish([(None, receipt)])
        marker = self.root / namespace(receipt) / "batches/done/accepted.json"
        marker.write_text("invalid: must never read")
        rows, _ = self.scan()
        self.assertEqual(rows, [])

    def test_late_marker_and_new_low_uuid_are_discovered_after_wrap(self):
        receipt = self.marker("z", 2)
        path = self.root / namespace(receipt) / "batches/a"
        path.mkdir()
        rows, _ = self.scan()
        self.finish(rows)
        self.marker("a", 1)
        rows, _ = self.scan()
        self.assertEqual([r["batch_id"] for _, r in rows], ["a"])

    def test_malformed_api_path_and_nonadvancing_token_fail_closed(self):
        self.marker("one", 1)
        with self.assertRaises(Conflict):
            self.scan(page=lambda *_: dict(contents=[dict(name="../escape", is_directory=True)]))
        calls = [0]

        def repeated(*_):
            calls[0] += 1
            return dict(contents=[], next_page_token="same")

        with self.assertRaises(Conflict):
            self.scan(page=repeated)
        self.assertEqual(calls[0], 2)

    def test_marker_identity_mismatch_fails_without_advancing_state(self):
        self.marker("one", 1)
        marker = next(self.root.glob("v1/*/batches/one/accepted.json"))
        receipt = json.loads(marker.read_text())
        receipt["batch_id"] = "other"
        marker.write_text(json.dumps(receipt))
        with self.assertRaises(Conflict):
            self.scan()
        self.assertFalse((self.control / "processor-discovery-pages.json").exists())

    def test_incremental_shards_preserve_legacy_and_detect_conflicts(self):
        old = self.marker("old", 1)
        legacy = dict(version=1, scopes={namespace(old): {"old": old["batch_sha256"]}})
        target = self.control / "processor-staged-index.json"
        processor.atomic_control(target, legacy)
        original = target.read_bytes()
        index = processor.staged_index(self.control)
        new = self.marker("new", 2)
        processor.index_staged(index, self.control, [new])
        self.assertEqual(target.read_bytes(), original)
        loaded = processor.staged_index(self.control)
        self.assertEqual(set(loaded["scopes"][namespace(old)]), {"old", "new"})
        with self.assertRaises(Conflict):
            processor.index_staged(loaded, self.control, [dict(new, batch_sha256="b" * 64)])
        shard = self.control / "processor-staged-shards/a.json"
        processor.atomic_control(
            shard, dict(version=1, scopes={namespace(old): {"old": "a" * 63 + "b"}})
        )
        with self.assertRaises(Conflict):
            processor.staged_index(self.control)
