"""Qualify worker-local preparation reuse in one read-only source snapshot.

Compare complete metadata and current-hour work/account decisions with independent
fresh preparation. Report counts/timing only; never print identifiers or payloads.
This is a preparation check, not an end-to-end throughput benchmark.
"""

import argparse
import contextlib
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from erg import call_projection, project_rollups, quota_projection, report_evidence, store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default=store.DB_PATH)
    parser.add_argument("--start", type=int, default=int(time.time()) // 3600 * 3600)
    parser.add_argument("--end", type=int, default=2**62)
    parser.add_argument("--output")
    args = parser.parse_args()
    if args.start < 0 or args.end <= args.start:
        parser.error("require 0 <= start < end")
    proof = dict(
        observed_at=time.time(),
        scope="One pinned source snapshot; complete metadata plus selected canonical-call work/account decisions; no writes or exports",
        start=args.start,
        end=args.end,
        runs={},
    )
    with contextlib.closing(store.open_readonly(args.database)) as db:
        db.execute("BEGIN")
        versions = project_rollups.state(db)
        if "preparation_generation" not in versions:
            raise SystemExit("Preparation counters have not been installed")
        if not call_projection.check(db, pending_limit=0).get("ready"):
            raise SystemExit("Canonical winners are pending; retry after catch-up")
        if not quota_projection.ready(db):
            raise SystemExit("Quota projection is pending; retry after catch-up")
        cache = report_evidence.PreparationCache()
        prepared = {}
        for name, current in [("fresh", None), ("cold", cache), ("reused", cache)]:
            wall = time.monotonic()
            cpu = time.thread_time()
            prepared[name] = report_evidence.prepare(db, cache=current, versions=versions)
            proof["runs"][name] = dict(
                cpu_seconds=time.thread_time() - cpu, wall_seconds=time.monotonic() - wall
            )

        def metadata(value):
            return json.dumps(
                {k: v for k, v in value[3].items() if k != "generated"},
                sort_keys=True,
                separators=(",", ":"),
            )

        proof["metadata_equal"] = (
            metadata(prepared["fresh"])
            == metadata(prepared["cold"])
            == metadata(prepared["reused"])
        )
        assert proof["metadata_equal"], "Prepared metadata differs"
        checked = 0
        for machine, tool, session, stamp, uid in db.execute(
            "SELECT machine,tool,session,ts,uid FROM call_winner_rows WHERE ts>=? AND ts<?",
            (args.start, args.end),
        ):
            reference = prepared["fresh"]
            actual = prepared["reused"]
            assert reference[0](machine, tool, session, stamp) == actual[0](
                machine, tool, session, stamp
            ), "Work decision differs"
            assert reference[1](tool, session, stamp, request_uid=uid) == actual[1](
                tool, session, stamp, request_uid=uid
            ), "Account decision differs"
            checked += 1
        if not checked:
            raise SystemExit("No canonical calls in the selected interval")
        proof.update(calls_checked=checked, decisions_equal=True, cache=cache.stats())
    if args.output:
        Path(args.output).write_text(json.dumps(proof, indent=2) + "\n")
    print(json.dumps(proof, indent=2))


if __name__ == "__main__":
    main()
