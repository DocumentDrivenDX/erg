#!/usr/bin/env python3
"""Read-only conservation audit of local canonical usage and project reporting.

No native histories, credentials or archived conversation bodies are read.
Counts and aggregate digests leave the connection; full rows never do.
"""

import argparse
import hashlib
import json
import math
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from erg import (
    history,
    project_dataset,
    project_reports,
    projects,
    report_scope,
    report_sqlite,
    store,
    unified,
    work,
)


def run(database):
    db = store.open_readonly(database)
    try:
        # On the supported stock Python runtime, clear an authorizer by replacing
        # it with a permissive callback, not set_authorizer(None).
        db.set_authorizer(
            lambda action, table, column, *rest: (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_READ and table == "history_events" and column == "body"
                else sqlite3.SQLITE_OK
            )
        )
        db.execute("BEGIN")
        config = projects.snapshot(db)
        started = time.monotonic()
        cols = "fresh_in cache_read cache_write out reasoning".split()
        oracle = db.execute(
            "SELECT "
            + ",".join("coalesce(sum(" + c + "),0)" for c in cols)
            + ",count(*) FROM ("
            + unified.CALLS_SELECT
            + ")"
        ).fetchone()
        allocated = project_dataset.build(db)
        rows = [dict(zip(allocated["llm_cols"], r)) for r in allocated["llm"]]
        totals = [sum(r[c] for r in rows) for c in cols] + [sum(r["calls"] for r in rows)]
        assert all(
            math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-6) for a, b in zip(totals, oracle)
        ), "Usage allocation did not conserve canonical totals"
        cost = project_reports.report(
            db, 0, 2**63 - 1, canonical_select=lambda c: report_sqlite.calls_select(c, db=db)
        )
        assert math.isclose(
            cost["totals"]["request_equivalents"], totals[-1], rel_tol=1e-12, abs_tol=1e-6
        )
        assert math.isclose(cost["totals"]["tokens"], sum(totals[:4]), rel_tol=1e-12, abs_tol=1e-6)
        for field, index in (
            ("known_value_low_usd", "value_low_usd"),
            ("known_value_high_usd", "value_high_usd"),
            ("priced_equivalents", "priced_calls"),
            ("unpriced_equivalents", "unpriced_calls"),
        ):
            assert math.isclose(
                cost["totals"][field], sum(r[index] for r in rows), rel_tol=1e-10, abs_tol=1e-6
            ), "Cost and Usage valuation diverged"
        selected = work.effectiveness(
            db, view="work", selection=report_scope.Selection(dict(start=0, end=2**63 - 1))
        )
        assert math.isclose(selected["total_calls"], totals[-1], rel_tol=1e-12, abs_tol=1e-6)
        for field, index in (
            ("known_value_low_usd", "value_low_usd"),
            ("known_value_high_usd", "value_high_usd"),
            ("priced_calls", "priced_calls"),
            ("unpriced_calls", "unpriced_calls"),
        ):
            assert math.isclose(
                selected["valuation"][field],
                sum(r[index] for r in rows),
                rel_tol=1e-10,
                abs_tol=1e-6,
            ), "Work and Usage valuation diverged"
        page = history.scoped_sessions(db, dict(start=0, end=2**63 - 1), limit=100)
        for row in page["sessions"]:
            count = db.execute(
                "SELECT count(*) FROM history_events WHERE machine=? AND seq<=? AND ((tool=? AND session=?) OR (tool='local' AND session IN (SELECT local_session FROM history_links WHERE tool=? AND session=?)))",
                (
                    row["machine"],
                    page["through_seq"],
                    row["tool"],
                    row["session"],
                    row["tool"],
                    row["session"],
                ),
            ).fetchone()[0]
            assert row["events"] == count, "Scoped history record count diverged"
        digest = hashlib.sha256(
            json.dumps(allocated["llm"], separators=(",", ":")).encode()
        ).hexdigest()
        return dict(
            source_modules_sha256={
                name: hashlib.sha256(
                    (Path(__file__).resolve().parents[1] / "erg" / name).read_bytes()
                ).hexdigest()
                for name in (
                    "projects.py",
                    "project_scope.py",
                    "project_dataset.py",
                    "project_reports.py",
                    "report_scope.py",
                    "project_admin.py",
                    "rates.py",
                    "work.py",
                    "history.py",
                    "server.py",
                    "static/index.html",
                )
            },
            canonical_calls=oracle[-1],
            canonical_tokens=sum(oracle[:4]),
            project_revision=config["revision"],
            human_projects=len(config["projects"]),
            folders=len(allocated["folder_catalog"]),
            allocated_hourly_rows=len(rows),
            usage_costs_work_totals_and_valuation_agree=True,
            history_sampled_sessions=len(page["sessions"]),
            history_record_counts_agree=True,
            archived_body_reads_denied=True,
            allocated_rows_sha256=digest,
            wall_seconds=time.monotonic() - started,
            scope="One pinned read-only local ledger snapshot, existing retained metadata only; archived conversation body reads prohibited by SQLite authorizer; no configuration edits, native-history import, inference, model reload or central export",
            limitation="Current configured hierarchy plus separate fixture proofs for splits and temporal folder moves; not a UI visual check or a new CPU acceptance observation",
        )
    finally:
        db.close()


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
