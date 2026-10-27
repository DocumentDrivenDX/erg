# erg

Local-first usage/productivity monitor. Read `README.md`, then `docs/helix/` (this repo uses HELIX; marker in `.helix.yml`).
Layout: `erg/collect.py` (single-file collector), `store.py` (SQLite + rollups), `gateway.py` (Databricks), `cli.py`, `server.py`, `static/index.html` (dashboard).
Tests: `python -m unittest discover -s tests`. No third-party dependencies.

Development gate: `make quality` using Python 3.12/uv and Node 22; developer tools
must never become installed runtime dependencies. Follow ADR-003 and concerns.md.
Use the checked test runner for completion claims, not a silently skipped suite.
Baseline shape/type/cycle allowances may decrease, never increase to hide a failure.
OTel work is deferred. Preserve the independently runnable single-file collector.
