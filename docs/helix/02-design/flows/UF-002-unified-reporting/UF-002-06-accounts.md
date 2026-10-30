---
ddx:
  id: UF-002-06-accounts
  type: wireframe
  authoring:
    home: repo
  links: [UF-002, US-002, design-system]
---

# UF-002-06-accounts: Accounts

**User flow**: UF-002 view `Accounts` | **Stories**: US-002

## Scope and Fidelity

- **View**: Inspect remaining capacity, resets and expense evidence.
- **Modality**: screen.
- **Fidelity**: mid, functional regions and control behavior; prototype remains low-fidelity.
- **Size classes**: compact, medium, expanded / large (DESIGN.md).
- **Pattern**: UX Builder CollectionList, within AppShell and Page.

## Layouts

### Compact

```text
Navigation (wrap)
Title + backend / freshness
Scope (stack)
Report body (stack)
Evidence (disclosure)
```

### Medium

```text
Navigation (wrap)
Title + backend / freshness
Scope (wrap)
Report body (one column)
Evidence (disclosure)
```

### Expanded / large

```text
Navigation | Title + backend / freshness
           | Scope (row)
           | Report body (summary then breakdown)
           | Evidence (disclosure)
```

### Annotations

| Element | Kind | Annotation | Size classes | Links |
|---|---|---|---|---|
| Navigation | control | Go to Overview, Projects, Sessions, Accounts or secondary System | All | UF-002: Go to destination |
| Title + backend / freshness | content | Current title, Local/Databricks, reporting-through time and scope boundary | All | US-002-AC1 |
| Scope | control | Date/time, project and machine; disclose folder/model/agent/route; invalid ranges retain input with inline error and make no request | All | UF-002: Apply scope |
| Report body | region | Account cards/rows: plan, current windows and resets; selected-period usage/value/expense; grouped missing evidence | All | US-002-AC1 |
| Evidence | control | Expand evidence in place; collapse returns focus to summary; counts and next action precede raw details | All | FEAT-002: MEAN-02 |
| Apply scope | control | Update the report within this view; retain scope | All | UF-002: Apply scope |
| Refresh | control | Update the report within this view; retain scope | All | UF-002: Refresh |
| Retry | control | Update the report within this view; retain scope | All | UF-002: Retry |
| Clear filters | control | Update the report within this view; retain scope | All | UF-002: Clear filters |

## States and Events

### States

Account capacity observations retain their own observation time even when report
usage is filtered. Detail content expiry is a partial state, not an empty session.

| State | Applies | Entered when | Person sees | Available actions | Announcement | Exit |
|---|---|---|---|---|---|---|---|
| ideal | yes | Publication available | Current results | Refresh / Apply scope / navigation | Results ready (polite) | REFRESH |
| empty | yes | No matching records | No results for scope | Clear filters / navigation | No results for this scope (polite) | CLEAR |
| loading | yes | Request starts | Previous publication or skeleton | Navigation | Loading results (polite) | LOADED / EMPTY / PARTIAL / FAILED / OFFLINE / SLOW / DENIED |
| partial | yes | Evidence or publication incomplete | Known results and grouped reasons | Refresh / navigation / Expand evidence | Results incomplete; inspect evidence (polite) | REFRESH |
| error | yes | Request failed | Source-specific message and retained results | Retry / navigation | Results unavailable (polite) | RETRY |
| offline | yes | Backend unreachable | Last good results labeled offline | Retry / navigation | Reporting source offline (polite) | RETRY |
| slow | yes | Request exceeds ordinary response time | Still loading; last good results retained | Navigation | Still loading (polite) | LOADED / EMPTY / PARTIAL / FAILED / OFFLINE / DENIED |
| permission-denied | yes | Cloud authorization denied | Access explanation; no fallback | Retry / explicit backend change / navigation | Access required (polite) | RETRY |
| success | yes | Refresh completes | Updated-through timestamp and results | Refresh / navigation | Results updated (polite) | SETTLED |

### Statechart

```mermaid
stateDiagram-v2
  [*] --> loading
  ideal --> loading: REFRESH
  partial --> loading: REFRESH
  empty --> loading: REFRESH
  empty --> loading: CLEAR
  error --> loading: RETRY
  offline --> loading: RETRY
  permission-denied --> loading: RETRY
  loading --> slow: SLOW
  loading --> success: LOADED
  slow --> success: LOADED
  loading --> empty: EMPTY
  slow --> empty: EMPTY
  loading --> partial: PARTIAL
  slow --> partial: PARTIAL
  loading --> error: FAILED
  slow --> error: FAILED
  loading --> offline: OFFLINE
  slow --> offline: OFFLINE
  loading --> permission-denied: DENIED
  slow --> permission-denied: DENIED
  success --> ideal: SETTLED
```

### Events

| Event | From | Guard | To | Effect |
|---|---|---|---|---|
| REFRESH | ideal / partial / empty | None | loading | Keep scope and previous publication |
| CLEAR | empty | None | loading | Reset optional filters explicitly |
| RETRY | error / offline / permission-denied | None | loading | Retry the selected backend |
| SLOW | loading | None | slow | Keep navigation usable |
| LOADED | loading / slow | None | success | Publish one coherent result |
| EMPTY | loading / slow | None | empty | Show scope and reset |
| PARTIAL | loading / slow | None | partial | Show known results with gaps |
| FAILED | loading / slow | None | error | Keep scope and previous results |
| OFFLINE | loading / slow | None | offline | Identify unavailable source |
| DENIED | loading / slow | None | permission-denied | Expose explicit access recovery |
| SETTLED | success | None | ideal | Keep updated-through timestamp |

## Accessibility

- **Focus order**: skip link, navigation, title, scope controls, report actions, report body links, evidence disclosures.
- **Accessible names**: backend selector, date start/end, human project, machine, search and every action use visible labels. Tables have captions and header associations.
- **Headings and landmarks**: one main landmark and one page heading; titled report/evidence sections. Navigation focuses the new heading; background refresh never steals focus.
- **Target size and input**: SDK controls use documented target sizes; keyboard works throughout. Wide trace/table content scrolls locally at compact widths.

## Open Questions

None blocking the navigation prototype. Validate content density with the operator
before increasing visual fidelity; live acceptance requires the story assertions.
