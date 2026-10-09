# Main Optimization

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
