"""Bounded metadata-only discovery/checkpoint evidence. Never reads transcripts."""

import collections
import hashlib
import json
import os
import time
from pathlib import Path

KEY = "source_reconciliation_v1"


def compare(
    db, paths, discovery_errors=(), discovery_error_count=0, budget=2, started_at=None, limit=100
):
    began = int(time.time())
    cpu_started = time.thread_time()
    deadline = time.monotonic() + budget
    paths = sorted(set((tool, str(path)) for tool, path in paths))
    known = {path for _, path in paths}
    columns = "path tool inode offset size mtime errors state last_seen sqlite_context".split()
    checkpoints = {
        r[0]: dict(zip(columns, r))
        for r in db.execute(
            "SELECT path,tool,inode,offset,size,mtime,errors,state,last_seen,CASE WHEN tool='opencode' THEN context END FROM history_sources"
        )
    }
    counts = collections.Counter()
    by_tool = {}
    issues = list(discovery_errors)[:limit]
    checked = 0
    digest = hashlib.sha256()

    def add(tool, state):
        counts[state] += 1
        by_tool.setdefault(tool, collections.Counter())[state] += 1

    def note(tool, path, state, **fields):
        if len(issues) < limit:
            issues.append(dict(tool=tool, path=path, state=state, **fields))

    for tool, path in paths:
        if time.monotonic() >= deadline:
            break
        checked += 1
        old = checkpoints.get(path)
        current = None
        if old and old["state"] == "consumed":
            state = "consumed"
        else:
            try:
                st = os.stat(path)
                current = [str(st.st_dev) + ":" + str(st.st_ino), st.st_size, st.st_mtime]
                if old is None:
                    state = "uncheckpointed"
                elif old["tool"] != tool:
                    state = "tool_conflict"
                elif old["errors"] or old["state"] not in ("baseline", "captured"):
                    state = (
                        "pending"
                        if old["state"] == "pending" and not old["errors"]
                        else "checkpoint_error"
                    )
                elif tool == "opencode":
                    from . import history_opencode

                    try:
                        signature = json.loads(old["sqlite_context"] or "{}").get("signature")
                    except ValueError:
                        signature = None
                    state = (
                        "checkpoint_current"
                        if signature == history_opencode.signature(Path(path))
                        else "changed"
                    )
                elif current != [old["inode"], old["size"], old["mtime"]]:
                    state = "changed"
                else:
                    state = (
                        "baseline_excluded" if old["state"] == "baseline" else "checkpoint_current"
                    )
            except FileNotFoundError:
                state = "disappeared_during_check"
            except OSError:
                state = "stat_error"
        add(tool, state)
        digest.update(
            json.dumps(
                [
                    tool,
                    path,
                    current,
                    state,
                    old
                    and [
                        old["inode"],
                        old["offset"],
                        old["size"],
                        old["mtime"],
                        old["state"],
                        old["errors"],
                    ],
                ],
                separators=(",", ":"),
            ).encode()
        )
        if state not in ("checkpoint_current", "baseline_excluded", "consumed"):
            note(
                tool,
                path,
                state,
                checkpoint_state=old["state"] if old else None,
                last_seen=old["last_seen"] if old else None,
                observed_inode=current[0] if current else None,
                observed_bytes=current[1] if current else None,
                observed_mtime=current[2] if current else None,
                checkpoint_offset=old["offset"] if old else None,
                checkpoint_bytes=old["size"] if old else None,
            )
    for tool, _ in paths[checked:]:
        add(tool, "deferred")
    historical = collections.Counter()
    unlisted = [r for path, r in checkpoints.items() if path not in known]
    examined = 0
    for row in unlisted:
        if time.monotonic() >= deadline:
            break
        examined += 1
        if row["state"] == "consumed":
            historical["consumed"] += 1
            continue
        try:
            os.stat(row["path"])
            state = "outside_current_discovery"
        except FileNotFoundError:
            state = "pruned_after_checkpoint"
        except OSError:
            state = "unavailable"
        historical[state] += 1
        if (
            state != "pruned_after_checkpoint"
            or row["errors"]
            or row["state"] not in ("captured", "baseline")
        ):
            note(
                row["tool"],
                row["path"],
                state,
                checkpoint_state=row["state"],
                checkpoint_errors=row["errors"] or 0,
            )
    historical["deferred"] = len(unlisted) - examined
    pending = sum(counts[k] for k in ("uncheckpointed", "changed", "pending", "deferred"))
    errors = (
        sum(
            counts[k]
            for k in ("checkpoint_error", "tool_conflict", "stat_error", "disappeared_during_check")
        )
        + discovery_error_count
    )
    result = dict(
        version=1,
        available=True,
        started_at=started_at or began,
        observed_at=int(time.time()),
        inventory_paths=len(paths),
        checked_paths=checked,
        inventory_sha256=hashlib.sha256(
            json.dumps(paths, separators=(",", ":")).encode()
        ).hexdigest(),
        comparison_sha256=digest.hexdigest(),
        counts=dict(counts),
        by_tool={k: dict(v) for k, v in by_tool.items()},
        pending_paths=pending,
        error_paths=errors,
        discovery_error_count=discovery_error_count,
        current_state="attention" if errors else "pending" if pending else "current",
        tracked_outside_inventory=dict(historical),
        issues=issues,
        issue_sample_limit=limit,
        elapsed_seconds=round(time.monotonic() - (deadline - budget), 4),
        cpu_seconds=round(time.thread_time() - cpu_started, 4),
        proof_scope="Metadata comparison of discovered persisted sources and their checkpoints during this interval. Baselines intentionally exclude pre-enrollment content. Directory symlinks are not traversed; producer-side omissions are not certified.",
        pruning_scope="A pruned source proves only its last observed checkpoint. A tail written and removed between checks cannot be reconstructed without separate lifecycle evidence.",
    )
    return result


def record(db, **kwargs):
    value = compare(db, **kwargs)
    with db:
        db.execute(
            "INSERT OR REPLACE INTO history_config VALUES(?,?)",
            (KEY, json.dumps(value, sort_keys=True, separators=(",", ":"))),
        )
    return value


def status(db, max_age=660):
    row = db.execute("SELECT value FROM history_config WHERE key=?", (KEY,)).fetchone()
    if not row:
        return dict(
            available=False,
            current_state="not_observed",
            reason="No background discovery/checkpoint comparison has completed",
        )
    try:
        value = json.loads(row[0])
    except (ValueError, TypeError):
        return dict(
            available=False, current_state="invalid", reason="Stored source comparison is invalid"
        )
    if not isinstance(value, dict) or value.get("version") != 1:
        return dict(
            available=False, current_state="invalid", reason="Unsupported source comparison version"
        )
    stamp = value.get("observed_at")
    now = int(time.time())
    if not isinstance(stamp, int) or isinstance(stamp, bool) or not 0 < stamp <= now + 30:
        return dict(
            available=False,
            current_state="invalid",
            reason="Stored source comparison timestamp is invalid",
        )
    age = max(0, now - stamp)
    return dict(value, age_seconds=age, stale=age > max_age)
