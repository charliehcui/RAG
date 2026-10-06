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
- **1-vs-3 comparisons:** D03, D13, D15, D16, H07, H08; each provides three meaningful independent goal groups and separate write targets/browser contexts. Keep data, reset, models/providers, tracing and other budgets identical. B5 limitations still apply.
- **Quick development subset:** D01, D04, D07, D10, D14, D16; clean checks, defects, multi-step browser/Jev work, ordered handoff and parallel workflows.
- **Coupled workflows:** D09, D12, H04, H06 require synchronized live identities; exclude speedup comparisons and reject one-Tester execution.

Trusted harness setup: explicitly set all six `DEMO_BUG_B1`–`DEMO_BUG_B6` switches, including disabled values; restart when they change. `/test/reset` resets data/sessions, not switches. Run cases serially at application level. Configure account secrets through `DEMO_ADMIN_PASSWORD`, `DEMO_MEMBER_PASSWORD`, `DEMO_MEMBER2_PASSWORD` using the demo defaults, plus a chosen `DEMO_NEW_MEMBER_PASSWORD` for D02. No credentials enter Agent-visible JSON values. Task-status API changes and the optional canvas visual fallback are outside this browser dataset.

Pass only `load_run_config(...)` from `web_testing_system.evaluation.scenarios` into the existing formal entry point. It projects safe goal/behavior/role/data/scope/reset/budget fields and rejects answer markers; raw cases, switches and notes never enter prompts, Shared State or Jev. `read_ground_truth(...)` checks persisted terminal status, `finished_at` and application version; it is not an Agent tool. Current Main/Tester tools do not expose filesystem/shell access. This is an input/tool boundary, not a filesystem lock. Keep **`LANGSMITH_TRACING=false`**; nothing was uploaded.

**Formal readiness blockers:** D08, D09, D12, D13, D15, H04, H06, H08. B5 needs guaranteed same-operation timing and request-identity evidence unavailable in the current gateway. B6 needs cross-identity removal during replay, which single-task replay cannot reconstruct. Also, the existing metrics lack post-run answer mapping, treat defect assertions as task FAIL, and report reproduction per attempt. Full matching/metric rules are in `ground_truth.json`; do not drop blocked cases or change answers to manufacture results. Architecture and demo code remain untouched.

An unseeded Members rendering race was observed with overlapping loads. These cases wait for ordinary view loading to settle; credible unexpected reports require independent review rather than automatic false-positive scoring.

Validation: all 24 cases and references checked; **44 valid RunConfig projections**, four rejected single-Tester coupled inputs, and active-run/version/answer-hint rejection; **9 existing tests + 8 focused local browser checks** passed; Ruff and strict mypy passed. Controlled timing in diagnostic checks does not establish formal readiness. No Baseline, formal evaluation, optimization, model calls, commit or push was performed.
