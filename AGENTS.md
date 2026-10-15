# erg

Local-first usage/productivity monitor. Read `README.md`, then `docs/helix/` (this repo uses HELIX; marker in `.helix.yml`).
Layout: `erg/collect.py` (single-file collector), `store.py` (SQLite + rollups), `gateway.py` (Databricks), `cli.py`, `server.py`, `static/index.html` (dashboard).
Tests: `python -m unittest discover -s tests`. No third-party dependencies.
