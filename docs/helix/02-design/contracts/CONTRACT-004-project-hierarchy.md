---
ddx:
  id: erg.project-hierarchy
  status: draft
  links: [prd, erg.architecture]
---
# Universal project hierarchy and reporting

## Problem and requirements

The operator requires sessions → folders or agent projects → human projects.
Current Costs independently resolves configured allocations, while Usage groups
folder hints and Work reports all retained PR/commit attribution. Their filters and
money presentation diverge. FR-18 and FR-41–45 govern this correction. The CPU
policy follows the PRD: low-rate use falls with demand, catch-up may use up to
one core, and representative record/byte capacity targets at least 10x arrivals.

## Shared data model and decisions

- Human-project IDs belong to the existing local master catalog. No names or
  customer-specific projects are hardcoded. Later Databricks master synchronization
  preserves these IDs and existing aliases; central export remains on hold.
- A folder is an observed machine-scoped absolute working directory; an agent
  project is an observed machine-and-agent-scoped native project ID or hint. Keep
  stable typed keys and display labels separate. Equal basenames on different
  machines/paths do not become the same folder or human project automatically.
- Session membership is temporal: use recorded context at each observation time,
  respecting worktree changes and ambiguous/missing context. A late folder record
  cannot backdate allocation. Never read the current filesystem or Git HEAD to
  classify historical usage. Session summaries may have several parents.
- Directory/repository rules provide inherited human-project allocation. Explicit
  session and agent-project bindings use the same versioned policy and priorities;
  no hidden specificity overrides conflicting rules. Preserve split weights and
  the unassigned remainder. Captured source project fields remain immutable.
- Extract the captured context resolver into shared reporting code; do not make
  Usage/Work depend on a Costs-only implementation. All metrics use the same
  resolver and project revision. Project policy remains independent of native
  capture, network IO and presentation.
- Allocate token amounts, value ranges and event/request equivalents by the same
  units. Distinct session/request counts are not additive across split projects.
  Deduplicate canonical observations before allocating. Unknown amounts remain
  unknown, with coverage counts; API-equivalent value, native reported estimates,
  subscription charges and actual billed spend remain distinct.
- Value each canonical request before hourly aggregation, preserving long-context
  tiers and cache-duration uncertainty. All views use the existing rate engine;
  never price a sum of unrelated requests as one long-context request.

## Reporting and UI interfaces

One shared filter context: range, human project, folder/agent project, machine and
agent. Lower-level session drill-down retains that context and labels full-session
versus allocated-window metrics. Usage shows token value beside tokens and offers
both metrics in charts/breakdowns. Work shows the same valued/allocated observations
beside commits and PRs, with input/attribution coverage. Costs becomes a concise
human-project → folder/agent-project → session rollup, not an independent date form.
Unknown billed totals are explained once rather than repeated as empty columns.

The common read model supplies catalog/folder metadata and preserves stable IDs in
filter values. Shared keys and effective allocation units join reports; labels and
free-text titles cannot join records. Existing portable query injection stays at
the reporter boundary. Cache keys include project revision and the full filter
context. A configuration edit invalidates calculated project views.

## Errors, privacy and retention

Unmatched/conflicting bindings remain unassigned with reasons. Missing folder
context does not prevent session capture. Changing project metadata cannot edit
source traces, usage identities, billing evidence or retention. Reports read one
snapshot and never hold writer locks during calculation. Central grants and content
retention policy are unchanged. No credential contents are read.

## Ordered implementation and verification

1. Share temporal context and hierarchy policy; add native-agent-project selectors.
   Verify machine/path identity, inherited directory rules, temporal moves,
   conflicting rules, weighted splits, partial/unassigned portions and renames.
2. Feed usage/event rollups and work attribution from that shared resolver. Verify
   conservation across human-project/folder/session levels and exact canonical
   deduplication; preserve source fields and dated per-request valuation.
3. Replace isolated Costs filters with the common context. Show cost and coverage
   in Usage and Work; simplify Costs to an expandable hierarchy and useful empty
   state. Test navigation, scoped requests, stale-response guards and missing prices.
4. Run `python3 -B -m unittest discover -s tests`, real local read-only parity and
   served-source/isolated-JavaScript checks. Deploy reporter independently, preserving
   collector/model processes. No browser visual claim without an actual permitted
   visual check. Capture final runtime/source evidence and remaining gaps honestly.

## Self-critique and risks

Do not treat PR output as AI authorship, feature count or billed savings. Filtering
usage must also scope input metrics rather than combining filtered tokens with all
historical inputs. Avoid duplicating sessions through split allocation. Legacy
commit metadata may lack a session or exact folder; use recorded repository
bindings where supported and otherwise retain unassigned evidence. An empty master
catalog is a useful first-run state, not permission to invent human projects.


## Implemented local interfaces

- `project_scope.Scope` owns temporal directory/repository context and hierarchy
  identity. `report_scope.Selection` validates shared filters. `project_dataset`
  builds allocated usage/input/edit/commit rollups, pricing each request before
  aggregation. Work and Costs use the same Scope and effective units.
- `project_key` is a human-project ID. `folder_key` is a typed observed resource
  key. `@unobserved` filters an evidence bucket; its constituent native sessions
  keep separate identities. It is not a claimed physical parent folder.
- Local `/api/data` supplies the shared allocated rollups and catalogs.
  `/api/effectiveness?view=work` accepts the same scope as `/api/costs`.
  `/api/history/sessions?scope=1` uses retained metadata only, groups each native
  session once and returns `through_seq` to protect pagination from appends.
  A project-revision change resets browsing; body drill-down remains full-session.
- The browser shares a stable hourly window anchor across these views, advancing
  it on explicit range changes/refresh. Server caches include project revision;
  stale browser replies cannot overwrite a newer revision or scope. Scoped
  browser caching is bounded to sixteen replies.
- `GET /api/projects` and JSON `POST /api/projects` administer local metadata.
  Actions are create or bind; bind has a concrete preview and explicit apply.
  Expected revision plus stable local mutation IDs prevent overwrite/retry
  duplication. Only project-owned tables are written. Unmatched/conflicting
  allocations remain visible after preview; priority is never silently invented.
- Metadata administration requires the SQLite backend, a loopback peer and Host,
  same-origin browser requests, bounded JSON input and revision validation. It is
  unavailable on the Databricks backend. This is a narrow exception to reporter
  read-only access: trace bodies, usage facts and billing evidence remain read-only.
- The portable central hierarchy and master/assignment sync remain future work
  under the private-destination hold. Local implementation does not establish
  remote parity or authorize central export.
