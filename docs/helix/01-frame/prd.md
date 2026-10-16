---
ddx:
  id: prd
  authoring:
    home: repo
  links: [product-vision]
kind: product
---

# Product Requirements Document

## Summary

erg captures coding-agent sessions from as many agents as it can, joins them with host utilization and work-completion evidence, and keeps them in one local ledger that can be centralized.
Its model is gateway-first: traffic through the AI gateway is already measured, so erg extends the same measurement to the local host to cover traffic the gateway never sees (subscription plans, local models) and to add what a gateway cannot know (host load, intent, value).
Users are engineers and platform teams who run Claude Code, Codex and Fizeau across machines. The approach is capture first, report second, centralize third.
Top measures: token totals reconcile within 2% of any source that reports its own, 90% of tokens carry a priced or reported cost, and a new machine reaches a first report in under 5 minutes.

## Problem and Goals

### Problem

An engineer using three agents on two machines has usage in agent logs, a gateway table, a vendor console and git, each in different units. Subscription traffic is invisible to the gateway, gateway traffic is invisible to local logs, and none of them know what the machine was doing or whether the work shipped.
Answering "what did last week's agent work cost per unit of delivered work, and did it load the machine" takes hand-joining four sources, so it is not done.

### Goals

1. Capture agent sessions broadly and uniformly: every supported agent produces the same normalized session record regardless of where its data comes from.
2. Account for cost with provenance: every cost says whether it was reported, configured, estimated or unknown, and prices are dated.
3. Relate usage to host utilization, intent and value (work completed) to the degree they are measurable.
4. Keep the capture shape compatible with centralization into the organization's AI gateway telemetry, with capture delivered first.

### Success Metrics

| Metric | Target | Measurement Method |
|--------|--------|--------------------|
| Token reconciliation against sources that report totals | Within 2% | Reconciliation report against `system.ai_gateway.usage` and provider exports |
| Tokens costed as reported, configured or estimated | 90% or more | Price coverage report |
| Agents captured with a normalized session record | 3 at P0 (Claude Code, Codex, Fizeau); more as research confirms | Adapter count and fixture tests |
| Time to first report on a new machine | Under 5 minutes | Timed install in CI |
| Capture overhead on the host | Under 1% of one core, averaged | Sampler self-measurement |

### Non-Goals

- Billing-grade metering or chargeback; cost is estimation-grade with provenance (concern `usage-metering` is not selected).
- An agent runtime or benchmark harness; Fizeau owns agents and measurement, erg archives what they publish.
- LLM-application observability (prompt management, evals).
- Storing prompt or response text or credentials; content capture is never on by default.
- Keystroke logging, a hosted service, or multi-user authentication.
- Defining the work-unit research or price research inside the tool: those are separate versioned data artifacts that erg consumes.
- Deployment orchestration to specific machines; erg must install and run on a variety of hosts, but rollout is outside this product.

Deferred items tracked in `docs/helix/parking-lot.md`.

## Users and Scope

### Primary Persona: Agent-heavy engineer

**Role**: Software engineer running Claude Code, Codex or Fizeau daily on one or more machines.
**Goals**: Know what the agents cost and produced, choose models and plans on evidence, spot wasted load.
**Pain Points**: Usage is split by agent, machine and billing path; no common unit; history disappears when tools rotate logs.

### Secondary Persona: Platform administrator

**Role**: Owner of the organization's AI gateway and telemetry tables.
**Goals**: See coding-agent traffic that bypasses the gateway next to the traffic that goes through it, in the same tables.
**Pain Points**: Subscription usage and local-model usage are invisible; machine-level cost drivers are unknown.

## Requirements

### Must Have (P0)

1. Capture sessions from Claude Code, Codex and Fizeau into one normalized session record, including usage buckets, model, project and time.
2. Ingest AI gateway usage and reconcile it with local capture without double counting.
3. Capture host utilization and relate it to agent activity by time.
4. Capture work-completion evidence from version control, and measure the user's own input as size only.
5. Report all of the above by hour, day, project, model, agent and machine, offline, with provenance on every cost.

### Should Have (P1)

1. A dated price ledger with offline fallbacks, update scripts, and live gateway prices where available.
2. Work-unit definitions consumed from a separate definitions artifact.
3. Intent and value proxies per session (what the session was for; whether the work was accepted or retained).
4. Capture for additional agents confirmed by research.

### Nice to Have (P2)

1. Energy estimates for local hosts.
2. Archiving measurements published by Fizeau (power, timing).

## Functional Requirements

### Subsystem: Session Capture

- **FR-1** — Each supported agent yields a normalized session record with the same usage buckets (fresh input, cache read, cache write, output) regardless of source.
- **FR-2** — Capture is idempotent: re-reading the same source never changes totals, and streamed, forked or resumed sessions are counted once.
- **FR-3** — Capture works from agent session logs when an agent emits no telemetry, and from native telemetry when it does, producing the same record either way.
- **FR-4** — Prompt and response text is measured (size) and discarded unless the operator explicitly enables content capture.

### Subsystem: Gateway Accounting

- **FR-5** — Gateway usage is ingested with requester identity, and the user can view their own, everyone's, or one requester's usage.
- **FR-6** — A call visible both in a local source and the gateway is counted once.
- **FR-7** — The report shows which days or machines have gateway usage but no local capture.

### Subsystem: Host Telemetry

- **FR-8** — CPU, memory, disk, network, GPU and thermal state are captured at hourly or finer resolution from the best available source on the host, and the source is recorded.
- **FR-9** — Short-lived upstream history (ring buffers) is archived before it expires.
- **FR-10** — The report flags heavy-load hours that no captured agent activity explains.

### Subsystem: Work and Value

- **FR-11** — Version-control output (commits, code and document lines) is captured per project and attributed to agents where the commit says so.
- **FR-12** — Work units are computed from a versioned definitions artifact, not from rules embedded in the tool.
- **FR-13** — A session has an intent label and a value signal when evidence exists (for example a linked commit or merged change), and shows unknown otherwise.

### Subsystem: Cost Accounting

- **FR-14** — Every cost carries provenance: reported, configured, estimated or unknown; unknown cost is never summed as zero.
- **FR-15** — Prices are dated; a call is costed at the price in force when it happened.
- **FR-16** — A committed fallback price set exists, so reports work offline; update scripts refresh it from industry sources, and live gateway pricing overrides it where available.
- **FR-17** — Subscription traffic is shown at pay-as-you-go-equivalent cost and as zero cash spend.

### Subsystem: Reporting

- **FR-18** — The dashboard answers tokens, cost, output and load by hour and day, grouped by project, model, agent and machine.
- **FR-19** — Correlation views relate host load to agent activity, including lagged relationships.
- **FR-20** — Efficiency (tokens, cost and energy per work unit) is shown with sample size and the share of unknown cost.

### Subsystem: Centralization

- **FR-21** — Captured sessions can be exported as standard OpenTelemetry traces compatible with the Fizeau telemetry contract.
- **FR-22** — A research-backed path exists for sending session traces and host metrics to the organization's Databricks tables; this follows local capture.

## Acceptance Test Sketches

| Requirement | Scenario | Input | Expected Output |
|-------------|----------|-------|-----------------|
| FR-2 | Capture the same session log twice | A fixture log with 3 streamed duplicate messages | Totals equal a single pass |
| FR-6 | A gateway row matches a local call | Local call and gateway row with equal output and cache-read tokens within 2 minutes | One call counted; the gateway row marked duplicate |
| FR-14 | Cost of a model with no price | Calls for a model missing from the ledger | Cost shown unknown, excluded from dollar totals, unknown-call count shown |
| FR-15 | Price change mid-week | Ledger with two dated prices for one model | Calls before and after the date use different prices |
| FR-10 | Heavy load without agents | Hour with 80% CPU and no sessions or prompts | Hour appears in the unexplained-load list |
| FR-4 | Session containing text | Prompt of known length | Stored size equals length; no text in the store |

## Technical Context

Current decisions, recorded without making them (rationale belongs in ADRs).

- **Language/Runtime**: Python 3.8+ floor, standard library only, single-file collector (see concerns override).
- **Data/Storage**: SQLite locally; Delta tables in Databricks for centralization (open).
- **APIs**: Databricks CLI and SQL statements API for gateway reads; OTLP over HTTP (protobuf) for trace export.
- **Platform Targets**: macOS and Linux hosts.
- **Trace model**: the Fizeau telemetry contract (OpenTelemetry GenAI conventions plus `ddx.*` fields) is the proposed canonical shape.

## Constraints, Assumptions, Dependencies

### Constraints

- **Technical**: collector runs on stock macOS Python; no third-party packages; the Databricks trace collector accepts traces only, as protobuf.
- **Business**: single maintainer; open source, MIT OR Apache-2.0.
- **Legal/Compliance**: no prompt content leaves the machine by default.

### Assumptions

- Agents keep writing local session records in the formats observed on 2026-10-15 (to be re-verified by the telemetry research).
- The gateway usage table stays readable to the operator.
- Work-unit definitions and prices can be treated as external data with their own versioning.

### Dependencies

- Fizeau telemetry contract (CONTRACT-001, CONTRACT-003) for trace shape and cost provenance.
- Databricks AI gateway tables and the Unity Gateway trace collector (Beta).
- Research artifacts in `docs/research/` (work units, agent telemetry, pricing sources) and the definitions and ledger they feed.

## Risks

| Risk | Probability | Impact | Mitigation |
|------|-------------|--------|------------|
| Agent log formats change without notice | High | High | Fixture-based adapter tests; version field per adapter; contract test against real logs in CI |
| Beta collector semantics change | Medium | Medium | Probe test for accepted spans; export path isolated behind one adapter |
| Work-unit definitions are contested | High | Medium | Separate versioned definitions; report shows the definition version; raw counts always available |
| Prices missing for new models | High | Medium | Unknown stays visible; update script and gateway live prices; no guessed values |
| Subscription usage cannot be measured exactly | Medium | Medium | Report effective cost as an estimate with provenance |

## Open Questions

- [ ] Which additional agents expose capturable sessions? — blocks FR-1 scope beyond P0, answered by `docs/research/agent-telemetry.md`
- [ ] What write path carries host metrics into Databricks (Unity Catalog table)? — blocks FR-22, needs research after capture ships
- [ ] Do session logs need OpenTelemetry on top for centralization, or is backfilled export enough? — blocks FR-21 design, answered by `docs/research/agent-telemetry.md`
- [ ] Which work-unit definitions earn evidence? — blocks FR-12, answered by `docs/research/work-units.md`
- [ ] Which price sources can the gateway provide live? — blocks FR-16, answered by `docs/research/pricing-sources.md`
- [ ] Should `estimated` become a cost source in the Fizeau contract? — blocks FR-14 interop, agree with Fizeau

## Success Criteria

- A single report shows tokens, cost, work and load for one week across Claude Code, Codex, Fizeau and the gateway, with every cost labelled.
- Reconciliation passes the 2% target against every source that reports totals.
- The same normalized records export as traces the Databricks collector accepts (probe passes) without changing capture.
