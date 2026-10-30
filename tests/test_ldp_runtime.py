import copy
import hashlib
import platform
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from central.erg_central import ldp_runtime


class NativeRuntimeAdmission(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.module = self.root / "stage.py"
        self.module.write_bytes(b"qualified source")
        self.binary = self.root / "umf" / "native.so"
        self.binary.parent.mkdir()
        self.binary.write_bytes(b"qualified platform binary")
        self.pins = dict(
            python=platform.python_version(),
            machine=platform.machine(),
            packages={"compiler": "1.0"},
            source_sha256={"qualified.stage": hashlib.sha256(self.module.read_bytes()).hexdigest()},
            distribution_files={
                "umf-core": {"umf/native.so": hashlib.sha256(self.binary.read_bytes()).hexdigest()}
            },
        )
        ldp_runtime._VERIFIED.clear()
        self.addCleanup(ldp_runtime._VERIFIED.clear)

    def verify(self, pins=None):
        dist = types.SimpleNamespace(locate_file=lambda name: self.root / name)
        with patch.object(
            ldp_runtime.importlib.metadata, "version", return_value="1.0"
        ), patch.object(
            ldp_runtime.importlib.metadata, "distribution", return_value=dist
        ), patch.object(
            ldp_runtime.importlib,
            "import_module",
            return_value=types.SimpleNamespace(__file__=self.module),
        ):
            ldp_runtime.verify_runtime(pins or self.pins)

    def test_exact_native_versions_source_and_binary_are_admitted(self):
        self.verify()
        self.assertIn(repr(self.pins), ldp_runtime._VERIFIED)

    def test_runtime_mismatches_raise_operational_errors(self):
        for key, value, reason in (
            ("machine", "wrong", "ARCHITECTURE"),
            ("python", "0.0", "VERSION"),
            ("source_sha256", {"qualified.stage": "0" * 64}, "SOURCE"),
            ("distribution_files", {"umf-core": {"umf/native.so": "0" * 64}}, "BINARY"),
            ("distribution_files", {"umf-core": {"../escape": "0" * 64}}, "PATH"),
        ):
            with self.subTest(reason=reason):
                pins = copy.deepcopy(self.pins)
                pins[key] = value
                with self.assertRaisesRegex(RuntimeError, reason):
                    self.verify(pins)
                self.assertNotIn(repr(pins), ldp_runtime._VERIFIED)

    def test_platform_specific_binary_hash_is_selected_exactly(self):
        pins = copy.deepcopy(self.pins)
        sha = pins["distribution_files"]["umf-core"]["umf/native.so"]
        pins["distribution_files"]["umf-core"]["umf/native.so"] = {platform.machine(): sha}
        pins["machines"] = [platform.machine()]
        self.verify(pins)
        pins["machines"] = ["unqualified"]
        with self.assertRaisesRegex(RuntimeError, "ARCHITECTURE"):
            self.verify(pins)
