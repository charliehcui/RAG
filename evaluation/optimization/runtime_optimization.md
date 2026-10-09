# Runtime Optimization

## Final Stabilization 最终结论 — 2026-10-10

- Problem：绑定/字段/视图状态及权限 oracle 不稳定，辅助步骤又使剩余必要证据和信号无法完成。
- Why：对象身份、控件类型、实际操作及必要检查缺少明确的一致关系。
- Change：确定性绑定原对象、合并重复候选、刷新和有限原容器恢复、真实字段值与目标校验、权限对象及操作证据、保持严格 mutation 前置条件的只读观察续接；保留信号、回放及清理。
- Before：最新 007 虽完成 7/7 Checks 和信号，但两个 Task 的 Finding 未在原上限内复现，严格 Task Success 0/2；历史记录不替换。
- After：最终冻结 D12 2/2 Tasks、7/7 Checks、FP 0、B2/B6 Recall 100%、Replay 8/8；三条信号顺序正确，非法动作/无目标断言/安全越界 0。一次真实原 Project 删除后的额外只读步骤触发 Admin Replan，收尾成功而无最终阻塞。415 Span 父子与原生计数一致，report/evaluation 同源评分；预算始终不变。
- Final Decision：FINAL_BENCHMARK_READY = YES，立即停止 Runtime/Tester 优化。仍有对象真实消失后的一次额外只读 Replan、模型服务时延及回放动作数量，保留为小问题，不为更漂亮数字继续改。详见 [Final Readiness Summary](final_readiness_summary.md)。

## Final Stabilization：原对象容器的有限视图恢复 — 2026-10-09

- Problem：007 字段/计划拒绝和 FP 已为 0，7/7 检查完成，但已知 Project 在隐藏 Projects 容器而当前 Tasks 视图无法执行 Delete；两次异常计划后接近原 900 秒上限，复现缺少时间，严格 Task Success 0/2。
- Why：当前不可见被当成没有候选，却已有稳定原对象与容器。Runtime 没有使用这些确定信息恢复对象视图，仍让 Tester 重新寻找流程。
- Change：仅在原 Project 稳定 ID、原容器和唯一当前合法 Projects 控件都已明确时恢复原容器并重新观察；每个 Goal 最多一次，计入现有 Goal/Task 步数，不切换同名其它对象。未知绑定不猜，预算不足不执行后续 mutation。在实际已选择动作的原行快照保留对象 ID 与 Owner/Actor 关系，使恢复后的权限断言仍检查同一原对象，避免错误视图下的提前快照丢失关系。
- Before：007 0/2 Tasks、7/7 Checks、FP 0、Tester 4/2；三个信号齐全，复现均因原 Runtime 上限未形成有效匹配，结果保留。
- After：新增 4 项测试通过，包括同名对象不替换、未知绑定拒绝、原步骤上限及完整缺陷双会话；后者 2/2 Tasks、7/7 Checks、FP 0、Recall 100%、两份 Initial Plan、无 Exception Replan，完整 Replay/Verification 通过。本次新增测试累计 45 项，Ruff/mypy/diff 通过，未重复旧完整回归或通过的 D03/D13。
- Final Decision：只补现有绑定恢复接口，不提高预算、改模型或让 Jev 规划；等待新冻结 D12 的必要验证，不运行最终 Benchmark/Holdout。

## Final Stabilization：明确视图与未执行操作证据 — 2026-10-09

- Problem：006 在 Tasks 执行目标为 Projects 的 delete，候选为空；后续把读访问仍存在当作破坏权限，FP 1。Project 名称断言放在隐藏视图后也触发一次前置条件恢复。
- Why：目的地声明未进入操作前的页面动作，页面暂不可见与对象消失混淆；权限结果缺少实际 destructive operation 的前提。
- Change：执行显式导航后由 Runtime 重新观察和绑定，再产生实际操作候选；不在导航前根据旧视图拒绝对象。未实际删除时，原 Project 存在或按钮可用不能形成删除权限 Finding，严格返回证据缺失；控件确实不可用、原对象范围明确时仍可记录保护。非所有者探测拒绝当前明确 owned 对象，不自动换其它 Project。准备及独立后续 Check 不套用删除结果规则。
- Before：006 1/2 Tasks、7/7 Checks、FP 1，全部源于未执行的权限 oracle；Main 启动/依赖/预算无回归，API 失败 0。
- After：最新相关单元和正常/缺陷完整集成通过，GT 后验 FP 0；冻结配置/数据/评分未修改。本次新增 41 项验证与静态检查通过，最新实际结果待冻结验证。
- Final Decision：只修明确目的地、对象与证据契约，不提高预算，不改 Jev/模型/Main，不运行正式 Benchmark/Holdout。

## Final Stabilization：实体身份与权限结果的稳定对象 — 2026-10-09

- Problem：Member 实体简称在已有明确行引用时仍 CONTROL_NOT_FOUND；真实删除后，辅助可见检查阻断剩余证据；按钮消失可能被错误当成删除被拒绝。
- Why：字段标签与实体类型混用；观察阶段和修改前置条件没有分开；权限结果没有明确锚定操作前的对象及所有者关系。
- Change：Project/Task/Member 的可见/隐藏实体检查只使用相应类型、明确引用、稳定 ID 和当前 Project 范围的唯一原数据行；消失后检查同一原行，不换对象。操作后只读辅助失败保持记录，必要检查继续；未来有 mutation、repeat-submit 或前置检查时不跳过。明确观察到非所有者身份和 Owner 后，删除权限结果检查原稳定对象保留；自己的对象及 Admin 不改写。重新执行/恢复不会清空原权限对象证据，预算和候选职责保持不变。
- Before：005 D12 0/2 Tasks、5/7 Checks，实际原项目 Delete 已发生，最后检查/信号被辅助观察阻断后时长耗尽；不当作基础设施失败替换。
- After：新正常/缺陷双会话集成通过，信号顺序完整；缺陷路径无 Replan、GT Precision 100%、Recall 100%、全部检查完成。新增真假对象/所有者/后续修改安全测试通过，无 Selector/Scoring 标准放宽。最新真实验证待冻结后进行，仅 D12。
- Final Decision：保持架构，Runtime 根据确定的对象/权限契约执行和记录，不由 Jev 规划；当前不运行正式 Benchmark/Holdout。

## Final Stabilization：创建对象与已有行的输入隔离 — 2026-10-09

- Problem：004 D03 创建任务把未来 row_reference 当作已有编辑行，先阻塞 Title 输入；实际通过一次重规划完成 3/3 Tasks、18/18 Checks。
- Why：输入筛选及提交前置条件依据 row_reference 是否存在，而未区分明确的 create 与 edit 操作。
- Change：明确 create/submit/login 的表单输入与提交不受“已存在编辑行/对话框”约束；明确 edit 保留已有行、对话框及严格输入绑定；旧未声明 operation 接口保留原规则。仍保留 Project 选择、表单一致性、合法 Scenario 输入和所有执行预算。只修类型明确的绑定，不自动换对象。字段后缀匹配限定输入/选择/单元格；断言的动作按钮须精确匹配，Member 不能指向 Add Member。
- Before：D03 初始计划一次 UNBOUND_REQUIRED_INPUT，第二份计划恢复；D12 泛化 Member 断言失败属于不明确计划目标，没有伪造匹配。
- After：新增未来行创建表单测试通过；失败 Check 的具体契约由 Tester/Runtime 接口传给 Replan，未知控件继续拒绝。所有旧有效结果保留，已通过 D03 不再实测。
- Final Decision：不扩大候选或预算。D12 待最新版本必要验证，Main/Scoring/Evaluation/模型均冻结。

## Final Stabilization：真实显示对象与语义字段 — 2026-10-09

- Problem：最新 D03 Member 分支的 Project selector 未绑定到 Member project；D12 文本断言同时命中隐藏视图，或同一成员行的两个同名单元格，执行失败后耗尽原时长上限。
- Why：计划控件描述与实际字段标签缺少受类型限制的对应；DOM 匹配数量被直接当成业务对象数量。原生 option 的不可见状态也不等于其文本没有在可见下拉控件显示。
- Change：仅在正确控件类型下解析 selector/dropdown/select/combobox 与 field/input/textbox 描述，继续要求唯一匹配及合法输入引用。原始断言优先唯一可见目标；text= 定位的已选 option 映射到唯一实际显示控件；多个相同文本单元格仅在可见/隐藏检查、同一稳定数据行时归为该行。多真实可见对象仍拒绝，count 继续统计原始集合，未选 option 不冒充当前显示状态；不自动替换 Project 或采用任意 first。
- Before：上一冻结版本 D03 为 2/3 Tasks、12/18 Checks；D12 为 0/2、1/7，两次 INVALID_ACTION，三个 Task 停于 MAX_RUNTIME_REACHED；没有 Provider 失败，全部作为历史质量结果保留。
- After：新增字段绑定、真实多对象拒绝、隐藏视图、副本计数及显示选项本地验证；补充直接重现真实失败选择器的双会话/缺陷/自动回放集成测试。原已通过完整回归和 D13 不重复运行。真实结果待修复版本冻结后的必要验证。
- Final Decision：保持架构、严格断言、Scoring、Evaluation 和预算。只修已有证据的解析/对象接口，尚不宣称最终达标。

## Final Stabilization 继续：唯一候选、实际字段状态与响应清理 — 2026-10-09

- Problem：D03/D13 的 13 个候选集包含重复 Candidate ID，唯一 Project 选择被错误交给 Jev；Task name / Task title 不一致，下拉框文本断言无法执行；重复提交结束后 Response.finished 留下后台 Target closed 异常。
- Why：Project binding 与显式输入生成相同动作，没有合并；字段解析和文本断言只支持部分页面控件；已安装 Playwright 的 Response.finished 创建关闭监听任务且正常返回时没有清理。
- Change：同 ID、同动作参数合并；同 ID 的冲突动作拒绝。实际已选中、引用一致的 Project 输入算作已绑定，冲突引用拒绝。Task name 仅映射到明确 Task title 输入；下拉框只检查选中项、输入框检查真实 value。使用现有 response.body 等待响应完成，保留原等待上限与路由清理。导航必须完成所有已声明输入；表头不作为数据行，明确结果列优先于后缀相似输入字段。原 Project 已真实观测并稳定绑定、有限重新观察后仍不可用时，只读正向检查记录原父对象不存在这一失败前提；不检查其他 Project、不宣称子对象状态、不把不存在改成成功。原父对象仍存在时继续执行全部原字段断言，标准保持严格。
- Before：上一轮 Task Success 62.50%、Checks 69.23%；Project 重复候选产生 LOW_JEV_CONFIDENCE，D13 Task 分支 0/7。
- After：冻结 D03/D12/D13 验证：重复候选、Jev 低置信度、no target assertion 错误均为 0；D13 两类 Bug 全部找到，3/3 Tasks、14/14 Checks，回放/验证通过且响应关闭后台异常消失。D03 导航过早完成导致 FP；D12 原父对象消失后不必要重规划与空值证据导致两个时长中止，结果保留。后续 111 个相关本地测试通过，包括正常/缺陷双会话、未知对象拒绝、已知父对象缺失、禁止替换 Project 和严格评分/回放；当前最终全量回归运行中，正式 readiness 尚待冻结验证。
- Final Decision：架构、预算、模型、冻结 Evaluation 均保持；本轮先集中验证，再根据真实证据继续处理剩余根因，不运行正式 Benchmark/Holdout。

## Final Stabilization：稳定对象、实时续接与回放参与者 — 2026-10-09

- Problem：导航继承旧 Project，消失对象的断言被选择绑定阻塞，旧单元格位置可能指向错误字段；实时流程和回放对依赖的表示不一致。重复提交的本地回放还有响应与页面更新同步问题。
- Why：已观察身份与当前选择混淆；旧定位器既被用于真实缺失，也被用于缺字段；Main 正确移除整 Task 等待边后，Replay 仍只看 Task 依赖或 LLM 手填关联任务。临时路由按请求数自动退出，可能在客户端处理响应前恢复缓存和旧页面状态。
- Change：没有显式 Project 的导航只负责跳转；保存语义选择的真实稳定 ID，快照用同一 ID 判断可用性。已记录单元格只在原行真实缺失且原视图可见时提供缺失证据；已存在行的缺字段先有限恢复，不使用旧列位置。Project 撤权后，只允许对先前真实观察的同一稳定对象在其当前可见容器中做隐藏/零数量断言，未知对象与错误视图仍拒绝；不选择其他 Project。已完成 Check 的续接保留必要边界和发布，未发布信号在 Replan 契约中保留。Replay 从实际记录的稳定 Check 依赖推导必要角色，保留各自会话，只纳入断言边界之前的动作。重复提交等待浏览器接收响应及客户端事件循环，路由仅暂存本次两个 POST，GET 正常继续，完整清理监听器与路由；原 timeout/预算不变。
- Before：既有 D12 有 PROJECT_BINDING_UNAVAILABLE、无目标断言及信号阻塞；完整本地回归另外暴露重复提交/回放同步和旧测试契约问题。响应同步单独修复未解决该回放问题，第二个方向调整了临时路由生命周期；没有第三个方向或真实 Case 调试。
- After：当前 344 个 local/unit/integration tests 最终全部通过；正常及 B2/B6 缺陷模式的 D12 固定本地计划均为 2/2 Tasks、7/7 Checks，三个信号顺序正确，缺陷在跨角色 Replay/Verification 中得到确认。其他重现/验证回归 21/21、重复提交相关回归 5/5；Ruff、mypy（41 源文件）、diff check 通过。真实模型测量仍未开始；本地测试结果不能代替实际模型能力指标。
- Final Decision：保留现有架构，唯一一次 D03/D12/D13 检查后停止，FINAL_BENCHMARK_READY = NO。Main 启动 8/8、依赖/死锁/预算缩减/重复任务/关闭重定向均 0；D12 三个信号正确保存和交接，7/7 检查有真实证据，跨角色回放成功。全轮重现 12/12 匹配，6 次 Verification 均 FAIL，表示记录的断言失败仍可重现，不等于六个真实缺陷。正式 GT 判出 1 个 FP，Task Success 5/8、Check Completion 27/39，未达目标。

剩余系统性问题由这唯一一次检查明确揭示，未继续修复：CANDIDATE_SET 有 13 次集合含完全重复的 candidate_id；D13 同一个 Project SELECT ID 连续重复两项，Runtime 按列表长度交给 Jev，违反唯一合法候选直接执行的预期。project_reference 与同字段 input binding 重叠是主要入口；D03/D13 共四次 LOW_JEV_CONFIDENCE，两任务停在 Replan Limit。D13 最终用文本比较断言 Task project 下拉框，但 Runtime 的文本比较候选仅包含 cell，真实 select 控件被报告 CONTROL_NOT_FOUND；此处是 Assertion 类型/控件契约问题，不能解释成对象真的不存在。D03 的 Task name 与实际 Task title 不一致也曾导致 UNBOUND_REQUIRED_INPUT，后续 Replan 恢复。D13 结束时还有 Response.finished 后台任务 Target closed 异常；六次该 Case 重现仍匹配，不能据此删除有效测量，但响应任务关闭清理仍有可靠性问题。本轮重复提交同步已尝试两个合理方向，不再继续第三个方向。

LangSmith 三条闭合追踪树共 598 个 Span，同 Trace、父子关系及原生计数一致；Main Planning、Tester Initial/Replan、Jev、Browser Action、Replay/Verification 均在实际发生时记录。正式 report/evaluation 评分一致，Provider 失败 0，全部测量冻结文件哈希一致。剩余 Candidate / Assertion / Finding Oracle / 清理问题跨 Runtime 与 Tester 接口，属于系统性阻塞，不建议现在进入最终 Benchmark/Holdout。详见 [Summary](final_readiness_summary.md)、[Metrics](../../artifacts/runs/final-readiness-cp-20261009-001/checkpoint_metrics.json) 和 [Tracing](../../artifacts/runs/final-readiness-cp-20261009-001/tracing_review.json)。没有第二轮检查、后续代码修正或 benchmark_history.md 更新。

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
