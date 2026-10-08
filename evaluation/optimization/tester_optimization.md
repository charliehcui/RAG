# Tester Optimization

状态：新的 Runtime / Jev 职责纠正阶段已完成一次冻结的 D05/D07/D13 代表性检查（Representative Checkpoint）并停止。Task Success、Check Completion、Tester Calls 和 Replan Limit 达标，Plan Validation Failure 仍为 2/8，目标未全部达到；不追加检查或修改，等待用户确认。以下前两轮结果保留为历史，不补跑。正式比较基准为 Baseline v2。完整基线（Baseline）见 [Benchmark History](../benchmark_history.md)。

这里只记录已实现的优化方向。本地验证数字说明执行路径，不能与完整基线（Baseline）直接相减计算性能收益；成熟版本的正式数字统一记录到基准历史（Benchmark History）。

## 一次完整测试计划，异常时重新规划

历史方向（Baseline v1 时期）；其 Before 不作为当前 v2 的可比基准。此处原失败记录保留，当前正式 v2 已使用正常工具选择完成测量。

- Problem：子目标执行仍可能返回 Tester，单步工具也允许模型继续决定页面动作，没有强制先提交覆盖整个任务的测试计划。
- Why：原接口只委托局部操作，计划完整性、子目标之间的推进和异常后的恢复没有统一放在一次执行中。
- Change：新增 `execute_test_plan`，先检查全部指定行为的断言覆盖，再连续执行完整计划；Jev 决定页面动作，Playwright 执行，Python 记录断言与 Finding。页面变化由 Jev 重新观察，真正阻塞时才重新规划，保留已完成检查并复用既有两次重新规划预算。重复提交仍建立同一待完成操作的两个请求，输入引用、确定性回放（deterministic replay）、安全检查和 LangSmith 追踪（tracing）继续保留。
- Before：正式基线（Baseline）为 34.588 次 Tester 请求/任务、192.068 秒平均任务耗时、23.53% 任务成功率（8/34）；已记录 Token/任务下界为 552,489.059。
- After：18 个受影响的本地测试用例分批通过；假模型（Fake/Mock）的正常流程用一次 Tester 请求完成两个子目标及三个断言，异常流程用两次请求且不重复已完成目标；重复提交缺陷经过两次匹配复现和独立验证。预定 D02/D05/D14 的一轮真实验证在 D02 首个 Tester 请求时被 OpenRouter 404 拒绝：固定 Relace 路由不支持新增的命名 `tool_choice`。Jev 未执行，D05/D14 未启动，没有补跑；正式请求数、Token、耗时及质量收益均为 N/A。
- Final Decision：暂时保留完整计划与异常重新规划方向，当前命名工具选择与固定供应商（Provider）的兼容性问题尚未解决；本轮按 API 失败停止规则终止，不再修改或重新运行真实验证，不能视为最终成功方案，也未达到优化停止条件。完整 16 个用例的最终基准（Benchmark）未运行。

## 任务契约与计划接口一致 — 2026-10-08

- Problem：Baseline v2 的 38 次计划提交中，26 次因 Check ID、Input Reference、边界动作或计划完整性校验失败；11/14 个已尝试任务达到重规划上限。首要问题是计划未能正确交给 Jev，不能靠继续减少 Tester Calls 解决。
- Why：原工具对 checks 与 before/after 使用同一个宽泛动作结构，合法字段／动作与 Python 校验规则不一致。模型要从零散文本推导唯一检查、输入和必要顺序；异常反馈后还沿用完整对话与嵌套执行历史。
- Change：Tester 从当前确定性评分读取已完成／剩余 Check，提供检查 ID、对应行为、依赖、必要证据动作、允许输入、当前页面与已完成准备的明确 Task Contract。工具结构直接列出当前 Task 合法 ID／引用，区分 assertion 与 boundary，隐藏非本任务输入及敏感值；现有严格校验仍在任何执行前拒绝非法计划。每次计划返回后重建异常上下文，仅保留当前页面、最新失败、检查状态和合法输入；浏览器、对象绑定和 prepared Check 状态保留。Tester 仍只做 Initial Plan / Exception Replan，Jev 连续决策，Playwright 执行；不修改 Main、Runtime、模型、Provider、预算、Scenario、Ground Truth 或 Scoring。
- Before：正式 Baseline v2：Task Success 3/14（21.43%）；Check Completion 19/74（25.68%）；Tester Requests 38/14（2.714/Task）；Plan Validation Failure 26/38（68.42%）；Replan Limit 11/14（78.57%）。选定 D02/D06/D08 的原记录为 Task Success 0/3、Check Completion 0/17、8/9 次计划校验失败；只读使用原记录，不重跑 Baseline。
- After：27 个相关 local/unit 测试通过，Ruff 通过，mypy 41 个源文件通过。D02/D06/D08 各一次有效候选测量：Task Success 0/3（0%）；Check Completion 3/17（17.65%）；Tester Calls 8/3（2.667/Task）；Plan Validation Failure 0/6（相同子集原记录 8/9）；Replan Limit 1/3（33.33%），另两任务达到原有 Runtime 上限。Jev 12 次决策，浏览器执行 53 次动作（探索 26、回放 27）；Bug Precision 1/1，Recall 1/2；一个 Finding 两次复现匹配，独立 Verification 为 FAIL / RECORDED_OUTCOME_MISMATCH。耗时 2642.527 秒，记录 Tokens 273,695，Cost $0.0783224444，三条 LangSmith Trace 均完整。评分沿用原生 report/evaluation，未补跑。数据见 [Checkpoint 1](../../artifacts/runs/tester-plan-cp1-20261008-001/checkpoint_metrics.json)。
- Final Decision：保留明确 Task Contract 与计划结构约束，结构校验问题已显著减少，但任务成功目标未达到。已有计划记录显示绑定字段仍被当作页面／操作说明：D02 未进入 Jev，D06/D08 出现 LOW_JEV_CONFIDENCE 与 CONTROL_NOT_FOUND。只对同一 Plan → Runtime 接口做第二组集中修改；不重复 D02/D06/D08，不调整模型或预算。未运行 Holdout 或完整 Benchmark，正式 Benchmark History 保持不变。

## 绑定字段与操作说明分离 — 2026-10-08

- Problem：第一轮结构合法的计划仍把注册字段目的、当前页面描述和创建操作说明写入 context；必要输入无法匹配实际表单，或对象范围干扰控件／断言定位。任务成功为 0/3。
- Why：Runtime 将对象、行、表单的 context 视为稳定绑定信息，Tester 的原字段描述却允许自然语言解释；工具结构没有列出当前可用绑定范围。精简异常信息时也丢掉了未绑定字段和最后动作，下一次计划无法针对当前阻塞修复。
- Change：从既有 Task 输入与观察到的页面生成合法对象／表单／输入范围，分别提供在工具结构与 Task Contract 中；创建、注册、登录及未来唯一表单默认使用空的可选范围，操作说明只写在 goal。现有执行前校验拒绝把说明文本作为绑定范围，返回字段位置与合法范围。异常上下文只增加 pending_inputs 和最后动作的 action/control/value_reference，不传原始值或嵌套历史。计划日志补齐 project_reference、row_reference、form_context，便于区分实际绑定。保持既有 Jev、Playwright、严格评分、敏感值隐藏和原有预算。
- Before：Checkpoint 1：Task Success 0/3；Check Completion 3/17；Plan Validation Failure 0/6；Tester Calls 2.667/Task；Replan Limit 1/3。该子集仅作根因诊断，正式比较仍使用冻结 Baseline v2，不将两个不同 Case 子集当作同一测试集。
- After：29 个相关 local/unit 测试通过，Ruff 与 mypy 41 个源文件通过。冻结后 D01/D03/D04 各一次有效测量：Task Success 2/5（40%）；Check Completion 9/31（29.03%）；E2E 1/3；Tester Calls 12/5（2.4/Task）；Plan Validation Failure 0/11；5/5 初始计划结构接受，但全部在执行阶段需要异常重新规划。Replan Limit 2/5（40%），另一个任务达到原有 Runtime 上限。Jev 43 次决策，浏览器动作 116（探索 74、Replay 42），No-progress Stops 1。D04 检出 B1：Precision 1/1、Recall 1/1，一个 Finding 两次复现均匹配；独立 Verification 为 FAIL / RECORDED_OUTCOME_MISMATCH，保留实际缺陷结果，不记作验证 PASS。三条 LangSmith Trace 完整，原生 report/evaluation 指标一致，没有 Provider 失败或替换 Case。数据见 [Checkpoint 2](../../artifacts/runs/tester-plan-cp2-20261008-001/checkpoint_metrics.json)。
- Final Decision：保留已验证的契约、结构约束和绑定范围修改，代码维持冻结；按本轮两轮检查上限停止。结构失败下降 100%、Tester Calls ≤3 已满足，Task Success ≥50% 与 Replan Limit ≤30% 未满足。Check Completion 比完整 Baseline 仅高 3.35 个百分点，且低于相同 D01/D03/D04 子集的 13/31（41.94%），不能认定业务质量目标达成。不运行第三轮，不修改 Jev、Runtime、Evaluation、模型、Provider 或预算，不运行 Holdout／完整 Benchmark，不更新 Benchmark History。

最终检查的相同 Case 对照仅使用冻结 Baseline v2 的已有记录：Task Success 从 1/5 到 2/5；Check Completion 从 13/31 到 9/31；Plan Validation Failure 从 9/13 到 0/11；Replan Limit 从 4/5 到 2/5。该小样本不能替代正式 Benchmark。

| Case | Task Success | Check Completion | Tester Requests | 最终记录 |
| --- | --- | --- | --- | --- |
| D01 | 0/1 | 2/10 | 2 | CONTROL_NOT_FOUND 后恢复请求达到 MAX_RUNTIME_REACHED，保留有效质量失败 |
| D03 | 1/3 | 4/18 | 8 | Project 4/4 PASS；Task 0/8、Member 0/6 达到 Replan Limit |
| D04 | 1/1 | 3/3 | 2 | 完整删除／持久性检查通过，B1 复现两次，严格 E2E PASS |

最终检查记录 Main 14 次请求（Planning 12、Replan 0、Summary 2）；Tester Initial Plan 5 次请求、Exception Replan 7 次请求（按上传 LLM span 的父级 phase 统计，并逐 Task 与本地请求记录核对；D01 的预算阻断重规划未形成 TESTER_PLAN 提交，所以请求数 12 与计划提交数 11 不相等）。

| 组件 | Requests | Input / Output Tokens | Cost (USD) | 请求耗时合计（秒） |
| --- | --- | --- | --- | --- |
| Main | 14 | 94,682 / 10,410 | 0.005533332 | 262.147 |
| Tester | 12 | 76,955 / 214,120 | 0.110135320 | 2802.360 |
| Jev | 43 | 94,193 / 5,327 | 0.003956106 | 24.387 |
| Overall | 69 | 265,830 / 229,857 | 0.119624758 | 3088.893 |

本轮最终检查 Wall-clock 2323.090 秒（38.72 分钟）；并发请求耗时合计可以超过 Wall-clock。每 Task 的 Tester Token／Cost／Latency、页面动作及 Replay 结果均保留在上述数据文件中。缺失值仍使用 N/A，不用计划提交数替代模型请求数。

剩余根因：D03 的 7 次失败执行计划为 LOW_JEV_CONFIDENCE ×4、CONSECUTIVE_NO_PROGRESS ×1、CONTROL_NOT_FOUND ×1、CANDIDATE_EXPIRED ×1；D01 首次失败为 CONTROL_NOT_FOUND，随后受既有 Runtime 上限阻断；D04 首次失败为 LOW_JEV_CONFIDENCE 后成功恢复。Jev 本地请求记录还显示 D03/D04 对 stop_current_path 候选出现低置信度，实际操作后仍可能无法正常转入断言；因此结构合法性并未解决页面／控件语义、完成判定和候选状态恢复。首次计划直接完成任务为 0/5。后续应先审查这些执行接口及断言前置条件，再决定下一阶段；本轮不实施进一步修改，也不通过提高预算或降低检查标准掩盖问题。

本地断言（Assertion）、发现（Finding）、回放（Replay）、最终评分一致性、敏感值隐藏和 Tracing 回归通过；小规模真实检查未产生 False Positive，D04 保留稳定复现与真实验证结果，但不能据此宣称所有业务能力无回归。测量期间源代码、测试、Evaluation、配置等冻结文件哈希一致。本轮与开始快照相比，产品源代码只改 Tester；其余已有未提交改动属于前序正确性修复，未在本阶段修改。

## Jev 从执行／规划职责纠正为 bounded Decision Model — 2026-10-08

- Problem：结构合法的计划仍因 LOW_JEV_CONFIDENCE、CONTROL_NOT_FOUND、CANDIDATE_EXPIRED、CONSECUTIVE_NO_PROGRESS 返回 Tester。Jev 被要求从宽泛控件中决定下一流程，并判断业务操作是否完成；已执行操作后对 stop_current_path 的低置信度也导致重规划。
- Why：原 Jev API 已是 bounded choice，没有它生成 selector 或测试计划的证据。错误是职责分配：输入仍包含业务子流程与执行历史，Runtime 候选过宽，结束候选缺少明确执行条件；部分可恢复问题立即升级给 Tester。候选截断在当前操作过滤之前，字段名称还可能误触 Project 推断，表头与配置引用同名可能污染对象断言缓存。
- Change：现有 PageGoal 增加高层 operation 与导航 destination，不引入新框架。Tester 一次计划声明目标、稳定 Check ID、预期结果、输入和约束；Runtime 管理导航、Project 选择、稳定 Row/Form 绑定、填写与提交阶段，在排序截断前过滤当前合法候选。唯一明确候选由 Runtime 验证执行，只有多个合法选项才调用 Jev；Jev 只收到当前有限判断、页面摘要和候选，选择 ID 或 none，非法 ID／非有限置信度不得执行。完成条件由明确操作的执行与随后页面观察确定，再运行原有严格 Assertions，不再让 Jev 判断整段流程结束。缺失／过期控件、低置信度先有限重读并重新生成一次候选；相同候选不重复调用 Jev，仍无法可靠执行才升级 Tester。输入完成状态按实际页面值重验，无效重复操作沿用原有 no-progress 检测；末次动作后的额外观察不增加动作预算。精简 Replan 增加当前对象快照，保留既有已完成／剩余 Check 与合法输入；日志记录候选、恢复结果及 choice 类型，保留脱敏与 Trace 父子关系。
- Before：正式 Baseline v2：Task Success 3/14（21.43%），Check Completion 19/74（25.68%），Tester Calls 2.714/Task。上一轮 D01/D03/D04 检查：2/5 Task Success、9/31 Check Completion、0/11 Plan Validation Failure、2.4 Tester Calls/Task、2/5 Replan Limit；5/5 初始计划都需要异常恢复。原始记录的 D03 失败为低置信度 ×4、no-progress ×1、控件未找到 ×1、候选过期 ×1，D04 首次对结束候选的置信度为 0.45。
- After：96 个相关 local/unit 用例分批通过，覆盖有限选项与 none、非法选择／NaN 拒绝、缺失控件重读、页面变化后候选刷新与低置信度恢复、相同候选不重猜、实际输入被清空后的 no-progress、导航后的 Project 选择、稳定对象断言、一次初始计划完成创建／编辑／删除、原生正常／缺陷 Replay、Safety、评分一致性及 Tracing。Ruff、mypy（41 源文件）通过。冻结后 D05/D07/D13 各一次有效测量：Task Success 4/5（80%），Check Completion 18/19（94.74%），E2E 2/3，Tester Calls 8/5（1.6/Task），Replan Limit 1/5（20%）。Plan Validation Failure 2/8（25%）：NAVIGATION_DESTINATION_REQUIRED ×1、INCOMPLETE_TEST_PLAN ×1。浏览器动作 217（探索 85、Replay 132），No-progress Stops 0；Runtime 生成 36 个候选集、记录 17 次操作完成、两次对象不可绑定的重读结果均未恢复。正式 report/evaluation 指标一致，无 Provider 失败，三条 LangSmith Trace 完整。数据见 [Bounded Decision Checkpoint](../../artifacts/runs/tester-bounded-cp-20261008-001/checkpoint_metrics.json)。
- Final Decision：保留当前职责划分与有限恢复实现，代码冻结；完成唯一授权检查后停止。主要业务质量和调用预算目标达标，计划校验仍有 25% 失败，不能称全部目标达成。本轮真实 Jev Requests 为 0，候选为唯一明确动作或空集，没有为增加次数而调用；有限多候选决策／恢复只有 local tests 证据，不能宣称已实测 Jev 多选性能。D05 的缺陷使目标 Project 消失，原计划仍尝试重新选择它；两次观察确认无法绑定后升级 Tester，第三次计划又遗漏必要的剩余 Check，最终只完成 2/3。剩余问题属于 Tester 计划合法性、对象消失后的 Runtime／Tester 观测衔接；未发现 Jev 自行生成 selector 或规划的证据。Evaluation 未调整，未观察的第三项不计完成。不按结果换 Case，不运行第二次检查、Holdout、Baseline 或完整 Benchmark，不更新 Benchmark History，等待用户确认下一步。

| 本阶段 Case | Task Success | Check Completion | Tester Requests | Exception Replan |
| --- | --- | --- | --- | --- |
| D05 | 0/1 | 2/3 | 3 | 2；Project 不可绑定，最后计划缺少剩余 Check |
| D07 | 1/1 | 2/2 | 1 | 0；初始计划直接完成编辑与持久性检查 |
| D13 | 3/3 | 14/14 | 4 | 1；初始 Task 计划缺少导航 destination，补全后完成 |

相同三个 Case 的冻结 Baseline v2 原记录：Task Success 2/5、Check Completion 6/19、Tester Requests 13（2.6/Task）、Replan Limit 3/5。本次为 4/5、18/19、8（1.6/Task）、1/5；只读取旧记录，未重跑。这是小样本诊断结果，不替代正式 Benchmark。

本轮 Precision 100%、Recall 100%，全部 4 个启用的 Case/Bug 配对检出；5 个 Finding 的 10 次复现均匹配。5 次独立 Verification 均为 FAIL / RECORDED_OUTCOME_MISMATCH，记录的是仍存在的被测缺陷，不记作验证 PASS。真实样本未出现 False Positive，但不据此扩大宣称所有业务能力无回归。CONTROL_NOT_FOUND、CANDIDATE_EXPIRED、LOW_JEV_CONFIDENCE 未在本轮向 Tester 升级；自动恢复成功证据来自 local tests，真实两个重读失败是缺陷删除对象后的 PROJECT_BINDING_UNAVAILABLE。

| 本阶段组件 | Requests | Input / Output Tokens | Cost (USD) | 请求耗时合计（秒） |
| --- | --- | --- | --- | --- |
| Main | 14 | 107,379 / 11,169 | 0.007637161252 | 209.453 |
| Tester | 8 | 47,695 / 96,870 | 0.050342800000 | 1394.012 |
| Jev | 0 | 0 / 0 | 0 | 0；平均 Latency N/A |
| Overall | 22 | 155,074 / 108,039 | 0.057979961252 | 1603.465 |

Tester Initial Plan 5 次请求、Exception Replan 3 次请求，按上传 LLM span 的父级 phase 逐 Task 与本地请求记录核对。总 Wall-clock 1498.495 秒（24.97 分钟）；并发请求耗时合计可以超过 Wall-clock。单 Task Tokens／Cost／Latency 详见数据文件。测量期间代码、测试、配置、数据冻结哈希全部一致；本阶段仅修改 Tester／Runtime 候选与执行接口及相关测试／优化记录，Main、Scenario、Ground Truth、Scoring、模型、Provider 和预算保留原值。
