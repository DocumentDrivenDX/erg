# erg — agent session and productivity monitor

Part of the DocumentDrivenDX family. MIT OR Apache-2.0. Python 3.8+, standard
library only. Collection, local storage and reporting work offline. Databricks
centralization is optional. The local web application can report on the Databricks corpus through the same
incremental reporting and project-allocation stack as the local ledger. Cloud
evidence remains limited to the record classes actually delivered.

## Install and first run (0.1.0rc1)

Python 3.8+ on macOS or Linux; no third-party runtime dependencies. Automatic
background service installation supports macOS. On Linux use the foreground
commands, cron or your own service manager.

From a downloaded release wheel (replace the path with your artifact):

```sh
python3 -m venv .venv
.venv/bin/python -m pip install /path/to/erg-0.1.0rc1-py3-none-any.whl
.venv/bin/erg sources
.venv/bin/erg collect
.venv/bin/erg serve
```

Open <http://127.0.0.1:8787>. Stop the foreground reporter with Ctrl-C.
`collect` runs once; repeat it or install the macOS service for continuous capture.
An empty agent history produces an empty dashboard. Collection and reporting work
without Databricks or a cloud account. Installing the package starts no services.
To install from a source archive or checkout, use `python3 -m pip install .` in
its extracted directory. To upgrade, install the replacement artifact in the same
environment, then reinstall any managed services to refresh their runtime copies.
Keep your ledger and state directory when upgrading.

The release candidate covers local collection and reporting. Central delivery,
cloud reporting and fleet enrollment remain experimental; see [central](central/README.md).
Automatic subscription billing may be unavailable; unknown charges stay unknown.
Source coverage and platform limits are described below.

## Privacy at first run

Default collection stores usage and identifying metadata locally; it does not
retain prompt/response bodies or automatically upload telemetry. Account discovery
may invoke installed agents and make read-only provider account/billing requests
using existing logins. GitHub enrichment may make authenticated metadata reads.
These requests are distinct from exporting the ledger. The local reporter binds
to loopback by default and has no authentication: keep it on loopback.

`erg history enroll` explicitly enables forward body retention. Enrolled content
can contain secrets typed into prompts or tool output. No automatic content expiry
is enabled. Read [PRIVACY.md](PRIVACY.md) before collecting or enrolling,
sharing a ledger or configuring exports; it covers storage, network activity,
expiration and removal. Contributor rules follow the same policy.

## Local collection and reporting

```sh
erg sources                         # distinguish installed agents from capture history
erg collect                         # capture one local cycle; enroll for forward bodies
erg serve                           # independent UI at http://127.0.0.1:8787
erg collect --interval 900           # polling loop; host sampling every 30 seconds
```

`erg collect` without an interval runs once for cron. Install the package with
`pip install .`, or invoke `bin/erg` from the checkout. The reporter never starts
collection. Continuous mode can be managed by launchd or systemd; cron invokes the
one-shot command. Run only one collector per enrollment/state directory.

After `erg history enroll`, one-shot collection archives forward session records
and durably queues usage projection before optional enrichment. Repeated runs and
mirrored native entries do not add duplicate calls. Without enrollment, collection
keeps its metadata/usage behavior and does not enable body capture. An enrolled
state directory must point at its enrolled ledger. Capture errors return a failed
command before an explicitly requested export.

For continuous capture, use `erg service install`: filesystem watching, lifecycle
hooks and the direct inference listener belong to that service. Cron can capture
persisted transcripts and already-installed lifecycle outboxes; it cannot recover
records removed before its next run or listen for direct ds4 requests between runs.

The unified ledger defaults to `~/.local/share/erg/erg-v2.db`, preserving the
previous `erg.db`. `ERG_HOME` changes the ledger directory; `ERG_DATABASE` selects
an exact database path. `ERG_STATE_DIR` changes sampler/enrollment state. Keep the
persisted `machine-id` with that state when upgrading. The reporter opens the
ledger read-only, so collect before first serving.

Discovery covers native Claude/Codex homes, their environment overrides, isolated
Omnigent Codex homes, Claude observer transcript paths, and Pi session directories.
Omnigent conversation metadata links to native session identities; its mirrored
messages and cumulative summaries never add duplicate calls. If a bridge history
has been pruned, erg retains a metadata-only session and any recorded usage
summary. The dashboard labels this `summary_only`; call charts exclude those
summaries. Missing usage stays `unknown`.

Optional `~/.config/erg/discovery.json` adds roots without changing parsers:

```json
{"codex_homes":["/path/to/codex-home"],"claude_homes":["/path/to/claude-home"],"pi_session_dirs":["/path/to/pi-sessions"]}
```

`ERG_DISCOVERY_FILE` selects another config file; `omnigent_home` in that file
selects an alternate Omnigent data directory. Existing `CODEX_HOME`,
`CLAUDE_CONFIG_DIR`, `PI_CODING_AGENT_DIR`, `PI_CODING_AGENT_SESSION_DIR` and
`OMNIGENT_DATA_DIR` overrides are honored. Session discovery reads selected metadata fields rather than credential files.
The separate account/billing probe can read an existing Codex file-backed login;
see [privacy](PRIVACY.md). It does not start inference or import Omnigent/Pi
runtime packages. Native transcript parsing, not provider registration, establishes
capture coverage.

OpenCode SQLite history has fixture and real isolated-session verification, including
source pruning. Legacy OpenCode JSON currently supplies usage only. Copilot persisted
events have fixture coverage; no real local Copilot session was available. Copilot
normally emits per-call usage ephemerally, so absent usage stays unknown.
Compressed Codex archives and compressed Omnigent metadata require the external
`zstd` decoder. Include it in the cron/service PATH; ordinary JSONL collection
has no such dependency.

## Universal project allocation and costs

Quota and reset reporting maintains incremental deduplicated counters and latest
window pointers in the local ledger. Initial metadata-only catch-up preserves
all source events; partial summaries use the raw reference until ready. The
reporting status includes its backlog.
Use `python3 scripts/verify-quota-summary.py --output /tmp/erg-quota-parity.json`
for an explicit read-only comparison with the full quota-history reference.

Usage and Projects & costs read durable incremental rollups. Opening or refreshing
an activated reporter never reprices the corpus. The collector rebuilds dirty
hours from one pinned snapshot and publishes each completed batch atomically.
Initial warming, pending changes and the previous allocation revision are explicit;
the last completed batch remains usable while a later batch builds. Exact partial
hour cost filters use retained priced facts rather than reading source sessions.
The reporter also reuses immutable hour payloads across publications, decoding
only changed hours (a bounded 1,024-hour cache). Forced refresh still reloads
current report metadata. Retired fact versions drain in short 256-row transactions
under the same projection budget, preventing repeated rewrites from accumulating
obsolete data.
Resource use follows demand. The managed service drains usage, edits and work
in one independent worker, targeting roughly 90% thread CPU during catch-up and
sleeping when empty or retry-blocked. Reporting yields to pending corpus projection,
then uses the available catch-up allowance. The operator permits up to one core
while catching up; the old 10% ceiling must not stretch a backlog across hours.
The capacity target is at least 10x representative record and byte arrivals,
including bursts and recovery. Process-tree measurement and return-to-idle behavior
remain acceptance checks; these are cooperative budgets, not an OS hard cap. A policy or rate-card change reprojects retained local metadata, without
importing additional upstream history. Crash staging is invisible and cleaned in
bounded chunks. Legacy ledgers without an activated projection use reference reads.

Sessions roll up to recorded **folders or agent projects**, then through explicit
bindings to **human projects**. Usage, Work, Projects & costs and Session history
share the same human-project, folder, machine, agent and time context. Source
folder/project hints stay captured evidence; they never become business projects
through name matching. A session can contribute to several folders or human
projects over time. Splits preserve the same shares of tokens, input/edit/commit
activity and value. Unmapped or conflicting portions remain unassigned.

Usage shows estimated token value beside tokens, including the same breakdown
and chart filters. Work values the selected canonical observations and scopes
input equivalents to them. Whole-PR diff sizes remain separate source evidence.
Projects & costs drills from human project to folder to session, using the shared
filters. Unknown folder evidence has one display bucket while native session
identities remain distinct. Opening a session shows its full retained history and
labels full-session versus selected-window evidence.

The human-project catalog starts empty. In **Projects & costs → Human projects &
folder assignments**, create a project, choose an observed folder/agent project,
preview the effect on retained usage, then apply. These loopback-only actions
change versioned project metadata, not traces or billing evidence. Project edits
invalidate the shared reports. The master catalog can later synchronize from
Databricks while retaining stable identities.

Reference value, native reported estimates, actual billed usage and subscription
fees remain separate. Unpriced requests retain explicit coverage; missing amounts
are never silently counted as free. Pricing is applied to each canonical request
before hourly grouping, preserving context tiers and cache-duration ranges.
Advanced splits, priorities, effective dates and resource combinations use the CLI:

```sh
erg projects export > projects.json
# Retain version/revision, stable project identities and aliases.
erg projects preview --file projects.json --start 0
erg projects import --file projects.json --apply
```

A binding in the `bindings` array has this shape (replace illustrative values):

```json
{
  "binding_id": "repository-default",
  "priority": 10,
  "selectors": {"repository": "github.com/example/product"},
  "allocations": [{"project_key": "local:KEY_FROM_CREATE", "units": 1000000}],
  "valid_from": 1792540800,
  "valid_to": null
}
```

Selectors support a canonical `repository`, a machine-scoped
`directory: {"machine":"MACHINE_ID","root":"/absolute/path"}`, or a native
`session: {"machine":"MACHINE_ID","tool":"pi","id":"SESSION_ID"}`.
An observed agent project can use
`agent_project: {"machine":"MACHINE_ID","tool":"pi","id":"OBSERVED_PROJECT_ID_OR_HINT"}`;
this is an explicit machine/agent-scoped selector, not a guessed directory.
Combined selectors all have to match. Use the machine ID shown in erg; directory
rules use recorded full paths, not folder-name guesses or current configuration.
Missing historical context leaves matching unresolved. Metadata from retained
forward records can be indexed using `erg projects index-context`; no upstream
history is imported. New capture indexes it continuously after upgrading capture.

Intervals are UTC epoch seconds with an exclusive end. Highest explicit priority
wins; equal-priority rules with differing allocations remain ambiguous. Shares use
integer millionths: 600000/400000 is a 60/40 split. A remainder stays unassigned;
shares cannot exceed one request. Preview/import without `--apply` never changes
configuration. Imports reject conflicting stale edits, keep revision history and
make exact retries idempotent. Rename a project without changing its key. To retire
it, use `state: "archived"` and an explicit `archived_at` epoch; older allocations
remain intact. Keep existing project identities instead of deleting their rows.

Project configuration is local metadata with independent retention; it does not
rewrite traces or consume subscription entitlement. Later sync will download the
Databricks master project list and publish assignment revisions. That exchange,
interactive UI binding edits, verified task/commit/PR selectors, and fixed-charge
allocation are not implemented in this first slice. Central export stays paused.

## Background local dashboard on macOS

```sh
erg reporter install --port 8788     # independent user LaunchAgent
erg reporter status                  # live job plus matching HTTP health
erg reporter stop                    # stop now; starts again at next login
```

The reporter runs independently of the collector and ds4. It opens the local ledger
read-only, binds to `127.0.0.1`, and starts at login or after a crash. Installing or
restarting it does not start collection, inference or Databricks export. The first
installation defaults to port 8787; subsequent installs preserve the configured
port unless `--port` is supplied. An occupied port belonging to another listener is
left untouched; stop a manually launched reporter before enrolling that port.

Each install creates a private immutable reporter runtime under the erg state
directory. A failed upgrade restores the previous service configuration; successful
upgrades keep the previous runtime for recovery. `reporter.out` and `reporter.err`
are private local logs. Automatic lifecycle installation currently supports macOS
launchd; `erg serve` remains available on other platforms. The collector and reporter
are upgraded separately: use `erg service install` and `erg reporter install`.

## Continuous capture on macOS

```sh
erg service install                 # install Pi lifecycle hook and start launchd collector
erg service status                  # heartbeat, latest capture outcomes and snapshot count
erg service stop                    # stop the current launchd job
```

The user LaunchAgent `dev.ddx.erg.collector` starts at login and restarts after a
crash. On macOS, native filesystem events queue changed transcripts and lifecycle
outboxes for capture. With forward content capture enabled, retained events drive
usage projection; the collector does not run a second usage scraper every 30 seconds.
A two-second worker cadence drains events, with a full source reconciliation every
five minutes and an explicit polling fallback if native events are unavailable.
The host sampler runs every 30 seconds, reading macOS CPU ticks directly from
Mach instead of launching `top`. Repository and performance enrichment runs
independently every 15 minutes, with its first run deferred by that interval.
With enrolled content capture, it reads retained source checkpoints and project
directories instead of reopening Claude, Codex, Pi or Omnigent histories. Copilot
and OpenCode keep their legacy usage adapters until equivalent projection exists.
`--poll` and `--interval` customize worker and enrichment cadences.
Dropped filesystem events request reconciliation; they cannot restore source
records deleted before capture. Native watching currently supports macOS only.
The service installs a private runtime copy under the erg state directory; rerun
`service install` after upgrading erg. Automatic installation currently supports
macOS launchd. It does not enable central export.

Installation also creates the private executable
`~/.local/share/erg/bin/erg` (or `ERG_STATE_DIR/bin/erg`). Use that absolute path
for cron or shells without an installed `erg` command. It loads the managed runtime
independently of the checkout/current directory, preserves nonsecret discovery
settings, and defaults to the installed state directory and ledger. Explicit
environment overrides still work. Installation does not change shell PATH.
Do not schedule cron against a ledger whose collector service is running: both
modes share the collector lock.

Pi lifecycle hooks save sanitized snapshots after turns, before tool execution,
and at session start/switch/compaction/shutdown boundaries. These hooks run after
Pi has assigned native entry IDs; collecting both a snapshot and its original
transcript counts each call once. Snapshots are atomically replaced, flushed to
disk, mode 0600 in a mode 0700 directory, and contain identifiers and token usage,
never message bodies, tool arguments or credentials. Independent hook instances
write independent snapshots so a stale resumed session cannot erase another
process's capture. Snapshot retention is local and has no automatic expiry yet.

The installer appends an absolute extension path to Pi settings while preserving
existing extensions and retaining a private backup. Omnigent inherits that path
when creating managed Pi directories. **Already-open Pi sessions need `/reload`**
to activate the hook; new sessions load it automatically unless extensions are
explicitly disabled. Fast polling remains a fallback, with a polling gap that
hooks close. Uncatchable process termination before a completed boundary can
still lose observations that were never persisted.

Capture health and PR enrichment health are reported separately. A fresh collector
heartbeat cannot hide a stalled native-usage, context, sampling, or account worker;
worker errors and overdue successful runs degrade capture health. PR API failures
remain visible as enrichment errors, with sanitized repository/status diagnostics.
Remote commits absent from an accessible repository remain unresolved attribution
gaps, retried after one hour. They never establish PR membership or count as a
collector failure. Authentication, rate-limit, validation and server failures
still report errors.

Logs and `collector-status.json` live under the erg state directory. Service
health and snapshot counts appear in the dashboard, which refreshes every 30
seconds. The installer disables the recognized old `dev.ddx.erg.sampler` job to
avoid two samplers writing the same state; its plist is preserved with a
`.disabled` suffix. Stopping the collector keeps its installed LaunchAgent, so
it starts again at the next login. To disable autostart, move its plist out of
`~/Library/LaunchAgents` after stopping. To restore the previous sampler, rename
its `.plist.disabled` file back to `.plist` and bootstrap that job.

## Centralize in Databricks

The selected deployment target is production, in a new private `erg` catalog
and dedicated schema. **Central export and app deployment are paused** until
the production profile, namespace and required permissions are available.
Do not use a developer-readable catalog as a fallback. Verify effective access,
including inherited grants, before running the commands below. Provisioning
requires a prepared namespace; it does not create catalogs/schemas, change
existing gateway tables or grant privileges.

```sh
erg provision --profile PROFILE --warehouse WAREHOUSE --namespace CATALOG.SCHEMA
erg push --profile PROFILE --warehouse WAREHOUSE --namespace CATALOG.SCHEMA
erg collect --interval 900 --push --profile PROFILE --warehouse WAREHOUSE --namespace CATALOG.SCHEMA
erg serve --backend databricks --profile PROFILE --warehouse WAREHOUSE --namespace CATALOG.SCHEMA
```

The implemented upload interface is SQL Statement Execution
(`POST /api/2.0/sql/statements`) against a selected SQL warehouse, with upserts
into the erg-owned `CATALOG.SCHEMA.erg_observations` Delta table. It exports
content-free observation snapshots, not the compressed `history_events` corpus.
Full message/tool-body delivery, project catalog/binding exchange, complete
account/subscription/quota sync and the newer incremental reporting stack still
need central implementation and live verification. Current project-cost reporting
explicitly requires SQLite. The existing Databricks Apps/backend path is a partial
integration, not parity with today's local dashboard. This is separate from
Gateway inference logging; erg does not write into Databricks-managed inference
tables or pretend reconstructed spans are native OTLP submissions.

Collection commits locally before export. Export uses bounded, parameterized SQL
MERGE batches and advances its destination cursor only after success. Network
failures leave snapshots pending for replay. Cursors are bound to the actual central
table ID; recreating a table causes a fresh export. `erg push --replay` repairs
missing central snapshots from retained local data. Defaults: 5000 records, capped at
2 MiB encoded content per batch; `--batch-size` accepts 1..10000 records.

`erg_observations` retains sanitized source snapshots. Typed views expose the
sessions, calls, work and metrics. `erg_inference_calls` aligns inference-request
fields while leaving unknown latency/status and request/response bodies NULL.
`erg_spans` provides reconstructed OpenTelemetry-style attributes and identifiers;
it does not claim to be complete native OTLP wire spans when timing is unknown.
`erg_hourly_usage` and `erg_daily_usage` support SQL/notebook reporting.

Central reports combine erg observations with `system.ai_gateway.usage`; the
querying identity or view owner needs access to that source. Shared request IDs
reconcile unambiguous gateway/local pairs. Otherwise both sources remain visible,
with an overlap notice; token/time similarity never silently removes calls.

## Run the dashboard on Databricks

```sh
python3 scripts/deploy-databricks.py --profile PROFILE --warehouse WAREHOUSE --namespace CATALOG.SCHEMA
```

The deployment packages the same Python reporter and static UI into a new `erg`
Databricks App. The app uses the signed-in user's forwarded token with read-only
SQL scope and their existing Unity Catalog/warehouse permissions. It does not
need to bind a warehouse resource or retain a personal token. First access
requires Databricks sign-in and SQL consent. The app remains private; the script
does not expand app access or alter existing catalog permissions. Existing app
configuration must already support that user-authorization mode when redeploying.

The root `app.yaml` has no destination defaults: namespace and warehouse must
be supplied explicitly. The deployment script generates configuration for the
verified production namespace and warehouse; it uploads code, never the local
ledger. Do not run deployment while the destination permission hold is active.

## Reporting and data quality

The existing hour/day, project, model, agent, machine, requester and token-bucket
pivots remain. Session capture coverage and session-call drilldown add source
health, surface, parentage and unknown-usage visibility. The recent-session list
is capped at 200, call drilldown at 1000; the UI/API disclose those limits.

Machine rollups retain observed coverage and prefer well-covered sources before
source priority. Active minutes use measured coverage. Git commit sightings and
copied call identities count once across machines. Agent edit activity remains
separate from committed work. Costs remain explicitly unknown until a dated price
ledger is applied; token counts and line volume are not claims of delivered value.

Telemetry adapters are described in `docs/adapters.md`. `erg sync` remains
available for configured SSH hosts and gateway reads. `erg add-host`,
`erg add-gateway` and `erg sampler` retain their explicit setup roles.

## Privacy and development

Default collection measures prompt sizes without retaining bodies. Explicit history
enrollment retains forward message/tool content locally; separately enabled export
can transmit enrolled content. Transport credential fields are excluded, but secrets
inside enrolled content may remain. Account and cloud clients use existing logins
as described in [PRIVACY.md](PRIVACY.md).

```sh
python3 -m unittest discover -s tests -v
```

The governing design and contracts live under `docs/helix/`. Live delivery and
deployment evidence belongs beside the implementation plan, with limitations
stated separately from verified behavior.

## Local input capture and PR effectiveness

The collector service tails Claude/Codex input sizes and task/git context every
fast poll. Pi's lifecycle hook retains input sizes and origin in durable snapshots;
reload existing Pi sessions to activate the updated hook. No message text is saved.
Injected Codex context and replayed history do not count as input. Human origin
is an interface proxy where available and otherwise remains unknown.

```sh
erg work scan --full                  # backfill native input/context evidence
erg work refresh --repo github.com/OWNER/REPO --limit 50 --save
erg work link --tool codex --session SESSION --pr https://github.com/OWNER/REPO/pull/123 --task TASK
erg work unlink --tool codex --session SESSION --pr https://github.com/OWNER/REPO/pull/123
```

Refresh is read-only against GitHub using the existing `gh` login. `--save` keeps
the repository whitelist in `~/.config/erg/prs.json` for broad service scans.
Assignments accept `--from-ts` and `--to-ts` inclusive Unix timestamp bounds.
Conflicting assignments stay unassigned. Explicit assignments can cover recorded
subagents through their parent session.

The local dashboard's per-PR view shows allocated tokens, observed input counts,
merged final code additions/deletions and attribution coverage across all history.
Automatic links use successful paired commit/PR creation tools plus verified
repository and git evidence. Creation evidence can cover earlier work in the same
repository/worktree/branch/task segment before the PR opened. Conflicting PRs stay
unknown. The older branch-and-recorded-head rule remains limited to the PR lifetime;
branch names alone never allocate usage. Read-only metadata refresh follows captured
PR URLs and bounded commit-to-PR lookups inside the saved repository whitelist.
Session details show sanitized evidence and pre-opening counts. The coverage
details list unassigned sessions for explicit linking. Missing inputs, attribution
and all costs remain unknown. Diff volume per captured token is partial evidence,
not AI authorship or feature value. See
[the input and attribution contract](docs/helix/02-design/contracts/CONTRACT-002-input-attribution.md).

## Session lengths, accounts and subscription spend

The local work view now starts with verified commits, including work without a PR.
Commit-boundary usage is assigned once; same-result commit batches remain unknown.
Explicit Codex tool working directories start new scopes and cannot claim earlier
work in a different checkout. A deleted worktree can use matching-origin git
objects for SHA verification. The surviving checkout's HEAD is never a substitute
for the historical evidence.

Session browsing shows cumulative observed processed tokens, peak request input,
and dated standard API-equivalent value. These totals cover all canonical calls,
independently of the 1,000-row detail cap. Child sessions are separate. Agent-reported
session/message estimates are separate from reference value and actual charges.
Unknown cache-write duration produces a value range. Unpriced models stay unknown.
The included `erg/rate_card.json` is the default dated offline price snapshot in
both checkouts and installed packages. An explicitly enabled shared rate snapshot
takes precedence. `ERG_PRICE_LEDGER` selects a complete JSON catalog or supported
YAML ledger explicitly; the authored `catalog/prices.yaml` is not auto-discovered.
When editing the authored ledger/overrides, regenerate the bundled snapshot before
building (see CONTRIBUTING). Reference value is not a historical invoice.

Account and plan discovery is dynamic. Codex creator account IDs come from local
thread metadata and are labeled as ownership, not proven billing. A read-only local
Codex account probe observes current account identity, quota windows, scheduled
resets and available reset credits every five minutes for discovered installations.
It never starts inference, logs in or consumes resets. The native account RPC uses
its existing authentication; the separate billing reader may read the file-backed
Codex login and use its token in memory for allowlisted provider GET requests.
Within one poll, installations proven to use the same unchanged file credential
source and supported auth settings share one fresh RPC. Alias identities are labeled
`codex_shared_account_read`; quota windows are recorded once. Credential file metadata is used for this RPC-sharing decision.
The separate billing reader uses credential contents in memory, never as report data. Keyring/auto storage, managed policy, unsupported config,
failed RPCs and changing metadata fall back to independent probes. A shared current
account snapshot does not establish which account paid for a session. Probe counts
are exposed in collector status as `account_probe_stats`.
Native logs retain quota changes and explicit limit-hit events. Claude's SessionStart
hook reads the actual caller's auth metadata; custom gateway and unidentified auth
remain unknown. Existing hooks are preserved and backed up. Current profile snapshots
are not backdated. IDs/emails are pseudonymized before storage (not anonymized).

```sh
erg accounts status                 # observed accounts, quotas and billing periods
erg accounts import --file metadata.json
```

The version-1 metadata object accepts `identities`, bounded `bindings` and `charges`
arrays as billing/association evidence becomes available. A charge needs a stable
`charge_id`, provider, account ID or email, `subscription_id`, period start/end and
amount/currency. Repeated imports update one charge. No recurring fee is guessed
from plan names. Non-USD charges retain their currency; conversion stays unknown.
Creator-only identity does not allocate subscription fees. An explicit session/period
binding can resolve historical billing later. Unknown charge/ownership/pricing makes
subscription return unknown; partial known-value ratios are labeled lower bounds.
See [the accounting contract](docs/helix/02-design/contracts/CONTRACT-003-accounting.md)
for the metadata schema and example. Forward-only central enrollment and the
separately approved reporting-metadata scope are described below.


Account/route verification is available in the dashboard's **Session account and
route evidence** section. It distinguishes owner identity, inference billing identity,
native/lifecycle snapshots and current configuration, with per-call endpoint coverage
and explicit unknown reasons. `erg accounts backfill` replays only retained owner/route
metadata; a versioned service startup pass also performs it once. It does not reset
usage capture or duplicate calls. New Pi processes inherit the upgraded lifecycle
extension; already-running processes need to load the updated extension on restart.

Optional `discovery.json.route_metadata` accepts endpoint/region entries from routing
inventory. These enrich configuration observations; profile names are not parsed
as regions and current configuration is never used as historical session proof.

## Forward session corpus and direct local inference

Enable private session bodies with `erg history enroll`, then `erg service install`.
Existing files become enrollment checkpoints; only subsequent records are archived.
New sessions may contain restored context, which is labelled separately from
metered calls. Synthetic Pi history never creates token usage or human inputs.
The collector scans discovered native histories independently of usage enrichment.
Pi lifecycle hooks also fsync incremental content batches before returning; the
collector acknowledges/removes its own batch only after committing the corpus.
Already-running Pi processes need an extension reload or a new session for the
updated hook. Native transcript polling remains independent.

The compressed event corpus, usage ledger and source checkpoints share the private
SQLite store. Open **Session history** for session ID, project and time filtering,
native messages/tool records, linked server request/response receipts and usage
provenance. Reconciliation reports known source errors and sessions missing content;
it cannot certify undiscovered installations or callers bypassing capture.

`erg local-proxy --upstream http://127.0.0.1:18001 --port 18000 --save`
enrolls a loopback-only endpoint for the collector service. Reinstall the service
after enrollment. Requests are committed before forwarding; streaming responses
are checkpointed and incomplete receipts remain visible. Exact unique response
identity links native and server observations without adding a second metered call.
An internal backend port is an operational boundary, not access control: callers
must use the enrolled front port to be captured. An explicitly adopted backend can use capture port 18000 and backend port 18001.
Its original argv/recovery manifest is `local-backend-original.json` in the state directory;
backend and collector have separate user LaunchAgents. Collection restarts do not
reload the model. The backend adoption helper preserves original arguments and
waits for an idle window before gracefully restarting the original server.

No automatic agent-content expiration is enabled. `erg history expire --before
EPOCH` previews; adding `--apply` removes eligible corpus bodies and retains
identity tombstones against replay. Records waiting for reporting projection are
protected from expiration; the preview lists the protected count and apply reports
the number actually expired. This is logical expiration, not a secure
filesystem/WAL erasure guarantee. Native source logs have their own retention.
Request and response content may contain sensitive material; transport credentials
and metadata envelopes are excluded, but user-written secrets inside content are
part of the explicitly enrolled private corpus.

Reporting projections are also derived from the retained corpus through a durable
queue. Event inserts and changed bodies enqueue atomically, including writes from
an older collector during an upgrade. The service processes bounded batches;
failures retain their bodies and retry without blocking unrelated records. Capture
health and reporting backlog are separate signals. Usage keeps native call IDs,
and replay replaces the same observations rather than adding calls. Native Codex
cumulative counters remain separate from request totals and may cover activity
before corpus enrollment.

```sh
erg history project --rebuild --tool pi --session SESSION_ID
erg history repair-metadata                 # preview exact Claude metadata repair
erg history repair-metadata --apply
```

`project --rebuild` enqueues retained bodies only, runs one bounded batch, and leaves
the rest to the collector service. It does not read upstream histories. Metadata
repair only revisits already-retained Claude records with exact UUIDs and unchanged
content/usage; it restores missing IDs and flags, never imports older conversation
bodies. Unavailable or conflicting source records stay unresolved. A cleared
projection queue proves processing of retained records, not universal source capture.

Local server receipts expose an owned inference-table-compatible projection;
native history retains richer events alongside it. Enrolled devices can deliver
forward-captured content to the verified private erg destination. Retained native
history is excluded from forward-only delivery. Optional desktop activity is a separate future linked resource with
its own retention policy; it does not change agent-history retention.

Session history loads session rows independently of its cached discovery health.
The coverage disclosure provides **Run full ledger reconciliation** for the
more expensive ledger-wide check; opening or refreshing History does not run it.

An hourly metadata index now maintains History browsing in the background in the
same SQLite ledger. Completed hours use stored session/folder/project facts;
changed hours, partial-hour filters and older pagination boundaries use exact raw
metadata. Capture, expiry, context and project-policy changes invalidate affected
hours. Rebuilds never read native log bodies or enable historical backfill.
`python3 scripts/verify-history-browse.py --output /tmp/erg-history-parity.json`
performs an explicit read-only comparison of all retained metadata and page
results with the raw path. It is intentionally expensive and stays off the
normal request path.

Session history browsing pushes machine, tool and session filters into SQLite,
including native sessions linked to direct-local inference receipts. Records
sharing the same session, observed project and exact timestamp are counted
together before project allocation. Physical event counts, expiration counts and
pagination cursors retain their original meaning. Session bodies load only when
opening a session; browsing reads metadata.

History reconciliation distinguishes metadata-only, archived, expired and source
checkpoint problems. Full background scans also publish a bounded, metadata-only
comparison of discovered files and checkpoints. It distinguishes intentional
pre-enrollment baselines from changed or uncheckpointed files, traversal errors,
consumed outboxes and tracked sources outside active discovery. The snapshot has
its own age and health signal; fast capture cannot refresh an old discovery
check. Pruned files establish their last observed checkpoint, not proof that no
later tail was lost. No source bodies are read by this comparison. FSEvents may omit append hints
until a writer closes, so each capture cycle also checks up to 128 recently
modified checkpointed transcripts within the active discovery roots. Unchanged
files are skipped; cold files retain the full-scan reconciliation bound. The adapter inventory separates executable visibility in the
collector PATH from observed source data. A source executable need not be on that
PATH for its discovered logs to be captured. Copilot persisted JSONL messages and
tool events now enter the forward corpus; ephemeral usage absent from those files
remains unknown. OpenCode's empty `session_v2` database is recognized without a
false parser failure. Forward SQLite capture now preserves observed message and part projections from
recognized schemas. Unsupported schema changes and missing usage remain explicit.
The legacy JSON storage adapter currently supplies usage only.

PR and session lists display recorded titles, with stable repository/PR and agent/
session identifiers alongside them. PR titles refresh from the existing read-only
GitHub metadata feed; title-only refreshes preserve the commit membership snapshot.
Current Codex thread/index metadata, native Claude/Pi title events and linked
Omnigent conversation titles supply private session display names. A title is
presentation metadata and never establishes PR attribution, account identity or
session equivalence. Missing names display as untitled. Session titles remain in
the private local store and are not added to the central observation journal.

OpenCode SQLite history uses dynamically discovered data directories (the XDG data
home plus optional `opencode_data_dirs` in discovery.json). Reads are query-only and
include WAL commits. A bounded primary-key traversal commits captured projections
and its checkpoint together; an unchanged database/WAL fingerprint avoids repeated
body reads. Only records created or updated after enrollment have their bodies
archived. Current session title metadata is independent of that boundary. Mutable
native rows replace the corresponding local projection; the stored provenance does
not claim to reconstruct intermediate mutations between polls. SQLite checkpoints
that are incomplete or unsupported are visible in reconciliation. Transcript rows
are not token calls; usage remains unknown until separately observed metering exists.

OpenCode message IDs are kept separate from server response IDs. Where native
records omit the latter, native/server request association remains unknown; combined
source totals may overlap and the dashboard warns about this. Capturing both
sources is not proof of request-level reconciliation. A real isolated OpenCode
verification preserved two messages and four parts after those six native records
were pruned. The proof profile's temporary discovery entry was then retired; its
corpus, title and observed usage remain in the private ledger.


### Background overhead verification

Collector heartbeats and reporter `/healthz` expose cumulative process CPU and
CPU charged to waited-for child processes. These counters support before/after
measurements; account for live children at the boundaries. They exclude browser
rendering, hooks running inside another agent, and operating-system work.
LaunchAgents request background scheduling and low-priority I/O; this is not a
hard CPU cap. Catch-up may use up to one core; when input falls, resource use must
fall with it. Validate at least 10x representative arrival capacity in records and
bytes, plus backlog recovery and return to idle. Include source reconciliation,
periodic enrichment and active capture; a quiet short sample proves no capacity
margin. The fixed 10% ceiling was superseded by the operator on 2026-10-24.

Dashboard automatic refresh polls only lightweight capture health, and pauses
while the page is hidden. Reporting datasets load on demand and through **Refresh
data**; large usage/work reports have a 15-minute server cache. Up to four usage windows
are cached independently so open tabs do not evict each other. Encoded JSON is
also cached within those same bounds, with fresh capture health appended to each
usage reply. Explicit refresh and report expiry invalidate both caches. Reader
instances do not share report bytes; request-scoped Databricks readers remain
isolated. Reload an already
open dashboard after upgrading to activate the new refresh behavior.


SQLite session reporting uses targeted response/UID lookups to suppress proven
local mirrors. The portable reporting query remains the parity oracle for central
SQL. Changed title databases are detected using data/WAL fingerprints. Codex
metadata reads use update watermarks (including the timestamp boundary), and title
indexes tail verified appends. A fixed five-minute full reconciliation catches
backdated edits and rewrites even while the source stays active. Source metadata
is read before taking the destination writer lock; progress is acknowledged only
after its title transaction commits. Omnigent refreshes cache only sanitized route/summary
projections and blob digests; changed metadata and new native links still apply.
With content capture enabled, Pi usage comes from retained-event projection rather
than a second whole-transcript live scrape. Scheduled enrichment uses the retained
ledger for covered agents. Unenrolled CLI collection retains its original behavior.
Counter-only Codex observations remain cumulative snapshots, without invented
request identities. An edit-only upgrade queue replays already retained bodies
with bounded CPU and retries, pins them against expiration until processed, and
preserves usage, account and route attribution. Edit catch-up is shown separately
from trace capture health.

Host metrics have derived hourly tier rows maintained by a bounded background
worker. Inserts, corrections, moves and deletions dirty affected buckets in the
same transaction as the raw metric. Reporting uses cached tiers only when seeding
is complete, relevant buckets are current and timestamps are compatible; otherwise
it uses raw metrics. Partial-hour boundaries still query raw samples. Existing
80% coverage/tier/source selection is unchanged. Non-integer timestamps trigger
raw fallback, which recovers after those samples are corrected or removed.

Initial cache filling reads only the existing local metric store. Status exposes
`metric_rollup_cache` checkpoints and readiness separately from trace health.
Maintenance has a five-millisecond thread-CPU budget per poll with finite seed
and bucket batches; a single batch may exceed the budget. Idle maintenance takes
no writer lock. Reports remain available during filling, and Databricks reporting
continues to use the portable raw relation. This cache is rebuildable derived data.

Session drill-down computes full-session usage independently of the PR report.
Native sessions select candidate call identities, then rank all sightings of those
identities before filtering to the session. This preserves global deduplication;
local/gateway receipts retain the canonical cross-source matching query. The
first 1,000 calls and complete token/cost/account summaries share one read snapshot.
Opening a session does not rebuild or invalidate the whole effectiveness report.
Dashboard and work report queries also share one read snapshot during capture.

Collector status includes fixed-name `worker_cpu` counters to identify expensive
work. They measure instrumented thread CPU, exclude subprocesses and uninstrumented
code, and do not enforce a cap. `cpu_accounting` remains the process-total counter;
whole-background acceptance must also include helper processes.

Repeated metadata-only mirrors use a hash pair tied to the current canonical
body, avoiding BLOB reads when unchanged. A changed canonical body invalidates
the hint; body updates, hints and capture checkpoints commit together.

A repeatable observer is available from the checkout:

```sh
python3 scripts/measure-background-cpu.py --port 8788 --duration 1000 --output /tmp/erg-cpu-run.json
```

Choose an unused output filename. The observer leaves existing services running,
records process/helper counters and scheduled-work observations, and rejects
restarted-process or reset-counter comparisons. Its report lists excluded work;
a narrow sample cannot establish whole-tool CPU acceptance.

PR/commit attribution reuses results only within intervals bounded by recorded
context changes, operator/parent-link edges, relevant PR creation/closure times
and commit anchors. Exact boundary timestamps have their own cache entries,
preserving inclusive and fractional-time behavior. The cache is bounded to 4,096
results per report; it does not carry attribution across report snapshots.

Network host sampling uses numeric interface output to avoid DNS lookup waits.

New retained bodies use fast zlib compression. Existing zlib bodies remain readable
and are not rewritten. A bounded local sample measured lower compression CPU with
about 9% larger compressed storage; this is a tradeoff in favor of background CPU,
not proof of the whole-tool CPU target.


Pricing tiers are compiled once per loaded card; token amounts and cache-write TTL
remain per-call inputs. Account decisions reuse only evidence-stable time ranges
within one report, with bounded caches and explicit identity/binding boundaries.
Unknown and conflicting billing assignments remain unknown. Projection batches
reuse decoded session context while retaining durable per-record progress and
rollback isolation. These reduce repeated work; the whole-background CPU target
still requires measurement.

Direct inference listeners initialize their ledger before accepting requests.
Each request uses its own connection, with short transactions for request,
checkpoint and response capture. No database writer is held while waiting for
inference. Stream checkpoints reuse that connection without repeating schema
initialization.


Repository enrichment resolves conventional checkout/worktree markers without
starting Git for every recorded working directory; uncommon layouts and Git
configuration overrides keep the CLI fallback. PR refresh reuses a bounded metadata
page for recent and known PRs, without importing unrelated entries or omitting
individual lookups for targets outside the page.


Work and Accounts load independently of the usage charts. Capture opens health
and session inventory first; use **Load session & identity metrics** for the full
history summary, or select a session for its individual usage and costs. Values
marked **On demand** have not been loaded; a dash after loading means evidence is
unavailable. Work/account navigation reuses reports for up to 15 minutes, and
**Refresh data** requests a fresh report for the current view.


Work evidence can be checked and repaired from a selected already-retained session,
without reopening its native transcript:

```sh
erg work replay-retained --tool codex --session SESSION_ID
# Insert absent verified work rows only; preserve existing facts and usage.
erg work replay-retained --tool codex --session SESSION_ID --apply
```

Claude sessions are supported when their historical repository context was recorded.
The default preview opens the ledger read-only. The repair is bounded by event,
body-byte and time limits; expired bodies, missing Codex headers, old stripped
flags, ambiguous prior context and conflicts are reported explicitly. Existing
work rows are never replaced, and usage, inputs, accounts and billing stay intact.
`--machine`, `--max-events` and `--budget-seconds` select and bound the operation.
Apply revalidates its source snapshot before committing; a refused apply returns
an error. This is retained-corpus repair, not upstream historical capture. Continuous
work collection still uses its native adapter until a durable incremental work
projection has equivalent replay and mutation handling.


Work, Accounts and optional Capture metrics request separate response shapes.
Accounts reuses a fresh complete report when available; otherwise it computes only
canonical-call valuation, subscription periods, identity and quota summaries. That
path skips PR/commit attribution, session rollups and route analysis, and caches its
response for 15 minutes. Refresh invalidates the related report caches. Both paths
share the same valuation and billing-evidence rules. Accounts receives a compact display registry,
with the observation source and time preserved for each nonempty email/plan hint;
that registry does not assign a billing subscription. Subscription periods, quota
history summaries, valuations and unknown charges retain their existing meaning.
The full `/api/effectiveness` endpoint still returns complete evidence. Optional
`view=work`, `view=accounts` and `view=capture` parameters omit unrelated fields.


Ready Work/Accounts/Capture responses use a short publication lock and can return
while another request builds a report. Cold builds remain serialized, so multiple
open tabs do not launch duplicate calculations. Cache expiry is measured from the
underlying snapshot; serving a ready response does not extend its lifetime. Refresh
invalidates the related snapshots immediately, and an older in-flight build cannot
restore an invalidated response. This avoids lock waits for ready replies; cold
report computation and sustained background CPU still require further optimization.


Forward Claude/Codex work projection now has a durable queue, per-session parser
checkpoints and per-record receipts in the retained ledger. Tool calls and results
can span process restarts and native-log pruning. Work projection does not change
usage, human-input, account or billing observations; compatible native facts are
reused and conflicting facts are not replaced. Changed work-bearing source data
withholds automatic attribution for the affected session while preserving the
original evidence for inspection. Owner/route metadata repairs do not invalidate
work evidence. Explicit operator PR links remain independent evidence.

Activation records a forward sequence boundary and does not enqueue old corpus
records or their metadata repairs. The separate native work reader remains active
while forward parity is established. Missing retained headers and unsupported
repository context remain explicit gaps; Claude uses recorded repository context
rather than treating current configuration as historical proof. History diagnostics
show work backlog, gap reasons and conflicting-session counts. Expiration protects
records pending usage, edit **or work** projection. Routine work projection yields
after a 40ms preparation CPU / 50ms wall budget between records (up to 128 records per batch); one record and its bounded Git
subprocesses are not preempted, so this is not a hard CPU cap.


Retained work replay can reconstruct scope changes into deleted worktrees when
recorded session-time repository witnesses identify the directory unambiguously.
Current repository configuration does not replace historical scope evidence.
Commit attribution still requires resolving the observed commit to immutable Git
objects; missing or ambiguous object locations leave that commit unknown. This
applies to both continuous forward projection and selected retained-session repair.


Work projection acknowledges up to 32 contiguous records following an already
recorded context gap in one atomic metadata transaction. They remain explicit
gaps; this does not infer work, remove traces or clear conflicts. Source revisions,
new earlier pending records and concurrent checkpoint changes force reselection.
Known Codex assistant-text/reasoning and token-counter event shapes stay archived
and retain usage processing, while future work enqueues omit those no-ops. Tools,
user inputs, patch events and unknown shapes remain eligible for work projection.
Upgrades preserve the existing queue and forward boundary.
Known Codex `item_completed`, `thread_settings_applied`, `task_started` and
`task_complete` notifications also retain their full trace/usage paths without
new work enqueues. They carry no work-state changes in the current work parser;
user messages, tool calls/results and unknown event shapes still enter the queue.
Already conflicted sessions can acknowledge multiple records in one bounded
transaction, keeping every receipt conflicting and deriving no facts. A newly
found conflict still defers that session's remaining prepared records. This avoids
one-record-per-poll throttling without making uncertain attribution look known.


Cold Work requests calculate PR/commit attribution without building unrelated
session, inference-route or subscription summaries. Their token values use the
same rate-card and attribution rules as the full report; unmatched usage stays
unknown. A fresh full report can still supply every view. Focused Work and Accounts
responses have independent 15-minute caches, and explicit refresh invalidates all
view generations, including computations already in flight. Full-session drill-down
and capture metrics retain their complete calculations.

SQLite Usage, Work and Accounts calculations materialize only the canonical-call
columns each calculation needs, plus every ranking/matching key. Winner ordering,
synthetic/unknown usage eligibility and gateway/local reconciliation remain the
portable canonical query's rules. Databricks uses the portable relation directly;
this local optimization does not change stored observations or exported schema.

Known PRs outside the recent metadata page use a bounded GraphQL update-marker
query for up to 100 PRs per repository, reducing CLI startups. Changed PRs still
fetch exact commit membership and final filtered diffs through REST. Missing,
partial or foreign batch results are discarded before bounded REST fallback;
authentication, rate-limit and server failures remain errors. No batch marker
alone establishes commit membership, accepted lines or billed spend.

Worker diagnostics also expose `child_cpu_windows`: process-wide waited-child CPU
changes during each worker phase, with overlapping-run and unknown/reset counts.
These are diagnostic windows, not exact subprocess ownership. Concurrent phases
can observe the same child CPU; do not sum the windows or add them to process
`cpu_accounting`. Live children enter the counter only when reaped. Thread CPU
remains separate, and no diagnostic counter enforces the background CPU budget.

A developer audit for a proposed call-winner read path is available from the source
checkout: `python3 -B scripts/benchmark-call-winner-cache.py --output NEW_FILE.json`.
It opens the main ledger read-only, materializes winners in SQLite TEMP storage,
compares every canonical column/row and hourly group, and emits only timings,
counts and row digests to a new private file. This is a static benchmark, not an
installed cache. The separately audited `erg.call_projection` cache is now connected
to collector maintenance and snapshot-aware local reporting.

The cache seeds existing local usage metadata in bounded batches and maintains
winner invalidations transactionally. Actual inserts and changed updates invalidate
cached winners sharing their destination primary key, covering implicit replacement
deletions; updates/deletes also invalidate the affected groups. No-op source upserts
do not enqueue work. Raw ingestion retries cache-specific trigger failures after
atomically disabling those triggers; primary-store errors still propagate and raw
usage plus its observation journal remain in one transaction.

Usage, Work, Accounts, Costs and session usage can use ready cached winners.
Readers require a pinned snapshot, completed seeding, an empty invalidation queue,
compatible schema/triggers/indexes and an audited canonical-rule definition.
Unsupported layouts/identities, pending work and damaged metadata use the portable
query. Costs receives the SQLite adapter from its controller so its domain module
retains the portable default. These are derived reporting rows, not a second charge
ledger or a retroactive import of upstream conversations.

Maintenance uses bounded seed prefixes and batches of at most 32 queued groups.
Seed admission is backpressured by pending groups: existing invalidations take
priority, and the seed allowance is rechecked under the writer lock. The default
seed watermark is 256 groups; new source changes can exceed that watermark and
are always durably enqueued. An existing larger queue is drained without deleting
work or resetting its seed checkpoint. Maintenance has a soft two-millisecond
budget between batches. One batch can exceed that
budget, including when preflight checks have already used it; this is not a hard
CPU cap. A full private metadata-copy audit preserved every canonical field in
443,636 rows, and a separate real-snapshot audit matched all public Usage, Work,
Accounts and Costs fields. Neither proves whole-tool CPU acceptance. During initial
warming the dashboard continues using raw queries; CPU measurements must retain their workload scope.

To repeat the incremental audit from a checkout, run
`python3 -B scripts/audit-call-projection.py --output NEW_FILE.json`.
It opens the source read-only, copies only raw usage metadata into a private
temporary ledger, maintains the cache there and compares every canonical field.
The output must be a new path and is created with owner-only permissions. It
contains timing, counts and digests; the temporary copy is removed afterward.
This diagnostic deliberately runs maintenance densely and is not background CPU
acceptance or an upstream conversation backfill.


The universal local hierarchy can be audited with
`python3 -B scripts/audit-project-hierarchy.py --output NEW_FILE.json`.
It pins one read-only ledger snapshot, checks canonical token/count conservation
and identical Usage/Work/Costs valuation, and samples physical history record
counts. An SQLite authorizer prohibits archived body reads. Output is counts,
source hashes and aggregate digests only; no live project configuration changes.

Work and Accounts use the same durable hourly reporting publication as Usage and
Costs. Their shared filters allocate token usage and estimated value by the same
session → folder/agent project → human project rules. Work retains conservative
commit/PR attribution and input coverage; Accounts shows both identified usage
and a distinct unassigned-account bucket. Account ownership alone does not prove
which subscription paid for a request.

Charges and quota evidence have a different scope: subscription charges cover
whole billing periods, and identity/quota history is account-wide. A project
filter narrows the token-value numerator, not the recorded subscription charge.
The dashboard labels that ratio explicitly. Missing billing remains unknown.
Evidence is materialized in the existing paced collector worker and published
atomically with its usage/cost facts. Refresh reads completed projections; warming,
allocation changes and pending hours remain visible.


Reporting progress now distinguishes publication time from the newest published
hour's pinned source time. Visible Usage, Costs, Work and Accounts views poll a
small `/api/reporting/status` response every 30 seconds for capture-check age,
usage/work queue counts and pending hours. A newer publication offers refresh;
it does not silently relabel the displayed data. Ordinary maintenance publishes
one hour at a time with recent/oldest fairness and interleaves bounded retirement
cleanup. Policy/rate changes remain atomic across all affected hours. Forward
work preparation batches validated checkpoints and keeps parsing outside the
writer transaction. These software budgets require live process-tree CPU and
arrival/drain-rate verification; snapshot timestamps alone do not prove coverage.


Background writes use FULL WAL synchronization with checkpoints owned by paced
maintenance, avoiding repeated checkpoint work on every commit during long
snapshots. Optional repository/PR and historical-host enrichment waits while
retained projection queues are large; capture adapters, native work evidence,
perf samples and live sampling continue. The common health bar identifies that
waiting state, and optional enrichment resumes on a scheduled pass after catch-up.


The proposed fleet upload API is specified in
`docs/helix/02-design/contracts/CONTRACT-006-central-delivery.md`: stdlib HTTPS
JSON to an authenticated write-only broker with durable spool acknowledgement,
then central forwarding into owned Unity Catalog tables. Full ordered session
events remain separate from inference calls, whose core follows production
Unity Gateway's 18-column schema. Accepted, materialized and projected delivery
positions are separate. This is a proposed contract and capability-spike target,
not an enabled exporter; private access and retention/rollout gates remain.


### Forward-only central session browser

The enrolled operator can open `http://127.0.0.1:8788/databricks` to browse
forward session traces directly in Unity Catalog without deploying an App.
The regular dashboard shows independent uploader health and accepted versus
materialized progress. Agent, session, folder, machine and time filters operate
on central current revisions. The full dashboard below also reports cloud usage,
costs, work and accounts with the same project policy as the local dashboard.

Real uploads require explicit enrollment, a native nonexportable machine key,
source byte/inode and timestamp boundaries, and the exact enabled broker binding.
Reinstalling the uploader preserves that boundary and never opts retained history
into export. Use `erg delivery status` for local progress. Cloud reader access uses
the operator's separately configured Databricks profile, not the ingestion secret.


### Full Databricks dashboard

Open `http://127.0.0.1:8788/databricks` for Usage, Costs, Work, Accounts and session
history from the uploaded forward corpus. `/` continues to use the local ledger;
`/databricks/traces` retains the direct SQL trace browser. No App deployment is
required. The full dashboard uses a private incremental read cache populated only
from verified Delta items, the shared report code and the operator's same project
catalog and bindings. The header shows cache backlog and project revision.

Report reads do not query Databricks. Cache refresh uses bounded SQL pages in a
background worker and avoids SQL polling when signed receipts show no new materialized
items. Cache data lives under `~/.local/share/erg/databricks-reporting/`; it is not
an export source and does not opt retained history into uploads. The enrolled uploader also sends explicitly approved, allowlisted work/PR and
account/binding/quota/charge evidence for forward-captured sessions through the
same signed delivery journal. Current metadata can annotate forward work, but
retained native bodies and pre-boundary usage are excluded. Credentials are never
exported. Metadata replay retains the original observation identity and revision,
so native traces and reporting evidence use the same canonical deduplication.
Unrecorded charges, unsupported billing matches and missing evidence remain unknown.


### Rack rates and effective expense

The dashboard shows rack-rate comparison value beside allocated effective
expense across Usage, Work, Projects and Accounts. Reviewer requests use an
explicit parent-model comparison range; local models use a sourced market-hosting
comparison. Accounts allows recording observed monthly plan prices separately
from actual subscription, reset and overage receipts. Plan prices accrue estimates
from observation onward; paid charges retain exact billing periods. No purchase
is inferred from a quota reset. Filters never shrink the whole observed-period
expense denominator. Partial account coverage and open periods are provisional.

The private Unity Catalog `erg.telemetry.rate_catalog` and
`rate_catalog_versions` tables hold immutable rate publications. Pull the current
catalog with the existing reporting identity (replace profile/warehouse as needed):

```sh
python3 scripts/rate-catalog.py pull --profile erg-prod --warehouse WAREHOUSE_ID --namespace erg.telemetry --automatic
```

`publish --file complete-catalog.json` requires an explicitly supplied full JSON
catalog and an identity permitted to write the application tables. Publication
creates a new immutable revision; readers verify every row before atomically
replacing their offline snapshot. No credentials are stored in the catalog.
