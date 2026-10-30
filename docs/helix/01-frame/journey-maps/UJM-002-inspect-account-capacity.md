---
ddx:
  id: UJM-002
  type: user-journey-map
  authoring:
    home: repo
  links: [FEAT-002, US-002]
---
# UJM-002: Inspect account capacity

**Feature**: FEAT-002 | **Map kind**: future-state | **Evidence**: operator requests and PRD; thoughts and emotions are assumptions.

## Actor and Scenario

- **Person**: Agent-heavy engineer, the PRD primary persona.
- **Task**: Inspect remaining capacity, from FEAT-002's Task Model.
- **Scenario**: The engineer is choosing whether to continue work on the current account. They know which observed windows remain available, reset timing, evidence age and any missing billing evidence.
- **Trigger**: The engineer is choosing whether to continue work on the current account.
- **End state**: They know which observed windows remain available, reset timing, evidence age and any missing billing evidence.

### Goals and Expectations

Follow one task to an evidence-backed answer; preserve scope and identify missing
or stale observations without reading repeated unknown rows. No purchase or reset
consumption is part of this task.

## Journey Phases

| Phase | Actions | Thoughts | Emotion | Touchpoints | Stories / ACs |
|---|---|---|---|---|---|
| Identify account | Find the account used by the workload | Is this the account paying for my sessions? (assumption) | neutral: investigating (assumption) | Current account observations | US-002-AC1, US-002-AC3 |
| Inspect availability | Read each window, observation time and next reset | How long can I continue? (assumption) | neutral: investigating (assumption) | Quota observations | US-002-AC1, US-002-AC4 |
| Explain spend | Distinguish plan fees, usage and purchased resets | Was a reset purchased or only scheduled? (assumption) | neutral: investigating (assumption) | Charge and reset evidence | US-002-AC2 |
| Decide next step | Choose whether to wait, switch routes or investigate missing evidence | Is this evidence current enough? (assumption) | neutral: investigating (assumption) | Engineering planning outside Erg | Outside product |

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

All in-product phases cite stable US-002 acceptance criteria above. Decisions
outside Erg are outside coverage. UF-002 owns navigation and view behavior.
