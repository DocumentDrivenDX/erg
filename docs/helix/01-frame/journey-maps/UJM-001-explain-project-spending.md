---
ddx:
  id: UJM-001
  type: user-journey-map
  authoring:
    home: repo
  links: [FEAT-002, US-001, US-003]
---

# UJM-001: Explain project spending

**Feature**: FEAT-002 | **Map kind**: future-state | **Evidence**: operator requests and current dashboard inspection; emotions are assumptions.

## Actor and Scenario

- **Person**: Agent-heavy engineer, reviewing work across native and gateway routes.
- **Task**: Explain project spending (`explain-spending`).
- **Scenario**: Weekly review starts with a spending question and ends with an evidence-backed explanation of consumption, expense and delivered work.
- **Trigger**: The engineer wants to know which project cost most last week.
- **End state**: They identify the largest contributor, inspect its sessions and work, and know which costs or associations remain unsupported.

### Goals and Expectations

Carry one time/project scope throughout. Compare token value with actual expense
without confusing the two. Reach original evidence without recreating the report.

## Journey Phases

| Phase | Actions | Thoughts | Emotion | Touchpoints | Stories / ACs |
|---|---|---|---|---|---|
| Form the question | Decide what period and work to review | What drove spending? (assumption) | neutral: curious (assumption) | Weekly review outside Erg | Outside product |
| Establish scope | Select last week and inspect totals | Am I seeing local or central data? (assumption) | neutral: cautious (assumption) | Reporting | US-001-AC1, US-001-AC4 |
| Find the contributor | Compare project cost and value | Does this include the subscription? (assumption) | neutral: questioning (assumption) | Project investigation | US-001-AC2 |
| Explain the work | Follow folders, sessions and verified output | What did this consume and produce? (assumption) | positive: informed (assumption) | Session/work evidence | US-001-AC1, US-003-AC1, US-003-AC3, US-003-AC4 |
| Qualify the answer | Inspect missing evidence and coverage | What cannot I conclude yet? (assumption) | neutral: deliberate (assumption) | Evidence gaps | US-001-AC3 |
| Decide what to change | Choose a workflow or plan adjustment | Can I justify the change? (assumption) | positive: confident (assumption) | Work planning outside Erg | Outside product |

## Insights

| Insight | Phase | Evidence |
|---|---|---|
| Costs separated from usage interrupts investigation | Find the contributor | Operator explicitly objected to isolated Costs and Work views |
| Repeated unknown rows obscure corrective work | Qualify the answer | Operator requested UI cleanup and fewer repeated unknown rows |
| Shared scope is necessary for trustworthy totals | Explain the work | PRD FR-18 and FR-25 |

## Opportunities

| Opportunity | Phase | Owner | Metric | Target |
|---|---|---|---|---|
| Preserve investigation scope | Establish scope | Erg maintainer | Scope resets needed to complete task | Zero in acceptance scenario |
| Co-locate cost and output evidence | Explain the work | Erg maintainer | Successful project-to-session-to-work walkthroughs | Observe during prototype review |
| Group evidence gaps by reason | Qualify the answer | Erg maintainer | Repeated gap rows before reaching a corrective action | Observe during prototype review |

## Story Coverage

All in-product phases map to the ACs above. Decisions about changing engineering
practice remain outside Erg; no recommendation engine or automated plan purchase
is implied. Navigation is defined in UF-002, not in this map.
