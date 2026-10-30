"""Compile explicit UMF deployment bindings; no cloud or production action."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from central.ldp.project import compile_project  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = compile_project(json.loads(args.profile.read_text()), args.output)
    print(json.dumps(manifest, sort_keys=True))


if __name__ == "__main__":
    main()
