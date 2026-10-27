"""Generated protocol inputs exercise invariants under an explicit recorded seed."""

import json
import unittest

from hypothesis import given, seed, settings
from hypothesis import strategies as st

from erg import delivery_protocol as wire


class ProtocolProperties(unittest.TestCase):
    @seed(20261027)
    @settings(max_examples=120, deadline=None)
    @given(st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=4096))
    def test_unicode_round_trip_and_replay_identity(self, content):
        record = dict(
            record_id="property-record",
            record_revision=1,
            record_kind="session_event",
            tool="pi",
            session="property-session",
            body=dict(content=content),
        )
        items = wire.record_items(record, 1)
        self.assertEqual(wire.reconstruct(list(reversed(items))), record)
        self.assertEqual(wire.record_items(record, 1), items)
        json.dumps(items, allow_nan=False)
