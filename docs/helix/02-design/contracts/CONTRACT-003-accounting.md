---
ddx:
  id: erg.contract.003
  authoring:
    home: repo
  links: [prd, erg.architecture, erg.contract.001, erg.contract.002]
---
# Session usage, identity and subscription accounting

**Version:** 3. **Boundary:** local capture and the explicitly approved private
Databricks reporting metadata export. Recording evidence does not purchase or
change a subscription, consume reset credits, or create an invoice.

## Purpose

Measure observed session token lengths and dollar values while keeping provider
accounts, subscriptions, quota windows and actual charges distinct. Account and
plan discovery MUST be dynamic; it MUST NOT require a fixed up-front list of plans.
Missing evidence MUST remain unknown. Creator identity MUST NOT be asserted to
be billing ownership. No price may be inferred merely from a subscription name.

## Normative surface

All timestamps are Unix seconds. Accounting intervals are half-open `[start,end)`.
This differs from the inclusive bounds of the work-link CLI.

| Record | Fields beyond uid/ts | Rules |
|---|---|---|
| identity | tool, session, installation, provider, account_key, account_id_hash, email_hash, email_hint, organization_hash, plan, subscription_key, provenance, scope | Provider-scoped hashes join exact IDs. Emails are normalized and hashed, with a masked display hint. Email alone MUST NOT merge distinct account IDs. |
| session_route | tool, session, installation, provider, endpoint_host, endpoint_path, transport, workspace_key, region, profile, account_key, account_provider, billing_mode, scope, request_uid, provenance | Endpoint credentials/query/fragment are discarded; paths are limited to protocol prefixes. Account/workspace identifiers are hashed. Evidence scope is explicit. |
| quota | tool, session, installation, provider, account_key, subscription_key, plan, limit_id, window, used_percent, window_minutes, resets_at, reset_credits_available, event, provenance, occurred_at | Missing fields are NULL. `used_percent` is used, not remaining. Scheduled resets are not proof of a reset having occurred. |
| session_cost | tool, session, usd, complete, provenance | Native agent estimates MUST NOT be relabeled billed spend. The maximum observed native session estimate is retained for reporting; partial-source limitations remain. |
| subscription_charge | provider, account_key, subscription_key, period_start, period_end, amount, currency, usd, source_charge_key, provenance | Provider/account-scoped charge identity deduplicates replay/mirrors; aliased identifiers reconcile by canonical account and hashed source charge key. Corrections replace the same charge snapshot. Non-USD amounts remain in original currency; USD is unknown without conversion evidence. |
| account_binding | tool, session, installation, start_ts, end_ts, provider, account_key, subscription_key, plan, provenance | Explicit, bounded metadata can resolve historical associations. Conflicting bindings remain unknown. |

`quota.event` distinguishes `snapshot`, `window_exhausted`, `limit_reached`,
`reset_observed` and `reset_credit_snapshot`. A 100% observation does not prove a
request was rejected. `limit_reached` requires an explicit provider flag/status or
recognized structured error. A reset transition is observed when the scheduled
boundary advances and used percentage drops; its cause is unknown. Available
reset credits do not establish consumption, and the monitor MUST NOT consume them.

## Incremental quota reporting

The private local ledger maintains `quota_summary_*` as derived metadata. Every
quota insertion, update, replacement and deletion MUST queue the affected native
UID transactionally. Bounded background batches select the latest source revision
per UID across machines, preserve timestamp-tie and first-observation ordering,
and update counters and latest-window pointers. Reset-history counters exclude
records without an observed occurrence time, matching the raw ledger semantics.
Provider account aliases are resolved at read time; changing identity evidence
MUST NOT require rebuilding or guessing account ownership.

A seeded, fully drained summary may replace full-history enumeration. Missing,
unseeded or pending summaries MUST use the raw history reference; partial counters
MUST NOT be presented as complete. The reporting worker drains bootstrap in short
transactions before publication, preserving the previous report during catch-up.
Status exposes the quota backlog. Stable machine/UID keys join source rows;
SQLite schema-version changes, including compaction, require requalification of
source-order ties before the summary is used again. Idle maintenance MUST perform
no writes. This
projection neither drops source events nor changes captured usage, plan charges,
credential handling, enrollment or the forward-only delivery boundary.

## Capture and precedence

Native log account metadata can identify a session at the observed timestamp.
Codex's local thread database supplies creator account IDs, labeled
`codex_creator_account`; those establish ownership, not subscription billing.
A custom model provider's creator MUST NOT be treated as its billing provider.
Its explicit Codex creator ID MAY identify the OpenAI app owner, independently of
the custom inference provider.

Claude `bridge-session.ownerAccountUuid` and `ownerOrganizationUuid` establish
session ownership (`claude_session_owner`, `scope=session_owner`), including
untimestamped records. Their timestamp is observation time; their ownership scope
is the explicitly named session, not a historical entitlement or billing claim.
Artifact reaction accounts MUST NOT establish session ownership. Conflicting
owner IDs MUST remain unknown. Bounded/authenticated billing bindings remain
separate and take precedence; creator evidence never allocates fees.
Current profile snapshots MUST NOT be backdated to historical sessions.

The collector MAY poll Codex's read-only `account/read` and
`account/rateLimits/read` endpoints using the installed local app server. It MUST
NOT invoke login, logout, inference or reset-consumption methods. It MUST NOT
read credential files in the native-log or profile-sharing adapters. The operator
has authorized a separate read-only billing adapter to use the existing Codex
ChatGPT login in memory. That adapter MAY read the selected home’s file-backed
login solely to authenticate GET requests to the allowlisted OpenAI billing
endpoints. It MUST NOT refresh the login, follow redirects, consume resets,
purchase anything, or send credentials to configurable/custom endpoints.
Provider account IDs and emails from these responses are hashed before storage; no token or raw response is persisted. Polls are bounded
and independent of fast log capture. Profiles are discovered from default and
isolated Codex homes rather than a configured plan list.

The billing adapter allowlist is `https://chatgpt.com/backend-api` with paths
`/wham/usage`, `/accounts/check/v4-2023-04-27`,
`/checkout_pricing_config/configs/USD`, `/subscriptions?account_id=...`,
`/wham/rate-limit-reset-credits`, and
`/wham/rate-limit-reset-credits/history` with an optional cursor. Account identity
MUST match the native login and, when supplied, the native RPC account. Responses
are bounded to 2 MiB, history to four pages, each request to four seconds and a
poll to eighteen seconds. A credential-version cache limits ordinary reads to
one poll per fifteen minutes. Unsupported credential stores remain unavailable.
Only allowlisted metadata is retained; credentials and raw responses MUST NOT
enter the ledger, logs, exports or diagnostic files.

`identity.billing_status` contains endpoint names and fixed status labels, never
raw HTTP bodies or exception messages. A denied pricing or invoice read MUST be
visible, MUST NOT erase an earlier price observation, and MUST NOT produce a
zero charge. A provider list price is an estimate, not evidence of payment.
Pricing amounts follow the UI's major currency units; minor-unit exponent controls
formatting, not amount division. Unsupported currencies require conversion evidence.

Provider reset history uses `quota.window=reset_history` and events
`reset_credit_granted`, `reset_credit_used`, `reset_credit_expired`,
`reset_credit_revoked` or `reset_credit_purchased`. `occurred_at` is the provider's
Unix event time; `ts` is first observation time. Provider/account/event identity
MUST deduplicate replay and mirrored collection. A grant or use MUST NOT imply a
paid purchase or a dollar amount. Prior reset metadata may be discovered now;
that does not authorize uploading retained native session history.

Session-time route evidence with an explicit paying principal and provider MAY
establish billing attribution for that request or subsequent session interval.
Request evidence MUST NOT leak to other requests. Configuration-only evidence
MUST NOT establish a historical payer. Conflicting routes remain unresolved,
and a changed endpoint MUST NOT inherit the former endpoint's payer. Explicit
operator bindings retain precedence.

Claude profile metadata can populate the account registry. A SessionStart hook
MAY read `claude auth status --json` in the actual caller's context and attach its
reported identity to that session. Identity capture runs synchronously before
requests, with a five-second client-read bound and eight-second hook deadline,
so short-lived sessions cannot outrun an asynchronous identity lookup. Gateway/custom-base-url contexts preserve explicit endpoint/profile evidence
from the actual lifecycle environment but MUST NOT borrow a first-party OAuth
identity. Unidentifiable inference principals remain unknown. The hook MUST NOT emit prompt context or retain stdin
message bodies. Existing user hooks are preserved and settings backed up.

Exact native IDs can join a current account snapshot to a creator record. A shared
email does not disambiguate multiple account IDs. Explicit bounded bindings take
precedence for billing association. Creator-only and current-profile evidence MUST
NOT allocate subscription charges to sessions automatically.

## Session lengths and valuations

`total_tokens` is cumulative observed fresh input + cache read + cache write +
output, across canonical requests of one native session. Repeated context is
processed again and therefore included. Codex cache-read and cache-write input
counts are subsets of reported input; fresh input subtracts both before the
three disjoint input buckets are summed. Reasoning is an output subset, not added
again. Child sessions remain separate. This is not unique conversation size.
`largest_request_input_tokens` and `largest_request_total_tokens` expose the
largest observed prompt and prompt-plus-output respectively. Unknown capture or
an empty call set is NULL, not zero. Summary calculations cover all canonical
calls, independently of the 1,000-row call-detail display cap.

Rack-rate comparison value uses a dated, versioned rate catalog. First-party
rates and explicitly sourced market-hosting comparisons retain their provenance.
The private `erg.telemetry.rate_catalog` and `rate_catalog_versions` tables publish
immutable model rows and a complete hash-verified manifest. An atomic local JSON
snapshot supplies identical offline rate data to both report backends.
It is an estimated reference valuation at the cited rate-card date, not an invoice
or an assertion about historical price in force. Historical billing remains unknown
without billed evidence. Unsupported models or unresolved aliases MUST produce an actionable rate-mapping
gap, never a zero price. The approved reviewer policy uses the minimum and maximum
rack-rate valuations of observed parent-session models. If an individual parent
is unavailable, the observed parent-model pool supplies an explicitly labeled
comparison range; this MUST NOT claim to identify the underlying reviewer model.
Long-context tiers apply per request. Unknown Claude cache-write duration yields
5-minute-to-1-hour valuation bounds; observed duration buckets can make it exact.
Native agent-reported estimates, reference valuations, metered invoices and
subscription charges MUST remain separate. They MUST NOT be summed as one cost.

A subscription period groups charges by canonical account, subscription identity
and exact period bounds. A call can contribute to one period only when its billing
association is supported and the interval is unique. Overlapping subscriptions or
periods remain unassigned. Charge totals MUST NOT multiply when joined to calls.
Known token value divided by positive subscription spend is an observed lower
bound; incomplete/unpriced usage MUST be counted and disclosed. No denominator,
zero spend, an unresolved currency conversion or conflicting charge snapshots
makes the ratio unknown. Subscription expense allocation uses each request's rack-value midpoint divided
by the whole published observed billing-period rack-value midpoint. UI filters
MUST NOT change that denominator. Project shares apply to both numerator and
expense. Partial account/machine coverage and current periods are provisional.
An unresolved price anywhere in a period prevents presenting that period's
allocation as complete.

## Metadata layer and error semantics

`erg accounts status` returns sanitized registry, quota and period observations.
`erg accounts import --file FILE` accepts a version-1 JSON object with optional
`identities`, `bindings` and `charges` arrays. Provider plus account ID or email
identifies the account. Binding rows require tool, session or installation, and
explicit `start_ts`/`end_ts`. Charge rows require charge ID, subscription ID, amount
(`usd` or `amount`), currency (default USD) and period bounds. Unknown plans remain
valid labels; no closed plan enumeration exists.

Validation is atomic. Invalid or unbounded metadata fails with no partial import.
Repeated charge IDs within one provider account update the same logical charge; the same invoice number in different accounts stays separate. Equal-time conflicting mirror
amounts stay unknown. Failed probes leave existing history intact and do not start
login. Missing source formats remain coverage gaps.

```json
{"version":1,"charges":[{"charge_id":"invoice-id","provider":"openai",
"account_id":"provider-account-id","subscription_id":"subscription-id",
"period_start":1790812800,"period_end":1793491200,"amount":200,"currency":"USD"}]}
```

This example supplies a charge, not a historical session binding. It does not
assert that any account or subscription exists on the operator's machine.


## Route observations, backfill and reporting

`session_route.scope=session_snapshot` records a native/lifecycle snapshot from
its recorded timestamp onward. `request_provider` records only the provider for
an exact native `request_uid`. It MUST NOT imply an endpoint or payer. Conflicting
same-time snapshots remain unknown. A later snapshot MUST NOT backdate coverage
of earlier calls. A provider mismatch between a request and lifecycle snapshot
MUST NOT inherit the lifecycle endpoint or account.

Omnigent's saved `inference_snapshot.runtime_config` MAY establish the named
harness's session configuration at its creation boundary. Only selected provider
metadata is retained. A saved Databricks profile pointer MUST NOT be resolved to
the profile's current host/principal for historical attribution. Missing snapshots
or conflicting harness/provider-family selections remain unknown.

`scope=configuration` is an observation of an active Omnigent wrapper's current
configuration. It MUST remain visible as configuration evidence and MUST NOT count
as historical per-call endpoint/account coverage. Provider endpoints are discovered
from native provider IDs and matching configuration entries; auth commands are
never executed or retained. Profile inventory comes through the CLI, not credential
files. Profile labels MUST NOT be interpreted as regions. Explicit optional
`discovery.json.route_metadata` entries may supply region metadata for matching
endpoints, with configuration scope, for example:

```json
{"route_metadata":[{"endpoint":"https://gateway.example/v1","region":"region-from-your-routing-inventory"}]}
```

Pi lifecycle snapshots retain allowlisted `ctx.model` route metadata before agent
requests and at existing durability boundaries. Newly captured route metadata MUST
NOT be attached to old messages. Snapshot/native request identities still deduplicate
usage. Claude lifecycle hooks preserve explicit route environment metadata and
first-party authenticated account metadata separately. No raw prompts, message
bodies, credentials, headers, auth commands or email addresses enter route evidence.

`erg accounts backfill` replays only supported owner/route metadata; it MUST NOT
reset native offsets or reingest usage. A versioned startup pass performs the same
backfill once. Replay preserves existing immutable evidence. Unsupported/pruned
history remains unknown. Source failures remain visible and retryable.

Session reports expose `ownership_accounts`, `billing_account`, `route_evidence`,
`route_call_coverage` and `unknown_reasons`. Endpoint coverage counts observed
canonical calls at/after matching native/lifecycle evidence; configuration-only
observations never contribute. Reasons distinguish missing owner/route/region/
inference account/subscription, conflicting owners/routes, partial endpoint
coverage, configuration-only evidence, owner-only billing evidence, and missing
call history. Provider/account/region labels are discovered data, never a fixed
machine-specific bucket list. Plan profile observations MUST NOT overwrite session
ownership evidence or be backdated as historical plan assignments.


## Proposed project accounting extension

Status: partially implemented for FR-41–45. Local catalog/bindings/aliases,
configuration history, retained-context indexing, resolution and Costs reports are
implemented. Allocation export snapshots, master sync, additional selector types
and fixed-charge allocation remain proposed. Existing accounting records and
version 2 semantics above remain in force. Initial storage is the local SQLite
ledger. The future master catalog arrives from Databricks; assignments flow back
to Databricks. No central connection or destination is selected by this proposal.

### Catalog identity and configuration

| Proposed table | Key and fields | Semantics |
|---|---|---|
| `project_catalog` | `project_key` PK; `name`, `state`, `source`, `source_revision`, `catalog_revision`, `updated_at` | Stable opaque identity, independent of display name. Initial local creation uses a namespaced UUID. State is active or archived; referenced projects are not hard-deleted. |
| `project_aliases` | `(authority, external_id)` PK; `project_key`, `revision`, `provenance` | Explicit upstream identity mapping. Names never establish an alias. Conflicts/cycles reject the catalog update atomically. Existing local keys remain resolvable when upstream adopts or maps them. |
| `project_bindings` | `(binding_id, revision)`; `writer`, `priority`, `selectors`, `allocations`, `valid_from`, `valid_to`, `enabled`, `provenance`, `supersedes` | Versioned configuration, not captured source facts. UTC half-open effective interval; no implicit historical reassignment. Revisions/retractions retain audit history. |
| `project_allocations` | `(machine, canonical_usage_uid)` current snapshot; `revision`, `catalog_revision`, `policy_revision`, `context_revision`, `state`, `allocations`, `binding_refs`, `reason` | Derived allocation after usage reconciliation. Updates replace the same snapshot, with revision history/tombstones retained for sync. No model usage is created here. |

The local table is writable through validated configuration commands in the first
iteration. Later, an imported upstream snapshot governs master-owned fields;
local-only drafts and explicit aliases remain separate from those fields. Import
validates unique IDs/references and commits a complete revision atomically. An
outage retains the last valid catalog plus its age/version; it must not erase
projects or bindings. Upstream deactivation preserves historical attribution.
Concurrent conflicting edits remain conflicts, not clock-based last-writer wins.

Selectors are typed, declarative and conjunctive: exact native session identity
(machine/tool/session), canonical repository identity, or a machine-scoped directory
root in the first slice. Additional registered types may match verified task,
commit, PR, account, subscription or route identities. They do not establish those
identities themselves. Unknown selector types reject configuration; no executable
expressions, SQL, or free-text title matching. Directory containment respects path
components: `/work/app` must not match `/work/app2`. A worktree matches its verified
repository when available; basename similarity is not sufficient. Context missing
for a request cannot be inferred from the session's latest directory or route.

Example proposal for the configuration payload (illustrative IDs and paths only):

```json
{
  "version": 1,
  "bindings": [
    {
      "binding_id": "repo-default",
      "priority": 10,
      "selectors": {"repository": "github.com/example/product"},
      "allocations": [{"project_key": "local:installation:project-a", "units": 1000000}],
      "valid_from": 1792540800,
      "valid_to": null
    },
    {
      "binding_id": "session-override",
      "priority": 100,
      "selectors": {"session": {"machine": "machine-id", "tool": "pi", "id": "native-session-id"}},
      "allocations": [{"project_key": "local:installation:project-b", "units": 1000000}],
      "valid_from": 1792540800,
      "valid_to": null
    }
  ]
}
```

These are business-accounting declarations, not claims that work belongs to a
verified PR, that an account owns a subscription, or that a human performed it.
Repository/project membership is many-to-many; one repository can serve multiple
projects and one project can span repositories, machines and tools.

### Resolution and conservation

Resolve against the call's recorded context at event time after canonical usage
reconciliation. A manual session/time binding is explicit policy and may override
broader rules through its configured priority. At the highest matching priority,
identical allocation sets coalesce and retain all rule references; different sets
produce `ambiguous`, not first-match/last-match wins. A more specific-looking path
or a newer title has no hidden precedence. Preview shows matched rules and changed
assignments before applying a revision. Missing targets reject configuration;
missing evidence or no matching rule leaves usage unassigned with a reason.

Allocation weights are integer millionths, 0 through 1,000,000. Their sum cannot
exceed 1,000,000. A remainder is explicitly unassigned; do not renormalize it away.
A normal single-project binding assigns 1,000,000 units. Explicit splits are policy,
not measured causality, and carry that label. Weighted tokens/value retain precision
until display; per-project distinct request counts are non-additive, while allocated
request equivalents sum to canonical requests. Repeating import, replay or sync
must preserve totals. Unresolved native/server overlap remains a separate coverage
problem; grouping must not convert overlapping source observations into a claimed
physical-spend total.

A session can move between projects or span several. Publish session/project links
as summaries with assigned intervals, usage coverage and allocation revision.
Call-level allocations are the accounting facts. A session summary must never
charge every linked project for its full-session tokens or cost. Raw observations,
work evidence and observed project hints remain unchanged by reclassification.

### Monetary reporting

Every project report carries separate columns for known reference token value
(with bounds and unpriced usage), native reported estimates, actual billed usage,
and policy-allocated fixed charges. They are alternative bases/components, not
four values to add together. Native session estimates cannot be added to their
per-call estimates. Exact billing records override neither captured usage nor
reference valuations; they are linked evidence with their own coverage.

Subscription charges stay at account/subscription/billing-period scope by default.
Optional project allocation is a separate versioned policy over a known charge;
usage-weighted, fixed-share or explicit assignments must say which policy was used.
No default allocation is implied by a plan name, shared email or project binding.
Unassigned expense and unassigned usage are separate. Currencies stay distinct
without explicit conversion evidence. A zero is shown only when established.

Project views use the same session/project/time/account/provider filters as their
usage denominators. They show assignment and price coverage independently, with
an unassigned/conflicting bucket and a drilldown to the binding, native context,
cost provenance and session. The full-session reader exposes readable cost totals
rather than requiring inspection of raw JSON. Accounts retain subscription/reset
and entitlement views, linked from Costs rather than serving as the sole cost view.

Current implementation gap: `rates.value` supplies latest cited standard API
reference valuation, not historical effective-date billing. Do not relabel it as
price-in-force accounting. Historical tariff selection requires effective intervals
and source evidence before FR-15 can be claimed complete.

### Eventual Databricks exchange

Import versioned master-project snapshots/deltas with deletion/archive evidence
and explicit identity aliases. Keep a durable inbound revision cursor. Export
project allocation snapshots keyed by machine/canonical usage identity, with
catalog/policy/context revisions, allocation weights, rule references and retraction
state. Stable session links are derived from those facts and may also be published
for shared navigation. Export retries update the same facts; later binding edits
must replace/retract old allocations rather than append duplicate spend.

Local directory selectors, names, emails and transcript bodies are not required
for shared accounting and are excluded from the default assignment payload.
Export stable project/resource references and provenance only. Share path-based
policy itself only through an explicit governed configuration channel. Local
revisions may be uploaded for central recomputation, but central dashboards must
select one declared policy/revision per accounting view rather than sum divergent
operators' assignments. Private catalog permissions remain a prerequisite.

Acceptance examples: a rename preserves assignments; a 60/40 split conserves one
call and its value; equal-priority disagreement leaves it unassigned; a noon binding
change does not move morning calls; replay changes no totals; an upstream alias
preserves the original assignment lineage; a catalog outage uses the last known
revision; unknown subscription charges remain unknown; cost coverage and project
coverage can differ independently. These are required tests for implementation,
not claims that the proposed feature is already shipped.


### Current local API and CLI

`GET /api/costs` accepts optional `start`, exclusive `end` (UTC epoch), `machine`,
`tool`, `session` and `project_key`. `@unassigned` is a reserved filter/bucket key,
not a catalog project. Duplicate/unknown filters and invalid intervals fail with
400. SQLite reports return config revision, catalog, weighted totals, project rows,
up to 100 session summaries ordered by allocated tokens, pricing dates/sources and
unassigned/source-overlap reasons. Billed and subscription allocation amounts are
NULL. Results use a consistent read snapshot and a 30-second cache invalidated by
configuration revision. Other reporting backends explicitly return unavailable.

`erg projects create/list/export/import/preview/index-context` manages the first
surface. Export carries a full version-1 JSON configuration and expected revision.
Import without `--apply` previews only; preview compares before/after against one
read snapshot. Apply serializes under a short SQLite transaction, rejects stale
conflicting changes, and retains previous full snapshots. Identical retries are
no-ops even if their expected revision is old. Missing existing project identities
are rejected; archive instead. Alias targets are direct catalog keys, so alias
cycles cannot enter this surface. Deleting a binding from a replacement config
removes it from the active projection; previous config snapshots preserve the
retraction history, but no central assignment tombstone is emitted yet.

Directory context is private local metadata. Recorded directory/worktree changes
invalidate stale repository/path combinations; same-time conflicting contexts
cannot establish a match. Captured traces and native project hints are unaffected
by configuration. Current pricing remains cited reference valuation. Accounts and
subscriptions retain their independent ledger and allocation restrictions.


## Scope and boundaries

The operator authorized account-specific plan-price observations, recorded actual
subscription/reset/overage charges, and their existing private reporting export.
A displayed recurring plan price is useful cost evidence, distinct from a paid
invoice. No historical native session bodies or new credential access are needed.

## Precedence and compatibility

`identity` adds optional `monthly_usd`, `price_currency`, `billing_interval` and
`price_provenance`. A price observation MUST have a known account and an explicit
observation timestamp. USD monthly prices accrue an expense estimate from that
observation onward using the actual UTC calendar-month length. Unknown billing
anniversary dates MUST NOT be invented. Repeated identical price observations
retain the first continuous observation time. Invoice-backed periods take
precedence over plan-price estimates; they MUST NOT be added together.

`subscription_charge` adds optional `charge_kind` (`subscription`,
`purchased_reset`, `overage`) and `reset_at`. Old rows mean subscription expense.
Each receipt or line remains idempotent under its existing provider/account key.
Purchased reset and overage expenses add once to the specified billing period;
`reset_at` is evidence, not another charge. Ordinary allowance resets and decreases
in available credits MUST NOT synthesize a purchased reset or its price.

## Error semantics

`POST /api/billing` is loopback/same-origin only and accepts version-1 accounting
metadata. It rejects unsupported charge kinds, negative/nonfinite amounts,
unobserved hashed account/provider combinations, invalid periods and reset times
outside their period. USD amounts and explicit billing intervals are required for
actual USD expense allocation. Non-USD evidence retains its existing unknown-FX
behavior. `GET /api/money` takes the exact shared report selection and exposes
rack-rate bounds, effective expense, known subtotals and counted resolution reasons.
Work, project and account responses carry the same `monetary` shape and per-row
allocations. Missing billing evidence MUST NOT be silently assigned zero expense.

Catalog pulls reject manifest/hash mismatches and missing model rows, preserving
the last complete offline snapshot. A configured cloud reader checks for new
rates no more than once per fifteen minutes per worker, including failed attempts.
Automatic refresh MUST match the enabled workspace origin, native profile and
catalog/schema. Namespace equality alone MUST NOT authorize a pull from another
workspace or profile. Legacy namespace-only opt-ins require explicit re-enablement
against the configured reporting connection. A mismatch MUST NOT query SQL or
modify the existing opt-in. Marker updates are atomic and private. Publisher
DDL is application-owned inside an explicitly selected private schema; it grants
no privileges and changes no jobs or enrollment boundaries.

## Examples

A $30 subscription plus one $10 reset gives $40 recorded period expense. A
session contributing one quarter of that period's observed rack value receives
$10 allocated expense even when viewed alone. A human project receiving 60% of
all calls' shares receives $24. The period's unknown or unobserved usage remains
a coverage limitation, rather than evidence of an exact provider invoice split.

A user-supplied account UI showing $500/month is stored as an observed monthly
price, not a $500 paid invoice. Subsequent usage can show accrued plan expense
estimates while the actual invoice date, taxes, discounts and purchased-reset
receipts remain separate evidence requirements.
