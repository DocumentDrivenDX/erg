---
ddx:
  id: erg.adr.003
  status: accepted
  links: [prd, erg.architecture, erg.implementation-plan]
---
# ADR-003: Local runtime and development quality contract

## Decision and scope

The operator authorized local-first concern adoption and quality gates on 2026-10-27,
then explicitly deferred OTel as irrelevant to the current scope. This decision
covers the collector, local SQLite/reporting services, dashboard and their source
release. Central delivery, fleet enrollment and desktop capture retain separate
acceptance. No infrastructure or live data activation follows from a quality gate.

All installed local Python modules retain Python 3.8+ and zero third-party runtime
dependencies. The collector remains one runnable stdlib file for SSH use. This
resolves the non-collector runtime question in the concern register: it has the
same floor, not a second Python 3.12 runtime. Configuration remains stdlib JSON and
ERG_* variables with validation at the owning entrypoint/boundary. A pydantic
runtime migration would break the consumer constraint and is not selected.

Development runs on pinned Python 3.12 and Node 22. uv.lock and package-lock.json
lock developer dependencies; none are installed as wheel runtime requirements.
Use setuptools with a static candidate version: changing to hatchling/dynamic
versioning provides no required local behavior. The development build uses the
locked setuptools via `build --no-isolation`; source installs also exercise the
consumer's compatible backend on the advertised minimum runtime.

## Quality adoption and exceptions

- Ruff checks and formatting cover handwritten Python, including tests and
  experimental source. E402 exceptions are line-local explanations for explicit
  checkout/bootstrap imports; there are no file-wide lint suppressions.
- Pyright checks all local Python modules at the Python 3.8 target. The typed gate
  policy module is strict. Existing diagnostics have exact file/rule/message/count
  allowances in quality/baseline.json. These are visible adoption debt, not a claim
  of strict typing. New diagnostics fail; allowance increases fail against the
  previous CI base. Reduce debt when changing its owning module. Full strict
  adoption remains follow-up work as this measured baseline decreases.
- Shape ceilings start at measured global worst values, with no per-file ceilings
  or shape silencers. They cover physical file length, function statements,
  branches, returns, public class methods and an AST complexity metric (one plus
  branch nodes and Boolean alternatives, excluding nested function/class bodies).
  This equivalent stdlib checker replaces Ruff's differently counted complexity
  rules. The standalone collector constraint remains load-bearing when splitting.
- Existing cyclic dependency edges are explicitly recorded. New cyclic edges,
  prohibited policy/presentation edges, and non-stdlib runtime imports fail.
  Legacy cycles require scoped design work to remove; the register does not assert
  that the current package is acyclic.
- unittest remains the runtime-floor test framework. Development-only Hypothesis
  checks selected protocol invariants with a recorded seed. Static synthetic wire
  fixtures are retained as independently hashed contract examples. This overrides
  wholesale pytest/faker conversion while preserving real SQLite/HTTP/filesystem
  integration and deterministic data.
- Required Node tests cannot silently skip. Only named platform/optional central
  tests, with their exact reason and matching environment, may skip. Dev-only
  property and gate-policy tests run separately and have no runtime skip allowance.
- Line and branch coverage have measured adoption floors; platforms may cover
  different branches. Floors cannot decrease. Test count is not a coverage metric.
- Chromium tests exercise real server/UI behavior in both themes, with synthetic
  homes and no external page requests. WCAG 2.2 AA rules supported by axe and
  keyboard navigation are gates. Screenshots/video/traces are retained as evidence;
  pixel baselines are deferred until a canonical rendering platform is selected.
  Manual screen-reader verification remains required before any full WCAG
  conformance claim; this candidate makes no such claim.

## Security and diagnostics

The local principal is the current OS user. The unauthenticated reporter's default
loopback HTTP is an explicit exception to production network TLS/auth assumptions.
It is not a secure boundary against malicious software running as that user.
Shared-interface hosting needs separate access-control design; the local candidate
must not imply authenticated multi-user support. Remote provider/cloud traffic uses
HTTPS and its existing narrowly scoped authentication. Existing negative-origin,
input-validation, expiration and credential-envelope tests remain required.

Erg does not implement encrypted SQLite. Sensitive storage relies on private file
permissions and the operator's OS/disk/backup controls. Enrolled bodies can contain
user-written secrets; never treat envelope filtering as arbitrary-content redaction.
Raw machine evidence stays outside source and release archives. See PRIVACY.md.

Secret scanning runs on history and public working source using checksum-pinned
Gitleaks. Its sole value exception is the exact SQLite key browse_turn_v1, a
non-credential false positive in history_browse.py. Dependency audits cover locked
developer/build tools and browser tools. Empty runtime requirements are checked by
installed-consumer proof, not used as a reason to omit audits.

OTel instrumentation, SDKs, exporters and OTel-specific signal work are deferred.
Existing local health/backlog/error/status diagnostics remain ordinary product
behavior. ADR-002's proposed OTel realization does not govern a new local runtime
obligation; it remains a future interoperability proposal.

## Verification

The shared entrypoint is `uv run --locked python scripts/quality.py all`, also
available as `make quality`. CI runs it, retains revision-linked reports and tests
wheel/source installs on macOS/Linux and Python 3.8/3.12. `make hooks` installs the
provided pre-commit subset; CI remains authoritative when hooks are absent.
The hook uses this same lint/type/architecture policy and checks staged whitespace.
