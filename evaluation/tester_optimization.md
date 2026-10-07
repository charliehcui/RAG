# Tester Optimization

状态：**已暂停（Paused），优化尚未完成。** 用户因 Token 不足要求暂停。本次仅修复文档乱码和完善交接说明，没有恢复真实模型调用、修改业务代码、提交或推送。

## Baseline 与要求完成状态

沿用已有 `evaluation/baseline.md`，没有重新运行基线（Baseline）。代码已实现批量操作（Batch Execution）、精确定位、字段断言（Assertion）、立即取证、重复发现去重和登录准备。已经尝试两种主要方案；第二轮仅 D02、D05 为有效代表用例，D14 因供应商限流（HTTP 429）中断。

**最终 16 个开发用例（Development）的完整评估尚未执行。当前样本平均任务耗时仍超出目标，不能宣布优化完成，也不能给出全量完成百分比。**

下表的当前样本仅统计第二轮有效 D02、D05；请求、Token、成本和耗时仅统计 Tester，不混入主智能体（Main Agent）。

| 要求 | Baseline | 完成目标 | 当前有效样本 | 全量状态 |
| --- | ---: | ---: | ---: | --- |
| 任务成功率（Task Success Rate） | 8/34，23.53% | ≥70% | 2/2，100% | 待完整验证 |
| 端到端成功率（E2E Success Rate） | 0/16，0% | ≥50% | 2/2，100% | 待完整验证 |
| 缺陷召回率（Bug Recall） | 0/20，0% | ≥70% | 1/1，100% | 待完整验证 |
| 缺陷精确率（Bug Precision） | N/A，无确认缺陷 | ≥80% | 2/2，100% | 待完整验证 |
| Tester 请求数/任务（Requests per Task） | 34.588 | ≤8 | 6 | 样本达标，全量待验证 |
| Tester Token/任务 | 已记录下界 552,489.059 | 降低 ≥40% | 65,602.5 | 待完整可比评估 |
| Tester 平均任务耗时（Task Latency） | 192.068 秒 | ≤144 秒 | 184.339 秒 | 样本未达标 |
| Tester 成本/任务（Cost per Task） | 已记录下界 $0.0182525 | 降低 ≥30% | $0.00582064 | 待完整可比评估 |

基线（Baseline）完整 Token 和成本（Cost）均为 N/A；缺失记录不能按零计算。两例样本与全量基线（Baseline）的范围不同，主智能体（Main Agent）的任务拆分也存在运行间差异，因此不能直接换算成正式全量降幅。基线（Baseline）Jev 请求为 0.118 次/任务，当前两例为零；本地唯一定位成功时直接使用 Playwright，歧义定位才调用 Jev。

## 问题 1：逐步推理与重复检查增加调用

第一种方案增加 `PageStep` 和 `execute_page_steps`，一次调用按顺序执行输入、点击、等待和断言（Assertion），支持 `finish=True`。输入保留数据引用（Data Reference），动作写入原生记录（Native Event）以支持回放（Replay）。

第一轮 D05 调用降到 13 次，但数字等待参数 `500` 被当成选择器（Selector），留下动作错误，任务未通过。第二种方案补齐类型转换和等待归一化，增加确定性登录准备（Deterministic Preparation）与初始页面摘要（Page Summary），精简工具并串行调用。第二轮 D02、D05 都通过，各调用 6 次。

登录准备仅在已观察到唯一登录控件且具备配置数据引用（Data Reference）时执行；注册流程不跳过，密码不暴露到页面摘要（Page Summary），也不提前判定目标完成。

## 问题 2：控件和验证字段定位不准确

第一轮 D14 在编辑对话框（Dialog）中填到了背景项目名称，并用整行或整页内容验证单个字段。运行超时后两个任务均未通过，缺陷召回率（Bug Recall）为 1/2，复现成功率（Reproduction Success Rate）为 1/7。这是有效质量失败，原始结果必须保留。

第二种方案限定前景对话框（Dialog）控件，通过已观察到的行、表头、单元格和实体标识定位。唯一控件直接交给 Playwright；歧义定位保留现有 Jev 调用、置信度和权限检查。字段断言（Assertion）读取实际单元格，删除后的缺失对象也产生明确检查结果。可见性检查不能替代文本比较，计数检查使用行集合。

当前代码还精确区分 `member` 与 `member2`，优先匹配 Username 列，避免匹配到相同 Role 值。**这项最后修改晚于第二轮真实运行，仅完成本地验证；已有样本不能视为当前源码的完整验证。**

## 问题 3：重复发现、复现成本和协作等待

失败断言（Assertion）会在当前动作边界立即记录证据（Evidence）与发现（Finding），保持准确回放起点。相同检查及重复人工报告复用已有发现（Finding），避免反复复现（Reproduction）。批量动作保留行为标识（Behavior ID）、预期值和数据引用（Data Reference）。

共享等待（Shared Wait）遇到直接的在线参与方依赖环时请求重规划（Replan），减少模型轮询。主智能体（Main Agent）的算法和提示词没有修改；基线（Baseline）D09、D12 的计划依赖问题仍属范围外。

## 代表用例结果与最终对比

| 用例 | 任务成功 | E2E | Tester 请求 | Tester Token | Tester 成本（Cost） | 平均任务耗时（Task Latency） | 判定 |
| --- | ---: | --- | ---: | ---: | ---: | ---: | --- |
| D02 | 1/1 | 通过 | 6 | 70,532 | $0.00669754 | 206.814 秒 | 有效样本 |
| D05 | 1/1 | 通过 | 6 | 60,673 | $0.00494374 | 161.864 秒 | B2 检出，两个确认发现均复现成功 |
| D14 | 1/2 | 未通过 | 7 | 56,096，不完整 | $0.00539445，不完整 | 120.378 秒 | 供应商故障，无效评估 |

D14 原生 `TESTER_FAILED` 明确记录 Relace 上游共享池限流（HTTP 429），原有 `report.json` 已标记 `valid_optimization=false`、`invalidation_reason=TESTER_PROVIDER_HTTP_429`。没有替代重跑；该次数据不能进入正式质量、Token 或成本（Cost）结论。

最终 before → after：before 已有完整 16 用例，after 尚未产生。以上只是中间证据，不是最终优化成绩。

## 继续开发交接

### 已保留的修改与数据

| 文件 | 当前职责 |
| --- | --- |
| `src/web_testing_system/agents/tester_agent.py` | `PageStep`、`execute_page_steps`、工具配置、串行调用、登录准备 |
| `src/web_testing_system/runtime/web_runtime.py` | `execute_page_action`、`_row_matches_context`、缺失行定位、等待依赖检查 |
| `src/web_testing_system/runtime/candidates.py` | `PageStateReader` 的行、列、前景控件及页面摘要（Page Summary） |
| `src/web_testing_system/runtime/models.py` | 候选动作的预期值、行为标识（Behavior ID）和数据引用（Data Reference） |
| `src/web_testing_system/runtime/playwright_executor.py` | 前景操作限制和结构化页面检查 |
| `src/web_testing_system/orchestration/runner.py` | 仅接入生产 Tester 的 `prepare_task_page`，主智能体（Main Agent）算法未修改 |
| `tests/integration/test_candidates.py`、`test_web_runtime.py` | 相关行为回归测试（Regression Test） |
| `tests/integration/test_baseline_readiness.py` | 修复假客户端（Fake Client）耗尽脚本后的夹具异常，不改变任务完成判定 |

第二轮运行标识：`tester-page2-D02-f7ad0c18`、`tester-page2-D05-76f30295`、`tester-page2-D14-c87afdd7`。原始数据位于 `artifacts/runs/<run_id>/state.db` 及其同名子目录下的 `report.json`、`ground_truth_matching.json`，证据（Evidence）保留在原目录。第一轮使用 `tester-page1-` 前缀：D02 是 HTTP 429 无效运行；D05、D14 是有效质量失败，不能删除或忽略。

临时运行助手（Evaluation Helper）：`C:\Users\jack\AppData\Local\Temp\rag_eval_run.py`；统计助手（Metrics Helper）：同目录 `rag_eval_metrics.py`。统计助手会包含无效优化运行，必须按上述分类过滤。第一种方案的临时代码备份（Code Backup）：`C:\Users\jack\AppData\Local\Temp\tester-page1-x3u029jf`。临时文件可能被系统清理，仓库源码与原始运行数据才是主要依据。

### 已完成的验证

- 开发范围的 8 模块相关套件：55 项通过、4 项排除；最后一轮完整 `test_web_runtime.py`：14 项通过，21.79 秒。
- `ruff check src tests` 与 `mypy src/web_testing_system` 已通过，类型检查（Type Check）覆盖 40 个文件；差异检查（Diff Check）通过。这些是暂停前结果，本次文档修复没有重新执行业务测试。
- 较早广泛套件为 129 通过、2 失败、4 排除；两处假客户端（Fake Client）夹具失败修复后对应检查通过。之后没有完整重跑全仓库，不能合并不同轮次宣称全仓库通过。
- 初次默认套件误触发 H04 假客户端（Fake Client）夹具，发现后中止。后续显式排除 H04、H06、H08 和 `all_24_scenarios`；没有运行真实留出集（Holdout）评估。
- 暂停时没有真实评估或本地测试仍在运行，也没有开始最终 16 用例验证。
- 上一轮环境策略（Environment Policy）拒绝删除临时测试目录（Temporary Test Directory），因此保留。`.pytest-temp-dev/`、`.pytest-temp-new/`、`.pytest-temp-stage2/` 不属于产品源码。

### 恢复时的步骤和边界

1. 阅读本文件、`evaluation/baseline.md` 和用户原始要求，检查当前工作区差异（Working Tree Diff）。保留未提交修改，不重做基线（Baseline）。
2. 先依据现有记录判断耗时问题及当前方案价值。两种主要方案已经尝试，不再无限调参；不降低调用预算、运行时限或覆盖要求制造达标。
3. 冻结最终代码版本（Frozen Version）后，执行唯一一次完整 16 个开发用例（Development）评估。不得把修改前样本与修改后的运行拼成同版本全量结果，也不得反复重跑有效用例提高成绩；只有明确供应商或基础设施故障允许原样替代无效运行。
4. 固定 Main：`deepseek/deepseek-v4-flash` / `streamlake/fp8`；Tester：`z-ai/glm-5.3-flash` / `relace`；Jev：`typesafe/jev-1.13`。保留三个浏览器/Tester 并发槽位（Concurrency Slots），不启用备用模型（Fallback Model），保持既有高余量预算和安全时限。
5. 不修改场景（Scenario）、标准答案（Ground Truth）或主智能体（Main Agent），不运行留出集（Holdout）或最终优化（Final Optimization），不提交或推送。
6. 最后只更新本文件的完整 before → after。所有目标都有完整证据才能标记完成；否则如实标记尚未稳定（Not Yet Stable）。不另建汇总、历史或结果文件。
