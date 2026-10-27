#!/usr/bin/env python3
"""Run unittest with isolated state, revision-linked reports and strict skip policy.

This entrypoint uses only the standard library and runs on the runtime floor.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import TypedDict

from quality_rules import permitted_skip

ROOT = Path(__file__).resolve().parents[1]


def available(name):
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def provenance() -> dict[str, object]:
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True)
    revision = commit.stdout.strip() if commit.returncode == 0 else "source-archive"
    if commit.returncode == 0:
        names = subprocess.check_output(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT
        ).split(b"\0")
    else:
        excluded = {
            ".git",
            ".venv",
            "node_modules",
            "reports",
            "test-results",
            ".quality-tools",
            "__pycache__",
            ".hypothesis",
            "dist",
            "build",
        }
        names = [
            os.fsencode(p.relative_to(ROOT))
            for p in ROOT.rglob("*")
            if p.is_file()
            and not (set(p.relative_to(ROOT).parts) & excluded)
            and not any(part.endswith(".egg-info") for part in p.parts)
        ]
    digest = hashlib.sha256()
    for name in sorted(set(names)):
        path = ROOT / os.fsdecode(name)
        # Prose acceptance notes can record this digest without self-reference.
        # Runtime, tests, tooling and configuration (including lockfiles) remain covered.
        if name and path.is_file() and path.suffix != ".md":
            digest.update(name + b"\0" + path.read_bytes() + b"\0")
    return {"base_commit": revision, "source_sha256": digest.hexdigest()}


class TestRecord(TypedDict):
    id: str
    seconds: float


class Result(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records: list[TestRecord] = []
        self.started = 0.0

    def startTest(self, test):
        self.started = time.monotonic()
        super().startTest(test)

    def stopTest(self, test):
        self.records.append({"id": test.id(), "seconds": time.monotonic() - self.started})
        super().stopTest(test)


def main():
    if not shutil.which("node"):
        raise SystemExit("Node is required: UI/hook tests must not silently skip")
    missing = {name for name in ("jwt", "cryptography", "azure.functions") if not available(name)}
    report_dir = ROOT / "reports"
    report_dir.mkdir(exist_ok=True)
    info = provenance()
    with tempfile.TemporaryDirectory(prefix="erg-quality-tests-") as temp:
        for name in list(os.environ):
            if name.startswith(
                ("ERG_", "CODEX_", "CLAUDE_", "OMNIGENT_", "PI_CODING_", "HARNESS_")
            ):
                os.environ.pop(name)
        os.environ.update(
            HOME=temp,
            ERG_HOME=temp + "/ledger",
            ERG_STATE_DIR=temp + "/state",
            ERG_DATABASE=temp + "/ledger/test.db",
            PYTHONDONTWRITEBYTECODE="1",
        )
        sys.path.insert(0, str(ROOT))
        suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
        result = unittest.TextTestRunner(verbosity=2, resultclass=Result).run(suite)
        assert isinstance(result, Result)
    skips = {test.id(): reason for test, reason in result.skipped}
    unexpected = {
        key: reason
        for key, reason in skips.items()
        if not permitted_skip(key, reason, sys.platform, missing)
    }
    failures = {test.id(): detail for test, detail in result.failures + result.errors}
    passed = result.wasSuccessful() and not unexpected and not result.expectedFailures
    info.update(
        command=sys.argv,
        python=sys.version,
        platform=sys.platform,
        tests=result.testsRun,
        skips=skips,
        unexpected_skips=unexpected,
        success=passed,
        exit_status=0 if passed else 1,
    )
    (report_dir / "tests.json").write_text(json.dumps(info, indent=2) + "\n")
    xml = ET.Element(
        "testsuite",
        name="erg",
        tests=str(result.testsRun),
        failures=str(len(failures) + len(unexpected)),
        skipped=str(len(skips)),
    )
    represented = {row["id"] for row in result.records}
    result.records.extend(TestRecord(id=key, seconds=0.0) for key in failures.keys() - represented)
    for row in result.records:
        case = ET.SubElement(xml, "testcase", name=row["id"], time=str(row["seconds"]))
        if row["id"] in failures:
            ET.SubElement(case, "failure").text = failures[row["id"]]
        elif row["id"] in unexpected:
            ET.SubElement(case, "failure").text = "Unexpected skip: " + unexpected[row["id"]]
        elif row["id"] in skips:
            ET.SubElement(case, "skipped").text = skips[row["id"]]
    ET.ElementTree(xml).write(report_dir / "junit.xml", encoding="utf-8", xml_declaration=True)
    if unexpected:
        print("Unexpected skipped tests:", json.dumps(unexpected, indent=2), file=sys.stderr)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
