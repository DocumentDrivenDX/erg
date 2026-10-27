"""Different dashboard windows must not force repeated whole-ledger reports."""

import contextlib
import io
import json
import threading
import unittest
import urllib.request
from unittest.mock import Mock, patch

from erg import server


class ReportCache(unittest.TestCase):
    def test_multiple_windows_reuse_reports_and_explicit_refresh_recomputes(self):
        reports = server.Reports()
        with patch.object(server.store, "open_readonly", return_value=Mock()), patch.object(
            server.store, "dataset", return_value={"llm": []}
        ) as dataset, patch("erg.service.status", return_value={}):
            reports.data(0)
            reports.data(7)
            reports.data(0)
            reports.data(7)
            self.assertEqual(dataset.call_count, 2)
            reports.data(0, refresh=True)
            self.assertEqual(dataset.call_count, 3)
            reports.data(7)
            self.assertEqual(dataset.call_count, 3)

    def test_cache_is_bounded_and_keeps_recently_used_window(self):
        reports = server.Reports()
        with patch.object(server.store, "open_readonly", return_value=Mock()), patch.object(
            server.store, "dataset", return_value={"llm": []}
        ), patch("erg.service.status", return_value={}):
            for days in (1, 2, 3, 4, 1, 5):
                reports.data(days)
            self.assertEqual(len(reports.cache), 4)
            self.assertIn(1, reports.cache)
            self.assertNotIn(2, reports.cache)

    def test_endpoint_cpu_metrics_drop_identifiers_and_bound_routes(self):
        reports = server.Reports()
        reports.record_request("/api/data?days=7", 0.25, 2)
        reports.record_request("/api/data?session=private-id", 0.5, 3)
        for i in range(100):
            reports.record_request("/api/private-" + str(i), 0, 0)
        stats = reports.request_stats()
        self.assertEqual(len(stats["routes"]), 2)
        self.assertNotIn("private-id", str(stats))
        self.assertNotIn("days=", str(stats))
        self.assertEqual(
            stats["routes"]["/api/data"],
            dict(requests=2, cpu_seconds=0.75, wall_seconds=5, max_wall_seconds=3),
        )
        self.assertEqual(stats["routes"]["/api/other"]["requests"], 100)

    def test_encoded_usage_reuses_large_json_and_keeps_health_fresh(self):
        reports = server.Reports()
        encode = json.dumps
        with patch.object(server.store, "open_readonly", return_value=Mock()), patch.object(
            server.store, "dataset", return_value={"llm": [], "title": "Session title"}
        ), patch(
            "erg.service.status", side_effect=[{"tick": 1}, {"tick": 2}, {"tick": 3}]
        ), patch.object(server.json, "dumps", wraps=encode) as dump:
            first = json.loads(reports.encoded_data(7))
            second = json.loads(reports.encoded_data(7))
            self.assertEqual(first["capture_service"], {"tick": 1})
            self.assertEqual(second["capture_service"], {"tick": 2})
            self.assertEqual(
                sum(
                    isinstance(c.args[0], dict) and "llm" in c.args[0] for c in dump.call_args_list
                ),
                1,
            )
            reports.encoded_data(7, refresh=True)
            self.assertEqual(
                sum(
                    isinstance(c.args[0], dict) and "llm" in c.args[0] for c in dump.call_args_list
                ),
                2,
            )
        first.pop("capture_service")
        second.pop("capture_service")
        self.assertEqual(first, second)

    def test_encoded_work_refresh_invalidates_even_with_unchanged_clock_or_object(self):
        reports = server.Reports()
        data = {"sessions": []}
        encode = json.dumps
        with patch.object(server.store, "open_readonly", return_value=Mock()), patch(
            "erg.work.effectiveness", return_value=data
        ) as report, patch.object(server.time, "monotonic", return_value=10), patch.object(
            server.json, "dumps", wraps=encode
        ) as dump:
            first = reports.encoded_effectiveness()
            self.assertIs(first, reports.encoded_effectiveness())
            self.assertEqual(report.call_count, 1)
            self.assertEqual(dump.call_count, 1)
            data["sessions"] = ["new observation"]
            refreshed = reports.encoded_effectiveness(refresh=True)
            self.assertEqual(report.call_count, 2)
            self.assertEqual(dump.call_count, 2)
            self.assertEqual(json.loads(refreshed)["sessions"], ["new observation"])

    def test_encoded_windows_stay_bounded_and_are_private_to_each_report_reader(self):
        a = server.Reports()
        b = server.Reports()
        with patch.object(server.store, "open_readonly", return_value=Mock()), patch.object(
            server.store, "dataset", side_effect=lambda *args: {"llm": []}
        ), patch("erg.service.status", return_value={}):
            for days in (1, 2, 3, 4, 1, 5):
                a.encoded_data(days)
            self.assertEqual(set(a.wire_data), set(a.cache))
            self.assertEqual(len(a.wire_data), 4)
            self.assertEqual(b.wire_data, {})
            b.encoded_data(2)
            self.assertEqual(set(b.wire_data), {2})

    def test_encoded_backend_without_local_work_support_does_not_serve_stale_local_cache(self):
        reports = server.Reports()
        reports.backend = "databricks"
        result = json.loads(reports.encoded_effectiveness())
        self.assertFalse(result["available"])
        self.assertIsNone(reports.wire_work)

    def test_http_cached_reports_preserve_refresh_and_fresh_health(self):
        created = []
        ready = threading.Event()
        real = server.http.server.ThreadingHTTPServer

        def make(address, handler):
            listener = real(address, handler)
            created.append(listener)
            ready.set()
            return listener

        ticks = iter(range(100))
        with patch.object(
            server.http.server, "ThreadingHTTPServer", side_effect=make
        ), patch.object(server.store, "open_readonly", return_value=Mock()), patch.object(
            server.store,
            "dataset",
            side_effect=lambda *args: dict(llm=[], generation=dataset.call_count),
        ) as dataset, patch(
            "erg.work.effectiveness",
            side_effect=lambda *args: dict(sessions=[], generation=work.call_count),
        ) as work, patch(
            "erg.service.status", side_effect=lambda: dict(tick=next(ticks))
        ), contextlib.redirect_stdout(io.StringIO()):
            thread = threading.Thread(target=server.serve, args=(0,), daemon=True)
            thread.start()
            try:
                self.assertTrue(ready.wait(3))
                base = "http://127.0.0.1:" + str(created[0].server_port)

                def get(path):
                    with urllib.request.urlopen(base + path, timeout=5) as response:
                        return json.load(response)

                first = get("/api/data?days=7")
                second = get("/api/data?days=7")
                self.assertEqual(first["generation"], second["generation"])
                self.assertNotEqual(first["capture_service"], second["capture_service"])
                self.assertEqual(get("/api/data?days=7&refresh=1")["generation"], 2)
                a = get("/api/effectiveness")
                b = get("/api/effectiveness")
                self.assertEqual(a, b)
                self.assertEqual(get("/api/effectiveness?refresh=1")["generation"], 2)
                self.assertEqual(dataset.call_count, 2)
                self.assertEqual(work.call_count, 2)
            finally:
                if created:
                    created[0].shutdown()
                thread.join(3)
