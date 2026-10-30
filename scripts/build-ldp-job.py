"""Write a paused Jobs template for an existing generated LDP pipeline; no API calls."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from central.ldp.deployment import job_settings  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    settings = job_settings(json.loads(args.profile.read_text()))
    with args.output.open("x") as stream:
        stream.write(json.dumps(settings, sort_keys=True, indent=2) + "\n")


if __name__ == "__main__":
    main()
