#!/usr/bin/env python3
"""Serve only a disposable synthetic ledger for real-browser verification."""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    with tempfile.TemporaryDirectory(prefix="erg-browser-") as temp:
        empty = os.environ.get("ERG_FIXTURE_EMPTY") == "1"
        for name in list(os.environ):
            if name.startswith(
                ("ERG_", "CODEX_", "CLAUDE_", "OMNIGENT_", "PI_CODING_", "HARNESS_")
            ):
                os.environ.pop(name)
        os.environ.update(
            HOME=temp,
            ERG_HOME=temp + "/ledger",
            ERG_STATE_DIR=temp + "/state",
            ERG_DATABASE=temp + "/ledger/fixture.db",
        )
        from erg import call_projection, history, project_rollups, projects, server, store

        with store.connect() as db:
            if not empty:
                stamp = int(time.time())
                rows = [
                    dict(
                        t="llm",
                        uid="browser-call",
                        ts=stamp,
                        tool="pi",
                        session="synthetic-session",
                        project="Synthetic app",
                        model="claude-haiku-4-5",
                        kind="interactive",
                        fresh_in=100,
                        cache_read=20,
                        cache_write=0,
                        out=7,
                        reasoning=0,
                    ),
                    dict(
                        t="session",
                        uid="browser-session",
                        ts=stamp,
                        tool="pi",
                        session="synthetic-session",
                        project="Synthetic app",
                        usage_status="observed",
                        provenance="quality_fixture",
                    ),
                ]

                rows.extend(
                    [
                        dict(
                            t="identity",
                            uid="fixture-owner",
                            ts=stamp,
                            tool="pi",
                            session="synthetic-session",
                            provider="fixture",
                            account_key="fixture-account",
                            email_hint="operator@example.invalid",
                            plan="Synthetic plan",
                            provenance="fixture",
                        ),
                        dict(
                            t="quota",
                            uid="fixture-window",
                            ts=stamp,
                            tool="pi",
                            session="synthetic-session",
                            account_key="fixture-account",
                            window="five_hour",
                            window_minutes=300,
                            used_percent=25,
                            resets_at=stamp + 7200,
                            event="snapshot",
                        ),
                    ]
                )

                store.ingest(db, map(json.dumps, rows), machine="quality-host")
                with db:
                    history.enroll(db)
                    history.put(
                        db,
                        "pi",
                        "synthetic-session",
                        "Synthetic app",
                        stamp,
                        "message",
                        {"text": "SYNTHETIC trace: preserve session history"},
                        "fixture-input",
                        role="user",
                        machine="quality-host",
                    )
                    db.execute(
                        "INSERT INTO session_titles VALUES(?,?,?,?,?,?,?)",
                        (
                            "quality-host",
                            "pi",
                            "synthetic-session",
                            "Preserve agent history",
                            "fixture",
                            stamp,
                            stamp,
                        ),
                    )
                    projects.observe_context(
                        db,
                        "quality-host",
                        "pi",
                        "synthetic-session",
                        stamp - 1,
                        "fixture-context",
                        "/code/synthetic",
                        "",
                    )
                    config = projects.snapshot(db)
                    config["projects"] = [dict(project_key="quality:app", name="Synthetic product")]
                    config["bindings"] = [
                        dict(
                            binding_id="fixture-binding",
                            priority=10,
                            selectors={
                                "directory": {"machine": "quality-host", "root": "/code/synthetic"}
                            },
                            allocations=[dict(project_key="quality:app", units=1000000)],
                            valid_from=0,
                        )
                    ]
                    db.commit()
                    projects.apply(db, config)
                    project_rollups.initialize(db)
                    project_rollups.put(db, enabled=1)
                for _ in range(20):
                    if call_projection.run(db, limit=100, seed_limit=100, budget=1)["ready"]:
                        break
            if not empty:
                project_rollups.rebuild(os.environ["ERG_DATABASE"])

        server.serve(port=int(sys.argv[1]), bind="127.0.0.1")


if __name__ == "__main__":
    main()
