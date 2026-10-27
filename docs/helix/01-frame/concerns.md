# Project Concerns: erg

## Area Labels

| Area | Description |
|------|-------------|
| `all` | Every work item |
| `collect` | The single-file collector and its source adapters (agent logs, git, iStat, endpoint CSV, sampler) |
| `store` | SQLite store, rollups, price ledger, unit normalization |
| `ui` | The offline dashboard (`erg/static/index.html`) and its HTTP server |
| `gateway` | Databricks gateway ingest and, later, OTLP trace forwarding |

## Active Concerns

- **python-uv** — Python language runtime, with project overrides below (areas: all)
- **testing** — layered tests; `unittest` here (areas: all)
- **o11y-otel** — OpenTelemetry is the governing telemetry model for traces erg emits and ingests (areas: collect, gateway)
- **security-owasp** — usage envelopes exclude content; explicitly enrolled private history preserves bodies, while transport credentials are excluded (areas: all)
- **scope-discipline** — explicit non-goals in the PRD gate gold-plating (areas: all)
- **unity-catalog** — Databricks realization of governance for gateway reads and any future writes (areas: gateway)
- **databricks-ai-gateway** — gateway usage and trace tables as an ingest and export target (areas: gateway)

## Not Selected

- **usage-metering** — erg's cost accounting is estimation-grade with provenance labels; it never drives invoices, so the financial-grade metering contract does not apply. Revisit if erg output is ever used for chargeback.

## Selection Sources

| Slot or concern | Selection | Source |
|---|---|---|
| language-runtime | python-uv, overridden (see below) | operator-override |
| architecture-style | none selected; revisit in Design | assumption |
| o11y-otel, security-owasp, scope-discipline, testing | selected | assumption (implied by the vision) |
| unity-catalog, databricks-ai-gateway | selected for the `gateway` area | assumption (research note, 2026-10-15) |

## Project Overrides

### python-uv

The shipped concern assumes Python 3.12+, uv, pydantic-settings, pytest and ruff. erg deviates on purpose:

- **Runtime floor**: the collector must run unmodified on stock macOS (`/usr/bin/python3`, 3.9) and on remote Linux hosts, streamed over SSH. The floor is Python 3.8; the collector is one stdlib-only file.
- **Dependencies**: none. No pydantic, no pytest, no third-party packages in the shipped tool. Configuration is JSON files and `ERG_*` environment variables.
- **Tests**: `python -m unittest discover -s tests`, run on Python 3.8 and 3.12 in CI.
- **Open**: whether Python 3.12+ and uv apply to the non-collector packages (`store`, `server`) is a Design question; record the answer in an ADR.
