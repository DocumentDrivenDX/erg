"""Compute starts only for unprocessed journal entries or unresolved receipts."""

import tempfile
import unittest
from pathlib import Path

from central.erg_central import journal, run_gate
from central.erg_central.receiver import Conflict, FileStore
from erg import delivery_protocol as wire


class RunGate(unittest.TestCase):
    def test_empty_pending_completion_and_new_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FileStore(Path(tmp) / "spool")
            root = "/Volumes/erg/ingest/spool"
            self.assertTrue(run_gate.pending(store, root))
            marker = (
                "v1/" + "a" * 64 + "/batches/00000000-0000-0000-0000-000000000001/accepted.json"
            )
            state = dict(version=1, root=root, page=0, offset=0, retry=[])
            cursor = "processor-journal-cursor.json"
            store.put_control_once(cursor, wire.encode(state))
            self.assertFalse(run_gate.pending(store, root))
            journal.append(store, marker)
            self.assertTrue(run_gate.pending(store, root))
            state.update(offset=1, retry=[marker])
            Path(tmp, "spool/control", cursor).write_bytes(wire.encode(state))
            self.assertTrue(run_gate.pending(store, root))
            scope, tail = marker.rsplit("/batches/", 1)
            store.put_control_once(
                scope + "/batches/" + tail.split("/")[0] + ".json",
                wire.encode(dict(items_staged=True)),
            )
            self.assertFalse(run_gate.pending(store, root))
            state["offset"] = 2
            Path(tmp, "spool/control", cursor).write_bytes(wire.encode(state))
            with self.assertRaises(Conflict):
                run_gate.pending(store, root)
