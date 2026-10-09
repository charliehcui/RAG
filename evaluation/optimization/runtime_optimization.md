# Runtime Optimization

## 对象绑定、断言契约与实时信号（Binding / Assertion Contract / Live Progress）— 2026-10-09

- Problem：已有 Main Budget Checkpoint 的 D12 Task Success 0/2、Check Completion 4/7，两个 Task 达到 Tester Replan Limit。计划跨度记录 PROJECT_BINDING_UNAVAILABLE 两次、PROGRESS_SIGNAL_NOT_AVAILABLE 两次、INVALID_ACTION 一次与 CHECK_ID_REQUIRED 一次；无目标断言还进入了后续 Replay。D03 历史检查三次 CONTROL_NOT_FOUND，但未保存失败 control / object，无法从已有记录精确还原其字段名称。只读使用这些结果定位，没有重新运行 Case。
- Why：navigate 动作已完成后仍要求绑定页面上的 Project 下拉框，导致仅打开 Tasks 的合法操作被错误阻塞；部分绑定依赖原名称、容器或旧控件定位器，没有稳定保存对象 ID。无目标断言直到 Playwright 才被拒绝。进度等待只查询最近 100 条事件、按子串匹配并在短暂等待后返回失败，没有区分生产者仍运行、已关闭和信号真正不可产生。原有有限刷新没有覆盖断言项目绑定暂时缺失及执行期间控件重绘。
- Change：集中修改既有 Runtime，不改 Main / Jev / Evaluation / 模型 / Provider / 预算。导航与 Project 选择分开；保存观察到的 Project / Row ID，并在新的页面和容器下重新绑定同一对象，字段别名仅按当前实际实体和表头对应。Project 下拉框用稳定 ID 恢复，显式指定 Project 的断言允许恢复同一个 Project；真正不可用的对象标记为当前视图 unavailable，不换成其他对象，也不把不可见直接解释成全局删除。Runtime 在整份计划执行前校验 Assertion Target 与 Expected State；缺失目标返回 ASSERTION_TARGET_REQUIRED，记录 Check ID、control / target、object 和原因，不猜定位器、不制造 body 断言。错误页面上的缺失控件不能形成隐藏断言成功。CONTROL_NOT_FOUND / CANDIDATE_EXPIRED / PROJECT_BINDING_UNAVAILABLE / LOW_JEV_CONFIDENCE 先使用既有有限重新观察和候选刷新；执行期间重绘导致的确切缺失控件同样恢复，安全拒绝和真正的 Assertion Failure 不重试。
- Change（Progress）：信号留存在既有 SQLite 事件中，登记明确生产者及关联检查，按确切名称读取，不一次性消费，不因生产者随后失败或历史超过 100 条而丢失。共享 StateStore 的轻量事件通知唤醒 Runtime 等待，不使用 sleep 轮询。正式实时流程在生产者仍可推进时保留当前 continuation；生产者关闭、已登记计划不产生所需信号、真实等待环或原 Run / Task 运行预算耗尽时返回明确原因，不提高 timeout、Step / Replan Budget。Tester 中原等待工具仅转交 Runtime，并接入 Runtime 的整计划断言预检和信号登记；唯一 Tester 逻辑 safeguard 是实际剩余检查遗漏 Check ID 时确定性拒绝并列出剩余 ID。Tester Prompt、输出 Schema、规划架构不改。
- Before：D12 预算已为 90/90，Main Requests 2、Main Replan 0、两个必要 Task 均启动，成员就绪→撤权→项目访问检查的前半段顺序正确，后续 tasks / delete / preservation 未完成；Tester Requests 6（3/Task）。这是执行层失败，不能再归因于 Main 预算或依赖。原始记录见 [Previous Checkpoint](../../artifacts/runs/main-budget-cp-20261009-001/checkpoint_metrics.json)。
- After：保存的 XML 汇总有 87 个不同本地测试最终通过（冻结前 85，检查结束后的 safeguard 作用范围补充 2），Ruff、mypy（41 源文件）和 git diff --check 通过；另有既有 Main / bounded decision / tracing 回归验证。覆盖临时缺失与真实当前视图不可用、原对象重命名/容器变化/删除、Project / Row / Owner 字段隔离、断言执行前拒绝、错误视图不能误判保护、控件重绘恢复及冻结评分对 recovered error 的一致处理、信号生产者退出/重复读取/超过 100 条/错误信号/取消等待/短暂未就绪、双会话交接、稳定 Check ID safeguard，以及 Finding / Replay / Verification。开发阶段真实 LLM 调用 0，未重复已通过且实现未变化的测试。详见 [Local Validation](../../artifacts/runtime-reliability-20261009/local_validation.json)。唯一一次冻结版本 D12 测量为 Task Success 0/2、Check Completion 1/7（14.29%）、E2E FAIL；Tester Requests 6（3/Task），两个 Task 均达到 Replan Limit。四次计划拒绝均为 CHECK_ID_REQUIRED，但这些计划摘要列出的必要 ID 集已覆盖各角色全部 required Check；不能把拒绝次数直接解释成四次遗漏 required Check。管理员三份计划均未进入页面操作，成员首份计划被拒绝、随后完成 member-ready，再因生产者关闭而无法继续。属于有效 Agent Quality Measurement，Provider/API 失败 0，未替换或补跑。
- Final Decision：本轮已停止，未达到 Task Success ≥70% / Check Completion ≥80% 的整体目标，不宣布 Runtime Optimization 成功。确定性运行时恢复在本地通过，但本轮管理员尚未进入执行，不能据零 PROJECT_BINDING_UNAVAILABLE 就声称完整 binding 流程已实测成功。已发布的 member-session-ready 保留；成员等待 membership-removed 104.52 秒，管理员因计划拒绝关闭后返回 PROGRESS_PRODUCER_CLOSED，未错误报告信号丢失，也未因短暂等待反复调用 Tester。剩余主要阻塞在 Tester / Plan Contract：额外断言的 Check ID 约定与 safeguard 的作用范围；历史摘要没有保存完整断言内容，无法逐项确认四次拒绝中哪些来自额外声明 behavior 的未标 ID 断言，哪些来自过宽防护。检查结束后仅作必要的确定性修正：保留原有“声明 behavior 就必须有合法 ID”校验，将新增 safeguard 限定为 Exception Replan 实际缺失剩余必要 ID，全部 required ID 已覆盖时不再因未参与评分的辅助断言误拒绝。补充 Initial/Replan 辅助断言和真实缺 ID 测试通过，未修改 Prompt / Schema / Runtime 恢复代码，未再次真实测量；最终版本只有本地验证，不能套用旧冻结版本的成功率宣称达标。Main 的预算/依赖/启动保持正确，未发现 Evaluation / Scoring 修改或新的 Main 问题。不再继续 Runtime 修改或真实测试，等待用户决定后续计划契约问题的处理；不运行 Baseline、旧 Checkpoint、Holdout 或完整 Benchmark，不更新 benchmark_history.md。

| 指标 | 唯一一次 D12 检查 |
| --- | --- |
| Task Success / Check Completion | 0/2；1/7（14.29%） |
| 必要 Task 启动 / execution budget | 2/2；90/90 |
| Main Requests / Main Replan | 2（Planning 1、Summary 1）；0 |
| Tester Requests / Task | 6/2 = 3 |
| Plan submissions / validated | 6 / 2（均为成员计划） |
| Plan validation rejection | CHECK_ID_REQUIRED ×4；必要 ID 集均有覆盖 |
| Runtime 等待退出 | PROGRESS_PRODUCER_CLOSED ×2；不是信号丢失 |
| PROJECT_BINDING_UNAVAILABLE / no-target INVALID_ACTION | 0 / 0；未完整进入后续绑定流程 |
| Browser Actions | 13，均成功；含准备操作 |
| Findings / Replay / Verification | 0；实际回放和验证 N/A，已有本地回归测试通过 |
| Jev | 0；没有为了增加使用次数虚构调用 |
| LangSmith | 49 个 Span，同 Trace、父子关系完整，原生计数一致 |

测量期间全部冻结文件哈希一致，正式 report/evaluation 的评分结果一致；结束后只有上述确定性 safeguard 和对应 local test 改动，另同步本文档。Main 源码、模型、Provider、预算、Scenario、Ground Truth、Scoring、Holdout、tester_optimization.md、main_optimization.md 和 benchmark_history.md 均保持本轮开始时的内容。检查结果保留原版本，不改写为修正后的结果。

数据见 [Checkpoint Metrics](../../artifacts/runs/runtime-reliability-cp-20261009-001/checkpoint_metrics.json)、[Runtime Review](../../artifacts/runs/runtime-reliability-cp-20261009-001/runtime_review.json)、[Main Budget / Dependency Review](../../artifacts/runs/runtime-reliability-cp-20261009-001/main_budget_review.json) 与 [Tracing Review](../../artifacts/runs/runtime-reliability-cp-20261009-001/tracing_review.json)。总体 LLM Requests 8（Main 2、Tester 6、Jev 0），Input/Output Tokens 47,753/142,282，总成本 USD 0.072491968046，Wall-clock 846.526 秒（14.11 分钟）。本阶段只有一个集中 Runtime 实现方向和一次真实检查；结束后的 safeguard 作用范围纠正仅用已有证据与本地测试，没有新一轮真实优化。
