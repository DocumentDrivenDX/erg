# Bounded central discovery

Production reconciliation is a Databricks Asset Bundle Python job. The
UMF/TableSpec-generated Lakeflow project in `ldp/` remains a separate migration
candidate; this fix does not switch canonical writers.

The incident on 2026-10-29 showed successful scheduled runs with zero attempted
batches and nearly all processor time spent in discovery. The FUSE directory
walk could enumerate a wide batch directory before reaching its budget check.
The old relative-path cursor did not advance when enumeration exhausted the
budget before yielding another accepted marker.

For Unity Catalog `/Volumes/` paths, `erg_central.discovery` uses the Files API
with at most 1,000 entries per page. A bounded DFS stack saves each directory's
opaque continuation token and its remaining page entries. It advances even when
no eligible batch has yet been found. It descends only through the receiver's
version/scope/batches layout. Marker metadata reads use at most eight threads;
discovery never reads a batch body. A page request and already-started reads may
finish beyond the nominal discovery deadline, so the budget is cooperative,
not a hard network deadline.

`processor-discovery-pages.json` is independent of the legacy local filesystem
cursor. Selected candidates remain in its bounded retry list until terminal
receipts or quarantine resolve them. A crash or processing deadline cannot
acknowledge a batch merely by advancing discovery. A completed sweep wraps to
find new low UUIDs and directories whose accepted markers arrived late.
Candidates found within a window are ordered by delivery sequence; this is not
a global source-order guarantee across unvisited pages. Canonical publication
still proves contiguous materialized and projected prefixes separately.

The existing `processor-staged-index.json` is a migration base. New terminal
completions update only one of sixteen digest-prefix files under
`processor-staged-shards/`. Startup validates and merges the base and shards,
rejecting inconsistent batch digests. Each file retains its size guard. These
files cache terminal receipts; acceptance and high water alone are insufficient.
There is no full legacy-index rewrite on each cohort.

The job reports discovery page/entry counts, selected candidates, budget expiry,
sweep completion and a state of `work_found`, `scan_in_progress` or
`sweep_complete`. A partial scan with no candidates is not evidence of an empty
backlog. Compare successive durable scan positions and terminal receipt progress
when diagnosing lag; these counters alone do not prove sustained capacity.

## Validation and deployment

`tests/test_processor_discovery_pages.py` covers interrupted page progress,
unacknowledged retry, completion pruning, late markers, unsafe API paths,
nonadvancing tokens, identity conflicts and shard migration/conflicts.
`scripts/qualify-central-discovery.py` runs with an explicit immutable archive
hash and a fresh synthetic qualification namespace. It checks real runtime Files
API pagination, retries and shard restart equality, then scans production
metadata using a separate qualification cursor without reading bodies or
publishing production receipts. Run the repository's `make quality` gate too.

Upload a new immutable archive, verify its bytes, and compare the complete job
settings with the saved preimage before each mutation. Pause scheduled starts,
wait for the existing writer to finish, and run qualification with the same
identity and concurrency-one job. On qualification success restore the full
original settings with only the application archive changed, then observe a real
reconciliation run. Use full Jobs reset rather than a partial task-array update.
Do not alter identities, grants, retention or the reporting backend.

On failure, wait for the candidate run to terminate and restore the exact prior
job settings/archive. The old processor can replay terminal receipts; shards
and page state must never substitute for receipt integrity. Keep both archives
and immutable spool data. An invalid/expired pagination token fails the job;
an operator may rebuild only the discovery page cursor after diagnosis. That
restarts metadata enumeration and cannot reset delivery receipts or erase data.

## 2026-10-29 runtime qualification

Candidate archive SHA-256:
`b693e2d1ab281887679354949a466063600ee1618b25e3c965983364096c02b8`.
Job `399884290036816`, qualification run `366491032352083`, succeeded under the
existing runtime identity. Eleven synthetic batches crossed seven small API
pages; unacknowledged retry and completion-index restart equality passed.
The read-only production scan found 512 candidates in 24.259 seconds, reading
3,957 directory entries across nine pages. It published no production receipt.
These are discovery measurements, not sustained materialization capacity.

First production run `1093251653315678` succeeded: 508 batches and 8,398 items
reached canonical completion. Discovery took 33.525 seconds; processor elapsed
323.269 seconds and complete job elapsed 338.5 seconds. Both contiguous receipts
advanced from 13,040,538 to 13,044,748. The completion cache grew from 94,768 to
95,276 batches. The remaining selected candidates stay in the retry list.
Canonical reconstruction/MERGEs took 164.222 seconds across two cohorts,
visibility 43.916 seconds and receipt publication 43.746 seconds. This fixes
metadata starvation but does not establish that the job keeps up with the
offered stream: the observed run exceeded its five-minute schedule interval.
The separately qualified UMF/TableSpec migration and sustained-capacity work
remain outstanding.

The full local `make quality` gate passed with 961 tests (six permitted skips),
14 browser tests, lint/types/architecture checks, vulnerability/secret audits,
and fresh wheel/source installs. The installed local runtime still has no
third-party dependencies. The exact staged processor also passed its discovery
and existing staging tests independently of other uncommitted checkout work.
