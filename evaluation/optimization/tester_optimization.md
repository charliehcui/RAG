# Tester Optimization

状态：已有实现和本地验证，正式优化尚未完成。完整基线（Baseline）见 [Benchmark History](../benchmark_history.md)。

这里只记录已实现的优化方向。本地验证数字说明执行路径，不能与完整基线（Baseline）直接相减计算性能收益；成熟版本的正式数字统一记录到基准历史（Benchmark History）。

## 页面子目标委托给 Jev + Playwright

- Problem：Tester 在页面操作和重复检查上消耗过多大语言模型请求（LLM Requests），即使批量提交步骤，仍需要生成较长的具体操作序列。
- Why：原有批处理主要合并工具调用，字段、选择器和点击顺序仍由 Tester 决定，页面决策没有交给 Jev。
- Change：已实现 `PageGoal`、`PageInput` 和 `execute_page_goals`，由 Tester 提供业务子目标、输入引用和断言，Jev 连续选择合法操作，Playwright 执行；同时保留精确实体绑定、候选重新验证、输入引用和 Python 实际断言，刷新及重复提交等边界操作继续使用 `execute_page_steps`。
- Before：正式基线（Baseline）的 Tester 请求数为 34.588 次/任务，平均任务耗时为 192.068 秒。
- After：已有本地假模型（Fake/Mock）验证记录为 1 次 Tester 请求、6 次 Jev 决策、5 次页面操作及最终断言；成熟版本的完整任务成功率、耗时、Token 和费用结果尚未完成，记为 N/A。
- Final Decision：当前暂时保留该方向，正式基准（Benchmark）完成前不视为最终方案。
