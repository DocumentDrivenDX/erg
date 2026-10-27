---
ddx:
  id: erg.incremental-project-rollups
  status: draft
  links: [prd, erg.architecture, erg.project-hierarchy]
---
# Incremental usage and project cost reporting

The operator requires fast opening/refreshing, demand-proportional low-rate CPU,
and rapid catch-up using up to one core across erg components and helpers. The prior cold all-history read consumed 35 seconds
of reporter CPU and 210 seconds wall time. It repeated allocation and request
pricing over the full canonical corpus. An in-memory TTL cache is insufficient.

## Decisions and interfaces

- Persist hourly Usage cells and per-session cost cells in the local SQLite
  ledger. Retain priced, allocated per-request facts for exact partial-hour Cost
  intervals. Rates are applied per request before summing; reported estimates,
  reference value, billing unknowns and allocation splits retain their meanings.
- Derived canonical-winner insert/delete/update invalidates affected hours,
  including request/response peers whose mirror suppression changes. Activity
  and recorded-context mutations invalidate their affected hours. Project policy,
  rate-card or projection-version changes require all retained hours to rebuild.
  This reads already retained metadata; no upstream history import occurs.
- One dirty batch uses one pinned read-only source snapshot. Stage writes in
  short writer transactions, then atomically publish every affected hour through
  head pointers. Changed dirty serials survive publication for a subsequent pass.
  A crash, interruption or partial staging never exposes half a batch. Old
  versions are garbage-collected in bounded indexed chunks. Drain retirement
  debt under the same duty cycle rather than deleting one chunk per publication,
  which cannot keep up with repeatedly rewritten busy hours.
- Readers use completed projections, never an automatic raw rebuild after cache
  activation. Initial warming is explicit; subsequent lag and old project-policy
  revisions are visible. Refresh reloads the durable projection, not raw history.
  Uninstalled legacy/fixture ledgers retain the reference implementation.
  The reporter reuses immutable decoded hour payloads across publications, with
  a 1,024-hour bound; a changed hour does not require decoding the whole corpus.
  Source-free hourly aggregates are already REAL numbers, so the read reducer
  sums them directly instead of converting each stored float through Decimal.
  The exact Decimal/raw implementation remains the parity oracle.
- The collector owns a separate projection thread, with an exclusive local worker
  lock. Default measured thread-CPU duty is 2%, including SQLite maintenance;
  the first evidence publication cycle (including retirement cleanup) may use
  4% before subsequent passes return to 2%. Background
  usage projection is bounded to 60 ms / 128 candidates per scan, separately
  from durable raw capture;
  sleeping holds no writer lock. This reserves headroom for existing capture
  under the fleet 10% target, not a claim of a hard global scheduler limit.
- Validate total, per-project, per-folder and per-session counts/tokens/value
  against the canonical reference, including split allocation, arbitrary time
  boundaries, source corrections/deletion, mirrors, late context, price/policy
  changes, interruption and concurrent arrivals. Authorizer tests prohibit source
  history reads on the activated report path. Measure actual deployed cold reads
  and sustained collector/reporter/helper CPU before claiming acceptance.
- Projection failures do not stop the collector's capture threads. Status exposes
  the error/backlog; persisted raw traces remain authoritative. No credentials,
  conversation bodies, inference, model reload or central export are required.

## Implementation and validation sequence

1. Add bounded reference reads and the shared hourly builder/cost valuation hook.
2. Add invalidation, staging/publication, paced maintenance and indexed cleanup.
3. Wire read-only Usage/Costs and collector lifecycle; expose warming/freshness.
4. Run SQLite parity/fault tests and the complete repository unittest gate.
5. Seed from retained records under duty pacing while capture continues; verify
   live conservation, restart-independent reads and cold refresh latency.
6. Upgrade services at the existing ds4 idle boundary, preserving model/discovery
   settings. Observe the whole background process tree including scheduled work.


## Work and Accounts on the same publication

Work and Accounts read hourly evidence cells and exact partial-hour evidence
facts through the same selection planner as Costs. Each canonical call is
priced and allocated once in the worker. Its work interval, conservative account
resolution, and uniquely supported subscription-period assignment are persisted
with that price and allocation. Human-input equivalents retain physical event
counts separately. Parent/child and mirrored-session handling uses the existing
canonical winners and conservative recorded work resolver.

All four views share atomic hourly head publication and project revision. No
activated Work or Accounts request scans source calls, prompts, account history,
or Git evidence, resolves context, or looks up prices. Empty/warming evidence
returns unavailable; it never launches an HTTP-triggered raw rebuild. Legacy
unactivated ledgers retain the reference readers. Server caches include publication
identity; client caches discard older reports after observing a newer publication.
Refresh does not ask the collector to recapture history.

Work/context, parent relationships and operator links invalidate related session
hours (including descendants and pre-PR usage). Identity changes invalidate
sessions sharing recorded account identifiers, including alias changes. PR/commit
registry changes invalidate sessions supported by matching branch/commit/link
evidence. Charge changes conservatively invalidate published hours; these are
metadata/policy corrections, not a periodic raw-corpus refresh. Identical upserts
are ignored. Quota/title changes publish metadata without repricing usage hours.
The existing worker CPU duty cycle and bounded garbage collection apply to the
additional facts. Retention/source expiry invalidations rebuild these facts too;
retired evidence versions are collected with retired cost versions.

Accounts uses shared time/project/folder/machine/tool/kind/requester filters for
usage, token value and native reported estimates. The account registry and quota
history remain account-wide observations. Subscription charges are whole-period
ledger records: a scoped ratio compares selected observed token value with the
whole-period charge, and never pretends that a project incurred that fee. Account
ownership, inference routing, subscription attribution, API-equivalent value,
native reported estimates and actual billed spend remain distinct. Unknown
account rows explain missing evidence; unmatched/ambiguous billing remains unknown.
Subscription period references use stable account/subscription/interval keys,
never array offsets that could change during metadata updates.


## Bounded publication cycles and observable lag

Ordinary maintenance publishes one hour per transaction. Two turns prefer the
newest dirty hour; the third serves the oldest debt. Every publication swaps the
same Usage/Costs/Work/Accounts heads. Project-policy, rate-card and definition
changes retain a full atomic rebuild, so allocation revisions never mix. Dirty
serials changed during a source snapshot remain queued. Per-hour source timestamps
record when the pinned read began, separately from publication time.

Retirement yields after at most 128 indexed chunks or 100 ms measured thread CPU
per cycle, including pacing waits outside transactions. Each chunk retains the
256-row/table mutation bound. Cleanup continues on subsequent cycles rather than
blocking every fresh publication until the entire retirement queue empties.

Forward work projection parses an ordered prefix outside a writer transaction,
using earlier staged contexts as witnesses for later records. Up to 64 validated
records commit together; preparation targets 40 ms CPU / 50 ms wall time and
selects at most 128 candidates. A single record or its bounded writes can exceed
that allowance; it is not a hard cap. Source hashes, queued hashes, checkpoints,
recorded witnesses, receipts and conflicts are checked under the writer lock.
A changed record defers its remaining session; failed writes roll back facts and
checkpoints together. Unknown or conflicting attribution remains unknown.

The collector checkpoints sampled capture/usage/work progress in derived metadata.
The lightweight `/api/reporting/status` route reads that metadata and published
hour watermarks without scanning history. The UI polls it every 30 seconds while
a reporting view is visible. It distinguishes the rendered publication from the
latest published usage source snapshot, pending usage/work records, pending hours
and capture-check age. A newer publication prompts refresh without relabeling an
old rendered report as fresh. Snapshot age and latest event age are not proofs
of complete source coverage; unrecorded watermarks remain explicitly unavailable.


Small batches expand to include cross-hour canonical-winner dependencies. Mutation
triggers connect old/new request hours, mirror peers and global prompt/commit
winner hours. Edge serials preserve new mutations arriving during a build; the
published cohort retires only its captured dependency serials. A one-time atomic
pass covers debt queued before dependency tracking was installed. Its pending
state is explicit; it imports no upstream data. Dependency discovery uses indexed
UID and mirror-peer probes, not corpus scans on source inserts.


During retained projection catch-up (usage pending >1,000, work pending >10,000,
or progress not observed yet), periodic enrichment continues covered retained
inventory, legacy Copilot/OpenCode adapters and collected perf samples. It defers
repository-wide Git statistics, historical host CSV/iStat ingestion and remote PR
refresh. Native work evidence, live sampling and trace capture continue. The next
scheduled pass resumes optional enrichment when queues are below those thresholds.
Capture status exposes `enrichment_deferred_reason=projection_backlog`; the common
health bar names the deferral without claiming a successful PR refresh. This
backpressure avoids spending the catch-up CPU reserve on repeatable metadata.


Background writer connections use managed WAL checkpoints and FULL synchronous
commits. Repeated automatic checkpoints at every commit were expensive while a
long reporting snapshot pinned WAL pages. The paced reporting worker now attempts
PASSIVE checkpointing at most once every 30 seconds, between rebuilds and outside
write transactions. It temporarily uses a zero busy timeout and restores it;
active readers/writers retain their transactions. Checkpoint work enters the same
worker CPU duty accounting. Standalone connections keep their existing automatic
checkpoint defaults. Unit tests cover pinned-reader isolation, committed data,
writer coexistence, transaction guards and checkpoint cadence.


## Demand-driven catch-up

The current PRD resource policy supersedes earlier 2%/4% rollup and 10% whole-tool
limits above. Capture must remain independent of derived queues. The managed
usage/edit/work worker drains bounded transactions continuously while progress is
possible, shares one CPU budget, and sleeps when empty or retry-blocked. Reporting
uses a small share while corpus projection is pending, then uses the available
catch-up budget. No database writer lock may span pacing, source subprocesses or
network waits. Failed/retry-blocked work must not cause a busy loop.

Capacity acceptance compares representative record and byte arrivals with actual
processing capacity, targeting at least 10x at each stage. Include large payloads,
source corrections, bursts, out-of-order evidence and recovery after offline time.
Measure active catch-up and the return to idle independently. Keep missing work
context, cost/account uncertainty and incomplete raw traces explicit even when a
queue has been fully acknowledged. Publication and cleanup remain bounded and
atomic; increasing catch-up speed does not relax those invariants.
