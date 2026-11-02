---
ddx:
  id: erg.central-delivery
  status: draft
  authoring:
    home: repo
  links: [prd, erg.architecture, erg.contract.001, erg.project-hierarchy, erg.incremental-project-rollups]
---
# Central session delivery and administration contract

**Contract ID**: CONTRACT-006
**Type**: HTTP API, event and data schema
**Version**: proposed v1

## Purpose

Define lightweight full-content delivery from enrolled collectors to the private
Databricks corpus, independent of the server-side ingestion mechanism. This
contract governs proposed interfaces, not production deployment or export approval.

## Scope and Boundaries

- Producers: standard-library-only erg collectors on Intune-managed Macs receiving
  `as-agent`; local SQLite remains authoritative during disconnection.
- Consumers: an authenticated write-only broker, durable central spool, canonical
  processor, Unity Catalog reporting and authenticated administration.
- Only Erik is an ordinary human reader/administrator. Service identities receive
  scoped processing privileges; privileged platform recovery remains possible.
- Infrastructure owns enrollment/authentication, private hosting/storage, effective
  access and the forwarding capability spike. Erg owns envelope/schema, local
  outbox, reconciliation and reporter semantics.
- No central deployment, existing-body export, native-history backfill or retention
  deletion is authorized by writing this contract. Apply the existing private
  production catalog/access hold before any real-content export.

## Normative Surface

### Central deployment profile and qualification

The selected live ingestion path remains native Zerobus REST. The synthetically qualified
isolated landing schema is `erg.zerobus_landing`, target `delivery_items_raw`,
with its own managed storage root. `ingest_schema` selects only that raw target;
it MUST NOT change the private acceptance spool, control volumes or canonical
`erg.telemetry` namespace. The original attempted raw table MUST NOT be recreated
or deleted to adopt the new landing schema. The existing 18 Gateway core columns
and five Erg extensions remain unchanged; raw session records remain separate.

Infrastructure owns the reviewed storage/network reachability change, UC schema,
locations, bindings and grants. Erg owns table/view migrations and its single
finite job. Storage endpoint reachability does not confer data authorization.
A regional service tag is not workspace identity, and NSP Learning/Transition
MUST NOT be represented as Enforced. No network plan, DDL preparation or schema
compatibility result alone qualifies insert or actual row materialization.

Only after infrastructure's concrete network-change review and provisioning may
the job migrate the new target and reconcile the four accepted synthetic batches.
The broker and pilot client can stay disabled for that step. Qualification MUST
prove actual Zerobus insert, Delta visibility, all 16 delivery items/eight immutable
revisions/seven current heads, exact hashes/chunk reconstruction/source order,
canonical deduplication and identical finite replay. Only then briefly reopen the
exact synthetic identity for signed status/receipt replay and revoke/close again.
Accepted/materialized/projected acknowledgements remain distinct. Real-content
export, fleet enrollment, retained import and deletion remain gated.

### Enrollment and wire transport

Collectors MUST use HTTPS with an enrollment-scoped credential. The broker MUST
validate issuer, audience, immutable subject, active enrollment and installation
binding. A hostname, email, serial or submitted enrollment ID is not authentication.
Silent Mac bootstrap, rotation and revocation MUST pass the infrastructure spike.
Collectors MUST NOT receive shared administrator credentials or corpus SELECT.

`POST /v1/batches` accepts `application/json` UTF-8, without request compression in
v1. Initial limits are 500 items and 2 MiB of complete encoded request bytes.
The client MUST persist exact request bytes before submission and send their
SHA-256 as `X-Erg-Batch-SHA256`. Duplicate retries MUST reuse the same bytes/ID.

| Field | Type | Required | Rule |
|---|---|---|---|
| `protocol_version` | integer | yes | Exactly 1. |
| `batch_id` | UUID string | yes | Unique within enrollment; immutable bytes/digest. |
| `enrollment_id` | opaque string | yes | Must match authenticated enrollment. |
| `installation_id` | opaque string | yes | Stable persisted collector identity bound at enrollment. |
| `destination_generation` | opaque string | yes | Server-issued data-plane generation; not a caller-selected catalog. |
| `items` | array | yes | 1..500 items, ordered by delivery sequence. |

Each item has these fields:

| Field | Type | Required | Rule |
|---|---|---|---|
| `delivery_seq` | positive int64 | yes | Monotonic per enrollment/generation; allocated transactionally. |
| `record_id` | nonempty string | yes | Stable erg identity; never a fake Gateway identity. |
| `record_revision` | positive int64 | yes | Monotonic per record; corrections/tombstones are new revisions. |
| `record_kind` | enum string | yes | session_event, inference_observation, usage_evidence, work_evidence, account_evidence, quota_evidence, host_metric, observed_resource, content_tombstone. |
| `record_sha256` | 64 lowercase hex | yes | SHA-256 of full reconstructed `payload_json` UTF-8 bytes. |
| `payload_json` | JSON-object string | unchunked | Contains identity, kind and all semantic fields; redundant identities must agree with the envelope. |
| `chunk_index` / `chunk_count` | integer | chunked | Zero-based index, positive total; both required. |
| `chunk_base64` | base64 string | chunked | At most 512 KiB decoded; payload and chunks are mutually exclusive. |
| `chunk_sha256` | 64 lowercase hex | chunked | Digest of decoded chunk bytes. |
| `record_bytes` | positive int64 | chunked | Full reconstructed byte length, initially at most 64 MiB. |

Within enrollment + destination generation, each `delivery_seq` MUST bind
immutably to record ID/revision, chunk index and digest, even across different
batch IDs. A different item reusing the sequence is a 409 conflict. The broker may store immutable bindings in bounded CAS shards (initially 512 sequences); every conditional update preserves existing bindings. A batch is acknowledged only after its exact body, all shard claims and the immutable accepted marker are durable. Partial shard writes after a crash are reservations, not accepted batches. A reinstall
MUST recover its prior identity/journal or obtain a new server-issued generation;
it MUST NOT restart sequence 1 within an existing generation. Previous-generation
gaps remain visible; reinstall does not declare missing data complete.

A chunk is an item with its own `delivery_seq`. All chunks share the record key,
revision, count, length and full digest. Reconstruction concatenates indices in
order and verifies every digest, UTF-8, identity and JSON schema. Conflicting
chunks are errors. Partial records MUST remain incomplete and MUST NOT appear as
complete conversations, inference or usage. Oversize records remain local with
explicit unsupported-size diagnostics; never truncate or silently discard them.
Limits are capability-spike gates before feature implementation; version changes
or server-advertised lower bounds must not silently strand supported local events.

The local capture transaction MUST persist each new revision and corresponding
outbox item atomically. `history_events.seq` alone is not a revision cursor:
updates to an existing event retain its original row sequence. A dedicated
monotonic delivery sequence and revision journal are required.

### Acceptance, materialization and replay

A `202` response MUST mean the exact immutable batch bytes and digest are durably
committed to central storage. Ephemeral Function/App memory or a scheduled task
is insufficient. Conditional storage creation makes batch ID/digest replay safe.
A lost response requires resubmitting identical bytes. The server MUST reconcile
spooled batches independently of notification delivery.

```json
{"protocol_version":1,"batch_id":"123e4567-e89b-42d3-a456-426614174000","batch_sha256":"<matching-request-digest>","enrollment_id":"enrollment-a","destination_generation":"pilot-1","state":"accepted","accepted_delivery_seqs":[1],"materialized_through":0,"projected_through":0}
```

Acceptance means durable spool only, not queryability. The response MUST bind
receipt to the authenticated enrollment, batch digest and destination generation.
A client MUST NOT advance progress on a mismatched/missing receipt. Accepted item
ranges/sets are immutable receipts; a contiguous accepted cursor MUST NOT jump a
hole. Repeated identical batches return the same accepted-item set.

`GET /v1/delivery/status` returns only that enrollment's receipts, missing ranges,
materialized cursor, projected cursor and generation. It MUST NOT expose another
enrollment's records. Receipt listings use `limit` 1..500 and an opaque snapshot/generation-bound `cursor`; responses include `next_cursor`. Materialization requires verified complete records in the
owned data plane; projection requires the corresponding canonical publication.
For a chunked record, every item's materialized disposition requires verification
of the complete manifest and all chunks; mere chunk presence is insufficient.
Each materialized item receives a durable projection disposition: `applied`,
`not_applicable`, or an explicit unresolved/error state. `projected_through`
advances contiguously across `applied` and policy-versioned `not_applicable` only.
A known raw-only record may be `not_applicable`; unsupported formats, processing
failures or incomplete evidence MUST NOT use that disposition to hide a gap.

Generation changes require explicit replay/reconciliation, never a silent cursor
reset. Central deduplication uses enrollment + record ID + revision + digest.
Older revisions do not replace newer ones; tombstones dominate older content.
A repeated identity/revision with a different digest is a persistent conflict.

Default content enrollment is forward-only. Existing local event identities at
the central enrollment boundary are ineligible for automatic body upload, even
if later metadata repair changes their revision. Exporting a retained range
requires a separate explicit rollout policy. New restored-context events retain
their provenance and never become new metered inference. No upstream log reread
is an implicit upload or installation step.

Local and spool expiration MUST NOT silently discard unaccepted or unmaterialized
content. Spool retirement requires verified materialization and the selected
retention/recovery policy. Content tombstones retain identity/revision evidence
and prevent replay resurrection. The retention period remains a rollout decision;
unset policy MUST NOT trigger automatic deletion.

### Canonical session and inference data

Owned logical schemas are `erg.ingest`, `erg.telemetry`, `erg.admin` and
`erg.reporting`. Collectors do not choose physical tables. Gateway-managed payload
and usage tables MUST NOT be modified by erg.

`session_events` preserves full native conversation/tool bodies independently of
inference: enrollment/machine/installation, agent/native session/parent/entry IDs,
record/revision, source order, event/capture/ingestion times, parser version,
content hash, body availability, reconstructed/synthetic provenance and expiry.
Source order is independent of arrival order and native event time. Revisions
and missing chunks remain inspectable. A turn or tool result is not automatically
an inference request. Transport credentials/credential files are excluded; user
content remains sensitive and requires the private content policy.

`inference_calls` MUST preserve the production Unity Gateway 18-column core:

| Type | Columns |
|---|---|
| TIMESTAMP | event_time |
| STRING | request_id, invocation_id, request, response, destination_type, destination_name, destination_model, requester, schema_version, url, api_type |
| MAP<STRING,STRING> | request_tags |
| INT | status_code |
| DOUBLE | sampling_fraction |
| BIGINT | latency_ms, time_to_first_byte_ms |
| ARRAY<STRING> | logging_error_codes |

Add namespaced erg record/revision, machine/enrollment/installation, tool/session,
parentage, observed repo/folder context, provider/account evidence, token/cache/
reasoning buckets, usage provenance, capture time and retention fields. Preserve
Gateway `schema_version` separately from envelope/parser/projection versions.
Unknown native HTTP fields remain NULL. Transcript text MUST NOT be fabricated
into observed request/response wire payloads or Gateway-issued IDs.

Canonicalization reconciles only exact, uniquely supported call identities.
Similar timestamps, models or token totals are insufficient. Keep ambiguous
source overlap and payer/charge uncertainty visible. Allocate each canonical call
once using the same temporal project policy as local reporting.

### Administration and reporting

`GET /v1/config` returns an enrollment-authorized snapshot with `config_revision`,
projects, aliases, applicable temporal bindings, pricing/retention policy versions
and an ETag. Devices cache it durably for offline use. It exposes no corpus bodies
or other enrollments' private session assignments.

Central tables MUST retain stable project keys and aliases; typed repo/folder/
agent-project/session selectors; effective UTC intervals; explicit priority;
integer-millionth split weights; unassigned remainder; policy/evidence provenance;
and immutable revisions/tombstones. Equal-priority conflicts remain unresolved.
Preserve imported local keys through aliases. Renames never rewrite historical
captured context. Central master metadata and device observations/proposals are
separate authorities; log ingestion cannot mutate permissions or master projects.

An authenticated administration write path MUST be separate from read-only SQL
and collector log submission. Mutations carry `mutation_id`, `base_revision`,
actor/reason and typed operation; exact retries are idempotent, stale bases conflict.
Account/subscription/quota/charge evidence remains separate from estimated value.
Desired/effective permission audits do not enforce permissions: Unity Catalog,
Entra and infrastructure configuration do. Publishers retain policy and source
watermarks for atomic versioned rollups shared across local/central views.

## Precedence and Compatibility

PRD scope and CONTRACT-004 allocation rules govern. Existing CONTRACT-001 metadata
SQL delivery remains a separate prototype; it does not implement this full-content
wire protocol. Additive schema changes need negotiated support; breaking identity,
authorization, digest or receipt semantics require a new protocol version.

Capture, projection, upload and publication capacity MUST target at least 10x
representative record/byte arrival rates, including bursts, large events, revisions
and outage recovery. Measure active work and return to idle separately. Catch-up
may use up to a core; low-rate use must fall with demand. No fixed polling delay
may cap productive queued work below the capacity target. Failed/retry-blocked
queues back off without spinning. Do not game the ratio by filtering raw traces.

## Error Semantics

| Condition | HTTP / outcome | Recovery |
|---|---|---|
| Bad JSON, digest or schema | 400 / no acknowledgement | Preserve local item; diagnose without body logging. |
| Expired credential | 401 | Renew; retry same bytes. |
| Wrong/revoked enrollment or scope | 403 | Stop submissions; preserve local backlog. |
| Reused identity with conflicting digest, stale admin base | 409 | Quarantine conflicting revision; expose gap; no cursor advance. |
| Size bound exceeded | 413 | Lossless chunking within advertised bounds; otherwise retain locally and report unsupported size. |
| Rate limit | 429 + Retry-After | Bounded exponential backoff with jitter. |
| Network loss or 5xx | ambiguous / pending | Retry identical batch; durable deduplication resolves uncertainty. |
| Accepted but not materialized/projected | 202 / separate lag | Reconcile receipts; no fabricated completeness. |

A poison record MUST NOT disappear or indefinitely block unrelated deliveries;
accepted sets may be non-contiguous, but completeness cursors cannot cross the gap.
Operational errors MUST NOT include bodies, credentials or arbitrary SQL payloads.

## Examples

```json
{"protocol_version":1,"batch_id":"123e4567-e89b-42d3-a456-426614174000","enrollment_id":"enrollment-a","installation_id":"install-a","destination_generation":"pilot-1","items":[{"delivery_seq":1,"record_id":"event-a","record_revision":1,"record_kind":"session_event","record_sha256":"<sha256-of-payload-json-utf8>","payload_json":"{\"record_id\":\"event-a\",\"record_revision\":1,\"record_kind\":\"session_event\",\"tool\":\"pi\",\"session\":\"native-session\",\"source_seq\":1,\"body\":{\"type\":\"message\",\"message\":{\"role\":\"user\",\"content\":\"Hello\"}}}"}]}
```

The example uses digest placeholders; an executable fixture MUST compute actual
hashes. Contract tests MUST cover duplicate/lost acknowledgements, corruption,
revision conflicts, chunk loss/reordering, generation changes, revoked devices,
missed notifications, no cross-enrollment reads, secret exclusion, expiry replay,
project conflict conservation and at least 10x measured recovery capacity.

## Non-Normative Notes

The selected path is an Azure Function broker with an immutable private Blob spool,
then one finite Databricks bundle job scanning the spool, forwarding with Zerobus
JSON REST, verifying materialization, canonicalizing and publishing receipts. It
starts PAUSED and admits at most one concurrent run. Infrastructure owns platform
resources; erg owns broker/processor code, table/view DDL and bundle jobs. Do not
create the same jobs/tables in Terraform. A native processor identity uses scoped
server-only credentials; absent/unqualified credentials fail closed. No implicit
notebook OAuth is assumed. A fallback requires reporting a measured incompatibility
and an explicit decision; Auto Loader is not silently substituted. Authentication,
private-storage routing, size and replay qualification remain production gates.
No Databricks SDK, OTLP encoder or sidecar is required on the Mac.


### Terminal receipt discovery index

The processor MAY cache batch IDs and original batch digests only after a private
control receipt explicitly records `items_staged=true`. Acceptance, observed high
water and a partial Zerobus landing MUST NOT enter this index. Completed immutable
batch directories can then be skipped before opening their markers or bodies.
The receiver's immutable batch-ID binding remains authoritative. Index corruption
or digest conflicts fail closed. No receipt or trace is deleted. Rebuilding the
index reads at most 50,000 private control receipts with at most 16 concurrent
reads; the cache is limited to 16 MiB and requires sharding beyond that pilot
limit. Index writes use the existing atomic private-control publication.

Discovery and processing have distinct time budgets, so returning a discovered
candidate after a slow scan MUST NOT result in a successful zero-work run. The
existing finite job timeout, single concurrency, UNPAUSED authorized enrollment,
original signed batch receipts and contiguous materialization acknowledgements
remain in force. The index changes discovery cost, not receipt meaning.


### Generated LDP migration and receipt authority

The canonicalization graph MUST be compiled from versioned UMF documents with
`tablespec.ldp.generate_ldp_project` and the typed `tablespec.pipeline:0.2.0`
extension. The compiler, UMF core, transform closure, stage schemas and emitted
files MUST be pinned by version and digest. Handwritten pipeline graphs and
unsupported UMF namespace aliases are not alternative compiler paths.

The source producer remains the enrolled collector through immutable acceptance
and the qualified Zerobus landing table. A serialized staging task may append
exact, visibility-verified wire rows into an owned Delta append source declared
by UMF. Admission MUST reject enrollment, installation or generation mismatches
before writing that dedicated source. The generated raw reader selects the full
declared scope; rows outside that selector are not admitted revision events.
Acceptance or raw visibility MUST NOT publish materialized/projected
progress. The source's initial admission boundary MUST name the completed legacy
prefix and pinned raw snapshot. Staging MUST omit deliveries already covered by
that completed prefix. A migrated production publisher MUST require the existing
scoped receipt at or above the declared boundary; missing state MUST NOT be
replaced with a zero cursor. Admission MUST preserve every pending chunk and never
skip an unmaterialized sequence. Already completed legacy revisions remain in
the existing canonical tables. New generated facts MUST be checked against that
immutable legacy history before publication, including same-key/different-hash
and same-sequence/different-wire-byte conflicts.

The generated archive retains every post-boundary immutable revision and durable
conflict event. Latest heads are a separate derivative. The coordinator MUST pin
all declared dependencies, prove conflict-free readiness and contiguous progress,
and copy only proven complete facts/items into the existing canonical Delta
interfaces. Reporting consumers retain their current tables, snapshot admission,
shared payload interpretation and project allocation rules.

The operator approved explicit serialized-job authority on 2026-10-29. Erg MUST
opt in through TableSpec's `allow_serialized_job_execution` publication policy;
this approval does not change TableSpec's fenced-writer default. Publication
MUST verify the actual job/run/task, code and complete job-definition pins, sole
active run, concurrency one and queue disabled from fresh backend facts. It MUST
retain the hashed observation and recheck immediately before receipt writes.
A changed definition, competing run, failed API call, expired evidence or any
unresolved scoped conflict MUST prevent publication. Serialized exclusive
execution MUST NOT be described as a distributed fenced lease. Receipt cursors
remain monotonic and independently scoped; a newer ready flow cannot jump a gap.

Cutover MUST stop and drain the old canonical writer before enabling the new
one. Initial qualification uses separate synthetic tables/checkpoints/control
paths. Rollback restores the saved old job definition and immutable archive only
after the new writer has stopped, preserving durable canonical writes and last
published cursors. No source body, receipt history, data grant or retention policy is
deleted or broadened by this migration. The existing processor may receive
`CAN_VIEW` on its own job to evaluate the approved authority; that metadata-only
permission does not authorize job edits or broaden Unity Catalog access.
