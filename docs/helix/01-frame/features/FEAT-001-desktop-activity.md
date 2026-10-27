---
ddx:
  id: FEAT-001
  status: proposed
  authoring:
    home: repo
  links: [prd, erg.contract.001]
---
# Feature Specification: FEAT-001 — Desktop Activity

**Priority:** P0 within the later desktop increment; session foundation remains first.
**Owner:** Erik LaBianca
**Covered PRD Subsystem(s):** Host Telemetry
**Covered PRD Requirements:** FR-40
**Cross-Subsystem Rationale:** None — one optional observation capability.

## Overview

An optional Mac helper supplies desktop activity intervals to erg's existing
local ledger. Intervals are independent linked resources, never agent sessions.
The source authority is [Erg Desktop Activity Feature Requirements](https://chatgpt.com/space/page_9295049ae888819197f44a1322bd4899),
updated October 21, 2026. Its DA identifiers remain handoff references.

## Ideal Future State

The local operator can distinguish foreground apps, recent input, unlocked idle,
lock state, app audio streams and missing observations. They can inspect timing
and provenance without granting additional macOS privacy permissions. Later
project links preserve uncertainty and corrections without rewriting observations.

## Problem Statement

Open applications and resource samples cannot establish interaction or attention.
Input idle does not establish absence of work. A separate observation timeline is
needed without changing agent usage, cost, human-input or session-count semantics.

## Functional Areas

| Area | Operator question | Responsibility |
|---|---|---|
| Observation | What signals were available? | Independent foreground, interaction/lock, audio and availability evidence |
| Interval history | What happened during this period? | Compact, durable intervals with gaps and policy provenance |
| Local inspection | What is known or missing? | Inspect/export observations and source health within erg |

## Requirements

- **DESK-01 (DA-OBS):** Observe stable foreground app identity/label, input idle,
  console/lock state and per-app audio input/output activity independently. Unknown
  readings stay unknown. Isolate active console users and retain boot/session identity.
- **DESK-02 (DA-INT):** Compress equivalent semantic states into independent
  timelines. Rising idle counters must not fragment stable state. Sleep, missed
  samples, clock changes and crashes must not extend apparent activity. Raw samples
  are transient. Stable interval identities, revisions and policy provenance survive recovery.
- **DESK-03 (DA-RET):** Default desktop retention targets 30 days and 100 MB per
  user including desktop staging, journal, logs and reserve. Only finalized local-only
  or latest-revision fully acknowledged intervals may expire. Every required destination
  must acknowledge. Pending/failed/ambiguous revisions remain pinned, including when
  an exporter is disabled. Explicit destination retirement is distinct from disabling.
- **DESK-04 (DA-EXP):** Local-only enrollment creates no permanent central pending
  queue. Resource/destination enrollment is explicit. New destinations replay retained
  data and disclose expired gaps; unrelated resource cursors must not skip retained data.
- **DESK-05 (DA-LIFE):** Ship an optional signed/notarized native helper within erg
  releases, supporting install/update/rollback/removal without a compiler, Homebrew,
  administrator interaction or new privacy prompts on supported standard-user Macs.
- **DESK-06 (DA-HEALTH):** Isolate stalled probes, expose coverage denominator,
  capability failures and backpressure. Protected desktop data exhausting its allocation
  pauses desktop capture with a visible gap and bounded health reserve; agent ingestion
  must remain available. Purging pending data requires a separate loss inventory and
  explicit acknowledgement; no purge is authorized by this feature.
- **DESK-07 (DA-INTEG):** Reuse machine identity, private ledger, journal and reporting.
  Preserve portable stdlib-only collection when the Mac helper is absent. Desktop
  intervals contribute no agent sessions, model calls, tokens, cost or human inputs.
- **DESK-08:** Keep project/session annotations in separate versioned evidence links.
  Time overlap is uncertain correlation; it cannot weaken exact agent/commit/PR attribution.

## Non-Functional Requirements

Proposed engineering targets, not measured results: 5-second samples, 60-second
recent-input threshold, gaps beyond 15 seconds, at most 30 seconds of uncheckpointed
observations, transition precision within 10 seconds, awake/active-console coverage
at least 99%, mean CPU below 1 core-percent and resident memory below 100 MB over
8 hours. Stable state must produce constant-size interval sets. Eligible expiry
must occur within 24 hours. Validate on three representative Macs over five days.

Security requires zero new Input Monitoring, Accessibility, Screen Recording,
Microphone, Automation or notification prompts. Do not collect individual inputs,
window titles, paths, URLs, screenshots, clipboard, document contents or audio.
Local store/export/log/diagnostic minimization tests must find zero fixture secrets.

## User Stories

Story decomposition and executable acceptance criteria remain pending design of
the optional helper boundary. Source DA-VERIFY scenarios are requirements inputs,
not completed tests or canonical story IDs. Implementation must not start from this
handoff alone; extend the observation/retention contract and release test plan first.

## Edge Cases and Error Handling

Unavailable signals degrade independently; input recency cannot prove physical
presence. Multiple simultaneous audio apps remain independent of foreground state.
Fast user switching cannot attribute another user's signals. Older acknowledgements
cannot release newer revisions. Disk pressure cannot evict protected pending data.
Expired tombstones prevent replay resurrection. Disabling export cannot imply consent
for deletion. New destinations cannot recover expired history and must disclose that gap.

## Success Metrics

Zero prompts and accounting contamination; all fixture transitions/gaps preserved;
coverage, checkpoint and resource targets demonstrated in installed-product pilot.
A terminal prototype does not establish compatibility or release acceptance.

## Constraints and Assumptions

Initial proposed support is Apple Silicon and deployed macOS 26 versions, pending
validation. No native dependency is mandatory for portable agent capture. The 30-day
and 100-MB policy applies only to desktop resources, never the entire SQLite database
or agent history. Existing central private-destination hold remains in force.

## Dependencies

FR-40; CONTRACT-001; a new desktop observation/retention contract and helper ADR;
installed-product no-prompt, console isolation, interval/recovery and retention tests.
Source DA-VERIFY-1 through DA-VERIFY-7 families define the scenario inventory.

## Out of Scope

Native implementation in this foundation pass; central deployment; data purge;
GitHub/Slack/calendar/meeting integrations; timesheets, billing/payroll submission;
automatic project certainty; Windows/Linux desktop support; richer permission tiers.
