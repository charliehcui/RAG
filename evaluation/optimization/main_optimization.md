# Main Optimization

## Final Stabilization 最终结论 — 2026-10-10

- Problem：需要确认下游集中修复没有回退既有工作流/依赖/预算继承。
- Why：后续业务失败不能通过改 Main 或压缩/扩大执行预算掩盖。
- Change：Main 源码保持冻结，仅核对当前真实 state.db、报告和 trace。
- Before：既有 Main 规划/预算检查通过，后续失败归属 Tester/Runtime 接口。
- After：最终 D12 必要任务启动 2/2、依赖/死锁/关闭重定向/预算缩减 0、每个 Task 90，Main Requests 2（Planning 1、Summary 1）、Replan 0。下游严格 2/2 Tasks、7/7 Checks、FP 0、E2E PASS；415 Span 树完整，保护文件哈希未变。
- Final Decision：Main 不再优化；FINAL_BENCHMARK_READY = YES，整体代码冻结并停止。最新测量仅 D12，不与旧 D03/D13 混合分数；最终 Benchmark/Holdout 仍等待用户单独授权，benchmark_history.md 不更新。

## Final Stabilization：业务操作描述与执行接口分离 — 2026-10-09

- Problem：D12 Main 的 required_operations 是 attempt-project-1-delete-or-record-protection 等业务步骤，Tester 接口若只识别字面 delete 会遗漏已明确分配的权限检查。
- Why：Main 负责工作流，其业务步骤不是 Runtime opcode 或浏览器预算。纠正接口不应把 Main 退回页面规划。
- Change：Main 未修改。下游以实际分配 Check ID、既有可信规格及角色确定必要权限操作，仍由 Tester 生成高层计划、Runtime 绑定和执行；不解析业务描述猜步骤。
- Before：005 两个必要任务均启动、预算 90、依赖/死锁/缩减 0，Main Requests 1、Replan 0；业务失败来自后续计划/断言接口。
- After：新 Main→Tester→Runtime 正常/缺陷双会话本地集成通过，所有步骤与权限职责保持；正式代码及预算冻结。
- Final Decision：Main 继续停止优化，仅修正确性接口。不修改 benchmark_history.md。

## Final Stabilization：继续保留 Main 冻结状态 — 2026-10-09

- Problem：后续检查仍有业务失败，需要确认并非任务依赖或 Main 缩减执行预算复发。
- Why：Runtime / Tester 失败不能通过重新规划架构或增加 Main 调用掩盖。
- Change：Main 源码、依赖编译、预算配置均未修改；只读整理最新已有 state.db 与报告。
- Before：此前必要工作流均启动，Task Budget 继承固定配置 90，Main Replan 0。
- After：最新 D03/D12/D13 必要工作流启动 8/8、无无效依赖/死锁、八个 Task 均继承 90、Main Replan 0、Requests 4。三个中止来自 Runtime/Tester 接口阻塞及原时长上限，没有 Main 导致的步数停止。
- Final Decision：Main 保持冻结，不重新优化。后续修复只限 Tester/Runtime 正确性；正式 Benchmark/Holdout 未运行，benchmark_history.md 未修改。

Final Stabilization 继续（2026-10-09）：沿用已验证的 Workflow / Check Dependency Compilation 与固定 execution budget inheritance。本轮目前没有 Main 源码修改；后续代表性验证检查 required task startup、dependency/deadlock、重复 Task 和每 Task 固定预算。上一轮 8/8 Tasks 启动、预算全部 90、Main Replan 0 的记录保留，不以其他层失败调整 Main。

Final Stabilization（2026-10-09）：Main 源码、Workflow/Dependency Compilation 与确定性 Execution Budget Inheritance 保持原冻结版本。完整本地回归包含启动、依赖、预算、并行和关闭任务保护；此次只在既有 Replay 接口中从 Check 依赖恢复实时参与者，未重新引入整 Task 等待边。唯一 D03/D12/D13 Readiness Checkpoint 验证必要工作流启动 8/8，全部 Task budget = 90，无无效/循环依赖、死锁、关闭重定向、重复 Task 或 Main 分配导致的 step stop。Main Requests 6（Planning 3、Summary 3）、Main Replan 0，完整实时信号顺序正确。Main 无本轮新增待修根因，继续冻结。系统总体仍为 FINAL_BENCHMARK_READY = NO：剩余候选重复、断言接口及错误测试 Oracle 属于 Runtime / Tester，详见 [Final Readiness Summary](final_readiness_summary.md)。不开始新的 Main 优化或正式 Benchmark，不修改 benchmark_history.md。

## 连续工作流与依赖编译（Workflow Planning / Dependency Compilation）— 2026-10-08

- Problem：D12 的成员会话等待管理员撤权，Main 却将管理员依赖于整个成员 Task 完成；触发 LIVE_PARTICIPANT_DEPENDS_ON_OBSERVER。既有 Replan 将原来的两个连续角色工作流拆成准备、管理员操作、成员验证、保留性检查四个新 Task，并重复分配 Check，最终留下四个未启动 Task。历史接口也允许模型逐次猜 Task ID/依赖和重规划对象，未知依赖/关闭 Task 只能在工具调用时被拒绝。
- Why：检查之间的必要顺序不等于整个 Task 完成顺序。两个同会话工作流互相需要中间状态时必须保持并行，通过既有进度信号协调。逐 Task 写入缺少整体范围与依赖预检；Replan 沿用旧会话历史，缺少精确的可修改 Task 集和已完成检查。
- Change：只修改 Main Agent。对现有 required_checks 的 goal_id/身份与既有 Scenario 投影编译工作流契约；一次提交全部可规划工作流，程序绑定原始身份、Input Reference、Check ID 与范围。连续角色工作流保持一个 Task；跨工作流单向前置条件按拓扑顺序创建，互相需要中间状态的工作流并行，外部前置条件对整个并行组一致生效。完整计划预检通过前不写 Task；传统接口补齐循环与不可完成依赖拒绝。每次 Replan 读取当前状态和原有严格评分，仅处理未完成部分；保护运行中/已完成 Task、历史证据与原步数预算，不 redirect 关闭 Task。已关闭实时会话不能用新的准备 Task 冒充恢复。Main 不执行页面操作，不设计 Tester 步骤；其余组件和冻结参数不改。成功计划提交后结束规划调用，不增加 Done 模型轮次；必要校验修复受原有上限约束。
- Before：冻结 Baseline v2 的 D02/D13/D12 Main Requests 分别 5/5/5，合计 15，平均 5/Case；D12 原图有一个整 Task 依赖冲突，管理员未启动。最新 D12 旧 Main 检查为 Main Requests 8、Main Replan 1，两个原 Task 后增加四个 Task，四个未启动；保留原记录，只读分析，未重新测量。历史未知依赖/关闭重定向错误的精确次数 N/A，不猜测。
- After：38 个必要 local/unit 测试分批通过；Ruff 和 mypy（41 源文件）通过。覆盖完整工作流范围、未知/前向/循环/关闭依赖、创建顺序、已完成检查保留、运行中 Task 不变、关闭实时会话不重建、双角色同会话进度交接、新 Main → 冻结 Tester/Runtime → 严格报告，以及现有协作/调度/追踪。冻结后 D02/D13/D12 各一次有效测量：必要工作流创建/启动 6/6，无 Main 导致的未启动 Task；依赖死锁、无效依赖、循环、关闭重定向、重复 Task、Main 计划拒绝均 0。Main Replan 0/3 Case。Main Requests 4（Initial Planning 3、Final Summary 1），同 Case Baseline 为 15；公平分阶段比较为 Initial Planning 从 12 到 3，不能把两个预算阻止的 Summary 当成收益。业务 Task Success 0/6，Check Completion 6/30（20%），E2E 0/3。D12 两个角色真正同时启动，记录 member-session-ready，但后续撤权/删除/保留性信号未完成，完整 live-session 顺序仍未验证。三条 LangSmith Trace 完整，正式 report/evaluation 一致，Provider/API 失败 0，无替换，测量期冻结文件哈希一致。数据见 [Main Checkpoint](../../artifacts/runs/main-workflow-cp-20261008-001/checkpoint_metrics.json) 与 [Planning Review](../../artifacts/runs/main-workflow-cp-20261008-001/main_planning_review.json)。
- Final Decision：本轮完成唯一检查后停止，代码保持冻结，Stop Condition 未完全达到，不宣布 Main Optimization 成功，也不继续修改或真实测试。依赖/启动/任务范围问题在本地和本轮得到验证，但存在真正属于 Main 的回归：新 Main 契约缺少明确的浏览器动作预算语义，LLM 将完整工作流的 step_budget 低估为业务步骤数；D13 从旧 Main 的 90/90/90 变为 10/8/18，三个 Task 均 MAX_TASK_STEPS_REACHED。D02 从 70 变为 45、D12 从 70/70 变为 10/12；后二者实际以 MAX_RUNTIME_REACHED 停止，不能将它们的超时直接归因于步数分配。配置中的预算上限没有改，但 Main 的有效任务分配发生变化，不能宣称“预算行为完全不变”或 Tester/Runtime 无回归。下一步最小建议是明确区分冻结的执行预算与业务步骤/Check 数量，防止 Main 重估并缩减执行配额；需先确认如何继承原预算策略，不通过提高配置上限解决问题。D02/D12 最终 Summary 被既有运行预算阻止，属于未完成汇总，保留失败。Tester 的候选/绑定/计划异常属于现有执行层问题，本轮不改它们。保留严格评分及全部失败，不运行 Baseline/旧检查组合/Holdout/完整 Benchmark，不更新 benchmark_history.md，等待用户确认。

| Case | 必要 Task 创建 / 启动 | Main Requests（Planning / Replan / Summary） | Main 分配步数 | Check Completion | 最终状态 |
| --- | --- | --- | --- | --- | --- |
| D02 | 1/1 | 1（1/0/0；Summary 被预算阻止） | 45 | 0/9 | FAILED：Runtime 上限 |
| D13 | 3/3 | 2（1/0/1） | 10 / 8 / 18 | 5/14 | INTERRUPTED：三个 Task 均达到分配的步数上限 |
| D12 | 2/2 | 1（1/0/0；Summary 被预算阻止） | 10 / 12 | 1/7 | FAILED：双角色均启动，后续 Runtime 上限 |

本轮仅一个集中实现方向（工作流契约、整体提交和依赖编译），未启动第二轮真实检查。此前的 Tester/Runtime 改动保持原始工作区状态与哈希，未在 Main 阶段修改。检查跨澳大利亚本地日期完成（2026-10-08 至 2026-10-09），保留开始时的 run ID。

总体 LLM Requests 17（Main 4、Tester 12、Jev 1），Input/Output Tokens 106,644/265,220，总成本 USD 0.136035795678，Wall-clock 2869.325 秒（47.82 分钟）。D13 检出一个真实 Finding、两次 Replay 均匹配，独立 Verification FAIL / RECORDED_OUTCOME_MISMATCH 表示缺陷仍存在；Precision 1/1，Recall 1/4（D13 两个及 D12 两个启用 Case/Bug 配对）。该回放路径有实测证据，但不能据此扩大宣称整体业务或实时会话无回归。

## 执行预算继承（Execution Budget Inheritance）— 2026-10-09

- Problem：Main 将业务步骤数量当作浏览器执行预算。上一轮 D13 分配 10/8/18，三个 Task 均 MAX_TASK_STEPS_REACHED；原 Baseline 为 90/90/90。D02 分配 45、D12 分配 10/12，后二者实际为运行时间耗尽，不将它们错误归因于步数预算。
- Why：MainTaskPlan 的 step_budget 字段由 LLM 填写，直接进入 Task，并被既有 Runtime BudgetGuard 用作有效上限。业务流程长度与浏览器动作数量不同；仅禁止超过配置最大值仍允许 Main 任意缩减预算。
- Change：保留既有工作流、依赖编译与调度结构。移除 Initial Plan / Replan 输出 Schema 中的 step_budget；传统 create_task 模型 Schema 同样不暴露该字段，旧 Python 调用参数仅兼容接收，程序覆盖为固定配置值。所有新 Task（含必要的恢复 Task）继承 RunConfig.budget.max_browser_steps_per_task，当前为 90，不按照 Check 数或业务步数估计。更新已有 Task 的 Replan 路径不写预算，运行中和已完成 Task 保持原样；已停止 Task 的历史预算和证据同样不变。契约展示只读预算来源和已有预算。既有 Runtime 继续取 Task 预算与配置上限的较小值；本轮没有修改 Runtime、Tester、Jev、Evaluation 数据或系统预算上限。
- Before：D13 10/8/18 且 Main 分配引起的步数停止 3；D12 10/12。上一轮这两个 Case 必要 Task 启动 5/5、依赖死锁/无效依赖均 0、Main Replan 0，Main Requests 3（Planning 2、Summary 1；D12 Summary 被运行预算阻止）。
- After：34 个必要本地测试分两批通过（33+1），Ruff、mypy（41 源文件）和 git diff --check 通过。覆盖模型无法缩减/扩大预算、Initial Plan 的多 Task/实时会话预算继承、非 90 配置继承、已有 Pending/Blocked/Waiting/Completed/Running Task 预算保留、新恢复 Task 使用统一配置、历史已完成检查证据保留、原依赖编译/并行交接及 Main→Tester→Runtime→Report。冻结后只运行一次 D13/D12 检查，各 Case 一次：五个新 Task 均继承 90，MAX_TASK_STEPS_REACHED 为 0；必要任务启动 5/5，依赖死锁/无效依赖/循环/关闭重定向/重复 Task/Main 计划拒绝均为 0。Main Requests 4（Planning 2、Replan 0、Summary 2），平均 Replan 0/Case；同 Case Baseline 为 10。与上一轮比较，Planning 仍为 2，新增的一次 Summary 是 D12 正常完成汇总，不是规划调用增加。D13 Task Success 3/3、Check Completion 14/14、E2E PASS；D12 Task Success 0/2、Check Completion 4/7、E2E FAIL，两个 Task 均达到 Tester Replan Limit，属于有效 Agent Quality Failure，未替换。总体 Task Success 3/5（60%）、Check Completion 18/21（85.71%），Tester Calls 9/5（1.8/Task）。正式 report/evaluation 指标一致，Provider/API 失败 0，测量期间冻结文件哈希一致。详见 [Checkpoint Metrics](../../artifacts/runs/main-budget-cp-20261009-001/checkpoint_metrics.json)、[Budget / Dependency Review](../../artifacts/runs/main-budget-cp-20261009-001/main_budget_review.json) 与 [Validity Review](../../artifacts/runs/main-budget-cp-20261009-001/validity_review.json)。
- Final Decision：停止 Main Optimization。Main 不再将业务步骤当作执行配额，预算继承、依赖和必要 Task 启动目标已验证，没有发现剩余的 Main 预算/规划问题；不宣称整个系统或完整实时业务流程全部通过。D12 的未完成检查为 revoked-tasks、delete-rejected、delete-preservation：Tester 一次 Replan 遗漏 Check ID（CHECK_ID_REQUIRED）；Runtime 两次 PROJECT_BINDING_UNAVAILABLE 无法恢复、无目标 assertion 导致 INVALID_ACTION，以及两次 PROGRESS_SIGNAL_NOT_AVAILABLE。两个实时任务确实并行，member-session-ready（00:52:41 UTC）→ membership-removed（00:55:21 UTC）→ revoked-project 检查（00:55:50 UTC）顺序正确；后续任务访问/删除拒绝/管理员保留性链条未完成，不能把部分交接成功写成完整 Live-session 成功。剩余失败归入 Tester / Runtime，按本轮规则保留并停止，不修改这些模块，也不进行第二次检查。不运行 Baseline、Holdout、完整 Benchmark，不更新 benchmark_history.md，等待用户确认。

| Case | 新 Task 执行预算 | 必要 Task 启动 | Main Requests（Planning / Replan / Summary） | Task Success | Check Completion | 结果 |
| --- | --- | --- | --- | --- | --- | --- |
| D13 | 90 / 90 / 90 | 3/3 | 2（1/0/1） | 3/3 | 14/14 | E2E PASS，无步数停止 |
| D12 | 90 / 90 | 2/2 | 2（1/0/1） | 0/2 | 4/7 | E2E FAIL，Tester Replan Limit 两次 |

本次只修改 Main Agent、相关 Main 本地测试及本文件；其余源文件、Development / Holdout Scenario、Ground Truth、Scoring、模型、Provider、冻结配置上限和 benchmark_history.md 均未修改。代码在检查前冻结，检查中及结果生成后未修改执行代码。

LangSmith 两条闭合追踪树共 323 个 Span（D13 188、D12 135），父子关系完整，均处于各自同一 Trace；包含 Main Planning / Summary、Tester Initial Plan / Exception Replan、Browser Action、Replay / Verification。Main Replan 与 Jev 调用均为 0，没有虚构对应 Span。见 [Tracing Review](../../artifacts/runs/main-budget-cp-20261009-001/tracing_review.json)。Replay 8/8 匹配，四次 Verification 均 FAIL / RECORDED_OUTCOME_MISMATCH，表示已检出缺陷仍存在；Bug Precision 4/4、Bug Recall 3/4 Case-Bug 配对。原始严格断言及失败完整保留。

本轮总体 LLM Requests 13（Main 4、Tester 9、Jev 0），Input/Output Tokens 114,691/155,402，总成本 USD 0.08241041477，Wall-clock 981.987 秒（16.37 分钟）。Main Input/Output Tokens 51,735/5,755，成本 USD 0.00564815797；记录测量数据，不进行 Token / Latency 优化。
