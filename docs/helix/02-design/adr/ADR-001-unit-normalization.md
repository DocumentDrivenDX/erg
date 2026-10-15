---
ddx:
  id: erg.adr.001
  status: proposed
  links: [erg.prd, erg.architecture, product-vision]
---
# ADR-001: Unit normalization (tokens, cost, work, energy)

## Context
erg reports raw tokens. Raw tokens are not comparable across models (a Haiku token is not a frontier-model token),
across billing kinds (subscription vs metered), or against output (lines, commits). The Fizeau agent runtime
(github.com/easel/fizeau) already separates several of these ideas; erg should adopt its vocabulary so catalogs and
reports interoperate.

## Concepts adopted from Fizeau
| Fizeau | erg use |
|---|---|
| Four token buckets: input, output, cache-read, cache-write | Already stored (`fresh_in`, `out`, `cache_read`, `cache_write`) |
| Catalog price fields `cost_input_per_m`, `cost_output_per_m`, `cost_cache_read_per_m`, `cost_cache_write_per_m` (USD per M tokens) | Same field names in erg's price table; `erg prices import <fizeau models.yaml>` |
| **Effective cost** (PAYG-equivalent, used for comparison) vs **actual cash spend** (incremental metered billing) | Subscription traffic (Claude/Codex accounts) has effective cost but no cash spend; gateway/API traffic is metered |
| Cost provenance `reported | configured | unknown`; unknown is never summed as zero | Every cost carries provenance; totals show `known` and `unknown-cost calls` separately |
| `cost_per_success`, `effective_cost_per_pass` | erg: `$ / work unit`, `tokens / work unit`, `work units / $` |
| Catalog `power` 1..10 (model strength) and deployment class | Optional capability-adjusted tokens: tokens weighted by model tier |
| Machine profiles with configured TDP watts | Energy estimate for local load: `kWh ≈ TDP × cpu_util × hours` (local only; API energy stays unknown) |

## Proposed units
- **Token (T)**: four buckets, unchanged. Headline `T_eff = fresh_in + cache_write + out` (cache reads reported separately).
- **Effective cost ($e)**: Σ bucket × catalog price; PAYG-equivalent for every call regardless of billing kind.
- **Cash spend ($c)**: $e restricted to metered billing kinds (gateway/API). Subscription adds 0.
- **Work unit (WU)**: one normalized change line = code lines added + 0.5 × doc lines added + 0.5 × lines removed,
  from commits (authoritative), lockfiles/data excluded, per-commit outliers winsorized. Commits are reported alongside.
- **Efficiency**: `T_eff / WU`, `$e / WU`, `WU / $e`, `WU / kWh`; always shown with the sample size and the unknown-cost share.

## Constraints
- Never invent prices: models without a configured price have `unknown` cost and are excluded from $ totals, with a visible count.
- Prices are data (user-editable, importable), not code.
- Attribution of commits to agents is by `Co-Authored-By`, so efficiency by model is only meaningful where trailers exist.

## Decision direction (2026-10-15)
- erg owns a **dated price ledger** (`catalog/prices.yaml`): per model and surface, effective-dated entries with source and as-of date,
  plus subscription plan prices and billing kind. Field names follow Fizeau's catalog (`cost_*_per_m`). Fizeau keeps power, surfaces,
  reasoning and routing metadata and is expected to read prices from the ledger (a separate Fizeau change).
- Fizeau is a **producer**: measurements it generates (power, timing, benchmark evidence) are archived by erg the way prices are.
  erg provides standardized trace collection and cost accounting; it does not run agents or benchmarks.

## Open questions
Work-unit weights; whether to adopt Fizeau `power` as a normalizer; price source of truth for gateway traffic
(`system.billing.usage` would give `reported` provenance).
