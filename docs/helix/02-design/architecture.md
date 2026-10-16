---
ddx:
  id: erg.architecture
  status: draft
  links: [prd]
---
# Architecture

```
 machine A (local)         machine B (ssh)
 collect.py  ── NDJSON ──▶ ssh host python3 - < collect.py ──▶ NDJSON
      │                                   │
      └────────────── erg ingest ◀──────┘   (idempotent upsert on uid)
                          │
                    SQLite  (~/.local/share/erg/erg.db)
                          │
                 erg serve  → /api/data (hourly rollups) → static SPA
 sampler (launchd/cron, 30s) → perf.ndjson on each machine → picked up by collect
```

## Decisions
- **ADR-1 Single-file stdlib collector.** It can be piped over SSH to any host with python3 (3.8+) and nothing installed.
- **ADR-2 NDJSON event protocol** (`t`: llm|edit|prompt|commit|perf), each with a stable `uid`; central store upserts, so re-scraping is safe.
- **ADR-3 Hourly rollups served, grouping done client-side.** Keeps the API tiny and every pivot instant.
- **ADR-4 Token normalization.** `fresh_in` = uncached input, `cache_read`, `cache_write`, `out` (includes reasoning; shown separately). Codex `cached_input` is a subset of `input` and is subtracted.
- **ADR-5 Lines from git are authoritative; agent edits are activity.** Commits attributed via Co-Authored-By trailers.
- **ADR-6 Own input = prompt size, not keystrokes.** Prompt text is measured and discarded. Active time from HID idle on macOS.

## Telemetry adapters (ADR-7)
Existing monitors are preferred over our own sampler; the collector reads them read-only and emits canonical `metric` events (`src`, `m`, `ts`, `iv`, `v`, `pk`).
- **Endpoint-metrics CSVs** (directories configured via `$ERG_ENDPOINT_CSV_DIRS` or `~/.config/erg/config.json`): 5-min memory (Activity-Monitor-equivalent), pressure, swap, compressor churn, CPU, thermal, OrbStack VM footprint.
- **iStat Menus 7 `history.db`**: multi-resolution ring buffers (5 s … 6 d; fine tiers expire, so sync regularly) for CPU, memory, disk and network throughput (KB/s), GPU, load, hashed sensor keys (temps/fans by value-range heuristic).
- **Own sampler** remains for hosts without either (Linux, remote), and is the only source of keyboard-active time.
- Hourly rollup picks, per machine/metric/hour, the highest-priority source, and within it the finest tier covering >=80% of the hour.
