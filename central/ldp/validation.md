# LDP migration validation

The migration implementation is committed in `7c6ededad89dd1abe14ab65a2ec289b6e1f88c76`.
A concurrent UI commit included the staged migration; its history is preserved.
Validation below used a clean export of that exact committed tree, excluding
unrelated working changes. The qualification date is 2026-10-30 UTC.

| Gate | Result |
|---|---|
| Lint and formatting | Passed |
| Darwin/Linux type ratchet | Passed; existing 281 allowances, no increase |
| Architecture, stdlib-only imports and quality controls | Passed |
| Full Python suite | 782 tests; six explicitly allowed platform/dependency skips |
| Coverage | 78.948% lines; 71.958% branches; above existing floors |
| Locked compiler generated-output suite | Five passed |
| Browser/accessibility checks | 28 passed in light/dark modes |
| Development dependency and secret scans | No known vulnerabilities or detected secrets |
| Wheel and source fresh installs | Passed; runtime dependencies empty |

The first browser run had one localhost `ERR_CONNECTION_RESET` fetching
`unified.js`; the retained trace showed the resulting undefined `ErgUI` error.
The unchanged full browser suite then passed. No assertion, timeout, retry policy,
coverage floor or quality allowance was weakened.

Reproduce local gates with `make quality` and the separately locked compiler
checks with `make ldp-test`. Cloud qualification additionally requires the
immutable dependency/build pins in [runtime-build.md](runtime-build.md), exact
emitted source and the synthetic scenarios exercised by
`scripts/qualify-ldp-output.py` and `central/ldp/tests/native_stage.py`.

[Native evidence](native-evidence.json) records fabricated-data output, replay,
crash/restart, scope rejection, conflict refusal and independent cursor checks.
The capacity fixture contains 16,796 unique items and 65,571,850 wire bytes;
publication reached 16,000 then 16,796 without duplicate canonical rows.
Synthetic table SELECT grants were restored after checks and the replay pipeline
was stopped. Production data permissions and the existing writer are unchanged.

The user approved explicit single-job authority. The owning TableSpec change is
[PR 48](https://github.com/DocumentDrivenDX/tablespec/pull/48), pinned at
`236c32f1fee17cfc949c4eb0479765d4797af165`; default fenced authority is preserved.
The local Erg runtime remains standard-library only.

Production cutover remains pending independent review authorization and the
controlled migration procedure in [README.md](README.md). Review dispatch was
blocked by automatic approval review because it would send local source to the
Codex/Claude harnesses without separate authorization. No alternative dispatch
was attempted. Native synthetic qualification does not establish fleet capacity
or authorize bypassing the stop/drain and pinned legacy-boundary checks.
