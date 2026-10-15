# Contributing

- Stdlib-only Python 3.8+; the collector (`erg/collect.py`) must stay a single file so it can be piped over SSH.
- Every collector event needs a stable `uid`; ingest must stay idempotent.
- Never store prompt text or credentials. Measure and discard.
- Run `python -m unittest discover -s tests` before sending a PR.
- Specs live in `docs/helix/` (HELIX / DDx). Change the spec with the code.
- By contributing you agree your work is dual-licensed under MIT OR Apache-2.0 (see README, License).
