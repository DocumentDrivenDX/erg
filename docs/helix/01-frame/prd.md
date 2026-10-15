---
ddx:
  id: erg.prd
  status: draft
  links: [product-vision]
---
# PRD: Agent Usage & Productivity Monitor

## Problem
Work is split across Claude Code and OpenAI Codex, many repos, and several machines. There is no single view of what AI work costs (tokens) and what it yields (code, docs, commits), nor of how it correlates with machine load or the operator's own input.

## Users
A single engineer running coding agents on a laptop plus remote boxes.

## Requirements
1. **Token spend** per hour/day, split by project, model, tool (claude/codex), machine; input / output / cache-read / cache-write / reasoning.
2. **Output** per hour/day, split by project, model, tool: lines added/removed (code and documents), documents touched, git commits. Agent-side edits (Edit/Write/apply_patch) and git history (authoritative) are both shown.
3. **Own input**: prompts sent, characters, words, estimated tokens typed, and active (non-idle) time. Raw prompt text is never stored.
4. **Machine performance** (CPU, memory, disk IO, network, load) sampled continuously where a sampler runs, correlated against tokens and output on shared time axes.
5. **Multi-machine**: scrape remote hosts over SSH with zero install (collector streams over stdin); every row carries a machine tag.
6. Local-only: stdlib Python, SQLite, offline static dashboard; no data leaves the machines.
7. Idempotent, incremental ingest; deduplicates streamed/forked/resumed sessions.

## Non-goals
- Billing-grade metering or chargeback: cost is estimation-grade with provenance labels (see `usage-metering` in concerns).
- Being an agent runtime or benchmark harness: fizeau owns measurement and agents; erg archives what they publish.
- LLM-application observability (prompts, evals, prompt management): Langfuse and Helicone cover that.
- Storing prompt or response text; storing credentials.
- Keystroke logging (opt-in only, out of v1); multi-user auth or a hosted service.

## Success
One command refreshes all machines; the dashboard answers every grouping above within a second.
