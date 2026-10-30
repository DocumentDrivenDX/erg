"""Export synthetic golden inputs for UMF/LDP qualification; no cloud operations.

This checks only the shared Python transform against the SQLite reference.
Actual LDP parity, runtime faults, reporting and capacity remain separate gates.
"""

import argparse
import hashlib
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from central.erg_central.canonical import Canonical  # noqa: E402
from central.erg_central.ldp_codec import (  # noqa: E402
    certify_prefix,
    reconstruct_revision,
    unique_items,
)
from central.erg_central.receiver import namespace  # noqa: E402
from central.ldp.qualification import IDENTITY, fixture, raw_rows  # noqa: E402
from central.ldp.reporting_fixture import qualify  # noqa: E402
from erg import delivery_protocol as wire  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    items = fixture()
    rows = raw_rows(items)
    reference = Canonical(sqlite3.connect(":memory:"))
    # Two updates split a chunked revision; then an exact full replay.
    phases = (items[:-1], items[-1:], items)
    started = time.monotonic()
    for page in phases:
        reference.apply(dict(IDENTITY, items=page))
    reference_seconds = time.monotonic() - started
    groups = {}
    started = time.monotonic()
    for item in unique_items(rows + rows, IDENTITY):
        groups.setdefault((item["record_id"], item["record_revision"]), []).append(
            wire.encode(item).decode()
        )
    facts = {key: reconstruct_revision(group) for key, group in groups.items()}
    codec_seconds = time.monotonic() - started
    expected = []
    heads = {}
    for ident, revision, sha, payload in reference.db.execute(
        "SELECT id,revision,sha256,payload FROM canonical_revisions WHERE namespace=? ORDER BY id,revision",
        (namespace(IDENTITY),),
    ):
        actual = facts[ident, revision]
        assert actual["state"] == "complete"
        assert (actual["record_sha256"], actual["payload_json"].encode()) == (sha, payload)
        record = dict(
            record_id=ident,
            record_revision=revision,
            record_sha256=sha,
            payload_json=payload.decode(),
            disposition=actual["disposition"],
        )
        expected.append(record)
        if ident not in heads or heads[ident]["record_revision"] < revision:
            heads[ident] = record
    assert len(facts) == len(expected)
    snapshots = []
    seen = []
    for phase in phases:
        seen.extend(raw_rows(phase))
        scoped = unique_items(seen, IDENTITY)
        partial_groups = {}
        for item in scoped:
            partial_groups.setdefault((item["record_id"], item["record_revision"]), []).append(
                wire.encode(item).decode()
            )
        partial_facts = {key: reconstruct_revision(group) for key, group in partial_groups.items()}
        cursors = certify_prefix(
            IDENTITY, dict(materialized_through=0, projected_through=0), seen, partial_facts
        )
        snapshots.append(
            dict(
                unique_items=len(scoped),
                complete_revisions=sum(f["state"] == "complete" for f in partial_facts.values()),
                pending_revisions=sum(f["state"] == "pending" for f in partial_facts.values()),
                **cursors,
            )
        )
    reporting = qualify(args.output)
    files = {
        "raw.jsonl": b"\n".join(wire.encode(r) for r in rows) + b"\n",
        "expected-revisions.jsonl": b"\n".join(wire.encode(r) for r in expected) + b"\n",
        "expected-heads.jsonl": b"\n".join(wire.encode(heads[k]) for k in sorted(heads)) + b"\n",
        "expected-reporting.json": wire.encode(reporting) + b"\n",
    }
    for name, raw in files.items():
        (args.output / name).write_bytes(raw)
    report = dict(
        scope="synthetic-only local codec/reference parity; not LDP runtime qualification",
        identity=IDENTITY,
        unique_items=len(items),
        canonical_revisions=len(expected),
        wire_bytes=sum(len(wire.encode(i)) for i in items),
        exact_hash_and_payload_parity=True,
        local_reporting_replay_parity=True,
        expected_update_snapshots=snapshots,
        updates=[dict(sequences=[i["delivery_seq"] for i in page]) for page in phases],
        fixture_sha256={name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()},
        local_reference_seconds=reference_seconds,
        local_codec_seconds=codec_seconds,
        live_ldp_qualified=False,
        production_cutover_allowed=False,
    )
    (args.output / "manifest.json").write_bytes(wire.encode(report) + b"\n")
    print(
        json.dumps({k: v for k, v in report.items() if k not in ("updates", "identity")}, indent=2)
    )


if __name__ == "__main__":
    main()
