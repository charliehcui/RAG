from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

import pytest
from agent_framework import BaseChatClient, ChatResponse, Content, Message
from agent_framework._tools import FunctionInvocationLayer

from demo_app import DEMO_VERSION, DemoAppServer, SeededBugs
from web_testing_system.agents import (
    MainAgentRunner,
    MainAgentTools,
    create_main_agent,
    create_tester_agent,
)
from web_testing_system.agents import TesterAgentTools as AgentTools
from web_testing_system.agents import TesterAssignment as Assignment
from web_testing_system.agents import TesterRunner as Runner
from web_testing_system.config import (
    AccountReference,
    BudgetConfig,
    ExpectedBehavior,
    ExpectedBehaviorSource,
    RunConfig,
    Settings,
)
from web_testing_system.evidence import EvidenceStore
from web_testing_system.findings import FindingService, ScreeningSignals
from web_testing_system.reporting import FinalReportBuilder
from web_testing_system.reproduction import ReplayPlanBuilder, ReproductionRunner
from web_testing_system.reproduction.replay import DeterministicReplay
from web_testing_system.runtime.browser import BrowserManager
from web_testing_system.runtime.budget import BudgetGuard, BudgetLimits
from web_testing_system.runtime.candidates import CandidateBuilder, PageStateReader
from web_testing_system.runtime.jev_selector import JevSelector
from web_testing_system.runtime.models import ActionType, WebAction
from web_testing_system.runtime.permissions import ExecutionPolicy, PermissionChecker
from web_testing_system.runtime.playwright_executor import PlaywrightExecutor
from web_testing_system.runtime.web_runtime import WebTestingRuntime
from web_testing_system.scheduler import LocalTesterScheduler
from web_testing_system.scheduler import TesterInstance as ScheduledInstance
from web_testing_system.state import StateStore, build_data_namespace
from web_testing_system.verification import VerificationRunner


class FakeGeminiMainClient(FunctionInvocationLayer, BaseChatClient):
    def __init__(self) -> None:
        self.phase = "initial"
        self.step = 0
        self.finding_id: str | None = None
        self.received_messages: list[str] = []
        super().__init__()

    def begin_replan(self, finding_id: str) -> None:
        self.phase = "replan"
        self.step = 0
        self.finding_id = finding_id

    def begin_summary(self) -> None:
        self.phase = "summary"
        self.step = 0

    async def _inner_get_response(self, *, messages: Sequence[Message], stream: bool, options: Mapping[str, Any], **kwargs: Any) -> ChatResponse:
        del stream, options, kwargs
        self.received_messages.append("\n".join(message.text for message in messages))
        self.step += 1
        if self.phase == "initial":
            return self._initial()
        if self.phase == "replan":
            return self._replan()
        return self._text("The run used two Testers and confirmed the recorded Task deletion persistence bug.")

    def _initial(self) -> ChatResponse:
        if self.step == 1:
            return self._tool("todo-phase5", "todos_add", {"todos": [{"title": "Test Project flow"}, {"title": "Test Task deletion persistence"}]})
        if self.step == 2:
            return self._tool("task-project", "create_task", {"task_id": "task-project", "goal": "Test Project workflow", "feature": "Project", "priority": "P1", "dependencies": [], "step_budget": 20, "data_requirements": {"role": "admin"}, "scope_targets": ["/"], "required_operations": ["read", "create"]})
        if self.step == 3:
            return self._tool("task-delete", "create_task", {"task_id": "task-delete", "goal": "Test Task deletion persistence", "feature": "Task", "priority": "P0", "dependencies": [], "step_budget": 30, "data_requirements": {"role": "member", "expected_behavior_ids": ["EB-B1"]}, "scope_targets": ["/"], "required_operations": ["read", "delete"]})
        return self._text("Initial plan created for two Testers.")

    def _replan(self) -> ChatResponse:
        assert self.finding_id is not None
        if self.step == 1:
            return self._tool("snapshot", "read_coordination_snapshot", {})
        if self.step == 2:
            return self._tool("redirect-project", "redirect_task", {"task_id": "task-project", "goal": "Check related Project deletion persistence after the Task finding", "feature": "Project", "priority": "P0", "dependencies": [], "data_requirements": {"role": "admin", "reason": "related persistence risk"}, "scope_targets": ["/"], "required_operations": ["read", "delete"], "parent_finding": self.finding_id})
        return self._text("Plan changed from the new Finding.")

    @staticmethod
    def _tool(call_id: str, name: str, arguments: dict[str, Any]) -> ChatResponse:
        return ChatResponse(messages=[Message(role="assistant", contents=[Content.from_function_call(call_id, name, arguments=arguments)])], usage_details={"input_token_count": 2, "output_token_count": 1})

    @staticmethod
    def _text(value: str) -> ChatResponse:
        return ChatResponse(messages=[Message(role="assistant", contents=[value])], usage_details={"input_token_count": 2, "output_token_count": 1})


class FakeTesterClient(FunctionInvocationLayer, BaseChatClient):
    async def _inner_get_response(self, *, messages: Sequence[Message], stream: bool, options: Mapping[str, Any], **kwargs: Any) -> ChatResponse:
        del messages, stream, options, kwargs
        return ChatResponse(messages=[Message(role="assistant", contents=["Done."])], usage_details={"input_token_count": 1, "output_token_count": 1})


class FakeJevClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def predict(self, state: Mapping[str, Any], questions: Mapping[str, Any]) -> Mapping[str, Any]:
        del questions
        state_copy = dict(state)
        self.calls.append(state_copy)
        goal = str(state["current_goal"]).casefold()
        wanted = "tasks" if "task" in goal else "projects"
        candidates = list(state["legal_candidates"])
        selected = next(candidate for candidate in candidates if str(candidate["label"]).casefold() == wanted)
        return {"answers": {"next_candidate": {"choice": selected["id"], "confidence": 0.99}}, "usage": {"cost": 0}}


def make_budget(max_contexts: int = 4) -> BudgetGuard:
    return BudgetGuard(BudgetLimits(max_runtime_seconds=60, max_llm_calls=5, max_input_tokens=500, max_output_tokens=500, max_jev_calls=5, max_computer_use_calls=0, max_task_steps=100, max_task_replans=1, max_browser_contexts=max_contexts))


def make_run_config(base_url: str) -> RunConfig:
    return RunConfig(
        target_url=f"{base_url}/",
        test_goal="Test Project and Task workflows with two Testers",
        focus_features=["Project", "Task"],
        allowed_scope=["/"],
        account_references=[
            AccountReference(identity_reference="identity-a", role="admin", permissions=["read", "create", "delete"], secret_reference="env:DEMO_ADMIN_PASSWORD"),
            AccountReference(identity_reference="identity-b", role="member", permissions=["read", "delete"], secret_reference="env:DEMO_MEMBER_PASSWORD"),
        ],
        test_data={"namespace": "run-phase5"},
        denied_operations=["production deletion"],
        expected_behaviors=[ExpectedBehavior(behavior_id="EB-B1", description="Deleted Task remains absent after refresh", applies_to="Task/member/delete", source=ExpectedBehaviorSource.DEMO_SPEC)],
        application_version=DEMO_VERSION,
        budget=BudgetConfig(max_runtime_seconds=60, max_llm_calls=5, max_input_tokens=500, max_output_tokens=500, max_jev_calls=5, max_computer_use_calls=0, max_testers=2, max_browser_steps_per_task=100, max_replans_per_task=1, max_reproductions_per_finding=2, max_parallel_browser_contexts=2),
    )


def make_tester(*, store: StateStore, manager: BrowserManager, base_url: str, tester_id: str, identity_id: str, task_id: str, role: str, jev: FakeJevClient) -> tuple[Runner, AgentTools, PlaywrightExecutor, BudgetGuard]:
    budget = make_budget()
    checker = PermissionChecker(store=store, policy=ExecutionPolicy(allowed_url_prefixes=(f"{base_url}/",)), run_id="run-phase5", task_id=task_id, tester_id=tester_id)
    executor = PlaywrightExecutor(store=store, permission_checker=checker, run_id="run-phase5", task_id=task_id, tester_id=tester_id)
    runtime = WebTestingRuntime(store=store, browser_manager=manager, executor=executor, page_state_reader=PageStateReader(), candidate_builder=CandidateBuilder(checker), jev_selector=JevSelector(jev), budget=budget, run_id="run-phase5", task_id=task_id, tester_id=tester_id, identity_id=identity_id, budget_id=f"budget-{task_id}")
    assignment = Assignment(run_id="run-phase5", task_id=task_id, tester_id=tester_id, identity_id=identity_id, role=role, data_namespace=build_data_namespace("run-phase5", tester_id, task_id), scope=(f"{base_url}/",), step_budget=100)
    tools = AgentTools(assignment=assignment, runtime=runtime, store=store)
    agent = create_tester_agent(client=FakeTesterClient(), assignment=assignment, tools=tools)
    return Runner(agent=agent, assignment=assignment, runtime=runtime, store=store, budget=budget, budget_id=f"budget-{task_id}"), tools, executor, budget


async def login(runner: Runner, base_url: str, username: str, password: str) -> None:
    actions = (
        WebAction(action_type=ActionType.NAVIGATION, url=f"{base_url}/", timeout_ms=5_000),
        WebAction(action_type=ActionType.INPUT, target="#login-username", value=username, value_reference=f"{username}_username"),
        WebAction(action_type=ActionType.INPUT, target="#login-password", value=password, value_reference=f"{username}_password"),
        WebAction(action_type=ActionType.CLICK, target="#login-form button[type='submit']"),
        WebAction(action_type=ActionType.WAIT, target="#app-section", timeout_ms=5_000),
    )
    for action in actions:
        result = await runner.execute_known(action)
        assert result["success"], result


@pytest.mark.integration
@pytest.mark.asyncio
async def test_low_cost_two_tester_fake_end_to_end_flow() -> None:
    with DemoAppServer(bugs=SeededBugs(b1_deleted_task_reappears=True)) as app:
        temporary_root = Path(".pytest-temp")
        temporary_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temporary_root) as directory:
            root = Path(directory)
            run_config = make_run_config(app.base_url)
            store = StateStore(root / "phase5.db")
            store.initialize()
            store.create_run(run_id="run-phase5", application=f"{app.base_url}/", application_version=DEMO_VERSION, test_goal=run_config.test_goal, scope={"focus_features": run_config.focus_features, "allowed_scope": run_config.allowed_scope}, status="RUNNING", global_budget=run_config.budget.model_dump(), remaining_budget=run_config.budget.model_dump())

            main_client = FakeGeminiMainClient()
            main_tools = MainAgentTools(store=store, run_id="run-phase5", target_url=f"{app.base_url}/", focus_features=run_config.focus_features, allowed_scope=run_config.allowed_scope, denied_operations=run_config.denied_operations, max_step_budget=100)
            main_agent = create_main_agent(client=main_client, settings=Settings(_env_file=None, main_agent_provider="gemini", main_agent_model="fake-gemini-main", tester_agent_provider="groq", tester_agent_model="fake-groq-tester"), tools=main_tools)
            main_runner = MainAgentRunner(agent=main_agent, tools=main_tools, run_id="run-phase5", max_replans=1)
            initial = await main_runner.create_initial_plan(run_config)
            assert initial.text == "Initial plan created for two Testers."

            identities = (("identity-a", "tester-a", "admin", "task-project"), ("identity-b", "tester-b", "member", "task-delete"))
            for identity_id, tester_id, role, task_id in identities:
                store.create_identity(identity_id=identity_id, run_id="run-phase5", role=role, secret_reference=f"env:{role.upper()}_PASSWORD", permissions=["read", "create", "delete"])
                store.register_tester(tester_id=tester_id, run_id="run-phase5", session_reference=f"fake-maf-{tester_id}", identity_id=identity_id, role=role, data_namespace=build_data_namespace("run-phase5", tester_id, task_id))
                store.create_budget(budget_id=f"budget-{task_id}", run_id="run-phase5", task_id=task_id)

            manager = BrowserManager(make_budget())
            jev_a = FakeJevClient()
            jev_b = FakeJevClient()
            runner_a, tools_a, _, _ = make_tester(store=store, manager=manager, base_url=app.base_url, tester_id="tester-a", identity_id="identity-a", task_id="task-project", role="admin", jev=jev_a)
            runner_b, tools_b, executor_b, budget_b = make_tester(store=store, manager=manager, base_url=app.base_url, tester_id="tester-b", identity_id="identity-b", task_id="task-delete", role="member", jev=jev_b)
            scheduler = LocalTesterScheduler(store=store, run_id="run-phase5", testers=[ScheduledInstance("tester-a", runner_a), ScheduledInstance("tester-b", runner_b)], max_testers=2, max_browser_contexts=2)
            evidence_store = EvidenceStore(store=store, artifacts_root=root / "artifacts" / "runs", temporary_sensitive_root=root / "temporary-sensitive")
            both_active = asyncio.Event()
            finding_ready = asyncio.Event()
            replan_done = asyncio.Event()
            active_testers: set[str] = set()
            finding_id: str | None = None
            boundary_event_id: str | None = None
            tester_a_redirected = False

            async def execute_task(runner: Runner, task: Mapping[str, Any]) -> str:
                nonlocal finding_id, boundary_event_id, tester_a_redirected
                active_testers.add(runner.assignment.tester_id)
                if len(active_testers) == 2:
                    both_active.set()
                await asyncio.wait_for(both_active.wait(), timeout=10)
                if runner.assignment.tester_id == "tester-a":
                    await login(runner, app.base_url, "admin", "demo-admin")
                    decision = await runner.explore("Open Projects")
                    assert decision["decision"]["source"] == "JEV_TO_PLAYWRIGHT"
                    await asyncio.wait_for(replan_done.wait(), timeout=20)
                    current = tools_a.read_shared_facts()["current_task"]
                    tester_a_redirected = current["parent_finding"] == finding_id and current["priority"] == "P0"
                    return "COMPLETED"

                await login(runner, app.base_url, "member", "demo-member")
                decision = await runner.explore("Open Tasks")
                assert decision["decision"]["source"] == "JEV_TO_PLAYWRIGHT"
                for action in (
                    WebAction(action_type=ActionType.WAIT, target="#tasks-table tr[data-task-id='task-1']", timeout_ms=5_000),
                    WebAction(action_type=ActionType.CLICK, target="#tasks-table tr[data-task-id='task-1'] .delete-task"),
                    WebAction(action_type=ActionType.ASSERTION, target="#tasks-table tr[data-task-id='task-1']", assertion="hidden"),
                    WebAction(action_type=ActionType.REFRESH, timeout_ms=5_000),
                    WebAction(action_type=ActionType.WAIT, target="#app-section", timeout_ms=5_000),
                    WebAction(action_type=ActionType.CLICK, target="#nav-tasks"),
                    WebAction(action_type=ActionType.WAIT, target="#tasks-table tr[data-task-id='task-1']", timeout_ms=5_000),
                ):
                    result = await runner.execute_known(action)
                    assert result["success"], result
                failed = await runner.execute_known(WebAction(action_type=ActionType.ASSERTION, target="#tasks-table tr[data-task-id='task-1']", assertion="hidden"))
                assert failed["error_type"] == "ASSERTION_FAILURE"
                boundary_event_id = str(failed["event_id"])
                finding = tools_b.record_finding(title="Deleted Task reappears after refresh", status="OBSERVATION", expected_result="Deleted Task remains absent after refresh", actual_result="Deleted Task is visible again", severity_hint="HIGH", affected_page="/", action="refresh", error_text="target is visible")
                finding_id = str(finding["finding_id"])
                screened = FindingService(store, "run-phase5").screen(finding_id, ScreeningSignals(assertion_failed=True))
                assert screened["status"] == "ANOMALY"
                session_id = runner.runtime.browser_session_id
                assert session_id is not None
                page = manager.get_session(session_id).page
                await evidence_store.capture_screenshot(page=page, run_id="run-phase5", task_id="task-delete", finding_id=finding_id, attempt_id="observation", browser_session_id=session_id, name="finding-created")
                finding_ready.set()
                await asyncio.wait_for(replan_done.wait(), timeout=20)
                return "COMPLETED"

            schedule_task = asyncio.create_task(scheduler.run_ready_tasks(execute_task))
            try:
                await asyncio.wait_for(finding_ready.wait(), timeout=20)
                assert finding_id is not None
                main_client.begin_replan(finding_id)
                replanned = await main_runner.replan("Tester B reported that a deleted Task reappeared after refresh")
                assert replanned is not None
                assert replanned.text == "Plan changed from the new Finding."
            finally:
                replan_done.set()
            results = await asyncio.wait_for(schedule_task, timeout=30)
            assert {result.tester_id for result in results} == {"tester-a", "tester-b"}
            assert {result.status for result in results} == {"COMPLETED"}
            assert len({result.browser_session_id for result in results}) == 2
            assert tester_a_redirected

            assert finding_id is not None and boundary_event_id is not None
            build = ReplayPlanBuilder(store).from_action_history(run_id="run-phase5", task_id="task-delete", input_values={"member_username": "member", "member_password": "demo-member"}, through_event_id=boundary_event_id)
            assert build.status == "READY" and build.plan is not None
            replay = DeterministicReplay(store=store, browser_manager=manager, executor=executor_b, evidence_store=evidence_store, budget=budget_b, budget_id="budget-task-delete")
            async def reset_demo() -> None:
                def reset_request() -> int:
                    request = Request(f"{app.base_url}/test/reset", data=b"{}", method="POST", headers={"Content-Type": "application/json"})
                    with urlopen(request) as response:
                        return response.status

                assert await asyncio.to_thread(reset_request) == 200

            reproduction = await ReproductionRunner(store=store, finding_service=FindingService(store, "run-phase5"), replay=replay).run(finding_id=finding_id, plan=build.plan, max_attempts=2, stable_successes=2, max_minimization_attempts=0, reset_hook=reset_demo)
            assert reproduction.status == "REPRODUCED"
            verification_build = ReplayPlanBuilder(store).from_finding(finding_id=finding_id, input_values={"member_username": "member", "member_password": "demo-member"})
            assert verification_build.status == "READY" and verification_build.plan is not None
            verification = await VerificationRunner(store=store, finding_service=FindingService(store, "run-phase5"), replay=replay).run(finding_id=finding_id, plan=verification_build.plan, reset_hook=reset_demo)
            assert verification.result == "FAIL"
            assert store.get_finding(finding_id)["status"] == "CONFIRMED_BUG"  # type: ignore[index]

            store.finish_run(run_id="run-phase5", status="COMPLETED")
            report = FinalReportBuilder(store).build("run-phase5", full_evaluation=False)
            assert report["test_summary"]["tester_count"] == 2
            assert report["confirmed_bugs"][0]["finding_id"] == finding_id
            assert report["full_evaluation"] == "Full Evaluation: NOT RUN"
            main_client.begin_summary()
            summary = await main_runner.summarize_report(report)
            assert "two Testers" in summary.text

            event_types = [event["event_type"] for event in store.list_events("run-phase5")]
            assert "JEV_CALL" in event_types
            assert "FINDING_CREATED" in event_types
            assert "PLAN_CHANGE" in event_types
            assert "REPRODUCTION_ATTEMPT" in event_types
            assert "VERIFICATION_RESULT" in event_types
            assert "COMPUTER_USE_REQUEST" not in event_types
            assert all("enabled_bugs" not in message and "b1_deleted_task_reappears" not in message for message in main_client.received_messages)
            assert all("enabled_bugs" not in str(call) and "b1_deleted_task_reappears" not in str(call) for call in [*jev_a.calls, *jev_b.calls])
            assert app.ground_truth.read("COMPLETED").enabled_bugs == ("B1",)
            await manager.close()
