# Benchmark History

正式基准（Benchmark）的长期记录。初始基线（Baseline）和之后的正式结果统一保存在这里；`artifacts/` 仅保存可清理的运行时产物。

## 记录与比较规则

- 用例（Scenario）和标准答案（Ground Truth）保持一致。
- 模型、供应商（Provider）、预算和重置（reset）规则保持一致。
- 保留用例（Holdout）不参与优化。
- LangSmith 追踪（tracing）设置保持一致。
- 有效失败保留在分母中。

## 历史 Evaluation Version v1 — 初始基线（Baseline）— 2026-10-07

状态：已完成。日期采用 Australia/Sydney。

### 测量条件

| 条件 | 设置 |
| --- | --- |
| 用例范围 | 16 个开发用例（Development），每个用例一次有效正式测量 |
| 任务与缺陷分母 | 34 个已尝试任务；20 次用例与缺陷组合 |
| Main Agent | `deepseek/deepseek-v4-flash` / `streamlake/fp8` |
| Tester Agent | `z-ai/glm-5.3-flash` / `relace` |
| Jev | `typesafe/jev-1.13` |
| 并发上限（concurrency limit） | 3 个 Tester / 3 个浏览器上下文 |

### Main Agent Baseline

| 指标（Metric） | 结果 |
| --- | ---: |
| 规划成功率（Planning Success，语义） | N/A |
| 最终总结忠实度（Final Summary Faithfulness，语义） | N/A |
| 重新规划次数（Replan Count） | 5 |
| 规划请求数 / 模型服务耗时 | 62 / 622.019 秒 |
| 重新规划请求数 / 模型服务耗时 | 15 / 183.931 秒 |
| 最终总结请求数 / 模型服务耗时 | 13 / 382.536 秒 |
| 完成最终总结的用例数 | 13/16 |
| 完整规划、重新规划、总结阶段耗时 | N/A |
| 大语言模型请求总数（LLM Requests） | 91 |
| 完整输入 / 输出 Token | N/A / N/A |
| 已记录输入 / 输出 Token 下界 | 539,701 / 78,088 |
| 完整费用（Cost） | N/A |
| 已记录费用下界（USD） | $0.018733 |

### Tester Agent Baseline

| 指标（Metric） | 结果 |
| --- | ---: |
| 任务成功率（Task Success） | 8/34（23.53%） |
| 大语言模型请求数 / 任务（LLM Requests / Task） | 34.588 |
| 大语言模型请求总数 / 每次运行均值（LLM Requests） | 1,176 / 73.500 |
| Jev 请求数 / 任务（Jev Requests / Task） | 0.118 |
| Jev 请求总数 / 每次运行均值（Jev Requests） | 4 / 0.250 |
| 平均任务耗时（Average Latency / Task） | 192.068 秒 |
| 最长任务耗时（Longest Task Latency） | 738.208 秒 |
| 完整输入 / 输出 Token | N/A / N/A |
| 已记录输入 / 输出 Token 下界 | 18,492,510 / 292,118 |
| 已记录 Token / 任务下界 | 552,489.059 |
| 完整费用（Cost） | N/A |
| 已记录费用下界（USD） | $0.620585 |
| 已记录费用 / 任务下界（USD） | $0.0182525 |

### Overall Baseline

| 指标（Metric） | 结果 |
| --- | ---: |
| 端到端成功率（E2E Success） | 0/16（0%） |
| 缺陷精确率（Bug Precision） | N/A（0 个确认缺陷） |
| 缺陷召回率（Bug Recall） | 0/20（0%） |
| 复现成功率（Reproduction Success） | N/A（0 次复现尝试） |
| 实际总耗时 / 每次运行均值（Wall-clock Time） | 6,464.459 / 404.029 秒 |
| 大语言模型请求总数 / 每次运行均值（LLM Requests） | 1,267 / 79.188 |
| Jev 请求总数 / 每次运行均值（Jev Requests） | 4 / 0.250 |
| 完整 Token 总数 / 费用 | N/A / N/A |
| 已记录 Token 总数下界 | 19,411,496 |
| 已记录费用下界（USD） | $0.639662 |
| 并发 Tester 峰值（Peak Concurrent Testers） | 3 |
| 并发利用率（Concurrency Utilization） | 40.22% |

### 必要的测量说明

- N/A 表示缺失或未评定，已记录下界不等于完整消耗；费用为供应商估计。
- 正确检出缺陷可以算任务成功（Task Success）；执行状态 COMPLETED 不等于端到端成功（E2E Success）。
- 大语言模型请求总数（LLM Requests）为 Main 加 Tester，Jev 单独计数；Tester 的 Token 与费用不含 Jev，Overall 包含 Jev。
- 模型服务耗时和并行任务耗时不能当作实际总耗时（Wall-clock Time）。

## Evaluation Version v2 — 2026-10-08

版本：`development-v2-checks-20261008`。开发集（Development Set）从 16 个 Case 精简为 10 个，保留 D01–D08、D12、D13，共 74 个稳定检查 ID（Check ID）。8 个保留用例（Holdout）的业务内容保持不变。

评分增加检查完成度（Check Completion），并区分已恢复错误（Recovered Error）、仍阻塞错误（Unresolved / Blocking Error）、中止任务（Interrupted Task）和未启动任务（Not Started Task）。任务成功（Task Success）与端到端成功（E2E Success）仍严格要求必要检查全部正确完成。运行时（Runtime）、报告（Report）和正式评分使用同一个确定性评分函数（Deterministic Scorer）。所有真实正式 Evaluation 都开启 LangSmith tracing。

上方旧 Baseline 的测量数字原样保留，属于 v1 历史结果。v2 的 Evaluation Set 和评分规则已经改变，不能与 v1 直接相减或据此宣称性能收益。当前没有运行 v2 Baseline、真实模型验证或新 Benchmark；待用户 Token 充足并另行授权后再安排。

删除与合并：D09 的双会话撤销检查由 D12 保留；D10 的验证与生命周期组合已由 D02、D03、D04、D06 覆盖；D11、D14 的保存与删除组合与 D04、D05、D07、D12 重复；D15、D16 的并行组合与 D03、D13 及聚焦缺陷用例重复。保留 D12 的耦合多缺陷流程和 D13 的独立并行多缺陷流程，必要的顺序、刷新和双会话要求仍在。

## 后续正式基准（Benchmark）

尚无成熟优化版本的完整正式结果。完成后在这里追加日期和三组结果表；优化方案记录在 [Tester Optimization](optimization/tester_optimization.md) 和 [Main Optimization](optimization/main_optimization.md)。
