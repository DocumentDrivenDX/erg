---
ddx:
  id: erg.competitive-analysis
  authoring:
    home: repo
---

# Competitive Analysis

## Market Landscape

| Attribute | Assessment |
|-----------|------------|
| Market Maturity | Emerging; many small single-agent tools, no common unit across agents (assumption, from the survey below) |
| Growth Rate | Unknown; no source found. Do not quote a figure. |
| Key Trends | Agents emit OpenTelemetry natively; gateways keep usage tables and trace tables; subscription and metered billing coexist |
| Entry Barriers | Low for log readers, Medium for cross-source normalization and price history |
| Buyer Power | High; the buyer is an individual engineer or small team with free alternatives |

## Competitive Forces

| Force | Pressure | Evidence / Confidence | Implication |
|-------|----------|-----------------------|-------------|
| Direct Rivalry | Medium | ccusage, ccmon, Claude Code Monitor; low switching cost (public sources, high) | Win on cross-agent normalization, not on a prettier per-agent view |
| Substitutes | High | Grafana dashboards on Claude Code OTel; vendor consoles; gateway dashboards (public sources, high) | Be the layer that feeds and joins them, not a replacement |
| New Entrants | Medium | Agent vendors can ship usage views; gateways add dashboards (assumption) | Own the neutral price ledger and the unit definitions |
| Buyer Power | High | Free local tools exist (high) | Local-first, zero-dependency install is table stakes |

## Competitor Profiles

| Competitor | Type | Positioning | Target Segment | Strengths | Weaknesses | Source / Confidence |
|------------|------|-------------|----------------|-----------|------------|---------------------|
| ccusage | Direct | CLI that reads Claude Code local JSONL for daily, monthly, session and 5-hour block cost | Individual Claude Code users; sibling tools for Codex and OpenCode | Fast install, cache buckets tracked separately, JSON output | Per machine; costs are virtual estimates; no output (code, commits) or machine-load side | npm and mintlify pages, high |
| Claude Code OTel + Grafana (dashboards 25255, 25052) | Substitute | Live telemetry pipeline into Prometheus or Azure Monitor | Teams with an observability stack | Per-user leaderboards, active time, lines, commits, cache hit ratio | Needs collector and backend; one agent; setup effort | Grafana pages, high |
| ccmon / Claude Code Monitor | Direct | Terminal or web UI over an OTLP receiver | Individual Claude Code users | Live view | One agent; no history ledger or prices | GitHub listings, medium |
| Langfuse, Helicone | Indirect | LLM-application observability (tracing, prompts, evals) and proxy | Teams building LLM apps | Broad feature set, self-hostable | Aimed at applications, not coding-agent sessions on a laptop; Helicone sits in the request path | Vendor and comparison pages, medium (some sponsored) |
| Databricks Unity Gateway usage tables and dashboard | Indirect | Governed usage and trace tables for traffic through the gateway | Databricks customers | Authoritative for gateway traffic, per requester | Sees only gateway traffic; no local machine, subscription or git output | Live schema and row reconciliation, high |

**Indirect Competitors**: provider consoles (Anthropic, OpenAI) show account totals only (medium threat); WakaTime-style coding-time trackers measure activity, not agent spend (low).

## Feature Comparison

| Feature | Us | ccusage | OTel + Grafana |
|---------|----|---------|----------------|
| Claude Code and Codex logs | Full | Full (separate tools) | Partial (Claude Code OTel) |
| Gateway usage ingest | Full | None | None |
| Git output (code, docs, commits) | Full | None | Partial (lines, commits) |
| Machine load correlation | Full | None | None |
| Multi-machine over SSH | Full | None | Full (via collector) |
| Dated price ledger, cost provenance | Planned | Partial | None |
| Cross-agent work-unit efficiency | Planned | None | None |

**Legend**: Full | Partial | Planned | None

## Differentiation Strategy

| Differentiator | Why It Matters | Defensibility |
|----------------|----------------|---------------|
| One ledger across agents, machines and gateways | Spend cannot be compared today across subscription and metered traffic | M |
| Output-side units (work, energy) beside tokens and dollars | Answers "was it worth it", not only "what did it cost" | M |
| Dated, shareable price ledger with provenance | Historical cost stays correct; other tools can consume it | H |
| Local-first, stdlib, SSH-streamed collection | Installs in minutes on any machine, nothing leaves it | M |

**Positioning**: For engineers and small teams running several coding agents across machines and gateways who cannot compare cost with output, erg is a local-first ledger that normalizes tokens, dollars, work and energy. Unlike ccusage or an OTel-plus-Grafana stack, it joins every agent, machine and gateway on one set of units.

## Strategic Implications

- **Attack**: cross-agent and cross-source normalization; the price ledger.
- **Defend**: zero-dependency, local-first install and data ownership.
- **Avoid**: LLM-application observability (prompts, evals); billing-grade metering.
