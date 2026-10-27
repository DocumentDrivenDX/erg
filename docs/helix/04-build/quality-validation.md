# Local-first quality validation — 2026-10-27

Scope: ADR-003, concerns.md and quality-plan.md. Work was isolated on
`codex/local-quality` from base `bb41d2c`; the mechanical Python format/lint commit
is `99f1f04`. Existing uncommitted reporting work in the original checkout was not
included. No cloud activation, real corpus capture, publication or OTel work ran.

## Observed gate results

`make quality` completed with exit status 0 using the locked Python 3.12/Node
development setup. The same entrypoint is wired into CI. Observed checks:

- Ruff lint and format clean across handwritten Python.
- Pyright checks Darwin and Linux at the Python 3.8 language target. New quality
  scripts have no diagnostics; the policy helper is strict. Legacy error debt is
  explicitly recorded: **283 diagnostic allowances** and **95 cyclic edges**.
  No new allowance or cycle is permitted; resolved type allowances must be retired.
- Architecture/stdlib imports, delivery boundaries and measured shape ceilings
  pass. Seven developer tests include actual Ruff/Pyright/Gitleaks rejection
  controls, skip/floor/debt controls and a seeded 120-example protocol property.
- macOS Python 3.12: 700 tests, six optional central skips, no unexpected skips.
  Measured line coverage 78.16%; branch coverage 71.54%.
- macOS Python 3.8: the stdlib checked runner passed 700 tests with the same six
  permitted central skips. A fresh wheel completed the installed-consumer smoke.
- Linux Python 3.12 in a disposable CI image with hash-locked development packages:
  700 tests passed, eight permitted native/central skips. Measured line coverage
  77.58%; branch coverage 71.19%. These observations set global adoption floors
  of **77% lines / 71% branches**, rounded down from the lower observed platform.
- Ten real Chromium checks passed across light/dark themes, using disposable
  populated/empty local reporters. They exercise keyboard/current-view semantics,
  filtering/refresh, error states, default history privacy, cross-origin mutation
  rejection and axe-supported WCAG 2.2 AA rules. Screenshots, video and failure
  traces are synthetic evidence; no screen-reader or pixel-baseline claim is made.
- Hash-enforced Python build/dev audit and npm audit: no known vulnerabilities.
  Gitleaks history and public-source scans: no leaks after the exact documented
  non-credential SQLite-key exception. The generated credential negative control
  was rejected and redacted.
- Locked-backend source/wheel builds, archive completeness/privacy checks and
  fresh dependency-free source/wheel installs passed. The installed probe checks
  all module imports, assets/rates, duplicate-safe synthetic collection, body
  retention off by default, and real loopback health/UI/report responses.
- CI workflow passed local Actionlint validation; shell hook syntax and the
  pre-commit subset are checked separately. Hosted GitHub jobs await a push.

## Provenance and reproducibility

The final test report's code/test/config digest (Markdown acceptance notes excluded
for non-circular recording) is `9d758f7ff02fc33c65f2cdf3a2e4fe9086c7faacfd832cc07628b081c1e39457`, against parent `99f1f04`. The checked runner
records actual Python/platform, command, executed count, skip reasons and exit
status in `reports/tests.json` and JUnit. Quality, browser, coverage and audit
reports are ignored local artifacts; CI retains synthetic reports for 30 days.
Preserve needed release observations before those artifacts expire.

Run `make setup`, then `make quality`; optionally install the provided hook with
`make hooks`. See CONTRIBUTING.md for each subgate. The compatibility matrix uses
only Python plus Node and separately installs wheel/source consumers without dev
packages. `quality/baseline.json` records the exact adoption budgets, not a claim
of strict-clean legacy code. Full strict typing, legacy-cycle removal, smaller
shape ceilings and manual screen-reader/canonical pixel verification remain named
follow-up work under ADR-003. OTel remains explicitly deferred.

## Final scoped verification

The runtime Python sources remain identical to `99f1f04`; the complete 700-test
proof above remains applicable. Final accessible-label and nested-test classification
refinements passed `make lint`, `make audit`, `make browser` and `make package`.
These runs completed successfully: seven developer checks, ten browser checks,
clean audits, and fresh wheel/source consumers. No additional runtime behavior was
introduced. Final public code/test/config digest: `f131e6986bacae6590e9a55968525b5bd3614cc2b075be140135cf03284ca51e`.
Final dashboard HTML SHA-256: `92ec3f531af2e3fd99cc637e9076d393c77ef4067a05f65bf74a8d8e9efc274e`.

## Integrated main validation — 2026-10-27

Main preserves the reporting preparation, scheduling, invalidation and delivery
work in `1a83bc7`, then merges `codex/local-quality` at `0148f7f` without rewriting
history. Merge resolution keeps both sets of behavior. Reporting trigger
instrumentation is passed into dependency initialization, avoiding a new cyclic
import. Correct callable-duty and heterogeneous worker-status annotations remove
two legacy diagnostic allowances; coverage, shape and cycle budgets are unchanged.

The complete `make quality` run passed on macOS Python 3.12.15: 723 runtime tests
with six permitted cloud-dependency skips, seven developer gate controls, ten
Chromium light/dark accessibility and behavior checks, dependency audits and
redacted source/history secret scans. Coverage measured 78.9861% lines and
72.0212% branches against the unchanged 77%/71% floors. The type gate retains 281
explicit legacy diagnostics. The pre-commit subset also passed with
`QUALITY_BASE_REF=codex/local-quality`, proving the integration does not weaken
its inherited allowances. Actionlint and shell-hook syntax checks passed.

Independent source snapshots passed all 723 tests on macOS Python 3.8.20 (six
permitted cloud-dependency skips) and Linux Python 3.12 (three permitted skips:
FSEvents, Mach counters and Azure Functions SDK). Linux also exercised the five
auth tests because its image supplied their optional dependencies. Both snapshots
match the integrated code/test/config SHA-256
`7d42d50eb091c83869872a3498df6d5515661927d4431345becddb1ee0954635`.
Fresh wheel and source installations passed the installed-runtime smoke checks on
Python 3.12 and Python 3.8, with no runtime dependencies or retained history bodies.
Archives were rebuilt after recording this evidence. Hosted CI and its complete
OS/Python matrix await a push; no remote publication is implied by these local runs.
