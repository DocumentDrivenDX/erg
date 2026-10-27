---
ddx:
  id: erg.contract.001
  authoring:
    home: repo
  links: [prd, erg.architecture, erg.adr.002]
---
# Session capture, synchronization and reporting contract

## Purpose

Define durable session evidence, identity, synchronization and consistent reporting
without presenting estimates, inferred attribution or missing facts as billed truth.

## Scope and boundaries

One operator, multiple agent installations, local reporting and optional central
reporting over Unity Catalog. The collector and reporter run independently.
This contract realizes FR-1 through FR-9, FR-11, FR-18 and FR-23 through FR-27.
Fizeau native telemetry, dated cost computation and value classification retain
their PRD authority and are separate from this deployment increment. Costs with
no computation or provider evidence are explicitly unknown.

## Precedence and compatibility

The PRD owns requirements; this contract defines their observable data semantics.
Executable allowlists define accepted fields. Additive optional metadata retains
schema version 1; incompatible identity changes require an explicit version.
The qualified forward full-content and reporting extensions below supersede the
earlier SQL metadata prototype for enrolled delivery. They do not authorize
retained session-history export or reset an existing enrollment boundary.

## Normative surface

### Observation records

`erg.unified.FIELDS` is the executable allowlist for schema version 1. Types are
`session`, `llm`, `prompt`, `edit`, `commit`, `metric`, `note`, `source`,
`project_context`, `work_context`, `work_link`, `pull_request`, `pr_commit`,
commit evidence and allowlisted account/billing evidence. The input and PR
semantics are governed by CONTRACT-002-input-attribution.md.
Records preserve source UID, event timestamp, tool, native session ID, project,
model and source-specific numeric fields. Session records also preserve observed
bounds, parent/fork relationships, CLI/Desktop surface and agent version.
Unknown usage is distinct from observed zero. Session bounds are observation
bounds, not a claim that all elapsed time was active or billed.

Four mutually exclusive usage buckets are fresh input, cache read, cache write
and output. Reasoning is a subset of output and is not added a second time.
Claude's fresh input is preserved; Codex cached input is subtracted from its
inclusive input count. The OTel projection includes all three input buckets in
`gen_ai.usage.input_tokens`.

The version-1 observation/export journal excludes prompt text, responses and tool arguments/results. Operator-enabled local history capture stores selected native event bodies in separate compressed history tables in the same private SQLite database. Arbitrary authentication/configuration fields and HTTP authorization headers are excluded. Prompt-size observations remain content-free.
Host-monitor notes contain application labels and memory figures only. No raw
Databricks inference request/response payload is queried or copied.

## Identity and snapshots

An observation has a deterministic hash key over schema version, machine, type
and source UID. Changed content gets a monotonically increasing local sequence;
unchanged replays do not. The most complete streamed call cannot be replaced by
an earlier fragment with less output. Copied response identities and commit
sightings are deduplicated in reporting, while source observations remain.

A capture transaction includes read-model updates and the export journal. Source
progress cannot advance if the transaction fails. Collector process failure
before ingestion cannot claim successful capture. Retention is non-destructive:
pending observations are not discarded automatically.

## Central schema and inference-table alignment

The owned `erg_observations` Delta table stores typed identity, machine, record
kind, event time, allowlisted JSON payload, content hash, local sequence and
schema version. It is not a Databricks-managed inference or usage table.
Typed views expose sessions, calls, prompt sizes, work evidence and metrics.

`erg_inference_calls` exposes request_id, invocation_id, event_time, destination
model, requester and schema version, alongside the four token buckets and native
session identity. Request/response bodies and unobserved latency/status are NULL.
Synthetic source identities are not presented as real gateway request IDs.

`erg_spans` exposes trace/span identity, observation timing and OpenTelemetry
GenAI attributes for interoperability. It marks reconstructed evidence explicitly;
unknown call end time remains NULL. It is a query projection, not a claim that
backfilled rows are valid complete native OTLP spans. Native gateway traces stay
in their original table. A future native OTLP exporter must follow its own timing
and completeness contract.

Live gateway usage is read through `system.ai_gateway.usage`; gateway bodies are
not required. Central gateway history is bounded to 400 days, matching collection's
initial history window. Gateway usage is a separate source from local sessions.

## Reconciliation

A shared nonempty request ID and model reconcile only when exactly one gateway
and one local call carry that identity. The local call provides project and host
context. Equal token counts and nearby timestamps are insufficient. Unmatched
sources remain visible and reports warn that totals may overlap; uncertainty
must not silently remove usage. Exact identity reconciliation is re-evaluated
from current evidence, so ingestion order does not affect the result.

The ledger preserves source token figures. This increment does not certify
billing-grade reconciliation or silently replace source values with estimates.

## Delivery

`erg push` batches current changed snapshots in sequence order. A parameterized
Databricks SQL MERGE upserts by event ID and ignores older revisions. The local
cursor advances only after SQL success. Failures and ambiguous network outcomes
leave the cursor pending; replay is safe. Each central Delta table ID has its own cursor, so recreating a table triggers a fresh export. `erg push --replay` repairs a destination from all retained snapshots without deleting central rows.
A newer concurrent local revision remains pending after an older batch succeeds.

Batch size is bounded to 1..10000 observations, default 5000, additionally capped at 2 MiB of encoded records. SQL execution has a
600-second deadline; failed statements expose error codes without printing row
payloads. Only erg-owned objects are provisioned in an explicitly selected
existing catalog.schema. The JSON SQL transport preserves the standard-library
runtime constraint; this increment does not require a handwritten protobuf codec.

## Error semantics

Digest, immutable-revision and enrollment mismatches fail closed. A failed local
transaction cannot acknowledge source progress. Invalid reporting observations
are quarantined independently so native delivery can continue; export/import
errors remain visible. Trace-cache sequence gaps cannot advance its cursor.
Unavailable usage, prices, account bindings and invoices remain unknown.

## Examples

A content-free call can report fresh input 100, cache read 50, cache write 0 and
output 20, with reasoning 5 contained in that output. Its total is 170 tokens,
not 175. A repeated source UID with unchanged content does not add another call.

An enrolled session's observed project_context at time 90 may supply its folder
for a first forward call at time 110 when enrollment starts at time 100. The
context may be exported as scoped metadata; usage or session bodies at time 90
remain outside forward export. A missing subscription invoice remains unknown
even when the account plan name is observed.

## Reporting

Both backends execute the same report model. SQLite stores local projections;
Databricks executes grouped SQL over typed views. Raw central event history is
not downloaded into a browser. Hourly and daily usage views are also available
to SQL/notebook consumers. Metric tier aggregation happens in the backend;
coverage-aware source selection is shared Python logic over bounded aggregates.

The existing dashboard's hour/day, project, model, tool, machine, cache-bucket,
kind and requester pivots remain. New session/source panels expose capture health,
parentage, surface and unknown usage; session drilldown returns sizes and IDs.
The session panel shows at most 200 recent sessions, with that limit visible;
call drilldown returns at most 1000 calls, with the limit in the response.

`/api/data?days=N` accepts 0..3660, where 0 means all available captured history;
its default is 90. `/api/session?tool=T&session=S` returns the selected call list.
The reporter caches one completed data request for 60 seconds and reports its
actual generated time. Failure returns 503, not an empty success dataset.

Metric results include sample count and covered seconds. Sources covering at
least 80 percent of an hour are preferred before configured source priority;
otherwise the best-covered source wins. Active minutes use observed coverage,
not an assumed complete hour. For coarse upstream tiers, coverage is capped at
one hour per aggregate; interval-boundary weighting is not yet a claim of this
increment and must be tested before finer-grained energy accounting.

## Central destination access

The selected central destination is a new private `erg` catalog and dedicated
schema in production. Before exporting real records, verify the workspace,
namespace and effective access, including inherited catalog/schema grants.
Collectors require authenticated write access; the operator reporter requires
read access. Do not grant broad developer access or fall back to a shared
catalog. Export and app deployment stay paused until the target and required
permissions are available. Prompt exclusion does not make session metadata
non-sensitive: machine, project, author and activity identifiers remain private.
Exact principals, schema name and retention policy are unresolved.

## Runtime and verification

`erg collect` runs once for cron. `erg collect --interval SECONDS` also samples
host counters every 30 seconds while scheduling capture; `--push` enables central
export. `erg serve` selects SQLite or Databricks and never initiates collection.
Enrolled one-shot capture commits forward bodies and their durable projection
queue before optional metadata/usage/context enrichment. It rejects a ledger
without matching enrollment, shares the daemon collector lock, and reports
historian errors as command failure before any explicitly requested export.
Without enrollment it never opts into body capture. Remote SSH usage collection
is unchanged. The managed private launcher must load its installed package from
any working directory and preserve explicit environment overrides. Filesystem
watching and live inference interception require the managed collector service;
one-shot capture cannot promise recovery of source data pruned between runs.
Databricks Apps uses the requesting user's forwarded token with read-only SQL scope. Tokens live only for that request; no cache is shared between identities. Local Databricks reporting uses the existing CLI login.

Unit/fixture gate: `python3 -m unittest discover -s tests -v`. Required evidence
includes real local capture, unchanged replay totals, failed-export cursor safety,
central row verification, central aggregate queries, local HTTP/API inspection
and a Databricks App deployment with an authenticated backend smoke check.

OpenTelemetry attribute names checked against the [GenAI registry](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/registry/attributes/gen-ai.md) on 2026-10-17; those conventions remain in development.


## Local discovery and orchestration links

Discovery resolves native homes independently of parsers: defaults, explicit
configuration/environment overrides, and Omnigent bridge metadata. Pi JSONL
messages retain native session/entry IDs and provider/model identities. Mirrored
native transcripts share the same observation identity and do not add usage.

Session observations may additionally carry `orchestrator`, `conversation_id`,
`external_session`, `summary_in`, `summary_out`, `summary_cache_read` and
`summary_cache_write`. Omnigent joins on workspace and conversation identity,
then links an unambiguous external session ID to the native harness. Mirrored
conversation messages and cumulative session usage are never emitted as calls.

Without a native history, the session is marked `omnigent_metadata` provenance
and either `summary_only` or `unknown` usage. Summary fields retain Omnigent's
reported quantities without claiming call-level completeness or reconstructing
request identities. If native evidence later arrives, the reporting session
relation suppresses the metadata placeholder for that conversation on that
machine. Linking and summary fields survive an unenriched native replay.
Call/token charts use captured call evidence; session summaries remain separate.

The local dashboard reports all retained per-machine/harness session counts,
call-evidence coverage and summary-only counts; its recent-session browser is
bounded to 200 records. Omnigent coverage reports linked and unresolved
conversations. Installed harnesses with no discovered history remain visible.


### Discovery-to-checkpoint evidence

Session browsing MUST request session rows independently of capture-health reads.
The default History page serves the cached discovery comparison. Full ledger
status and reconciliation scans run only when the operator selects **Run full
ledger reconciliation**. Failed or superseded diagnostics MUST NOT replace the
session list or overwrite a newer health snapshot.
Within a pinned session-list request, allocation results may be reused only for
the same machine, native tool/session, observed project, native/work context
epochs and binding-validity interval. This request-local cache is bounded to 4096
entries and does not replace physical record counts, timestamps or high-water
pagination.

The local `history_browse_*` projection MUST contain metadata only, in the same
private ledger. Its background worker builds at most one occupied hour per
service iteration, alternating oldest and recent debt during catch-up. Closed-hour
debt drains without idle pacing; active-hour-only work coalesces on a 15-second
service cadence, with exact raw reads while dirty. Each build loads context only
for identities recorded in that hour, including linked native receipt identities. Context
changes invalidate affected future hours through indexed session dependencies;
physical event, expiration, deletion and receipt-link changes invalidate their
corresponding tiles. SQLite replacement writes MUST invalidate the displaced
context as well as the replacement. Identical context replay MUST remain idle.
Project policy revisions invalidate older allocation tiles.

A tile publishes only if its source dirty serial, relevant context versions,
receipt-link version and project revision still match the pinned source snapshot.
Concurrent mutations MUST preserve rebuild debt. Report reads MUST remain
read-only and use raw metadata for dirty, absent, incompatible, partially selected
hours or tiles newer than the requested pagination high-water mark. Cached and raw
facts MUST preserve physical counts, expired counts, event bounds, receipt
identity, split allocation and the complete matching folder catalog. Responses
expose indexed and raw hour counts. Initial seeding rebuilds derived metadata
from already-retained records; it MUST NOT discover or import pre-enrollment
native history, alter expiration, or change central enrollment boundaries.

Each full background source scan MUST publish a version-1 metadata comparison
under `history_config.source_reconciliation_v1`. Local-only
`GET /api/history/discovery` MUST serve that cached comparison without rescanning
source files or the event corpus. Targeted fast scans MUST NOT
replace its timestamp or clear its independent discovery-health error. Reporting
MUST expose its observation time and age; comparisons older than 660 seconds are
stale. Missing or invalid comparisons MUST remain explicitly unproven.

The comparison MUST distinguish current captured checkpoints, intentional
pre-enrollment baselines, consumed durable outboxes, uncheckpointed files,
changed files, pending scans, parser/checkpoint errors, tool conflicts, files
that disappeared during checking, and checks deferred by the audit budget.
`inventory_paths` and `checked_paths` describe files, not complete sessions.
Native file comparisons use inode, size and modification time. SQLite adapters
compare the database/WAL signature; native byte offsets MUST NOT be compared to
SQLite traversal positions or compressed-record offsets.

Directory traversal/stat failures MUST be reported with operation, path and
numeric error code, without raw exception messages or source content. Missing
optional home directories are normal; an unavailable explicitly referenced
transcript remains evidence to investigate. The comparison reads only file
metadata and stored checkpoints, uses a two-second comparison budget, and bounds
issue details to 100 records. Deferred work MUST NOT be labeled current.

Tracked sources outside the current inventory MUST be reported separately as
consumed, pruned, present outside active discovery, unavailable or unexamined.
A pruned source proves its last observed checkpoint only. Unobserved tail writes
between capture and pruning MUST NOT be declared captured. Directory symlinks
are not traversed by this discovery adapter. Producer-side omissions, bypassed
listeners and unknown roots remain outside this comparison's proof.

Inventory and comparison digests identify the candidate list and inspected
metadata, respectively. They are not transcript content hashes. Source audit
records MUST NOT create token usage, modify native capture boundaries, expire
content or enter the session export journal as activity.


## Continuous local capture

Pi lifecycle capture persists a content-free JSONL snapshot under
`ERG_STATE_DIR/pi-snapshots` before temporary bridge pruning. It observes native
persisted entries after turns, before tool execution, and at session lifecycle
boundaries; `message_end` itself precedes native entry-ID assignment and is not
used to invent a second call identity. Snapshot entries reuse the transcript's
session/entry IDs. Each hook instance has its own writer ID, preventing stale
concurrent processes from overwriting another process's durable evidence.
Writes use a private temporary file, fsync, atomic rename and directory fsync.
Capture failure warns locally without disrupting the agent.

FSEvents notifications are hints. An agent may hold a transcript writer open;
append notifications MUST NOT be assumed to arrive before close. Each history
cycle additionally checks up to 128 most recently modified checkpointed native
transcripts still owned by current discovery. Unchanged files MUST NOT be reread,
and this fallback MUST NOT walk the entire inventory. The complete rotating
source scan remains necessary for colder files and newly discovered roots.
Configured mutable SQLite sources also have their database/WAL signature checked
on each cycle; unchanged signatures MUST NOT reopen source tables or write a
checkpoint more often than the source heartbeat interval.

A single enrollment service owns the collector lock and host sampling. Its
independent event-queue workers and fifteen-minute retained enrichment use separate
SQLite connections and transactional ingestion. Failed transactions retain the
previous checkpoint; broad scan failure does not stop fast polling. launchd
restarts crashed collectors. A heartbeat and separate fast/full outcome fields
make degraded capture visible. Neither hooks nor this service initiate exports.
New managed Pi sessions inherit the explicit global extension path; already-open
sessions require a reload. Explicit extension disabling and termination before a
persisted boundary remain observable coverage limits, not guarantees of lossless
capture of every attempted call.


## Foundation history extension

Local content capture is now explicitly authorized. Preserve full conversation and tool event bodies from discovered native transcripts and lifecycle snapshots, with source-file identity, source offset, parser revision, native session/entry IDs and content hash. A transaction commits event content and progress together. Store history compressed; indexes contain machine, agent, session, project and event time. Partial trailing records do not advance checkpoints. Parsed-but-unmetered history remains distinct from metered calls. Omnigent restored assistant records with synthetic zero timestamps are reconstructed history; all-zero usage without reliable native metering evidence is unknown.

Reconciliation enumerates source roots and wrapper metadata independently of the usage report. Report completeness only for observed, readable sources and the retained time window. Retain missing/unreadable source and metadata-only session states. No capture process can certify clients that bypass enrolled endpoints, disabled hooks, histories pruned before enrollment, or undiscovered private roots.

The inference compatibility projection follows [Unity AI Gateway inference tables](https://docs.databricks.com/aws/en/ai-gateway/inference-tables): request_id, invocation_id, event_time, destination, requester, request/response and observed status/timing fields. Unobserved values remain NULL; erg source IDs do not masquerade as Databricks-issued IDs. Extend records with native session ID, machine, agent, project, usage provenance, cache buckets, subscription evidence, history availability, expiry and schema version. These owned records do not alter Databricks-managed tables.

Content retention is explicitly configured; default expiration is disabled. Expiration must support preview and leave tombstones so source replays cannot silently resurrect expired content. Metadata/cost retention is independent of content retention. Pending central content delivery must not be discarded by an implicit retention policy. Raw content export remains disabled in this increment; private central permissions are still unresolved.

Capture starts at enrollment. Existing native files are baselined at their current end; prior content is not backfilled. New append records and new sessions are captured. New-session reconstructed context is labelled as such and never counted as fresh inference. Completeness is bounded by enrollment time; the operator does not require retrospective capture.

### Display titles

PR observations may include an optional `title` from their GitHub metadata source.
A title-only refresh preserves the PR commit-membership observation timestamp.
Private local `session_titles` records carry machine, native tool/session identity,
recorded title, provenance, precedence and source update time. Explicit native
names outrank generic wrapper fallback names; older equal-precedence names cannot
revert a rename. Title discovery reads selected current metadata, never credentials.
It does not backfill historical conversation bodies. Session display titles are
outside the central content-free journal in this increment. Reporting APIs provide
titles alongside stable IDs. Missing titles remain NULL and UI content is escaped.
Titles cannot establish identity, cost, account ownership or work attribution.

### OpenCode SQLite source projections

The optional SQLite adapter opens discovered `opencode.db` files read-only, with a
consistent snapshot for each bounded traversal step. Supported source session tables
are `session` or `session_v2`; transcript tables are `message`, `session_message` and
optional `part`, requiring native row/session IDs, data and creation time. Unknown
schema is an explicit source error. Discovery honors the XDG data directory and
configured `opencode_data_dirs`; it never reads authentication stores.

Only rows whose native creation/update time reaches enrollment are archived. Old
rows are traversed as identifiers/timestamps without loading their bodies. Stable
source table/row IDs identify compressed `sqlite_record` projections, with native
creation/update time, type and payload preserved. Updated rows replace the observed
projection, and expiration tombstones prevent resurrection. Parts remain independent
records. This preserves observed persisted state, not a claimed complete sequence of
mutations between polls. JSONL append-only guarantees must not be inferred for this
source. Source projections do not create model calls or token usage by themselves.

Database plus WAL fingerprints avoid idle rereads. Primary-key traversal and the
source checkpoint commit with the local projection; replacement files reset traversal.
Bounded pending scans and unsupported records remain visible through source health.
Bodies above 32 MiB are unsupported explicitly, rather than silently truncated.
Raw content stays local under the existing enrollment, expiry and export restrictions.


### Durable reporting from retained content

The local corpus is the replay source for supported per-call usage and input
projections. Database triggers atomically enqueue inserted or changed event bodies,
including writes by pre-upgrade collectors. The projector uses stable native call
and message IDs, bounded batches, per-record savepoints and transactional queue
acknowledgments. Invalid records roll back their partial observations, retain their
bodies and retry after a delay; unrelated records continue. Expiration excludes
queued bodies atomically and reports protected and actually expired counts.
Tombstone triggers prevent older writers from resurrecting expired bodies.

Projection gaps are independent of source completeness. Missing native IDs, missing
usage fields and unsupported input provenance remain explicit. Claude streaming
fragments merge by exact message ID; omitted token fields are never converted to
zero. Synthetic Pi history does not become metered usage even when copied records
contain positive token counts. OpenCode parts aggregate into one input per native
user message, with origin unknown unless supported. Native Codex cumulative counters
are separate snapshots: their window may precede enrollment and they are not added
to per-request totals or assigned invented request IDs.

A retained-corpus rebuild reads no upstream histories. Optional Claude metadata
repair requires an existing retained event, exact native UUID and identical retained
content/usage. It may restore IDs and flags from supported source roots, but cannot
import older conversations or modify session identity. Missing or conflicting
sources remain unresolved. Verified work/commit/PR attribution is preserved;
complete reconstruction of that attribution from raw content is not yet supported.


### Event-driven capture and efficient reconciliation

On macOS, FSEvents feeds independent bounded queues for history, context and fast
metadata workers. Coalesce repeated paths; retry failures and deferred paths at
the tail so one source cannot starve the others. Queue overflow, kernel drop/root
change flags and discovery configuration changes request full reconciliation.
Refresh dynamically discovered roots; reconcile at least every five minutes.
Unsupported or failed native watching is explicitly reported as polling fallback.
Targeted capture must not walk the whole source inventory. Retained projection
must drive its query from the pending queue rather than scanning the corpus when
the queue is empty, while preserving native sequence ordering and retry deadlines.
A real filesystem-event test exercises capture with the inventory walker disabled.

Unchanged discovered JSONL sources are stat-checked during reconciliation.
Persisted `last_seen` is a throttled checkpoint heartbeat (at most once per 60 seconds
per unchanged source), not the exact time of the most recent stat. Batch those
heartbeats into one short transaction; changed bodies and their byte progress keep
the existing per-source atomic commit. Prefetch checkpoint metadata per scan and
avoid decoding unchanged context JSON. Scan results expose checked/unchanged counts,
heartbeat writes, inventory/wall time and history-thread CPU time. These counters
measure one worker, not total-tool CPU-budget compliance. Parse errors on unchanged
sources remain visible, and a budget-exhausted pass does not advance past an
unvisited source.

Native context parsing skips EOF-complete unchanged files before opening a write
transaction. Parse transcripts and inspect git metadata outside the writer lock;
then compare the durable starting checkpoint and atomically commit sanitized
observations with the ending checkpoint. A concurrent checkpoint winner, source
replacement or ingest failure must not permit stale progress to be committed.
This reduces lock contention but does not itself prove the background CPU target.


Host CPU sampling on macOS reads HOST_CPU_LOAD_INFO through the system library;
compute utilization from successive cumulative tick snapshots. Do not launch
whole-machine `top` sampling for this gauge. Missing, reset or wrapped counters
produce unknown utilization until valid interval evidence returns. Reported CPU
budget evidence must include collector and reporter processes and their helpers;
record browser/hook/kernel exclusions and periodic jobs omitted from the window.


### SQLite reporting parity and bounded identity lookup

The portable `unified.SESSIONS_SELECT` remains the reporting parity oracle and
central SQL definition. SQLite may use an equivalent targeted session relation:
rank local receipt candidates, then probe eligible native response identities and
their winning tool/UID sightings using indexes. A losing native sighting, ambiguous
response match, synthetic/unknown usage, model mismatch or another machine must
not suppress a local session. Metadata-only local sessions remain visible. Never
replace exact identity evidence with temporal similarity to make this lookup cheap.

Native session call lookup may select the session's candidate UIDs, then rank
all eligible sightings of those UIDs globally before applying the final session
filter. It must not resurrect a losing copy whose winner belongs to another
session. Local/gateway receipt drill-down retains the canonical cross-source
query until an equivalent narrower query is proven. A partial eligible-session
index bounds native lookup work against unrelated sessions.

Session summaries share the exact usage, valuation, account and route accumulator
with the full effectiveness report. Preserve canonical accumulation order, raw
cost-sighting semantics and unknown billing. Full metrics and the bounded call
list use one read snapshot; the first 1,000 details must not truncate totals.
Opening a session must not refresh or invalidate whole-ledger PR reporting.

Fixed-name worker CPU diagnostics use thread CPU and count both successful and
failed work. They store no arguments, content or exception messages. Their scope
excludes helpers and uninstrumented code; they cannot substitute for process-tree
acceptance evidence or claim an enforced cap.

An eligible input-ranking index follows the existing global winner order; excluded
Codex role records do not enter that index. Work reports stream metadata and
precompute immutable conflict results per recorded context/route timestamp. Request
route overrides and caller mutations must not alter cached session-route evidence.
Same-timestamp ambiguity remains unknown until a later unambiguous interval.

Optimization evidence includes randomized comparison with the portable relation,
a VM-instruction regression with unrelated native history, and exact full-report
comparison within one real-ledger read snapshot. These prove parity for the tested
scope; they do not establish the whole-background CPU acceptance target.


Provider-home notifications include unrelated plugin/cache/build files. Filter
these before enqueueing so they cannot displace capture work; enroll transcript
roots dynamically and retain bridge-state/OpenCode/Omnigent metadata hints.
Filesystem loss flags always request reconciliation even for filtered paths.
Discovery watches both a symlink's directory and its target directory, refreshing
watch roots on target edits or symlink replacement. Ignored-event counters and
queue overflow/reconciliation counters make this distinction observable.


Metadata refresh caches acknowledge only successful destination commits. Title
source fingerprints include database/WAL inode, size and nanosecond modification
time; a five-minute safety refresh recovers independently changed local links.
Errors are not cached as success. Unchanged titles do not cause repeated writes.
Omnigent caches bounded sanitized summary/route projections plus blob digests,
not full provider configuration or raw compressed envelopes. Cached observations
are re-emitted so a failed ingest can retry; native matching is recomputed even
when metadata is unchanged. Missing/ambiguous native matches stay unknown.

When forward content capture is enabled, durable Pi event projection owns live
Pi usage. Its lifecycle snapshots and native files retain the same event/call
identities; disabling the duplicate live usage scraper must not change totals.
An isolated running-service test forbids that scraper and verifies capture of
one call with exact token totals. Enrolled scheduled enrichment uses retained
source checkpoints and project directories rather than reparsing covered Claude,
Codex, Pi or Omnigent histories. Copilot/OpenCode legacy usage adapters remain.
Unenrolled collection preserves its existing adapters. Counter-only observations
must not acquire fabricated request identities.

Projector version 2 shares native edit extraction with the legacy adapters. A
separate edit-only migration queue reads already retained bodies, preserving
non-edit facts. It is CPU bounded, retries errors, checks content hashes before
acknowledging results and pins queued bodies against expiration. Missing bodies
produce explicit gaps. The dashboard reports edit catch-up separately from trace
capture health. This upgrade does not read historical source bodies.

Forward body writes use zlib level 1 to reduce compression CPU. All zlib levels
share the existing decoder; content hashes cover uncompressed JSON and keep their
identity semantics. No historical corpus rewrite or source backfill is required.


Metadata-only mirror hints store only an incoming source hash and the canonical
body hash it produced. A hint may bypass BLOB decoding only when both match the
current observation. Canonical body changes invalidate old hints automatically.
Hint updates roll back with body/checkpoint transactions. Expiration still blocks
content resurrection; hints do not count as traces or usage.

`scripts/measure-background-cpu.py` observes existing local services and writes
private evidence. CPU percentages refer to one core and comparisons are invalid
after a process identity change or counter reset. Include scheduled work and
record exclusions before interpreting a window; the script does not declare
whole-tool acceptance from its measured subset.


### Bounded derived host metrics

The raw metric relation is authoritative. Hourly per-source/tier count, sum and
peak caches may accelerate reporting only after their existing-store seed is
complete and no relevant dirty bucket remains in the same read snapshot. Raw
inserts, value/peak corrections, dimension/time moves and deletions atomically
mark affected buckets. Failed maintenance must preserve dirty markers and seed
checkpoints. Replayed unchanged metric values must not re-dirty completed buckets.

Initial seeding uses finite rowid batches with a frozen upper bound; new writes
are independently covered by triggers. Maintenance uses a separate connection
from history capture and finite batches with a thread-CPU budget. Idle maintenance
must not acquire the writer. Capture failure rolls back its partial transaction
before maintenance/retry. Maintenance errors cannot make stale data eligible or
stop capture. Partial-hour boundaries query raw samples; source/tier coverage and
selection remain identical. Incompatible timestamps require raw fallback, with
recovery once unsupported rows are corrected or removed. Portable Databricks
reporting does not depend on local cache tables.

Title metadata scans use update watermarks with inclusive timestamp boundaries,
verified JSONL append offsets, and a full-read fallback for unknown schemas or
source replacement. Full reconciliation deadlines must not slide forward during
continuous source activity. Source reads/parsing cannot hold the destination
writer transaction; offsets, watermarks and fingerprints advance only after the
destination commit. Cache tails retain hashes rather than arbitrary source bytes.

Initial source metadata/title/reconciliation scans must run independently of the
monotonic clock's origin; a fresh process-relative clock cannot defer them until
the first periodic deadline. Scheduled broad enrichment may retain its explicit
startup deferral.


Encoded report reuse must follow query-cache expiry, explicit refresh and window
LRU eviction. Capture health remains fresh in cached usage replies. Report bytes
must remain within their reader/authentication scope; user-scoped Databricks
requests cannot share them. Wire encoding changes must preserve decoded JSON.

PR/commit interval reuse must include context timestamps, explicit/inherited link
bounds, relevant PR creation/closure timestamps and scope-specific commit anchors.
Exact boundaries must be distinct from adjacent intervals, including fractional
timestamps. Preserve ambiguity and missing evidence; cached task sets are immutable.
Reuse is bounded and limited to one report snapshot, never across metadata changes.

Dirty host-metric buckets may commit in a finite CPU-bounded batch. A failed batch
must roll back acknowledgments and cache writes together; idle maintenance must
not acquire a writer. Avoid per-bucket commit churn within one maintenance pass.


Metadata-only lifecycle mirrors must enrich, rather than replace, the retained
native envelope. Preserve omitted parent/tool/stop metadata, synthetic-history
markers and itemized native usage/cost fields. Explicitly supplied mirror values
still update the corresponding fields; omitted components are last-observed
native values, not recomputed charges. Hash hints are keyed to the newly merged
canonical body. This preservation applies going forward without upstream body
backfill; unavailable earlier fields remain unknown.


Idle usage projection and inference linking without candidate receipts must not
acquire a writer lock or commit a caller's transaction. Queued records whose retry
time has not arrived remain pending while idle passes stay read-only.


Report-local compiled pricing may reuse model/tier selection but must preserve
strict thresholds and authored partial-tier order. Per-call token counts and cache
TTL evidence still determine valuation; unsupported pricing stays unknown. Account
resolution caches must be bounded and scoped to one immutable resolver snapshot,
including installation identity and all applicable binding/identity boundaries.
Cached results cannot turn owner identity into billing proof or resolve ambiguity.
Returned account objects must be independent of stored cache decisions.


Direct local inference listeners initialize the selected persistent ledger once
before accepting requests. Request workers use separate request-local connections
without schema initialization at each stream checkpoint. Request bodies commit
before upstream forwarding, and no database writer transaction spans inference or
network waits. Checkpoints and completed responses commit independently; usage
projection/replay must preserve one canonical invocation.


Usage projection may reuse decoded session state within one bounded candidate
batch, while durable progress/context writes remain atomic per record. A failed
record must discard any mutated cached state after rollback. No transient context
cache may survive the invocation or change the state reconstructed after restart.


Periodic repository discovery may resolve conventional Git metadata without a
subprocess, with per-invocation caching and Git fallback for discovery overrides
or unsupported layouts. PR metadata pages may be larger than the requested recent
scope, but only requested recent and evidence-linked/known PRs are persisted.
Targets outside a page still require individual refresh; pagination reuse cannot
silently drop targets or relax commit/diff completeness checks.


Dashboard views must fetch only the reports required for their visible content.
Work/account rendering cannot require the usage-chart dataset. Deferred capture
metrics must be labelled as unloaded, distinct from unknown evidence, and remain
accessible explicitly and through session drill-down. Client report reuse is
bounded in age; explicit refresh and visible error recovery remain available.


Selected-session work replay shares the native record parser but emits only work
context, verified commit evidence and PR-creation evidence. It must never reopen
native transcripts, infer historical repository identity from current config,
replace conflicting facts, alter native offsets, or rewrite usage/inputs/accounts/
billing. Preview uses a read-only connection. Apply parses and verifies Git objects
outside the database writer, then atomically rechecks source hashes, expiration,
relevant recorded contexts and existing row compatibility before inserting absent
facts. Replay counts distinguish bounded samples from complete session scans.
Missing context/flags/headers, expiration, decoding errors, limits and conflicts
remain visible. Continuous native work collection remains authoritative until a
bounded durable work projection handles append, mutation, replay and expiration.


The default work-report API retains its complete response. View-specific wire
shapes may omit unrelated fields but must preserve visible metrics exactly and
share the same immutable report snapshot. Account display registries preserve
latest nonempty field sources/timestamps and counts of underlying observations;
they must not carry an old record's subscription/install/session fields as a new
billing assignment. Historical evidence remains in the full endpoint and store.
Per-view encoded caches are limited to the three known views, cleared on explicit
refresh/expiry and private to each report reader. Invalid view requests fail before
report computation; backend failures remain service errors.


## Forward work-evidence projection

The history ledger queues forward Claude/Codex work-bearing records independently
of usage and edits. Activation atomically records the existing maximum sequence;
it does not seed older records. Parser state contains bounded, sanitized tool
intent and context, never raw commands or human-message text. Native tool IDs pair
calls/results across restarts. Parsing, decompression and read-only Git verification
occur outside the write transaction. Source hash, session checkpoint and recorded
repository witnesses are checked again before facts and progress commit together.

Only absent work facts are inserted; compatible native facts are reused. Changed
work-bearing source data, out-of-order work or incompatible existing facts create
a persistent session conflict. Reporting withholds automatic commit/PR attribution
for that session while preserving raw facts and independent operator links. Missing
headers, native flags or historical repository witnesses remain explicit gaps.
Owner/route metadata-only repairs do not change work semantics. Work-pending bodies
are protected from expiration alongside pending usage and edit records. The native
work reader remains enabled until forward parity supports a verified cutover; this
increment does not establish complete work coverage or the whole-tool CPU target.


Recorded repository witnesses also govern tool invocation scopes during retained
replay, including directories removed after capture. A missing directory must not
erase an observed scope transition or merge distinct work segments. Current origin
configuration may locate immutable Git objects for verification; it cannot assign
a historical scope. Missing/ambiguous recorded origin or object evidence remains
unknown. Live native parsing retains its independent current-source behavior.


## Central full-content delivery extension

CONTRACT-006-central-delivery.md governs the proposed fleet full-content wire
protocol, inference-table core and central administration. Its write-only broker
and separate accepted/materialized/projected receipts supersede the metadata-only
SQL prototype for that scope. The prototype remains available for explicit metadata
exports; it is not a full-corpus delivery implementation. No current deployment
or real export is implied. Preserve the private destination hold, forward default
and operator-selected retention/retained-upload boundary. Apply the PRD's current
demand-driven catch-up and at least 10x record/byte capacity requirements.


## Forward cloud reporting evidence

After explicit operator authorization, scoped work/PR and account/binding/quota/
recorded-charge observations use the existing signed delivery journal as
work_evidence, account_evidence and usage_evidence records. Original normalized
observation identity, content hash and source revision remain authoritative. The
receiver/Delta path stays unchanged; the private read cache applies the same
reporting projections and current project policy as the local dashboard.
Unrecorded charges and unsupported billing matches remain unknown.

The additive project_context observation contains uid, ts, tool, session, cwd
and repo. Only an enrolled forward session's observed context anchor and changes
are eligible. A context predating enrollment may annotate its first forward call;
it does not opt earlier bodies or usage into export. Activity, cumulative session
summary counters and session costs are checked against the original boundary by
both publication and cloud import. Tied contexts preserve ambiguity.

A header-only cache lane may read verified work/account items through the signed
contiguous materialization receipt. It cannot advance the native trace cursor or
import trace bodies/usage envelopes. The same item digest and record-revision
heads deduplicate later contiguous replay. Both progress cursors are explicit.
