#!/usr/bin/env python3
"""Install only the pinned, SHA-256 verified public scanner binary for this host."""

import hashlib
import io
import json
import os
import platform
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    settings = json.loads((ROOT / "quality/tools.json").read_text())
    system = {"Darwin": "darwin", "Linux": "linux"}.get(platform.system())
    machine = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x64", "AMD64": "x64"}.get(
        platform.machine()
    )
    key = str(system) + "_" + str(machine)
    asset = settings["gitleaks"]["assets"].get(key)
    if not asset:
        raise SystemExit("Unsupported scanner host: " + key)
    with urllib.request.urlopen(asset["url"], timeout=60) as response:
        raw = response.read(32 * 1024 * 1024)
    if hashlib.sha256(raw).hexdigest() != asset["sha256"]:
        raise SystemExit("Scanner checksum mismatch")
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        member = archive.getmember("gitleaks")
        if not member.isfile():
            raise SystemExit("Scanner archive does not contain a regular binary")
        source = archive.extractfile(member)
        if source is None:
            raise SystemExit("Scanner binary unavailable")
        body = source.read()
    target = ROOT / ".quality-tools/gitleaks"
    target.parent.mkdir(exist_ok=True)
    temp = target.with_suffix(".tmp")
    temp.write_bytes(body)
    os.chmod(temp, 0o755)
    os.replace(temp, target)
    print("Installed gitleaks", settings["gitleaks"]["version"])


if __name__ == "__main__":
    main()
