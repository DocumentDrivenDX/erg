#!/usr/bin/env python3
"""Shared local/CI gate: python scripts/quality.py [all|lint|types|rules|test|audit|browser|package]."""

from __future__ import annotations

import argparse
import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

from quality_rules import increased_counts, weakened_floors

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "quality/baseline.json"


def run(*args, **kwargs):
    return subprocess.run(list(map(str, args)), cwd=ROOT, check=True, **kwargs)


def paths():
    raw = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT
    )
    return sorted(
        {ROOT / os.fsdecode(p) for p in raw.split(b"\0") if p and (ROOT / os.fsdecode(p)).is_file()}
    )


def shape():
    values = Counter()
    for path in paths():
        if path.suffix not in (".py", ".js", ".mjs", ".html"):
            continue
        group = "test" if {"tests", "e2e"} & set(path.relative_to(ROOT).parts[:-1]) else "source"
        text = path.read_text()
        values[group + "_lines"] = max(values[group + "_lines"], len(text.splitlines()))
        if path.suffix != ".py":
            continue
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                methods = sum(
                    isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and not n.name.startswith("_")
                    for n in node.body
                )
                values["public_methods"] = max(values["public_methods"], methods)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                nodes: list[ast.AST] = []
                pending: list[ast.AST] = list(node.body)
                while pending:
                    child = pending.pop()
                    nodes.append(child)
                    if not isinstance(
                        child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
                    ):
                        pending.extend(ast.iter_child_nodes(child))
                statements = sum(isinstance(n, ast.stmt) for n in nodes)
                returns = sum(isinstance(n, ast.Return) for n in nodes)
                branches = sum(
                    isinstance(
                        n,
                        (
                            ast.If,
                            ast.For,
                            ast.AsyncFor,
                            ast.While,
                            ast.ExceptHandler,
                            ast.IfExp,
                            ast.comprehension,
                        ),
                    )
                    for n in nodes
                )
                complexity = (
                    1
                    + branches
                    + sum(len(n.values) - 1 for n in nodes if isinstance(n, ast.BoolOp))
                )
                for key, value in dict(
                    statements=statements, returns=returns, branches=branches, complexity=complexity
                ).items():
                    values[key] = max(values[key], value)
    return dict(values)


def diagnostics():
    counts = Counter()
    reports = {}
    for platform in ("Darwin", "Linux"):
        result = subprocess.run(
            [sys.executable, "-m", "pyright", "--pythonplatform", platform, "--outputjson"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        if result.returncode not in (0, 1):
            raise RuntimeError(result.stderr or result.stdout)
        payload = json.loads(result.stdout)
        reports[platform] = payload
        current = Counter()
        for row in payload["generalDiagnostics"]:
            if row["severity"] != "error":
                continue
            file = Path(row["file"]).relative_to(ROOT).as_posix()
            key = (
                file
                + "|"
                + row.get("rule", "configuration")
                + "|"
                + row["message"].replace(str(ROOT), "<repo>")
            )
            current[key] += 1
        counts |= current
    (ROOT / "reports").mkdir(exist_ok=True)
    (ROOT / "reports/types.json").write_text(json.dumps(reports, indent=2) + "\n")
    return dict(counts)


def architecture():
    protected = {
        "projects": set(),
        "report_scope": set(),
        "project_scope": {"projects"},
        "project_reports": {"projects", "rates", "project_scope", "report_scope"},
        "project_admin": {"projects", "project_reports"},
    }
    edges = {}
    for path in sorted((ROOT / "erg").glob("*.py")):
        local = set()
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    if top not in sys.stdlib_module_names and top != "erg":
                        raise ValueError("Non-stdlib runtime import: " + str(path) + ": " + top)
                    if alias.name.startswith("erg."):
                        local.add(alias.name.split(".")[1])
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    local.update(
                        [node.module.split(".")[0]] if node.module else [a.name for a in node.names]
                    )
                elif node.module and node.module.split(".")[0] not in sys.stdlib_module_names | {
                    "erg"
                }:
                    raise ValueError("Non-stdlib runtime import: " + str(path) + ": " + node.module)
        if path.stem in protected and local - protected[path.stem]:
            raise ValueError(
                "Forbidden dependency from " + path.stem + ": " + str(local - protected[path.stem])
            )
        edges[path.stem] = local
    # Record reachability cycles explicitly; legacy cycles are adoption debt.
    reach = {}
    for origin in edges:
        pending = list(edges[origin])
        seen = set()
        while pending:
            target = pending.pop()
            if target in seen:
                continue
            seen.add(target)
            pending.extend(edges.get(target, set()) - seen)
        reach[origin] = seen
    return sorted(
        origin + "->" + target
        for origin in edges
        for target in edges[origin]
        if origin in reach.get(target, set())
    )


def check_ratchet(current):
    refs = []
    explicit = os.environ.get("QUALITY_BASE_REF")
    if explicit and not explicit.strip("0"):
        explicit = "HEAD^"
    if explicit:
        refs.append(explicit)
    refs.append("HEAD")
    for ref in refs:
        old = subprocess.run(
            ["git", "show", ref + ":quality/baseline.json"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        if old.returncode:
            if explicit and ref == explicit:
                # First adoption may legitimately have no baseline in its base.
                if subprocess.run(
                    ["git", "cat-file", "-e", ref + "^{commit}"], cwd=ROOT, capture_output=True
                ).returncode:
                    raise ValueError("Invalid quality base revision")
            continue
        previous = json.loads(old.stdout)
        for section in ("shape", "types"):
            if increased_counts(current[section], previous[section]):
                raise ValueError("Quality allowance increased: " + section)
        if set(current["cycles"]) - set(previous["cycles"]):
            raise ValueError("New cyclic module allowance")
        if weakened_floors(previous["coverage"], current["coverage"]):
            raise ValueError("Coverage floor weakened")


def rules():
    run(sys.executable, "-m", "unittest", "discover", "-s", "quality/tests", "-v")
    baseline = json.loads(BASELINE.read_text())
    check_ratchet(baseline)
    bad = increased_counts(shape(), baseline["shape"])
    if bad:
        raise ValueError("Shape ceilings exceeded: " + str(bad))
    if set(architecture()) - set(baseline["cycles"]):
        raise ValueError("New cyclic runtime module")
    run(sys.executable, "scripts/check-delivery-boundaries.py")
    print("Architecture, stdlib imports and adoption ratchets passed")


def types():
    baseline = json.loads(BASELINE.read_text())
    check_ratchet(baseline)
    observed = diagnostics()
    new = increased_counts(observed, baseline["types"])
    if new:
        raise ValueError("New type errors:\n" + "\n".join(new))
    if increased_counts(baseline["types"], observed):
        raise ValueError("Resolved type allowances must be retired with the baseline helper")
    print("Type gate passed; remaining legacy diagnostics:", sum(observed.values()))


def tests():
    run(sys.executable, "-m", "coverage", "erase")
    run(sys.executable, "-m", "coverage", "run", "--branch", "scripts/run-tests.py")
    run(sys.executable, "-m", "coverage", "combine")
    run(sys.executable, "-m", "coverage", "json", "-o", "reports/coverage.json")
    run(sys.executable, "-m", "coverage", "xml", "-o", "reports/coverage.xml")
    totals = json.loads((ROOT / "reports/coverage.json").read_text())["totals"]
    measured = {
        "lines": 100 * totals["covered_lines"] / totals["num_statements"],
        "branches": 100 * totals["covered_branches"] / totals["num_branches"],
    }
    floor = json.loads(BASELINE.read_text())["coverage"]
    if weakened_floors(floor, measured):
        raise ValueError("Coverage below measured adoption floor: " + str(measured))
    print("Coverage:", measured)


def secrets():
    tool = ROOT / ".quality-tools/gitleaks"
    if not tool.is_file():
        raise SystemExit("Run python scripts/install-gitleaks.py first")
    run(tool, "git", "--redact", "--no-banner")
    with tempfile.TemporaryDirectory(prefix="erg-public-source-") as temp:
        for path in paths():
            target = Path(temp) / path.relative_to(ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
        run(tool, "dir", temp, "--redact", "--no-banner")


def audit():
    (ROOT / "reports").mkdir(exist_ok=True)
    run(
        "uv",
        "export",
        "--locked",
        "--only-group",
        "dev",
        "--no-emit-project",
        "-o",
        "reports/audit-requirements.txt",
        stdout=subprocess.DEVNULL,
    )
    run(
        sys.executable,
        "-m",
        "pip_audit",
        "-r",
        "reports/audit-requirements.txt",
        "--require-hashes",
        "--disable-pip",
        "-f",
        "json",
        "-o",
        "reports/pip-audit.json",
    )
    run("npm", "audit", "--audit-level=low")
    secrets()


def package():
    for path in (ROOT / "dist").glob("erg-*"):
        path.unlink()
    run(sys.executable, "-m", "build", "--no-isolation")
    run(sys.executable, "scripts/check-release-artifacts.py")
    for kind, pattern in (("wheel", "*.whl"), ("source", "*.tar.gz")):
        (artifact,) = (ROOT / "dist").glob(pattern)
        with tempfile.TemporaryDirectory(prefix="erg-install-" + kind) as temp:
            python = Path(temp) / "bin/python"
            run("uv", "venv", temp, "--python", sys.executable, "--allow-existing")
            run("uv", "pip", "install", "--python", python, "--no-deps", artifact)
            run(python, "-I", "-B", "scripts/smoke-installed.py")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "gate",
        choices=(
            "all",
            "lint",
            "types",
            "rules",
            "test",
            "audit",
            "browser",
            "package",
            "baseline",
        ),
        default="all",
        nargs="?",
    )
    args = parser.parse_args()
    if args.gate == "baseline":
        old = (
            json.loads(BASELINE.read_text())
            if BASELINE.exists()
            else {"coverage": {"lines": 0, "branches": 0}}
        )
        measured = dict(
            shape=shape(), types=diagnostics(), cycles=architecture(), coverage=old["coverage"]
        )
        BASELINE.parent.mkdir(exist_ok=True)
        BASELINE.write_text(json.dumps(measured, indent=2, sort_keys=True) + "\n")
        return
    actions = {
        "lint": lambda: (
            run(sys.executable, "-m", "ruff", "check", "."),
            run(sys.executable, "-m", "ruff", "format", "--check", "."),
        ),
        "types": types,
        "rules": rules,
        "test": tests,
        "audit": audit,
        "browser": lambda: run("npm", "test"),
        "package": package,
    }
    for name in actions if args.gate == "all" else (args.gate,):
        print("Quality gate:", name, flush=True)
        actions[name]()


if __name__ == "__main__":
    main()
