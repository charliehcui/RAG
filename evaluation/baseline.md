# Baseline

Status: **BASELINE COMPLETE ? 16 Development Cases, one valid Run each**. Completed 2026-10-07 (Australia/Sydney). Tester performance optimization has not started.

## Protocol and measurement

- Main: `deepseek/deepseek-v4-flash` / `streamlake/fp8`; Tester: `z-ai/glm-5.3-flash` / `relace`; Jev: `typesafe/jev-1.13`. No Backup.
- 3 Tester slots / browser contexts; existing SQLite Manager/Workers, deterministic reproduction (2 matching successes within 3 attempts) and fresh verification unchanged. Scenario/Ground Truth/version/reset unchanged; no Holdout.
- Model runaway headroom: 500 requests / 10,000,000 input / 1,000,000 output tokens; client tool loop 500 rounds. Existing 900-second operation-time guard, 90 maximum browser steps per Task, 250 replay steps, 120 Jev calls and 2 replans retained. Main allocated lower per-Task steps; those quality failures are retained.
- One valid formal Run per Case. No median/variability repeats. LangSmith OFF except the completed D10 formal Trace `baseline-primary-D10-b12c4c71`; older D04/D07 traces are preserved and not repeated.
- Requests are native dispatch/event counts; Total LLM = Main + Tester, Jev separate. Two interrupted requests are included in counts; their usage/cost is unavailable. Recorded zero usage for these events means unavailable, not zero consumption.
- Task Success = post-run native matching for assigned/attempted Tasks; NORMAL_APPLICATION_BEHAVIOR or APPLICATION_BUG_DETECTED. UNKNOWN is not success. Unstarted dependent Tasks remain visible in reports and E2E failures.
- E2E requires COMPLETED Run/all Tasks, every attempted Task successful, executed assertion coverage for required behavior IDs, every enabled bug detected, and no false positives. This is an operational evidence metric; semantic adequacy is not judged by another LLM.
- Wall-clock = actual Run finish minus start. Tester latency = native Task start/finish intervals. Concurrency utilization = occupied Task time / (3 ? exploration window); waiting occupies a slot. Main phase latency below is recorded model service time, not inferred full phase wall time.
- D09/D12/D15 were externally stopped for demonstrated deadlock/repeated completion checks. Quality failures stay in all-16 denominators. Their human stop timing is identified separately and excluded from paired latency/cost attribution. No token cap was lowered to create savings.

## Valid formal cohort

| Case | Run | Native status | Successful / attempted Tasks | Wall s | Tester / Main / Jev requests |
|---|---|---|---:|---:|---:|
| D01 | `baseline-primary-D01-ab56750b` | COMPLETED | 2/4 | 400.468 | 128 / 9 / 0 |
| D02 | `baseline-primary-D02-8153479b` | STOPPED | 0/1 | 146.944 | 27 / 7 / 0 |
| D03 | `baseline-primary-D03-882ca20d` | STOPPED | 0/3 | 140.968 | 93 / 4 / 0 |
| D04 | `baseline-primary-D04-f62b8fec` | STOPPED | 1/2 | 287.119 | 42 / 5 / 0 |
| D05 | `baseline-primary-D05-75ef471f` | COMPLETED | 0/1 | 248.687 | 32 / 5 / 1 |
| D06 | `baseline-primary-D06-57bf62cb` | COMPLETED | 0/1 | 236.395 | 30 / 5 / 0 |
| D07 | `baseline-primary-D07-7db4e4fb` | STOPPED | 1/3 | 544.218 | 42 / 8 / 0 |
| D08 | `baseline-primary-D08-c75e0077` | COMPLETED | 1/2 | 512.270 | 61 / 5 / 1 |
| D09 | `baseline-primary-D09-ad4f40b6` | STOPPED | 0/1 | 748.093 | 53 / 4 / 0 |
| D10 | `baseline-primary-D10-b12c4c71` | STOPPED | 1/3 | 526.092 | 61 / 8 / 0 |
| D11 | `baseline-primary-D11-bdf645e5` | COMPLETED | 0/1 | 382.971 | 91 / 5 / 0 |
| D12 | `baseline-primary-D12-203c0ede` | STOPPED | 0/1 | 426.480 | 30 / 3 / 0 |
| D13 | `baseline-primary-D13-105db278` | COMPLETED | 1/3 | 411.385 | 156 / 4 / 0 |
| D14 | `baseline-primary-D14-a480af97` | COMPLETED | 0/2 | 324.588 | 46 / 9 / 0 |
| D15 | `baseline-primary-D15-945dc3a2` | STOPPED | 1/3 | 805.086 | 167 / 5 / 1 |
| D16 | `baseline-primary-D16-866b1f66` | COMPLETED | 0/3 | 322.693 | 117 / 5 / 1 |

All 16 Cases have E2E FAIL. COMPLETED execution is not a quality PASS. Failed results are not rerun to improve scores.

## Main Agent Baseline

| Metric | Result |
|---|---:|
| Planning Success (semantic) | N/A |
| Final Summary Faithfulness (semantic) | N/A |
| Replan Count | 5 |
| Planning LLM requests / model service seconds | 62 / 622.019 |
| Replan LLM requests / model service seconds | 15 / 183.931 |
| Summary LLM requests / model service seconds | 13 / 382.536 |
| Full planning/replan/summary phase wall time | N/A |
| Total LLM Requests | 91 |
| Complete Input / Output Tokens | N/A |
| Complete Cost | N/A |
| Recorded Input / Output lower bounds | 539,701 / 78,088 |
| Recorded cost lower bound (USD) | $0.018733 |

13 final summaries completed; 3 did not run. One cancelled Main request has unknown phase and usage. No semantic evaluator/LLM Judge was added.

## Tester Agent Baseline

| Metric | Result |
|---|---:|
| Task Success | 8/34 (23.53%) |
| LLM Requests / Task | 34.588 |
| LLM Requests total / mean per Run | 1176 / 73.500 |
| Jev Requests / Task | 0.118 |
| Jev Requests total / mean per Run | 4 / 0.250 |
| Average Latency / Task | 192.068 s |
| Longest Task Latency | 738.208 s |
| Complete Input / Output Tokens | N/A |
| Complete Cost | N/A |
| Recorded Input / Output lower bounds | 18,492,510 / 292,118 |
| Recorded cost lower bound (USD) | $0.620585 |

Tester tokens/cost exclude Jev, which is reported separately and included in Overall. D09 has one cancelled Tester request with missing usage.

## Overall Baseline

| Metric | Result |
|---|---:|
| E2E Success | 0/16 (0%) |
| Bug Precision | N/A (0 confirmed Findings) |
| Bug Recall | 0/20 (0%) |
| Reproduction Success | N/A (0 attempted deterministic reproductions) |
| Total / mean Wall-clock | 6464.459 / 404.029 s |
| Total / mean LLM Requests | 1267 / 79.188 |
| Total / mean Jev Requests | 4 / 0.250 |
| Complete Total Tokens / Cost | N/A / N/A |
| Recorded total token lower bound | 19,411,496 |
| Recorded total cost lower bound (USD) | $0.639662 |
| Peak Concurrent Testers | 3 |
| Concurrency Utilization | 40.22% |

The 20 enabled-bug count is Case/bug exposures, not 20 distinct bug implementations. Costs are locally recorded provider estimates, not a reconciled bill. Missing interrupted-call data is not guessed.

## Observed problems and scope

- Main: D09/D12 serialized a required live-session participant behind its observer, producing a planning deadlock. Other Cases exhausted Main-assigned 25/30-step allocations or left dependent Tasks unstarted. Missing dependency/closed-task redirect attempts remain observations. No Main prompt, planning algorithm or latency optimization.
- Tester: many model turns per action/subgoal and repeated DOM inspection; bad selectors/check predicates; missing goal metadata or Finding associations; literal configured inputs prevent deterministic replay.
- Findings: 41 screened RECORDED_INPUT_REFERENCE_MISSING, 2 duplicates, 2 unscreened observations. No confirmed bug, reproduction or fresh verification success is invented.
- Session-ready Observations are available through read_shared_facts; they were not a wrong publishing channel. Deadlock belongs to Main planning.

## Baseline blocker repairs (correctness only)

1. Main feature schemas enumerate configured feature names; existing scope validation remains.
2. Disabled tools remove both tool declarations and tool_choice=none for the same fixed Provider.
3. Formal conversion raises legacy model budgets and client tool-loop headroom without editing Scenario JSON.
4. Tester ActionType and existing Finding status/severity/operator schemas advertise supported values; not_contains is implemented and unknown operators rejected.
5. Hidden elements do not consume the visible-control limit; DOM Inspection returns existing safe control descriptors. Optional whole-page inspections/text assertions use body.
6. Runtime stop marks task_finished; middleware terminates the model loop without another Done request. Goal finish checks retain missing coverage and unreported failures.
7. Assertions use bounded Playwright retries; missing asserted elements are assertion failures. Unambiguous single-goal IDs are recorded, not guessed for multiple goals.
8. Small synchronous create_task writes commit in declared tool-call order.
9. Explicit identity_reference supplies its configured role when redundant role is omitted; explicit conflict remains rejected. This corrected a Runtime bug, not a Main planning error.

Relevant tests passed for each correction; latest cross-role checks and the related architecture suite passed, Ruff passed, strict mypy passed for 40 source files. No performance optimization was applied before this Baseline.

## Preserved invalid / interrupted records

| Case | Preserved Run | Classification |
|---|---|---|
| D01 | `baseline-D01-f2b7ead4` | Scope / tool-choice blocker |
| D02 | `baseline-D02-f842507c` | Scope / tool-choice blocker |
| D03 | `baseline-D03-72e5d2c9` | Old input-token limit |
| D04 | `baseline-D04-e65f18a9` | Scope / tool-choice blocker |
| D05 | `baseline-D05-730f6652` | Old input-token limit |
| D06 | `baseline-D06-c441fa96` | Old input-token limit |
| D07 | `baseline-D07-a461de33` | Old input-token limit |
| D08 | `baseline-D08-644fec4d` | Old input-token limit |
| D09 | `baseline-D09-e6b9913f` | Interrupted for infrastructure corrections |
| D01 | `baseline-valid-D01-56ba9697` | Interrupted to complete framework-budget setup |
| D01 | `baseline-formal-D01-c64423d1` | Pre-contract-fix diagnostic; 100 requests, 19 invalid actions, no completed goal assertions |
| D02 | `baseline-formal-D02-131f43a6` | Interrupted to apply the corrected action contract |

- Prior quota pause: `baseline-final-D01-ad634301`, USER_PAUSED_QUOTA; preserved, not comparable.
- Resumed D01 infrastructure diagnostics: `baseline-checkpoint-D01-a6de160e`, `baseline-validation-D01-c1c97563`, `baseline-ready-D01-f3987193`, `baseline-cohort-D01-378e7b7f`; preserved, excluded.
- Identity-role infrastructure failures: `baseline-primary-D09-1dc55367`, `baseline-primary-D13-65290fc2`, `baseline-primary-D14-ba410a3c`, `baseline-primary-D15-60d8cdbf`, `baseline-primary-D16-764ad7fc`; valid_baseline=false. Their recorded identity_reference values were correct; the earlier Main-blame classification was corrected.
- Only those five affected Cases were repeated after local tests. D09 became a genuine quality deadlock failure and was not repeated again. D13 proved three-Actor real dispatch before D14?D16 continued.

## Continuation Summary

- Baseline complete: the 16 Run IDs above are the sole cohort. Keep all valid quality failures and all excluded historical records.
- Tester optimization: Attempt 1 review complete and PAUSED; D02/D05 valid and preserved, D14 terminal Provider-timeout failure with saved artifacts, not a valid completed sample. See tester_optimization.md for per-Task comparison, local D05 matching correction and PARTIAL KEEP recommendation. No Attempt 2, Baseline repeat or new Trace.
- Attempt 1 representatives from measured data: D02 (No-Bug/basic, 34 total calls), D05 (ordinary permission bug, 37 calls), D14 (complex two-identity/two-bug flow, 55 calls). No additional runs were used to choose them.
- Optimization: 2?4 measured problems, at most 3 attempts each; abandon ineffective directions early. At most one final full-16 optimized validation cohort. No Main Optimization, Holdout, Final Optimization, resume metrics, commit or push.
- Code: minimal correctness changes in agents/main_agent.py, agents/tester_agent.py, providers.py, evaluation/scenarios.py, runtime/candidates.py, runtime/playwright_executor.py, runtime/web_runtime.py and orchestration/runner.py, with related regression tests.
- Raw data: artifacts/runs/<run_id>/state.db and artifacts/runs/<run_id>/<run_id>/report.json, evidence and ground_truth_matching.json. Original earlier data remains artifacts/state/shared_state.db and original run folders.
- Thin temporary helpers: C:/Users/jack/AppData/Local/Temp/rag_eval_run.py and rag_eval_metrics.py; existing FormalRunExecutor/SQLite output, no new evaluation store or registry.
- Do not repeat environment/project survey, blocker fixes, valid Baseline Cases, obsolete low-budget runs, completed D04/D07/D10 traces or stats repeats.
