"""Read-only quota-summary parity and performance qualification.

python3 scripts/verify-quota-summary.py [--database PATH] [--output PATH]
No source events, credentials, plan charges or enrollment settings are changed.
"""

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from erg import account_reports, accounting, quota_projection, store

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--database", default=store.DB_PATH)
parser.add_argument("--output")
args = parser.parse_args()
proof = dict(observed_at=time.time(), runs={})
db = store.open_readonly(args.database)
try:
    db.execute("BEGIN")
    if not quota_projection.ready(db):
        raise SystemExit("Quota summary is not ready; wait for background catch-up")
    unused, root, identities = accounting.resolver(db)
    results = []
    for name, call in [
        ("incremental", lambda: quota_projection.summary(db, root)),
        ("raw", lambda: account_reports.raw_quota_summary(db, root)),
    ]:
        wall = time.monotonic()
        cpu = time.thread_time()
        value = call()
        results.append(value)
        proof["runs"][name] = dict(
            wall_seconds=time.monotonic() - wall,
            cpu_seconds=time.thread_time() - cpu,
            sha256=hashlib.sha256(
                json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        )
    proof["equal"] = results[0] == results[1]
    assert proof["equal"], "Quota summaries differ from the raw reference"
    proof["source_rows"], proof["unique_source_uids"] = db.execute(
        "SELECT count(*),count(DISTINCT uid) FROM quota_events"
    ).fetchone()
    proof["latest_windows"] = len(results[0]["quota_latest"])
    proof["reset_history"] = len(results[0]["reset_history"])
    proof["quota_counts"] = results[0]["quota_counts"]
finally:
    db.close()
if args.output:
    Path(args.output).write_text(json.dumps(proof, indent=2) + "\n")
print(json.dumps(proof, indent=2))
