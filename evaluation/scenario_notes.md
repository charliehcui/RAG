# Scenario notes

**24 cases: 16 development + 8 holdout.** Holdout is reserved for final regression/evaluation; never tune against it. The quick subset is an annotation within development, not a third split.

| Distribution | Development | Holdout | Total |
|---|---:|---:|---:|
| Easy | 4 | 2 | 6 |
| Medium | 7 | 4 | 11 |
| Complex | 5 | 2 | 7 |
| No-bug | 3 | 2 | 5 |
| Single-bug | 6 | 3 | 9 |
| Multi-bug | 7 | 3 | 10 |
| Multi-step E2E | 12 | 6 | 18 |
| Independent 3-Tester workloads | 4 | 2 | 6 |

The suggested maxima of 5 no-bug + 9 single-bug + 7 multi-bug total only 21. Exactly 24 cases therefore use 10 multi-bug cases and 29 bug appearances, balanced at four-to-five per bug.

| Bug | Behavior | Development / Holdout | Total |
|---|---|---|---:|
| B1 | Deleted task returns | D04, D10, D11, D15 / H07 | 5 |
| B2 | Non-owner deletes project | D05, D12, D14 / H03, H07 | 5 |
| B3 | Empty normalized name accepted | D06, D10, D13, D16 / H05 | 5 |
| B4 | Saved name stale before refresh | D07, D11, D14, D16 / H08 | 5 |
| B5 | Same-operation submission duplicates | D08, D13, D15 / H06, H08 | 5 |
| B6 | Old session retains removed access | D09, D12 / H04, H06 | 4 |

- **No-bug controls:** D01, D02, D03, H01, H02; all switches off.
- **1-vs-3 comparisons:** D03, D13, D15, D16, H07, H08; each provides three meaningful independent goal groups and separate write targets/browser contexts. Keep data, reset, models/providers, tracing and other budgets identical.
- **Quick development subset:** D01, D04, D07, D10, D14, D16; clean checks, defects, multi-step browser/Jev work, ordered handoff and parallel workflows.
- **Coupled workflows:** D09, D12, H04, H06 require synchronized live identities; exclude speedup comparisons and reject one-Tester execution.

Trusted harness setup: explicitly set all six `DEMO_BUG_B1`–`DEMO_BUG_B6` switches, including disabled values; restart when they change. `/test/reset` resets data/sessions, not switches. Run cases serially at application level. Configure account secrets through `DEMO_ADMIN_PASSWORD`, `DEMO_MEMBER_PASSWORD`, `DEMO_MEMBER2_PASSWORD` using the demo defaults, plus a chosen `DEMO_NEW_MEMBER_PASSWORD` for D02. No credentials enter Agent-visible JSON values. Task-status API changes and the optional canvas visual fallback are outside this browser dataset.

Keep `scenarios.py` as the thin Scenario-to-RunConfig conversion: pass only `load_run_config(...)` output to the existing runtime. It includes business goals/behaviors/roles/data/scope/reset/budget, never case switch metadata. It does not read answers. Raw cases and notes are not prompt attachments or Agent tools. Keep **`LANGSMITH_TRACING=false`**; real tracing and uploads are off during these fixes.

**24/24 prepared; previous code blockers resolved.** `repeat_submit` pairs two pending POSTs before either completes, preserving submission identity and sanitized responses in existing history/evidence. Two-request interception expires before subsequent GETs. Cross-task Findings record `behavior_id` and necessary `related_task_ids`; replay merges actual histories up to the Finding boundary. Steps retain configured identity references and original session references, preserving removal and retained-session reads in separate contexts. Reproduction and verification remain deterministic Python processes.

For dataset evaluation, construct `FormalRunExecutor(run_config, settings, scenario_id=case_id, ground_truth_path=Path("evaluation/ground_truth.json"), ...)`. After runtime and final summary end, evaluation reads answers, matches verified assertions/API evidence, and writes `ground_truth_matching.json` beside the report. It yields Finding-to-bug mappings, TP/FP/missed bugs, Precision/Recall and task classifications. Answers never enter runtime prompts, Shared State or Jev; the post-run reader checks terminal status, finish time and application version.

Evidenced defect checks count as Tester success while application assertions remain FAIL. Missing assertions remain incomplete; browser failures remain execution failures. Unmatched confirmed reports include false positives in controls. Reproduction success uses findings; the attempt rate is separate. Failed/interrupted evaluation records retain quality denominators. Verification checks the current Finding while preserving earlier recorded defects, avoiding truncation of multi-bug paths at the wrong assertion.

Validation covers all 24 conversions without answer loading, the eight previously blocked cases through fake-provider execution/reproduction/verification/matching, repeated submits with switches on/off, separate sessions for one identity, false positives, incomplete checks, execution failures and post-run access. Stale Task/Member responses are discarded to fix the observed unseeded rendering race. Positive tracing tests use a fully local recording stub. No real model calls, Baseline, optimization, commit or push were performed. Scenario definitions, splits, models/providers, scheduler and concurrency remain fixed.

Final checks: **105 tests passed**, Ruff passed, and strict mypy passed for **40 source files**. All 24 readiness annotations are ready; every original expected defect, role, path and enabled-bug combination is preserved. Ready for Baseline.
