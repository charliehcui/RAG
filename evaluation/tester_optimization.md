# Tester Optimization

Status: **ATTEMPT 1 — LOCAL VALIDATION PASSED; REPRESENTATIVE CHECKPOINT PENDING**.

Baseline is frozen in baseline.md: 16 valid Cases, 8/34 successful Tasks, 1,176 Tester requests, 34.588 requests/Task, 0/20 bug exposures detected. No Baseline repeat, Main optimization, Holdout or budget reduction.

## Fixed experiment

- Exactly 3 representative Cases per attempt: D02 (No-Bug/basic registration/project flow, 34 Baseline total calls), D05 (ordinary permission bug/multi-step, 37 calls), D14 (complex two-identity/two-bug flow, 55 calls). Selected from existing data without extra selection Runs.
- Main/Tester/Jev Models and Providers, 3 Workers/contexts, Scenario/Ground Truth, reset, reproduction and verification unchanged. Tracing OFF; completed D10 Trace not repeated.
- Local tests precede real checkpoints. At most 3 attempts per problem, no requirement to use all 3. Discard an ineffective direction early. At most one final full 16-case optimized cohort; no intermediate full cohort.

## Problems and Attempt 1

| Problem | Baseline evidence | Attempt 1 change | Attempts used |
|---|---|---|---:|
| Model round per action / repeated inspection | 34.588 Tester requests/Task; many fill/click/inspect turns | Tester instructions group ordered known actions into safe subgoals, inspect at boundaries, stop a batch at unknown targets | 1/3 |
| Configured inputs lose replay references | 41 Findings screened RECORDED_INPUT_REFERENCE_MISSING; no deterministic reproduction attempted | Explicit value_reference for configured INPUT/SELECT, including usernames/blanks; use secret references directly and reuse nonsecret assertion values | 1/3 |
| Goal/Finding metadata and check predicates | Missing assertions/associations, wrong body-text placeholder checks, repeated finish errors | Assigned behavior_id plus goal_check on real goal checks; record failures immediately with matching IDs; use locator visibility for controls; request existing replan on repeated no progress | 1/3 |

This attempt changes only Tester instructions. No new tool, batching engine, context store, architecture layer, concurrency rule or Main instruction. Existing Runtime page lock executes known actions in order; assertions, budgets and success calculation are retained. Jev does not judge expected behavior or final success.

Local validation: a deterministic Fake-client test put username fill, password fill, submit and an actual browser assertion in one model turn, then finished in the second. Recorded action order and goal PASS were verified. No real LLM was called. Ruff passed. Earlier corrected Runtime tests and strict mypy already passed; no new type/interface change.

## Saved representative results

| Case | Baseline Run | Attempt 1 Run | Comparable? |
|---|---|---|---|
| D02 | baseline-primary-D02-8153479b | tester-compact1-D02-282b1395 | Yes; attempted work differs (1 vs 3 Tasks) |
| D05 | baseline-primary-D05-75ef471f | tester-compact1-D05-ae7b8dd0 | Yes; saved post-run matching corrected locally |
| D14 | baseline-primary-D14-a480af97 | tester-compact1-D14-3df0217f | No; Provider timeout / incomplete checks |

Each cell below is Baseline ? Attempt 1. Tester tokens/cost exclude Main and Jev. Task Success is post-run Ground Truth scoring, not the preliminary finish verdict. All per-Task denominators count assigned/attempted Tasks, including failures. Latency/Task is the mean native started_at?finished_at Task duration; it is not just model service time. Wall-clock is the complete native Run duration, including Main, replay, verification and summary. Aggregate means divide summed numerators by summed attempted Tasks. Costs are recorded USD estimates, not reconciled billing.

| Case | Tasks attempted / successful | Tester LLM Requests / Task | Tester Latency / Task (s) | Overall Wall-clock (s) | Jev total / per Task |
|---|---|---:|---:|---:|---|
| D02 | 1/0 ? 3/1 | 27 ? 13 | 70.975 ? 216.552 | 146.944 ? 700.788 | 0/0 ? 0/0 |
| D05 | 1/0 ? 1/1 | 32 ? 15 | 195.924 ? 79.997 | 248.687 ? 149.324 | 1/1 ? 0/0 |
| D14 | 2/0 ? 1/0 observed, infrastructure-invalid | 23 ? 10 observed | 103.628 ? 29733.011 observed | 324.588 ? 29762.269 observed | 0/0 ? 0/0 |

| Case | Tester Input / Output Tokens per Task | Tester Cost per Task (USD) | Overall Cost per Run (USD) |
|---|---|---|---|
| D02 | 196185/4070 ? 160398.333/9230.667 | 0.006233 ? 0.010390 | 0.007378 ? 0.033061 |
| D05 | 339230/8769 ? 168463/9154 | 0.016597 ? 0.010642 | 0.017416 ? 0.011777 |
| D14 | 310422/6318 ? N/A (recorded lower bounds 75877/6284) | 0.010999 ? N/A (lower bound 0.005874) | 0.023631 ? N/A (lower bound 0.006375) |

| Case | Deterministic replay | Fresh verification | Bug matching / quality |
|---|---|---|---|
| D02 | Not attempted ? not attempted; 3 input-reference-blocked Findings plus 2 duplicates | None ? none | TP/FP 0/0 ? 0/0, no seeded Bugs. Extra reproduction Task remains UNKNOWN_INCOMPLETE despite its preliminary PASS. No-Bug E2E still 0. |
| D05 | Not attempted (2 Findings missing references) ? 2/2 matching reproductions | None ? one independent expected-behavior assertion FAIL, confirming the deviation | TP/FP 0/0 ? 1/0; recall 0/1 ? 1/1. Original false positive was an evaluation formatting bug. E2E 0 ? 1. |
| D14 | Not attempted (3 Findings missing references) ? 0/3, all MAX_RUNTIME_REACHED before actions | None ? none | Baseline TP/FP 0/0, recall 0/2. Attempt 1 matching remains raw 0/0 with two missed Bugs, but no valid quality conclusion; joined-delete never started. |

### D14 terminal failure and disconnection check

The SQLite Run started 2026-10-06 14:33:56.159 UTC and finished 2026-10-06 22:49:58.428 UTC (Sydney 2026-10-07 09:49:58). Wall-clock was 8h16m2s. The final Tester request recorded ChatClientException / APITimeoutError after 29538.306s (8h12m18s). Its usage and cost are unavailable. Subsequent replay and final summary hit MAX_RUNTIME_REACHED / BudgetExceededError; this is a consequence of the timeout, not a revived old token restriction.

12 model requests succeeded and 1 failed (3 Main, 10 Tester including the failed request). owner-save failed; dependent joined-delete was never assigned. State DB, report.json, Ground Truth matching and evidence are present. The report is now marked INFRASTRUCTURE_PROVIDER_TIMEOUT and valid_optimization_sample=false, preserving native status and all evidence. No running driver process remains. Chat disconnection is not established as the cause. Do not treat saved closing artifacts as successful Scenario completion, or count this as an Agent Quality Failure.

No automatic replacement D14 was started: the current instruction authorizes a replacement if chat disconnection interrupted it; saved evidence establishes a Provider timeout instead. A future explicitly resumed D14 would be an infrastructure replacement within Attempt 1, not Attempt 2. D02/D05 must remain frozen.

## D05 local investigation and matching correction

Classification **B: Evaluation Correctness Bug**, not a wrong reported Bug. Finding finding-72832837da4b411cb210bc914712458e records behavior EB-PROJECT-DELETE-AUTH, member identity/role, project-1 owned by admin, and actual deletion. Saved fresh-verification network evidence proves member login, prior visibility/ownership, DELETE /api/projects/project-1 = 200, then absence of that same project in subsequent GET /api/projects. The existing ordered behavior matcher returned true before any fix. Two fresh reproductions matched; one independent fresh verification correctly failed the expected preservation assertion.

The only blocking comparison was affected_page='/ (Projects list) and /api/projects' against Ground Truth page='/'. The matcher incorrectly parsed the human annotation as part of the URI path. Minimal correction accepts a parenthesized annotation only when the canonical leading page is independently corroborated by successful verification navigation. The exact page, behavior, role, identity, entity, ordered ownership/deletion proof, stable reproduction and fresh verification criteria remain required. No Ground Truth, Scenario, Finding or replay artifact was changed.

Local deterministic validation: all 22 evaluation unit tests passed, including 12 matching variants covering canonical/annotated pages and rejection of wrong or unverified page, wrong role/behavior/identity/entity, legitimate owner deletion, denied deletion, project still present and unstable reproduction. Ruff passed; strict mypy passed for matching.py. No browser, real model or new replay was run.

Saved D05 Ground Truth matching was recomputed: original TP=0, FP=1 ? corrected TP=1, FP=0; task-reject-delete ? APPLICATION_BUG_DETECTED. Previous and corrected verdicts are retained in the existing SQLite EVALUATION_MATCHING_CORRECTION event; the existing ground_truth_matching.json now holds the corrected result. This evaluation repair is separate from the Tester Optimization attempt count.

## Aggregate comparison

| Metric | Three Cases: Baseline ? Attempt 1 raw observation | Comparable D02+D05: Baseline ? Attempt 1 |
|---|---|---|
| Tasks attempted / successful | 4/0 ? 5/2 observed; D14 invalid | 2/0 ? 4/2 (0% ? 50%) |
| Tester requests total / per Task | 105/26.250 ? 64/12.800 observed | 59/29.500 ? 54/13.500 (-54.24% per Task) |
| Tester latency per Task | 118.539 ? 6092.533s observed | 133.449 ? 182.413s (+36.69%) |
| Overall wall total / mean per Case | 720.220/240.073 ? 30612.381/10204.127s observed | 395.632/197.816 ? 850.112/425.056s (+114.87%) |
| Jev total / per Task | 1/0.250 ? 0/0 | 1/0.500 ? 0/0 |
| Tester input / output tokens per Task | 289064.750/6368.750 ? N/A | 267707.500/6419.500 ? 162414.500/9211.500 |
| Tester total tokens per Task | 295433.500 ? N/A | 274127 ? 171626 (-37.39%) |
| Tester cost per Task (USD) | 0.011207 ? N/A (lower bound 0.009537) | 0.011415 ? 0.010453 (-8.43%) |
| Overall cost total / per Task (USD) | 0.048424/0.012106 ? N/A | 0.024793/0.012397 ? 0.044838/0.011209 |
| Overall LLM requests | 126 ? 79 observed | 71 ? 66 (-7.04%) |
| Replayable Findings with stable reproduction | 0 ? 1 plus one infrastructure-blocked Finding | 0 ? 1 |
| Bug precision / recall | Raw 1/1 / 1/3; incomplete cohort, no valid comparison | N/A / 0/1 ? 1/1 / 1/1 |
| E2E | 0/3 ? 1/3 observed, incomplete cohort | 0/2 ? 1/2 |

D14 raw values are preserved for accounting only. They cannot support a three-Case efficiency or quality pass, a percentage saving, or a general conclusion about complex/parallel behavior. Recorded three-Case Attempt 1 Tester input/output lower bounds are 725535/43130 and total cost lower bound is $0.051213. The paired two-Case comparison is also workload-confounded: D02 advances from one early-stopped Task to three attempted Tasks. It is descriptive, not an equal-work causal speed measurement.

## Six explicit review answers

1. **Action Grouping: supported.** Exploration-only Tester requests/action: D02 27/25=1.080 ? 39/114=0.342 (-68.32%); D05 32/26=1.231 ? 15/28=0.536 (-56.47%). Paired aggregate 59/51=1.157 ? 54/142=0.380 (-67.13%). The 72 D05 replay/verification actions are excluded; counting all 100 would manufacture a better ratio. D14 partial actions are excluded from the conclusion.
2. **Input References: partial improvement, unresolved.** D05 configured inputs now have references (2/2), and both reproductions plus fresh verification execute. D02 input actions improve from 3/7 to 23/27 references, but 4 still lack references and 3 Findings remain blocked. In comparable Cases, 2/2 Baseline Findings were blocked; Attempt 1 has 3 blocked, 2 duplicates and 1 confirmed Finding. Absolute blocked count did not fall in that subset; different Finding counts must not be confused with resolution of the full 41-Finding Baseline issue.
3. **Goal Assertion / Finish: partial improvement.** D02 actual tagged executable goal checks increase 2?22 and all required behavior IDs are covered instead of three missing IDs. D05 uses one direct goal check rather than three repeated checks and reaches verified quality success. D02 still retains an invalid selector failure and an unconfirmed extra Task; its self-written '3/3 reproduction' text is not native deterministic verification. D14 finish behavior cannot be evaluated through the timeout.
4. **Efficiency: mixed.** Per-Task requests and tokens improve. D05 Task latency -59.17%, wall -39.96%, Tester cost -35.88%. D02 Task latency +205.11%, wall +376.91%, Tester cost/Task +66.68%; its mean Tester request service time rises 2.234?15.960s and its attempted workload triples. Paired latency/wall regress and total recorded cost rises 80.85%. No overall latency/cost victory is claimed.
5. **Quality: improved in the two valid Cases, still inadequate.** Task success 0/2?2/4, E2E 0/2?1/2, and D05 yields one stable matched true Bug. D02 still fails No-Bug E2E, references and duplicate reporting remain problems. One matched Bug is not evidence of broad precision/recall robustness. D14 complex quality is unmeasured.
6. **Recommendation: PARTIAL KEEP.** Retain the supported action grouping and configured-input reference direction; do not accept the complete Attempt 1 version as final. Goal/finish correctness, nonreplayable inputs and D02 repeated self-reproduction remain unfinished. Current Tester instructions are left unchanged pending the user's decision; no selective rollback, new Tester/Runtime/Main change or Attempt 2 was made during this review.

## Stop targets and continuation

Original stop targets A (<=3 requests/Task or -40%), B (two of latency -25%, wall -25%, total LLM -30%), C (tokens -20% or cost -20%), D (no quality regression) are retained as historical criteria. The new quality requirement also seeks higher true Task success, replayability and meaningful goals; an already poor Baseline is not a quality floor. On the valid subset, request and per-Task token gains are promising, but latency/wall target B fails and the full representative quality checkpoint is incomplete. Do not launch final full-16 validation.

- **PAUSED ? READY TO CONTINUE.** Finish the Attempt 1 review before deciding any next execution. No remaining unstarted representative Case exists; D14 is the sole missing valid representative result, with its infrastructure failure preserved. It has not been automatically rerun.
- D02/D05 and all 16 valid Baseline Cases must not be repeated. No quality-failure rerun, representative statistics repeats, new Trace, Main Optimization or Holdout.
- If a next attempt is requested, base it on the remaining saved evidence: D02 literal/nonreferenced inputs, duplicate/manual self-reproduction and inefficient inspection/goal finish. Do not implement speculative changes now. At most 3 attempts per major problem; 1 used, not a requirement to use all 3.
- Models/Providers fixed, 3 Workers, Scenario/Ground Truth and architecture unchanged; runaway guards remain. No budget reduction.
- Latest review changes only evaluation/matching.py and its existing unit tests, D05 post-run matching, D14 exclusion annotation and the two existing Markdown summaries. Tester/Main/Runtime code not further modified. No commit/push.
- Raw data stays artifacts/runs/<run_id>/state.db and artifacts/runs/<run_id>/<run_id>/report.json, evidence and ground_truth_matching.json. There is no new result registry or experiment DB. This report is also archived under E:/Codex/reports/.
