---
ddx:
  id: erg.adr.002
  status: proposed
  links: [prd, erg.adr.001]
---
# ADR-002: Canonical trace model and the consumer boundary

## Local-first disposition — 2026-10-27

This remains a deferred proposal. The operator explicitly deferred OTel for the
current local-first scope; ADR-003 governs runtime/tooling and current native
ledger/accounting contracts govern the implemented shape. No OTel SDK/exporter
or trace-shape migration is a local release gate. The proposal below records
future interoperability intent and does not override those current contracts.

## Context
The operator's direction (2026-10-16): gate everything through the AI gateway, extend measurement to the local host for traffic the gateway
never sees, capture first and centralize later, and keep research and definitions separate from usage. Fizeau already publishes a normative
telemetry contract (CONTRACT-001: OpenTelemetry GenAI conventions plus `ddx.*` identity, cost and timing fields; CONTRACT-003: cost provenance).

## Decision (proposed)
1. **One normalized session record, shaped as CONTRACT-001.** Collectors for agents with no native telemetry (Claude Code and Codex logs) backfill
   spans in that shape; agents that emit conforming telemetry (Fizeau) are ingested as they are. The local store keeps the flattened rollups.
2. **erg is a consumer of two external data artifacts**, each versioned and researched elsewhere: the price ledger and the work-unit definitions.
   erg ships a seed copy as an offline fallback and never embeds the research or rules in code.
3. **Cost provenance** follows the Fizeau contract (`provider_reported`, `gateway_reported`, `configured`, `unknown`) plus one proposed addition,
   `estimated`, for a price taken from a public list or an update script. Fizeau's rule that unknown cost is never guessed stays intact because
   `estimated` is labelled and never replaces a reported or configured value.
4. **Sequence**: capture and report locally first; the Databricks write path (traces to the unified trace table, host metrics to a Unity Catalog table)
   is designed after research, behind one export adapter.

## Consequences
- Backfilled spans must be marked as reconstructed (no live timing), so consumers can tell them from native spans.
- The proposed `estimated` value needs agreement with Fizeau before it appears in exported data.
- Open: whether native OpenTelemetry is needed alongside full logs; see `docs/research/agent-telemetry.md` when it lands.
