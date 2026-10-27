# Local-first release candidate — 2026-10-27

Version: **0.1.0rc1**. Prepared for review; no tag, push, package publication or
cloud deployment is part of this gate.

## Scope

Local usage/work/cost/account reporting, SQLite storage, native source discovery,
explicit forward-only history enrollment and the loopback dashboard. Automatic
collector/reporter installation supports macOS; Linux supports foreground commands
and operator-managed scheduling. No third-party local runtime packages are required.
Central delivery/reporting and fleet deployment remain experimental. Automatic
subscription prices/paid charges may be unavailable and remain unknown.

## Verification

The macOS suites ran from an exact export of the staged candidate. The Linux suite
ran from the source distribution, mounted read-only in a disposable CI-like
container with networking disabled. Results:

| Environment | Tests run | Skipped | Seconds |
| --- | ---: | ---: | ---: |
| macOS, CPython 3.8.20 | 700 | 6 | 257.490 |
| macOS, CPython 3.12.15 | 700 | 6 | 183.710 |
| Linux, CPython 3.12.3 (source archive) | 700 | 3 | 36.229 |

macOS skips are five central JWT/crypto tests and one Azure Functions SDK test.
Linux ran those five JWT/crypto tests in its existing capable runtime; its skips
are the two macOS-only native tests and the Azure Functions SDK test. Node hook/UI
checks ran; the service test helper now retains CI's selected Node path before
isolating each fixture's environment.

Fresh-install checks passed for a wheel on macOS Python 3.8.20 and 3.12.15,
installation from the source archive on macOS Python 3.12.15, and a network-disabled
Linux Python 3.11.17 wheel installation. Each used a new environment and synthetic
home/state outside the checkout, without inherited credentials or discovery roots.
Checks covered entrypoint startup, all runtime module imports, packaged UI/hooks,
validated offline prices, one-shot empty collection, synthetic native collection,
idempotent ingest, body retention disabled by default, and loopback UI/health/report
responses. No real content capture, provider inference or export was performed.

Both source and wheel builds also succeeded with Python 3.8. Build tools are
separate from the dependency-free runtime. The metadata uses the PEP 621 license
table to retain compatibility with Python 3.8-capable setuptools; newer backends
can emit a license-metadata deprecation warning. Both license files are included.

Additional gates: delivery dependency-boundary check, pricing snapshot/source
consistency and explicit override regression, installed reporter price snapshot,
archive asset/license/fixture checks, staged whitespace check, and a high-confidence
credential/private-identifier scan. The scan is a bounded check, not a claim that
arbitrary captured content can be safely published.

## Artifacts and reproduction

Build `erg-0.1.0rc1-py3-none-any.whl` and `erg-0.1.0rc1.tar.gz` using
`python3 -m build`. Validate with `python3 -B scripts/check-release-artifacts.py`.
Run the unittest and install commands in [CONTRIBUTING](../../../CONTRIBUTING.md).
The local `dist/` directory contains the candidate archives and `SHA256SUMS`;
CI builds and uploads archives without publishing them to a package index.

The source archive includes its executable tests and checksum fixtures. The wheel
contains only the erg package and metadata/licenses. Neither includes local ledgers,
raw machine evidence, generated central bundles or bundle deployment state.
Historical operator notes are preserved locally under ignored `.local-evidence/`
and `docs/helix/04-build/evidence/`; they are not public acceptance requirements.

Privacy behavior is governed publicly by [PRIVACY](../../../PRIVACY.md), README and
CONTRIBUTING. Default metadata, provider/account network reads, explicit body
capture/export, retention and uninstall semantics are documented consistently.
