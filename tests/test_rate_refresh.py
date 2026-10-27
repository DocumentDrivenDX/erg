import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from erg import rate_catalog as r


class RateRefresh(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.patch = patch.object(r, "directory", return_value=self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.client = Mock()
        self.client.profile = "fixture"

    def test_namespace_alone_cannot_wake_a_warehouse_or_modify_the_optin(self):
        p = self.root / "enabled.json"
        p.write_text(json.dumps(dict(namespace="erg.telemetry")))
        before = p.read_bytes()
        r.refresh(self.client, "erg.telemetry", "https://fixture.invalid")
        self.client.sql.assert_not_called()
        self.assertEqual(p.read_bytes(), before)

    def test_workspace_or_profile_mismatch_is_read_only_and_does_not_pull(self):
        r.enable("fixture", "erg.telemetry", "https://fixture.invalid")
        p = self.root / "enabled.json"
        before = p.read_bytes()
        with patch.object(r, "pull") as pull:
            r.refresh(self.client, "erg.telemetry", "https://other.invalid")
            self.client.profile = "other"
            r.refresh(self.client, "erg.telemetry", "https://fixture.invalid")
            pull.assert_not_called()
        self.assertEqual(p.read_bytes(), before)

    def test_matching_source_pulls_once_and_failures_remain_throttled(self):
        r.enable("fixture", "erg.telemetry", "https://fixture.invalid/")
        with patch.object(r.time, "time", return_value=10000), patch.object(
            r, "pull", side_effect=RuntimeError("SECRET")
        ) as pull:
            r.refresh(self.client, "erg.telemetry", "https://fixture.invalid")
            r.refresh(self.client, "erg.telemetry", "https://fixture.invalid")
            self.assertEqual(pull.call_count, 1)
        text = (self.root / "enabled.json").read_text()
        self.assertNotIn("SECRET", text)
        self.assertEqual(json.loads(text)["error"], "RuntimeError")
        self.assertEqual((self.root / "enabled.json").stat().st_mode & 0o777, 0o600)

    def test_success_and_invalid_configuration_have_explicit_boundaries(self):
        for host in (
            "http://fixture.invalid",
            "https://user:SECRET@fixture.invalid",
            "https://fixture.invalid/token",
        ):
            with self.assertRaises(ValueError):
                r.enable("fixture", "erg.telemetry", host)
        r.enable("fixture", "erg.telemetry", "https://fixture.invalid")
        with patch.object(r.time, "time", return_value=10000), patch.object(
            r, "pull", return_value=dict(revision="verified", models=1)
        ):
            r.refresh(self.client, "erg.telemetry", "https://fixture.invalid")
        self.assertEqual(
            json.loads((self.root / "enabled.json").read_text())["revision"], "verified"
        )
