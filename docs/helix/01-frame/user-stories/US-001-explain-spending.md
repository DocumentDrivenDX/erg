---
ddx:
  id: US-001
  type: user-stories
  authoring:
    home: repo
  links: [FEAT-002]
---

# US-001: Explain project spending

**Feature**: FEAT-002 — Unified reporting investigation
**Feature Requirements**: NAV-01/02/03, MEAN-01/02/03, TRUST-01/02
**PRD Requirements**: FR-18, FR-20, FR-25
**Priority**: P0

## Story

**As a** agent-heavy engineer
**I want** to explain project spending
**So that** I can make cost and capacity decisions from evidence.

## Context

Reports share scope and allocation. Missing evidence must remain distinguishable
from zero usage or zero expense. This story changes presentation, not accounting policy.

## Walkthrough

1. Open the relevant primary destination with the current scope.
2. Select the account, project or session of interest.
3. Inspect the summary, then follow the underlying evidence.
4. Return to the originating investigation with scope preserved.

## Acceptance Criteria

- [ ] **US-001-AC1** — Given a populated report, when I select a period and project and open its details, then the same backend, scope, allocation revision, token total and cost bases remain visible through folder, session and work drill-down.
- [ ] **US-001-AC2** — Given subscription and metered usage, when I compare expense and rack-rate value, then separate values identify period, allocation basis, coverage and estimate ranges without summing rack value into expense.
- [ ] **US-001-AC3** — Given missing rates, charges or project links, when I expand the gap summary, then affected counts, reasons and available corrective actions are shown; excluded amounts never become zero.
- [ ] **US-001-AC4** — Given a stale, warming or unavailable report, when I open or refresh the investigation, then the last completed results and reporting-through time stay labeled, or a recoverable unavailable state preserves scope.

## Edge Cases

Empty results offer a filter reset. Failed requests retain scope and expose retry.
Denied cloud access never falls back silently. Synthetic prototype figures are
illustrative, never operational evidence.

## Test Scenarios

| Scenario | AC ID | Input / State | Action | Expected Result |
|---|---|---|---|---|
| 1 | US-001-AC1 | a populated report | I select a period and project and open its details | the same backend, scope, allocation revision, token total and cost bases remain visible through folder, session and work drill-down |
| 2 | US-001-AC2 | subscription and metered usage | I compare expense and rack-rate value | separate values identify period, allocation basis, coverage and estimate ranges without summing rack value into expense |
| 3 | US-001-AC3 | missing rates, charges or project links | I expand the gap summary | affected counts, reasons and available corrective actions are shown; excluded amounts never become zero |
| 4 | US-001-AC4 | a stale, warming or unavailable report | I open or refresh the investigation | the last completed results and reporting-through time stay labeled, or a recoverable unavailable state preserves scope |

## Dependencies

FEAT-002; UF-002; existing ledger, accounting and project-allocation contracts.

## Out of Scope

Changing capture, pricing policy or subscription purchases. Prototype checks do
not establish production acceptance coverage; implementation must add behavioral
browser tests citing these AC IDs.
