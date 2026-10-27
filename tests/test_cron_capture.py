"""Cron must share durable forward capture, without enrolling or serving implicitly."""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from erg import account_probe, cli, collect, history, routes, service, store, titles, work


class CronCapture(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.path = self.root / "ledger"
        for obj, key, value in (
            (collect, "HOME", str(self.root)),
            (collect, "STATE_DIR", str(self.root / "state")),
            (collect, "machine_name", lambda: "host"),
        ):
            p = patch.object(obj, key, value)
            p.start()
            self.addCleanup(p.stop)
        self.db = store.connect(self.path)
        self.addCleanup(self.db.close)

    def context(self):
        stack = contextlib.ExitStack()
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        stack.enter_context(patch.object(work, "scan", return_value={"errors": 0}))
        stack.enter_context(patch.object(titles, "refresh", return_value={"errors": 0}))
        stack.enter_context(patch.object(account_probe, "poll", return_value=0))
        stack.enter_context(patch.object(routes, "profiles", return_value=[]))
        stack.enter_context(patch.object(routes, "backfill_once", return_value=0))
        return stack

    def test_forward_body_and_usage_survive_pruning_and_repeated_cron_runs(self):
        history.enroll(self.db)
        stamp = int(time.time()) + 1
        logs = self.root / "logs"
        logs.mkdir()
        path = logs / "session.jsonl"
        path.write_text(
            "\n".join(
                json.dumps(r)
                for r in [
                    dict(type="session", id="s", timestamp=stamp, cwd="/code/project"),
                    dict(
                        type="message",
                        id="call",
                        timestamp=stamp,
                        message=dict(
                            role="assistant",
                            model="m",
                            content="retained",
                            usage=dict(input=5, output=3),
                        ),
                    ),
                ]
            )
            + "\n"
        )
        with self.context(), contextlib.ExitStack() as stack:
            for name, roots in (
                ("pi_roots", [str(logs)]),
                ("native_roots", []),
                ("claude_roots", []),
                ("opencode_roots", []),
            ):
                stack.enter_context(patch.object(collect, name, return_value=roots))
            calls = []

            def enrich(db, host, full, retained):
                calls.append(retained)
                self.assertEqual(
                    db.execute(
                        "SELECT count(*) FROM history_events WHERE role='assistant'"
                    ).fetchone()[0],
                    1,
                )

            stack.enter_context(patch.object(cli, "sync_one", side_effect=enrich))
            cli.capture_once(self.db)
            path.unlink()
            cli.capture_once(self.db)
        self.assertEqual(calls, [True, True])
        self.assertEqual(
            self.db.execute("SELECT count(*),sum(fresh_in),sum(out) FROM calls").fetchone(),
            (1, 5, 3),
        )
        self.assertEqual(
            history.events(self.db, "pi", "s")["events"][-1]["body"]["message"]["content"],
            "retained",
        )

    def test_unenrolled_collection_does_not_capture_bodies_or_enable_content(self):
        with self.context(), patch.object(cli, "sync_one") as sync, patch.object(
            history, "scan", side_effect=AssertionError("implicit body capture")
        ):
            result = cli.capture_once(self.db)
        self.assertFalse(result["history"]["enabled"])
        self.assertFalse(history.config().get("content"))
        sync.assert_called_once_with(self.db, None, False, retained=False)

    def test_opt_in_for_another_ledger_is_rejected_before_capture_or_enrichment(self):
        history.enroll(self.db)
        with self.db:
            self.db.execute("DELETE FROM history_config WHERE key='enrolled_at'")
        with patch.object(history, "scan") as scan, patch.object(cli, "sync_one") as sync:
            with self.assertRaisesRegex(RuntimeError, "not enrolled"):
                cli.capture_once(self.db)
        scan.assert_not_called()
        sync.assert_not_called()

    def test_retained_enrichment_uses_this_ledger_and_package_worker_from_any_directory(self):
        history.enroll(self.db)
        seen = []
        response = subprocess.CompletedProcess(
            [], 0, stdout=json.dumps(dict(t="hello", machine="host")) + "\n", stderr=""
        )

        def execute(argv, **kwargs):
            seen.append((argv, kwargs))
            return response

        with patch.object(cli.subprocess, "run", side_effect=execute), contextlib.redirect_stdout(
            io.StringIO()
        ):
            cli.sync_one(self.db, None, retained=True)
        argv, kwargs = seen[0]
        self.assertEqual(argv[2], "-c")
        self.assertIn("runpy.run_module('erg.collect'", argv[3])
        self.assertEqual(argv[4], "collect")
        self.assertEqual(argv[argv.index("--retained-database") + 1], str(self.path.resolve()))
        self.assertEqual(
            kwargs["env"]["PYTHONPATH"].split(os.pathsep)[0], str(Path(cli.COLLECT).parent.parent)
        )

    def test_body_capture_is_committed_before_enrichment_failure(self):
        history.enroll(self.db)

        def capture(db):
            with db:
                history.put(
                    db,
                    "pi",
                    "s",
                    "project",
                    int(time.time()),
                    "message",
                    dict(content="durable"),
                    "entry",
                )
            return dict(enabled=True, errors=0)

        with patch.object(history, "scan", side_effect=capture), patch.object(
            cli, "sync_one", side_effect=RuntimeError("enrichment failed")
        ):
            with self.assertRaises(RuntimeError):
                cli.capture_once(self.db)
        self.assertEqual(self.db.execute("SELECT count(*) FROM history_events").fetchone()[0], 1)

    def test_actual_cli_from_cron_directory_obeys_lock_and_keeps_pruned_content(self):
        history.enroll(self.db)
        logs = self.root / ".pi/agent/sessions"
        logs.mkdir(parents=True)
        stamp = int(time.time()) + 1
        native = logs / "session.jsonl"
        native.write_text(
            "\n".join(
                json.dumps(row)
                for row in [
                    dict(
                        type="session",
                        id="cron-proof",
                        timestamp=stamp,
                        cwd=str(self.root / "project"),
                    ),
                    dict(
                        type="message",
                        id="call",
                        timestamp=stamp,
                        message=dict(
                            role="assistant",
                            model="m",
                            content="retained",
                            usage=dict(input=5, output=3),
                        ),
                    ),
                ]
            )
            + "\n"
        )
        env = dict(
            os.environ,
            HOME=str(self.root),
            PATH="/usr/bin:/bin",
            ERG_STATE_DIR=collect.STATE_DIR,
            ERG_DATABASE=str(self.path),
            CODEX_HOME=str(self.root / ".codex"),
            CLAUDE_CONFIG_DIR=str(self.root / ".claude"),
            PI_CODING_AGENT_DIR=str(self.root / ".pi/agent"),
            PI_CODING_AGENT_SESSION_DIR=str(logs),
            OMNIGENT_DATA_DIR=str(self.root / ".omnigent"),
            ERG_DISCOVERY_FILE=str(self.root / "discovery.json"),
            XDG_DATA_HOME=str(self.root / ".local/share"),
            PYTHONDONTWRITEBYTECODE="1",
        )
        env.pop("PYTHONPATH", None)
        shadow = self.root / "erg"
        shadow.mkdir()
        (shadow / "__init__.py").write_text("raise AssertionError('wrong installation')\n")
        entry = Path(cli.__file__).resolve().parents[1] / "bin/erg"

        def run():
            return subprocess.run(
                [sys.executable, "-B", str(entry), "collect"],
                cwd=self.root,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )

        with collect.collector_lock():
            blocked = run()
        self.assertNotEqual(blocked.returncode, 0)
        self.assertEqual(self.db.execute("SELECT count(*) FROM history_events").fetchone()[0], 0)
        first = run()
        self.assertEqual(first.returncode, 0, first.stderr)
        native.unlink()
        second = run()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(
            self.db.execute(
                "SELECT count(*),sum(fresh_in),sum(out) FROM calls WHERE tool='pi' AND session='cron-proof'"
            ).fetchone(),
            (1, 5, 3),
        )
        self.assertEqual(
            history.events(self.db, "pi", "cron-proof")["events"][-1]["body"]["message"]["content"],
            "retained",
        )

    def test_retained_cycle_reuses_wrapper_linking_without_duplicate_pi_usage_scrape(self):
        history.enroll(self.db)
        with self.context(), patch.object(cli, "sync_one"), patch.object(
            collect, "collect_pi", side_effect=AssertionError("duplicate Pi scraper")
        ), patch.object(service, "fast_capture", wraps=service.fast_capture) as metadata:
            result = cli.capture_once(self.db)
        metadata.assert_called_once_with(self.db, 0, include_pi=False)
        self.assertIsNotNone(result["metadata"])

    def test_cli_reports_partial_capture_failure_without_exporting(self):
        result = dict(
            history=dict(enabled=True, errors=1),
            work={},
            titles={},
            account_profiles=0,
            metric_rollups={},
        )
        from erg import databricks

        with patch.object(store, "connect", side_effect=lambda: self.db), patch.object(
            cli, "capture_once", return_value=result
        ), patch.object(collect, "take_sample"), patch.object(
            databricks, "Client"
        ) as client, patch.object(databricks, "sync") as export, contextlib.redirect_stdout(
            io.StringIO()
        ):
            with self.assertRaisesRegex(RuntimeError, "needs attention"):
                cli.main(
                    [
                        "collect",
                        "--push",
                        "--profile",
                        "fixture",
                        "--warehouse",
                        "fixture",
                        "--namespace",
                        "private.erg",
                    ]
                )
        client.assert_not_called()
        export.assert_not_called()

    def test_private_managed_launcher_works_without_checkout_or_shell_path(self):
        managed = self.root / "managed space"
        runtime = managed / "runtime"
        runtime.mkdir(parents=True)
        package = runtime / "erg"
        package.mkdir()
        source = Path(cli.__file__).parent
        for path in list(source.glob("*.py")) + list(source.glob("*.json")):
            shutil.copy2(path, package / path.name)
        sessions = self.root / "custom-pi"
        sessions.mkdir()
        (sessions / "one.jsonl").write_text("{}\n")
        discovery = self.root / "custom-discovery.json"
        discovery.write_text(json.dumps(dict(pi_session_dirs=[str(sessions)])))
        launcher = service.install_cli(
            managed,
            runtime,
            managed / "ledger",
            dict(
                ERG_DISCOVERY_FILE=str(discovery),
                CODEX_HOME=str(self.root / "custom-codex"),
                ANTHROPIC_API_KEY="SECRET",
            ),
        )
        self.assertNotIn("SECRET", Path(launcher).read_text())
        env = dict(os.environ, HOME=str(self.root), PATH="/usr/bin:/bin")
        for key in ("PYTHONPATH", "ERG_STATE_DIR", "ERG_DATABASE") + service.DISCOVERY_ENV:
            env.pop(key, None)
        shadow = self.root / "erg"
        shadow.mkdir()
        (shadow / "__init__.py").write_text("raise AssertionError('wrong installation')\n")
        result = subprocess.run(
            [launcher, "--help"], cwd=self.root, env=env, capture_output=True, text=True, timeout=10
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("collect", result.stdout)
        self.assertEqual(Path(launcher).stat().st_mode & 0o777, 0o700)
        inventory = subprocess.run(
            [launcher, "sources"],
            cwd=self.root,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(inventory.returncode, 0, inventory.stderr)
        self.assertIn(
            "history_files=1",
            next(line for line in inventory.stdout.splitlines() if line.startswith("pi ")),
        )
        # Explicit user overrides remain available, while source-package imports
        # always come from this private installation rather than the checkout.
        settings = managed / "capture.json"
        settings.write_text(json.dumps(dict(content=True, enrolled_at=100)))
        override = self.root / "override"
        override.mkdir()
        (override / "capture.json").write_text(json.dumps(dict(content=False)))
        env.update(ERG_STATE_DIR=str(override), ERG_DATABASE=str(self.path))
        status = subprocess.run(
            [launcher, "history", "status"],
            cwd=self.root,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertFalse(json.loads(status.stdout)["enabled"])
        enrolled = subprocess.run(
            [launcher, "history", "enroll"],
            cwd=self.root,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(enrolled.returncode, 0, enrolled.stderr)
        stamp = int(time.time()) + 1
        (sessions / "one.jsonl").write_text(
            "\n".join(
                json.dumps(row)
                for row in [
                    dict(
                        type="session",
                        id="managed-proof",
                        timestamp=stamp,
                        cwd=str(self.root / "project"),
                    ),
                    dict(
                        type="message",
                        id="call",
                        timestamp=stamp,
                        message=dict(
                            role="assistant",
                            model="m",
                            content="managed body",
                            usage=dict(input=2, output=4),
                        ),
                    ),
                ]
            )
            + "\n"
        )
        captured = subprocess.run(
            [launcher, "collect"],
            cwd=self.root,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(captured.returncode, 0, captured.stderr)
        self.assertNotIn("managed body", captured.stdout)
        self.assertEqual(
            self.db.execute(
                "SELECT count(*),sum(fresh_in),sum(out) FROM calls WHERE session='managed-proof'"
            ).fetchone(),
            (1, 2, 4),
        )
        self.assertEqual(
            history.events(self.db, "pi", "managed-proof")["events"][-1]["body"]["message"][
                "content"
            ],
            "managed body",
        )
