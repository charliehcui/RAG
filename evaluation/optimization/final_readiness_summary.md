# Final Readiness Summary — Final Development Benchmark v2，2026-10-10

## D02 / D03 最后一次小范围修复 — 2026-10-10

本节描述正式测量之后的新代码；下方正式 Final Development Benchmark v2 的有效结果全部保留，**FINAL_DEVELOPMENT_PASS = NO** 不变。未重新运行真实 Case、Benchmark 或 Holdout，新代码的真实 Task Success / Check Completion / False Positive / Tester Calls 均为 **N/A**。

只修改 Tester → Runtime 的断言对象契约、当前填表候选与稳定行定位这三处直接必要代码。Main、Scenario、Ground Truth、Scoring、Model / Provider、Budget、Jev Selector、Replay、Safety 和 Tracing 实现保持原值。架构仍为 Main 规划依赖、Tester Initial Plan / Exception Replan、Runtime 状态与恢复、Jev bounded decision、Playwright 执行。

- D02：原三个候选分别是三个已经绑定的注册字段。Runtime 按计划中的未完成字段顺序生成当前唯一动作，在排序/截断前筛选，并合并等价候选与重复输入绑定；直接执行唯一合法动作，真正多选仍调用 Jev，低置信度仍拒绝执行。
- D03：原辅助 Name 断言缺少原对象范围。程序继承明确行对象或唯一实际操作输入，保留显式 target（同时提供 control 时也保留）；不从 expected 反推目标，form_context 不充当目标行。无法确定对象的列断言在整个计划执行前拒绝。稳定行目标不再被相同文本的另一行覆盖。
- 本地验证：**LOCAL_FIX_VALIDATED = YES**。全量首轮 420 项中 400 项通过；修正受新契约影响的旧模拟计划/接口后，原 20 个失败项与 D02 八个候选组合的定向批次 **28/28 通过**。D03 对象/表单/显式目标与预检的新增边界验证收尾通过，最新计划契约 **57/57 通过**；Ruff、mypy（41 源文件）及 diff check 通过。按当前收集的 **428 个测试节点逐项核对，全部有通过记录**，这是全量与必要定向收尾的分批证据，不声称最后重新执行了全部 428 项。没有重复未受后续修改影响的已通过测试，没有调用真实 LLM。证据见 [本地核对记录](../../artifacts/d02-d03-fix-review.json)。
- 建议：本地验证通过后，可以由用户另行授权一次冻结版本的正式 Final Development Benchmark。此前正式失败不覆盖，新测量单独记录；不自动开始，Holdout 继续等待授权。本地通过不保证四项真实目标达标。

具体变更记录：[Runtime Optimization](runtime_optimization.md)、[Tester Optimization](tester_optimization.md)。`benchmark_history.md` 和 Main Optimization 本轮不修改。

## 已完成的正式测量（修复前代码）

**FINAL_DEVELOPMENT_PASS = NO。**正式 10-case Development Benchmark 已完成。三项核心条件达标，Check Completion 未达到 90%。停止本轮，不自动优化、补跑或运行 Holdout。

此前 FINAL_BENCHMARK_READY = YES 来自 D12 readiness 样本；当前以完整正式 Development 结果为准。原就绪报告保留在本轮 pre_benchmark_readiness_summary.md 和原归档文件，不把其样本成绩混入本轮。

架构保持：Main = Workflow / Task / Dependency / Live-session；Tester = Initial Plan + Exception Replan；Runtime = 页面状态 / Binding / Candidates / Recovery / Assertion / Progress Signal；Jev = bounded choice / boolean / scoring；Playwright = Execute。模型、Provider、Budget、Scenario、Ground Truth、Scoring、Prompt、生产源代码与 Evaluation 配置保持冻结。

| 指标（Metric） | Baseline v2 | Final Development Benchmark v2 |
| --- | --- | --- |
| 任务成功（Task Success） | 3/14（21.43%） | 13/15（86.67%） |
| 检查完成（Check Completion） | 19/74（25.68%） | 58/74（78.38%） |
| 端到端成功（E2E Success） | 1/10（10.00%） | 8/10（80.00%） |
| 缺陷召回（Bug Recall） | 3/9（33.33%） | 9/9（100.00%） |
| 稳定复现（Reproduction Success） | 6/6（100.00%） | 15/15（100.00%） |
| 复现尝试成功（Replay Attempt Success） | 12/12（100.00%） | 30/30（100.00%） |
| 缺陷精确率（Bug Precision） | 4/6（66.67%） | 15/15（100.00%） |
| 误报（False Positive） | 2 | 0 |
| Tester Calls / Task | 2.714 | 1.267 |
| Main Requests / Replans | 47 / 0 | 18 / 0 |
| Jev Decisions | 65 | 2 |
| Browser Actions | 320 | 889 |
| Replan Limit Stops | 11 | 0 |
| Total Tokens | 1,296,408 | 769,976 |
| Total Cost USD | 0.163595949 | 0.220798106 |
| Wall-clock 秒 | 4,323.695 | 5,375.683 |

必要流程启动 15/15；Main 依赖、死锁、重复任务、closed-task redirect、执行预算缩减为 0，每 Task 90 steps。Baseline 分母为 14 个已尝试任务及 1 个未启动，本轮 15 个全部启动，保留同一计分定义。费用与总耗时上升，如实保留。

- **D02（Runtime / Jev 的填表候选接口）**：D02.registered 的 fill 阶段有 3 个候选，两次返回 NO_SELECTION；Runtime 各重新观察一次，状态与结果仍相同，0/9 检查完成。Tester 实际请求 3 次，服务耗时共 996.136 秒，最终 MAX_RUNTIME_REACHED。现有测量不能单独区分候选质量与 Jev 选择质量，不能归为 Provider/API 故障。
- **D03（Tester → Runtime 的断言目标契约）**：task-workflow 的初始计划返回 AMBIGUOUS_ASSERTION_TARGET，仅完成准备检查（1/8）；任务创建及后续 7 个检查未完成。Exception Replan 生成后遇到 BUDGET_EXCEEDED，正式停止原因为 MAX_RUNTIME_REACHED。其他两个 Task 通过，Case 合计 11/18。
- 两个失败 Task 的预算均为冻结的 90 steps；不是 Main 缩减预算或 MAX_TASK_STEPS_REACHED。RunConfig 保持 900 秒，D02 实际 1010.335 秒；既有预算检查不会在所有进行中的模型请求上立即硬取消。D02/D03 没有新的 Main Summary 模型请求，总结请求总数为 8 次。没有延长预算、改停止逻辑或继续修复。

已恢复问题：D01 的 CONTROL_NOT_FOUND 经一次 Exception Replan 收尾，10/10 检查完成。D12 信号顺序正确；原 Project 真正删除后，ORIGINAL_OBJECT_ABSENCE_CHECK 保留原对象缺失状态并续接，2/2 Tasks、7/7 Checks，只有两次 Initial Plan。

Replay / Safety / Scoring：15/15 Finding 正确稳定复现，30/30 尝试匹配；15 个 Verification FAIL 表示真实缺陷仍存在。没有安全越界、伪造或非法动作，Report 与 Evaluation Metrics 全部一致。102 个 ASSERTION_FAILURE 含真实缺陷断言，不等于系统错误。

LangSmith：10/10 完整闭合 Trace Tree、1,162 Span，父子关系完整，模型 / Jev / 浏览器动作计数 10/10 匹配。计划提交与阶段 Span 严格同数为 9/10：D02 最后一个 Replan 因预算停止而未提交。Initial/Replan LLM Calls 标签缺失，记 N/A；实际提交 Initial 15 / Replan 3，阶段 Span Initial 15 / Replan 4，保留各自定义。总请求 Main 18 + Tester 19 + Jev 2 = 39；Tester 平均请求耗时 299.139 秒，阶段与逐任务细项见正式报告。

本轮每 Case 一次有效测量，基础设施替换 0，Provider/API 失败 0。没有重新运行 local/unit tests，没有中途修改冻结文件，只更新正式文档和测量记录；旧 v1/v2 与 Checkpoint 记录保留。

**本轮正式 Development 未通过；现在停止，后续由用户单独决定，不自动运行 Holdout。**

证据：[正式 Benchmark 报告](../../artifacts/runs/final-development-v2-20261010-001/final_development_v2_report.md)、[指标](../../artifacts/runs/final-development-v2-20261010-001/measurement_metrics.json)、[失败证据](../../artifacts/runs/final-development-v2-20261010-001/failed_case_evidence.json)、[追踪及安全核对](../../artifacts/runs/final-development-v2-20261010-001/runtime_tracing_integrity.json)。
