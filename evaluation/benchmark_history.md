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

版本：`development-v2-checks-20261008`。开发集（Development Set）从 16 个 Case 精简为 10 个，保留 D01–D08、D12、D13，共 74 个稳定检查 ID（Check ID）。随后完成保留集（Holdout）的最终检查，冻结为 `holdout-v2-checks-20261008`，保留 8 个 Case、66 个 Check；整理记录见 [Holdout v2](holdout_v2.md)。本次正式基线（Baseline）不运行 Holdout。

评分增加检查完成度（Check Completion），并区分已恢复错误（Recovered Error）、仍阻塞错误（Unresolved / Blocking Error）、中止任务（Interrupted Task）和未启动任务（Not Started Task）。任务成功（Task Success）与端到端成功（E2E Success）仍严格要求必要检查全部正确完成。运行时（Runtime）、报告（Report）和正式评分使用同一个确定性评分函数（Deterministic Scorer）。所有真实正式 Evaluation 都开启 LangSmith tracing。

上方旧 Baseline 的测量数字原样保留，属于 v1 历史结果。v2 的 Evaluation Set 和评分规则已经改变，不能与 v1 直接相减或据此宣称性能收益。本节最初记录时尚未运行 v2 Baseline；用户授权后的正式 Baseline v2 结果已追加在下方。

删除与合并：D09 的双会话撤销检查由 D12 保留；D10 的验证与生命周期组合已由 D02、D03、D04、D06 覆盖；D11、D14 的保存与删除组合与 D04、D05、D07、D12 重复；D15、D16 的并行组合与 D03、D13 及聚焦缺陷用例重复。保留 D12 的耦合多缺陷流程和 D13 的独立并行多缺陷流程，必要的顺序、刷新和双会话要求仍在。

## 后续正式基准（Benchmark）

下方 Baseline v2 是后续优化（Optimization）的正式比较基准；只与 v2 比较，v1 保留为历史记录。本轮没有开始优化或修改优化记录。今后获得用户授权的正式结果在这里追加；优化方案记录在 [Tester Optimization](optimization/tester_optimization.md) 和 [Main Optimization](optimization/main_optimization.md)。

## 正式 Baseline v2 — 2026-10-08

状态：10 个开发用例（Development Case）各完成一次有效正式测量。日期采用 Australia/Sydney。

本轮仅测量（Measurement），没有优化（Optimization）。Agent、运行时（Runtime）、提示词（Prompt）、模型、供应商（Provider）、预算（Budget）、用例（Scenario）、标准答案（Ground Truth）和评分（Scoring）保持冻结；文件指纹检查通过。保留集（Holdout）未运行。

此前被用户中断的 D01 保留在 `artifacts/runs/baseline-v2-20261008-001/`，标记 `EXCLUDED_USER_INTERRUPTION`，不计入下方结果及消耗。正式轮次为 `artifacts/runs/baseline-v2-20261008-002/`。没有因 Agent 质量失败重跑；基础设施替换次数为 0。

### 测量条件

| 指标（Metric） | 结果 |
| --- | ---: |
| Evaluation / Holdout Version | `development-v2-checks-20261008` / `holdout-v2-checks-20261008` |
| 用例范围（Case Set） | D01–D08、D12、D13；10 个 Case；74 个稳定检查（Check） |
| Main Agent | `deepseek/deepseek-v4-flash` / `streamlake/fp8` |
| Tester Agent | `z-ai/glm-5.3-flash` / `relace` |
| Jev | `typesafe/jev-1.13` |
| 并发上限（Concurrency Limit） | 3 Tester / 3 Browser Contexts |
| 备用模型使用（Backup Model Usage） | 本轮未触发备用模型，实际模型 / 请求 Provider 与冻结配置一致 |
| 冻结运行预算（RunConfig Budget） | 900 秒；500 LLM 请求；10M 输入 / 1M 输出 Token；120 Jev |
| 每任务预算（Task Budget） | 90 Browser Steps；2 Exception Replans；0 Computer Use |
| 复现预算（Replay Budget） | 3 Attempts / Finding；250 Steps / Finding |

### Main Agent

| 指标（Metric） | 结果 |
| --- | ---: |
| 大语言模型请求总数（LLM Requests） | 47 |
| 重新规划次数（Replan Count） | 0 |
| 规划（Planning）请求 / 模型服务耗时（Service Latency） | 37 / 445.038 秒 |
| 规划（Planning）完整阶段耗时（Span Latency） | 448.371 秒 |
| 重新规划（Replan）请求 / 模型服务耗时（Service Latency） | 0 / 0.000 秒 |
| 重新规划（Replan）完整阶段耗时（Span Latency） | 0.000 秒 |
| 总结（Summary）请求 / 模型服务耗时（Service Latency） | 10 / 241.766 秒 |
| 总结（Summary）完整阶段耗时（Span Latency） | 242.426 秒 |
| 完整输入 / 输出 Token | 283,336 / 36,651 |
| 完整费用（Cost，USD） | 0.010910239 |
| 已记录输入 / 输出 Token 下界 | 283,336 / 36,651 |
| 已记录费用下界（USD） | 0.010910239 |

### Tester Agent

| 指标（Metric） | 结果 |
| --- | ---: |
| 任务成功率（Task Success） | 3/14（21.43%） |
| 检查完成度（Check Completion） | 19/74（25.68%） |
| LLM Requests / Task | 2.714 |
| LLM Requests 总数 | 38 |
| Initial Plan LLM Calls / Replan LLM Calls | N/A / N/A |
| 初始计划 / 异常重规划提交（Plan Submissions） | 14 / 24 |
| Input / Output Tokens / Task | 41,081.357 / 18,633.714 |
| Cost / Task（USD） | 0.010509117 |
| 模型服务耗时 / 任务（Service Latency / Task） | 296.382 秒 |
| 平均 / 最长任务耗时（Task Wall-clock） | 312.111 / 594.978 秒 |
| 完整输入 / 输出 Token | 575,139 / 260,872 |
| 完整费用（Cost，USD） | 0.147127640 |
| 已记录输入 / 输出 Token 下界 | 575,139 / 260,872 |
| 已记录费用下界（USD） | 0.147127640 |

### Jev / Runtime

| 指标（Metric） | 结果 |
| --- | ---: |
| 决策总数 / 任务（Jev Decisions / Task） | 65 / 4.643 |
| 总 / 平均 Jev 耗时（Latency） | 35.016 秒 / 538.710 毫秒 |
| 探索浏览器动作 / 任务（Exploration Browser Actions / Task） | 146 / 10.429 |
| 全部浏览器动作 / 任务（All Browser Actions / Task） | 320 / 22.857 |
| 复现 / 验证浏览器动作（Replay / Verification Actions） | 174 |
| 无进展停止（No-progress Stops） | 0 |
| 稳定复现（Reproduction Success） | 6/6（100.00%） |
| 复现尝试成功（Replay Attempt Success） | 12/12（100.00%） |
| 验证结果（Verification Results） | {"FAIL": 6} |
| 完整输入 / 输出 Token | 132,335 / 8,075 |
| 完整费用（Cost，USD） | 0.005558070 |
| 已记录输入 / 输出 Token 下界 | 132,335 / 8,075 |
| 已记录费用下界（USD） | 0.005558070 |

### Overall

| 指标（Metric） | 结果 |
| --- | ---: |
| 端到端成功率（E2E Success） | 1/10（10.00%） |
| 任务成功率（Task Success） | 3/14（21.43%） |
| 缺陷精确率（Bug Precision） | 4/6（66.67%） |
| 缺陷召回率（Bug Recall） | 3/9（33.33%） |
| 检查完成度（Check Completion） | 19/74（25.68%） |
| 实际整轮耗时（Total Wall-clock） | 4,323.695 秒 |
| 逐 Case 耗时合计（Case Wall-clock Sum） | 4,323.551 秒 |
| Main + Tester LLM Requests | 85 |
| Jev Requests | 65 |
| 所有模型请求（Total Model Requests） | 150 |
| 完整 Token 总数（Total Tokens） | 1,296,408 |
| 已恢复 / 阻塞错误（Recovered / Blocking Errors） | 0 / 0 |
| 未启动 / 中止 / Agent 执行失败任务（Task Outcomes） | 1 / 9 / 2 |
| Provider 失败请求（Failed Requests） | 0 |
| 完整输入 / 输出 Token | 990,810 / 305,598 |
| 完整费用（Cost，USD） | 0.163595949 |
| 已记录输入 / 输出 Token 下界 | 990,810 / 305,598 |
| 已记录费用下界（USD） | 0.163595949 |

### 逐 Case 正式结果

| Case | 执行状态（Status） | Task Success | Check Completion | Main / Tester / Jev Requests | Browser Actions（探索 / 全部） | Wall-clock 秒 |
| --- | --- | --- | --- | ---: | ---: | ---: |
| D01 | INTERRUPTED | 0/1 | 4/10 | 5 / 3 / 11 | 18 / 57 | 700.435 |
| D02 | INTERRUPTED | 0/1 | 0/9 | 5 / 3 / 0 | 2 / 2 | 658.324 |
| D03 | INTERRUPTED | 1/3 | 8/18 | 4 / 7 / 29 | 48 / 48 | 554.323 |
| D04 | INTERRUPTED | 0/1 | 1/3 | 4 / 3 / 7 | 12 / 42 | 378.418 |
| D05 | INTERRUPTED | 0/1 | 0/3 | 5 / 3 / 4 | 8 / 8 | 269.616 |
| D06 | INTERRUPTED | 0/1 | 0/3 | 5 / 3 / 0 | 5 / 5 | 338.707 |
| D07 | COMPLETED | 1/1 | 2/2 | 4 / 3 / 5 | 13 / 43 | 224.324 |
| D08 | INTERRUPTED | 0/1 | 0/5 | 5 / 3 / 0 | 6 / 24 | 474.527 |
| D12 | INTERRUPTED | 0/1 | 0/7 | 5 / 3 / 0 | 5 / 5 | 306.857 |
| D13 | INTERRUPTED | 1/3 | 4/14 | 5 / 7 / 9 | 29 / 86 | 418.020 |

表中 `INTERRUPTED` 是正式执行器因原有停止条件结束的状态，不表示再次被用户中断；这些有效结果全部计入正式 Baseline。用户中断的旧 D01 单独排除。

### 逐任务原始指标（Task Metrics）

| Case / Task ID | 最终分类（Outcome） | Checks | Tester Calls | Input / Output Tokens | Cost USD | Task Wall-clock 秒 | Jev Decisions | Browser Actions（探索 / 全部） |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| D01 / T01-task-workflow | AGENT_EXECUTION_FAILURE | 4/10 | 3 | 72,391 / 31,705 | 0.018222060 | 594.978 | 11 | 18 / 57 |
| D02 / onboard-workflow | INTERRUPTED | 0/9 | 3 | 69,340 / 31,945 | 0.017782260 | 570.495 | 0 | 2 / 2 |
| D03 / D03-project-workflow | NORMAL_APPLICATION_BEHAVIOR | 4/4 | 1 | 4,804 / 8,640 | 0.004512160 | 179.697 | 10 | 19 / 19 |
| D03 / D03-task-workflow | INTERRUPTED | 4/8 | 3 | 53,849 / 21,330 | 0.012093200 | 308.125 | 19 | 24 / 24 |
| D03 / D03-member-workflow | INTERRUPTED | 0/6 | 3 | 62,444 / 28,169 | 0.016136820 | 473.004 | 0 | 5 / 5 |
| D04 / task-delete-seed-task | INTERRUPTED | 1/3 | 3 | 49,184 / 19,124 | 0.010857360 | 314.275 | 7 | 12 / 42 |
| D05 / D05.permission-delete-seed-project | INTERRUPTED | 0/3 | 3 | 31,435 / 15,215 | 0.008435780 | 235.826 | 4 | 8 / 8 |
| D06 / T01-project-name-validation | INTERRUPTED | 0/3 | 3 | 41,784 / 16,858 | 0.009506120 | 277.337 | 0 | 5 / 5 |
| D07 / rename-project | APPLICATION_BUG_DETECTED | 2/2 | 3 | 30,456 / 11,575 | 0.006776300 | 152.608 | 5 | 13 / 43 |
| D08 / TASK-repeat-create | AGENT_EXECUTION_FAILURE | 0/5 | 3 | 45,872 / 21,795 | 0.012413660 | 406.813 | 0 | 6 / 24 |
| D12 / offboarded-access | INTERRUPTED | 0/4 | 3 | 37,471 / 14,143 | 0.008082660 | 235.323 | 0 | 5 / 5 |
| D12 / offboard-member | NOT_STARTED | 0/3 | 0 | 0 / 0 | 0.000000000 | N/A | 0 | 0 / 0 |
| D13 / name-validation | APPLICATION_BUG_DETECTED | 4/4 | 1 | 4,751 / 12,001 | 0.006190540 | 229.066 | 6 | 17 / 74 |
| D13 / pending-submission | INTERRUPTED | 0/3 | 3 | 23,178 / 7,845 | 0.004565460 | 102.401 | 0 | 5 / 5 |
| D13 / task-workflow | INTERRUPTED | 0/7 | 3 | 48,180 / 20,527 | 0.011553260 | 289.605 | 3 | 7 / 7 |

### LangSmith 追踪（Tracing）

完整查询到 10/10 个已结束追踪树（Trace Tree）；与本地 LLM、Jev、BrowserAction、Initial Plan / Replan 事件计数一致的 Case 为 10/10。所有 Case 均配置开启追踪。

| Case | 云端跨度（Cloud Spans） | 同一 Trace / 父节点完整 | 本地事件与云端计数一致 |
| --- | ---: | --- | --- |
| [D01](https://smith.langchain.com/o/d3430704-a04f-4863-aabc-c675857e1e0d/projects/p/2df542ca-6ddc-4a30-9092-652d656d1ecb/r/01a118e1-943e-77b0-b2eb-d33f82941e01?trace_id=01a118e1-943e-77b0-b2eb-d33f82941e01) | 97 | True / True | True |
| [D02](https://smith.langchain.com/o/d3430704-a04f-4863-aabc-c675857e1e0d/projects/p/2df542ca-6ddc-4a30-9092-652d656d1ecb/r/01a118ec-4424-79c3-aa8d-b6dc127604ab?trace_id=01a118ec-4424-79c3-aa8d-b6dc127604ab) | 23 | True / True | True |
| [D03](https://smith.langchain.com/o/d3430704-a04f-4863-aabc-c675857e1e0d/projects/p/2df542ca-6ddc-4a30-9092-652d656d1ecb/r/01a118f6-4fba-71f0-9e38-bc757105f65a?trace_id=01a118f6-4fba-71f0-9e38-bc757105f65a) | 121 | True / True | True |
| [D04](https://smith.langchain.com/o/d3430704-a04f-4863-aabc-c675857e1e0d/projects/p/2df542ca-6ddc-4a30-9092-652d656d1ecb/r/01a118fe-c51a-7742-b7fc-16d1ddeac293?trace_id=01a118fe-c51a-7742-b7fc-16d1ddeac293) | 74 | True / True | True |
| [D05](https://smith.langchain.com/o/d3430704-a04f-4863-aabc-c675857e1e0d/projects/p/2df542ca-6ddc-4a30-9092-652d656d1ecb/r/01a11904-8b62-72a0-8626-32836318f3a3?trace_id=01a11904-8b62-72a0-8626-32836318f3a3) | 35 | True / True | True |
| [D06](https://smith.langchain.com/o/d3430704-a04f-4863-aabc-c675857e1e0d/projects/p/2df542ca-6ddc-4a30-9092-652d656d1ecb/r/01a11908-a89b-7130-b151-a38b05033c68?trace_id=01a11908-a89b-7130-b151-a38b05033c68) | 26 | True / True | True |
| [D07](https://smith.langchain.com/o/d3430704-a04f-4863-aabc-c675857e1e0d/projects/p/2df542ca-6ddc-4a30-9092-652d656d1ecb/r/01a1190d-d3b6-7ab1-bd38-b2bfb139a43b?trace_id=01a1190d-d3b6-7ab1-bd38-b2bfb139a43b) | 73 | True / True | True |
| [D08](https://smith.langchain.com/o/d3430704-a04f-4863-aabc-c675857e1e0d/projects/p/2df542ca-6ddc-4a30-9092-652d656d1ecb/r/01a11911-400f-7eb2-a2cd-7d5c50ed7397?trace_id=01a11911-400f-7eb2-a2cd-7d5c50ed7397) | 49 | True / True | True |
| [D12](https://smith.langchain.com/o/d3430704-a04f-4863-aabc-c675857e1e0d/projects/p/2df542ca-6ddc-4a30-9092-652d656d1ecb/r/01a11918-7dba-7ff0-8692-5bd9d25d815e?trace_id=01a11918-7dba-7ff0-8692-5bd9d25d815e) | 27 | True / True | True |
| [D13](https://smith.langchain.com/o/d3430704-a04f-4863-aabc-c675857e1e0d/projects/p/2df542ca-6ddc-4a30-9092-652d656d1ecb/r/01a1191d-2c6b-79e3-b717-2af6cf656ad1?trace_id=01a1191d-2c6b-79e3-b717-2af6cf656ad1) | 144 | True / True | True |

已执行的阶段使用现有名称：`MainPlanning`、`TesterInitialPlan`、`TesterExceptionReplan`、`Jev`、`BrowserAction`、`Reproduction`（Replay）、`Verification` 和 `MainFinalSummary`。未触发的阶段不产生 Span；例如没有进入 Jev 或没有生成 Finding 的 Case，不会有对应 Jev / Replay / Verification 节点。敏感输入和输出继续隐藏，只读取非敏感元数据（Metadata）。

只读查询曾遇到超时和 HTTP 429 限流，放慢读取后完成核对；没有因追踪（Tracing）读取问题替换或重跑 Case。Main Replan 本轮未触发。

### 评分与统计说明

- 有效 Agent 失败、中止和未完成检查全部保留在对应分母中；执行状态与任务成功（Task Success）分别记录。检查完成度（Check Completion）表示完成了检查，不自动表示业务行为通过。
- 本轮评分直接使用冻结正式执行器（Formal Executor）的结果。原始报告（Report）与正式指标（Evaluation Metrics）全部一致；汇总只加总计数，不重新评分。
- Initial Plan / Replan 的 LLM 请求子阶段没有独立原生标签，记录 N/A。计划提交（Plan Submission）次数来自原生事件，不能当作 LLM 请求次数。LangSmith 对应 Span 包含计划提交 / 执行阶段，LLM Span 位于 Tester 下面。
- 模型服务耗时（Service Latency）是实际请求耗时合计；完整阶段耗时（Span Latency）独立报告，不能与并行任务耗时相加来估计总耗时。费用（Cost）采用供应商记录，缺失总量为 N/A，并保留已记录下界。
- Overall 的请求、Token、费用包含 Main、Tester、Jev；Main + Tester 请求另列，保持定义明确。复现成功（Reproduction Success）衡量可重复性，与标准答案（Ground Truth）匹配和缺陷精确率（Bug Precision）分别统计。
- 任务成功（Task Success）以 14 个已尝试任务为分母；另有 1 个未启动任务，仍使相应 Case 的 E2E 失败。Precision 按确认的 Finding 统计；Recall 按去重后的 Case 与 Bug 组合统计，4 条正确 Finding 对应 3 个检出组合。
- Verification 的 6 个原生结果均为 `FAIL / RECORDED_OUTCOME_MISMATCH`；按原值保留。该字段不等于 Ground Truth 正确性判定，不能用来替代 Precision / Recall。
- v1 Baseline 保留为历史 Evaluation Version。v2 的用例和评分已改变，两者不完全直接可比；后续优化（Optimization）只与本节 Baseline v2 比较。

### 当前主要瓶颈（Bottlenecks）

1. 计划与执行接口的约束频繁不一致。38 次计划提交中，26 次返回检查 ID、输入、边界动作或计划完整性校验原因；另有 7 次低 Jev 置信度（LOW_JEV_CONFIDENCE）和 2 次控件缺失（CONTROL_NOT_FOUND）。分项来自对应计划 Span 的失败元数据，不是 Provider 请求失败。
2. 必要检查没有稳定完成。11 个任务达到原有重规划上限（MAX_TASK_REPLANS_REACHED）；D02、D06、D08、D12 没有完成任何 Check。D12 的另一依赖任务未启动。D04 正确检出缺陷，但只完成 1/3 检查，最终分类为 INTERRUPTED，未计为 Task Success。
3. 稳定复现仍有误报。D01、D08 各有 1 个可复现的错误 Finding；全部复现成功并不代表正确判断业务行为。D13 两个正确 Finding 匹配同一检出组合，Recall 仍按去重结果计数。
4. 模型服务耗时集中在 Tester：4,149.346 秒，占累计模型请求服务耗时的 85.18%；Jev 累计 35.016 秒。该比例是请求耗时合计，不是实际总耗时占比。本轮不据此优化调用数、Token 或耗时。

上述为本轮测量证据，不修改冻结评分、Evaluation 或 Agent / Runtime，也没有为差结果补跑。后续动作等待用户确认。

逐任务的 Token、费用、耗时、检查和错误记录见 `measurement_metrics.json`；有效性判定见 `validity_review.json`；追踪验证见 `tracing_review.json`，均位于正式轮次目录。没有更新 `tester_optimization.md`，没有开始后续优化。
