---
ddx:
  id: US-002
  type: user-stories
  authoring:
    home: repo
  links: [FEAT-002]
---

# US-002: Inspect account capacity

**Feature**: FEAT-002 — Unified reporting investigation
**Feature Requirements**: NAV-01/02, MEAN-01/02/04, TRUST-01
**PRD Requirements**: FR-18, FR-25
**Priority**: P0

## Story

**As a** agent-heavy engineer
**I want** to inspect account capacity
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

- [ ] **US-002-AC1** — Given observed account windows, when I open Accounts, then each account shows its plan, remaining window capacity, reset time and observation time independently of session-date filters.
- [ ] **US-002-AC2** — Given a recorded charge or paid reset, when I inspect account expenses, then billing periods and charge provenance are distinct from scheduled quota resets; absent receipts are explained.
- [ ] **US-002-AC3** — Given sessions with route and owner evidence but no verified payer, when I inspect account-linked usage, then unassigned usage remains visible and the UI explains why owner or route alone cannot prove billing.
- [ ] **US-002-AC4** — Given expired or unavailable account observations, when I inspect capacity, then stale evidence is labeled and no current availability is invented.

## Edge Cases

Empty results offer a filter reset. Failed requests retain scope and expose retry.
Denied cloud access never falls back silently. Synthetic prototype figures are
illustrative, never operational evidence.

## Test Scenarios

| Scenario | AC ID | Input / State | Action | Expected Result |
|---|---|---|---|---|
| 1 | US-002-AC1 | observed account windows | I open Accounts | each account shows its plan, remaining window capacity, reset time and observation time independently of session-date filters |
| 2 | US-002-AC2 | a recorded charge or paid reset | I inspect account expenses | billing periods and charge provenance are distinct from scheduled quota resets; absent receipts are explained |
| 3 | US-002-AC3 | sessions with route and owner evidence but no verified payer | I inspect account-linked usage | unassigned usage remains visible and the UI explains why owner or route alone cannot prove billing |
| 4 | US-002-AC4 | expired or unavailable account observations | I inspect capacity | stale evidence is labeled and no current availability is invented |

## Dependencies

FEAT-002; UF-002; existing ledger, accounting and project-allocation contracts.

## Out of Scope

Changing capture, pricing policy or subscription purchases. Prototype checks do
not establish production acceptance coverage; implementation must add behavioral
browser tests citing these AC IDs.
