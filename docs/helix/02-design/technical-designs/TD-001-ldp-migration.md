---
ddx:
  id: TD-001
  authoring:
    home: repo
  links: [erg.architecture, erg.central-delivery]
---
# Technical Design: generated LDP canonicalization

## Scope

Migrate the one-Mac central canonicalizer under CONTRACT-006 while preserving the
stdlib-only local runtime, immutable spool/Zerobus admission and existing Delta
reporting interfaces. The source is the checked UMF document under
`central/ldp/umf/`, using UMF core 0.10.3 and TableSpec pipeline vocabulary 0.2.0.
No new core vocabulary, compiler or public namespace alias is introduced.

## Technical Approach

Compile through TableSpec's owning emitter. Generated raw streaming, sequence
state, revision archive, fact-event and latest-head datasets are the business
graph. Erg owns only the pure pinned codec, source admission and protocol receipt
coordinator. Build output records exact source/document/compiler/artifact hashes.

Keep legacy completed revisions in the existing canonical Delta tables. Pin the
old writer's final materialized and projected boundaries, stop it, seed the new
append source with exact raw rows above the materialized boundary, then admit
new visibility-proven deliveries. This preserves incomplete chunk groups while
avoiding reprocessing the completed corpus. The coordinator checks all new facts
against legacy immutable keys and hashes before copying proven prefix rows into
`delivery_items` and `records`. Existing reporting views and time-travel readers
continue to consume those tables.

## Component Changes

- `central/ldp/project.py`: bind declared deployment profiles and invoke the
  existing compiler; record generated-file provenance without editing output.
- `central/erg_central/ldp_codec.py` and `disposition.py`: shared pure validation,
  bounded reconstruction and projection disposition; no Spark or publication.
- `central/erg_central/ldp_stage.py`: bounded spool discovery, exact Zerobus
  visibility and append-source admission; its private completion cache cannot
  acknowledge canonical materialization.
- `central/erg_central/ldp_publish.py`: fixed dependency snapshots, legacy conflict
  checks, bounded contiguous-prefix proofs and idempotent compatibility writes.
- `central/erg_central/ldp_authority.py`: actual backend observation and final
  authority recheck. TableSpec owns its typed opt-in evidence model/evaluator.
- `central/ldp/deployment.py`: paused three-task orchestration around the existing
  generated pipeline; backend-normalized settings and external config digest bind
  the applied definition. No business graph or cloud activation is generated here.
- `central/ldp/qualification.py`: deterministic synthetic scenarios and exact
  comparisons of native output, heads, sequence bytes and reporting payloads.

Allowed dependency direction is central adapters → pure Erg codec/protocol and
TableSpec public LDP API. The installed `erg/` runtime cannot import TableSpec,
Spark or Azure. `scripts/check-delivery-boundaries.py` and `make quality` enforce
that boundary. Generated code remains outside handwritten shape metrics.

## API/Interface Design

CONTRACT-006 governs source admission, canonical facts, receipt semantics and
single-writer migration. TableSpec's LDP capability API governs typed stages,
archive dependencies and publication authority. Existing broker and reporting
interfaces retain their schemas and consumers. No cloud credentials enter UMF,
fixtures, generated output or committed evidence.

## Security

Use existing private workspace/volume paths and the approved processor identity.
Qualification namespaces contain fabricated data only. Content appears only in
the private tables whose payloads the test compares; operational evidence records
counts/digests, never real session bodies or credentials. Effective access and
artifact readback are checked before native execution. No data-grant expansion or
retention activation is part of cutover.

## Testing

| Requirement | Test surface | Required outcome |
|---|---|---|
| UMF is authoritative | Generated-output parity/provenance tests | Exact files equal the owning emitter; scope, BIGINT and all five dependencies retained |
| Codec closure | Pinned artifact/schema tests | Hash mismatch and unexpected runtime errors fail the flow |
| Replay/chunks/revisions | Native multi-update fixtures | Exact wire/payload/hash parity; incomplete remains pending; all revisions retained; no duplicate facts |
| Conflicts and scope | Sequence/revision/malformed/foreign fixtures | Producer rejects foreign scope before append; generated selector excludes it; in-scope corruption yields durable rejection and blocks publication |
| Integer correctness | Values above 2^31 and 2^53 | Exact BIGINT and nullable state values survive native Pandas boundaries |
| Reporting | Real observation-codec fixtures | Shared usage/work/account payloads and project allocations match reference |
| Authority | TableSpec plus actual backend collector tests | Default fencing refusal; opt-in only; every stale/mismatched/competing case blocked |
| Crash/restart | Same checkpoint, idempotent copy and publication-fault cases | Durable facts survive; no premature/gap-skipping or backward receipt |
| Checkpoint loss | Fresh graph reading the same synthetic source from version zero | Exact reconstructed state and conflicts; no duplicate canonical facts |

Run the central generated-output suite in an isolated compiler environment and
`make quality` for local runtime gates. Native qualification must use the exact
emitted files and installed base wheel, preserving managed Spark/Pandas/PyArrow.
Record offered items/bytes, completed update wall time and remaining backlog;
local state-machine tests cannot establish native checkpoint durability or speed.

## Migration & Rollback

Keep production running during synthetic qualification. Save and hash complete
job settings, runtime archives, boundary receipts and canonical snapshots. At
cutover pause scheduled starts, drain the old run, seed the declared source and
install a stage → generated pipeline update → publish job with concurrency one
and queue disabled. Verify actual settings and one writer before each receipt.
On failure stop/drain the new job, reconcile durable canonical data and cursors,
then restore the exact old job definition/archive. Do not reset cursors or delete
old/new immutable archives. General fleet enrollment remains separately gated.

## Risks

Native runtime/library changes fail pinned admission; fixture faults remain
isolated. Cold pipeline startup and compatibility writes may dominate runtime;
measure completed work before claiming capacity. Serialized-job authority relies
on the backend exclusivity boundary and does not protect against arbitrary
privileged out-of-band writers; the explicit policy never claims fencing.

Native synthetic results, including crash/restart, exact wire parity, independent
cursors and the 16,796-item capacity fixture, are recorded in
[`central/ldp/native-evidence.json`](../../../../central/ldp/native-evidence.json).
Production remains on the prior finite writer pending independent review and
the controlled cutover.
