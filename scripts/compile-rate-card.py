#!/usr/bin/env python3
"""Regenerate the public offline snapshot from authored prices and overrides."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from erg import rate_catalog, rates  # noqa: E402 - checkout/bootstrap imports follow path and schema setup


def main():
    card = rates.compile_ledger(ROOT / "catalog/prices.yaml")
    overrides = json.loads((ROOT / "catalog/rate-overrides.json").read_text())
    card["entries"].extend(overrides.get("entries", []))
    card["aliases"].update(overrides.get("aliases", {}))
    if "reviewer_valuation" in overrides:
        card["reviewer_valuation"] = overrides["reviewer_valuation"]
    rate_catalog.validate(card)
    (ROOT / "erg/rate_card.json").write_text(json.dumps(card, indent=2, sort_keys=True) + "\n")
    print("Compiled %d dated rates" % len(card["entries"]))


if __name__ == "__main__":
    main()
