# Contributing

The local runtime supports Python 3.8+ and the standard library. Keep the portable
collector (`erg/collect.py`) runnable as a single file over SSH. Cloud dependencies
belong only to central packaging; they must not enter the local wheel.

Use synthetic fixtures. Default capture retains usage/metadata; body retention is
explicitly enrolled and forward-only. Never add credentials or real user content
to fixtures, logs, screenshots or commits. See [PRIVACY.md](PRIVACY.md).
Every collector event needs a stable `uid`; ingest and retries stay idempotent.
Update governing specs in `docs/helix/` with behavior changes.

Run the release gate:

```sh
python3 -B -m unittest discover -s tests -v
python3 -B scripts/check-delivery-boundaries.py
python3 -m pip install build
python3 -m build
python3 -B scripts/check-release-artifacts.py
python3 -m venv /tmp/erg-install-check
/tmp/erg-install-check/bin/python -m pip install --no-deps dist/erg-0.1.0rc1-py3-none-any.whl
/tmp/erg-install-check/bin/python -I -B scripts/smoke-installed.py
```

Build/test dependencies are development-only. CI also installs the source archive
into a second fresh environment and runs the same smoke check outside the checkout.
Central JWT tests need PyJWT/cryptography; JavaScript checks need Node. Tests report
these optional-environment skips explicitly.

The shipped `erg/rate_card.json` is the default pricing snapshot, identical in a
checkout and wheel. After editing `catalog/prices.yaml` or `rate-overrides.json`,
run `python3 -B scripts/compile-rate-card.py` and commit the regenerated snapshot.
Explicit `ERG_PRICE_LEDGER` overrides and opt-in shared snapshots remain supported.
Prices are reference values with provenance, not invoices.

Generated bundles, deployment state and machine-specific evidence are ignored.
Keep public release evidence in `docs/helix/04-build/local-first-release.md`.
Tagging/publishing follows review of a committed, validated candidate.

Contributions are dual-licensed under MIT OR Apache-2.0.
