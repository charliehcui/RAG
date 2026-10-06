from __future__ import annotations

import asyncio
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from agent_framework import BaseChatClient, ChatResponse, Content, Message
from agent_framework._tools import FunctionInvocationLayer

from web_testing_system.agents import (
    MainAgentRunner,
    MainAgentTools,
    create_main_agent,
    create_tester_agent,
)
from web_testing_system.agents import (
    TesterAgentTools as AgentTools,
)
from web_testing_system.agents import (
    TesterAssignment as Assignment,
)
from web_testing_system.agents import (
    TesterRunner as Runner,
)
from web_testing_system.config import (
    AccountReference,
    ExpectedBehavior,
    ExpectedBehaviorSource,
    RunConfig,
    Settings,
)
from web_testing_system.orchestration.scheduler import LocalTesterScheduler
from web_testing_system.orchestration.scheduler import (
    TesterInstance as ScheduledInstance,
)
from web_testing_system.runtime.browser import BrowserManager
from web_testing_system.runtime.budget import BudgetGuard, BudgetLimits
from web_testing_system.runtime.candidates import CandidateBuilder, PageStateReader
from web_testing_system.runtime.jev_selector import JevSelector
from web_testing_system.runtime.permissions import ExecutionPolicy, PermissionChecker
from web_testing_system.runtime.playwright_executor import PlaywrightExecutor
from web_testing_system.runtime.web_runtime import WebTestingRuntime
from web_testing_system.state import StateStore, build_data_namespace


class ScriptedMainClient(FunctionInvocationLayer, BaseChatClient):
    def __init__(self) -> None:
        self.phase = "initial"
        self.step = 0
        self.finding_id: str | None = None
        self.replan_messages: list[str] = []
        super().__init__()

    def begin_replan(self, finding_id: str) -> None:
        self.phase = "replan"
        self.step = 0
        self.finding_id = finding_id

    async def _inner_get_response(
        self,
        *,
        messages: Sequence[Message],
        stream: bool,
        options: Mapping[str, Any],
        **kwargs: Any,
    ) -> ChatResponse:
        del stream, options, kwargs
        self.step += 1
        if self.phase == "initial":
            return self._initial_response()
        self.replan_messages.append(str(messages))
        return self._replan_response()

    def _initial_response(self) -> ChatResponse:
        if self.step == 1:
            self.step = 2
        task_arguments = {
            2: {
                "task_id": "task-project",
                "goal": "Test Project creation and update",
                "feature": "Project",
                "priority": "P1",
                "dependencies": [],
                "step_budget": 6,
                "data_requirements": {"role": "member", "project_prefix": "phase3"},
                "scope_targets": ["/projects"],
                "required_operations": ["read", "create"],
            },
            3: {
                "task_id": "task-task",
                "goal": "Test Task creation and deletion",
                "feature": "Task",
                "priority": "P1",
                "dependencies": [],
                "step_budget": 6,
                "data_requirements": {"role": "member", "task_prefix": "phase3"},
                "scope_targets": ["/tasks"],
                "required_operations": ["read", "create"],
            },
            4: {
                "task_id": "task-member",
                "goal": "Test Member management",
                "feature": "Member",
                "priority": "P2",
                "dependencies": [],
                "step_budget": 5,
                "data_requirements": {"role": "member"},
                "scope_targets": ["/members"],
                "required_operations": ["read"],
            },
            5: {
                "task_id": "task-permission",
                "goal": "Test Project permission boundaries after Project setup",
                "feature": "Permission",
                "priority": "P0",
                "dependencies": ["task-project"],
                "step_budget": 5,
                "data_requirements": {
                    "role": "member",
                    "expected_behavior_ids": ["EB-PERMISSION-1"],
                },
                "scope_targets": ["/permissions"],
                "required_operations": ["read"],
            },
        }
        if self.step in task_arguments:
            return self._tool_response(
                f"initial-task-{self.step}", "create_task", task_arguments[self.step]
            )
        return self._text_response("Initial plan created.")

    def _replan_response(self) -> ChatResponse:
        assert self.finding_id is not None
        if self.step == 1:
            return self._tool_response(
                "replan-read", "read_coordination_snapshot", {}
            )
        if self.step == 2:
            return self._tool_response(
                "replan-redirect",
                "redirect_task",
                {
                    "task_id": "task-project",
                    "goal": "Validate that a Member cannot delete another user's Project",
                    "feature": "Permission",
                    "priority": "P0",
                    "dependencies": [],
                    "data_requirements": {"role": "member", "fresh_project": True},
                    "scope_targets": ["/projects"],
                    "required_operations": ["delete"],
                    "parent_finding": self.finding_id,
                },
            )
        if self.step == 3:
            return self._tool_response(
                "replan-create",
                "create_task",
                {
                    "task_id": "task-permission-followup",
                    "goal": "Check Member permission boundaries across shared Projects",
                    "feature": "Permission",
                    "priority": "P0",
                    "dependencies": [],
                    "step_budget": 4,
                    "data_requirements": {"role": "member", "fresh_project": True},
                    "scope_targets": ["/projects"],
                    "required_operations": ["read", "delete"],
                    "parent_finding": self.finding_id,
                },
            )
        return self._text_response("Plan updated from the shared Finding.")

    @staticmethod
    def _tool_response(call_id: str, name: str, arguments: dict[str, Any]) -> ChatResponse:
        return ChatResponse(
            messages=[
                Message(
                    role="assistant",
                    contents=[Content.from_function_call(call_id, name, arguments=arguments)],
                )
            ],
            usage_details={"input_token_count": 2, "output_token_count": 1},
        )

    @staticmethod
    def _text_response(text: str) -> ChatResponse:
        return ChatResponse(
            messages=[Message(role="assistant", contents=[text])],
            usage_details={"input_token_count": 2, "output_token_count": 1},
        )


class IdleTesterClient(FunctionInvocationLayer, BaseChatClient):
    async def _inner_get_response(
        self,
        *,
        messages: Sequence[Message],
        stream: bool,
        options: Mapping[str, Any],
        **kwargs: Any,
    ) -> ChatResponse:
        del messages, stream, options, kwargs
        return ChatResponse(messages=[Message(role="assistant", contents=["Done."])])


class UnusedJevClient:
    def predict(
        self, state: Mapping[str, Any], questions: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        raise AssertionError("Jev is not used by this scheduling test")


def make_run_config() -> RunConfig:
    return RunConfig(
        target_url="http://app.test/",
        test_goal="Test Project, Task, Member, and Permission workflows",
        focus_features=["Project", "Task", "Member", "Permission"],
        allowed_scope=["/projects", "/tasks", "/members", "/permissions"],
        account_references=[
            AccountReference(
                identity_reference="identity-a",
                role="member",
                permissions=["read", "create", "delete"],
                secret_reference="env:MEMBER_A_PASSWORD",
            ),
            AccountReference(
                identity_reference="identity-b",
                role="member",
                permissions=["read", "create", "delete"],
                secret_reference="env:MEMBER_B_PASSWORD",
            ),
        ],
        test_data={"project_prefix": "phase3"},
        denied_operations=["delete production data"],
        expected_behaviors=[
            ExpectedBehavior(
                behavior_id="EB-PERMISSION-1",
                description="A Member cannot delete another user's Project",
                applies_to="Permission/member/delete-project",
                source=ExpectedBehaviorSource.USER_PROVIDED,
            )
        ],
    )


def make_budget(max_contexts: int = 2) -> BudgetGuard:
    return BudgetGuard(
        BudgetLimits(
            max_runtime_seconds=60,
            max_llm_calls=10,
            max_input_tokens=1_000,
            max_output_tokens=1_000,
            max_jev_calls=5,
            max_computer_use_calls=0,
            max_task_steps=20,
            max_task_replans=2,
            max_browser_contexts=max_contexts,
        )
    )


def create_store(path: Path, run_config: RunConfig) -> StateStore:
    store = StateStore(path)
    store.initialize()
    store.create_run(
        run_id="run-phase3",
        application=str(run_config.target_url),
        application_version="test",
        test_goal=run_config.test_goal,
        scope={
            "focus_features": run_config.focus_features,
            "allowed_scope": run_config.allowed_scope,
            "denied_operations": run_config.denied_operations,
        },
        status="RUNNING",
        global_budget=run_config.budget.model_dump(),
        remaining_budget=run_config.budget.model_dump(),
    )
    return store


def test_main_tool_schema_only_advertises_configured_features(tmp_path: Path) -> None:
    config = make_run_config()
    config.focus_features = ["Task"]
    store = create_store(tmp_path / "state.db", config)
    tools = MainAgentTools(store=store, run_id="run-phase3", target_url=str(config.target_url), focus_features=config.focus_features, allowed_scope=config.allowed_scope, denied_operations=config.denied_operations, max_step_budget=config.budget.max_browser_steps_per_task)
    agent = create_main_agent(client=ScriptedMainClient(), settings=Settings(_env_file=None), tools=tools)
    for tool in agent.default_options["tools"]:
        if tool.name in {"create_task", "redirect_task"}:
            assert tool.parameters()["properties"]["feature"]["enum"] == ["Task"]
    with pytest.raises(ValueError, match="feature is outside"):
        tools.create_task(task_id="outside-auth", goal="Log in", feature="Auth", priority="P1", dependencies=[], step_budget=5, data_requirements={}, scope_targets=["/tasks"], required_operations=["read"])
    assert store.list_tasks("run-phase3") == []


@pytest.mark.asyncio
async def test_parent_then_child_tool_calls_commit_in_declared_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = make_run_config()
    store = create_store(tmp_path / "ordered.db", config)
    original_create = store.create_task

    def delayed_create(**arguments: Any) -> dict[str, Any]:
        if arguments["task_id"] == "parent":
            time.sleep(0.05)
        return original_create(**arguments)

    class OrderedClient(FunctionInvocationLayer, BaseChatClient):
        def __init__(self) -> None:
            self.calls = 0
            super().__init__()

        async def _inner_get_response(self, *, messages: Sequence[Message], stream: bool, options: Mapping[str, Any], **kwargs: Any) -> ChatResponse:
            self.calls += 1
            if self.calls == 1:
                calls = []
                for task_id, dependencies in (("parent", []), ("child", ["parent"])):
                    arguments = {"task_id": task_id, "goal": "Check the project", "feature": "Project", "priority": "P1", "dependencies": dependencies, "step_budget": 6, "data_requirements": {}, "scope_targets": ["/projects"], "required_operations": ["read"]}
                    calls.append(Content.from_function_call("call-" + task_id, "create_task", arguments=arguments))
                return ChatResponse(messages=[Message(role="assistant", contents=calls)])
            return ChatResponse(messages=[Message(role="assistant", contents=["Plan complete."])])

    monkeypatch.setattr(store, "create_task", delayed_create)
    tools = MainAgentTools(store=store, run_id="run-phase3", target_url=str(config.target_url), focus_features=config.focus_features, allowed_scope=config.allowed_scope, denied_operations=config.denied_operations, max_step_budget=config.budget.max_browser_steps_per_task)
    client = OrderedClient()
    agent = create_main_agent(client=client, settings=Settings(_env_file=None), tools=tools)
    await agent.run("Create the ordered plan", session=agent.create_session())
    assert {task["task_id"] for task in store.list_tasks("run-phase3")} == {"parent", "child"}
    assert store.get_task("child")["dependencies"] == ["parent"]  # type: ignore[index]
    assert client.calls == 2


def create_tester_runner(
    *,
    store: StateStore,
    manager: BrowserManager,
    tester_id: str,
    identity_id: str,
    task_id: str,
) -> tuple[Runner, AgentTools]:
    task_budget = make_budget()
    checker = PermissionChecker(
        store=store,
        policy=ExecutionPolicy(allowed_url_prefixes=("http://app.test/",)),
        run_id="run-phase3",
        task_id=task_id,
        tester_id=tester_id,
    )
    runtime = WebTestingRuntime(
        store=store,
        browser_manager=manager,
        executor=PlaywrightExecutor(
            store=store,
            permission_checker=checker,
            run_id="run-phase3",
            task_id=task_id,
            tester_id=tester_id,
        ),
        page_state_reader=PageStateReader(),
        candidate_builder=CandidateBuilder(checker),
        jev_selector=JevSelector(UnusedJevClient()),
        budget=task_budget,
        run_id="run-phase3",
        task_id=task_id,
        tester_id=tester_id,
        identity_id=identity_id,
        budget_id=f"budget-{task_id}",
    )
    assignment = Assignment(
        run_id="run-phase3",
        task_id=task_id,
        tester_id=tester_id,
        identity_id=identity_id,
        role="member",
        data_namespace=build_data_namespace("run-phase3", tester_id, task_id),
        scope=("http://app.test/",),
        step_budget=6,
    )
    tools = AgentTools(assignment=assignment, runtime=runtime, store=store)
    agent = create_tester_agent(
        client=IdleTesterClient(), assignment=assignment, tools=tools
    )
    runner = Runner(
        agent=agent,
        assignment=assignment,
        runtime=runtime,
        store=store,
        budget=task_budget,
        budget_id=f"budget-{task_id}",
    )
    return runner, tools


@pytest.mark.integration
@pytest.mark.asyncio
async def test_two_testers_share_finding_and_main_agent_replans_running_task() -> None:
    temporary_root = Path(".pytest-temp")
    temporary_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=temporary_root) as directory:
        run_config = make_run_config()
        store = create_store(Path(directory) / "phase3.db", run_config)
        main_tools = MainAgentTools(
            store=store,
            run_id="run-phase3",
            target_url=str(run_config.target_url),
            focus_features=run_config.focus_features,
            allowed_scope=run_config.allowed_scope,
            denied_operations=run_config.denied_operations,
            max_step_budget=run_config.budget.max_browser_steps_per_task,
        )
        main_client = ScriptedMainClient()
        settings = Settings(
            _env_file=None,
            main_agent_provider="streamlake/fp8",
            main_agent_model="fake-strong-gemini",
        )
        main_agent = create_main_agent(
            client=main_client, settings=settings, tools=main_tools
        )
        main_runner = MainAgentRunner(
            agent=main_agent,
            tools=main_tools,
            run_id="run-phase3",
            max_replans=1,
        )

        initial_response = await main_runner.create_initial_plan(run_config)

        assert initial_response.text == "Initial plan created."
        initial_tasks = {task["task_id"]: task for task in store.list_tasks("run-phase3")}
        assert set(initial_tasks) == {
            "task-project",
            "task-task",
            "task-member",
            "task-permission",
        }
        assert initial_tasks["task-permission"]["dependencies"] == ["task-project"]
        assert initial_tasks["task-project"]["data_requirements"]["feature"] == "Project"
        assert initial_tasks["task-permission"]["data_requirements"][
            "expected_behavior_ids"
        ] == ["EB-PERMISSION-1"]
        assert "todo" not in main_runner.session.state
        assert main_agent.additional_properties["provider"] == "streamlake/fp8"
        assert main_agent.additional_properties["model"] == "fake-strong-gemini"

        for suffix in ("a", "b"):
            store.create_identity(
                identity_id=f"identity-{suffix}",
                run_id="run-phase3",
                role="member",
                secret_reference=f"env:MEMBER_{suffix.upper()}_PASSWORD",
                permissions=["read", "create", "delete"],
            )

        manager = BrowserManager(make_budget(max_contexts=2))
        runner_a, tools_a = create_tester_runner(
            store=store,
            manager=manager,
            tester_id="tester-a",
            identity_id="identity-a",
            task_id="task-project",
        )
        runner_b, tools_b = create_tester_runner(
            store=store,
            manager=manager,
            tester_id="tester-b",
            identity_id="identity-b",
            task_id="task-task",
        )
        for runner in (runner_a, runner_b):
            assignment = runner.assignment
            store.register_tester(
                tester_id=assignment.tester_id,
                run_id=assignment.run_id,
                session_reference=runner.session.session_id,
                identity_id=assignment.identity_id,
                role=assignment.role,
                data_namespace=assignment.data_namespace,
            )
            store.create_budget(
                budget_id=f"budget-{assignment.task_id}",
                run_id="run-phase3",
                task_id=assignment.task_id,
            )

        scheduler = LocalTesterScheduler(
            store=store,
            run_id="run-phase3",
            testers=[
                ScheduledInstance("tester-a", runner_a),
                ScheduledInstance("tester-b", runner_b),
            ],
            max_testers=2,
            max_browser_contexts=2,
        )
        both_started = asyncio.Event()
        finding_ready = asyncio.Event()
        replan_done = asyncio.Event()
        started: set[str] = set()
        finding_id: str | None = None
        tester_a_changed_task: dict[str, Any] | None = None
        tester_a_shared_facts: dict[str, Any] | None = None

        async def execute_task(
            runner: Runner, task: Mapping[str, Any]
        ) -> str:
            nonlocal finding_id, tester_a_changed_task, tester_a_shared_facts
            started.add(runner.assignment.tester_id)
            if len(started) == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=5)
            if runner.assignment.tester_id == "tester-b":
                finding = await tools_b.record_finding(
                    title="Member can delete another user's Project",
                    status="ANOMALY",
                    expected_result="A Member cannot delete another user's Project",
                    actual_result="The Project delete operation was accepted",
                    severity_hint="HIGH",
                    affected_page="/projects",
                )
                finding_id = str(finding["finding_id"])
                finding_ready.set()
            await asyncio.wait_for(replan_done.wait(), timeout=5)
            if runner.assignment.tester_id == "tester-a":
                tester_a_shared_facts = tools_a.read_shared_facts()
                tester_a_changed_task = tester_a_shared_facts["current_task"]
            return "COMPLETED"

        schedule_future = asyncio.create_task(scheduler.run_ready_tasks(execute_task))
        try:
            await asyncio.wait_for(finding_ready.wait(), timeout=10)
            assert finding_id is not None
            main_client.begin_replan(finding_id)
            replan_response = await main_runner.replan(
                "Tester B reported a cross-user Project deletion anomaly"
            )
            assert replan_response is not None
            assert replan_response.text == "Plan updated from the shared Finding."
        finally:
            replan_done.set()
        results = await asyncio.wait_for(schedule_future, timeout=10)
        await manager.close()

        assert {result.tester_id for result in results} == {"tester-a", "tester-b"}
        assert {result.status for result in results} == {"COMPLETED"}
        assert len({result.browser_session_id for result in results}) == 2
        assert len({runner_a.session.session_id, runner_b.session.session_id}) == 2
        permission_task = store.get_task("task-permission")
        assert permission_task is not None
        assert permission_task["status"] == "PENDING"
        assert permission_task["assigned_tester"] is None
        assert store.get_task("task-member")["assigned_tester"] is None  # type: ignore[index]

        assert tester_a_changed_task is not None
        assert tester_a_changed_task["priority"] == "P0"
        assert tester_a_changed_task["parent_finding"] == finding_id
        assert "another user's Project" in tester_a_changed_task["goal"]
        assert tester_a_shared_facts is not None
        assert any(
            finding["finding_id"] == finding_id
            for finding in tester_a_shared_facts["recent_findings"]
        )

        related_task = store.get_task("task-permission-followup")
        assert related_task is not None
        assert related_task["parent_finding"] == finding_id
        assert related_task["priority"] == "P0"
        final_project = store.get_task("task-project")
        final_task = store.get_task("task-task")
        assert final_project is not None and final_project["finished_at"] is not None
        assert final_task is not None and final_task["finished_at"] is not None
        assert store.get_tester("tester-a")["status"] == "READY"  # type: ignore[index]
        assert store.get_tester("tester-b")["status"] == "READY"  # type: ignore[index]

        skipped_replan = await main_runner.replan(
            "Ignore this request because the replan budget is exhausted"
        )
        assert skipped_replan is None

        events = store.list_events("run-phase3")
        event_types = [event["event_type"] for event in events]
        assert event_types.count("TASK_ASSIGNED") == 2
        assert {
            "TASK_CREATED",
            "FINDING_CREATED",
            "PLAN_CHANGE",
            "PRIORITY_CHANGE",
            "REPLAN_COMPLETED",
            "REPLAN_SKIPPED",
            "COORDINATION_SNAPSHOT_READ",
        } <= set(event_types)
        assignment_events = [
            event for event in events if event["event_type"] == "TASK_ASSIGNED"
        ]
        assert all(event["result"]["identity_id"] for event in assignment_events)
        assert all(event["result"]["data_namespace"] for event in assignment_events)
        assert all(event["result"]["step_budget"] == 6 for event in assignment_events)
        assert event_types.index("FINDING_CREATED") < event_types.index(
            "COORDINATION_SNAPSHOT_READ"
        ) < event_types.index("PLAN_CHANGE")
        snapshot_event = next(
            event
            for event in events
            if event["event_type"] == "COORDINATION_SNAPSHOT_READ"
        )
        assert finding_id in snapshot_event["result"]["finding_ids"]
