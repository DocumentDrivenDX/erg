import copy
import importlib.util
import unittest
from pathlib import Path

from erg import delivery_protocol as wire

spec = importlib.util.spec_from_file_location(
    "synthetic_qualification",
    Path(__file__).resolve().parents[1] / "scripts/qualification/synthetic-delivery.py",
)
qualification = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qualification)


class ReceiptQualificationTests(unittest.TestCase):
    def fixture(self):
        identity = dict(
            enrollment_id="synthetic", installation_id="install", destination_generation="gen"
        )
        batches = []
        bodies = {}
        pages = []
        for seq in (1, 2):
            raw = wire.batch(
                identity,
                wire.record_items(
                    dict(
                        record_id="record" + str(seq),
                        record_revision=1,
                        record_kind="session_event",
                        body={},
                    ),
                    seq,
                ),
            )
            value = wire.decode(raw)
            ident = value["batch_id"]
            sha = wire.digest(raw)
            batches.append(dict(batch_id=ident, sha256=sha))
            bodies[ident] = raw
            receipt = dict(
                protocol_version=1,
                state="accepted",
                enrollment_id="synthetic",
                destination_generation="gen",
                batch_id=ident,
                batch_sha256=sha,
                accepted_delivery_seqs=[seq],
            )
            pages.append(
                dict(
                    enrollment_id="synthetic",
                    destination_generation="gen",
                    materialized_through=2,
                    projected_through=2,
                    publication_state="published",
                    receipts=[receipt],
                    next_cursor="page2" if seq == 1 else None,
                )
            )
        return dict(identity=identity, batches=batches, delivery_items=2), bodies, pages

    def api(self, pages):
        class API:
            def status(self, **kwargs):
                return pages[1] if kwargs.get("cursor") else pages[0]

        return API()

    def test_all_receipts_must_match_pinned_bodies_across_pages(self):
        manifest, bodies, pages = self.fixture()
        _, observed, proof = qualification.verify_receipt_snapshot(
            self.api(pages), manifest, bodies
        )
        self.assertEqual(len(observed), 2)
        self.assertEqual(proof["delivery_items"], 2)

    def test_missing_duplicate_wrong_digest_scope_and_cursor_fail_closed(self):
        manifest, bodies, pages = self.fixture()
        bads = []
        missing = copy.deepcopy(pages)
        missing[0]["next_cursor"] = None
        bads.append(missing)
        duplicate = copy.deepcopy(pages)
        duplicate[1]["receipts"] = duplicate[0]["receipts"]
        bads.append(duplicate)
        digest = copy.deepcopy(pages)
        digest[1]["receipts"][0]["batch_sha256"] = "0" * 64
        bads.append(digest)
        scope = copy.deepcopy(pages)
        scope[1]["enrollment_id"] = "other"
        bads.append(scope)
        cursor = copy.deepcopy(pages)
        cursor[1]["next_cursor"] = "page2"
        bads.append(cursor)
        pending = copy.deepcopy(pages)
        pending[1]["receipts"][0]["state"] = "pending"
        bads.append(pending)
        boolean = copy.deepcopy(pages)
        boolean[0]["receipts"][0]["accepted_delivery_seqs"] = [True]
        bads.append(boolean)
        for bad in bads:
            with self.assertRaises(ValueError):
                qualification.verify_receipt_snapshot(self.api(bad), manifest, bodies)
