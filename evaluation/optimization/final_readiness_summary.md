# Final Readiness Summary — 2026-10-10（测量 ID 保留 20261009）

**FINAL_BENCHMARK_READY = YES。四项核心停止条件已达成，已停止优化。**

最终架构保持：Main = Workflow / Task / Dependency / Live-session；Tester = Initial Plan + Exception Replan；Runtime = 页面观察 / 稳定对象绑定 / 合法候选 / 恢复 / 断言 / 信号；Jev = bounded choice / boolean / scoring；Playwright = Execute。唯一合法候选由程序直接执行。

主要根因及修复：计划与执行契约不一致、辅助断言与必要检查混淆、对象/输入/字段作用域混用、错误视图下生成空候选，以及无实际操作的权限误报。补齐严格计划校验和剩余 Check ID 状态、确定性断言类型、明确目的地执行、重复候选合并、字段与实体类型绑定、原对象消失检查、有限原容器恢复、权限操作及所有者关系证据、只读辅助观察续接、进度信号续接、跨角色 Replay 和 Playwright 响应清理。没有降低检查标准或改变测试目标；准备、刷新和独立保留性 Check 不被统一改成删除操作。

最新冻结验证：**D12 一次有效测量，2 个 Task / 7 个 Check**，run_id = final-stabilization-D12-20261009-008。修复后仅验证仍失败的复杂 live-session 流程；已通过 D03/D13 不重跑，旧成绩不并入新版分数。

| 指标 | 最新真实结果 |
| --- | --- |
| Task Success | 2/2 = 100% |
| Check Completion | 7/7 = 100% |
| False Positive / Precision / Recall | 0 / 100% / 2/2 = 100% |
| Tester Calls / Task | 3/2 = 1.5；计划提交 Initial 2、Exception Replan 1 |
| 严格 E2E Success | PASS |
| Required Task Startup | 2/2 = 100% |
| Main Requests / Replan | 2 / 0 |
| Dependency / Deadlock / Budget 缩减 | 0；每个 Task 预算继承 90 |
| Plan Validation Failure / Replan Limit | 0 / 0 |
| Replay | 4 个 Finding，8/8 复现匹配 |
| Verification | 4 次 FAIL / RECORDED_OUTCOME_MISMATCH，原缺陷仍存在；不是回放系统失败 |
| Browser Actions | Exploration 35，Replay / Verification 336 |
| Jev Decisions / Latency | 0 / N/A；真实动作均由当前唯一合法候选直接执行 |
| Wall-clock / Requests / Tokens / Cost | 523.431 秒 / 5 / 171,088 / USD 0.042304259 |
| LangSmith | 415 个闭合 Span，同一 Trace Tree、父子关系及原生计数完整一致 |

Runtime 当前没有无目标断言、非法动作、信号丢失或安全权限越界。一次 OBJECT_UNAVAILABLE_IN_CURRENT_VIEW 来自 B2 已实际删除原 Project 后的额外只读 Tasks 步骤，由一次 Admin Exception Replan 收尾；最终没有阻塞错误、任务中止或未复现 Finding。正式报告与 evaluation 评分一致。Main / 模型 / Provider / 配置上限 / Development 与 Holdout Scenario / Ground Truth / Scoring 的保护哈希保持不变。

本地回归：沿用此前全量 355 项及额外安全 1 项的通过证据；本次续做新增 **45 项**根因相关 unit / integration tests 最终通过，含真实 Playwright、假模型、正常/缺陷双会话及自动回放。没有重复运行原完整测试集。最终 Ruff、mypy（41 个源文件）、diff 检查通过。

剩余小问题：实际对象删除后的额外只读任务仍可能产生一次合理 Replan；模型服务时延与回放动作较多，本轮没有优化它们。本次最后版本只测 D12，小样本不能证明全 Development / Holdout 的总体成功率；普通多工作流 D03 的历史 3/3、18/18 和 D13 的历史 3/3、14/14 仅作为既有证据，不合并计分。

历史质量失败及 FP 全部保留。此前 001–007 的有效结果不替换、不删除；本次与它们使用不同冻结代码，禁止挑选或混合版本成绩。旧 Baseline v2 与 benchmark_history.md 保持不变。

**建议进入最终 Development Benchmark + Holdout 的授权步骤，但本次没有运行它们。代码冻结，不继续优化或再做真实 checkpoint。**

证据：[最新 Metrics](../../artifacts/runs/final-stabilization-rebinding-20261009-008/checkpoint_metrics.json)、[Main / Budget](../../artifacts/runs/final-stabilization-rebinding-20261009-008/main_budget_review.json)、[Runtime / Signals](../../artifacts/runs/final-stabilization-rebinding-20261009-008/runtime_review.json)、[LangSmith](../../artifacts/runs/final-stabilization-rebinding-20261009-008/tracing_review.json)、[本地验证](../../artifacts/final-stabilization-20261009/continued-local-validation.json)。
