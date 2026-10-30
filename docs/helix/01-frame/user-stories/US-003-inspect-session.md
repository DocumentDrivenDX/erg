---
ddx:
  id: US-003
  type: user-stories
  authoring:
    home: repo
  links: [FEAT-002]
---

# US-003: Inspect a session

**Feature**: FEAT-002 — Unified reporting investigation
**Feature Requirements**: NAV-02/03, MEAN-01/02/03, TRUST-01
**PRD Requirements**: FR-18, FR-20, FR-25
**Priority**: P0

## Story

**As a** agent-heavy engineer
**I want** to inspect a session
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

- [ ] **US-003-AC1** — Given captured sessions, when I search by ID, title, folder, project or date and open a result, then the session shows its title and full ID, time bounds, token totals and peak context with preserved scope.
- [ ] **US-003-AC2** — Given a session with multiple models, routes or projects, when I inspect usage and costs, then each allocated portion and its evidence remains distinguishable; mirrored observations do not increase canonical totals.
- [ ] **US-003-AC3** — Given retained trace content, when I open the session timeline, then ordered human inputs, responses and tool activity can be inspected with lazy pagination; unavailable or expired content has a specific explanation.
- [ ] **US-003-AC4** — Given verified work evidence, when I inspect linked work, then commit IDs and PR titles/state appear with evidence; uncertain matches remain unassigned and browser Back restores the prior investigation.

## Edge Cases

Empty results offer a filter reset. Failed requests retain scope and expose retry.
Denied cloud access never falls back silently. Synthetic prototype figures are
illustrative, never operational evidence.

## Test Scenarios

| Scenario | AC ID | Input / State | Action | Expected Result |
|---|---|---|---|---|
| 1 | US-003-AC1 | captured sessions | I search by ID, title, folder, project or date and open a result | the session shows its title and full ID, time bounds, token totals and peak context with preserved scope |
| 2 | US-003-AC2 | a session with multiple models, routes or projects | I inspect usage and costs | each allocated portion and its evidence remains distinguishable; mirrored observations do not increase canonical totals |
| 3 | US-003-AC3 | retained trace content | I open the session timeline | ordered human inputs, responses and tool activity can be inspected with lazy pagination; unavailable or expired content has a specific explanation |
| 4 | US-003-AC4 | verified work evidence | I inspect linked work | commit IDs and PR titles/state appear with evidence; uncertain matches remain unassigned and browser Back restores the prior investigation |

## Dependencies

FEAT-002; UF-002; existing ledger, accounting and project-allocation contracts.

## Out of Scope

Changing capture, pricing policy or subscription purchases. Prototype checks do
not establish production acceptance coverage; implementation must add behavioral
browser tests citing these AC IDs.
