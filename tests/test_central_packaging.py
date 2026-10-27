"""Deployed code must remain readable by non-root Linux workers and reproducible."""

import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class Packaging(unittest.TestCase):
    def test_allowlisted_archives_are_regular_readable_and_reproducible(self):
        with tempfile.TemporaryDirectory() as temp:
            for component in ("receiver", "processor"):
                outputs = [Path(temp) / (component + str(i) + ".zip") for i in range(2)]
                for output in outputs:
                    subprocess.run(
                        [
                            sys.executable,
                            "-B",
                            str(ROOT / "scripts" / ("package-central-" + component + ".py")),
                            str(output),
                        ],
                        cwd=str(ROOT),
                        check=True,
                        capture_output=True,
                    )
                    with zipfile.ZipFile(output) as archive:
                        for entry in archive.infolist():
                            self.assertEqual(entry.create_system, 3)
                            self.assertEqual(entry.external_attr >> 16, stat.S_IFREG | 0o644)
                            self.assertFalse(entry.filename.endswith((".db", ".env", ".pem")))
                        self.assertIn("erg/delivery_protocol.py", archive.namelist())
                        if component == "receiver":
                            self.assertIn("function_app.py", archive.namelist())
                self.assertEqual(outputs[0].read_bytes(), outputs[1].read_bytes())

    def test_processor_python_archive_imports_in_isolated_python(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "erg-central.pyz"
            subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(ROOT / "scripts/package-central-processor.py"),
                    str(output),
                ],
                cwd=str(ROOT),
                check=True,
                capture_output=True,
            )
            code = "import sys;sys.path.insert(0,sys.argv[1]);import erg.delivery_protocol as p;import erg_central.processor;assert p.VERSION==1;assert '.pyz/' in p.__file__"
            subprocess.run(
                [sys.executable, "-I", "-B", "-c", code, str(output)],
                cwd=temp,
                check=True,
                capture_output=True,
            )
