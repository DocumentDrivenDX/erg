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
        from erg import server, store

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
                store.ingest(db, map(json.dumps, rows), machine="quality-host")
        server.serve(port=int(sys.argv[1]), bind="127.0.0.1")


if __name__ == "__main__":
    main()
