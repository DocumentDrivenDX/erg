---
ddx:
  id: FEAT-002
  type: feature-specification
  authoring:
    home: repo
  links: [prd]
---

# Feature Specification: FEAT-002 — Unified reporting investigation

**Feature ID**: FEAT-002
**Priority**: P0
**Owner**: Erg maintainer
**Covered PRD Subsystem(s)**: Reporting
**Covered PRD Requirements**: FR-18, FR-19, FR-20, FR-25
**Cross-Subsystem Rationale**: Reporting consumes existing accounting, allocation and capture authorities; it does not change their policies.

## Overview

One investigation carries scope and accounting meaning from a summary to project,
session and work evidence. Account capacity and system health have distinct destinations.

## Ideal Future State

An agent-heavy engineer can answer what work cost, what it produced, and how much
subscription capacity remains. They can explain any total by following its evidence,
without recreating filters or translating between unrelated cost screens.

## Problem Statement

The current dashboard separates Usage, Session history, Work, Costs, Accounts &
spend, Capture and Machine. Session lists overlap. Long accounting explanations and
setup forms compete with primary reports. The operator has explicitly requested
less clutter and fewer repeated unknown rows.

## Functional Areas

| Area | User question | Responsibility |
|---|---|---|
| Overview | What happened in this scope? | Usage, rack-rate value, effective expense, output and actionable gaps |
| Project investigation | What did this human project consume and produce? | Shared hierarchy through folders to sessions and verified work |
| Session investigation | What happened in this session? | Searchable traces, model/route/account evidence and cost basis |
| Account availability | What capacity remains and when does it reset? | Plan, quota windows, charges and attribution evidence |
| System inspection | Is capture and delivery healthy? | Secondary diagnostics, host correlations and reporting freshness |

## Task Model

| Task | Goal | Users | Needs | Rules | Realized by | Key |
|---|---|---|---|---|---|---|
| Explain project spending | Trace project expense to sessions and work | Agent-heavy engineer | Know agent cost and output | Existing CONTRACT-004/005 and accounting contract | UJM-001, UF-002 | explain-spending |
| Inspect remaining capacity | Know account availability and reset timing | Agent-heavy engineer | Choose plans on evidence | Existing account and route evidence policies | UF-002 | inspect-capacity |
| Inspect a session | Read trace and explain tokens and payer | Agent-heavy engineer | Durable session history | Existing session ledger and accounting contracts | UF-002 | inspect-session |

## Requirements

### Investigation continuity

- **NAV-01**: The system must provide Overview, Projects, Sessions and Accounts as primary destinations; System is secondary. Project and session detail remain nested.
- **NAV-02**: Date/time, human project and machine scope must persist through navigation, drill-down, refresh, history navigation and shareable links. Folder, model, agent and route are progressive filters. Detail identity does not silently overwrite the parent scope.
- **NAV-03**: Usage, work, costs and account summaries must use the same allocation revision and accounting basis. A mixed publication must be identified before presenting a comparison as reconciled.

### Meaning and evidence

- **MEAN-01**: Each money summary must distinguish rack-rate value (estimate or range), reported metered charges, subscription/reset charges and effective allocated expense. Never add rack value to incurred expense. Show billing-period denominator and provisional coverage.
- **MEAN-02**: Missing facts must have an affected count, specific reason and next action when available. Collapse repeated reasons; never hide affected usage or replace missing cost with zero. Rate gaps require resolution and remain visible.
- **MEAN-03**: Session title and stable ID, project path, timestamps, token totals, peak context, routes, payer evidence and verified commit/PR titles must be reachable from the investigation. Native owner and inference payer remain distinct.
- **MEAN-04**: Account availability must identify observation time, each quota window, reset time and source; calendar resets and paid resets remain distinct. Historical account association requires session-time evidence.

### Reporting trust

- **TRUST-01**: Backend, corpus boundary, reporting-through time, capture health and delivery progress must be distinguishable. Preserve last good results during refresh; a failed cloud read must not silently fall back to local.
- **TRUST-02**: Host load and lagged correlations remain available in System. Work comparisons show sample size, attribution coverage and denominators; unmerged PRs are not accepted output and commits without PRs remain visible.

### Non-Functional Requirements

Keyboard users must complete each task without a pointer. Views must reflow at
320 CSS pixels without page-level horizontal scroll; wide trace/table regions may
scroll locally. Existing incremental reports remain the source: opening a view
must not trigger collection, corpus scans or repricing. Compare cold/warm request
latency and background CPU against the installed baseline during implementation;
catch-up may use one core under the existing demand-driven PRD policy.

## User Stories

- [US-001 — Explain project spending](../user-stories/US-001-explain-spending.md)
- [US-002 — Inspect account capacity](../user-stories/US-002-inspect-capacity.md)
- [US-003 — Inspect a session](../user-stories/US-003-inspect-session.md)

## Edge Cases and Error Handling

Empty scope retains filters and offers a reset. Conflicting project bindings stay
unassigned. Cross-project sessions show allocated portions, never duplicate full
usage. Missing traces retain metadata and explain enrollment/expiry. Account-wide
billing denominators remain explicit when filters select only part of a period.
Cloud denial, outage and stale publications have separate states with recovery.

## Constraints and Assumptions

Preserve current collectors, independent reporter and stdlib installed runtime.
Use UX Builder for presentation development; no framework migration is implied.
The primary operator is the PRD's agent-heavy engineer. Four primary destinations
are a design decision inherited from the operator-approved IA proposal, not a usability finding.

## Dependencies

PRD; CONTRACT-001, CONTRACT-003, CONTRACT-004, CONTRACT-005 and CONTRACT-006.
FR-28/29/30/34 and FR-41/42/43/44 supply existing evidence and allocation behavior.

## Out of Scope

Changing capture, billing allocation algorithms, enrollment, export scope, retention,
authentication or central infrastructure. New account purchases/reset consumption.
Full redesign of project-rule and charge-entry forms; retain access to existing
administration during migration. This feature designs their placement.
