# Contributing

The installed local runtime supports Python 3.8+ with no third-party packages.
The portable collector stays one runnable stdlib file for SSH. Developer tooling
is a separate locked environment: Python 3.12, uv 0.13.3 and Node 22.

```sh
make setup                 # uv sync --locked; npm ci; verified scanner; Chromium
make hooks                 # opt this Git checkout into the pre-commit subset
make quality               # same complete quality command as CI
```

`make lint` runs Ruff check/format, Pyright's explicit adoption baseline,
architecture/stdlib import checks, shape ratchets, and gate/property negative
controls. `make test` runs isolated-state unittest with branch/line coverage and
revision-linked JSON/JUnit reports; unapproved skips fail. `make browser` exercises
real reporters in Chromium/light/dark with accessibility and keyboard checks.
`make audit` checks locked Python build/dev and npm dependencies, Git history and
public source. `make package` builds with the locked backend and verifies fresh
wheel/source consumers. Individual targets support iteration; all must pass before
release. Network-dependent audits/installers fail visibly if unavailable.

The Python 3.8/3.12 macOS/Linux CI matrix uses the stdlib-only `scripts/run-tests.py`
and isolated installed consumers, without development packages. Node is required
for JS/hook tests. Named native/central test skips require the exact reason and
matching platform/missing optional dependencies. Coverage floors and debt limits
live in `quality/baseline.json` and cannot be weakened against the CI base revision.
Legacy type diagnostics/cycles are recorded debt, not full strict conformance.

Reports go under ignored `reports/` and `test-results/`; CI retains synthetic
reports, screenshots, videos and failure traces for 30 days. Preserve needed proof
with a release/PR acceptance record before expiry. Never upload real transcripts,
credentials or raw machine evidence. Review pending changes against the existing
requirement mapping in [quality-plan](docs/helix/04-build/quality-plan.md).

Use synthetic data and meaningful behavioral assertions. Default capture retains
usage/metadata; body capture is explicitly enrolled and forward-only. Read
[PRIVACY.md](PRIVACY.md). Every observation needs a stable identity and idempotent
replay. Update governing artifacts with behavior changes. OTel is deferred.

The bundled `erg/rate_card.json` is the default offline snapshot in both checkout
and wheel. After editing authored prices/overrides, run
`uv run --locked python scripts/compile-rate-card.py` and commit the snapshot.
Reference prices are not invoices. Generated deployment bundles, real ledgers and
operator evidence remain ignored and excluded from archives.

`python3 -B -m unittest discover -s tests -v` remains available for raw runtime
experimentation; the checked runner and complete quality gate govern completion.
Contributions are dual-licensed under MIT OR Apache-2.0.
