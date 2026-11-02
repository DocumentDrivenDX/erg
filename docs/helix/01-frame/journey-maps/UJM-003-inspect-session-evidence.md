---
ddx:
  id: UJM-003
  type: user-journey-map
  authoring:
    home: repo
  links: [FEAT-002, US-003]
---
# UJM-003: Inspect session evidence

**Feature**: FEAT-002 | **Map kind**: future-state | **Evidence**: operator requests and PRD; thoughts and emotions are assumptions.

## Actor and Scenario

- **Person**: Agent-heavy engineer, the PRD primary persona.
- **Task**: Inspect a session, from FEAT-002's Task Model.
- **Scenario**: The engineer wants to explain a costly or surprising session. They can read available trace content and explain usage, route, allocation and verified work without inventing a payer or PR match.
- **Trigger**: The engineer wants to explain a costly or surprising session.
- **End state**: They can read available trace content and explain usage, route, allocation and verified work without inventing a payer or PR match.

### Goals and Expectations

Follow one task to an evidence-backed answer; preserve scope and identify missing
or stale observations without reading repeated unknown rows. No purchase or reset
consumption is part of this task.

## Journey Phases

| Phase | Actions | Thoughts | Emotion | Touchpoints | Stories / ACs |
|---|---|---|---|---|---|
| Locate session | Find the session by title, ID, folder or date | Is this the right session? (assumption) | neutral: investigating (assumption) | Session inventory | US-003-AC1 |
| Explain consumption | Inspect cumulative tokens, peak context and allocated cost | Which context or route drove the usage? (assumption) | neutral: investigating (assumption) | Usage and route evidence | US-003-AC2 |
| Read the work | Read available inputs, tools and responses, then verified output | What happened and what was accepted? (assumption) | neutral: investigating (assumption) | Trace and commit/PR evidence | US-003-AC3, US-003-AC4 |
| Return to investigation | Go back to the prior project or session list | Can I continue without rebuilding filters? (assumption) | neutral: investigating (assumption) | Reporting scope | US-003-AC4 |

## Insights

| Insight | Phase | Evidence |
|---|---|---|
| Evidence must stay close to the answer | Inspect evidence | Operator requested account/cost attribution and trace drill-down |
| Missing and stale observations cannot establish certainty | Qualify the answer | PRD FR-14, FR-28/29/34 |

## Opportunities

| Opportunity | Phase | Owner | Metric | Target |
|---|---|---|---|---|
| Expose provenance after the primary answer | Investigate | Erg maintainer | Successful evidence walkthroughs | Observe in operator prototype review |
| Preserve scope when returning | Return | Erg maintainer | Filter reconstruction steps | Zero in acceptance scenario |

## Story Coverage

All in-product phases cite stable US-003 acceptance criteria above. Decisions
outside Erg are outside coverage. UF-002 owns navigation and view behavior.
