---
ddx:
  id: erg.research.work-units
  authoring:
    home: repo
---
# Units of software work and value for agent-assisted development

Date: 2026-10-16. Status: draft research. Companion data file: `catalog/work-units.yaml` (`schema_version: 0`).
erg is only a consumer of that file; nothing here prescribes erg code.

Confidence tags: **high** = primary source read in full or its key passage read; **medium** = primary page read in part, or consistent secondary sources; **low** = single secondary source or unverified. Source ids `[Sn]` are listed at the end. Where a primary page was not readable (OpenAI returned 403, GitClear page gave no numbers) that is stated.

## Summary

- No accepted unit of software "work" or "value" exists. Every size measure (lines, function points, story points) is contested, and every outcome measure (resolved, merged, accepted) captures a different slice of "done" [S1, S2, S3].
- Strongest finding for a ledger design: **passing a verifier is not the same as being landable or retained.** METR had maintainers review test-passing SWE-bench patches; maintainer merge rates were roughly one-third to one-half of grader pass rates (raw gap 34.9 pp, about 24 pp after normalizing to human golden patches) [S9, high]. So "success" needs a ladder of levels, not one flag.
- Agent-written code appears to churn faster. One study of 33,596 agent PRs found agent-introduced symbols were removed after a median 3 days versus 34 days for human PRs [S17, medium: abstract read via search summary plus arXiv page for a sibling paper]. This supports retention as a distinct, cheap-to-compute signal, and a short survival window as a meaningful lens.
- Productivity at the individual/session level is poorly captured by activity counts. METR's RCT found 19% longer completion with AI while developers believed they were about 20% faster [S7, high]; METR's 2026 follow-up could not produce a reliable estimate because many developers refused the no-AI condition [S8, medium]. Self-report and throughput counts are unreliable value proxies.
- Fizeau treats "success" at two levels: benchmark success (verifier reward > 0, with invalid-run classes excluded and denominators stated) and session success (terminal agent status, a process-level notion) [S20, high]. erg should copy the discipline (versioned formulas, explicit denominators, invalid classes) and not the session-level definition as a value claim.
- **Recommendation (v0):** one primary *volume* unit (`normalized_change`, a size proxy, explicitly not value), a *landing/retention ladder* as the value proxy, an *intent* label, and efficiency ratios that always name which ladder rung is the denominator. All weights are `tunable, no evidence` unless a source supports them; currently none do.

## Candidate units

"Computable" means from data erg can plausibly have: git numstat and history, commit trailers, agent session logs (Edit/Write/apply_patch lines, tool-call counts, timestamps), test/CI results seen in logs or CI, PR merge state.

| # | Unit | Definition | Computable by erg | Gaming / critique risks | Evidence quality |
|---|---|---|---|---|---|
| 1 | Source lines of code (SLOC) added/removed | Count of non-blank, non-comment lines; input to COCOMO (effort = a x KSLOC^b; basic coefficients from 63 TRW projects) [S3] | Yes: git numstat per commit, per file; classify file type by path | Verbosity inflates it; good refactoring shortens code [S2]; agents generate boilerplate cheaply so the metric is more gameable with AI. Function points correlate with LOC (Albrecht), so the two are not independent alternatives [S4] | Weak as value, usable as rough size [S2, medium] |
| 2 | Function points (IFPUG, COSMIC, etc.) | Size of user-visible functionality from inputs, outputs, inquiries, files, interfaces; ISO-standardized variants exist [S4] | No: needs requirements-level counting; automated counting exists (OMG AFP) but needs configuration [S4] | Hides internal algorithmic work; one system counted by different experts varied by a factor of three [S2, medium: Fowler's claim] | Established but not feasible from logs |
| 3 | Story points / velocity | Team-relative estimate of effort [S12] | Only if the tracker is linked; not from agent logs | Invented for planning not measurement; creator says misused and gamed [S12, low: secondary quotes]. Not comparable across teams | Poor for cross-session comparison |
| 4 | Commit count | Commits on a branch, optionally attributed by `Co-Authored-By` trailer | Yes; attribution only where trailers exist (ADR-001) | Commit size is arbitrary; agents/people can split or squash | Weak alone; useful as a companion count |
| 5 | Merged PR count / PR throughput | PRs merged to default branch; Faros, DX and Anthropic all use it [S10, S11, S14] | Yes when PR state is available (GitHub API or merge commits); otherwise approximate from merge commits | PR size varies; Faros reports high-adoption teams merged 98% more PRs while review time rose 91% and delivery metrics were flat [S10, low-medium: secondary coverage of vendor report] | Widely used, vendor-reported |
| 6 | Cycle time / change lead time | Commit-to-production time (DORA) [S5] | Partially: commit to merge from git; production deploy not visible | Org-level; gameable by tiny PRs | Established (DORA) but org-scale |
| 7 | Change failure / rework rate | Share of deployments needing immediate fix; share of unplanned incident-driven deployments (DORA instability) [S5] | Proxy only: revert commits, fix-after-merge commits touching same lines | Needs production signal; proxy is noisy | DORA 2025 links AI adoption to more instability [S6, medium] |
| 8 | Code churn / code survival | Share of added lines still present after N days (git blame), or lines changed again within N days [S16, S17] | Yes: git history, per commit, any N | Rewards not touching code; refactors look like churn; windows are arbitrary; GitClear's own churn definition was not stated on the page read [S16, low] | Directionally supported: agent symbols removed sooner [S17, medium]; GitClear reports cloned lines 8.3% to 12.3% of changed lines 2021-2024 and refactoring-related lines 25% to under 10% [S16, medium] |
| 9 | Copilot suggestion acceptance / lines accepted | Accepted divided by suggested; "lines added" counted when inserted in editor [S13] | No: needs IDE telemetry; agent CLIs do not emit suggestion events | Acceptance is counted even if code is later deleted; GitHub gives no formula; third-party vendors disagree on the formula [S13, medium] | Weak as value; fine as adoption signal |
| 10 | Benchmark "resolved" (SWE-bench) | All FAIL_TO_PASS tests pass and all PASS_TO_PASS tests still pass after the patch [S9a, medium] | Not for real work, except by analogy: "tests green after last edit" | Contamination and flawed tests: OpenAI stopped reporting SWE-bench Verified in Feb 2026, citing an audit where 59.4% of 138 audited hard tasks had material test/description issues [S18, medium: search summary, page returned 403]; another audit reports 54.2% of Verified instances with annotation errors [S9a, low] | Good for model comparison, weak for production value |
| 11 | Cost per resolved | Total spend divided by resolved issues; counts the cost of failures [S19, medium] | Yes if "resolved" is mapped to a ladder rung | Excludes unresolved effort if averaged only over successes; SWE-Effi finds failures burn resources ("expensive failures") [S19] | Metric families exist; exact formulas not verified |
| 12 | Terminal-Bench pass rate | Fraction of tasks where a container-state verifier passes; v2 reports mean over repeated trials [S21, medium] | Not applicable to real sessions | Verifier bypasses and broken oracles: an audit of zero-pass tasks found many were not genuinely unsolved [S21, medium] | Benchmark-only |
| 13 | METR time horizon | Human time (geometric mean of successful skilled baseliners) of tasks the model completes at 50% (or 80%) success; doubling about every 207 days [S1, high] | No: needs human baselines. Cannot be derived from a session log | Needs algorithmic scoring and well-specified tasks; messier tasks do worse [S1, high] | Strong for capability trend, not a per-session unit |
| 14 | Tokens or cost per accepted/landed change | Spend divided by outcome at a chosen rung (Fizeau `cost_per_success`; SWE-Effi) [S19, S20] | Yes, given rung and unit definitions | Ratio of two noisy quantities; denominator definition dominates; unknown cost must stay excluded (ADR-001) | Fizeau implements the idea; no external validation found |
| 15 | AI-attributed line share | Fraction of lines in a commit authored by AI, tracked via git notes (Git AI) [S15] | Only where such notes or trailers exist | Vendor-defined; reports accepted vs committed lines; Thoughtworks Radar rates it "Assess" [S15, medium] | Emerging, vendor spec |
| 16 | Self-reported productivity / time saved | Survey estimates, e.g. Anthropic engineers: +20% to +50% self-reported; 27% of assisted work would not otherwise be done [S14, high] | No (survey) | METR shows perception and measurement can disagree in sign [S7] | Biased; use only as context |
| 17 | Intent (what a session was for) | Category label: feature, fix, refactor, test, docs, chore, investigation, other | Heuristic: commit subject prefix, file-path mix, first prompt text | Misclassification; sessions often mix intents | No standard taxonomy found; AIDev-based papers classify agentic PR task types [S17, S22, low] |
| 18 | Value (completed/accepted/retained) | Ladder of observable proxies: ended, produced diff, verified, landed, retained | Rungs 1-5 yes; business value no | Fowler: true productivity requires delivered business value and that is not measurable from outputs [S2, high] | See Recommendation |

## Evidence and critiques

**Lines of code and COCOMO.** COCOMO takes thousands of delivered SLOC as input; basic coefficients (organic a=2.4, b=1.05) were fit to 63 waterfall-era TRW projects, so it fits modern practice poorly [S3, high for the formula, as read on Wikipedia]. Fowler's critique: well-factored code is shorter, and one refactor alone can double the line count without changing behavior; functionality delivered is not value delivered [S2, high]. Fowler still concedes LOC gives a rough size. For erg this means a line-based unit is acceptable only as a *denominator for trends over many sessions*, never as a ranking of individuals or as value.

**Function points.** ISO-standardized (IFPUG ISO/IEC 20926, COSMIC 19761, etc.), counted from requirements, correlated with LOC in Albrecht's data, and blind to algorithmic complexity [S4, high]. Fowler cites a factor-of-three spread across counters on one system [S2, medium]. Infeasible from agent logs; included for completeness.

**Story points.** Team-local, planning-oriented estimates; their reputed inventor says he regrets them and that velocity is misused and gamed [S12, low: relayed by blogs]. No cross-team meaning; excluded from v0.

**Churn and survival.** GitClear (211 million changed lines, 2020-2024) reports cloned lines rising from 8.3% to 12.3% of changed lines and refactoring-attributed lines falling from 25% to under 10% [S16, medium: page gave no churn figures and no definition of churn; a 3.3% to 7.1% churn figure circulates in a secondary blog and is unverified, **low**]. GitClear is a vendor with a commercial interest. The AIDev-based study found agent-introduced symbols removed after a median 3 days versus 34 days for human PRs, with 7.33% versus 4.10% removed [S17, medium]. A blog summary of an MSR 2026 Codex study says about half of human-added lines persisted at three days and agent code fell below that [S17b, **low**]. No source found that fixes a standard survival window (N days); N is therefore tunable.

**DORA and SPACE.** DORA's five metrics are change lead time, deployment frequency, failed-deployment recovery time (throughput), change fail rate and deployment rework rate (instability) [S5, high]. DORA 2025 (about 5,000 respondents per coverage) reports AI adoption now correlates with higher throughput and still with higher instability [S6, medium: secondary coverage; primary report not read]. SPACE (Forsgren, Storey et al., 2021) lists five dimensions (satisfaction, performance, activity, communication, efficiency/flow), says no single metric suffices, and advises at least three dimensions including one perceptual measure [S23, medium]. Both are team/organization instruments; erg can supply only activity-type inputs. Consequence: erg should not call any single number "productivity".

**Copilot metrics.** GitHub's docs describe acceptance rate and lines suggested/added/deleted without formulas [S13, high for that statement]. Third parties define line acceptance as accepted/suggested, note it counts lines later deleted, and one writes the ratio inverted [S13b, medium]. Not computable for agentic CLIs.

**SWE-bench "resolved" and cost-per-resolved.** Resolved means all designated fail-to-pass tests pass and no pass-to-pass test breaks [S9a, medium]. OpenAI stopped reporting Verified scores in Feb 2026 citing contamination and flawed tests (59.4% of 138 audited tasks had material issues) [S18, medium]. A secondary arXiv paper claims a later retraction of the SWE-bench Pro recommendation; **unverified, low**. METR's maintainer review (296 AI patches, 4 maintainers, 3 repos, 47 human golden patches as noise control) found merge decisions about 24 points below grader scores after normalization and 34.9 pp raw, with caveats: one attempt without feedback, no CI, small sample [S9, high]. Cost per resolved: SWE-Bench+ defines an effectiveness-aware cost as total spend over issues resolved [S19, medium: via search summary]; SWE-Effi argues resolve rate alone hides token and time cost and documents "expensive failures" [S19, high for abstract].

**Terminal-Bench.** Containerized tasks with human oracle solution and a verifier on final container state; scores are mean pass rate over repeated trials; later versions drop saturated or flawed tasks, and an audit found some unsolved tasks were broken oracles or verifier bypasses [S21, medium]. Lesson: even verifier-based success needs invalid-run and bypass handling.

**METR time horizon.** The 50% time horizon is the human task duration at which a model succeeds half the time, with durations from skilled baseliners (geometric mean of successful runs); 80% horizons are about 4-6x shorter; doubling time about 207 days (95% interval 166-240) [S1, high]. It requires human baselines and automatic scoring, so a per-session "human-equivalent hours" cannot be derived honestly from logs. DX reports "human-equivalent hours" for agents [S24, low: vendor, method unknown]; no validated method found.

**Value and perception.** METR RCT: 16 developers, 246 tasks, 19% longer with AI, believed 20% faster; authors say results reflect early-2025 tools [S7, high]. Follow-up (2026) could not give a reliable estimate because 30-50% of developers (per secondary source) would not do no-AI tasks [S8, medium]. Anthropic's internal study: Claude Code transcripts show rising autonomy (max consecutive tool calls 9.8 to 21.2) and merged PRs per engineer per day up 67%, plus self-reported gains; the authors caution about self-report and that task mix shifted [S14, high]. Faros: more PRs, larger PRs, longer review, flat delivery metrics [S10, low-medium].

**Newer (2025-2026) directions found.** Resource-aware effectiveness (SWE-Effi) [S19]; agent-authorship metadata in git notes (Git AI) [S15]; merge-readiness versus test-pass gap (METR) [S9]; agentic-PR datasets (AIDev) with merge-rate, survival and failure-reason studies [S17, S22]; unmerged-PR reasons (test failures and duplicate prior fixes dominate; build failures rare) [S22, medium]. No consensus unit emerged.

## Fizeau's notions

Read from the local clone (untrusted, read-only; nothing executed). Files: `docs/helix/02-design/solution-designs/SD-012-benchmark-evidence-ledger.md`, `docs/benchmarks/sections/method-notes.md`, `docs/benchmarks/data`, `internal/session/usage.go`, `website/content/docs/observability/_index.md`. Confidence: high for what the files state.

- **Benchmark success.** pass@1 = graded repetitions with reward > 0 over total graded repetitions; pass@k = unique tasks solved in any repetition over unique tasks attempted. "Real runs" exclude invalid setup, network, container-startup and zero-turn timeouts so per-trial medians reflect actual model interaction (`method-notes.md`).
- **Session success.** The usage report counts a session as a success when its terminal agent status is `StatusSuccess` (`internal/session/usage.go`). This is process completion, not verification of the outcome. `CostPerSuccess` = known cost divided by successful sessions, nil when there are none or cost is unknown; success rate = success sessions / sessions.
- **cost_per_pass.** In the benchmark aggregates, one row shows `avg_cost_usd` 0.6764 over 4 repetitions with 1 pass and `cost_per_pass_usd` 2.7057, consistent with total cost over passes (inferred from a single row; **medium**). That counts the cost of failures, matching the effectiveness-aware cost idea [S19].
- **tool_calls_per_success.** Named only as an example `components` field in SD-012; no formula or implementation found (**no source found** beyond the name).
- **Model power.** Catalog `power` is an integer (1 weak to 10 frontier in the benchmark scatter) *derived* from raw evidence, policy-owned and versioned; capability-led, with cost/latency kept as routing signals; weights explicitly an open question ("data-driven once enough runs exist") (SD-012; `06-model-power-observations.md`). The harness is never dropped from identity: `ClaudeCode__GLM-4.7` is a model x harness score, not a model score.
- **Claim discipline.** Every claim must carry versions of tool, harness, provider, model, benchmark, scorer, dataset commit, repetition count, denominator and invalid-run classes, plus confidence/uncertainty; comparisons refuse to vary more than the stated axis; derived metrics (FHI) carry formula version, weights, evidence window and exclusions (SD-012).
- **Implications for erg.** (a) Version the definitions file and stamp every reported number with it. (b) State denominators and exclude unknown cost rather than zero it. (c) Do not call terminal-status "success" a value claim; keep it as the lowest rung. (d) Keep model/harness identity attached to efficiency ratios. (e) Do not import power weights as if evidence-backed; Fizeau itself lists them as open.

## Recommendation

Principle: erg should report **volume, outcome rung, intent, and cost** as separate axes and let efficiency ratios name their rung. It should not publish a single "productivity" number. Evidence supports the *structure* below; it does not support any numeric weights.

**Unit 1, `normalized_change` (primary, volume).** Lines changed in commits, after excluding generated, lock, vendored and data files, with doc/comment lines and removed lines possibly down-weighted. ADR-001 proposes added code lines + 0.5 x doc lines + 0.5 x removed lines. No source found for those weights; they are carried in the catalog as `tunable, no evidence`. Winsorization threshold, exclusion patterns: `tunable, no evidence`. Always show raw added/removed alongside. Supported by: Fowler concedes LOC is a rough size [S2]; Albrecht's FP-LOC correlation suggests simple size proxies track each other at aggregate level [S4]. Limits: not value; gameable; cannot compare individuals.

**Unit 2, outcome ladder `value_rung` (value proxy).** Ordered rungs per session: 0 ended (terminal status only); 1 produced a diff (edits/commits attributable to session); 2 verified (a test/build command in the session exited 0 after the last edit); 3 landed (commit on default branch or PR merged within a window); 4 retained (landed change survives N days and is not reverted); 5 not computable (business value). Rationale: test-pass and merge-readiness diverge [S9], agent code churns faster [S17], DORA instability rises with AI [S6]. Window and N are `tunable, no evidence`. Rung 2 is the closest analogue to SWE-bench "resolved" [S9a] but is weaker, because tests may be absent or trivial.

**Unit 3, `retained_change` (secondary).** Fraction of a commit's added lines still present at N days via blame, plus revert detection. Evidence for the premise: S17 (medium). N values: `tunable, no evidence`; suggest reporting two windows rather than choosing one.

**Unit 4, `landed_change` (secondary).** Count of commits/merged PRs attributable to sessions (by trailer, or by time and path overlap if not). Companion to Unit 1, so size and count are never shown alone.

**Unit 5, `intent` (label).** Categories (feature, fix, refactor, test, docs, chore, investigation, other). Heuristic inputs ranked by reliability: commit subject prefix, then file-path mix, then first prompt. A session with no diff is `investigation` only if tool calls were read-only; otherwise `unknown`. No standard taxonomy found, so the list is `tunable, no evidence`; report the share of `unknown`.

**Unit 6, `efficiency` ratios.** `tokens / WU`, `cost / WU`, and `cost per rung-N session`, where cost is effective cost with unknown excluded and counted separately (ADR-001), and the cost numerator **includes sessions that failed** (as Fizeau's `cost_per_pass` and SWE-Effi do) [S19, S20]. Always show n and the unknown-cost share. Never compare across intents without stratifying.

**Not adopted in v0:** function points, story points, Copilot acceptance, DORA metrics (org-level; may be imported if erg later sees deploy data), METR time horizon / human-equivalent hours (no honest method from logs), AI-attributed share (optional import of Git AI notes when present).

**Reporting rules (from evidence).** Report with n; stratify by intent and by model/harness; say which rung and window; stamp the definitions version; treat self-reported value as context only [S7, S14].

## Open questions

1. **Retention window N and survival definition.** No source fixes N; agent code churns within days [S17] but long-lived value may need weeks. Needs empirical calibration on the user's own repos.
2. **Weights in `normalized_change`.** No evidence for 0.5 doc/removed weights. Should a per-file-type weight be learned (e.g., from review effort) or should the unit be volume-only?
3. **Attribution.** Trailers are sparse; time-and-path overlap attribution has unknown error. Git AI notes may help but are vendor-defined [S15].
4. **Verified rung.** Detecting "tests passed" from logs is agent-specific and brittle; trivial or absent tests make rung 2 meaningless.
5. **Value vs. intent.** Investigation sessions produce no diff but may be high value; rung ladder undervalues them. Unknown how to credit them without self-report.
6. **Human-equivalent effort.** METR-style horizons need human baselines; a calibrated proxy (e.g., diff size x intent) would need its own validation. None found.
7. **Gaming/Goodhart.** Once units are visible, agents or users can optimize them (split commits, add boilerplate). No source found measuring this for agent-era metrics.
8. **Unverified claims to re-check:** OpenAI's SWE-bench retraction chain and GitClear's 2025 churn figures; DORA 2025 primary report; METR 2026 update exact numbers.

## Sources

- [S1] METR, Measuring AI Ability to Complete Long Software Tasks: https://arxiv.org/html/2503.14499v3 (read, partial: first ~100k characters)
- [S2] Martin Fowler, CannotMeasureProductivity: https://martinfowler.com/bliki/CannotMeasureProductivity.html
- [S3] COCOMO (Wikipedia): https://en.wikipedia.org/wiki/COCOMO
- [S4] Function point (Wikipedia): https://en.wikipedia.org/wiki/Function_point
- [S5] DORA metrics guide: https://dora.dev/guides/dora-metrics/
- [S6] DORA 2025 coverage (secondary): https://www.infoq.com/news/2025/09/dora-state-of-ai-in-dev-2025 ; https://cusy.io/en/blog/dora-report-2025.html (primary report not read)
- [S7] METR RCT, early-2025 AI and experienced developers: https://metr.org/blog/2025-07-10-early-2025-ai-experienced-os-dev-study/
- [S8] METR 2026 design update (secondary summaries): https://blog.robbowley.net/2026/04/04/metrs-developer-productivity-research-2026-update/ ; https://particula.tech/blog/metr-reversed-19-percent-slower-ai-coding-study (METR's own post not read)
- [S9] METR, Many SWE-bench-passing PRs would not be merged: https://metr.org/notes/2026-03-10-many-swe-bench-passing-prs-would-not-be-merged-into-main/
- [S9a] SWE-bench resolved definition (secondary; original paper not read): https://codesota.com/guides/swe-bench-explained ; annotation-error audit: https://arxiv.org/pdf/2609.08149 (snippet only)
- [S10] Faros AI Productivity Paradox (secondary coverage): https://www.faros.ai/blog/ai-software-engineering ; https://blog.robbowley.net/?p=2545
- [S11] DX Core 4 / AI measurement: https://docs.getdx.com/dx-core-4/
- [S12] Story points critique (secondary): https://www.infoq.com/news/2012/12/stop_points_velocity ; https://infoq.com/news/2010/03/story-points
- [S13] GitHub Copilot usage metrics: https://docs.github.com/en/copilot/concepts/copilot-usage-metrics/copilot-metrics ; [S13b] third-party formulas: https://docs.software.com/article/120-lines-accepted ; https://docs.hivel.ai/copilot-adoption/github-copilot-expanded-metrics-guide
- [S14] Anthropic, How AI is transforming work at Anthropic: https://www.anthropic.com/research/how-ai-is-transforming-work-at-anthropic
- [S15] Git AI: https://usegitai.com/docs/how-git-ai-works (page 404 on fetch; described via search) ; Thoughtworks Radar: https://www.thoughtworks.com/radar/tools/git-ai (via search)
- [S16] GitClear 2025 AI code quality: https://www.gitclear.com/ai_assistant_code_quality_2025_research
- [S17] Agent-generated symbol survival (AIDev-based; 33,596 agent PRs vs 6,618 human): exact arXiv id unconfirmed, search surfaced https://arxiv.org/pdf/2601.17627 and https://arxiv.org/pdf/2601.17581v3; figures from a search summary, not re-read (medium-low) ; [S17b] Codex churn blog summary (low): https://codex.danielvaughan.com/2026/05/01/agent-code-churn-research-codex-cli-durability-patterns/ ; agentic PR study: https://arxiv.org/abs/2607.21832
- [S18] OpenAI, Why we no longer evaluate SWE-bench Verified: https://openai.com/index/why-we-no-longer-evaluate-swe-bench-verified/ (403 on fetch; figures via search summary) ; https://aiweekly.co/node/5874
- [S19] SWE-Effi: https://arxiv.org/abs/2509.09853 ; SWE-Bench+ cost metric (search summary): https://arxiv.org/pdf/2410.06992
- [S20] Fizeau clone (local, read-only): SD-012, method-notes, usage.go as listed above; repo https://github.com/easel/fizeau
- [S21] Terminal-Bench: https://www.tbench.ai/news/terminal-bench-3-0 ; https://arxiv.org/pdf/2609.26826 (search summaries; original paper not read)
- [S22] Unmerged agent PRs: https://arxiv.org/abs/2602.00164
- [S23] SPACE: https://queue.acm.org/detail.cfm?id=3454124 (via search summary) ; https://getdx.com/blog/space-framework-primer/
- [S24] DX AI measurement: https://getdx.com/blog/how-to-implement-ai-measurement-framework/ (vendor, via search)

Not found: a published, validated method to compute human-equivalent effort from agent logs; a standard retention window for code survival; a source for any specific doc/removed-line weight.
