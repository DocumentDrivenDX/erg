#!/usr/bin/env python3
"""Check release allowlists and fixture completeness without extracting archives."""

import sys
import tarfile
import zipfile
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    dist = Path(sys.argv[1]) if len(sys.argv) > 1 else root / "dist"
    wheels = list(dist.glob("erg-*.whl"))
    sources = list(dist.glob("erg-*.tar.gz"))
    if len(wheels) != 1 or len(sources) != 1:
        raise SystemExit("Expected exactly one wheel and one source archive")
    with zipfile.ZipFile(wheels[0]) as archive:
        names = set(archive.namelist())
        required = {
            "erg/static/index.html",
            "erg/static/databricks.html",
            "erg/hooks/pi.mjs",
            "erg/rate_card.json",
        }
        assert required <= names, required - names
        assert any(n.endswith("/LICENSE-MIT") for n in names)
        assert any(n.endswith("/LICENSE-APACHE") for n in names)
        assert all(n.startswith("erg/") or ".dist-info/" in n for n in names)
        for path in (root / "erg").rglob("*"):
            if path.is_file() and path.suffix in (
                ".py",
                ".html",
                ".mjs",
                ".json",
                ".js",
                ".css",
                ".txt",
            ):
                name = path.relative_to(root).as_posix()
                assert name in names, "Missing runtime file: " + name
    wheel_names = names
    with tarfile.open(sources[0]) as archive:
        names = {n.split("/", 1)[1] for n in archive.getnames() if "/" in n}
        assert {
            "pyproject.toml",
            "PRIVACY.md",
            "LICENSE-MIT",
            "LICENSE-APACHE",
            "central/ldp/pyproject.toml",
            "central/ldp/uv.lock",
        } <= names
        for path in (root / "tests/fixtures").rglob("*"):
            if path.is_file():
                name = path.relative_to(root).as_posix()
                assert name in names, "Missing fixture: " + name
    for name in names | wheel_names:
        assert not any(
            p in name.split("/") for p in (".local-evidence", ".databricks", "__pycache__", ".venv")
        ), name
        assert not name.startswith("docs/helix/04-build/evidence/"), name
        assert not name.endswith((".db", ".pyz", ".zip", ".pem", ".key", ".pyc")), name
    print(
        "Release archives contain runtime assets, licenses and fixtures; no private evidence/build products"
    )


if __name__ == "__main__":
    main()
