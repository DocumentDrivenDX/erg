import unittest

from central.erg_central.processor import batch_already_staged


class StagingProgress(unittest.TestCase):
    def test_staged_out_of_order_batch_does_not_starve_the_missing_prefix(self):
        self.assertTrue(
            batch_already_staged(
                {"items_staged": True, "materialized_through": 0}, [{"delivery_seq": 5000}]
            )
        )

    def test_unstaged_batch_does_not_ack_or_skip_from_high_water_only(self):
        self.assertFalse(
            batch_already_staged(
                {"items_staged": False, "materialized_through": 0, "observed_high_water": 9000},
                [{"delivery_seq": 1}],
            )
        )
        self.assertFalse(
            batch_already_staged({"materialized_through": 0}, [{"delivery_seq": 5000}])
        )


class ReceiptScan(unittest.TestCase):
    def test_pending_order_uses_sequence_and_skips_staged_body_reads(self):
        import json
        import tempfile
        import time
        from pathlib import Path

        from central.erg_central.processor import pending_markers
        from central.erg_central.receiver import namespace

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "spool"
            control = Path(directory) / "control"
            for seq, name, staged in [(100, "a", False), (1, "z", False), (2, "b", True)]:
                receipt = dict(
                    enrollment_id="test",
                    destination_generation="gen",
                    batch_id=name,
                    batch_sha256="a" * 64,
                    accepted_delivery_seqs=[seq],
                )
                p = root / name
                p.mkdir(parents=True)
                (p / "accepted.json").write_text(json.dumps(receipt))
                # No body exists: metadata scanning must not read it, including staged data.
                if staged:
                    out = control / namespace(receipt) / "batches" / (name + ".json")
                    out.parent.mkdir(parents=True)
                    out.write_text(json.dumps(dict(batch_sha256="a" * 64, items_staged=True)))
            result = pending_markers(root, control, 512, time.monotonic(), 10)
            self.assertEqual([r["accepted_delivery_seqs"][0] for _, r in result], [1, 100])


class ReportingDisposition(unittest.TestCase):
    def test_only_explicit_raw_reporting_contract_receives_terminal_raw_disposition(self):
        from central.erg_central.processor import raw_disposition

        value = dict(
            record_kind="usage_evidence",
            kind="usage_evidence",
            origin="derived_reporting",
            body={"schema_version": 1},
        )
        self.assertEqual(raw_disposition(value), "not_applicable:raw-reporting-v1")
        self.assertIsNone(raw_disposition(dict(value, origin="native")))
        self.assertIsNone(raw_disposition(dict(value, body={"schema_version": 2})))
        self.assertEqual(
            raw_disposition({"record_kind": "session_event"}), "not_applicable:raw-session-v1"
        )


class CohortValidation(unittest.TestCase):
    def batches(self):
        from erg import delivery_protocol as wire

        identity = dict(enrollment_id="e", installation_id="i", destination_generation="g")
        result = []
        for n in (1, 2):
            payload = dict(
                record_id="r" + str(n),
                record_revision=1,
                record_kind="session_event",
                body={"value": n},
            )
            raw = wire.batch(identity, wire.record_items(payload, n))
            value = wire.decode(raw)
            result.append((raw, dict(batch_id=value["batch_id"], batch_sha256=wire.digest(raw))))
        return result

    def test_cohort_preserves_original_receipts_and_batch_origin(self):
        from central.erg_central.processor import cohort_values

        batches = self.batches()
        value, receipts, origins = cohort_values(batches)
        self.assertEqual([i["delivery_seq"] for i in value["items"]], [1, 2])
        self.assertEqual(receipts, [r for _, r in batches])
        self.assertEqual(origins, {1: receipts[0]["batch_id"], 2: receipts[1]["batch_id"]})
        replay, _, _ = cohort_values(batches + batches)
        self.assertEqual(len(replay["items"]), 2)

    def test_cohort_rejects_cross_generation_and_conflicting_sequence(self):
        from central.erg_central.processor import cohort_values
        from central.erg_central.receiver import Conflict
        from erg import delivery_protocol as wire

        batches = self.batches()
        value = wire.decode(batches[1][0])
        value["destination_generation"] = "other"
        raw = wire.encode(value)
        with self.assertRaises(Conflict):
            cohort_values(
                [batches[0], (raw, dict(batch_id=value["batch_id"], batch_sha256=wire.digest(raw)))]
            )
        value = wire.decode(batches[1][0])
        value["items"][0]["delivery_seq"] = 1
        raw = wire.encode(value)
        with self.assertRaises(Conflict):
            cohort_values(
                [batches[0], (raw, dict(batch_id=value["batch_id"], batch_sha256=wire.digest(raw)))]
            )
        with self.assertRaises(wire.ProtocolError):
            cohort_values(batches * 129)

    def test_duplicate_items_still_respect_encoded_byte_budget(self):
        from unittest.mock import patch

        from central.erg_central import processor
        from erg import delivery_protocol as wire

        batches = self.batches()
        with patch.object(processor, "MAX_COHORT_BYTES", len(batches[0][0])):
            with self.assertRaises(wire.ProtocolError):
                processor.cohort_values(batches)


class StagedIndex(unittest.TestCase):
    def test_terminal_index_prunes_completed_directories_without_reopening_markers(self):
        import json
        import tempfile
        import time
        from pathlib import Path
        from unittest.mock import patch

        from central.erg_central import processor
        from central.erg_central.receiver import namespace

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "spool"
            control = Path(temp) / "control"
            identity = dict(enrollment_id="e", destination_generation="g")
            space = namespace(identity)
            for ident, staged in [("done", True), ("pending", False)]:
                p = root / space / "batches" / ident
                p.mkdir(parents=True)
                (p / "accepted.json").write_text(
                    json.dumps(
                        dict(
                            identity,
                            batch_id=ident,
                            batch_sha256="a" * 64,
                            accepted_delivery_seqs=[1],
                        )
                    )
                )
                q = control / space / "batches" / (ident + ".json")
                q.parent.mkdir(parents=True, exist_ok=True)
                q.write_text(json.dumps(dict(items_staged=staged, batch_sha256="a" * 64)))
            index = processor.staged_index(control)
            self.assertEqual(set(index["scopes"][space]), {"done"})
            original = Path.read_bytes

            def guarded(path):
                if (
                    path == root / space / "batches" / "done" / "accepted.json"
                    or path == control / space / "batches" / "done.json"
                ):
                    raise AssertionError("completed batch reread")
                return original(path)

            with patch.object(Path, "read_bytes", guarded):
                self.assertEqual(processor.staged_index(control), index)
                pending = processor.pending_markers(
                    root, control, 512, time.monotonic(), 30, staged=index
                )
            self.assertEqual([r["batch_id"] for _, r in pending], ["pending"])

    def test_discovery_time_does_not_discard_already_found_work(self):
        import json
        import tempfile
        from pathlib import Path
        from unittest.mock import Mock, patch

        from central.erg_central import processor
        from erg import delivery_protocol as wire

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "spool"
            root.mkdir()
            control = Path(temp) / "control"
            raw, receipt = CohortValidation().batches()[0]
            _value = wire.decode(raw)
            receipt.update(
                enrollment_id="e", destination_generation="g", accepted_delivery_seqs=[1]
            )
            marker = root / "accepted.json"
            marker.write_text(json.dumps(receipt))
            (root / "body.json").write_bytes(raw)
            now = [0.0]

            def discover(*args, **kwargs):
                now[0] = 300.0
                return [(marker, receipt)]

            runner = Mock()
            runner.process_many.return_value = dict(
                state="canonical", cohort_batches=1, cohort_items=1
            )
            zero = Mock()
            zero.metrics = {}
            with patch.object(processor, "Processor", return_value=runner), patch.object(
                processor, "pending_markers", side_effect=discover
            ), patch.object(processor.time, "monotonic", side_effect=lambda: now[0]):
                result = processor.run(Mock(), zero, root, control, "erg")
            self.assertEqual(result["attempted_batches"], 1)
            runner.process_many.assert_called_once()
            self.assertEqual(
                processor.staged_index(control)["scopes"][processor.namespace(receipt)][
                    receipt["batch_id"]
                ],
                receipt["batch_sha256"],
            )

    def test_invalid_terminal_index_fails_closed(self):
        import json
        import tempfile
        from pathlib import Path

        from central.erg_central import processor
        from central.erg_central.receiver import Conflict

        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / "processor-staged-index.json").write_text(
                json.dumps(dict(version=1, scopes={"../outside": {}}))
            )
            with self.assertRaises(Conflict):
                processor.staged_index(temp)
