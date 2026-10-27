---
ddx:
  id: concerns
  authoring:
    home: repo
  links: [prd, erg.adr.003]
---
# Project Concerns: erg

Authority: the operator's local-first quality instruction and explicit OTel deferral
on 2026-10-27, recorded in ADR-003. Selections below use the HELIX library names;
project-specific practices take precedence through the explicit overrides.

## Active Concerns

| Concern | Source | Areas | Why Active | Key Practices |
| --- | --- | --- | --- | --- |
| python-uv | library | all | Portable Python package with separate developer tools | Locked dev group; Ruff lint/format; Pyright adoption ratchet; runtime-floor installed-consumer matrix |
| testing | library | all | Capture/accounting and state transitions need behavioral proof | unittest real SQLite/HTTP/filesystem; generated protocol invariants; measured coverage; exact skip policy |
| verification | library | all | Tests must prove the scoped installed outcome | Revision/code-digest reports; real-browser and package proof; review against existing requirement mapping |
| security-owasp | library | all | Private metadata/bodies, credential reads and local HTTP boundaries | Pinned history/source secret scanner; locked audits; privacy/rejection tests; explicit local trust model |
| scope-discipline | library | all | Local release must not silently activate broader capabilities | Local-first acceptance; central/fleet/desktop remain separate |
| modularity-and-encapsulation | library | all | Source ownership and dependency direction must remain deliberate | Protected policy imports; stdlib runtime imports; recorded cyclic edges; semantic ownership review |
| code-shape-ceilings | library | all | Existing handwritten code needs a measured adoption ceiling | Global measured maxima and only-tighten ratchets; no per-file shape exemptions |
| concurrency-model | library | area:collect, area:store, area:ui | Threads/workers share capture and report state | Owned connections; bounded work; publication locks/cancellation; existing concurrency regressions |
| caching-strategy | library | area:store, area:ui | Derived reports must remain correct and bounded | Revision-keyed publication; warming/staleness and eviction; cache/publication regressions |
| relational-data-modeling | library | area:store | SQLite identities, constraints and migrations protect evidence | Stable keys; transaction/publication discipline; replay/migration and read-only reporter tests |
| resilience | library | area:collect, area:ui, area:gateway | External calls and interrupted work have real failure surfaces | Bounded calls; retry/recovery; last-completed publications; explicit unavailable/unknown state |
| e2e-playwright | library | area:ui | Client-rendered flows need a real browser | Chromium; populated/empty/error/privacy/filter flows; videos/screenshots/failure traces |
| a11y-wcag-aa | library | area:ui | Dashboard controls must be usable and identifiable | Axe-supported WCAG 2.2 AA checks and keyboard/current-view checks in both themes |
| unity-catalog | library | area:gateway | Experimental cloud adapter access must remain governed | Existing cloud contracts; separately qualified identity/grants; no local release dependency |
| databricks-ai-gateway | library | area:gateway | Gateway identity/reconciliation has separate semantics | Existing gateway ownership contracts and cloud acceptance |

## Project Overrides

| Concern | Practice | Override | Authority |
| --- | --- | --- | --- |
| python-uv | Runtime/config packages | Entire installed package stays Python 3.8+/stdlib; JSON/ERG_* configuration validated by owning boundaries; no pydantic runtime | ADR-003; consumer/SSH constraint |
| python-uv | Developer environment/build | Python 3.12, uv 0.13.3, Node 22; locked setuptools/static versioning, not hatchling/dynamic versioning | ADR-003 |
| python-uv, testing | pytest/faker/coverage | Retain unittest; development-only Hypothesis for selected invariants and coverage.py for branches; hashed synthetic protocol fixtures remain contract examples | ADR-003; quality-plan mapping |
| python-uv | Strict typing | Check all local runtime/new quality scripts; strict policy helper. Exact legacy file/rule/message/count debt is visible and cannot grow; resolved allowances must be removed. Full strict migration is named follow-up | ADR-003; quality/baseline.json |
| code-shape-ceilings | Greenfield defaults | Existing-project measured global maxima, AST-defined equivalent metrics; no per-file ceilings or silencers. Lower maxima during decomposition while preserving the runnable collector | ADR-003; quality/baseline.json |
| modularity-and-encapsulation | Existing cycles | Explicit cyclic-edge adoption baseline; new cyclic edges fail. Protected dependency rules still apply without exemptions | ADR-003 |
| security-owasp | TLS/auth/encryption assumptions | Default local OS-user/loopback HTTP, not shared hosting; private files and OS/disk/backup controls, no encrypted-SQLite claim. Remote traffic uses HTTPS/existing auth | ADR-003; PRIVACY.md |
| e2e-playwright | Pixel baselines | Retain screenshots/video/traces as evidence; canonical cross-platform pixel baselines deferred, not claimed as passed | ADR-003 |
| a11y-wcag-aa | Manual screen-reader / conformance | Automated supported rules and keyboard checks are gates; manual screen-reader review remains required before full AA claims. Plain HTML/CSS design decisions stay in architecture/ADR-003 | ADR-003 |
| verification | Skips and evidence | Only named platform/optional-central cases with exact reasons/matching environment may skip. Reports identify revision/code/config digest; retain release proof before CI artifacts expire | ADR-003; quality-plan |

## Area Labels

- `area:collect` — discovery/capture and standalone collector
- `area:store` — SQLite, projections, migrations, prices and allocation
- `area:ui` — local HTTP reporter and dashboard
- `area:gateway` — experimental cloud adapters
- `area:quality` — development and release gates

## Concern Conflicts

| Conflict | Resolution |
| --- | --- |
| Python stack defaults vs stock Python/SSH consumers | Keep runtime constraint; isolate modern tools in development groups |
| Shape reduction vs independently runnable collector | Adopt measured global maxima; decomposition must preserve the single-file distribution behavior |
| Generic network hardening vs offline loopback UI | Explicit local trust model; shared hosting requires separate access-control design |
| Broad cloud product contracts vs local release | Cloud/fleet/desktop qualification and activation stay separate |

## Observability Applicability

**o11y-otel is deferred by the operator.** Add no SDK, exporter or OTel-specific
instrumentation work. Existing health/backlog/error/status diagnostics remain
ordinary product behavior. ADR-002's proposed trace realization is future work.

## Formal Methods Applicability

Not selected for this scope. Import/shape checks and generated protocol tests are
bounded regression tools, not a claim of formal correctness.

## Not Selected

- `usage-metering`: reference costs/observed charges do not drive invoices. Revisit
  if financial chargeback becomes a requirement.
- `auth-local-sessions` / `multi-tenancy`: no authenticated shared-user service is
  claimed. Shared hosting requires separate design.
- A new architecture framework is not selected; current ownership/coupling is
  governed explicitly. Concern adoption does not authorize a rewrite.
