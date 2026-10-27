"""Shared account snapshots require stable file-store proof, never credential reads."""

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from erg import account_probe, accounting, collect, store
from erg import account_probe_context as context


class AccountProbeContext(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.a = self.root / ".codex"
        self.b = self.root / "isolated"
        for home in (self.a, self.b):
            home.mkdir()
        (self.a / "auth.json").write_text("FIXTURE MUST NOT BE READ")
        (self.b / "auth.json").symlink_to(self.a / "auth.json")
        for target, value in [("HOME", str(self.root)), ("machine_name", lambda: "fixture")]:
            p = patch.object(collect, target, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.dict(os.environ, {}, clear=True)
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(context, "managed", return_value=False)
        self.managed = p.start()
        self.addCleanup(p.stop)
        p = patch.object(
            collect, "native_roots", return_value=[str(h / "sessions") for h in (self.a, self.b)]
        )
        p.start()
        self.addCleanup(p.stop)
        self.db = store.connect(str(self.root / "ledger"))
        self.addCleanup(self.db.close)

    def response(self, account="a", used=10, reset=1000, complete=True):
        return dict(
            account=dict(
                type="chatgpt", accountId=account, email="fixture@example.invalid", planType="pro"
            ),
            limits=dict(
                accountId=account,
                rateLimitsByLimitId=dict(
                    codex=dict(
                        limitId="codex",
                        primary=dict(usedPercent=used, windowDurationMins=300, resetsAt=reset),
                    )
                ),
            ),
            _complete=complete,
        )

    def poll(self, response=None):
        with patch.object(
            account_probe, "read", return_value=response or self.response()
        ) as rpc, patch.object(account_probe.time, "time", return_value=100):
            observed = account_probe.poll(self.db)
        return observed, rpc.call_count, dict(account_probe.poll.last_stats)

    def test_shared_file_uses_one_rpc_preserves_alias_provenance_and_quota_once(self):
        original = Path.open

        def guarded(path, *args, **kwargs):
            if path.name == "auth.json":
                raise AssertionError("credential read")
            return original(path, *args, **kwargs)

        with patch.object(Path, "open", guarded):
            observed, calls, stats = self.poll()
        self.assertEqual((observed, calls, stats["shared_auth_snapshots"]), (2, 1, 1))
        identities = accounting.rows(self.db, "identity_events")
        self.assertEqual(len(identities), 2)
        self.assertEqual(
            {r["provenance"] for r in identities},
            {"codex_account_read", "codex_shared_account_read"},
        )
        shared = next(r for r in identities if r["provenance"] == "codex_shared_account_read")
        self.assertEqual((shared["scope"], shared["ts"]), ("shared_auth_snapshot", 100))
        self.assertEqual(len(accounting.rows(self.db, "quota_events")), 1)
        resolve, _, _ = accounting.resolver(self.db)
        self.assertFalse(resolve("codex", "", 100)["billing_evidence"])
        self.assertEqual(self.poll()[1], 1)  # Fresh RPC every poll; no cross-cycle cache.

    def test_separate_copies_and_service_overrides_do_not_share(self):
        (self.b / "auth.json").unlink()
        (self.b / "auth.json").write_text("FIXTURE MUST NOT BE READ")
        self.assertEqual(self.poll()[1], 2)
        (self.b / "auth.json").unlink()
        (self.b / "auth.json").symlink_to(self.a / "auth.json")
        (self.b / "config.toml").write_text(
            'chatgpt_base_url = "https://service.example.invalid/"\n'
        )
        self.assertEqual(self.poll()[1], 2)

    def test_keyring_without_auth_file_is_probed_independently(self):
        (self.b / "auth.json").unlink()
        (self.b / "config.toml").write_text('cli_auth_credentials_store = "keyring"\n')
        self.assertEqual(self.poll()[:2], (2, 2))

    def test_managed_unknown_environment_and_partial_rpcs_do_not_share(self):
        self.managed.return_value = True
        self.assertEqual(self.poll()[1], 2)
        self.managed.return_value = False
        with patch.dict(os.environ, {"CODEX_ACCESS_TOKEN": "fixture"}):
            self.assertEqual(self.poll()[1], 2)
        self.assertEqual(self.poll(self.response(complete=False))[1], 2)
        with patch.object(account_probe, "read", return_value=None) as rpc:
            self.assertEqual(account_probe.poll(self.db), 0)
            self.assertEqual(rpc.call_count, 2)

    def test_auth_rotation_during_probe_does_not_copy_stale_identity(self):
        seen = []

        def read(home):
            seen.append(home)
            if len(seen) == 1:
                (self.b / "auth.json").unlink()
                (self.b / "auth.json").write_text("new fixture")
                return self.response("a")
            return self.response("b")

        with patch.object(account_probe, "read", side_effect=read):
            self.assertEqual(account_probe.poll(self.db), 2)
        self.assertEqual(len(seen), 2)
        self.assertEqual(
            len({r["account_key"] for r in accounting.rows(self.db, "identity_events")}), 2
        )
        self.assertEqual(account_probe.poll.last_stats["shared_auth_snapshots"], 0)

    def test_config_rotation_during_probe_forces_independent_rpc(self):
        def read(home):
            if home == self.a:
                (self.b / "config.toml").write_text('cli_auth_credentials_store = "auto"\n')
            return self.response()

        with patch.object(account_probe, "read", side_effect=read) as rpc:
            account_probe.poll(self.db)
            self.assertEqual(rpc.call_count, 2)

    def test_token_refresh_or_config_replacement_invalidates_pre_probe_guards(self):
        def read(home):
            if home == self.a:
                (self.a / "auth.json").write_text("token refresh fixture")
            return self.response()

        with patch.object(account_probe, "read", side_effect=read) as rpc:
            account_probe.poll(self.db)
            self.assertEqual(rpc.call_count, 2)

    def test_literal_root_settings_and_unsupported_config_fall_back_safely(self):
        (self.b / "config.toml").write_text(
            '"cli_auth_credentials_store" = \'file\' # comment\n[model_providers.other]\ncli_auth_credentials_store = "keyring"\n'
        )
        self.assertEqual(self.poll()[1], 1)
        for config in (
            'cli_auth_credentials_store = "auto"\n',
            'profile = "named"\n',
            'instructions = """multiline"""\n',
            'x = [\n"list"\n]\n',
            'cli_auth_credentials_store = "file"\ncli_auth_credentials_store = "keyring"\n',
        ):
            with self.subTest(config=config):
                (self.b / "config.toml").write_text(config)
                self.assertEqual(self.poll()[1], 2)

    def test_reset_is_observed_once_for_the_shared_account(self):
        self.poll(self.response(used=99, reset=1000))
        self.poll(self.response(used=2, reset=2000))
        quotas = accounting.rows(self.db, "quota_events")
        self.assertEqual(sum(q["event"] == "reset_observed" for q in quotas), 1)

    def test_rpc_errors_mark_result_incomplete_without_refresh_or_login(self):
        read_fd, write_fd = os.pipe()
        stream = os.fdopen(read_fd, "rb", buffering=0)
        messages = [
            dict(id=0, result={}),
            dict(id=1, result=dict(account=self.response()["account"])),
            dict(id=2, error=dict(code=-1, message="fixture")),
        ]
        os.write(write_fd, b"".join(json.dumps(m).encode() + b"\n" for m in messages))
        os.close(write_fd)

        class Input(io.BytesIO):
            def close(self):
                self.saved = self.getvalue()
                super().close()

        stdin = Input()
        proc = Mock(stdin=stdin, stdout=stream)
        proc.poll.return_value = 0
        proc.wait.return_value = 0
        try:
            with patch.object(
                account_probe.shutil, "which", return_value="/fixture/codex"
            ), patch.object(account_probe.subprocess, "Popen", return_value=proc):
                result = account_probe.read(self.a)
            self.assertFalse(result["_complete"])
            requests = [json.loads(line) for line in stdin.saved.splitlines()]
            self.assertEqual(
                [r["method"] for r in requests],
                ["initialize", "initialized", "account/read", "account/rateLimits/read"],
            )
            self.assertFalse(requests[2]["params"]["refreshToken"])
        finally:
            stream.close()
