# Tester Optimization

## Final Development D03：断言对象预检 — 2026-10-10

- Problem：正式 D03 task-workflow 创建 d03-task-branch 后，辅助 Name 断言缺少 target/context，遭遇 AMBIGUOUS_ASSERTION_TARGET，后续必要检查未完成；该 Task 仅完成 1/8 Checks。
- Why：计划已提供 Project name → task_branch_name 的唯一操作输入，断言却丢失相同对象的范围。仅 Project 选择不能标识目标行，expected 文本也不能充当对象选择规则。
- Change：提交计划与直接 page-goals 入口先绑定断言对象：保留显式 target/对象范围，继承 Goal 的 row/context；操作后的字段断言仅在实际输入中存在唯一匹配引用时继承该引用，不从 expected 猜对象。form_context 只绑定操作表单，不能充当列断言的行对象。无对象范围的 Name/Title/Username/Owner/Role 列断言在任何操作前返回 ASSERTION_OBJECT_REQUIRED，记录 Check ID、control、target、对象与原因。before-step 不推断未来创建对象；辅助断言不被删掉，必要 Check ID 与期望保持严格。同有 control 与 target 的断言执行明确 target，Runtime 的稳定行目标优先于同名文本回退。
- Before：正式 D03 Case 为 11/18 Checks，task-workflow 初始计划失败后 Replan 受到原 Runtime 上限限制；保留全部有效失败及 Trace。
- After：本地多行页面可创建原对象并执行辅助 Name 与两个独立必要检查，包括显式创建表单的计划；目标始终是同一新行。测试覆盖计划/直接入口提前拒绝未指定对象、表单范围不能冒充目标行、错误 expected 不能改选别行、同名行仍使用显式稳定对象，以及 control 与 target 同时提供时的确定性执行。既有测试补齐明确原始对象，假 Runtime 补齐已存在的接口字段，断言标准、Check ID 和业务流程不变。完整本地结果见 [Final Readiness Summary](final_readiness_summary.md)；真实指标 N/A。
- Final Decision：保留确定性对象绑定与执行前预检，不改 Prompt、Tester 架构、模型、预算或评分。本轮不做真实测量，不把本地测试当成正式 Benchmark 通过。

## Final Stabilization 最终结论 — 2026-10-10

- Problem：Baseline 的重规划限制与不可执行计划，后续还出现作用域、目的地及无操作权限 Finding；不能靠换模型、放宽评分或加预算处理。
- Why：业务目标、计划结构、页面状态及真实证据之间的确定性契约没有闭合。
- Change：保留 Initial Plan / Exception Replan，集中补齐计划、输入、Check、对象、目的地、权限证据与只读续接；Runtime 恢复已知视图，Jev 保持有限判断，Playwright 执行，Scoring/Evaluation 不变。
- Before：保留冻结 Baseline v2 与 001–007 的有效质量失败；006 即便 7/7 Checks 仍 FP 1，007 受异常重规划后原时长限制导致复现不足，均不替换。
- After：最终冻结 D12：Task Success 2/2、Checks 7/7、FP 0、Tester 3/2 = 1.5、计划 Initial 2 / Exception 1、Validation 0、Replan Limit 0，严格 E2E PASS；B2/B6 全检出，8/8 复现匹配。新增 45 项相关测试通过；旧全量 355+安全 1 的证据保留而不重跑。最新版本仅测 D12，旧 D03/D13 成绩不合并。见 [Final Readiness Summary](final_readiness_summary.md)。
- Final Decision：FINAL_BENCHMARK_READY = YES。达到四项核心条件，停止优化、冻结代码，不再做真实 checkpoint，不运行最终 Benchmark/Holdout，不更新 benchmark_history.md。

## Final Stabilization：对象视图恢复保留原权限结果 — 2026-10-09

- Problem：007 合法剩余计划仍需 Tester 为已知 Project 的错误视图恢复，导致最终复现赶上原时长上限。
- Why：Exception Replan 不应承担已知容器恢复；恢复后不能继续使用恢复前错误页面的 Owner 快照。
- Change：Runtime 先有限恢复原已绑定 Project 容器；返回实际选中动作对应的原对象快照和非敏感 Owner/Actor 关系，Tester 的已有结果契约继续使用该原对象。高层计划、剩余 Check ID、权限准备/独立保留性检查及预算规则保持。
- Before：007 0/2 Tasks、7/7 Checks、Tester 4/2，FP 0，必要信号齐全，复现没有在原上限内完成。
- After：新增完整缺陷双会话不调用 Exception Replan，2/2、7/7、FP 0，两类缺陷全部检出且回放完成；包含未知对象、同名对象与步数限制验证。45 项新增测试最终通过，原通过内容不重测。
- Final Decision：保留 Tester = Initial Plan / Exception Replan，已知视图恢复由 Runtime 完成；最新版本待冻结验证，正式 Benchmark/Holdout 不运行。

## Final Stabilization：目的地执行与权限证据边界 — 2026-10-09

- Problem：006 两个 Task 已关闭、7/7 Checks，但 1 个 FP 使严格 Task Success 1/2。Member 的 delete 目标声明 destination=Projects，却停留 Tasks；Replan 未尝试 Delete，把项目仍存在错误归成删除越权。稳定回放不等于正确 Finding。
- Why：非 navigate 操作的显式 destination 没有执行；权限保护仅挡按钮 hidden，未挡无操作证据的 Project count/visibility。相同行为还包含准备及独立保留性检查，不能都强制删除或改写断言。
- Change：把明确目的地编译成原 before_steps 之后、实际操作之前的无评分导航，保留原顺序及全部预算；当前视图不可见时不能在导航前断言对象消失。删除权限 Finding 必须有真实操作证据；可见 Project/按钮不能证明破坏权限，真正不可用控件仍可记录保护。仅明确描述删除拒绝/不可用结果的已分配 Check 获得操作约束与原对象保留性契约，准备/刷新/Task 保留性检查独立且不改写。已观察 Owner 等于 Actor 时，非所有者探测在执行前拒绝；正常所有者/管理员删除保持原语义。
- Before：006 Task Success 1/2、Checks 7/7、Tester 4/2、FP 1；检测 B6，B2 未执行，三个 Finding 全部稳定回放，错误 oracle 被冻结 GT 正确拒绝。
- After：新增目的地 Initial/Replan、完整正常/缺陷双会话、无操作权限误报拒绝、真实不可用控件、独立准备/保留性检查及非所有者对象安全验证通过；本次续做累计 41 项新测试，原完整回归及通过的 D03/D13 不重复。Ruff/mypy/diff 通过。
- Final Decision：保留严格 GT/Scoring 和全部有效历史。等待最新冻结 D12 必要验证，不拼接旧成绩、不运行正式 Benchmark/Holdout。

## Final Stabilization：显式权限操作与只读结果续接 — 2026-10-09

- Problem：005 字段/ID 校验拒绝已为 0，但成员计划仍用 Delete 按钮隐藏替代操作证据；实际 Delete 后无评分的项目可见检查阻止正式 Check，最终 0/2 Tasks、5/7 Checks、Tester 5/2，原时长中止。
- Why：只检查检查数量不等于覆盖必要操作；无评分的操作后观察不应阻断后续只读证据。Main 的 required_operations 是业务描述，不能依赖字面 delete 枚举来识别权限任务。
- Change：按当前实际分配的稳定 Check ID、可信权限规格和角色，Initial Plan 明确包含关联的 scoped Delete；不从通用规格扩展检查。Replan 保留已执行操作及不可用控件保护。仅在已完成操作、剩余步骤全部只读时保留辅助失败并继续正式检查；提交前及后续仍有修改时严格中止。已实际尝试非所有者删除时，用操作前明确 Owner/Actor 和原稳定对象校验保留性，按钮消失不能伪装拒绝；所有者/管理员正常删除不改写。准备操作复用不覆盖原对象/权限证据，不增加预算或修改评分。
- Before：005 计划字段拒绝 0，目标/权限/辅助边界造成三次异常计划，最后检查及信号未完成；历史质量结果保留。
- After：新增 10 项必要测试通过：正常/缺陷双会话均 2/2 Tasks、7/7 Checks、FP 0；缺陷路径 Initial Plans 2、Exception Replan 0、两类缺陷全检出并完整回放。覆盖所有者权限不变、类型明确的实体行、辅助观察与真实前置条件隔离、已完成 Check 的后续安全边界、Main 业务描述兼容。Ruff、mypy、diff 通过；原完整回归及通过的 D03/D13 不重复。
- Final Decision：代码集中完成，等待最新冻结 D12 的必要验证。权限校验加强同一对象的结果标准，不降低 Evaluation；不运行最终 Benchmark/Holdout。

## Final Stabilization：异常断言契约与确定性类型补齐 — 2026-10-09

- Problem：004 中 D03 已 3/3 Tasks、18/18 Checks；D12 第一次重规划四个检查省略 action_type，导致额外模型等待并耗尽原时长。此前 CONTROL_NOT_FOUND 的具体 Check/control/object 已写事件，却未传入异常计划。
- Why：checks 本来限定 assertion/url_check，有明确目标和合法断言运算符时类型可确定；异常返回又把具体失败目标压缩成笼统原因。
- Change：仅在 checks 内、action_type 键缺失、存在明确 target/control 且提供合法断言运算符时确定补齐 assertion；显式 null/错误类型、无目标、无运算符或未知边界仍拒绝。不补 Check ID/输入/目标/预期。异常上下文保留最新失败 Check ID、control/target、对象引用和运算符，不重传完整历史，不自动猜失效对象。实体控件名不再通过后缀误匹配动作按钮，未明确的 Member 目标不会被 Add Member 按钮冒充。
- Before：004 D03/D12 为 3/5 Tasks、19/25 Checks、FP 0、Tester 8/5；D12 为 0/2、1/7，初始控制描述失败、一次 Schema 拒绝及原时长中止，均保留有效历史。
- After：新增 11 项相关测试通过，含确定性类型、严格拒绝、失败目标上下文、未来行创建及实体/按钮隔离；完整本地双会话重现“已撤权但语义断言目标错误→重规划”，不重复撤权、覆盖所有剩余 Check、保留信号和自动回放。冻结 GT 后验为 2/2 Tasks、7/7 Checks、FP 0、两类缺陷全部检出。D03、D13 和此前通过的完整回归不重跑。修复版本必要真实验证只针对尚失败的 D12，不拼接版本成绩。
- Final Decision：保留原架构和全部严格条件。最新版本尚未完成真实验证，不标记 ready；不运行正式 Benchmark/Holdout。

## Final Stabilization：显式检查依赖与提交阶段 — 2026-10-09

- Problem：已有有效 D13 记录中，pending-settled 放在 checks，而其依赖的 repeat_submit 放在 after_steps；预检正确拒绝顺序，但引发一次不必要重规划。
- Why：模型输出的列表位置与稳定 Check ID 的明确依赖不一致。完整覆盖检查并不保证可执行顺序。
- Change：仅在当前 checks 全部通过明确依赖指向同一个 after_steps.repeat_submit 时，将这些结果检查放到提交及紧邻等待之后、原刷新边界之前。保留全部 Check ID、动作、等待、刷新和检查标准；辅助前置检查或无依赖检查不移动。Initial Plan / Exception Replan 共用此编排，不修改 Prompt、模型或预算。
- Before：上一冻结版本 D03/D12/D13 为 5/8 Tasks、27/39 Checks、FP 0、Tester 13/8；D13 自身 3/3、14/14，但存在一次 CHECK_DEPENDENCY_ORDER_REQUIRED。
- After：新增 Initial/Replan 编排及依赖边界本地测试通过。已通过的 D13 和原完整回归不重跑；下一次必要真实验证只覆盖修复后仍失败的普通流程与实时双会话，不将不同版本结果拼成最终达标数字。
- Final Decision：保留确定性编排，历史有效结果不替换。当前尚未完成修复版本的真实验证，不能标记 ready；不运行最终 Benchmark 或 Holdout。

## Final Stabilization 继续：可执行契约与权限证据 — 2026-10-09

- Problem：上一轮真实检查的 6/19 计划提交遗漏 assertion target / expected state / navigation destination；非法等待在已执行操作后才拒绝；D12 把按钮可见误报为删除越权。
- Why：输出 Schema 没有完整表达执行前校验；可见控件与业务操作结果不是同一证据，稳定回放也不能证明错误 oracle 正确。
- Change：Schema 明确 assertion target/control、expected/reference 与导航条件；整份计划在执行前检查现有等待上限。权限规格下，当前可用按钮的 hidden 断言不能形成权限 Finding，返回明确操作证据缺失原因；真正不可用控件仍可记录保护。提供原有可信 expected_behaviors，不新增必测目标。语义导航保留显式 Project 引用。后续集中修复非断言边界的无关 null 运算符；空字符串使用 JSON 明确记录，Finding 预期从实际断言事件读取。辅助失败仅允许继续原计划的只读观察，禁止后续修改、漏检或发布成功信号。
- Before：D03/D12/D13：Task Success 5/8、Checks 27/39、Tester Calls 19/8、False Positive 1。
- After：首组集中修改后全量本地测试 350/350 通过。冻结的 D03/D12/D13 验证为 Task Success 5/8、Checks 38/39、Tester 14/8、FP 1。D03 暴露导航跳过显式 Project 输入；D12 空字符串证据被误当成缺失而多发起无效 Replan，管理员辅助前置失败也造成不必要 Replan；D13 非断言 null 运算符有两次 Schema 拒绝，最终 3/3 Tasks、14/14 Checks、FP 0。所有结果保留为有效质量测量。后续相关 111 个本地测试通过，完整双会话缺陷路径改为两份 Initial Plan、无 Exception Replan；最终全量回归和下一份冻结验证进行中，尚不宣称 ready。
- Final Decision：按用户新的继续指令集中修复并验证，直到四个核心指标同时达标；正式 Development Benchmark 和 Holdout 不运行。此前一次检查及失败结果保留。

## Final Stabilization：完整计划、辅助边界与恢复契约 — 2026-10-09

- Problem：最新 D12 四份计划已列齐必要 Check ID，仍被 CHECK_ID_REQUIRED 拒绝；恢复只记录 prepared_check_ids，可能跳过不同操作或丢失已完成检查后的信号。
- Why：逐步检查 behavior 标记早于整份计划完整性判断；Check 完成、操作已执行、后续边界及信号发布被混为同一状态。辅助断言失败也可能被继续执行，历史计划失败缺少具体目标信息。
- Change：先严格校验全部剩余 Check ID、动作、输入和顺序；合法 ID 的冗余 behavior 由程序补齐，冲突、重复和真实遗漏仍拒绝。完整覆盖之外的辅助断言/刷新保持未评分，辅助失败阻止危险后续动作。仅复用相同操作、对象引用和输入的已执行准备；完成检查后的边界与信号继续处理，信号幂等发布，Replan 不得丢弃已接受计划的未发布信号。明确导航边界交给 Runtime。记录 Check ID、control/target、对象和失败原因，不修改 Prompt、模型或预算。
- Before：Runtime Checkpoint D12 为 0/2 Tasks、1/7 Checks，Tester 6 Calls；四次拒绝摘要均覆盖必要 ID。此前整 Goal 跳过会忽略 publisher。
- After：344 个当前本地测试最终全部通过（全量回归加相关修改后的针对性回归）；完整双会话正常及缺陷流程、本地自动跨角色 Replay、严格评分/报告一致性、Tracing 和 Safety 均覆盖。开发阶段真实 LLM 调用 0。冻结后唯一一次 D03/D12/D13 检查：Task Success 5/8（62.50%）、Checks 27/39（69.23%）、Tester 19 Calls（2.375/Task），Initial 8、Exception Replan 11；两个 Task 停在 Replan Limit（25%）。Check ID / Input Reference 拒绝为 0，但缺 Target ×4、缺 Expected State ×1、缺 Navigation Destination ×1，合计 6/19 计划提交被预检拒绝；另有一份进入执行的计划在动作发生后因非法 wait duration 返回。所有被接受计划均覆盖剩余必要 ID，未通过 Check 并未被伪造为完成。
- Final Decision：FINAL_BENCHMARK_READY = NO，停止，不进行第二次真实检查或进一步代码修改。剩余 Tester / Plan Contract 问题是断言目标/预期和导航边界没有完整反映到输出约束，等待限制还在执行阶段拒绝，以及业务测试语义校验不足。D12 把 Delete 按钮隐藏当作删除权限目标，没有实际触发 Delete；该错误断言虽能稳定回放，仍是 FP，正式评分正确让成员 Task FAIL。重复 Project 候选导致的 Jev 低置信度是 Runtime 问题，不能只归因于 Tester。详见 [Final Readiness Summary](final_readiness_summary.md) 和 [Native Review](../../artifacts/runs/final-readiness-cp-20261009-001/final_readiness_review.json)。正式 Benchmark/Holdout 与 benchmark_history.md 均未运行或更新。

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

## Plan / Replan Completeness — 2026-10-08

- Problem：已有 D13 初始计划遗漏后续 Tasks 导航 destination；D05 删除缺陷使原 Project 消失，重规划继续选择旧 Project，最后又漏掉 D05.task-preserved。完整性拒绝虽已存在，但工具结构未表达全部剩余 Check 的覆盖要求，current_object 仍可能包含缓存的绑定意图。
- Why：必要 destination 是可选字段；同一行为的后续检查需要逐个覆盖。对象缓存用于保持断言身份，不能直接当作当前对象存在的证据。修复当前阻塞也不能删掉后续必要 Check。
- Change：现有工具结构要求显式 destination（非导航为 null），导航约束非空；按规范评分的已完成 Check 生成每个剩余 Check 的结构覆盖约束。沿用严格 Input/Check 预检，补齐导航边界目的地与本 Task 的必要 Check 顺序校验。计划整体合法后才执行，记录 TESTER_PLAN_VALIDATED 的实际剩余/计划 Check ID。页面摘要重新核对稳定行目标、Project 选项和当前选择，区分 present、unavailable、not_observed；每个操作前再次拒绝已不可用的对象。原始断言目标缓存保留，检查原对象偏差不替换对象、不伪造完成。Exception Replan 只接收当前评分的完成/剩余项、新页面/对象状态及最新失败。仅修改 Tester 接口；Runtime/Jev/Playwright、Main、Evaluation、模型、Provider 和预算不改。
- Before：上一阶段 D05/D07/D13 检查为 Task Success 4/5、Check Completion 18/19、Tester Requests 8/5、Plan Validation Failure 2/8、Replan Limit 1/5；两类校验失败分别为 NAVIGATION_DESTINATION_REQUIRED 和 INCOMPLETE_TEST_PLAN。只读取旧结果，不重测作为 Before。
- After：99 个相关 local/unit 测试分批通过，覆盖缺失导航、剩余下游 Check 遗漏、依赖倒置、对象删除/改名/视图未打开的区别、执行中消失后阻止旧绑定、原对象 Finding，以及既有 Assertion/Replay/Safety/Scoring/Tracing 回归。Ruff 与 mypy（41 源文件）通过。冻结后唯一检查 D03/D12 各一次有效测量：Task Success 2/5（40%），Check Completion 13/25（52%），E2E 0/2，Tester Requests 9/5（1.8/Task；单任务最高 3），Initial/Exception 请求为 5/4，实际计划提交 8 次，Preflight Validation Failure 0/8。8/8 提交均覆盖提交时全部剩余 Check，所有 navigate 有明确 destination；一条预算阻止的请求未形成计划提交，不能从提交数猜模型请求数。MAX_TASK_REPLANS_REACHED 停止为 1/5（20%）；D12 另一个任务重规划用量也达到 2，但以 LIVE_PARTICIPANT_DEPENDS_ON_OBSERVER 停止，不计入该停止原因比例。4 个 Task 未启动（官方已启动 Task 为成功率分母；全部 9 个原任务/后续任务均计入则为 2/9）。Browser Actions 76，Jev Requests 0，No-progress Stops 0；未强行调用 Jev。两条 LangSmith 树关闭、父子关系完整，report/evaluation 指标一致，Provider/API 失败 0，无替换。测量期冻结文件哈希全部一致。D12 首次在当前优化阶段测量；不同子集不能直接比较整体百分比。数据见 [Completeness Checkpoint](../../artifacts/runs/tester-completeness-cp-20261008-001/checkpoint_metrics.json) 和 [Completeness Review](../../artifacts/runs/tester-completeness-cp-20261008-001/completeness_review.json)。
- Final Decision：保留本轮确定性覆盖/顺序/已知不可用对象校验，代码继续冻结。本轮结束，不再真实测试、不追加修改。检查覆盖与导航遗漏未复现，但 Task Success ≥80%、Check Completion ≥90% 未达标；不能按全部质量目标宣布 Tester Optimization 正式完成。0/8 只表示现有预检未拒绝计划，不证明语义/前置条件已完整：D12 的 Tasks 导航未提供 project_reference，仍被接受并在执行时遇到 PROJECT_BINDING_UNAVAILABLE。D03 的 D03.member-project-created 断言连续 CONTROL_NOT_FOUND，完整重规划仍无效；现有事件未记录失败断言的具体 control/target，不能臆测具体定位器。D12 原 Main DAG 将管理员依赖于整个成员任务，而成员要等待管理员撤权，触发 LIVE_PARTICIPANT_DEPENDS_ON_OBSERVER；既有 Main Replan 生成后续任务后，准备任务再次缺少 Project 绑定并以 MAX_RUNTIME_REACHED 停止，保留全部历史失败和未启动任务。剩余根因分别在 Tester/Runtime 断言契约、未来视图必需 Project 绑定校验、Main 双会话调度；本轮不修。单任务 3 次 Tester Calls 仍存在。Local Assertion/Finding/Replay/Safety 回归通过；真实检查无 Finding，Replay/Verification 未触发（N/A），Precision N/A，Recall 0/2，不能宣称真实回放或缺陷发现已无回归。不运行 Baseline/旧检查组合/Holdout/完整 Benchmark，不更新 Benchmark History，等待用户确认。

| Case | Task Success（已启动 Task） | Check Completion | Tester Requests | 最终结果 |
| --- | --- | --- | --- | --- |
| D03 | 2/3 | 12/18 | 5 | Project/Task 初始计划直接完成；Member 断言无法解析，Replan Limit |
| D12 | 0/2 | 1/7 | 4 | 跨会话依赖冲突、缺少 Project 绑定、后续准备超时；另有 4 个未启动 Task |

Tester Initial Plan 5 次请求、Exception Replan 4 次请求。按上传 LLM span 的父级 TesterPlanGeneration.phase、tester_id 与本地 tasks.assigned_tester 关联，逐 Task 核对请求数；task_id 可能按既有规则脱敏，不用未匹配名称猜分阶段次数。总 Wall-clock 1738.205 秒（28.97 分钟），LLM Requests 21，Input/Output Tokens 235,085/169,312，总成本 USD 0.089116532961。Main 12 次请求、Tester 9 次、Jev 0 次；各 Task 用量及停止原因保留在数据文件中。Replay/Verification 和 Jev 多候选决策的真实性能在本轮为 N/A。
