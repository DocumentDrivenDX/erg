import unittest

from central.erg_central import inline
from central.erg_central.processor import cohort_values
from central.erg_central.receiver import Conflict
from erg import delivery_protocol as wire


class InlineReconstruction(unittest.TestCase):
    def items(self):
        small = wire.record_items(
            dict(record_id="small", record_revision=1, record_kind="session_event", body="abc"), 1
        )
        large = wire.record_items(
            dict(
                record_id="large",
                record_revision=1,
                record_kind="session_event",
                body="x" * wire.CHUNK_BYTES,
            ),
            2,
        )
        return small + large

    def test_mixed_partial_replay_groups_match_original_byte_for_byte(self):
        items = self.items()
        for present in (set(), {1}, {2}, {1, 2, 3}):
            pending = inline.additions(items, present)
            staged = [i for i in items if "payload_json" not in i or i["delivery_seq"] in present]
            groups, bounds = {}, []
            for i in staged:
                groups.setdefault((i["record_id"], i["record_revision"]), []).append(i)
            for (ident, revision), group in groups.items():
                bounds.append(
                    dict(
                        record_id=ident,
                        record_revision=revision,
                        n=len(group),
                        bytes=sum(len(wire.encode(i)) for i in group),
                    )
                )
            inline.check_bounds(bounds, pending)
            for i in pending:
                groups.setdefault((i["record_id"], i["record_revision"]), []).append(i)
            self.assertEqual(sum(len(g) for g in groups.values()), len(items))
            for (ident, revision), group in groups.items():
                original = [
                    i for i in items if (i["record_id"], i["record_revision"]) == (ident, revision)
                ]
                self.assertEqual(
                    wire.reconstruct(group, as_bytes=True),
                    wire.reconstruct(original, as_bytes=True),
                )

    def test_inline_additions_are_included_in_both_manifest_limits(self):
        item = self.items()[0]
        row = dict(record_id="small", record_revision=1, n=128, bytes=100)
        with self.assertRaises(Conflict):
            inline.check_bounds([row], [item])
        row.update(n=1, bytes=192 * 1024 * 1024)
        with self.assertRaises(Conflict):
            inline.check_bounds([row], [item])
        inline.check_bounds([dict(row, bytes=100)], [item])

    def test_malformed_inline_is_rejected_before_fast_path_selection(self):
        item = self.items()[0]
        item["payload_json"] += "tampered"
        envelope = dict(
            enrollment_id="fixture",
            installation_id="i",
            destination_generation="g",
            protocol_version=1,
            batch_id="00000000-0000-0000-0000-000000000001",
            items=[item],
        )
        raw = wire.encode(envelope)
        with self.assertRaises(wire.ProtocolError):
            cohort_values(
                [(raw, dict(batch_id=envelope["batch_id"], batch_sha256=wire.digest(raw)))]
            )
