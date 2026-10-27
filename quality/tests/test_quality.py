"""Negative controls: gates must reject regressions, not merely run tools."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from quality_rules import increased_counts, permitted_skip, weakened_floors  # noqa: E402 - source tooling bootstrap


class GateDecisions(unittest.TestCase):
    def test_new_types_or_larger_shape_are_rejected_without_blanket_ignores(self):
        self.assertEqual(increased_counts({"legacy": 2, "new": 1}, {"legacy": 2}), ["new"])
        self.assertEqual(increased_counts({"legacy": 3}, {"legacy": 2}), ["legacy"])
        self.assertEqual(increased_counts({"legacy": 1}, {"legacy": 2}), [])

    def test_a_removed_or_lower_coverage_floor_is_rejected(self):
        self.assertEqual(weakened_floors({"branches": 70}, {"branches": 69}), ["branches"])
        self.assertEqual(weakened_floors({"branches": 70}, {}), ["branches"])
        self.assertEqual(weakened_floors({"branches": 70}, {"branches": 71}), [])

    def test_skip_allowances_are_test_reason_and_environment_specific(self):
        name = (
            "test_host_counters.HostCounters.test_native_cpu_counters_advance_without_a_subprocess"
        )
        self.assertTrue(permitted_skip(name, "Mach counters require macOS", "linux", set()))
        self.assertFalse(permitted_skip(name, "Mach counters require macOS", "darwin", set()))
        self.assertFalse(permitted_skip(name, "TODO", "linux", set()))
        self.assertFalse(permitted_skip("test_new.Feature.test_regression", "TODO", "linux", set()))


class RealToolControls(unittest.TestCase):
    def test_linter_rejects_an_actual_unused_import(self):
        import subprocess

        result = subprocess.run(
            [sys.executable, "-m", "ruff", "check", "--stdin-filename", "negative.py", "-"],
            input="import unused_quality_dependency\n",
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("F401", result.stdout)

    def test_secret_scanner_rejects_a_generated_credential_shape_without_printing_it(self):
        import subprocess
        import tempfile

        root = Path(__file__).resolve().parents[2]
        token = "ghp_" + "0123456789abcdefABCDEFGHIJKLMNOPQRST"
        with tempfile.TemporaryDirectory(prefix="erg-secret-negative-") as temp:
            (Path(temp) / "negative.txt").write_text('token = "' + token + '"\n')
            result = subprocess.run(
                [str(root / ".quality-tools/gitleaks"), "dir", temp, "--redact", "--no-banner"],
                cwd=root,
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, 1)
        self.assertNotIn(token, result.stdout + result.stderr)

    def test_type_checker_rejects_an_actual_new_error(self):
        import json
        import subprocess
        import tempfile

        root = Path(__file__).resolve().parents[2]
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", prefix="_quality_negative_", dir=root / "erg"
        ) as fixture:
            fixture.write('quality_value: int = "invalid"\n')
            fixture.flush()
            result = subprocess.run(
                [sys.executable, "-m", "pyright", "--outputjson"],
                cwd=root,
                capture_output=True,
                text=True,
            )
            report = json.loads(result.stdout)
            errors = [d for d in report["generalDiagnostics"] if d["file"] == fixture.name]
        self.assertEqual(result.returncode, 1)
        self.assertTrue(any(d.get("rule") == "reportAssignmentType" for d in errors))
