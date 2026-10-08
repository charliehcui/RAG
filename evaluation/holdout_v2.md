# Holdout v2 final check — 2026-10-08

Holdout retains H01–H08 and now declares 66 stable, independently scored Check IDs. Development v2 remains D01–D08, D12 and D13 with 74 checks. The Development scenario/answer records, scoring code, global evaluation rules, Agent code, models/providers and budgets were not changed by this Holdout finalization.

| Case | Purpose | Goals | Checks | Enabled defects |
| --- | --- | ---: | ---: | --- |
| H01 | Unassigned member2 login, refresh, logout and supplied invalid-username control | 1 | 7 | None |
| H02 | Admin invitation/edit/removal followed by the invitee's first fresh login | 2 | 9 | None |
| H03 | Legitimate owner cleanup contrasted with joined-member deletion and project/task preservation | 1 | 8 | B2 |
| H04 | Private-project invitation before member2 login, followed by revocation in that retained session | 2 | 7 | B6 |
| H05 | Whitespace Edit validation and unchanged immediate/persisted private-project name | 1 | 3 | B3 |
| H06 | Independent pending creation alongside a synchronized existing-member offboarding branch | 3 | 9 | B5, B6 |
| H07 | Independent private-task cleanup and joined-project deletion on isolated targets | 2 | 11 | B1, B2 |
| H08 | Parallel role/project isolation across owner rename, same-operation submission and joined-task edit | 3 | 12 | B4, B5 |

No Case was deleted or merged. H07's unrelated team-members lifecycle was removed because H02 already covers that lifecycle, while H07 retains both original seeded defects and their isolated task/permission targets. H07 now has two independent branches and is excluded from three-branch speed comparisons. H06 and H08 retain intentional coordination/isolation coverage rather than being split into unrelated browser sessions.

H03/H07 now explicitly supply the joined Seed Task ID and title. Every goal declares its Check IDs, acting identity and available data. Ground Truth mirrors all required checks and identifies each defect's actor, object, prerequisites and applicable session order. Pending-operation proof uses repeat_submit and cannot be replaced by an ordinary DOM assertion. B5 evidence guidance reflects the existing sanitized operation/request evidence.

Reference behavior descriptions do not add unlisted tests or inputs. Stable Check IDs and dependencies use the existing v2 scoring contract: Check Completion is recorded separately; Task Success and E2E Success remain strict; recovered errors do not permanently fail a completed Task; unresolved blocking errors remain failures. Reproduction still requires two matching fresh attempts within three, followed by independent verification.

Validation: 59 unit tests passed across Holdout schema/scoring, existing scoring and evaluation matching. Three relevant local fake-provider/Chromium integration cases passed (H04/H06/H08). H06's first local run encountered a replay PAGE_TIMEOUT; only that failed test was repeated and passed. No Runtime, Agent, Provider or budget change was used to address that timeout. Final H07 reference cleanup passed its two affected local checks. Ruff and mypy passed. No real LLM was dispatched for Holdout.

The final data/scoring hashes are recorded in evaluation_v2_freeze.json. From this freeze onward, Scenario, Ground Truth and scoring stay unchanged. Measurement reports and benchmark_history.md may record results without changing this evaluation contract. Tester Optimization remains paused.

The first Development Baseline attempt was paused by user instruction during D01, before D02 started. Its artifacts are retained with pause provenance; it is not silently treated as an Agent Quality Failure or discarded for a better score. Its timing/validity requires explicit handling before resuming the authorized Development-only Baseline. Holdout is not part of that Baseline.
