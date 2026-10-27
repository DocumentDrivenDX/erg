"""A wheel must price the same observations as a checkout, without ambient files."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from erg import collect, rates


class PackagedRates(unittest.TestCase):
    def test_default_uses_bundled_snapshot_and_explicit_override_is_complete(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            "os.environ", {}, clear=True
        ), patch.object(collect, "STATE_DIR", temp):
            expected = json.loads(Path(rates.__file__).with_name("rate_card.json").read_text())
            self.assertEqual(rates.load(), expected)
            override = Path(temp) / "custom.json"
            card = dict(version=1, aliases={}, entries=[])
            override.write_text(json.dumps(card))
            with patch.dict("os.environ", {"ERG_PRICE_LEDGER": str(override)}):
                self.assertEqual(rates.load(), card)
            shared = Path(temp) / "rate-catalog/current.json"
            shared.parent.mkdir()
            shared.write_text(json.dumps(card))
            self.assertEqual(rates.load(), card)
            with patch.dict("os.environ", {"ERG_PRICE_LEDGER": str(Path(temp) / "missing.json")}):
                with self.assertRaises(FileNotFoundError):
                    rates.load()

    def test_snapshot_matches_authored_prices_overrides_and_valuation(self):
        root = Path(__file__).resolve().parents[1]
        card = rates.compile_ledger(root / "catalog/prices.yaml")
        extra = json.loads((root / "catalog/rate-overrides.json").read_text())
        card["entries"].extend(extra.get("entries", []))
        card["aliases"].update(extra.get("aliases", {}))
        if "reviewer_valuation" in extra:
            card["reviewer_valuation"] = extra["reviewer_valuation"]
        self.assertEqual(card, json.loads((root / "erg/rate_card.json").read_text()))
