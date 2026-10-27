#!/usr/bin/env python3
"""Reconcile published project rollups with one quiet canonical source snapshot.

Local retained metadata only. No archived conversation body reads or source writes.
"""

import argparse
import json
import math
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from erg import (
    call_projection,
    project_dataset,
    project_reports,
    project_rollups,
    report_sqlite,
    store,
)


def same(a, b, label):
    if not math.isclose(a, b, rel_tol=1e-10, abs_tol=1e-5):
        raise AssertionError((label, a, b))


def run(database, wait=1800):
    deadline = time.monotonic() + wait
    while True:
        db = store.open_readonly(database)
        db.execute("BEGIN")
        state = project_rollups.status(db)
        if (
            state["ready"]
            and not state.get("stale_policy")
            and call_projection.check(db, pending_limit=0).get("ready")
        ):
            break
        db.close()
        if time.monotonic() > deadline:
            raise RuntimeError("No quiet, fully published source boundary before deadline")
        time.sleep(5)
    started = time.monotonic()
    try:
        db.set_authorizer(
            lambda action, table, column, *rest: (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_READ and table == "history_events" and column == "body"
                else sqlite3.SQLITE_OK
            )
        )
        reference = project_dataset.build(db)
        actual = project_rollups.dataset(db)
        pending = {r[0] for r in db.execute("SELECT hour FROM project_rollup_dirty")}
        complete_actual = actual
        # A live source need not quiesce. Compare unchanged hours to the current
        # canonical snapshot; compare every published hour internally below.
        reference = dict(reference)
        actual = dict(actual)
        for kind in ("llm", "edits", "prompts", "commits"):
            reference[kind] = [r for r in reference[kind] if r[0] not in pending]
            actual[kind] = [r for r in actual[kind] if r[0] not in pending]
        for kind, cols, metrics in [
            (
                "llm",
                "llm_cols",
                (
                    "fresh_in",
                    "cache_read",
                    "cache_write",
                    "out",
                    "reasoning",
                    "calls",
                    "value_low_usd",
                    "value_high_usd",
                    "priced_calls",
                    "unpriced_calls",
                    "reported_usd",
                    "reported_calls",
                ),
            ),
            ("edits", "edit_cols", ("added", "removed", "n")),
            ("prompts", "prompt_cols", ("n", "chars", "words", "est_tokens")),
            (
                "commits",
                "commit_cols",
                (
                    "n",
                    "added_code",
                    "removed_code",
                    "added_doc",
                    "removed_doc",
                    "files",
                    "doc_files",
                ),
            ),
        ]:
            self_cols = actual[cols]
            assert self_cols == reference[cols]

            def grouped(data):
                result = {}
                for values in data[kind]:
                    row = dict(zip(self_cols, values))
                    key = json.dumps(
                        [row[c] for c in self_cols if c not in metrics], separators=(",", ":")
                    )
                    acc = result.setdefault(key, {m: 0 for m in metrics})
                    for m in metrics:
                        acc[m] += row[m] or 0
                return result

            a = grouped(actual)
            b = grouped(reference)
            assert a.keys() == b.keys(), kind + " cell identities differ"
            for key in a:
                for metric in metrics:
                    same(a[key][metric], b[key][metric], kind + " " + metric)
        expected = project_reports.report(
            db,
            0,
            2**63 - 1,
            canonical_select=lambda fields: (
                "SELECT "
                + ",".join(fields)
                + " FROM ("
                + report_sqlite.calls_select(fields, db=db)
                + ") WHERE ts/3600 NOT IN (SELECT hour FROM project_rollup_dirty)"
            ),
        )
        full_cost = project_rollups.costs(db, start=0, end=2**63 - 1, requester="all")
        fields = project_rollups.NUM
        grouped = list(
            db.execute(
                "SELECT c.human,c.folder,"
                + ",".join("SUM(c." + c + ")" for c in fields)
                + " FROM project_rollup_cost_hour c JOIN project_rollup_heads h ON h.hour=c.hour AND h.version=c.version WHERE c.hour NOT IN (SELECT hour FROM project_rollup_dirty) GROUP BY c.human,c.folder"
            )
        )
        total = {m: 0 for m in fields}
        projects = {}
        folders = {}
        for row in grouped:
            human, folder = row[:2]
            values = dict(zip(fields, row[2:]))
            if human == "":
                for m in fields:
                    total[m] += values[m]
            else:
                target = projects.setdefault(human, {m: 0 for m in fields})
                for m in fields:
                    target[m] += values[m]
                folders[(human, folder)] = dict(values, project_key=human, folder_key=folder)
        cost = dict(
            totals=total,
            projects=[dict(v, project_key=k) for k, v in projects.items()],
            folders=list(folders.values()),
        )
        llm = [dict(zip(complete_actual["llm_cols"], r)) for r in complete_actual["llm"]]
        same(
            sum(r["calls"] for r in llm),
            full_cost["totals"]["request_equivalents"],
            "all published request equivalents",
        )
        same(
            sum(sum(r[k] for k in ("fresh_in", "cache_read", "cache_write", "out")) for r in llm),
            full_cost["totals"]["tokens"],
            "all published tokens",
        )
        for a, b in [
            ("value_low_usd", "known_value_low_usd"),
            ("value_high_usd", "known_value_high_usd"),
        ]:
            same(sum(r[a] for r in llm), full_cost["totals"][b], "all published " + a)
        for project in full_cost["projects"]:
            subset = [r for r in llm if r["project"] == project["project_key"]]
            same(
                sum(r["calls"] for r in subset),
                project["request_equivalents"],
                "all published project calls",
            )
            same(
                sum(
                    sum(r[k] for k in ("fresh_in", "cache_read", "cache_write", "out"))
                    for r in subset
                ),
                project["tokens"],
                "all published project tokens",
            )
        for m in project_rollups.NUM:
            same(cost["totals"][m], expected["totals"][m], "cost " + m)
        for field, keys in [
            ("projects", ("project_key",)),
            ("folders", ("project_key", "folder_key")),
        ]:
            a = {tuple(r[k] for k in keys): r for r in cost[field]}
            b = {tuple(r[k] for k in keys): r for r in expected[field]}
            assert a.keys() == b.keys(), field + " identities differ"
            for key in a:
                for m in project_rollups.NUM:
                    same(a[key][m], b[key][m], field + " " + m)
        # Activate a strict source-denying authorizer and exercise the exact
        # persistent read paths used by the deployed reporter after parity.
        forbidden = {
            "llm",
            "call_winner_rows",
            "prompts",
            "edits",
            "commits",
            "project_contexts",
            "work_contexts",
            "history_events",
        }
        db.set_authorizer(
            lambda action, table, column, *rest: (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_READ and table in forbidden
                else sqlite3.SQLITE_OK
            )
        )
        _direct = project_rollups.dataset(db)
        project_rollups.costs(db, start=0, end=2**63 - 1)
        return dict(
            pending_hours_excluded_from_current_source_comparison=len(pending),
            stable_reference_requests=cost["totals"]["request_equivalents"],
            all_published_totals_reconcile=True,
            project_revision=actual["project_revision"],
            human_projects=len(actual["project_catalog"]),
            request_equivalents=full_cost["totals"]["request_equivalents"],
            tokens=full_cost["totals"]["tokens"],
            reference_value_low_usd=full_cost["totals"]["known_value_low_usd"],
            reference_value_high_usd=full_cost["totals"]["known_value_high_usd"],
            usage_activity_cells_match_reference=True,
            project_folder_cost_totals_match_reference=True,
            report_reads_deny_source_tables=True,
            archived_body_reads_denied=True,
            materialized_hours=db.execute("SELECT count(*) FROM project_rollup_heads").fetchone()[
                0
            ],
            wall_seconds=time.monotonic() - started,
            scope="One pinned canonical boundary: unchanged hours compared against raw reference; all published Usage/Costs totals reconciled. Pending hours are explicit freshness lag, not claimed current. Existing retained metadata only; no capture, source mutations, inference or export",
        )
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default=store.DB_PATH)
    parser.add_argument("--output", required=True)
    parser.add_argument("--wait", type=float, default=1800)
    args = parser.parse_args()
    if os.path.lexists(args.output):
        raise ValueError("Choose a new evidence path")
    result = run(args.database, args.wait)
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(result, f, indent=2)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
