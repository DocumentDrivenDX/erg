"""The acceptance cursor can advance independently of the historical spool."""

import concurrent.futures
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from central.erg_central import discovery, journal, journal_cursor, processor
from central.erg_central.receiver import FileStore, Receiver, namespace
from erg import delivery_protocol as wire
from tests.test_delivery import IDENTITY, payload


class AcceptanceJournal(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = FileStore(self.root / "spool")
        self.control = self.root / "control"
        self.receiver = Receiver(self.store, publish_candidate=journal.append)
        self.identity = dict(IDENTITY, active=True)
        self.staged = dict(version=1, scopes={})
        self.batches = {}

    def accept(self, seq):
        if seq not in self.batches:
            self.batches[seq] = wire.batch(IDENTITY, wire.record_items(payload(), seq))
        raw = self.batches[seq]
        return self.receiver.accept(raw, wire.digest(raw), self.identity)

    def scan(self, limit=512):
        no_files_api = patch.object(
            discovery, "volume_page", side_effect=AssertionError("listed spool")
        )
        no_fuse_list = patch.object(Path, "iterdir", side_effect=AssertionError("listed spool"))
        with no_files_api, no_fuse_list:
            return journal_cursor.journal_markers(
                self.store.root,
                self.control,
                limit,
                time.monotonic(),
                30,
                self.staged,
                processor.atomic_control,
            )

    def complete(self, rows):
        processor.index_staged(self.staged, self.control, [receipt for _, receipt in rows])

    def test_checkpoint_reads_only_new_entries_and_can_resume_inside_a_page(self):
        for seq in range(1, 4):
            self.accept(seq)
        first, initial = self.scan(limit=1)
        self.assertEqual(first[0][1]["accepted_delivery_seqs"], [1])
        self.assertEqual(initial["entries"], 1)
        retry, _ = self.scan(limit=1)
        self.assertEqual(retry, first)
        self.complete(first)
        remaining, later = self.scan()
        self.assertEqual(
            [receipt["accepted_delivery_seqs"] for _, receipt in remaining], [[2], [3]]
        )
        self.assertEqual(later["entries"], 2)
        self.complete(remaining)
        self.assertEqual(self.scan()[1]["entries"], 0)
        self.accept(4)
        new, metrics = self.scan()
        self.assertEqual(len(new), 1)
        self.assertEqual(metrics["entries"], 1)

    def test_failure_between_journal_and_marker_is_reachable_after_retry(self):
        real_put = self.store.put_once

        def fail_marker(key, raw):
            if key.endswith("accepted.json"):
                raise OSError("simulated broker loss")
            return real_put(key, raw)

        with patch.object(self.store, "put_once", side_effect=fail_marker):
            with self.assertRaisesRegex(OSError, "simulated broker loss"):
                self.accept(1)
        self.accept(2)
        published, metrics = self.scan()
        self.assertEqual([r["accepted_delivery_seqs"] for _, r in published], [[2]])
        self.assertEqual(metrics["unresolved"], 2)
        self.complete(published)
        self.accept(1)
        recovered, metrics = self.scan()
        self.assertEqual([r["accepted_delivery_seqs"] for _, r in recovered], [[1]])
        self.assertEqual(metrics["entries"], 0)

    def test_full_page_seals_and_parallel_writers_preserve_every_pointer(self):
        prefix = namespace(IDENTITY)
        markers = [
            f"{prefix}/batches/12345678-0000-0000-0000-{i:012d}/accepted.json"
            for i in range(journal.PAGE_SIZE + 4)
        ]
        for marker in markers[:-4]:
            journal.append(self.store, marker)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda marker: journal.append(self.store, marker), markers[-4:]))
        rows = journal.decode_page(self.store.get(journal.page_key(0)))
        self.assertEqual(rows, markers[:-4])
        self.assertEqual(
            set(journal.decode_page(self.store.get(journal.page_key(1)))), set(markers[-4:])
        )
