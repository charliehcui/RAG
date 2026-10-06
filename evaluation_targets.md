# Evaluation Targets

状态：仅定义指标，未运行 Baseline / Evaluation。所有实测 Baseline 为 **TBD**；上一轮 Unit/Fake Tests 和接口 Smoke 的数字不作为 Baseline。本文件供后续 Baseline、Optimization、Regression 和 Final 使用。本轮不修改代码、架构、模型配置或运行框架。

## 1. Evaluation Principles

- **Fixed Model + Provider**：Main 固定 `deepseek/deepseek-v4-flash` / `streamlake/fp8`；Tester 固定 `z-ai/glm-5.3-flash` / `relace`。Backup 仅人工切换，切换后属于新实验系列，不与原系列混合。
- **Fixed Architecture**：Main Manager + 默认 3 路并发同类 Tester Workers + SQLite Single Source of Truth。保留 Jev `typesafe/jev-1.13`、Playwright 与确定性 Reproduction/Verification。默认 Optional Visual 关闭；若单独评测它，配置与预算必须在对照组一致。
- **Same Scenario / Ground Truth**：同一 Scenario 的 Goal、必需检查、Expected Behaviors、角色/数据引用、Scope、Demo/浏览器版本、启用 Bugs、初始数据、Reset、预算和可配置模型参数固定。Ground Truth 只在 Run 结束后提供给评测，不进入 Agent Prompt。场景只启用该 Scope 内待测的 Bugs，避免把不可达 Bug 放进 Recall 分母。
- **Quality before latency**：减少调用或耗时必须同时满足 Hard Guardrails。不能通过少测目标、少执行断言、跳过复现/验证、删除失败样本或拆小 Task 来制造改善。
- **Local results first**：SQLite、`report.json`、`summary.md`、Evidence 与项目现有 Evaluation 结果是正式来源；LangSmith 只观察少量 Run。人工检查使用相同的小型检查单，不使用 LLM Judge。
- **Fair comparison**：逐 Scenario 对照，记录样本数、分子/分母与失败原因。调用数是实际 Provider 请求，包括已发送的失败请求，不是 `Agent.run()` 次数。Tracing ON/OFF 的 Run 不混合比较 Latency。Model、Provider、场景或计量口径改变时，单独建立新系列。

### 统一计量口径

| Term | Definition |
|---|---|
| Run | 一次完整 Scenario 执行，包含 Main Planning、Workers、必要的复现/验证和 Main Final Summary；已计划启动但失败/中断的 Run 仍进入 E2E 分母。 |
| Attempted Tester Tasks (`A`) | SQLite `tasks.started_at` 非空的 Task，表示已经 claim/开始执行。包含启动失败、FAILED、STOPPED 和结果 UNKNOWN；没有开始的目标由 Planning/E2E 检查单兜底，不能从质量要求中消失。 |
| Successful Tester Task | 完成该 Task 必需的有效检查，正确记录应用结果，必要时上报有依据的 Finding，且没有因执行错误留下未完成目标。使用固定检查单、断言、History/Evidence 做运行后核对。 |
| Application verdict | 现有 `success_status=PASS/FAIL/UNKNOWN` 表示应用目标断言结果。正确发现已启用 Bug 时，应用 verdict 可以是 FAIL，但 Tester 正确完成了测试；不能把应用 PASS Rate 当成 Agent Task Success Rate。 |
| Confirmed predictions (`P`) / Ground Truth (`G`) | `P` 为去重后的 `CONFIRMED_BUG` claims；同一根因/角色/路径的重复 Finding 合并。`G` 为该 Scenario 预先确定的启用 Bug 集合。运行后根据 Expected/Actual、路径、角色与证据做一对一匹配；`TP` 是匹配正确的 claims。NEEDS_CONFIRMATION 等状态不计作 TP。 |
| LLM vs Jev requests | `LLM_CALL` 中 Main + Tester + Optional Visual 是 Total LLM Requests；`JEV_CALL` 独立统计。Jev 不再加进 Total LLM Requests，避免双计数。 |
| Tokens / Cost | Agent 层按所属 `LLM_CALL` 计量；Overall 包含 Main、Tester、Jev、Optional Visual，且每次请求只计一次。使用 Provider 返回的 usage/费用，单位 tokens / USD；缺失不是 0，报告 N/A 或已知部分及缺失原因。 |
| TBD / N/A | TBD：尚未建立 Baseline。N/A：分母为 0、没有触发该阶段、数据缺失或样本不满足报告条件。零请求是有效 0，零次 Replan 的 Unnecessary Replan Rate 是 N/A。 |

跨 Run 的 Rate 用 `Σ分子 / Σ分母`，不直接平均不同分母的百分比。Task 平均数同时报告 Task 数及目标粒度；若规划粒度变化，不能仅凭 Requests/Task 下降宣称改善。

### 当前数据能力与缺口（只记录，不在本轮修复）

| Metric area | Current data / limitation |
|---|---|
| Planning / Task Success / E2E Success | Tasks、断言、History 与报告已保存；缺少统一 Scenario 必需目标检查单及评测判定标签。`TASK_CREATED` 只保存部分初始计划字段，运行后的 Tasks 可被 Replan 改写，不能假定它是完整初始 Plan 快照。需要人工核对；无法追溯的项写 N/A。 |
| Summary Faithfulness / Unnecessary Replan | Summary、结构化事实和 Replan reason 已存在；没有事实一致性或必要性评分。使用人工检查单；代码触发了 Replan 不等于它一定必要。缺少完整决策前快照时不猜测。 |
| Main phase wall time | Main Planning/Replan 在选定 LangSmith Trace 中有完整阶段耗时；本地 `llm_by_phase.latency_ms` 是模型请求耗时之和，不含全部 Tools/间隔，不能替代阶段 Wall-clock。Final Summary 成功时有本地 elapsed latency；失败事件的 0/缺失不是有效测量。 |
| Tester / parallelism | `tasks.started_at`、`TASK_STARTED`、`TASK_FINISHED`、`TESTER_FAILED` 和配置容量可推导 Task 耗时及并发利用率，但现有报告未自动输出所有指标。记录缺失/取消未闭合时写 N/A，不靠累计 budget runtime 猜测。 |
| Bug Precision / Recall | 存在 `GroundTruthComparison` 接口，但 `FormalRunExecutor` 当前没有传入 Ground Truth 或 Finding→Bug 映射。现有 Recall 因此可能 N/A；`false_positive_rate` 实际是误报 claims/confirmed findings，不是 `FP/(FP+TN)`，也不直接当作去重后的 Bug Precision。 |
| Reproduction Success | 当前 `MetricsCalculator.reproduction_success_rate` 是成功 replay 尝试数/尝试数。本文件要求 Finding 级稳定复现率，不能复用同名值。可用 REPRODUCTION_ATTEMPT、状态/稳定步骤推导部分结果；Reset 在事件写入前失败等情况缺少完整开始记录，必须标注，不能默默排除失败。 |
| Usage / cost completeness | 真实 Main/Tester 的每请求 usage、phase、task_id、费用和 cost_known 已记录；Fake 计量不是正式请求账本。Jev 缺 usage 时默认 0 且无完整性标记；Visual 的 LLM event 没有完整费用字段；失败请求的账单/usage 也可能缺失。Budget 的混合总计与 LLM events 不能相加，否则双计。完整 Cost/Token 指标须核对可用记录，缺失标 N/A/partial。 |
| Failed evaluation records | 现有 EvaluationRunner 对失败/中断 Run 的 `result.json.metrics` 置空；已产生的耗时、请求与费用仍可能存在 SQLite/report 中。不能只统计成功 Run，也不能把失败的空 metrics 当作零消耗。 |

以上是测量/判定能力的限制，不是本轮架构变更事项。未具备正式口径的数据保留 TBD/N/A，不填写推测值。

## 2. Main Agent Targets

归属：Planning、Delegation、Replanning、Final Summary。Delegation 的有效性纳入 Planning Success，不再增加一套同义指标。

| Metric | Definition | Baseline | Target | Priority |
|---|---|---|---|---|
| Planning Success Rate | 覆盖全部 Scenario 必需目标、角色/数据引用有效且依赖可执行的初始 Plans / 所有要求 Planning 的 Runs。规划失败计未成功；看必需目标覆盖，不看生成 Task 数是否多。 | TBD | 覆盖所有必需目标；No Quality Regression | Critical |
| Final Summary Faithfulness | 无虚构 Bug、状态提升、错误数字/引用且交代必需结果的 Summaries / 所有要求最终 Summary 的 Runs。缺失/截断不算成功。人工对照 Structured Facts。 | TBD | 无虚构、状态篡改、错误数字或无效引用；No Quality Regression | Critical |
| Unnecessary Replan Rate | 没有新阻塞/相关高风险事实、已有状态足够继续却触发的 Replan 次数 / 已触发 Replan 总数。按同一人工规则审阅；无 Replan 时 N/A。 | TBD | 趋近 0；不能以跳过必要 Replan 降低它 | Important |
| Main Planning Latency | Planning 阶段 `Agent.run` 结束时间 − 开始时间，包含模型与 Tool loop，单位 s/Run。 | TBD | Improve from Baseline without Quality Regression | Important |
| Main Replan Latency | 每次已触发 Replan 的结束时间 − 开始时间，单位 s/调用；附次数与各次耗时，无调用时 N/A。 | TBD | Improve from Baseline without Quality Regression | Secondary |
| Main Final Summary Latency | Summary 阶段结束时间 − 开始时间，单位 s/Run；只计该阶段，不能用 0 代替失败耗时。 | TBD | Improve from Baseline without Quality Regression | Important |
| Main LLM Requests / Run | `count(LLM_CALL where agent=main)`；Planning + Replan + Final Summary，分 phase 同时列出。 | TBD | 减少不必要请求；正常 Final Summary 保持 1 次 | Important |
| Main Input Tokens / Run | `Σinput_tokens(LLM_CALL where agent=main)`；phase 分项保留。 | TBD | Improve from Baseline without Quality Regression | Important |
| Main Output Tokens / Run | `Σoutput_tokens(LLM_CALL where agent=main)`；不因截断 Summary 制造节省。 | TBD | Improve from Baseline without Quality Regression | Important |
| Main Cost / Run | `ΣProvider cost(LLM_CALL where agent=main)`；包含失败请求的已知费用，完整账单缺失时标 N/A/partial。 | TBD | Improve from Baseline without Quality Regression | Important |

Summary 的任务/Finding/Evidence 对照结构化事实；提及耗时、Token、成本时，对照 Main 实际收到的调用前事实，即 `testing_phase_cost_and_performance`，不误拿含 Summary 调用的最终总计去判错。完整性检查涵盖测了什么、哪些完成、问题/复现/验证、失败/不确定项与必要引用。

## 3. Tester Agent Targets

归属：**Per Task + Aggregate Testers**。Bug Precision、Bug Recall、Reproduction Success Rate 只在 Overall 定义；可按 Finding 的 `task_id` / `first_seen_by` 追溯 Worker 来源，不另算不同口径的 Tester 指标。

| Metric | Definition | Baseline | Target | Priority |
|---|---|---|---|---|
| Task Success Rate | 经固定目标检查单确认成功的 Tester Tasks / `count(A)`。正常路径正确通过检查、Bug 路径正确检查并上报均可成功；无有效断言、遗漏目标或未解释 UNKNOWN 不算成功。 | TBD | No Quality Regression from Baseline | Critical |
| Tester Latency / Task | 每个 Task 的 terminal event 时间 − `tasks.started_at`，单位 s；terminal 为 TASK_FINISHED/TESTER_FAILED，包含 Browser 启动和清理，不含 Pending 排队、后续系统 Replay。缺终点时 N/A。 | TBD | Improve from Baseline without Quality Regression | Important |
| Average Tester Latency / Task | `Σ已完整记录 Task latency / 完整记录的 attempted Task 数`；附 timing 缺失数及失败任务，不能删去慢任务。 | TBD | Improve from Baseline without Quality Regression | Important |
| Longest Tester Latency / Run | `max(Task latency)`；注明对应 Task。非空且 timing 完整才报告准确最大值；不把它等同于完整 Run critical path。 | TBD | Improve from Baseline without Quality Regression | Important |
| Tester LLM Requests / Task | Per Task：该 task_id 的 Tester LLM 请求数；Aggregate：`Total Tester LLM Requests / count(A)`，包括已发送失败请求。 | TBD | Reduce from Baseline without Quality Regression | Critical |
| Tester LLM Requests / Run | `count(LLM_CALL where agent=tester)`；排除 Main、Jev 和 Optional Visual。 | TBD | Improve from Baseline without Quality Regression | Important |
| Jev Requests / Task | Per Task：该 task_id 的 JEV_CALL 数；Aggregate：`Total Jev Requests / count(A)`。 | TBD | 与安全动作委派效果一同解释；不要求盲目下降 | Important |
| Jev Requests / Run | `count(JEV_CALL)`；Jev 是独立请求分类，不与 Tester LLM 双计。 | TBD | 允许合理增加，但整体 Latency/Token/Cost 必须改善且质量不退化 | Secondary |
| Tester Input Tokens / Task | Per Task：该 Task 的 Tester LLM input tokens；Aggregate：`ΣTester LLM input tokens / count(A)`，不含 Jev/Visual。 | TBD | Improve from Baseline without Quality Regression | Important |
| Tester Output Tokens / Task | Per Task：该 Task 的 Tester LLM output tokens；Aggregate：`ΣTester LLM output tokens / count(A)`，不含 Jev/Visual。 | TBD | Improve from Baseline without Quality Regression | Important |
| Tester Cost / Task | Per Task：该 Task 的 Tester LLM Provider cost；Aggregate：`ΣTester LLM cost / count(A)`，不含 Jev/Visual，转移的费用在 Overall Total Cost 中核对。 | TBD | Improve from Baseline without Quality Regression | Important |

在单个 Run 和同条件多个 Run 中均保留 Task 原始值、Attempted Task 数、聚合请求/Token/费用及缺失记录。**三个并发 Tester 的 latency 之和不是 Overall latency**；最长单 Task 也不是多批次任务、Main Replan 和 Replay 的完整 critical path。

## 4. Overall System Targets

归属：最终完整运行质量、真实 Wall-clock、并发效果以及所有模型/决定请求的总开销。Longest Tester Latency 引用第 3 节，不再定义第二个版本。

| Metric | Definition | Baseline | Target | Priority |
|---|---|---|---|---|
| End-to-End Task Success Rate | 满足全部 Scenario 必需目标、正确 Bug/非 Bug 结论、必要证据/稳定复现/验证且有忠实 Summary 的完整 Runs / 全部纳入该场景的 Runs（含失败/中断）。一次 Run 是一次用户 E2E Task；不是 SQL 子任务 PASS 比例或仅 `run_status=COMPLETED`。 | TBD | No Quality Regression from Baseline | Critical |
| Bug Precision | `TP / count(P)`；P 是去重后的最终 confirmed claims。无 claims 时 N/A；无 Bug 的控制场景同时要求误报 claims 为 0。 | TBD | No Quality Regression from Baseline | Critical |
| Bug Recall | `已被正确 Confirm 的唯一 Ground Truth Bug 数 / count(G)`；G 为空时 N/A。未确认的候选 Finding 不算 Recall，重复发现不增加分子。 | TBD | No Quality Regression from Baseline | Critical |
| Reproduction Success Rate | `达到固定稳定复现条件的唯一 Findings / 已启动复现的唯一 Findings`。正常默认需至少两次匹配 replay；Verification 不替代 Reproduction，最终 CONFIRMED/CLOSED 也不能抹掉既有复现事实。 | TBD | No Quality Regression from Baseline | Critical |
| Total Wall-clock Time | `runs.finished_at − runs.started_at`，单位 s；含 Main Planning/Replan、所有 Worker 等待/执行、Replay/Verification、Summary。不求各 Agent latency 的和。现有边界不含 Run 建立前 tracing 初始化、末尾 LangSmith Flush 和最终文件输出之后的 CLI 时间。 | TBD | Improve from Baseline without Quality Regression | Critical |
| P50 / Median Runtime | 同 Scenario、同配置的独立 Reset Runs 的 `median(Total Wall-clock)`；本项目仅在至少 3 次重复时报告描述性 median 并附 n，n<3 时 N/A。此条件不代表统计显著性。 | TBD | Improve from Baseline without Quality Regression | Important |
| Peak Concurrent Testers | `max_t N_active(t)`；TASK_STARTED 加入，TASK_FINISHED/TESTER_FAILED 移除，与现有报告一致。 | TBD | 有至少 3 个独立 ready Tasks 时发挥默认容量；不超过配置容量 | Important |
| Concurrency Utilization | `Σactive interval duration / (C × exploration window)`；C 为 configured_tester_capacity，active interval 为 TASK_STARTED→terminal，window 为首次 Task claim→最后 terminal。只评估探索阶段，Main 初始 Planning、后续 Replay/Summary 不进窗口；空/缺失窗口 N/A。 | TBD | Improve from Baseline without Quality Regression；仅比较可并行工作量相同的场景 | Important |
| Parallel Speedup | 配对 Run 的 `1-Tester Wall-clock / 3-Tester Wall-clock`，同 Goal/工作量/模型/预算/数据/Tracing 状态，多个配对先各算比值再报告 median；未做配对实验时 N/A。 | TBD | 在可并行场景改善，且满足 Hard Guardrails；Baseline 不强制立即做此实验 | Secondary |
| Total LLM Requests | `Main LLM Requests + Tester LLM Requests + Optional Visual LLM Requests`；包含所有已发送失败请求，Jev 另计。 | TBD | Reduce from Baseline without Quality Regression；Main Summary 不能被省略 | Critical |
| Total Jev Requests | `ΣJev Requests`，含产生错误结果的请求。 | TBD | 与 LLM 委派、Latency 和总费用一起解释；不以最小值为唯一目标 | Secondary |
| Total Input Tokens | `ΣProvider input tokens(Main + Tester + Jev + Visual)`；不用只含 LLM 的字段冒充全模型总计，缺 usage 标 N/A/partial。 | TBD | Improve from Baseline without Quality Regression | Important |
| Total Output Tokens | `ΣProvider output tokens(Main + Tester + Jev + Visual)`；包含 Provider 报告的 reasoning tokens，不重复计入。 | TBD | Improve from Baseline without Quality Regression | Important |
| Total Cost / Run | `Σ实际 billed cost(Main + Tester + Jev + Visual)`，单位 USD/Run；检查每种来源后只加一次，不能把 budget 总计再与 events 相加。缺账单/费用可用性时标 N/A/partial。 | TBD | Improve from Baseline without Quality Regression | Important |

Concurrency Utilization 是执行窗口占用率，等待 LLM 的 Task 也算 active，不代表 CPU 利用率或必然加速。低利用率可能来自任务依赖/工作量不足；不能因此放松数据隔离。计量窗口有缺失时保留原始事件并标 N/A。

Reproduction 分母包括已进入复现却因环境/预算失败的 Findings，不包含从未启动的 Observations。其启动/稳定条件若无法完整追溯，指标写 N/A，不能从最终状态猜出成功率。现有 replay attempt 成功率可作为诊断值，并明确标为 **Reproduction Attempt Match Rate**，不与本表混名。

没有足够样本时不报告 P95，本阶段 P95 为 N/A。质量分子/分母及成本记录包含失败 Run；成功完成 Run 的 Latency 可单独展示，但必须同时保留全部 Run 耗时及失败情况，不能用提前失败制造“更快”。

## 5. Hard Guardrails

| Guardrail | Acceptance rule |
|---|---|
| Tester Task Success / E2E Success | 固定目标检查单下不发生已验证的质量回归；失败、遗漏、无有效断言或无必要 Summary 不能被当作成功。 |
| Bug Precision | 已确认 claims 必须有正确运行后匹配与有效 Evidence，不以降低误报标准换速度；无 Bug 控制场景不得凭空报告 confirmed bug。 |
| Bug Recall | 不通过不测目标、过滤困难样本、改变启用 Bugs/Scope 或重复发现同一 Bug 提高比率。已能正确发现的固定 Bug 不能无依据地丢失。 |
| Reproduction Success | 固定 replay 预算、稳定条件与 Reset；不把未复现/单次匹配当成稳定复现，也不静默删除复现失败的 Finding。 |
| Main Planning / Summary | 必需目标保持覆盖；Summary 不得虚构 Bug、提升状态、改变数字或引用不存在的 Evidence。 |

没有 Baseline 时不指定 95%/99% 等绝对门槛。Baseline 后以同场景逐例结果及相同公式的聚合值为依据；差异先标记待复核，未能证明质量守住时不宣称 Optimization 成功。允许下降多少的统计容差不能在看完优化结果后临时决定。

## 6. Structural Latency Optimization Targets

本节只是后续对照方向，**这一轮不实现 LLM per Subgoal，也不调整 Jev/Playwright 或 Scheduler**。

| Focus | Future comparison |
|---|---|
| Tester LLM Requests / Task | 首要关注 `LLM per Action → LLM per Subgoal` 是否降低每个同等目标 Task 的真实 Tester 请求数；保留有效断言和必要 Finding。 |
| Total LLM Requests | 同时查看 Main Planning/Replan、Tester、Visual 和固定一次 Main Summary，防止只把请求移到别处。 |
| Jev delegation | 对照 Jev 请求、已验证的候选执行与 BROWSER_ACTION History，解释更多合法页面动作由 Jev + Playwright 执行的效果；不是每个 Browser Action 都需要 Jev，不要求 Jev 次数单调增加或下降。 |
| Wall-clock / parallelism | 比较真实 Run Wall-clock、最长 Task 和 Concurrency Utilization。1 vs 3 的 Parallel Speedup 留作后续受控实验，不根据三路 latency 相加估计。 |
| Tokens | 对照 Total Input/Output Tokens，并检查 Tester 与 Main 的归属；包含 Jev 的新增消耗。 |
| Cost | 对照完整 billed Total Cost，检查缓存、价格日期和缺失记录；不能把模型/Provider 切换收益归为结构优化。 |

计量优先顺序：先满足第 5 节，再比较 Wall-clock、Tester Requests/Task、Total Requests、Tokens、Cost。无合理绝对值的 Target 统一为 **Improve from Baseline without Quality Regression**。Baseline 前不预填节省比例。

## 7. LangSmith Usage Policy

| Run type | Tracing policy |
|---|---|
| 普通 Unit/Fake tests、批量重复实验、常规 Regression | **OFF**：显式 `LANGSMITH_TRACING=false`。本地 State/report/结果照常保留。 |
| 少量预先选定的正式 Baseline Runs | **ON**：`LANGSMITH_TRACING=true`，仅采集用于阶段定位的代表性 Run。 |
| Optimization 前后关键配对 Runs | 两边相同 Tracing 状态；仅少量关键配对开启，不挑最快结果作为代表。 |
| 异常 Debug Run | 按需要单次开启，定位后恢复 OFF，不自动把整批 Regression 上传。 |
| 专门 Tracing contract tests | 仅本地 Mock SDK/Backend 开启，不能连接生产 Key 或实际上传。 |

当前已有配置开关；但代码默认 `langsmith_tracing=True`，有 Key 时会启用。因此 OFF 是本阶段明确推荐的运行政策，不是已修改后的默认值。本轮不修改 `.env`、Settings 或 Tracing 架构。当前专门 Mock tracing tests 继承环境开关，统一 OFF 可能使要求存在 Span 的断言不成立，需与普通测试区分；本轮只指出。

每批开始前人工查看 Usage and Billing，按**剩余额度**决定少量 Trace 名单，给异常 Debug 留余量。可在控制台人工设置低于可用额度的 Usage Limit；本轮不访问账户或修改其设置，也不新增采样/额度 Router。

截至 2026-10-06，官方 Developer Plan 列出每月 5,000 base traces，base retention 为 14 天，超出免费量可能产生额外费用；账户实际额度以其 Usage/Billing 为准。本项目继续只用短期基础 tracing，不配置 extended retention/feedback/evaluator。一个 Trace 可包含多个步骤，不能把本项目的 Span 数直接当成月度收费 Trace 数；同时仍应关注事件量与数据量限制。[官方定价](https://www.langchain.com/pricing)

官方支持在控制台设置 Usage Limits；限制是近似执行，并非严格保证永不超额，需结合显式 OFF 和人工查看余量使用。[官方 Usage Limits](https://docs.langchain.com/langsmith/administration-overview#usage-limits)

不创建 LangSmith Dataset、Evaluator、LLM-as-a-Judge 或第二套 Evaluation System；不把 LangSmith 当正式指标仓库，不依赖它保存长期结果。只上传既有安全 Metadata，继续隐藏 Prompt、Tool 参数/结果、页面内容、凭据及敏感测试值。

后续成果可按 `evaluation/baseline/`、`optimization/`、`regression/`、`final/` 分阶段保存或沿用现有 EvaluationRunner 输出结构；保留 Run ID、配置、State、报告、Summary 与 Evidence 的对应关系。本文件是唯一 Targets 文档，不再建同义 targets.md；这一轮不创建这些目录或 Run 文件。

## 8. Resume Metrics Candidates

只保留以下 5 个候选，待真实测量后再填写数字；均同时说明 Scenario、样本数和限制。

| Candidate | What it demonstrates |
|---|---|
| End-to-End Task Success Rate | 完整自主测试目标到事实报告/用户总结的成功率，体现 Autonomous E2E Testing。 |
| Bug Recall at measured Bug Precision | 说明在有限、明确 Ground Truth 的缺陷场景中检测到了多少正确 Bug，不泛化为任意网站准确率。 |
| Finding-level Reproduction Success Rate | 体现 Evidence Collection、稳定 Bug Reproduction 与确定性 Verification。 |
| 1-vs-3 Tester Parallel Speedup | 体现 Multi-Agent Orchestration、Parallel Execution 与 BrowserContext 隔离；只有受控配对实验完成后才使用。 |
| Tester LLM Requests / Task reduction | 体现 Subgoal Planning 与 Jev/Playwright Tool Delegation 的实际效率；同时引用 Whole-run Latency/Cost 与质量守护结果。 |

不把 Mock/Smoke 数字写成正式质量或优化收益，不编造当前成功率、速度提升、Token 节省或费用下降。
