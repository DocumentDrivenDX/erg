"""Reporter lifecycle must remain independent, private and recoverable."""

import json
import plistlib
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from erg import collect, store
from erg import reporter_service as reporter


class ReporterService(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.state = self.home / "state"
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            self.port = listener.getsockname()[1]
        for obj, name, value in (
            (collect, "HOME", str(self.home)),
            (collect, "STATE_DIR", str(self.state)),
            (store, "DB_PATH", str((self.home / "private.db").resolve())),
            (reporter.sys, "platform", "darwin"),
        ):
            mock = patch.object(obj, name, value)
            mock.start()
            self.addCleanup(mock.stop)
        self.ready = dict(running=True, state="running")

    def install(self, port="initial", loaded=False):
        if port == "initial":
            port = self.port
        with patch.object(
            reporter, "job", return_value=dict(loaded=loaded, pid=123 if loaded else None)
        ), patch.object(reporter, "health", return_value=loaded), patch.object(
            reporter, "bootstrap"
        ), patch.object(reporter, "wait_ready", return_value=self.ready), patch.object(
            reporter.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)
        ) as command:
            result = reporter.install(port)
        return result, command

    def test_install_is_independent_local_and_uses_private_runtime(self):
        result, command = self.install()
        state, plist = reporter.paths()
        config = plistlib.loads(plist.read_bytes())
        argv = config["ProgramArguments"]
        self.assertEqual(config["ProcessType"], "Standard")
        self.assertTrue(config["LowPriorityBackgroundIO"])
        self.assertNotIn("LowPriorityIO", config)
        self.assertTrue(result["running"])
        self.assertEqual(config["Label"], reporter.LABEL)
        self.assertEqual(argv[3:6], ["-m", "erg.cli", "serve"])
        self.assertEqual(argv[argv.index("--backend") + 1], "sqlite")
        self.assertEqual(argv[argv.index("--bind") + 1], "127.0.0.1")
        self.assertEqual(
            config["EnvironmentVariables"]["ERG_DATABASE"],
            str((self.home / "private.db").resolve()),
        )
        self.assertNotIn("ERG_PROFILE", config["EnvironmentVariables"])
        self.assertTrue(config["KeepAlive"])
        self.assertTrue(config["RunAtLoad"])
        self.assertEqual(plist.stat().st_mode & 0o777, 0o600)
        self.assertEqual((state / "reporter.err").stat().st_mode & 0o777, 0o600)
        self.assertTrue(
            (Path(reporter.read_config()["runtime"]) / "erg/static/index.html").is_file()
        )
        self.assertFalse((self.home / "Library/LaunchAgents/dev.ddx.erg.collector.plist").exists())
        command.assert_not_called()

    def test_install_freezes_explicit_prices_without_a_checkout_catalog(self):
        card = dict(version=1, entries=[], aliases={})
        override = self.home / "prices.json"
        override.write_text(json.dumps(card))
        with patch.dict("os.environ", {"ERG_PRICE_LEDGER": str(override)}):
            self.install()
        installed = Path(reporter.read_config()["runtime"]) / "erg/rate_card.json"
        self.assertEqual(json.loads(installed.read_text()), card)

    def test_occupied_port_and_unknown_job_are_left_untouched(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            with patch.object(
                reporter, "job", return_value=dict(loaded=False, pid=None)
            ), patch.object(reporter.subprocess, "run") as command:
                with self.assertRaisesRegex(RuntimeError, "occupied"):
                    reporter.install(port)
                command.assert_not_called()
        self.assertFalse(reporter.paths()[1].exists())
        with patch.object(reporter, "job", return_value=dict(loaded=True, pid=123)), patch.object(
            reporter.subprocess, "run"
        ) as command:
            with self.assertRaisesRegex(RuntimeError, "not recognized"):
                reporter.install(8788)
            command.assert_not_called()

    def test_upgrade_preserves_port_and_only_keeps_previous_runtime(self):
        self.install()
        first = reporter.read_config()
        self.install(None, True)
        second = reporter.read_config()
        self.assertEqual(second["port"], self.port)
        self.assertNotEqual(first["instance"], second["instance"])
        self.install(None, True)
        third = reporter.read_config()
        self.assertEqual(
            {p.name for p in (self.state / "reporter-runtime").iterdir()},
            {second["instance"], third["instance"]},
        )

    def test_failed_upgrade_restores_previous_configuration_and_runtime(self):
        self.install()
        previous = reporter.read_config()
        old_plist = reporter.paths()[1].read_bytes()
        with patch.object(reporter, "job", return_value=dict(loaded=True, pid=123)), patch.object(
            reporter, "health", return_value=True
        ), patch.object(reporter, "bootstrap") as bootstrap, patch.object(
            reporter, "wait_ready", side_effect=RuntimeError("injected failed readiness")
        ), patch.object(
            reporter.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)
        ):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                reporter.install()
            self.assertEqual(bootstrap.call_count, 2)
        self.assertEqual(reporter.read_config(), previous)
        self.assertEqual(reporter.paths()[1].read_bytes(), old_plist)
        self.assertEqual(len(list((self.state / "reporter-runtime").iterdir())), 1)

    def test_status_requires_matching_health_and_live_job(self):
        self.install()
        with patch.object(reporter, "job", return_value=dict(loaded=True, pid=123)), patch.object(
            reporter, "health", return_value=False
        ):
            self.assertFalse(reporter.status()["running"])
        with patch.object(reporter, "job", return_value=dict(loaded=False, pid=None)), patch.object(
            reporter, "health", return_value=True
        ):
            self.assertFalse(reporter.status()["running"])
        with patch.object(reporter, "job", return_value=dict(loaded=True, pid=456)), patch.object(
            reporter, "health", return_value=True
        ):
            self.assertEqual(reporter.status()["pid"], 456)
            self.assertTrue(reporter.status()["running"])

    def test_stop_does_not_touch_collector_or_unrecognized_plist(self):
        self.install()
        with patch.object(reporter, "job", return_value=dict(loaded=True, pid=123)), patch.object(
            reporter.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)
        ) as command:
            self.assertEqual(reporter.stop()["state"], "stopped")
            self.assertTrue(command.call_args.args[0][-1].endswith("/" + reporter.LABEL))
        _, plist = reporter.paths()
        data = plistlib.loads(plist.read_bytes())
        data["Label"] = "unrelated"
        plist.write_bytes(plistlib.dumps(data))
        with patch.object(reporter.subprocess, "run") as command:
            with self.assertRaisesRegex(RuntimeError, "not recognized"):
                reporter.stop()
            command.assert_not_called()

    def test_invalid_port_and_unsupported_platform_do_not_mutate(self):
        for port in (-1, 0, 65536, True, "8788"):
            with self.assertRaises(ValueError):
                reporter.install(port)
        with patch.object(reporter.sys, "platform", "linux"):
            self.assertFalse(reporter.status()["supported"])
            with self.assertRaisesRegex(RuntimeError, "macOS"):
                reporter.install(8788)
        self.assertFalse(self.state.exists())

    def test_health_rejects_a_different_reporter_instance(self):
        import http.server
        import threading

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"status":"ok","instance":"correct"}')

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            self.assertFalse(reporter.health(dict(port=server.server_port, instance="wrong")))
            self.assertTrue(reporter.health(dict(port=server.server_port, instance="correct")))
        finally:
            server.shutdown()
            server.server_close()
