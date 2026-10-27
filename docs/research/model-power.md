# Model power ranking: sources and method (erg.research.model-power)

erg archives fizeau's model-strength measurement ("power", 1..10) as data, the way it archives
prices (PRD P2 "Archiving measurements published by Fizeau (power, timing)"; ADR-001 row
"Catalog `power` 1..10 ... Optional capability-adjusted tokens"). The artifact is
`catalog/power.yaml`; this note records where every number came from and how the gap between
fizeau's measurements and erg's current model lineup was handled.

## What fizeau publishes

Reviewed read-only (nothing executed) from the Go module cache
`~/go/pkg/mod/github.com/easel/fizeau@v0.17.5/` and from GitHub master
(github.com/easel/fizeau, checked 2026-10-16):

- `internal/modelcatalog/catalog/models.yaml` — embedded catalog, schema version 5,
  `catalog_version: 2026-05-08.1`, `generated_at: 2026-04-27`. Per model: `power`,
  `power_provenance` (method, benchmarks, recency, cost, deployment_class),
  `swe_bench_verified`, `exact_pin_only`, deployment class, costs, surfaces.
- `internal/modelcatalog/power.go` — `BootstrapModelPowers`: groups models by
  (family, deployment_class), ranks by recency → cost → SWE-bench, keeps a power only for the
  group leader; others become `power: 0, exact_pin_only` (a routing-eligibility concept, not a
  strength claim). Class defaults/caps: managed/prepaid frontier 9/10, metered_cloud 7/10,
  local_free and community_self_hosted 5/6. The shipped catalog is this bootstrap plus hand
  curation (e.g. sonnet 8, gpt-5.4-mini 8, gemini-2.5-pro 9).
- `scripts/benchmark/terminalbench_model_power.json` (committed 2026-05-06) — external
  Terminal-Bench-2 leaderboard submissions mapped onto the 1..10 scale, with published bands:
  frontier >= 9, medium_frontier 7–8, non_frontier 4–6.
- `docs/benchmarks/` (SD-012 evidence ledger; snapshot generated 2026-05-13) — the benchmark
  run behind the mapping.

## Staleness finding (2026-10-16)

- ddx pins `github.com/easel/fizeau v0.17.3` (`ddx/cli/go.mod`); v0.17.5 (2026-09-21) is the
  latest release. The embedded catalog and benchmark mapping are byte-identical across
  v0.17.3, v0.17.5 and GitHub master, so no newer fizeau measurement exists anywhere.
- fizeau's newest measurement is grok-4.5 (recency 2026-07-08); the catalog itself is
  2026-04/05. erg's price ledger (as_of 2026-10-16) contains a model generation fizeau has
  never ranked: fable-5/5-1, opus-5/5-5, sonnet-5/5-5, gpt-6*, gpt-5.6*, gemini-3.5+,
  gemini-4-argon, glm-5*, kimi-k3, deepseek-v4*, qwen35-122b.

## How `catalog/power.yaml` was produced

- `provenance: reported` — values copied verbatim from the fizeau catalog or the
  Terminal-Bench-2 mapping (each entry cites which, and the benchmark `as_of`). fizeau's
  routing zeros (`power: 0, exact_pin_only` on superseded models, e.g. gpt-5.4) are NOT
  copied; where the benchmark mapping states the strength (gpt-5.4 → 10), that is used and
  the discrepancy noted.
- `provenance: derived` — fizeau has no measurement. Value = fizeau's band for the model's
  deployment class, pinned by (a) the nearest measured predecessor in the same family tier
  and (b) list price + generation ordering from `catalog/prices.yaml`; smaller variants of a
  measured model (nano/lite/luna/flash) sit at or below the measured sibling. Each derived
  entry names its anchor in `note`. All derived values are provisional by construction and
  flip to `reported` when fizeau publishes coverage.
- Models with no fizeau measurement AND no family anchor (llama/gemma families) are `unrated`
  rather than guessed, mirroring the `unpriced` discipline in `prices.yaml`. Embeddings,
  image models, and gateway service aliases are unrated (scale not applicable / identity
  unresolved).
- fizeau-only anchors not in erg's ledger (claude-opus-4.7, gemini-2.5-*, grok-4.5) are kept
  as `reported` entries so every derived value is auditable.

## Refresh path

1. Watch fizeau for a catalog bump (`catalog_version` > 2026-05-08.1) or a new
   `terminalbench_model_power.json` version; on release, re-copy `reported` values and
   retire matching `derived` entries.
2. ddx still pins v0.17.3; irrelevant for this artifact (data identical) but worth bumping
   in `ddx/cli/go.mod` next time ddx is touched.
3. If erg later adopts capability-adjusted tokens (ADR-001 open question), consume
   `power.yaml` via a small compile step like `rates.py::compile_ledger` — stdlib-only,
   fail closed on missing entries (unrated ≠ 0).
