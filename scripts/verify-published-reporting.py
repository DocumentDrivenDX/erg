"""Read-only qualification of completed reporting hours against raw canonical facts.

Defaults to all history before the active hour. This intentionally performs an
expensive reference calculation; it never publishes, captures or exports data.
"""

import argparse
import contextlib
import json
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from erg import (
    monetary,
    project_reports,
    project_rollups,
    report_evidence,
    report_scope,
    report_sqlite,
    store,
)

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--database", default=store.DB_PATH)
parser.add_argument("--start", type=int, default=0)
parser.add_argument("--end", type=int, default=int(time.time()) // 3600 * 3600)
parser.add_argument("--output")
args = parser.parse_args()
if args.start % 3600 or args.end % 3600:
    parser.error("--start and --end must be whole-hour boundaries")
proof = dict(observed_at=time.time(), start=args.start, end=args.end, runs={})


def check(a, b, label):
    if not math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-6):
        raise AssertionError(label + " differs: " + str(a) + " / " + str(b))


with contextlib.closing(store.open_readonly(args.database)) as db:
    db.execute("BEGIN")
    if db.execute(
        "SELECT 1 FROM project_rollup_dirty WHERE hour*3600<? AND (hour+1)*3600>? LIMIT 1",
        (args.end, args.start),
    ).fetchone():
        raise SystemExit("Selected hours are still queued; wait for reporting catch-up")
    scope = dict(start=args.start, end=args.end)
    selection = report_scope.Selection(scope)
    wall = time.monotonic()
    cpu = time.thread_time()
    actual = project_rollups.costs(db, **scope)
    assert actual.get("available"), "Reporting has not been published"
    proof["runs"]["published"] = dict(
        wall_seconds=time.monotonic() - wall, cpu_seconds=time.thread_time() - cpu
    )
    wall = time.monotonic()
    cpu = time.thread_time()
    expected = project_reports.report(
        db, canonical_select=lambda cols: report_sqlite.calls_select(cols), **scope
    )
    proof["runs"]["reference"] = dict(
        wall_seconds=time.monotonic() - wall, cpu_seconds=time.thread_time() - cpu
    )
    for metric in project_rollups.NUM:
        check(actual["totals"][metric], expected["totals"][metric], "totals " + metric)
    for field, identity in [
        ("projects", lambda r: r["project_key"]),
        ("folders", lambda r: (r["project_key"], r["folder_key"])),
        ("sessions", lambda r: (r["machine"], r["tool"], r["session"])),
    ]:
        left = {identity(r): r for r in actual[field]}
        right = {identity(r): r for r in expected[field]}
        assert left.keys() == right.keys(), field + " identity sets differ"
        for key in left:
            for metric in project_rollups.NUM:
                check(left[key][metric], right[key][metric], field + " " + metric)
        proof[field + "_checked"] = len(left)
    print(
        "Canonical totals, project/folder rows and top sessions match.", file=sys.stderr, flush=True
    )
    data = project_rollups.dataset(db, since=args.start)
    columns = data["llm_cols"]
    index = {c: i for i, c in enumerate(columns)}
    # Usage payloads contain only project-weighted portions, including the
    # explicit unassigned remainder; summing all portions recovers the total.
    rows = [r for r in data["llm"] if args.start <= r[index["hour"]] * 3600 < args.end]
    usage_value = sum(r[index["value_low_usd"]] for r in rows)
    usage_tokens = sum(
        sum(r[index[k]] or 0 for k in ("fresh_in", "cache_read", "cache_write", "out"))
        for r in rows
    )
    check(usage_tokens, actual["totals"]["tokens"], "usage tokens")
    check(usage_value, actual["totals"]["known_value_low_usd"], "usage value")
    values = {"usage": usage_value, "costs": actual["totals"]["known_value_low_usd"]}
    for view in ("work", "accounts"):
        report = report_evidence.report(db, view, selection)
        assert report["available"]
        check(report["total_calls"], actual["totals"]["request_equivalents"], view + " calls")
        values[view] = report["valuation"]["known_value_low_usd"]
        check(values[view], values["costs"], view + " value")
    money = monetary.report(db, selection)
    check(money["totals"]["rack_rate_low_usd"], values["costs"], "monetary value")
    proof.update(
        equal=True,
        totals={k: actual["totals"][k] for k in project_rollups.NUM},
        session_count=actual["session_count"],
        rack_rate_values=values,
        effective_cost_usd=money["totals"]["effective_cost_usd"],
        project_revision=actual["revision"],
    )
if args.output:
    Path(args.output).write_text(json.dumps(proof, indent=2) + "\n")
print(json.dumps(proof, indent=2))
