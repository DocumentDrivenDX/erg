"""Valid synthetic reporting records produced by the existing observation codec."""

import json
from pathlib import Path
from unittest.mock import patch

from erg import (
    call_projection,
    project_rollups,
    projects,
    report_delivery,
    report_evidence,
    report_scope,
    store,
    unified,
)
from erg import delivery_protocol as wire

MACHINE = "synthetic-ldp-host"
START = 1700002800
CARD = dict(
    entries=[
        dict(
            model="synthetic-ldp-model",
            as_of="2026-10-28",
            source="fabricated qualification rates, not provider prices",
            input=2,
            output=4,
            cache_read=0.2,
            cache_write_5m=3,
            cache_write_1h=6,
        )
    ]
)


def events():
    rows = []
    for n, tool in enumerate(("claude", "codex", "pi")):
        base = dict(tool=tool, session="synthetic-" + tool, project="synthetic-app")
        rows.extend(
            [
                dict(
                    base,
                    t="llm",
                    uid="call-" + tool,
                    ts=START + 100 + n,
                    model="synthetic-ldp-model",
                    fresh_in=100,
                    cache_read=10,
                    out=20,
                    usage_kind="observed",
                ),
                dict(
                    base,
                    t="prompt",
                    uid="human-" + tool,
                    ts=START + 90 + n,
                    chars=40,
                    words=8,
                    origin="human",
                    provenance="native_user_message",
                ),
                dict(
                    base,
                    t="work_context",
                    uid="context-" + tool,
                    ts=START + 50,
                    repo="https://github.com/synthetic-ldp/fixture",
                    branch="synthetic",
                    scope_id="scope-" + tool,
                    capture_version=5,
                ),
                dict(
                    base,
                    t="work_evidence",
                    uid="work-" + tool,
                    ts=START + 120,
                    repo="https://github.com/synthetic-ldp/fixture",
                    scope_id="scope-" + tool,
                    sha="a" * 40,
                    provenance="native_commit_result",
                    capture_version=5,
                ),
                dict(
                    base,
                    t="identity",
                    uid="account-" + tool,
                    ts=START + 40,
                    account_key="synthetic-account",
                    provider="synthetic",
                    plan="fixture",
                    provenance="native_metadata",
                ),
                dict(
                    base,
                    t="quota",
                    uid="quota-" + tool,
                    ts=START + 130,
                    account_key="synthetic-account",
                    provider="synthetic",
                    event="snapshot",
                    window="weekly",
                    used_percent=25,
                    resets_at=START + 604800,
                ),
            ]
        )
    rows.extend(
        [
            dict(
                t="pull_request",
                uid="synthetic-pr",
                ts=START + 140,
                repo="https://github.com/synthetic-ldp/fixture",
                number=1,
                url="https://github.com/synthetic-ldp/fixture/pull/1",
                title="Synthetic PR",
                state="merged",
                merged_ts=START + 140,
            ),
            dict(
                t="pr_commit",
                uid="synthetic-pr-commit",
                ts=START + 140,
                pr_url="https://github.com/synthetic-ldp/fixture/pull/1",
                sha="a" * 40,
            ),
        ]
    )
    return rows


def payloads():
    result = []
    for seq, event in enumerate(events(), 1):
        event_id, machine, kind, ts, raw, sha = unified.normalize(MACHINE, event)
        record_kind = (
            "account_evidence"
            if kind in report_delivery.ACCOUNT
            else "work_evidence"
            if kind in report_delivery.CONTEXT
            else "usage_evidence"
        )
        result.append(
            dict(
                record_id=wire.digest(("erg-report:" + event_id).encode()),
                record_revision=seq,
                record_kind=record_kind,
                origin="derived_reporting",
                kind=record_kind,
                body=dict(
                    schema_version=1,
                    event_id=event_id,
                    machine=machine,
                    kind=kind,
                    ts=ts,
                    payload=json.loads(raw),
                    content_hash=sha,
                    observation_sequence=seq,
                ),
            )
        )
    return result


def policy(db):
    value = projects.snapshot(db)
    value["projects"] = [dict(project_key=k, name=k) for k in ("synthetic-a", "synthetic-b")]
    value["bindings"] = [
        dict(
            binding_id="synthetic-" + tool,
            selectors={"agent_project": dict(machine=MACHINE, tool=tool, id="synthetic-app")},
            allocations=[
                dict(project_key="synthetic-a", units=750000),
                dict(project_key="synthetic-b", units=250000),
            ],
            valid_from=0,
        )
        for tool in ("claude", "codex", "pi")
    ]
    projects.apply(db, value)


def summary(db):
    selection = report_scope.Selection(dict(start=START, end=START + 3600))
    cost = project_rollups.costs(db, start=START, end=START + 3600)
    result = dict(
        costs=cost["totals"],
        projects={
            p["project_key"]: {k: p[k] for k in project_rollups.NUM} for p in cost["projects"]
        },
    )
    for view in ("work", "accounts"):
        value = report_evidence.report(db, view, selection)
        result[view] = dict(total_calls=value["total_calls"], valuation=value["valuation"])
    data = project_rollups.dataset(db, since=START)
    result["usage"] = dict(columns=data["llm_cols"], rows=data["llm"])
    return result


def qualify(directory, replay_payloads=None):
    """Compare direct ingestion with exact exported metadata replay, no network."""
    result = {}
    bodies = payloads() if replay_payloads is None else replay_payloads
    for label in ("source", "replay"):
        path = Path(directory) / (label + ".db")
        db = store.connect(path)
        try:
            if label == "source":
                store.ingest(db, map(json.dumps, events()), machine=MACHINE)
            else:
                for body in bodies + bodies:
                    assert report_delivery.apply(db, body)
                db.commit()
            policy(db)
            project_rollups.initialize(db)
            with db:
                project_rollups.put(db, enabled=1)
            for _ in range(100):
                if call_projection.run(db, limit=1000, seed_limit=1000, budget=1)["ready"]:
                    break
            else:
                raise AssertionError("synthetic canonical calls did not converge")
            with patch("erg.rates.load", return_value=CARD):
                project_rollups.rebuild(path)
                result[label] = summary(db)
        finally:
            db.close()
    assert result["source"] == result["replay"], "synthetic reporting replay differs"
    expected = result["source"]
    assert expected["costs"]["request_equivalents"] == 3
    assert expected["costs"]["tokens"] == 390
    assert expected["projects"]["synthetic-a"]["request_equivalents"] == 2.25
    assert expected["projects"]["synthetic-b"]["request_equivalents"] == 0.75
    assert expected["work"]["total_calls"] == expected["accounts"]["total_calls"] == 3
    return dict(
        reports=expected, project_policy="75% synthetic-a / 25% synthetic-b", rate_card=CARD
    )
