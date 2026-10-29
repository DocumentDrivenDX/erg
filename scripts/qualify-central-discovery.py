"""Runtime-identity qualification; writes only an explicit synthetic namespace.

The operator provides an immutable archive digest and a fresh qualification
prefix. Production discovery is metadata-only and publishes no terminal receipt.
"""

import hashlib
import json
import sys
import time
from pathlib import Path


def main(config):
    archive = Path("/Workspace" + config["archive"])
    if hashlib.sha256(archive.read_bytes()).hexdigest() != config["sha256"]:
        raise ValueError("qualification archive digest mismatch")
    root = Path(config["qualification"])
    if (
        not str(root).startswith("/Volumes/erg/ingest/control/qualification/discovery_")
        or root.exists()
    ):
        raise ValueError("fresh synthetic qualification prefix required")
    sys.path.insert(0, str(archive))
    from erg_central import discovery, processor
    from erg_central.receiver import namespace

    spool, control = root / "spool", root / "control"
    index = dict(version=1, scopes={})
    for n in range(11):
        receipt = dict(
            enrollment_id="synthetic-discovery",
            destination_generation="test",
            batch_id="batch-%02d" % n,
            batch_sha256=hashlib.sha256(str(n).encode()).hexdigest(),
            accepted_delivery_seqs=[n + 1],
        )
        processor.atomic_control(
            spool / namespace(receipt) / "batches" / receipt["batch_id"] / "accepted.json", receipt
        )
    original_size = discovery.PAGE_SIZE
    discovery.PAGE_SIZE = 3
    seen = set()
    pages = 0
    for n in range(8):
        rows, metrics = discovery.paged_markers(
            spool, control, 2, time.monotonic(), 50, index, processor.atomic_control
        )
        pages += metrics["pages"]
        if n == 0:
            repeated, _ = discovery.paged_markers(
                spool, control, 2, time.monotonic(), 50, index, processor.atomic_control
            )
            assert repeated == rows, "unacknowledged candidates must retry"
        for _, receipt in rows:
            assert receipt["batch_id"] not in seen, "completed batch repeated"
            seen.add(receipt["batch_id"])
        processor.index_staged(index, control, [r for _, r in rows])
        if metrics["sweep_complete"]:
            break
    assert len(seen) == 11 and pages >= 7, (len(seen), pages)
    loaded = processor.staged_index(control)
    assert loaded == index, "shard restart lost completions"
    discovery.PAGE_SIZE = original_size
    completed = processor.staged_index(config["production_control"])
    start = time.monotonic()
    rows, metrics = discovery.paged_markers(
        Path(config["production_spool"]),
        root / "production-readonly-scan",
        512,
        start,
        50,
        completed,
        processor.atomic_control,
    )
    print(
        json.dumps(
            dict(
                erg_discovery_qualification=dict(
                    synthetic_batches=len(seen),
                    synthetic_pages=pages,
                    restart_index_equal=True,
                    production_metadata_only=True,
                    production_discovery=metrics,
                    production_scan_seconds=time.monotonic() - start,
                    production_candidates=len(rows),
                )
            ),
            sort_keys=True,
        )
    )
