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
| Resource use and recovery | Demand-proportional idle/steady use; up to one core during catch-up; at least 10x representative input capacity | Process-tree CPU, records/bytes per second, backlog and recovery tests |

### Demand-driven processing and catch-up

When data arrives slowly, collector and reporter resource use must fall with demand.
During catch-up, the operator permits up to one core across erg components and
helpers. The earlier 10% limit must not force hours of avoidable backlog. Keep
bounded transactions, independent durable capture, and idle/retry backoff; avoid
redundant scans, repricing, decoding and writes. Do not spin on empty queues.

Capture, projection and eventual upload must demonstrate at least 10x representative
arrival-rate capacity in both records and bytes. Include bursts, large events,
revisions and outage recovery. Report actual source, accepted, materialized and
projected lag separately. Filtering work-neutral events must preserve raw traces
and must not make an input-capacity test appear to pass by narrowing its denominator.
Measure process-tree CPU over sustained busy and idle windows; instantaneous CPU
or a parent-only sample does not establish efficient resource use. Report the
observed workload and unsupported paths; no universal coverage claim follows from
one benchmark. Runtime measurements belong in iteration evidence.

### Non-Goals

- Billing-grade metering or chargeback; cost is estimation-grade with provenance (concern `usage-metering` is not selected).
- A general telemetry SaaS or arbitrary multi-tenant authorization product. An enrollment-scoped write-only ingestion broker supports the private Databricks corpus.
- An agent runtime or benchmark harness; Fizeau owns agents and measurement, erg archives what they publish.
- LLM-application observability (prompt management, evals).
- Implicit content collection or credential-file capture. Full conversation/tool content requires explicit managed enrollment; transport credentials are excluded.
- Keystroke logging, a managed erg SaaS, or custom multi-user authentication.
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
- **FR-4** — Prompt and response text is measured and discarded unless the operator explicitly enables content capture. The operator has enabled local session-content capture for this deployment. Store conversation and tool-call history privately in the local ledger; central content export remains disabled until the private destination and content policy are verified.

### Subsystem: Gateway Accounting

- **FR-5** — Gateway usage is ingested with requester identity, and the user can view their own, everyone's, or one requester's usage.
- **FR-6** — A call visible both in a local source and the gateway is counted once.
- **FR-7** — The report shows which days or machines have gateway usage but no local capture.

### Subsystem: Host Telemetry

- **FR-40** — Add optional macOS desktop activity as independent linked intervals under FEAT-001. Preserve no-new-prompt capture, independent signal uncertainty, portable agent collection and resource-scoped retention/backpressure; desktop history must not alter agent accounting or agent-content retention. Delivery remains explicitly enrolled and governed by the private destination hold.

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
- **FR-17** — Subscription token value is shown as a dated API-equivalent estimate, separately from subscription charges and any metered charges. Plan names do not imply prices, zero cash spend or billing ownership.

- **FR-28** — Discover provider/account identities and subscription entitlements dynamically from available metadata. Preserve account/plan changes and provenance; exact account IDs and email aliases may join evidence, while conflicting identities remain unknown. Creator identity and billing ownership are distinct.
- **FR-29** — Retain quota windows, reset schedules, observed reset transitions, reset-credit availability and explicit limit-hit events where providers expose them. The monitor does not consume resets or start login/inference.
- **FR-30** — Support later, replay-safe billing and association metadata. Compare known API-equivalent value with subscription spend by account, subscription and billing period; report unknown attribution/prices and currency conversions. A fixed up-front subscription list is not required.
- **FR-31** — Report cumulative observed session token usage and peak observed request input/total tokens separately. Repeated/cache context and reasoning subsets are explicit; row-display caps do not truncate summaries.
- **FR-32** — Link sessions to verified commits first, including commits with no PR. Layer verified PR membership above commits. Ambiguous commit batches and uncertain work/PR associations remain unknown.
- **FR-33** — Capture explicit native session owners separately from inference accounts, including untimestamped session-scoped ownership records. Backfill supported metadata without assigning current plans or routes to historical usage.
- **FR-34** — Discover session-time endpoints, provider/profile, workspace/region and inference account metadata through native agents and wrappers. Preserve evidence scope and timestamps; report call coverage, conflicts and specific unknown reasons. Current configuration, provider/model names and shared email alone do not prove historical routes or billing.

### Subsystem: Durable History and Capture Proof

- **FR-35** — Preserve native conversation and tool-call events in one local store, with stable source identities, compressed content, source progress committed atomically with events, and replay-safe forward capture from enrollment. Existing historical files are not backfilled. Imported history is retained as history, never inferred to be new metered requests.
- **FR-36** — Continuously reconcile discovered native and orchestrated sessions with archived history and usage. Expose missing sources, partial records, parse failures, unsupported formats and uninstrumented local endpoints. A healthy worker is not proof of complete history.
- **FR-37** — Capture direct local inference through enrolled server instrumentation or an explicitly configured capture endpoint. Preserve request/response identities, streaming completion and errors, unknown usage, latency and model evidence. Agent and server observations reconcile only through exact shared identity; ambiguous overlap remains visible.
- **FR-38** — Browse all retained sessions by native ID, project and time; paginate conversation/tool events and distinguish archived, usage-only, reconstructed and expired content. Retention supports explicit expiration with a dry-run inventory and durable expiration evidence; no automatic deletion is enabled without an operator-selected retention period.
- **FR-39** — Package background collection, local reporting and endpoint enrollment for the standard system distribution. Align inference records with published Databricks inference-table fields and preserve erg-specific session, provenance, coverage and retention extensions. Export remains durable, resumable and subject to FR-27 access restrictions.

### Subsystem: Reporting

- **FR-18** — The dashboard answers tokens, cost, output and load by hour and day, grouped by human project, folder/agent project, model, agent and machine. Project allocation is shared across Usage, Work, Costs and session drill-down; Costs is not a separate accounting silo.
- **FR-19** — Correlation views relate host load to agent activity, including lagged relationships.
- **FR-20** — Efficiency (tokens, cost and energy per work unit) is shown with sample size and the share of unknown cost.

### Subsystem: Project Cost Accounting

- **FR-41** — Maintain a local catalog of human projects with stable IDs, names and archived state. Sessions roll up to observed folders or agent projects, which roll up through explicit bindings to these human projects. Initially local administration owns this table; later sync receives master projects from Databricks. Renames and upstream identity reconciliation preserve existing assignments. Project names, folder basenames and free-text titles are not identity keys.
- **FR-42** — Support versioned configuration bindings from observed resources to catalog projects, beginning with repositories, machine-scoped directories, machine-and-agent-scoped native project IDs and sessions, then verified tasks/commits/PRs and other typed resources. Preserve captured project hints independently. Preview rule changes; overlapping conflicting assignments remain unresolved, and unmatched usage remains unassigned.
- **FR-43** — Resolve project allocation per canonical usage observation and effective time, and apply the same hierarchy and allocation policy to work, usage, input and edit reporting. A session that changes folder or binding over time can contribute to several parents; never relabel its full history using only its latest folder. Support explicit weighted splits and partial/unassigned portions without duplicating usage, and preserve rule/evidence/version provenance. Session summaries may span projects; a session-level label must not charge every project for the full session.
- **FR-44** — Keep prices, observed/reported cost evidence, billed usage and subscription charges in erg. Project views distinguish these bases, pricing coverage, assignment coverage and policy-based fixed-charge allocation. Estimates and subscription fees are never added as though they were separate incurred usage charges.
- **FR-45** — Support eventual master-project download and replay-safe publication of project assignment revisions/tombstones to Databricks, including session links and call-level allocations needed for shared dashboards. Remain useful offline with a versioned local catalog snapshot; do not enable central export before FR-27 is satisfied.

### Subsystem: Deployment and Reporting Backends

- **FR-23** — The collector runs once under cron or continuously as a service through the same collection pipeline. The reporter runs independently and never schedules collection.
- **FR-24** — A collector persists full enrolled traces locally and delivers them through a versioned write-only ingestion interface to the private Databricks data plane. Central outages do not prevent local collection or reporting; export resumes from durable progress.
- **FR-25** — The same reporting UI and report semantics work against local SQLite and governed Unity Catalog data. The reporter may run locally in either mode; central UI hosting is optional.
- **FR-26** — Central reports combine agent-session evidence, gateway usage, work evidence and host metrics without counting observations of the same call or commit as separate work or usage. Capture and export retries do not change totals; late evidence can update reconciliation.
- **FR-27** — The first central deployment captures full forward session content from every Intune-managed Mac receiving as-agent, with an explicitly designated operator as the only ordinary human reader/administrator. Collectors authenticate to the central sink; Databricks query authorization remains governed by Unity Catalog. Prompt and response bodies are excluded by default, and transport credential fields are excluded from telemetry; secrets typed into explicitly enrolled content may remain in that content. Central session data must use a new private erg catalog/schema in the production workspace, with effective access checked for inherited grants. Central export and deployment remain paused until the required permissions are available; a developer-readable catalog is not an acceptable fallback. Processing identities have narrowly scoped service privileges; platform administrators retain privileged recovery capabilities. Content retention and any upload of already-retained local events require explicit rollout policy.

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
- **Data/Storage**: SQLite locally; Unity Catalog Delta tables for centralized observations and reporting.
- **APIs**: Databricks SQL for central reporting and gateway reads; CONTRACT-006 JSON/HTTPS to a durable private broker, then selected Zerobus REST into owned staging and canonical tables. Optional OTel interoperability is separate from fleet delivery.
- **Platform Targets**: macOS and Linux hosts.
- **Trace model**: the Fizeau telemetry contract (OpenTelemetry GenAI conventions plus `ddx.*` fields) is the proposed canonical shape.

## Constraints, Assumptions, Dependencies

### Constraints

- **Technical**: portable collection and JSON/HTTPS delivery run on stock macOS Python without third-party packages. Cloud SDKs are central-only. Exact persisted UTF-8 bytes, bounded chunks and separate accepted/materialized/projected receipts govern delivery. Device enrollment, private storage access and scoped Zerobus OAuth require qualification; a missing credential never causes a broader fallback.
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
