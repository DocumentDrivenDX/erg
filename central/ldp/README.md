# UMF / Lakeflow migration

The authoritative declaration is `umf/revisions.umf.json`, using UMF core 0.10.3
and TableSpec pipeline vocabulary 0.2.0. `project.py` binds an explicit deployment
profile and calls `tablespec.ldp.generate_ldp_project`; it writes the resolved
UMF, exact emitted files and a source/hash manifest. It contains no alternate
Spark graph. Only the raw-source SQL and generated Python are runtime libraries.

The compiler is pinned by source commit in this directory's separate `uv.lock`.
Run `make ldp-test` at the repository root. CI runs the same generated-output
checks. These compiler dependencies are separate from Erg's stdlib-only local
runtime. The deployment wheel is built from that exact compiler source; native
admission additionally checks package versions, compiler files and the selected
UMF platform binary. Serverless can allocate either ARM or x86, so deployment
requirements must pin a wheel for each supported architecture with conditional
markers and hashes. Do not install a replacement PySpark over the managed runtime.

## Generated processing and publication

The generated graph incrementally reads an explicitly scoped, append-only Delta
source. It preserves exact wire bytes, collapses identical sequence replays,
records sequence/revision conflicts, reconstructs bounded revision groups,
appends every immutable revision, and derives latest heads separately through
AUTO CDC. Archive and fact-event tables disallow full refresh. Incomplete chunks
remain pending across updates; they are never evicted through a watermark.

`erg_central/ldp_codec.py` is the declared pure transform. Its version, archive
hash, schema hashes, grouping, maximum count/bytes and rejection type are declared
in UMF. Operational import/artifact/runtime failures fail execution; they cannot
be disguised as rejected source records. `qualification.py` compares exact scope,
wire bytes, every revision, hashes, dispositions and latest heads with the shared
SQLite reducer. Reporting tests use the shared usage/work/accounting logic.

The staging task proves Zerobus visibility and appends validated rows to the
new source. It uses a separate `ldp-staging` control index and cannot publish
canonical receipts. The publication task pins all generated, source and legacy
Delta dependencies, checks conflicts against completed legacy history, proves
bounded independent contiguous materialized/projected prefixes, copies proven
facts idempotently into the existing canonical tables, and verifies durable
copies before publishing the existing status document. Unknown projections and
earlier gaps block the corresponding cursor.

The human approved an explicit `serialized_job_execution` authority profile.
Publication verifies the actual job definition, sole active job/run/task,
concurrency one, queue disabled, immutable workload/configuration hashes and
fresh backend observations before and after durable writes. This profile does
not assert a distributed fence. TableSpec still requires fenced authority by
default; Erg opts in explicitly. Only one canonical writer may run.

## Qualification and cutover

Native tests use new `ergqual_*` sources, tables, checkpoints and private control
paths with fabricated identities. `scripts/qualify-ldp-output.py` reads exact
completed native update snapshots; local tests alone establish neither native
checkpoint durability nor capacity. Test split/missing/duplicate chunks,
out-of-order revisions, identical replay, conflicting sequences/revisions,
invalid identity/hash, large integers, projection lag, interrupted publication,
and replay into a fresh graph. A conflict must block publication even when
another flow succeeds. Never reset an immutable production archive.

Record native update wall time including startup, unique items and wire bytes,
separately from duplicate attempts. Compare equivalent offered backlog with the
existing processor. No fleet or incremental-recompute claim follows from codec
unit tests or a small synthetic update.

`scripts/build-ldp-job.py --profile PROFILE.json --output job-template.json`
builds the three-task Jobs settings for an already compiled pipeline. The profile
requires `name`, `processor_principal`, `pipeline_id`, `bootstrap`,
`application_archive`, `deployment` and `requirements`. Bootstrap, application
and requirements paths contain their SHA-256; the private deployment path is
versioned and its digest is passed separately. The generated schedule is paused.
This does not create or activate resources. The existing `databricks.yml` remains
the legacy job definition and must not be reapplied over a migrated job.

Read back backend-normalized template settings, embed them in the deployment
configuration, then bind its external digest using
`ldp_authority.bind_job_settings`. Reset the full definition and verify exact
readback before activation. Archive every applied definition for rollback; a
schedule change also changes the pinned definition and requires a new binding.

Before cutover, retain the exact previous job settings, application archive,
permissions and final materialized/projected boundaries. Seed the new source
from a pinned visibility-proven raw snapshot above that completed boundary,
including pending chunks. Staging ignores sequences at or below this declared
admission floor; production publication refuses a missing, unscoped or regressed
receipt. The producer rejects foreign scope before append. The generated source
selector includes enrollment, installation and generation, so foreign identities
are outside this stream; corruption inside that scope is retained as rejection
evidence. Warm generated targets without writing compatibility
or receipt state. Stop and drain the old writer before enabling the
stage → pipeline → publish job. Reset the entire job definition so array entries
cannot leave an old task behind. Keep concurrency one and queue disabled.

Cutover requires independent review of native output, restart and publication
evidence. Rollback stops/drains the new writer, reconciles durable facts and the
last published cursors, and restores the previous exact job definition/archive.
Keep completed canonical history, reporting interfaces, one-Mac scope, private
access and disabled real-content retirement intact.

See [the migration design](../../docs/helix/02-design/technical-designs/TD-001-ldp-migration.md)
for the contract and acceptance matrix.
