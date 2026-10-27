#!/usr/bin/env python3
"""Audit incremental canonical winners in a private temporary usage-only ledger.

The source is opened read-only and pinned while copying raw usage metadata. No
session bodies, credentials or native histories are read. Dense maintenance is a
diagnostic workload, not service cadence or whole-tool CPU acceptance. A new
private output file contains counts, timing, code hash and row digests only.
"""

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from erg import call_projection as cache
from erg import store, unified


def run(database):
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="erg-full-winner-audit-") as directory:
        db = store.connect(Path(directory) / "ledger")
        with db:
            cache.disarm(db)
        copied = 0
        source_digest = hashlib.sha256()
        copy_cpu = time.process_time()
        with store.open_readonly(database) as source:
            source.execute("BEGIN")
            cols = [r[1] for r in source.execute("PRAGMA table_info(llm)")]
            names = ",".join(cache.quoted(c) for c in cols)
            cursor = source.execute("SELECT rowid," + names + " FROM llm ORDER BY rowid")
            insert = (
                "INSERT INTO llm(rowid,"
                + names
                + ") VALUES("
                + ",".join("?" for _ in range(len(cols) + 1))
                + ")"
            )
            while True:
                rows = cursor.fetchmany(2000)
                if not rows:
                    break
                db.executemany(insert, rows)
                copied += len(rows)
                for row in rows:
                    source_digest.update(json.dumps(row, separators=(",", ":")).encode())
                    source_digest.update(b"\n")
            source.rollback()
        db.commit()
        copy_cpu = time.process_time() - copy_cpu
        copied_digest = hashlib.sha256()
        for row in db.execute("SELECT rowid," + names + " FROM llm ORDER BY rowid"):
            copied_digest.update(json.dumps(row, separators=(",", ":")).encode())
            copied_digest.update(b"\n")
        assert source_digest.digest() == copied_digest.digest(), "Raw metadata copy differed"
        cache.initialize(db)
        db.commit()
        cycles = processed = 0
        peak = 0
        cpu = time.process_time()
        print(
            json.dumps(dict(state="maintaining_private_snapshot", source_rows=copied)), flush=True
        )
        while True:
            report = cache.run(db)
            cycles += 1
            processed += report["processed"]
            peak = max(peak, report.get("cpu_seconds", 0))
            if report.get("seed_complete") and not report.get("pending"):
                break
            if not report.get("available") or report.get("reason") not in (
                "seeding",
                "pending",
                "",
            ):
                raise RuntimeError("Unexpected maintenance state")
            if cycles > copied * 2 + 10:
                raise RuntimeError("Maintenance failed to converge")
        maintenance_cpu = time.process_time() - cpu
        db.execute("BEGIN")
        fields = [r[1] for r in db.execute("PRAGMA table_info(calls)")]
        cached = cache.cached_select(db, fields)
        assert cached, "Cache cannot prove readiness"

        def digest(sql):
            h = hashlib.sha256()
            n = 0
            for row in db.execute(
                "SELECT " + ",".join(fields) + " FROM (" + sql + ") ORDER BY tool,uid,machine"
            ):
                h.update(json.dumps(row, separators=(",", ":")).encode())
                h.update(b"\n")
                n += 1
            return n, h.hexdigest()

        raw = digest(unified.CALLS_SELECT)
        derived = digest(cached)
        assert raw == derived, "Canonical parity failed"
        result = dict(
            source_rows=copied,
            raw_copy_digest_identical=True,
            canonical_rows=raw[0],
            canonical_sha256=raw[1],
            all_rows_and_columns_identical=True,
            maintenance_cycles=cycles,
            processed_partitions=processed,
            copy_cpu_seconds=copy_cpu,
            maintenance_cpu_seconds=maintenance_cpu,
            max_cycle_cpu_seconds=peak,
            wall_seconds=time.monotonic() - started,
            final_cache_ready=report["ready"],
            module_sha256=hashlib.sha256(Path(cache.__file__).read_bytes()).hexdigest(),
            scope="Full raw-usage snapshot copied from a read-only live ledger into private temporary storage; dense diagnostic maintenance, not real service cadence; no native-history/body import, live cache/schema/runtime change or central export",
            whole_tool_cpu_acceptance=False,
        )
        db.close()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default=store.DB_PATH)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if os.path.lexists(args.output):
        raise ValueError("Choose a new evidence path")
    result = run(args.database)
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as output:
        json.dump(result, output, indent=2)
        output.write("\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
