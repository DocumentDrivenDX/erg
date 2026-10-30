"""Deterministic isolated LDP adapter closure; no compiler or deployment logic."""

import argparse
import hashlib
import json
import stat
import zipfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("output", type=Path)
parser.add_argument("--runtime-pins", type=Path)
args = parser.parse_args()
output = args.output.resolve()
if output.suffix != ".zip" or output.exists():
    raise SystemExit("new explicit .zip output required")
files = {"erg/__init__.py": b"", "erg_central/__init__.py": b""}
files["erg/delivery_protocol.py"] = (root / "erg/delivery_protocol.py").read_bytes()
for name in ("ldp_codec.py", "disposition.py", "receiver.py"):
    files["erg_central/" + name] = (root / "central/erg_central" / name).read_bytes()
if args.runtime_pins:
    pins = json.loads(args.runtime_pins.read_text())
    files["erg_central/ldp_runtime.py"] = (root / "central/erg_central/ldp_runtime.py").read_bytes()
    files["erg_central/runtime_pins.py"] = ("PINS = " + repr(pins) + "\n").encode()
with zipfile.ZipFile(output, "x", zipfile.ZIP_DEFLATED) as archive:
    for name, raw in sorted(files.items()):
        entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
        entry.create_system = 3
        entry.external_attr = (stat.S_IFREG | 0o644) << 16
        entry.compress_type = zipfile.ZIP_DEFLATED
        archive.writestr(entry, raw)
print(
    json.dumps(
        dict(
            sha256=hashlib.sha256(output.read_bytes()).hexdigest(),
            callable="erg_central.ldp_codec.reconstruct_revision",
            version="1.1.0",
            closure_sha256={n: hashlib.sha256(raw).hexdigest() for n, raw in sorted(files.items())},
        ),
        indent=2,
    )
)
