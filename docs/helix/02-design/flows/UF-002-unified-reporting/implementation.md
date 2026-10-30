# Unified reporting implementation and verification

Governing scope: FEAT-002, US-001/002/003, UF-002 and DESIGN.md.

## Module boundaries

| Owner | Responsibility | Dependency direction |
|---|---|---|
| `erg/static/index.html` | Existing report requests, scope validation, charts and incremental publication guards | Presentation consumes existing reporter APIs; never starts collection |
| `erg/static/unified.js` | SDK frame composition, navigation/history, scope persistence, capacity and session evidence presentation | Uses report controllers; contains no pricing, capture, payer resolution or allocation algorithm |
| `erg/static/unified.css` | Erg report regions, responsive density and role-token aliases | Consumes versioned SDK tokens; does not redefine provider/account identity |
| `scripts/build-ui.mjs` | Generate AppShell markup and copy presentation assets from the pinned local SDK | Development-only React/SDK use; no JavaScript package installation at operator runtime |
| `erg/server.py` | Exact static asset allowlist with correct content types and no-store responses | Reads packaged files; arbitrary filesystem paths remain unavailable |
| `scripts/serve-quality-fixture.py` | Isolated synthetic project, native trace and quota observations | No credentials, live accounts, external requests or retained history |

`uv run --locked python scripts/quality.py rules` checks existing module/stdlib
boundaries and ratchets. `make quality` is the full release gate. Package smoke
requires all new assets and fetches them from an installed, isolated reporter.

## UX coverage and executable acceptance

| Pattern | Scope / acceptance | Exercising test source |
|---|---|---|
| Navigation | US-001-AC1; project → session, browser Back and reload preserve scope | `e2e/unified.spec.js`: project spending carries scope to a full session and browser Back restores it |
| Shared costs | US-001-AC1/2/3; canonical tokens and missing payer remain visible across reports | `e2e/unified.spec.js`: overview projects and accounts agree on canonical token usage and scope survives switching; existing `tests/test_monetary.py` covers period allocation and ranges |
| Async reports | US-001-AC4; a failed refresh retains completed results, with their original scope | `e2e/unified.spec.js`: a failed refresh retains the last complete project results and offers recovery; `tests/test_universal_dashboard.py` covers publication/scope races |
| Forms | US-001-AC1; invalid date interval remains editable and makes no report change | `e2e/unified.spec.js`: custom whole-hour dates are validated and shareable |
| Account data states | US-002-AC1/2/3/4; remaining capacity, observation/reset time and absent charges are distinct | `e2e/unified.spec.js`: accounts prioritize observed capacity and distinguish quota resets from charges; existing quota/account tests cover source evidence |
| Session evidence | US-003-AC1/2/3/4; title/ID, token/peak metrics, trace and uncertain work evidence | Project/session browser journey plus existing session, mirror and project-allocation parity tests |
| Keyboard / appearance | Primary navigation, disclosures, scrollable reports, 320/720/1024/1366 widths and both appearances | `e2e/local.spec.js` axe/keyboard tests; `e2e/unified.spec.js` responsive navigation |
| Access failure | US-001-AC4; Databricks denial is explicit and does not select Local silently | `e2e/unified.spec.js`: Databricks denial stays explicit and never silently switches backend |
| Destructive actions | Not applicable to this navigation change; no delete, purchase, reset consumption or enrollment action added | Existing administrative mutation guards retained |
| Modal overlays | No new modal workflow; native disclosures and SDK navigation adapt by size | Browser keyboard/axe coverage; existing project preview/cancel controls retained |

Covering browser scenarios carry `@covers` citations. The headless VM fixture
stubs only browser presentation; actual report/filter/publication code runs.
Playwright separately exercises the SDK frame against the real local HTTP server.

## Presentation limits

Exact native session IDs query retained history. Title/folder text search is
explicitly limited to the loaded session page; pagination loads more retained
metadata. Custom reporting dates use whole UTC-hour boundaries because the usage
summary is hourly; partial-minute totals must not be presented as exact. Session
trace and full-session metrics are labeled independently of the originating report
range. Machine and capture diagnostics remain secondary under System.

The SDK uses four visible bottom destinations at compact widths and a side
navigation at expanded widths. This is the SDK's adaptive frame; it preserves all
four primary destinations and the separate System action. Prototype files remain
synthetic and are never imported by the installed dashboard.

## Build and rollback

Regenerate pinned presentation files with:

```sh
ERG_UX_BUILDER_ROOT=/path/to/ux-builder node scripts/build-ui.mjs /path/to/erg
make quality
```

The generator verifies UI 0.14.0 and tokens 1.7.0. Generated assets and license text
ship with the package, so ordinary install/run needs no Node, React, npm or network.
A reporter-only installation creates a new immutable runtime. Preserve the prior
runtime for rollback; collector service, enrollment, uploads and ledger schema are
unchanged. Do not deploy unqualified central processor or infrastructure changes as
part of this UI update.

## Verification evidence

Browser reports: `reports/browser-junit.xml`, `reports/browser/`, screenshots and
retained failure traces. Full gate output: `/private/tmp/erg-unified-quality-final.log`.
These are run evidence, not prerequisites for another machine to understand scope.
