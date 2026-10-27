---
ddx:
  id: erg.research.pricing-sources
  authoring:
    home: repo
---

# Pricing sources and price-ledger design

Research date: 2026-10-16. All figures below were read on that date from the cited URLs or from read-only Databricks queries. Nothing is guessed; gaps are listed. Seed data: `catalog/prices.yaml`.

## Summary

- **Live pricing works through the Databricks gateway, and it is better than any public list.** With profile `example-profile`: `GET /api/2.0/serving-endpoints/<name>` returns `foundation_model.input_price` and `.price` in DBUs per 1M tokens; `system.billing.list_prices` gives USD per DBU; `system.billing.usage` and `system.ai_gateway.usage` are both readable by this identity; `system.ai_gateway.external_model_spend` carries USD directly for external providers. I reconciled one day (2026-10-13, `claude-sonnet-5-5`): gateway tokens x endpoint DBU rates = 36.05 DBU, and `system.billing.usage` shows 36.0549 DBU. The model is exact for non-cached traffic.
- **Gateway cash price is not the public list price for Anthropic models.** Databricks bills Anthropic models at 0.105 USD/DBU against 0.07 for OpenAI/Gemini, so Claude traffic through the gateway costs 1.5x Anthropic's list (Sonnet 5.5: 3/15 vs 2/10 per MTok). OpenAI models match list (GPT-6.1 Sol 2/10). erg must therefore keep a per-channel price, not one price per model.
- **Official public pages are authoritative for first-party list prices:** Anthropic (fetched in full) and OpenAI (fetched; its page is JS-heavy and was read through a summarizer, so treat as "reported", cross-checked against OpenRouter, which agreed on every GPT-6/5.6 figure).
- **Machine-readable industry feeds:** OpenRouter `/api/v1/models` (public, 466 models, has cache write 5m/1h, batch as separate `:batch` ids, long-context overrides) and LiteLLM JSON (MIT, 4,194 keys, wide coverage). Both lag nothing visible today, but both are aggregator data: use for discovery and fallback, never to override a first-party or gateway price.
- **Gaps:** no public price for `codex-auto-review`; embeddings and many open-weight models are host-dependent; Databricks endpoint metadata publishes no cache-read/cache-write rates, and the gateway usage table reports cache tokens as 0 for Claude traffic.

## Source evaluation

| Source | Access | Freshness | Coverage | Licence | Reliability | Confidence |
|---|---|---|---|---|---|---|
| Databricks serving-endpoint metadata (`/api/2.0/serving-endpoints/<name>`) | Authenticated, read-only; `list` returned `{}` so names must come from usage tables | Live; reflects what the gateway bills | input/output DBU per 1M tokens per endpoint; no cache, batch, long-context fields | Customer's own data | High (reconciled to billing) | High for input/output |
| `system.billing.list_prices` + `system.billing.usage` | SQL warehouse, readable by `example-profile` | Daily/hourly; list prices dated by `price_start_time` | USD per DBU by SKU; DBU per endpoint per hour | Customer's own data | High | High |
| `system.ai_gateway.external_model_spend` | SQL, readable | Hourly buckets, 2026-10-02..16 | USD cost for external providers (seen: azure-openai), with `service_tier` and `long_context` flags | Customer's own data | High but only external-provider traffic | High |
| Anthropic pricing page `platform.claude.com/docs/en/about-claude/pricing` | Public HTML/markdown | Current today; no "last updated" date shown | All Claude models: base, 5m/1h cache write, cache read, batch, fast mode, data residency, Haiku 5.5 long-context tiers | Proprietary page; facts not copyable as a dataset but each price is a fact we record with citation | Authoritative | High |
| OpenAI API pricing `developers.openai.com/api/docs/pricing` | Public; `openai.com/api/pricing` returns 403 to fetchers | Current (mentions rename on 2026-07-30) | GPT-6/5.6/5.5: standard, batch/flex, fast (ex-priority), cache write, >272K tier | Proprietary page | Authoritative, but read via summarizer | Medium-high |
| OpenRouter `https://openrouter.ai/api/v1/models` | Public, no key | Live (a model dated 2026-10-16 was present) | 466 models; prompt, completion, cache read/write 5m/1h, `:batch` variants, `overrides` for long context and time-of-day; Gemini/OpenAI/Anthropic/open-weight | No explicit data licence found; OpenRouter terms apply (not reviewed in detail) | Good; price is the cheapest route, so open-weight models are host-dependent | Medium |
| LiteLLM `model_prices_and_context_window.json` | Public raw GitHub | Updated continuously by community; 4,194 keys | Very wide: provider-prefixed keys (bedrock, vertex, azure, databricks, chatgpt), cache and tier fields | MIT for everything outside `enterprise/` (LICENSE first line: "Portions of this software are licensed as follows") | Community-maintained, can contain errors; key naming inconsistent | Medium |
| Fizeau `modelcatalog` (repo clone) | Local only | Snapshot 2026-05 (stale: lists Opus 4.7 at 15/75) | Pulls from OpenRouter | MIT | Not a source; a design reference | n/a |
| Other lists (Helicone, pricepertoken, etc.) | Not evaluated | | | | | Not used |

Mapping problems seen today: gateway/local ids use dashes (`gpt-6-1-sol`, `gemini-3-5-flash`) while providers and OpenRouter use dots (`gpt-6.1-sol`, `google/gemini-3.5-flash`); Anthropic ids use dashes natively (`claude-sonnet-5-5` vs OpenRouter `anthropic/claude-sonnet-5.5`); dated snapshots (`claude-haiku-4-5-20251001`); Gemini `-preview` suffixes in OpenRouter; LiteLLM prefixes (`azure/`, `vertex_ai/`, `databricks/`, `us.anthropic.`); gateway service aliases (`ai.gateway.glm`) that are not model ids.

## Databricks findings

Profile `example-profile`, workspace `example-workspace`, warehouse `example-warehouse`. All queries were SELECT/DESCRIBE/SHOW with row limits. Nothing was created or altered. No credentials were printed.

**Readable**

- `system.billing.usage`: 54,372 rows. `billing_origin_product` includes `AI_GATEWAY`, `MODEL_SERVING`, `AI_FUNCTIONS`. Docs say admin-only; this identity can read it (it presumably holds the grant described in `organization-infra/docs/databricks-ai-gateway.md`, which says only admins can read it by default). Columns: `sku_name`, `usage_quantity` (DBU), `usage_metadata.endpoint_name`, `usage_type`, `product_features.serving_type`, `custom_tags`, `identity_metadata`. No token counts, no USD.
- `system.billing.list_prices`: USD per DBU by SKU, with `price_start_time`/`price_end_time`. Relevant (Azure): `PREMIUM_ANTHROPIC_MODEL_SERVING` 0.105 (from 2025-03-26), `PREMIUM_OPENAI_MODEL_SERVING` 0.07 and `PREMIUM_GEMINI_MODEL_SERVING` 0.07 (from 2025-09-23), `PREMIUM_SERVERLESS_REAL_TIME_INFERENCE_US_CENTRAL` 0.079.
- `system.ai_gateway.usage`: 37,122 rows. Has `destination_model`, `endpoint_name`, `requester`, `input_tokens`, `output_tokens`, `token_details` (cache_read, cache_creation incl. 5m/1h fields), `invocation_metadata.service_tier`, `session_metadata.coding_agent`. No cost or DBU column. For `claude-sonnet-5-5` the cache token columns were 0 on 2026-10-12..14, so cached tokens appear folded into `input_tokens` (or not reported); this needs confirmation before pricing cache for gateway traffic.
- `system.ai_gateway.external_model_spend`: 356 rows (2026-10-02..16). `usage_quantity` is USD; `pricing_metadata` has `service_tier` and `long_context`. Seen providers: `azure-openai` only (gpt-5.6-terra 4.41 USD total, gpt-6-sol, gpt-6-astra, small others). This is the one place the platform reports USD for you.
- `system.serving.served_entities` and `system.serving.endpoint_usage` exist (served_entities queried with `LIMIT 3`).
- `GET /api/2.0/serving-endpoints/<name>` for each named endpoint returned `foundation_model.input_price`, `.price`, `.price_unit = "DBUs per 1M tokens"`, `pricing_model = "Pay-per-token"`, `display_name`, `api_types`, plus `capabilities` (e.g. `priority_processing`, `long_context`).

**Not readable / absent**

- `GET /api/2.0/serving-endpoints` (list) returned `{}` for this identity, and `databricks serving-endpoints list` printed an empty array, although individual `GET` by name works. Discover names from `system.billing.usage.usage_metadata.endpoint_name` or `system.ai_gateway.usage.destination_model` instead.
- `system.ai` has no tables (`SHOW TABLES IN system.ai` empty).
- No cache-read or cache-write DBU rates, no batch or long-context rates, in endpoint metadata. `list_prices` has no per-token model SKUs, only DBU SKUs.
- No `PREMIUM_*_MODEL_SERVING` price for models other than the three vendor families (DeepSeek, Zhipu, Moonshot SKUs exist in usage but their `list_prices` rows were not queried).

**Derived gateway prices (reported inputs, derived arithmetic; USD = DBU/MTok x USD/DBU)**

| Endpoint | DBU in / out per MTok | USD in / out | Public list in / out | Ratio |
|---|---|---|---|---|
| databricks-claude-sonnet-5-5 | 28.5714 / 142.857 | 3.00 / 15.00 | 2 / 10 | 1.5x |
| databricks-claude-sonnet-5 | 28.5714 / 142.857 | 3.00 / 15.00 | 2 / 10 | 1.5x |
| databricks-claude-opus-5-5 | 57.1429 / 285.714 | 6.00 / 30.00 | 4 / 20 | 1.5x |
| databricks-claude-opus-5 | 71.4286 / 357.143 | 7.50 / 37.50 | 5 / 25 | 1.5x |
| databricks-claude-fable-5-1 | 142.857 / 714.286 | 15.00 / 75.00 | 10 / 50 | 1.5x |
| databricks-claude-haiku-4-5 | 14.286 / 71.429 | 1.50 / 7.50 | 1 / 5 | 1.5x |
| databricks-gpt-6-1-sol | 28.5714 / 142.857 | 2.00 / 10.00 | 2 / 10 | 1.0x |
| databricks-gpt-6-astra | 142.857 / 714.286 | 10.00 / 50.00 | 10 / 50 | 1.0x |
| databricks-gpt-5-6-terra | 28.571 / 171.429 | 2.00 / 12.00 | 2 / 12 | 1.0x |

These are list rates. Any negotiated discount on the account would show only in invoices; the tables above do not expose it. Gateway use also incurs a small separate `AI_GATEWAY` charge (`PREMIUM_SERVERLESS_REAL_TIME_INFERENCE_US_CENTRAL` rows: about 16 DBU against about 20,549 DBU of Anthropic model serving since 2026-07-13), which erg should record as a fee, not a token price.

**Exact queries tried (all succeeded unless noted)**

```sql
SELECT sku_name, usage_unit, pricing.default AS price, price_start_time FROM system.billing.list_prices WHERE sku_name ILIKE '%FOUNDATION%' OR sku_name ILIKE '%MODEL%' LIMIT 15
SELECT sku_name,usage_unit,pricing.default,pricing.effective_list.default,price_start_time,price_end_time,cloud FROM system.billing.list_prices WHERE sku_name IN ('PREMIUM_ANTHROPIC_MODEL_SERVING','PREMIUM_OPENAI_MODEL_SERVING','PREMIUM_GEMINI_MODEL_SERVING','PREMIUM_SERVERLESS_REAL_TIME_INFERENCE_US_CENTRAL')
SELECT count(*) FROM system.billing.usage LIMIT 1
SELECT DISTINCT billing_origin_product FROM system.billing.usage
SELECT sku_name,usage_unit,count(*),sum(usage_quantity) FROM system.billing.usage WHERE billing_origin_product='AI_GATEWAY' GROUP BY 1,2
SELECT DISTINCT usage_metadata.endpoint_name,sku_name FROM system.billing.usage WHERE billing_origin_product='AI_GATEWAY' OR billing_origin_product='MODEL_SERVING'
SELECT usage_date, sum(usage_quantity) FROM system.billing.usage WHERE usage_metadata.endpoint_name='databricks-claude-sonnet-5-5' AND usage_date BETWEEN '2026-10-12' AND '2026-10-14' GROUP BY 1
DESCRIBE system.billing.usage / system.billing.list_prices / system.ai_gateway.usage / system.ai_gateway.external_model_spend
SELECT count(*) FROM system.ai_gateway.usage LIMIT 1
SELECT destination_model, invocation_metadata.service_tier, count(*), sum(input_tokens), sum(output_tokens) FROM system.ai_gateway.usage WHERE event_time > current_date() - 14 GROUP BY 1,2
SELECT date(event_time), sum(input_tokens), sum(output_tokens), sum(token_details.cache_read_input_tokens), sum(token_details.cache_creation_input_tokens) FROM system.ai_gateway.usage WHERE destination_model='claude-sonnet-5-5' AND date(event_time) BETWEEN '2026-10-12' AND '2026-10-14' GROUP BY 1
SELECT usage_metadata.provider,usage_metadata.model,usage_unit,pricing_metadata.service_tier,pricing_metadata.long_context,count(*),sum(usage_quantity) FROM system.ai_gateway.external_model_spend GROUP BY 1,2,3,4,5
SELECT count(*), min(usage_date), max(usage_date) FROM system.ai_gateway.external_model_spend
SHOW SCHEMAS IN system ; SHOW TABLES IN system.ai ; SHOW TABLES IN system.ai_gateway ; SHOW TABLES IN system.serving
SELECT * FROM system.serving.served_entities LIMIT 3
SELECT col_name,data_type FROM (DESCRIBE ...)   -- FAILED: syntax; DESCRIBE cannot be subqueried
SELECT ..., usage_metadata.destination_name ... FROM system.billing.usage   -- FAILED: no such struct field
```

REST: `GET /api/2.0/serving-endpoints` (empty), `GET /api/2.0/serving-endpoints/<name>` for sonnet-5-5, opus-5-5, haiku-4-5, fable-5-1, sonnet-5, opus-5, gpt-6-1-sol, gpt-6-astra, gpt-5-6-terra, gpt-oss-120b, gte-large-en.

Cross-check with `~/Projects/organization-infra/docs/databricks-ai-gateway.md` (Observability section): it states that `system.ai_gateway.usage` needs SELECT or admin, `system.billing.usage` is admin-only for the `db-prod-users` group, and it warns column names differ from the docs (`endpoint_name` vs `service_name`). Today's results are consistent with that (both readable here, `endpoint_name` and `service_name` both present).

## Fizeau approach

Reviewed read-only (not executed): `internal/modelcatalog/openrouter.go`, `manifest.go`, `billing.go`, `catalogdist/`.

- One embedded YAML manifest (`catalog/models.yaml`, schema `version: 5`, `catalog_version` string) compiled into the binary via `go:embed`. Fields per model: `cost_input_per_m`, `cost_output_per_m`, `cost_cache_read_per_m`, `cost_cache_write_per_m`, `openrouter_id`, `surfaces` (map of harness/provider surface to model id), context window, benchmark and "power" scores.
- Refresh is an explicit command (`UpdateManifestPricing`): fetch OpenRouter `/models` (optional `OPENROUTER_API_KEY`, 15s timeout), match each catalog model by `openrouter_id`, then any surface id, then the catalog id; overwrite the four cost fields and context window; write the manifest to a user path; return a not-found list. No scheduler, no TTL.
- Billing model is a coarse enum (`fixed | per_token | subscription`) by provider system or harness: `claude`, `codex`, `gemini`, `grok` harnesses are all "subscription".
- Distribution (`catalogdist`) builds a stable bundle with a `manifest_sha256`, so a published catalog can be fetched and verified.
- Weaknesses relative to erg's needs: single price per model (no channel, no date, no history), floats in USD/M with no provenance or as_of, no 5m/1h cache split, no batch or long-context tiers, no gateway or live price source, and a stale embedded catalog (Opus 4.7 at 15/75). Fizeau's cost is for routing decisions; erg's is for accounting, so erg needs dated, auditable entries.
- Worth borrowing: embedded fallback manifest, `surfaces` alias map, match-by-explicit-id-then-surface-then-name order, not-found report, checksum-verified bundle.

## Subscriptions

Published (read 2026-10-16):

| Plan | Price | Stated limits | Source |
|---|---|---|---|
| Claude Pro | $20/mo ($17/mo billed annually, $200 up front) | rolling 5-hour session plus weekly limits; "at least 5x" Free per session | claude.com/pricing |
| Claude Max 5x | from $100/mo, monthly only | 5x Pro per session; 50% of weekly limits for Fable; page lists $100/mo API credits | claude.com/pricing |
| Claude Max 20x | $200/mo, monthly only | 20x Pro per session; $200/mo API credits | claude.com/pricing |
| Claude Team Standard | $25/seat/mo ($20 annual) | more than Pro, multiplier not stated | claude.com/pricing |
| Claude Team Premium | $125/seat/mo ($100 annual) | 5x Standard seat; pooled credits up to $500/mo for Team | claude.com/pricing |
| ChatGPT Plus | $20/mo | 5-hour and weekly windows; estimated local messages per 5h: GPT-6.1 Sol 15-160, GPT-6 Sol 15-150, GPT-6 Luna 350-3,000, GPT-6 Astra 5-45 | learn.chatgpt.com/docs/pricing |
| ChatGPT Pro | $100, $200 or $500/mo | "no five-hour limit right now"; allowance not published as a number; Ultrafast only on $500 | learn.chatgpt.com/docs/pricing |
| ChatGPT Business | $25/user/mo ($20 annual) | same windows as Plus | learn.chatgpt.com/docs/pricing |

Neither vendor publishes token or message allowances for flat plans (Claude: "no fixed hour, token, or message counts"; OpenAI: estimates only). Pro multipliers vs Plus (5x/20x) appear only in community posts and secondary articles, which also conflict (one claims a cut to 10x); not recorded as fact. The `help.openai.com` and `chatgpt.com/pricing` pages returned 403 to fetchers; the Codex pricing page redirects to `learn.chatgpt.com/docs/pricing`, which was read. Credit-to-dollar conversion is not stated there (credits per MTok are: GPT-6.1 Sol 50 in / 2.5 cached / 250 out).

**Defensible "effective cost" for subscription traffic.** Keep two separate measures and never sum them without a label:

1. **Cash spend**: money actually leaving the account: plan fees prorated to the period, plus overage/credits, plus PAYG and gateway DBU cost. This is a fact from invoices or the plan table.
2. **PAYG-equivalent (effective) cost**: tokens observed locally x the public list price of the same model on the first-party API, using the same dated ledger. It answers "what would this have cost on the API", is an upper-bound-style comparison, and does not claim the subscriber saved that money.

Rules that keep it defensible: label every number with `basis: cash | payg_equivalent`; price subscription traffic only at first-party list (never gateway markup); apply cache read/write and batch/long-context rules exactly as for PAYG; show the ratio `payg_equivalent / plan_fee` as "utilisation" rather than "savings"; attribute a flat plan fee to usage by token-weighted share of the period; if a plan fee is configured but token volume is zero, report plan fee as unused cash. Reasoning-token and cache-token reporting from the CLI logs may be incomplete, so mark the equivalent as an estimate with an `incomplete_usage` flag when cache fields are missing.

## Model coverage for local data

Local DB `~/.local/share/erg/erg.db`, table `llm`: 69 distinct model ids across tools `claude`, `codex`, `gateway`. Row counts are from `select tool,model,count(*)`.

| Local model id (tool, rows) | Priced from | Notes |
|---|---|---|
| claude-sonnet-5-5 (claude 50,497; gateway 12,652) | Anthropic official (claude); Databricks endpoint x DBU (gateway) | gateway price is 1.5x list |
| claude-opus-5-5 (claude 9,930; gateway 6,690) | Anthropic official; Databricks | same |
| claude-fable-5-1 (claude 1,660; gateway 2,405), claude-fable-5, claude-sonnet-5, claude-opus-5, claude-haiku-4-5 (+ `-20251001` snapshot), claude-sonnet-4-5, claude-haiku-5-5 | Anthropic official; Databricks for fable-5-1/sonnet-5/opus-5/haiku-4-5 | `claude-haiku-4-5-20251001` is a dated snapshot alias of haiku-4-5 (assumed, same price) |
| claude-opus-4-8, claude-sonnet-4-6 | Anthropic official | priced in seed |
| gpt-6.1-sol (codex 157,218), gpt-6-sol, gpt-6-astra (codex 35,292; gateway), gpt-6-luna, gpt-5.6-terra/sol/luna, gpt-5.5 | OpenAI official; Databricks `external_model_spend` / DBU for gateway | gateway dashed ids map by alias |
| gpt-5.4-mini/nano, gpt-5.4, gpt-5.2, gpt-5.1, gpt-5, gpt-5-mini, gpt-5-nano, gpt-5.3-codex, gpt-5.5-pro | OpenRouter (not on OpenAI page fetched) | aggregator confidence |
| gemini-3.5-flash, 3.6-flash, 3.7-flash, 3.8-flash, 3.1-pro, 3.1-flash-lite, 3.5-flash-lite, 3-flash, 4-argon | OpenRouter (Google names `-preview` there) | no Google first-party page fetched; mapping of `3-1-pro` to `3.1-pro-preview` is assumed |
| gpt-oss-120b, gpt-oss-20b, glm-5-2/5-3/5-3-flash, kimi-k3, deepseek-v4-1-flash, deepseek-v4-flash-0731, qwen35-122b-a10b, qwen3-next-80b-a3b-instruct, gemma-3-12b-it, llama-4-maverick, llama_v3_3_70b_instruct, meta_llama_v3_1_8b_instruct | No first-party price. Gateway DBU rate exists for gpt-oss-120b only (read), others not yet read | OpenRouter shows host-dependent cheapest-route prices; deepseek-v4.1-flash varies by UTC hour |
| gte_large_en_v1_5, bge_large_en_v1_5, qwen3-embedding-0-6b | Databricks endpoint `databricks-gte-large-en` (1.857 DBU/MTok) is the only read; id mapping assumed | embeddings |
| gemini-3-1-flash-image, gemini-3-pro-image | none in seed | per-image output pricing not modelled |
| **codex-auto-review** (codex 34,753) | **None** | Codex-internal alias; underlying model unknown. Do not guess |
| ai.gateway.glm, ai.gateway.gpt-5-6-luna (codex, 1 row each) | None | gateway service names, not models |

The seed ledger contains 41 priced entries (including two fast-mode rows), a Databricks gateway rate table, 10 subscription plans, and 22 `unpriced` items with reasons. Models in the top-40 query that carry most volume (sonnet-5-5, opus-5-5, gpt-6.1-sol, gpt-6-astra, fable-5-1) are all priced from an authoritative source.

## Proposed ledger schema

File: `catalog/prices.yaml` (committed fallback), overlaid at runtime by a generated cache in the data dir (`~/.local/share/erg/prices.cache.yaml`) and a user override file (`~/.config/erg/prices.yaml`).

```yaml
schema_version: 0
aliases:              # canonical id -> surface spellings (dash/dot, dated snapshots, gateway ids, provider-prefixed ids)
  gpt-6.1-sol: [gpt-6-1-sol, openai/gpt-6.1-sol]
entries:              # one row per (model, channel, service_tier, effective period)
  - model: claude-sonnet-5-5
    channel: anthropic-api | openai-api | databricks-gateway | openrouter | bedrock | ...
    billing_kind: payg | subscription_equivalent | aggregator | gateway_dbu | fixed
    provenance: reported | configured | unknown   # reported = read from a source, configured = user typed, unknown = none verified
    source: <url or api path>
    as_of: '2026-10-16'                # when read
    effective_from: null               # when the vendor says it took effect, if known (needed to price history correctly)
    input: 2
    output: 10
    cache_read: 0.1
    cache_write_5m: 2.5
    cache_write_1h: 4
    tiers:                              # modifiers, applied in a fixed order
      - {kind: batch, multiplier: 0.5}
      - {kind: long_context, min_prompt_tokens: 272000, input: 4, output: 15}
      - {kind: fast, multiplier: 2}
databricks_gateway: {usd_per_dbu: {...}, endpoints: {<endpoint>: {sku, dbu_per_mtok_input, dbu_per_mtok_output}}}
subscriptions: [{plan, vendor, usd_per_month | usd_per_seat_month, source, as_of, provenance}]
unpriced: [{model, reason}]
```

Design rules:

- Entries are append-only and dated. A price change adds a row with a new `as_of`/`effective_from`; the older row stays so history is priced at the rate in force. Costs are computed at ingestion and stored with the entry id used, so later edits do not silently rewrite totals.
- `provenance: unknown` is a first-class value: cost is NULL, never zero. `unpriced` lists the reason (no public price, host-dependent, mapping unconfirmed).
- Channel decides price. Same model id can have a first-party row and a gateway row; the ledger picks the row by the traffic source (`tool` = claude/codex means first-party or subscription-equivalent; `tool` = gateway means `databricks-gateway`).
- Units: USD per 1M tokens as decimals (store as strings or integers in micro-USD to avoid float drift). Ints in the seed are display values.
- Surface aliases resolve before lookup; resolution order: exact id, alias map, snapshot-suffix strip (`-YYYYMMDD`), provider prefix strip, then unpriced. Never fuzzy-match across versions (Sonnet 5 and 5.5 differ in cache-read price: 0.20 vs 0.10).
- Billing kinds on usage rows: `payg`, `subscription_equivalent`, `gateway_dbu`, `gateway_usd` (from `external_model_spend`), `fee`. Plan prices live in `subscriptions` and produce a separate `cash` series.
- Cache semantics: Anthropic reports cache read and creation tokens separately from input; OpenAI "cached input" is a subset of input. Cost functions must be per-vendor; do not share one formula.

## Proposed updater design

Scripts (stdlib-only, matching the repo's "no third-party dependencies" rule): `scripts/prices_search.py` (query sources for a model id, print candidates and diffs, read-only) and `scripts/prices_update.py` (write the cache or a proposed patch to `catalog/prices.yaml`). Offline-capable and idempotent.

**Source precedence (highest first)**

1. User-configured entry (`provenance: configured`), e.g. negotiated discount.
2. Databricks gateway live read (for `tool = gateway` traffic): endpoint metadata x `list_prices`; USD from `external_model_spend` when present. Needs `databricks` CLI/profile; skip silently if absent.
3. First-party official page (Anthropic, OpenAI): fetched and parsed, or maintained by hand when parsing is brittle (the OpenAI page is JS-rendered, 403 to some clients).
4. OpenRouter `/api/v1/models` (public JSON; stable fields, includes cache write 5m/1h and `:batch`).
5. LiteLLM JSON (MIT) as a cross-check and for provider-prefixed ids (bedrock, vertex, azure).
6. Committed `catalog/prices.yaml` fallback. If nothing above resolves, the model is `unknown`.

A disagreement between rank 3 and rank 4/5 is reported, not auto-resolved (OpenRouter and Anthropic agreed on every Claude 5.x price today; OpenRouter and OpenAI agreed on GPT-6/5.6).

**Mapping**

- `openrouter_id` and `litellm_key` fields per canonical model, seeded from the alias table; discovery lists new ids seen in the local DB but missing from the ledger.
- Version-strict: `claude-sonnet-5` and `claude-sonnet-5-5` are different models. Dated snapshots map to the undated model only via explicit alias.

**Staleness policy**

- Every entry carries `as_of`. Fresh <= 7 days for live sources (Databricks, OpenRouter), <= 30 days for official pages; older prices still apply but are flagged `stale` in the dashboard and `erg prices status`.
- A model seen in new traffic with no entry, or an entry whose source disappeared, is flagged `unpriced`, not dropped.
- On a price change the updater appends a new dated row and prints a diff; it never edits history. Large jumps (> 50% change, or a ratio change between channels) require `--accept` and are listed in the CHANGELOG.
- Databricks DBU rates are re-read at each update; `list_prices` rows carry `price_start_time`, so a DBU rate change is a new dated row.

**Offline fallback**

- `catalog/prices.yaml` is committed and package data; erg runs fully offline from it. The runtime cache and user override are overlays. Network and Databricks reads happen only in `prices_update`/the optional background refresh, with short timeouts and no key needed (OpenRouter public; Databricks via the user's existing CLI profile, read-only SELECT with a LIMIT, never printed credentials).
- If the live read fails, the last good cache is used, then the committed file; the source actually used is recorded on each cost row.

**Tests**: golden fixtures for OpenRouter and LiteLLM JSON and for endpoint-metadata responses (no network in `python -m unittest`); a reconciliation test shaped like today's check (tokens x endpoint rates = billing DBU) as an optional live check.

## Open questions

1. Is the 1.5x Anthropic multiplier on `PREMIUM_ANTHROPIC_MODEL_SERVING` the contract rate, or does the account have a discount that only invoices show? Finance should confirm; erg can only show list.
2. How are Claude cache reads/writes billed on the gateway? `ai_gateway.usage` reports 0 cache tokens, and the endpoint metadata has no cache rate. The reconciliation matched with input at the full rate, which suggests cache tokens are either not reported or billed as input. Needs a controlled test or Databricks confirmation.
3. Should "effective cost" for subscription traffic use first-party list, or the cheapest equivalent channel (e.g., the gateway or OpenRouter)? This design says first-party list; the owner may prefer a configurable basis.
4. What is `codex-auto-review` (34,753 rows)? If it is an OpenAI-internal reviewer model with no public price, it should stay unpriced or be attributed to the underlying model via configuration.
5. Does the owner want OpenRouter-only entries (Gemini, older GPT, open-weight) in the committed fallback, or only first-party and gateway prices? The seed includes the former with `channel: openrouter` and aggregator confidence.
6. How to price time-varying and host-varying models (DeepSeek time-of-day overrides; open-weight cheapest-route)? Proposed: unpriced unless a gateway DBU rate exists.
7. Anthropic and OpenAI `effective_from` dates are not on their pages; history before the first ledger run cannot be priced precisely. Use `as_of` plus the first-seen date until a source (e.g., LiteLLM git history) is wired in.
8. Is the account's DBU price for non-Anthropic gateway endpoints (e.g., gpt-oss) the 0.079 serverless SKU? SKU for `databricks-gpt-oss-120b` was observed but the model was not reconciled.

## Sources

- Anthropic pricing: https://platform.claude.com/docs/en/about-claude/pricing
- Claude plans: https://claude.com/pricing
- OpenAI API pricing: https://developers.openai.com/api/docs/pricing
- OpenAI/ChatGPT and Codex plans: https://learn.chatgpt.com/docs/pricing (redirect target of https://developers.openai.com/codex/pricing)
- OpenAI Pro tiers help article (403 to fetcher; existence only via search): https://help.openai.com/en/articles/9793128-about-chatgpt-pro-tiers
- OpenRouter models API: https://openrouter.ai/api/v1/models
- LiteLLM price file: https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json
- LiteLLM licence: https://raw.githubusercontent.com/BerriAI/litellm/main/LICENSE
- Databricks (read-only, profile `example-profile`): `GET /api/2.0/serving-endpoints[/<name>]`, `POST /api/2.0/sql/statements` on warehouse `example-warehouse` for `system.billing.list_prices`, `system.billing.usage`, `system.ai_gateway.usage`, `system.ai_gateway.external_model_spend`, `system.serving.served_entities`
- Internal: `~/Projects/organization-infra/docs/databricks-ai-gateway.md` (Observability section)
- Fizeau clone (read-only, untrusted, not executed): `.../scratchpad/fizeau-src/internal/modelcatalog/{openrouter.go,manifest.go,billing.go}`, `catalogdist/catalogdist.go`
- Secondary (not used as fact): third-party plan-price summaries returned by web search (e.g. benchlm.ai, community.openai.com threads) for Pro multipliers
