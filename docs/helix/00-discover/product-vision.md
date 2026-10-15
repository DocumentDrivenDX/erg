---
ddx:
  id: product-vision
  authoring:
    home: repo
  links: [erg.competitive-analysis, erg.research.databricks-otel]
---

# Product Vision

## Mission Statement

erg gives engineers who run several coding agents a single local ledger of what the agents spent, produced and cost the machine, in units that compare across agents, models, machines and gateways.

## Positioning

For engineers and small teams (1 to 15) who run Claude Code and Codex daily across several machines and AI gateways and cannot say what that work costs or yields,
erg is a local-first usage ledger that turns agent traces, dated prices, git output and machine load into comparable units.
Unlike ccusage (token cost from one agent's logs on one machine) or an OpenTelemetry-plus-Grafana stack (live metrics for one agent's telemetry),
erg joins every agent, machine and gateway on one set of units and keeps the history after the sources discard it.

## Vision

Every agent session, wherever it runs, leaves a standard trace and a cost record. Prices live in a dated ledger that other tools read, so last month's tokens are costed at last month's prices.
Spend is quoted as dollars per unit of work, with its provenance (reported, configured or unknown) beside it. Subscription and metered traffic appear on the same axis.
Agent runtimes such as fizeau publish their own measurements (power, timing) into the same archive. Traces from local machines reach the organization's telemetry store next to the gateway's spans, joined by trace id.

**North Star**: Any agent's work, on any machine or gateway, appears on one comparable ledger within minutes.

## User Experience

An engineer installs erg on a laptop and two remote hosts, then runs `erg sync`. erg reads Claude Code and Codex sessions, git history, the gateway's usage table and the machines' load history, and opens a dashboard.
The engineer filters to the past week and sees tokens, dollars and work units per project. A table shows that one model costs twice as much per work unit as another on the same repository.
A load chart shows CPU and OrbStack memory rising with agent activity, and flags three hours of heavy load with no agent running. Each cost carries a label: reported by the gateway, configured from the price ledger, or unknown.
At the end of the week `erg sync` also forwards the session traces to the workspace's telemetry table, where the platform team sees the same sessions beside the gateway's spans.

## Target Market

| Attribute | Description |
|-----------|-------------|
| Who | Engineers and teams of 1 to 15 who run Claude Code or Codex daily on two or more machines or through a gateway |
| Pain | Spend and output are scattered across logs, vendor consoles and gateway tables with different units |
| Current Solution | ccusage per machine, vendor consoles, gateway dashboards, hand-built Grafana boards |
| Why They Switch | A model or plan decision needs cost per unit of work across agents, and no current tool can produce it |

## Key Value Propositions

| Value Proposition | Customer Benefit |
|-------------------|------------------|
| One ledger across agents, machines and gateways | Choose models and plans on a like-for-like basis instead of by feel |
| Dollars and tokens per unit of work | See whether extra spend bought extra output |
| Dated price ledger with cost provenance | Historical reports stay correct and unknown costs stay visible |
| Machine load and energy beside tokens | Size hardware and spot idle load with evidence |
| Local-first, standard-library install, SSH collection | Running on a new host takes minutes and no prompt text leaves the machine |

## Success Definition

| Metric | Target |
|--------|--------|
| Reconciliation: token totals against provider or gateway-reported totals, where a source reports them | Within 2% for every such source (erg reconciliation report) |
| Price coverage: tokens costed with `configured` or `reported` provenance | 90% or more (`erg prices check`) |
| Time to first dashboard on a new machine | Under 5 minutes (install script timing in CI) |
| Sources unified on one ledger | At least 4 kinds: agent logs, gateway tables, machine telemetry, git (adapter count) |
| Decisions supported | One published cost-per-work-unit comparison across agents per quarter (docs/reports) |

## Why Now

Claude Code exports OpenTelemetry natively, and Databricks added a unified trace table that accepts client spans (Beta, September 2026).
Teams now mix subscription and metered billing across at least two vendors, so a token count no longer tells them what an agent cost, and agent runtimes such as fizeau publish measurements that need an archive.
The OpenTelemetry generative-AI conventions are still in Development, which leaves room for a neutral ledger to define the units before vendors do.
