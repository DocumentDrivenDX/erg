"""Assert exact synthetic output from a completed generated LDP update (read-only)."""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from central.ldp.native_output import qualify_capacity, qualify_faults, qualify_output  # noqa: E402
from erg.databricks import Client  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--document", type=Path, required=True)
    parser.add_argument("--pipeline-id", required=True)
    parser.add_argument("--update-id", required=True)
    parser.add_argument("--phase", type=int, choices=(1, 2, 3, 4), default=3)
    parser.add_argument("--case", choices=("happy", "faults", "capacity"), default="happy")
    parser.add_argument("--capacity-items", type=int, default=8398)
    parser.add_argument("--scope-faults", action="store_true")
    parser.add_argument("--databricks-profile", required=True)
    parser.add_argument("--warehouse", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    client = Client(profile=args.databricks_profile, warehouse=args.warehouse)
    document = json.loads(args.document.read_text())
    if args.case == "happy":
        result, _ = qualify_output(client, document, args.pipeline_id, args.update_id, args.phase)
    elif args.case == "faults":
        result = qualify_faults(
            client, document, args.pipeline_id, args.update_id, args.scope_faults
        )
    else:
        result = qualify_capacity(
            client, document, args.pipeline_id, args.update_id, args.capacity_items
        )
    with os.fdopen(
        os.open(args.evidence, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
    ) as output:
        json.dump(result, output, sort_keys=True, indent=2)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
