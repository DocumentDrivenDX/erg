---
ddx:
  id: erg.contract.002
  authoring:
    home: repo
  links: [prd, erg.architecture, erg.contract.001]
---
# Local input capture and PR attribution

## Durable evidence

Claude and Codex inputs and repository, branch, head, session and explicit task
identities are tailed independently of broad usage scans. Complete JSONL records,
sanitized observations and the tail checkpoint commit in one transaction. Partial
lines are retried. Historical scans include compressed Codex archives. Pi lifecycle
snapshots retain input sizes and origin alongside usage so native-log pruning does
not erase the captured evidence. Existing Pi processes need a hook reload.

No input text, response text or tool arguments/results are persisted. Native
message identities deduplicate mirrored inputs; source observations remain intact.
Known origins survive later unenriched replays. Repository URLs exclude credentials.
Explicit task IDs are recognized only from anchored task markers or execution
commands, not arbitrary mentions in injected documentation.

Codex response items count only with explicit user content metadata; injected
context and history predating the session are excluded. Native CLI user-message
events count with unknown actor. Claude user text has unknown actor; sidechain
inputs are excluded. Interactive Pi input and main-session Codex user-text events
are interface proxies for human input, not proof of a physical actor. Extension
and resumed Pi input can have unknown actor. Counts are observed lower bounds.
Legacy unclassified Codex role records remain audit evidence but are excluded from
the canonical input projection. Raw history is never needed for reporting.

## Attribution

Definition version: `erg.work-effectiveness.v3`. Automatic attribution MUST use
one of these evidence paths:

- A successful paired `git commit` tool result, with its abbreviated SHA resolved
  unambiguously against local git objects, and that full SHA in the PR's current
  fetched commit membership. Reading a hash, viewing history, commit ancestry and
  the current checkout head MUST NOT be treated as proof of creating work.
- A successful paired `gh pr create` tool result containing the canonical PR URL
  in the same repository. Merely viewing/listing/mentioning a PR MUST NOT count.
- The previous branch-plus-recorded-head membership rule, confined to observations
  inside the PR lifetime. This fallback MUST NOT allocate pre-PR work.

Successful creation evidence MAY link earlier observations in the same contiguous
repository/worktree/branch/explicit-task segment, including before PR creation.
The segment begins at observed session/context entry or a recorded boundary;
there is no arbitrary time-nearness matching. A successful recorded branch switch
starts a new segment. A changed explicit task ID or native repository/worktree/
branch context also starts a segment. Two distinct PR candidates in one segment
MUST remain ambiguous, even if one was closed earlier. Observations after PR
merge/closure and creation evidence produced after closure MUST NOT be allocated.
Unmatched subagents remain unknown unless explicitly assigned through a parent.

Tool invocations/results MUST be paired by their native call ID. Supported shell
invocations are Claude Bash, Codex exec_command/write_stdin and literal single
executor calls wrapped in Codex functions.exec, plus correlated asynchronous shell/cell completion. Unsupported/dynamic wrappers,
multi-executor output without individual correlation, failed results, missing
local git objects and cross-repository activity MUST remain unknown. Shell text
MUST NOT execute during capture. Pending checkpoint state retains only sanitized
intent, repository/path and identifiers, never commands, bodies or results.
Codex tool history predating the session MUST be excluded when native timestamps
or metadata establish replay. Detected replay cannot create fresh work evidence.

`work_evidence` observations contain only `uid`, `ts`, `tool`, `session`, `repo`,
`scope_id`, `task_id`, `sha`, `pr_url`, `provenance` and `capture_version`. Provenance is `tool_commit`
or `tool_pr_create`. Tool evidence must have capture version 5 or newer; older
parser evidence remains retained but cannot allocate calls. An explicit Codex tool working directory in another worktree of the same
verified repository starts an observed work segment at invocation time. It MUST
NOT claim earlier native-context usage. Later native turns with an unchanged
startup directory MUST NOT erase that observed tool scope. Subdirectories of one
worktree share the tool worktree root. Unsupported cross-repository activity stays
unknown. Deleted native worktrees MAY resolve commit objects from one surviving
checkout with the exact recorded origin; its current HEAD is never used as a
historical attribution claim. Here-document bodies MUST NOT be interpreted as
executed shell commands.

Contexts carry `scope_id` and `capture_version`; version 3 contexts supersede older
parser contexts at the same native timestamp. Source
observations remain durable. Current PR commit membership MUST use the snapshot
matching the PR metadata timestamp, so removed force-push commits cannot keep
allocating usage from obsolete membership.

An operator can explicitly assign a native session, optionally scoped by inclusive
Unix timestamp bounds and a task identity. Recorded subagents inherit explicit
parent assignments within those bounds. Overlapping assignments to different PRs
are ambiguous; calls never contribute to two PRs. Unlink retracts an assignment.

PR metadata is fetched through read-only GitHub API calls for explicitly selected
repositories. A private local whitelist can schedule refreshes with broad scans.
Refresh also follows captured PR-creation URLs and commit-to-PR associations,
strictly inside the configured repository whitelist. Commit lookups are capped
by the configured limit per repository per refresh and cached for one hour;
up to 100 evidence/previously known PR targets are refreshed alongside recent PRs.
Missing or failed lookups MUST remain unknown. GitHub's read-only
[commit-to-PR endpoint](https://docs.github.com/en/rest/commits/commits#list-pull-requests-associated-with-a-commit)
is discovery evidence only; exact fetched PR commit membership still verifies
allocation. Lookup bounds and metadata timestamps are coverage boundaries, not
proof that all PRs were discovered. No comments or repository changes are sent to GitHub.

## Commit-first work attribution

A verified `work_commit` record has uid, ts (git commit time), repo, full sha,
filtered added_code, removed_code, files and provenance `git_object`. Commit identity
is repository plus full SHA. It MUST exist independently of PR metadata.

Session-to-commit links use successful paired commit output. Within one contiguous
work segment, usage can be assigned to the next observed commit boundary. Distinct
commits from one result/second are a batch whose per-commit usage remains unknown;
no equal splitting is invented. Calls after the last commit are not charged back
to that commit. A commit can have a verified session relation even when none of
that session's usage is allocatable. Missing metadata and ambiguous batches are
counted in coverage. PR grouping uses current fetched membership and must not
sum shared usage twice. No PR is not the same as no work.

## Reporting

The local per-PR view counts canonical allocated calls and input observations.
It exposes pre-opening call counts and up to 100 sanitized tool evidence references
per PR for audit. All-history chart independence and unknown costs are unchanged.
Total tokens sum fresh input, cache read, cache write and output; reasoning is
already a subset of output. Unknown human origin makes the human count unknown.
Missing input capture is not evidence of zero human intervention.

Accepted means merged. Added/deleted code is the final PR diff filtered by the
existing generated/document rules, only when file coverage is complete. Added code
per million captured tokens measures diff volume, not AI authorship, retention,
feature delivery or value. Partial usage can inflate it. Every usage allocation is
reported as partial; PR metadata alone does not establish usage completeness.

CONTRACT-003-accounting.md governs reported agent estimates, dated API-equivalent
reference value, subscription charges, account associations and quota history.
Absent billed evidence remains unknown. No feature count is inferred from PR count.
Chart date filters do not apply to the all-history PR view. Databricks effectiveness
reporting and central export remain outside this local increment.
