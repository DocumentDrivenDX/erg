# PR 5 independent source reviews

The user authorized Codex and Claude reviews, fixes and merge after green CI on
2026-11-02. Both reviewers inspected an isolated checkout of PR 5, including the
LDP migration, unified reporting changes and their governing artifacts. They had
read-only source access; credentials, operator scripts and production data were
excluded. No production activation occurred.

DDx dispatch failed before either reviewer started because its process lifecycle
cleanup state was indeterminate. The authorized reviews then ran directly through
Codex (read-only sandbox) and Claude (restricted Read/Grep/Glob tools). No harness
pin was widened and no approval bypass was used.

| Round | Codex | Claude | Disposition |
|---|---|---|---|
| 1 | REQUEST_CHANGES | APPROVE with warnings to fix | Historical totals blocker; refresh, Back and stale-anchor findings |
| 2 | REQUEST_CHANGES | REQUEST_CHANGES | Complete presentation/date-control restoration and reject malformed anchors |
| 3 | APPROVE | APPROVE | No blocking findings or unresolved warnings |
| 4, post-merge follow-up | APPROVE | APPROVE | Background-load date-draft race corrected |

## Corrections

- `erg/static/index.html` applies both time boundaries before aggregating usage,
  value, breakdowns, heatmap and other scoped evidence. Explicit custom end wins
  over the default anchor, including daily buckets with later same-day records.
- `erg/static/unified.js` refreshes Overview allocations on explicit refresh and
  publication/catalog changes, and ignores stale overlapping responses/errors.
- URL restoration rebuilds scope and presentation controls, including date input
  values. Presentation changes save history state. Clearing filters resets the
  anchor and clears the custom-date form; absent or malformed anchors use now.
- `e2e/unified.spec.js` asserts numeric totals and boundaries, refresh results,
  Back restoration of project and presentation controls, date forms, invalid
  anchors and clear behavior. Current-time cases freeze Date. Longer independent
  journeys were separated while retaining the existing 30-second test timeout.
- `tests/ui_vm.js` supplies the minimal DOM operations needed by rebuilt controls;
  full DOM/navigation assertions remain real-browser tests.

## Accepted notes

The previous canonical writer must be paused and drained, with terminal-run and
empty active-run observations retained, before enabling the migrated job. Pausing
alone does not stop an active run. The README now makes those checks explicit.

The publisher pins the observed completed pipeline update across publication,
but its first observation does not independently map the update ID to the job's
pipeline-task invocation. Privileged manual updates during a run are excluded
by the documented single-writer activation profile. This is an activation scope
condition, not a distributed fence claim or a merge blocker.

Round 3's draft-clearing NOTE was initially accepted. A slower post-merge local
browser run exposed a real consequence: an asynchronous data response could
rebuild controls and close the date form while a user typed. The follow-up removes
form synchronization from ordinary rebuilds. It runs only for URL restoration,
Apply, Clear and explicit range changes. A regression deliberately holds the
initial data response, enters a start date, releases the response and verifies
the draft/open form survive before applying the end date. Existing validation,
Back and clear cases remain intact. Codex and Claude both approve this correction.
One Codex attempt could not inspect source because the prompt disallowed commands;
it produced no code finding. Clarifying permission for read-only file inspection
allowed the completed review; no incomplete approval was treated as evidence.

Claude's remaining NOTE concerns a pre-existing stale validation message after
explicit state restoration. It is non-blocking and unrelated to preserving draft
values or committed reporting scope.

Coverage evidence now follows the selected time/project scope consistently with
the other report sections. Native qualification remains the fabricated-data proof
recorded in `native-evidence.json`; source review does not replace native tests.

## Merge gate

Local validation and the PR's full quality, locked compiler and four Linux/macOS
Python 3.8/3.12 runtime jobs must pass on the final head. Merge preserves the
existing history with a merge commit. Merge does not activate production cloud
processing or alter installed runtime dependencies.
