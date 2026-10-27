---
ddx:
  id: erg.research.agent-telemetry
  authoring:
    home: repo
---
# Research: coding-agent telemetry, session logs, and OpenTelemetry

Date: 2026-10-16. Method: primary docs fetched 2026-10-16 (URLs in Sources, numbered [n]); local ground truth from this
machine, read-only, printing only key names, counts and numeric fields (no prompt or response text was read). Versions seen
locally: Claude Code 2.1.285 CLI, Codex CLI 0.160.0, Codex Desktop builds 0.162.0-alpha.*. Doc pages were read through a
summarizing fetcher, so quoted names are as reported by it; names that matter for code should be re-checked against the raw
page before use. Where I could not confirm something from a primary source or a local file it is marked **unverified**.
Confidence: high / medium / low.

## Summary

- **Session logs are the only complete record, and OTel is not a substitute.** Claude Code and Codex both write full local
  transcripts with per-call token usage, tool calls, model, cwd, git branch and timestamps, with no setup. Native OTel adds
  live streaming, real trace/span ids, latency and (for Claude Code) cost, but is off by default, needs per-agent env or
  config, and misses anything that happened before it was enabled. (high; local files + [1][2][3])
- **OTel is worth adding for Claude Code only as an optional second path**, mainly for `trace_id`/`traceparent` joining with
  gateway spans, `ttft_ms`, `duration_ms` and tool-decision detail that logs lack. Codex OTel exists (logs, metrics, traces
  keys in `[otel]`) but is **not configured here** and its span schema is undocumented in what I read. (medium)
- **Backfilling logs into OTel-shaped spans is feasible but not lossless.** Tokens, model, tool name/outcome, session, cwd,
  turn timing and Codex per-item timing survive. Real trace ids, LLM-call start times, TTFT, and the gateway join do not;
  they must be synthesized (deterministic ids) or joined by request id, which is unverified. (medium)
- **Subscription (no API key) usage is visible in logs** for Claude Code and Codex: tokens are recorded regardless of plan.
  Dollar figures are list-price estimates, not bills ([4]). Codex logs additionally carry plan rate-limit windows
  (`used_percent`, `resets_at`), verified locally. (high)
- **Third-party agents vary widely.** Gemini CLI, Copilot CLI and (per third parties) OpenCode have native OTel; Cursor,
  Windsurf, Aider and Amp have hooks or files but no verified OTel; Cline/Roo have local task files and (Cline) enterprise
  OTel. Most of these rows are medium/low confidence.

## Agent x signal matrix

| Agent | Local logs | OTel (native) | Hooks | Subscription visibility | Content capture | Confidence |
|---|---|---|---|---|---|---|
| **Claude Code CLI** | `~/.claude/projects/<cwd-slug>/<session>.jsonl` plus `.../subagents/*.jsonl`; per-message `usage` (input, output, cache read, cache create 5m/1h, thinking, web search/fetch), model, requestId, cwd, gitBranch, version, timestamps; tool results with `durationMs`. Retention: removed after `cleanupPeriodDays`, 30 default [4][5]. | Metrics + logs/events (GA); traces beta (`CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1`). `CLAUDE_CODE_ENABLE_TELEMETRY=1`, `OTEL_*`; grpc, http/json, http/protobuf [1]. | 33 events incl. `PostToolUse`, `Stop`, `SessionEnd`, `PreModelSwitch`; stdin has `session_id`, `prompt_id`, `transcript_path`, `cwd`; no token usage [6]. | Tokens: yes (logs, OTel). Cost: list-price estimate; Pro/Max cost not a bill [4]. Plan-limit bars come from a usage endpoint, not seen in sampled logs. | Logs: full (local). OTel: opt-in per class (`OTEL_LOG_USER_PROMPTS`, `_ASSISTANT_RESPONSES`, `_TOOL_DETAILS`, `_TOOL_CONTENT`, `_RAW_API_BODIES`) [1]. | High |
| **Claude Code Desktop** | Same JSONL schema, `entrypoint` = `claude-desktop`, under `~/.claude/projects/` (cwd slugs under `~/Library/Application Support/Claude/...`). Verified locally. Separate `desktopSessionCleanupPeriodDays` setting exists [5]. | The research note found `claude-code-desktop` spans with `claude_code.llm_request` in the Databricks trace table (infra doc, prior note). Desktop-specific env/config path for OTel: **unverified**. | Shares Claude Code hooks (a `SessionStart` hook is present in local settings.json). Desktop-specific hook behavior: **unverified**. | Same as CLI. | Same as CLI. | Medium |
| **Codex CLI** | `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`; `~/.codex/archived_sessions/*.jsonl.zst` (zstd); `history.jsonl` (prompt text only); SQLite state/logs DBs. Verified locally. | `[otel]` in `~/.codex/config.toml`: `exporter` (logs), `metrics_exporter` (default `statsig`), `trace_exporter`; `otlp-http`/`otlp-grpc`; `protocol = binary\|json` [7][8]. Event names `codex.conversation_starts`, `codex.api_request`, `codex.sse_event`, `codex.websocket_request`, `codex.websocket_event`, `codex.user_prompt`, `codex.tool_decision`, `codex.tool_result` [7]. Span names/attributes: **unverified**. `otel` is ignored in project `.codex/config.toml` [8]. | `hooks.json` / `[hooks]`; events SessionStart, SessionEnd, SubagentStart, PreToolUse, PermissionRequest, PostToolUse, PreCompact, PostCompact, UserPromptSubmit, SubagentStop, Stop, Interrupt; stdin has `session_id`, `transcript_path`, `cwd`, `model`, `turn_id`; no token usage [9]. `notify` fires on `agent-turn-complete` [7]. | Tokens: yes. Logs carry `rate_limits.primary.used_percent/window_minutes/resets_at` and `credits` (local). | Logs: full (reasoning is `encrypted_content`). OTel: `log_user_prompt` opt-in, default false [8]. | High (logs), medium (OTel) |
| **Codex Desktop** | Same rollout format; `originator` = `Codex Desktop` in 518 of 519 sampled `session_meta` records; also `source` `vscode`, `cli`, and subagent variants. Verified locally. | Shares `~/.codex/config.toml` presumably; Desktop honoring `[otel]`: **unverified**. | Same hooks page [9]; Desktop behavior **unverified**. | Same as CLI. | Same as CLI. | Medium |
| **Gemini CLI** | `~/.gemini/tmp/<project_hash>/chats/`; includes tokens (input, output, cached), tool inputs/outputs, thoughts [10]. Default retention: `maxAge` 30d [10]. File format not documented. | Yes: `GEMINI_TELEMETRY_ENABLED`, `..._TARGET` (`local`/`gcp`), `..._OTLP_ENDPOINT`, `..._OTLP_PROTOCOL` (`grpc` default, `http`), `..._OUTFILE`; logs, metrics, traces (`..._TRACES_ENABLED`). Events `gemini_cli.api_response`, `gemini_cli.tool_call`, etc.; metric `gemini_cli.token.usage`; also `gen_ai.client.token.usage`; spans use `gen_ai.*` attributes [11]. | `gemini_cli.hook_call` event exists [11]; hook event list **unverified**. | **unverified** (token fields exist regardless of auth). | **Prompt logging defaults to ON** (`GEMINI_TELEMETRY_LOG_PROMPTS` default true) [11]. | Medium |
| **GitHub Copilot CLI / agent mode** | `~/.copilot/session-state/<id>/events.jsonl`, `~/.copilot/logs/process-*.log`; token fields in `events.jsonl` **unverified** [12]. | Yes: traces (`invoke_agent` root with `chat` and `execute_tool` children), metrics (`gen_ai.client.token.usage`), events [13]. Enablement env vars from third parties only: `COPILOT_OTEL_ENABLED`, `OTEL_EXPORTER_OTLP_ENDPOINT`; third parties disagree on protocol (JSON vs protobuf) [14]. Default exporter may be a local file (low). | Hooks: **unverified** for CLI. SDK has `assistant.usage` events [15]. | **unverified** (usage billed as premium requests; not checked). | Opt-in; classes (identity, prompts, responses, tool args/output, policy) off by default [13]. | Medium (OTel), low (files) |
| **Cursor** | Documented: `transcript_path` supplied to hooks (null if disabled) [16]. On-disk store/format: **unverified** (not in primary docs read). | None documented in primary docs read: **unverified**. | Yes: `sessionStart`..`stop`, `afterAgentResponse`, `afterFileEdit`, `beforeShellExecution`, `preCompact`; fields `conversation_id`, `generation_id`, `model`, `cursor_version`, `user_email`; no per-request tokens [16]. | **unverified** | Hooks see prompts/responses/edits. | Medium (hooks), low (rest) |
| **OpenCode** | Storage path/format **unverified** (docs read do not state it). | `experimental.openTelemetry` flag per a third party [17]; not in the OpenCode config page I read [18]. Community plugins export token/cost metrics [17]. | Plugin events: `session.*`, `message.updated`, `tool.execute.before/after`, `file.edited`, `permission.*` [19]. | **unverified** | Plugin-level (full). | Low-medium |
| **Aider** | `.aider.chat.history.md`, `.aider.input.history`, `--llm-history-file` (raw LLM log), `--analytics-log` [20]. Token fields in these: **unverified**. | None found. | None found. | n/a (BYO API key typical). | Chat and LLM history files hold full text. | Medium (files), low (rest) |
| **Amp** | Thread storage/format: **unverified** (manual and appendix pages do not state it). | **unverified** | **unverified** | **unverified** | Threads are stored server-side ([21] says only that diagnostics are deleted after 7 days). | Low |
| **Cline** | Local task storage: **unverified** from primary docs. | Enterprise OpenTelemetry export (metrics and logs) to Datadog/New Relic/Grafana [22]; env vars **unverified**. Product telemetry is separate and opt-out [22]. | **unverified** | **unverified** | Telemetry excludes code/prompts except for Cline-provider requests [22]. | Low-medium |
| **Roo Code** | Per task `api_conversation_history.json` (raw API messages) and `ui_messages.json`; token/cost in `api_req_started` entries. Source is third-party wiki pages, not primary [23]. | **unverified** | **unverified** | **unverified** | Full in local files. | Low |
| **Windsurf (Cascade)** | Hook-written transcripts: `post_cascade_response_with_transcript` writes JSONL, max 100 files retained [24]. Native session store: **unverified**. | **unverified** (enterprise analytics not checked) | Yes: `pre_/post_read_code`, `write_code`, `run_command`, `mcp_tool_use`, `pre_user_prompt`, `post_cascade_response`; fields `trajectory_id`, `execution_id`, `model_name`; no token usage [24]. | **unverified** | Transcript hook gives full steps. | Medium (hooks), low (rest) |

## Verified local formats

All counts are from this machine on 2026-10-16. Scripts printed only key names, types, counts and numeric fields.

### Claude Code (`~/.claude/projects/**/*.jsonl`)

- 1,681 JSONL files; 1,411 are under `subagents/` (separate file per subagent). First 300 files span 2026-10-04 to
  2026-10-16. In the sampled files `entrypoint` is `claude-desktop` (390 of 390 files that had the field), so this machine's
  data is Desktop-origin; CLI records use the same schema per docs but were **not observed here**.
- Record `type` counts (first 400 files): `assistant` 20,722; `user` 12,877; `attachment` (many subtypes, e.g. `model`,
  `environment`, `session_context`, `hook_*`, `structured_output`, `total_tokens_reminder`) ~25,000; `queue-operation` 2,231;
  `custom-title` 1,694; `last-prompt`, `agent-name`, `bridge-session`, `pr-link`, `mode`, `file-history-snapshot`,
  `worktree-state`, `cost-state` 64, `system` (`stop_hook_summary` 614, `compact_boundary` 4).
- Common envelope on message records: `uuid`, `parentUuid`, `sessionId`, `timestamp`, `cwd`, `gitBranch`, `version`,
  `entrypoint`, `isSidechain`, `userType`, `slug`, `agentId` (subagents).
- `assistant` records carry: `message.id`, `message.model`, `requestedModel`, `requestId`, `message.stop_reason`, `effort`,
  `thinkingDurationMs`, and `message.usage` = `input_tokens`, `output_tokens`, `cache_read_input_tokens`,
  `cache_creation_input_tokens`, `cache_creation.ephemeral_5m_input_tokens` / `ephemeral_1h_input_tokens`,
  `output_tokens_details.thinking_tokens`, `server_tool_use.web_search_requests` / `web_fetch_requests`, `service_tier`,
  `speed`, `inference_geo`, `iterations[]`.
- **Duplication hazard:** in 300 files, 16,817 assistant records share 8,441 unique `message.id`s (one record per content
  block), and 265 repeats had differing `input`/`output` token values. Aggregate per `message.id` taking the last record
  (verify against max) or tokens are roughly double counted.
- `user` records with tool results carry `toolUseResult` with `durationMs`, `code`, `bytes`, `interrupted`, `stdout`/`stderr`
  keys (content), `persistedOutputPath`; `permissionDecision.{decision,reasonType,source}`; `turnPosition`; `promptId`.
- `cost-state` records (a session roll-up, 64 seen): `totalCostUSD`, `totalDuration`, `totalAPIDuration`,
  `totalToolDuration`, `totalLinesAdded`, `totalLinesRemoved`, `hasUnknownModelCost`, and per-model `modelUsage.<model>`
  {`inputTokens`, `outputTokens`, `cacheReadInputTokens`, `cacheCreationInputTokens`, `thinkingTokens`,
  `webSearchRequests`, `costUSD`}. It is undocumented in the docs I read; treat as an implementation detail. `costUSD` is a
  local list-price estimate ([4]).
- `attachment/queued_command.usage` has `durationMs`, `toolUses`, `totalTokens` (subagent summary).
- **Not in logs:** trace/span ids, per-request latency (only record timestamps, `thinkingDurationMs`, tool `durationMs`),
  time to first token, OTel `prompt.id` is present as `promptId` on user records (match to OTel by [6]), API errors/retries
  (not observed as records; `api_error` is an OTel event only), plan-limit percentages (not seen in sampled records).
- Format stability: I found no documentation promising a stable transcript schema; hooks only promise a `transcript_path`
  that "may lag" ([6]). Treat the schema as private and version-gate on the `version` field. (medium)

### Codex (`~/.codex/sessions/**/*.jsonl`, `archived_sessions/*.jsonl.zst`)

- 1,186 live session files; 98 archived as `.jsonl.zst` (readable with `zstd -dc`). `history.jsonl` has only
  `session_id`, `ts`, `text` (prompts), not a transcript, contradicting the config docs' wording that transcripts go to
  `history.jsonl` ([8]); the real transcripts are the rollout files. Also `logs_2.sqlite` (23,882 rows: `ts`, `level`,
  `target`, `thread_id`, body), `goals_1.sqlite`, `queue_1.sqlite`, `memories_1.sqlite`.
- Envelope: `{timestamp, type, ordinal, payload{type,...}}`. Top-level types (first 400 files, counts): `event_msg` (item_completed
  234,025; token_count 109,616; task_started 17,251; task_complete 17,227; thread_settings_applied 10,559;
  turn_aborted 21), `token_usage_record` 102,248, `response_item` (reasoning 89,226; custom_tool_call 66,109 + output 66,112;
  message 54,372; agent_message 22,372; function_call 18,275 + output 20,015; compaction 363), `turn_context` 17,895,
  `inter_agent_communication_metadata` 17,171, `world_state` 1,253, `compacted` 667, `session_meta` 412.
- `session_meta.payload`: `id`, `session_id`, `cwd`, `cli_version`, `originator`, `source` (string `cli`/`vscode` or
  `subagent` object with `thread_spawn.{parent_thread_id, depth, agent_nickname}`), `model_provider`,
  `git.{branch,commit_hash,repository_url}`, `forked_from_id`, `parent_thread_id`, `creator_account_id`,
  `creator_user_id`, `thread_source`. In 519 sampled records: `originator` Codex Desktop 518, `codex-tui` 1; 466 are
  `guardian` subagent sessions, 34 `thread_spawn`.
- Tokens, two parallel forms: `event_msg/token_count` with `info.last_token_usage` and `info.total_token_usage`, and the
  newer `token_usage_record` with `response_id`, `usage`, `turn_token_usage`, `thread_token_usage`. Each has
  `input_tokens`, `cached_input_tokens`, `cache_write_input_tokens`, `output_tokens`, `reasoning_output_tokens`,
  `total_tokens`. Per-model attribution comes from `turn_context.payload.model`, not from the token record.
- **Cumulative and repeated:** `total_token_usage` is cumulative per thread; 8,439 of 118,110 `token_count` records repeated
  the previous total exactly. Do not sum `total_*`; sum per-response `usage`/`last_token_usage` keyed by `response_id`, or
  take deltas of cumulative totals, and dedupe repeats.
- Plan limits in logs: `token_count.rate_limits.primary.{used_percent, window_minutes, resets_at}`, `credits.{balance,
  has_credits, unlimited}`, `plan_type` (null in sampled records), `secondary` (null).
- Timing: `task_started.{started_at, model_context_window, turn_id, root_turn_id}`, `task_complete.{duration_ms,
  time_to_first_token_ms, started_at, completed_at}`, `item_completed.{started_at_ms, completed_at_ms, item.exit_code,
  item.duration.{secs,nanos}, item.status}`. So Codex logs have TTFT and per-tool duration, which Claude logs lack.
- Other: `turn_context` has `model`, `effort`, `approval_policy`, `sandbox_policy`, `cwd`; `compacted` embeds
  `latest_token_usage_record`; `thread_goal_updated.goal.{tokensUsed, timeUsedSeconds}`.
- Content-bearing keys present (not read): `payload.content`, `arguments`, `output`, `aggregated_output`, `stdout`,
  `stderr`; reasoning is `encrypted_content` (opaque).
- **OTel config check:** `grep -i otel ~/.codex/config.toml` found nothing: no `[otel]` section on this machine.
- **Claude Code OTel config check:** `~/.claude/settings.json` has keys `hooks` (SessionStart only), `theme`,
  `agentPushNotifEnabled`, `skipWorkflowUsageWarning`; no `env` block, so no `OTEL_*` or
  `CLAUDE_CODE_ENABLE_TELEMETRY`. Shell-profile or Desktop-app environment was **not** inspected.

## OpenTelemetry GenAI mapping

**Where the spec lives and its status.** The GenAI semantic conventions were moved out of the main semantic-conventions
repo (the old opentelemetry.io page now says it is no longer maintained there) to
`open-telemetry/semantic-conventions-genai` [25]. Every page I read is marked **Development**; only generic attributes
(`error.type`, `server.address`, `server.port`) are Stable [26]. The repo shows no releases and a TODO schema URL [25]. So all
`gen_ai.*` names below may change. (high)

| erg concept | OTel GenAI name | Notes / stability |
|---|---|---|
| LLM call span | name `{gen_ai.operation.name} {gen_ai.request.model}`, kind CLIENT (INTERNAL if in-process), `gen_ai.operation.name=chat` [27] | Development |
| Provider | `gen_ai.provider.name` (e.g. `anthropic`, `openai`, `gcp.gemini`) [26] | Replaced the older `gen_ai.system`; Claude Code still emits `gen_ai.system` [1]. Likely churn. |
| Model | `gen_ai.request.model`, `gen_ai.response.model` [27] | |
| Input tokens | `gen_ai.usage.input_tokens`, **includes cached tokens** [26][27] | Claude's `input_tokens` EXCLUDES cache reads/creation; must add them to map correctly. |
| Output tokens | `gen_ai.usage.output_tokens` | Reasoning is a subset: `gen_ai.usage.reasoning.output_tokens` [27] |
| Cache read | `gen_ai.usage.cache_read.input_tokens` (subset of input) [27] | |
| Cache write | `gen_ai.usage.cache_write.input_tokens` (page defines `cache_write`, not `cache_creation`) [27] | Older drafts used `cache_creation`; name drift is a known risk. |
| Finish reason | `gen_ai.response.finish_reasons` (array) [27] | |
| Response / request id | `gen_ai.response.id` [27] | Maps to Claude `message.id`/`requestId`, Codex `response_id`. |
| Conversation/session | `gen_ai.conversation.id`; must not be synthesized from a trace id or hash [27][28] | Use agent session id; also keep `session.id` as a vendor attribute. |
| Agent span | `invoke_agent {gen_ai.agent.name}`, CLIENT or INTERNAL [28]; `gen_ai.agent.id/name/description/version` | Subagent = nested `invoke_agent`. `invoke_workflow` and `plan` also defined. |
| Tool span | `execute_tool {gen_ai.tool.name}`, INTERNAL; `gen_ai.tool.call.id`; opt-in `gen_ai.tool.call.arguments`, `.result` [26] | Development |
| Content | `gen_ai.input.messages`, `gen_ai.output.messages`, `gen_ai.system_instructions`, `gen_ai.tool.definitions`: all Opt-In [26][27] | May be stored externally by reference. |
| Metrics | `gen_ai.client.operation.duration`, `gen_ai.client.token.usage`, `gen_ai.invoke_agent.duration`, `gen_ai.invoke_agent.tool_calls`, `gen_ai.execute_tool.duration` [29] | Not usable with the Databricks collector (traces only). |
| Not covered by GenAI conventions | cost USD, lines added/removed, commits/PRs, accept/reject decisions, host utilization, plan rate-limit percent | Must be vendor/`erg.*` attributes. |

**What the agents actually emit.**
- Claude Code traces use vendor names: `claude_code.interaction` > `claude_code.llm_request` / `claude_code.tool` >
  `tool.blocked_on_user` / `tool.execution`, and `claude_code.hook`. The `llm_request` span carries `input_tokens`,
  `output_tokens`, `cache_read_tokens`, `cache_creation_tokens` (not `gen_ai.usage.*`), plus partial GenAI names:
  `gen_ai.system`, `gen_ai.request.model`, `gen_ai.response.id`, `gen_ai.response.finish_reasons`, tool spans
  `gen_ai.tool.call.id` [1]. So it is "GenAI-flavored", not conformant. A Databricks view should read the Claude names.
- Gemini CLI spans use `gen_ai.usage.input_tokens/output_tokens`, `gen_ai.conversation.id`, `gen_ai.agent.name`,
  `gen_ai.tool.name` and operation names `llm_call`, `tool_call`, `agent_call` (not the spec's `chat`/`execute_tool`) [11].
- Copilot CLI: `invoke_agent` > `chat` / `execute_tool` plus `gen_ai.client.token.usage` per GitHub docs summary [13]: closest
  to spec.
- Codex: events and metrics named `codex.*` [7]; span schema unverified.

## Logs vs OTel

**(a) Only in session logs** (not available from native OTel; high for Claude, Codex):
- Full history before OTel was enabled, and everything when OTel is off or managed settings block it. [1]
- Content that OTel redacts by default or truncates (`CLAUDE_CODE_OTEL_CONTENT_MAX_LENGTH` 60 KB default [1]); Codex
  reasoning is encrypted in logs either way.
- Message tree structure (`parentUuid`, `uuid`, sidechains, compaction boundaries, `compact_boundary.preTokens/postTokens`).
- Per-edit/file context: `file-history-snapshot/delta` backups, `edited_text_file`, `worktree-state`, `pr-link`
  (PR number/url), git branch and commit per session (Codex `session_meta.git`).
- Codex plan rate-limit windows and credits (`token_count.rate_limits`); Codex `thread_goal_updated`; Claude `cost-state`
  roll-ups (undocumented).
- Hook outcomes recorded in-transcript (`hook_*` attachments, `stop_hook_summary`).
- Replayability: tokens can be re-priced later from the log with a different price table.

**(b) Only in OTel** (high for Claude, medium for others):
- Real `trace_id`/`span_id`, `traceparent` propagation to Bash subprocesses and to the Anthropic API/HTTP MCP, with
  inbound `TRACEPARENT` for `-p`/SDK sessions [1].
- Latency detail: `ttft_ms`, `first_content_ms`, `duration_ms` per request, retries (`attempt`), `claude_code.api_error`,
  `api_refusal`, `claude_code.tool.blocked_on_user` duration (human wait time), `claude_code.active_time.total`.
- Tool decision provenance (`source`: config/hook/user_permanent/...), `code_edit_tool.decision` accept/reject, MCP
  connection events, plugin/skill attribution on cost and tokens (`skill.name`, `plugin.name`, `mcp_server.name`) [1].
- Cost computed by the client, including `modelPricing` managed overrides [4]; OTel `cost_usd` is the same list-price
  estimate. Streaming (near real time) rather than file scraping.
- Raw API request/response bodies (`OTEL_LOG_RAW_API_BODIES`) [1].
- Managed-fleet enforcement: managed settings can force exporter and endpoint, repo files cannot enable telemetry [1].

**Overlap (token counts, model, tool name and success, session id, timestamps):** present in both, which is what makes
dedupe by `session.id` + `message.id`/`request_id` possible. (high)

## (c) Can logs be backfilled into OTel-shaped spans for the Databricks trace table?

Known collector constraints (prior note): traces only, http/protobuf only, 200 even when spans are dropped, join to
gateway spans only by `trace_id`, `parent_span_id` unusable, client spans redacted by default.

| Need | Claude Code logs | Codex logs |
|---|---|---|
| Session/trace root | Yes (`sessionId`; create a deterministic `trace_id`, e.g. UUIDv5/hash of agent+session id) | Yes (`session_id`, `root_turn_id`) |
| Turn span | Approximate (`promptId`, `turnPosition`; end = last record time) | Exact (`task_started`/`task_complete.duration_ms`) |
| LLM-call span | Start time not recorded (only completion `timestamp`; could estimate from previous record) | `response_id`, TTFT, token usage; duration approximate |
| Tokens incl. cache/reasoning | Yes, but dedupe per `message.id` and re-map `input_tokens` | Yes, dedupe cumulative repeats |
| Tool span | Yes: `tool_use` block + result `durationMs` | Yes: `item_completed` start/end, exit code |
| Content | Yes locally; policy decision needed before sending (collector redacts client spans by default) | Same |
| Join to gateway spans | **No**, unless `traceparent` was sent at request time. Logs hold no `trace_id`. Possible fallback: join on `requestId` if gateway rows record it (**unverified**), else the existing time/token exact-match dedupe | Same; Codex Desktop `traceparent` propagation unverified (low) |

Verdict: **yes for analytics-grade spans, no for lossless.** Backfilled spans can carry the same token and tool attributes
as native spans, using deterministic ids (idempotent re-send, so replays do not duplicate). They cannot reproduce native
trace linkage, TTFT/latency for Claude, retries and errors, or blocked-on-user time. Mark backfilled spans with a distinct
attribute (e.g. `erg.source=session_log`) so analysts never mix them with native spans, and expect the partial-success
check (`rejected_spans`) to be mandatory because of the 200-while-dropping behavior. (medium; depends on collector
behavior that is Beta)

## Recommended capture strategy

1. **Baseline for every agent: session-log scraping into the erg store** (Claude Code, Codex now; Gemini, Copilot, Cline/Roo
   next). It is zero-config, covers history, works on subscriptions, and is the lossless source of truth. Failure modes:
   private/undocumented schemas drift (version-gate on `version`/`cli_version`; keep unknown record types), duplicates
   (Claude per-block records; Codex repeated cumulative totals; archives re-read as `.zst`), retention (Claude Code
   deletes after 30 days by default; Gemini 30 days; Windsurf transcript hook keeps 100 files [5][10][24]) so ingest
   continuously and set `cleanupPeriodDays` higher, and sidechain/subagent double counting.
2. **Claude Code: add native OTel as an optional enrichment** via managed settings or env in the Desktop and CLI launch
   environment (`CLAUDE_CODE_ENABLE_TELEMETRY=1`, `OTEL_TRACES_EXPORTER=otlp`, `OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf`,
   `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1`, content flags left OFF) pointed at the Databricks collector, plus
   `CLAUDE_CODE_PROPAGATE_TRACEPARENT=1` when routing through a custom gateway [1]. Failure modes: traces are beta; logs and
   metrics are rejected by the collector (400), so only spans are usable; double counting versus backfill (dedupe on
   `session.id` + `request_id`); env must reach the Desktop app, not a shell only; collector silent-drops.
3. **Codex: logs only for now.** `[otel]` is available but undocumented at span level and ignored in project config; revisit
   after a probe. Use `hooks.json` `Stop` + `transcript_path` only as a flush trigger, never as the data source (no token
   usage in hook input [9]).
4. **Hooks as triggers, not data.** Claude (`SessionEnd`, `Stop`), Codex (`Stop`, `SessionEnd`), Cursor (`stop`,
   `afterAgentResponse`), Windsurf (`post_cascade_response_with_transcript`) give a clean "session ended, flush now" signal
   and, for Cursor/Windsurf, the only structured transcript path. None provide token usage [6][9][16][24], so they cannot
   replace logs or OTel for cost.
5. **Proxy/gateway for agents with no usable local usage record** (Cursor, Amp, Windsurf, Aider if tokens are missing):
   the Databricks gateway's `system.ai_gateway.usage` already records every routed call (prior note), but it only sees
   traffic that is routed through it, so IDE-hosted agents that call vendor backends directly remain invisible. Gateway
   usage is the reconciliation source, not the primary for these.
6. **Copilot CLI and Gemini CLI: native OTel to a local file or collector** is the best-documented path; Gemini also has
   `--outfile` style file export, so it can be scraped without a network [11]. Gemini defaults prompt logging ON, so set
   `GEMINI_TELEMETRY_LOG_PROMPTS=false` unless content is wanted.
7. **Price ledger and dedupe are erg's job in all cases.** Neither logs nor OTel give contracted prices ([4]); keep a dated
   price table and treat client-side USD as an estimate.

Cross-cutting failure modes: missing content when opt-in flags are off (by design); clock skew between agent and host
metrics; multi-device usage (logs and OTel are per machine, plan limits are per account [4]); subscription windows reset on
the vendor side, so `used_percent` snapshots must be stored with `resets_at`.

## Open questions

1. Does the gateway log Claude `requestId` / Codex `response_id` / `client_request_id`, so a log-derived span can join a
   gateway span without `traceparent`? (Needed for (c).)
2. Does Codex Desktop honor `[otel]` in `~/.codex/config.toml`, what are its span names and attributes, and does it send
   `traceparent` to the gateway? (Research note finding 9 remains unverified.)
3. Does Claude Code Desktop read OTel env from `settings.json` `env`, managed settings, or only the launching
   environment; does the Desktop `desktopSessionCleanupPeriodDays` differ from the CLI sweep?
4. Does a Pro/Max-authenticated Claude Code session emit plan-limit data anywhere locally, or only via the usage endpoint?
5. What are the token and cost fields in Copilot CLI `events.jsonl`, Cursor's on-disk store, OpenCode storage, Amp threads,
   and Cline/Roo task files? Need first-hand inspection, not docs.
6. Will the GenAI conventions rename or drop `cache_write`/`cache_read` attributes before stabilizing, and which version
   should erg pin (`OTEL_SEMCONV_STABILITY_OPT_IN` was not mentioned on the pages I read)?
7. Is the `cost-state` record in Claude transcripts a supported, stable roll-up that can be used for reconciliation?

## Sources

[1] https://code.claude.com/docs/en/monitoring-usage (first 100,000 of ~152,000 characters read; later events not seen)
[2] https://code.claude.com/docs/en/claude-directory
[3] Local ground truth: `~/.claude/projects/**/*.jsonl`, `~/.codex/sessions`, `~/.codex/archived_sessions`, `~/.codex/config.toml`, `~/.claude/settings.json`
[4] https://code.claude.com/docs/en/costs
[5] https://code.claude.com/docs/en/claude-directory and https://code.claude.com/docs/en/settings (`cleanupPeriodDays`, `desktopSessionCleanupPeriodDays`; the 30-day default is stated in [4])
[6] https://code.claude.com/docs/en/hooks (first 100,000 of ~270,000 characters read)
[7] https://developers.openai.com/codex/config-advanced (redirects to https://learn.chatgpt.com/docs/config-file/config-advanced)
[8] https://developers.openai.com/codex/config-reference (redirects to https://learn.chatgpt.com/docs/config-file/config-reference; first 100,000 of ~158,000 characters read)
[9] https://developers.openai.com/codex/hooks (redirects to https://learn.chatgpt.com/docs/hooks)
[10] https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/session-management.md
[11] https://raw.githubusercontent.com/google-gemini/gemini-cli/main/docs/cli/telemetry.md
[12] https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-config-dir-reference
[13] https://docs.github.com/en/copilot/concepts/enterprise/opentelemetry ; summary of span/metric names comes from a search-result snippet of https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference (OTel section not read directly)
[14] Third-party, conflicting: https://openobserve.ai/docs/integration/ai/github-copilot-tracing/ , https://langwatch.ai/docs/coding-agents/github-copilot-cli , https://langfuse.com/integrations/developer-tools/github-copilot.md
[15] https://docs.github.com/en/copilot/how-tos/copilot-sdk/observability/opentelemetry
[16] https://cursor.com/docs/hooks
[17] Third-party: https://langwatch.ai/docs/coding-agents/opencode , https://github.com/devtheops/opencode-plugin-otel
[18] https://opencode.ai/docs/config/
[19] https://opencode.ai/docs/plugins/
[20] https://aider.chat/docs/config/options.html
[21] https://ampcode.com/manual/appendix and https://ampcode.com/manual
[22] https://docs.cline.bot/more-info/telemetry
[23] Third-party, not primary: https://deepwiki.com/RooCodeInc/Roo-Code/7.1-message-history-and-dual-storage , https://raw.githubusercontent.com/getagentseal/codeburn/main/docs/providers/ibm-bob.md
[24] https://docs.devin.ai/desktop/cascade/hooks (Windsurf Cascade hooks docs now hosted under docs.devin.ai)
[25] https://github.com/open-telemetry/semantic-conventions-genai
[26] https://raw.githubusercontent.com/open-telemetry/semantic-conventions-genai/main/docs/gen-ai/gen-ai-spans.md
[27] https://raw.githubusercontent.com/open-telemetry/semantic-conventions-genai/main/docs/gen-ai/client-inference.md (last ~16,000 characters not read)
[28] https://raw.githubusercontent.com/open-telemetry/semantic-conventions-genai/main/docs/gen-ai/gen-ai-agent-spans.md (last ~47,000 characters not read)
[29] https://raw.githubusercontent.com/open-telemetry/semantic-conventions-genai/main/docs/gen-ai/gen-ai-metrics.md
[30] Prior note: docs/helix/00-discover/research-note-databricks-otel.md
