"""Uploader phase accounting covers the real loop without network or credentials."""

import contextlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from erg import delivery_journal as journal
from erg import delivery_service, store


class DeliveryWorkerCPU(unittest.TestCase):
    def test_one_iteration_records_phase_cpu_and_retains_no_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = str(Path(tmp) / "ledger.db")
            identity = dict(
                enrollment_id="fixture-device",
                installation_id="fixture-install",
                destination_generation="fixture-generation",
            )
            with contextlib.closing(store.connect(database)) as db:
                journal.enroll(db, identity)
            stop = threading.Event()
            api = MagicMock()
            api.status.return_value = dict(identity, materialized_through=0, projected_through=0)
            cfg = dict(
                enabled=True,
                broker_origin="https://fixture.invalid",
                token_helper=["fixture-secret-helper"],
                identity=identity,
                reporting_metadata_enabled=True,
            )

            def step(*args):
                stop.set()
                return "idle"

            with patch.object(delivery_service, "configuration", return_value=cfg), patch.object(
                delivery_service.threading, "Event", return_value=stop
            ), patch.object(delivery_service.signal, "signal"), patch.object(
                delivery_service.client, "HTTPS", return_value=api
            ), patch.object(delivery_service.client, "step", side_effect=step), patch(
                "erg.costs_bridge.request_refresh"
            ), patch.object(delivery_service, "directory", return_value=Path(tmp)):
                delivery_service.run(database=database)
            value = json.loads((Path(tmp) / "status.json").read_text())
            self.assertEqual(value["state"], "stopped")
            self.assertEqual(
                set(value["worker_cpu"]["workers"]),
                {"report_metadata", "prepare", "send", "receipt_refresh", "cost_refresh"},
            )
            self.assertTrue(
                all(
                    r["runs"] == 1 and r["errors"] == 0
                    for r in value["worker_cpu"]["workers"].values()
                )
            )
            self.assertGreaterEqual(value["cpu_accounting"]["process_cpu_seconds"], 0)
            self.assertNotIn("fixture-secret-helper", json.dumps(value))
