---
ddx:
  id: erg.research.databricks-otel
  authoring:
    home: repo
---
# Research Note: Databricks gateway and OpenTelemetry as erg targets

Date: 2026-10-15. Method: the Databricks workspace's own docs in the operator's infra repo, read-only queries against the
live gateway tables, and public web sources. Nothing was written to Databricks.

## Findings
| # | Finding | Evidence | Confidence |
|---|---|---|---|
| 1 | `system.ai_gateway.usage` records every gateway call with requester, model, token buckets, coding agent and session id. | Schema and 27,034 rows reconciled exactly against a direct SQL total (1,840,880,467 tokens). | High |
| 2 | Per-service **inference tables** (`ai.telemetry.*_payload`) are written by the gateway itself; clients cannot write to them. | Infra doc, "Observability". | High |
| 3 | The **unified trace table** `ai.telemetry.unity_gateway_otel_spans` (Beta) accepts client spans through an OTLP collector at `/ai-gateway/otel/v1/traces`. | Table exists; describes as OTel span format, schema v3; holds spans from gateway services, an app, and `claude-code-desktop` (`claude_code.llm_request` spans carry input, output, cache-read and cache-creation token attributes). | High |
| 4 | The collector takes **traces only**, **http/protobuf only** (JSON gets 400), and `/v1/metrics` and `/v1/logs` get 400. Machine-load metrics therefore cannot use OTLP metrics. | Infra doc, tested 2026-09-30. | Medium (vendor may change in Beta) |
| 5 | The collector answers **200 while dropping spans** when tracing is not configured; success is only visible in `partial_success.rejected_spans`. | Infra doc and probe script. | High |
| 6 | Client and gateway spans join on `trace_id` when the client sends W3C `traceparent`; `parent_span_id` is not usable. | Infra doc, checked 2026-09-30. | Medium |
| 7 | Client spans are content-redacted by default; the gateway's own spans carry the full request and response. | Infra doc. | High |
| 8 | Claude Code exports OpenTelemetry natively (env-var configured); Grafana and other dashboards exist for it. | Public sources (Sources below). | High |
| 9 | Codex OTel export and `traceparent` propagation are reported by the infra docs but not verified in this research. | Infra doc only. | Low |
| 10 | The OpenTelemetry GenAI semantic conventions define `gen_ai.usage.input_tokens` / `output_tokens`, are in Development status, and attribute names may change. | Public sources. | Medium |

## Implications for erg
- Local **session traces** can be forwarded to the unified trace table as OTLP spans and join gateway spans on `trace_id`.
- Local **machine load** cannot ride OTLP metrics to this collector; it needs another path (span attributes on a session span, or a governed table write).
- Cost accounting cannot come from the trace table alone: prices are not in it. A dated price ledger is required.
- Gateway rows and local logs describe the same calls when Claude Code routes through the gateway; de-duplication is already implemented by exact match and must carry over to traces.

## Open questions
1. Which write path for machine metrics into Databricks (span attributes, Zerobus Ingest, or a Unity Catalog table)?
2. Does erg emit its own spans (an `erg.session` root with cost and work attributes) or only forward the agents' spans?
3. Retention and access: the trace table is admin-readable only; who may read per-user erg data?
4. How stable are Beta collector semantics; what is the contract-test (the probe) cadence?

## Sources
- ccusage and OTel dashboards survey: https://npmjs.com/package/ccusage, https://grafana.com/grafana/dashboards/25255
- OpenTelemetry GenAI semantic conventions: https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-spans/
