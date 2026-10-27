# Local-first quality gate and acceptance mapping

Authority: ADR-003 and concerns.md. The operator's 2026-10-27 instruction authorizes
these local quality changes while preserving the dependency-free runtime, existing
reporting work and the standalone collector. OTel work is explicitly deferred.

## Acceptance and existing proof

| Requirement / risk | Primary proof | Gate |
| --- | --- | --- |
| FR-1/FR-2 normalized, duplicate-safe observations | test_unified.py, test_call_projection.py and generated protocol round trips | test; rules |
| FR-4 default body exclusion / explicit retention | test_history.py, test_forward_boundary.py, test_cron_capture.py; empty browser history | test; browser |
| FR-14/15/16 unknown costs, dated offline rates | test_accounting.py, test_packaged_rates.py, test_monetary.py | test; package |
| FR-17/28/29 identity, charges and quota uncertainty | test_account_reports.py, test_codex_billing.py, test_quota_projection.py | test |
| Local report filters, refresh and unavailable states | existing report/cache/concurrency tests; e2e/local.spec.js against actual HTTP server | test; browser |
| Protected project mutation and trust boundary | test_project_admin.py and browser cross-origin rejection | test; browser |
| Capture independence, restart/replay and source errors | test_service.py, test_reporter_service.py, test_capture_efficiency.py | test |
| Schema, publication, invalidation and read-only reports | test_project_rollups.py, test_report_sqlite.py, test_report_concurrency.py | test |
| Installed distribution, assets, stdlib compatibility | check-release-artifacts.py; smoke-installed.py in fresh environments | package; runtime matrix |
| New quality debt and silent test skips | quality/tests/test_quality.py negative controls; baseline ratchets; run-tests.py | lint; types; rules; test |
| Keyboard, labels, contrast and active view | real-browser axe and keyboard checks, both themes | browser |
| Sensitive source/dependency exposure | pinned scanner and locked Python/npm audits | audit |

Existing meaningful tests are reused; no duplicate per-criterion test campaign is
required. New/changed criteria must update the owning row and observed proof.
The CI test report records base revision, public code/test/config digest (excluding Markdown acceptance notes), command, Python,
platform, exit status, executed count and exact skips. Browser JUnit, screenshots,
video and failure traces are synthetic and retained with that run. Do not put real
session bodies, credentials or machine observations in these artifacts.

## Reproduction

Use `make setup` on Python 3.12/Node 22, then `make quality`. `make hooks` opts the
checkout into the supplied pre-commit subset. The complete quality command is the
same locally and in CI; individual targets support iteration. Browser fixtures
start disposable loopback reporters on ports 8877/8878 and refuse existing servers.
The runtime matrix invokes the stdlib test runner and fresh installs without dev
packages. Missing Node is an error. Native/central skips require the named test,
exact expected reason and matching platform/missing optional dependency.

## Ratchets and remaining adoption work

Measure shape/type debt with `python scripts/quality.py baseline` in the locked dev
environment. This is an adoption/ratcheting helper, not permission to increase debt:
CI compares committed allowances against the PR/push base. Coverage floors derive
from observed macOS and Linux branch/line measurements, rounded down to whole
percentages; they may only increase. Keep deterministic work/query-count regression
tests as the normal performance gate; wider workload/10x capacity claims still
require their own measured qualification. Do not introduce flaky wall-time gates
or claim untested fleet/cloud capacity.

When editing a typed-debt owner, remove relevant allowances after fixing diagnostics.
Design legacy cyclic-edge removal by ownership, and lower global shape maxima as
code is split. Preserve the runnable collector constraint. Manual screen-reader
review and a canonical pixel-baseline platform remain explicit prerequisites for
stronger accessibility/visual conformance claims. They do not silently pass through
an automated aggregate test count.

Observed adoption results: [quality-validation.md](quality-validation.md).
