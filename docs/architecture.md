# 架构与数据流

这份说明对应当前生产代码。旧计划放在本地 `docs/history/`；测量时的历史状态保留在 `evaluation/`，不能用它们推断当前执行流程。

## 从哪里开始读

1. [config.py](../src/web_testing_system/config.py)：输入、身份引用、必要检查与预算。
2. [orchestration/runner.py](../src/web_testing_system/orchestration/runner.py)：完整测试如何组装与收尾。
3. [main_agent.py](../src/web_testing_system/agents/main_agent.py) → [scheduler.py](../src/web_testing_system/orchestration/scheduler.py)：任务从规划到并行启动。
4. [tester_agent.py](../src/web_testing_system/agents/tester_agent.py) → [web_runtime.py](../src/web_testing_system/runtime/web_runtime.py)：完整计划如何变成实际操作。
5. [reproduction/replay.py](../src/web_testing_system/reproduction/replay.py) → [verification/runner.py](../src/web_testing_system/verification/runner.py) → [reporting/builder.py](../src/web_testing_system/reporting/builder.py)：从异常证据到最终报告。

## Main、Harness 与调度

`run()` 建立 SQLite Run、预算与追踪上下文（Tracing Context），然后调用 `MainAgentRunner.create_initial_plan()`。Main 使用 `create_harness_agent()`，只暴露受范围约束的规划工具（Planning Tool）。它不操作浏览器。

有必要检查（Required Check）时，`configure_workflows()` 按 `goal_id` 编译连续工作流。Main 提交 `submit_task_plan()`，Python 检查完整覆盖，并绑定身份、输入键、检查和依赖。任务步数来自固定配置，不能由模型估计。

`LocalTesterScheduler` 领取优先级最高且依赖已完成的任务，以 AsyncIO 持续填补空闲槽位。每个任务得到独立的 Tester、Session、BrowserContext 和 Runtime。并发数量默认 3，可配置为 1–4；整个运行创建的实例总数可以超过并发上限。

Main 重规划只在有待执行任务且出现相关请求或高风险 Finding 时触发。运行中和已完成的工作不会因重规划被重新分配或重复执行。

## Tester → Runtime → Jev → Playwright

正常路线由 `TesterRunner._run_complete_plan()` 构建当前任务契约（Task Contract），只暴露 `execute_test_plan`。Tester 提交 `PageGoal`、输入引用与 `PageStep`，Python 在任何浏览器操作前校验完整计划。

```text
Task Contract
→ Initial Plan
→ preflight: references / scope / object / checks / order
→ business goals + deterministic boundaries + assertions
→ finish_task

Execution blocker
→ current page + completed checks + remaining checks + latest failure
→ Exception Replan for the remaining plan
```

`project_reference` 指向项目，`row_reference` 指向原有行，`form_context` 约束实际观察到的表单。它们不能由断言期望值反推。Runtime 保留稳定对象绑定（Object Binding），避免改名、删除或同名行导致检查换了对象。

`WebTestingRuntime.execute_page_goal()` 管理观察、定位、填表和提交阶段。`PageStateReader` 读取实际页面，`CandidateBuilder` 生成当前合法动作。已知动作和唯一合法动作直接执行；需要选择时 `JevSelector.select()` 从给出的候选 ID 中选择一个或返回 none。当前使用的是 `choice` 决策，没有在这条生产路径中实现 boolean/scoring 决策。

候选执行前再次检查页面状态、目标与权限。低置信度、失效候选或无可用动作不会直接操作页面。Runtime 负责有界恢复（Bounded Recovery），无法继续时把异常交给 Tester；Jev 不规划任务或恢复整段工作流。

`PlaywrightExecutor` 执行动作与确定性断言（Deterministic Assertion），记录输入引用、实际结果、错误类型和时间。每个页面有自己的锁（Page Lock），同一页面的操作串行，不影响其他 Tester 的独立页面。

## Shared State 与跨会话交接

[StateStore](../src/web_testing_system/state/store.py) 是执行事实的来源，主要保存：

| 数据 | 使用者 |
| --- | --- |
| Run、Task、Tester、Identity、Budget | Main、Scheduler、Runtime |
| Path、Progress、Checkpoint、Event | Tester 协作、恢复与追踪核对 |
| Action History、Finding、Evidence 索引 | 评分、回放、验证与报告 |
| Resource 与权限信息 | 调度和动作执行前的范围检查 |

连续双会话流程不能拆成完成依赖：例如成员保持登录，管理员撤权，再由同一个成员会话检查访问。Runtime 通过已记录检查和 `publish_progress` / `wait_for_progress` 同步，Shared State 的更新通知唤醒等待者。信号生产者退出或预算耗尽时停止等待。

输入值只在 Runtime 中解析。密码使用 `env:` 引用，操作历史保存引用；证据（Evidence）由文件保存，SQLite 只保存索引。

## 从 Finding 到报告

必要断言失败时，Tester 立即建立 Finding，并通过检查 ID 和浏览器事件绑定证据。`FindingService` 执行筛选与去重。所有探索结束后才复现，避免 Demo 的全局重置干扰活跃 Tester。

`ReplayPlanBuilder` 从实际历史截取异常之前的操作，并补齐必要参与者与前置检查。`DeterministicReplay` 用新浏览器会话和原身份、输入引用执行；有重置钩子（Reset Hook）时先重置应用。

默认最多 3 次复现中有 2 次匹配，进入 `REPRODUCED`。随后 `VerificationRunner` 独立回放，把目标失败断言改为检查正确预期：实际断言失败得到 `CONFIRMED_BUG`，正确预期通过得到 `CLOSED`；无法执行或缺少确定性依据则保持 `NEEDS_CONFIRMATION`。

[scoring.py](../src/web_testing_system/scoring.py) 按实际检查 ID、身份、顺序与确认的 Finding 计算结果。检查完成（Check Completion）与业务通过是不同概念。Runtime、报告与正式评估（Evaluation）使用同一评分函数；正式评估在运行结束后再用 Ground Truth 匹配缺陷。

`FinalReportBuilder` 生成确定性 `report.json`。同一个 Main 清空工具，在新会话中生成 `summary.md`。总结失败仍保留结构化报告并显式标记运行失败。最终报告包含模型配置、并发、各阶段请求与用量、费用完整性和最终总结调用的消耗。

LangSmith 中间件和显式阶段跨度（Span）覆盖上述流程。追踪仅上传允许的元数据；它不参与任务调度、评分或缺陷判定。
