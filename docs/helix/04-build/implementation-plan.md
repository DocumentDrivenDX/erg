---
ddx:
  id: erg.implementation-plan
  authoring:
    home: repo
  links: [prd, erg.architecture, erg.contract.001]
---
# Local-first release candidate

## Scope

Release 0.1.0rc1 covers local agent usage, work and cost reporting, SQLite storage,
forward-only opt-in session history, and the loopback dashboard. Python 3.8+ on
macOS and Linux remains the portable runtime. Automatic collector/reporter service
installation is macOS-only. No third-party runtime dependencies are required.
Central delivery, managed fleet enrollment and desktop activity are experimental
or deferred; their broader specifications do not expand this release's promise.

## Acceptance

1. Build a source distribution and wheel with an explicit isolated backend.
2. Include static UI, lifecycle hooks, dated price snapshot and both licenses.
3. Install both wheel and source distribution into fresh environments outside the
   checkout. Verify CLI, fixture collection, reports, and loopback HTTP serving
   without credentials, source-tree imports, cloud services or runtime packages.
4. Run the unittest suite, delivery boundary check, and installed-package smoke
   check. CI exercises Python 3.8 and 3.12 on macOS and Linux.
5. Explain capture defaults, account/network reads, enrollment, retention,
   expiration and removal consistently in README, PRIVACY and CONTRIBUTING.
6. Commit source, tests and public documentation. Exclude generated bundles,
   local ledgers, credentials and machine-specific qualification evidence.

## Validation

See [release evidence](local-first-release.md) for results and limitations.
The release candidate is prepared for review; tagging, publishing and deployment
are separate operations.
