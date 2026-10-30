---
ddx:
  id: design-system
  type: design-system
  authoring:
    home: repo
  links: [prd, FEAT-002]
---

# DESIGN.md — Erg

## Modalities, Size Classes and App Frame

Screen modality. UX Builder classes: compact <600px, medium 600–839px, expanded
840–1199px, large >=1200px (extra-large shares large structure). Expanded/large
use a side navigation; compact/medium use wrapping navigation without hiding
primary destinations. Filters follow the heading; detailed filters are disclosed.

## Navigation and Active State

Overview, Projects, Sessions and Accounts are primary; System is secondary.
Project and session details use breadcrumbs. Work is investigated within its
project/session context. System contains Capture, Delivery, Reporting and Machine
sections. Existing project/charge administration stays reachable by labeled links.

| Surface | Component | Active cue | Semantic |
|---|---|---|---|
| Page destinations | AppShell navigation | Theme selection marker and emphasized label, bound to current destination | aria-current="page" |
| Detail ancestry | Breadcrumbs | Last item marked current | aria-current="page" |
| In-view sections | Document headings and anchor links | Focused heading on navigation | No false tab semantics |

### Destinations by Size Class

| Destination | Compact | Medium | Expanded / large |
|---|---|---|---|
| Four primary destinations | Wrapping navigation | Wrapping navigation | Side navigation |
| System | Secondary navigation link | Secondary navigation link | Separated secondary link |
| Detail ancestry | Parent Back link | Breadcrumbs | Breadcrumbs |

## Visual Hierarchy

Backend and reporting-through time sit beside the page title. Date, project and
machine form the first scope row. A summary contains tokens, rack-rate value,
effective expense and output; cost basis appears beside the figure. Then show
one breakdown and its drill-down. Reasons, provenance and configuration follow
in disclosures. Do not repeat full explanatory paragraphs above every table.
Account capacity is current observation data; historical usage follows the chosen
range. State those distinct scopes beside the account section headings.

## Interaction States

| State | Applies to | Convention |
|---|---|---|
| Hover / focus-visible | Enabled controls | SDK theme cue; keyboard focus remains visible |
| Disabled | Unavailable mutations | Native disabled semantics plus reason |
| Loading / slow | Async reports | Preserve previous publication; announce loading once |
| Empty | Filtered collections | Explain scope and offer Clear filters |
| Partial | Incomplete evidence | Known totals plus affected counts; expand grouped reasons |
| Error / offline | Failed source request | Retain scope, explain source, offer Retry |
| Permission denied | Governed cloud reports | Explain access requirement; Local switch is explicit |
| Success | Refresh completion | Updated-through time announced politely |

## Tokens

Versioned external references: @ux-builder/tokens 1.7.0 and @ux-builder/ui 0.14.0,
verified in the local SDK checkout. Use `plain` light for low-fidelity exploration;
proposed product appearance is `synaptiq` light/dark with system preference.
Brand/theme choice is reversible and does not govern the navigation review.

### Color

Use --piq-color-ground, --piq-color-surface, --piq-color-text,
--piq-color-text-muted, --piq-color-accent, --piq-color-border and status roles.
State is always named in text. Charts use the SDK series roles, not agent-specific literals.

### Spacing

Use --piq-space-1 through --piq-space-8. Page, Stack, Cluster and Grid own layout;
Dashboard owns summaries, CollectionList collections, RecordDetail details and
ReportViewer diagnostics. SDK components own their internal spacing.

### Type

Use --piq-font-body, --piq-font-heading, --piq-font-mono and corresponding size
roles. Session/commit IDs use mono; numeric columns align and preserve units.

## Non-Goals

Runtime architecture, data flow, component implementation internals and
architecture-significant decisions belong in architecture, contracts and technical
designs. SDK code generation is a prototype aid, never an application implementation.
