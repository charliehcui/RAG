from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import MethodType, SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from agent_framework import BaseChatClient, ChatResponse, Content, Message
from agent_framework._tools import FunctionInvocationLayer

from demo_app import DemoAppServer, SeededBugs
from web_testing_system.agents.main_agent import (
    MainAgentRunner,
    MainAgentTools,
    create_main_agent,
)
from web_testing_system.agents.tester_agent import TesterAgentTools as WorkerTools
from web_testing_system.agents.tester_agent import TesterAssignment as Assignment
from web_testing_system.agents.tester_agent import TesterRunner as Worker
from web_testing_system.config import RunConfig, Settings
from web_testing_system.evaluation.scenarios import load_run_config
from web_testing_system.orchestration.runner import _budget_limits
from web_testing_system.orchestration.runner import run as run_system
from web_testing_system.orchestration.scheduler import LocalTesterScheduler
from web_testing_system.orchestration.scheduler import TesterInstance as WorkerInstance
from web_testing_system.runtime.budget import BudgetGuard
from web_testing_system.runtime.jev_selector import JevSelector
from web_testing_system.runtime.web_runtime import WebTestingRuntime
from web_testing_system.state import StateStore


def tools_for(tmp_path: Path, case_id: str = "D12", supplied_config: RunConfig | None = None) -> tuple[MainAgentTools, RunConfig]:
    config = supplied_config or load_run_config(Path("evaluation/scenarios.json"), scenario_id=case_id, target_url="http://app.test/", tester_count=3)
    store = StateStore(tmp_path / "main.db")
    store.initialize()
    store.create_run(run_id="main", application="http://app.test/", application_version="test", test_goal=config.test_goal, scope={"required_checks": [check.model_dump(mode="json") for check in config.required_checks], "configured_tester_capacity": 3}, status="RUNNING", global_budget=config.budget.model_dump(), remaining_budget=config.budget.model_dump())
    tools = MainAgentTools(store=store, run_id="main", target_url="http://app.test/", focus_features=config.focus_features, allowed_scope=config.allowed_scope, denied_operations=config.denied_operations, max_step_budget=config.budget.max_browser_steps_per_task)
    tools.configure_workflows(config)
    return tools, config


def drafts(tools: MainAgentTools) -> list[dict[str, Any]]:
    return [{"goal_id": group["goal_id"], "goal": group["description"], "priority": "P1", "required_operations": ["read"]} for group in tools.planning_contract()["workflows"] if group["editable"]]


@pytest.mark.parametrize("case_id", ["D01", "D03", "D12", "D13"])
def test_plan_covers_whole_role_workflows_with_authoritative_inputs(tmp_path: Path, case_id: str) -> None:
    tools, config = tools_for(tmp_path, case_id)
    result = tools.submit_task_plan(drafts(tools))  # type: ignore[arg-type]
    assert result["ok"], result
    tasks = tools.store.list_tasks("main")
    ids = [check_id for task in tasks for check_id in task["data_requirements"]["check_ids"]]
    assert len(ids) == len(set(ids)) == len(config.required_checks)
    assert set(ids) == {check.check_id for check in config.required_checks}
    for task in tasks:
        group = tools.workflow_contract[task["data_requirements"]["goal_id"]]
        assert task["data_requirements"]["identity_reference"] == group["identity_reference"]
        assert task["data_requirements"]["test_data_keys"] == group["test_data_keys"]
        assert group["description"] in task["goal"]
        assert task["step_budget"] == config.budget.max_browser_steps_per_task
        limits = _budget_limits(config, task_steps=task["step_budget"], remaining=config.budget.model_dump())
        assert limits.max_task_steps == config.budget.max_browser_steps_per_task
    assert all(not task["dependencies"] for task in tasks)
    if case_id == "D12":
        assert tools.live_peers == {"offboarded-access": {"offboard-member"}, "offboard-member": {"offboarded-access"}}
        assert len(tasks) == 2


@pytest.mark.parametrize("problem", ["missing_workflow", "duplicate_workflow", "empty_goal", "denied_operation"])
def test_entire_plan_is_validated_before_any_task_is_created(tmp_path: Path, problem: str) -> None:
    tools, _ = tools_for(tmp_path, "D03")
    plan = drafts(tools)
    if problem == "missing_workflow":
        plan.pop()
    elif problem == "duplicate_workflow":
        plan.append(dict(plan[0]))
    elif problem == "empty_goal":
        plan[-1]["goal"] = " "
    else:
        plan[-1]["required_operations"] = [next(iter(tools.denied_operations))]
    result = tools.submit_task_plan(plan)  # type: ignore[arg-type]
    assert not result["ok"]
    assert tools.store.list_tasks("main") == []


@pytest.mark.parametrize("case_id, proposed_budgets", [("D13", [10, 8, 18]), ("D12", [10, 12]), ("D02", [45]), ("D13", [0, -1, 1000])])
def test_main_cannot_choose_task_execution_budgets(tmp_path: Path, case_id: str, proposed_budgets: list[int]) -> None:
    tools, config = tools_for(tmp_path, case_id)
    original_config = config.model_dump()
    original_run = tools.store.get_run("main")
    contract = tools.planning_contract()
    schema = tools.plan_tool(contract).parameters()["$defs"]["MainTaskPlan"]
    assert "step_budget" not in schema["properties"] and "step_budget" not in schema["required"]
    assert contract["execution_budget"]["new_task_steps"] == config.budget.max_browser_steps_per_task
    plan = drafts(tools)
    for task, budget in zip(plan, proposed_budgets, strict=True):
        task["step_budget"] = budget
    result = tools.submit_task_plan(plan)  # type: ignore[arg-type]
    assert result["ok"], result
    assert all(task["step_budget"] == config.budget.max_browser_steps_per_task for task in tools.store.list_tasks("main"))
    assert config.model_dump() == original_config and tools.store.get_run("main") == original_run


@pytest.mark.parametrize("proposed_budget", [1, 1000, None])
def test_legacy_main_creation_also_inherits_fixed_budget(tmp_path: Path, proposed_budget: int | None) -> None:
    tools, config = tools_for(tmp_path, "D02")
    group = next(iter(tools.workflow_contract.values()))
    result = tools.create_task(task_id="legacy", goal=group["description"], feature=group["feature"], priority="P1", dependencies=[], step_budget=proposed_budget, data_requirements={"identity_reference": group["identity_reference"], "expected_behavior_ids": list({check["behavior_id"] for check in group["checks"]}), "check_ids": [check["check_id"] for check in group["checks"]]}, scope_targets=group["scope_targets"], required_operations=["read"])
    assert result["task"]["step_budget"] == config.budget.max_browser_steps_per_task


def test_execution_budget_is_inherited_from_config_not_hardcoded(tmp_path: Path) -> None:
    config = load_run_config(Path("evaluation/scenarios.json"), scenario_id="D13", target_url="http://app.test/", tester_count=3)
    config = config.model_copy(update={"budget": config.budget.model_copy(update={"max_browser_steps_per_task": 37})})
    tools, config = tools_for(tmp_path, supplied_config=config)
    assert tools.submit_task_plan(drafts(tools))["ok"]  # type: ignore[arg-type]
    tasks = tools.store.list_tasks("main")
    assert len(tasks) == 3 and all(task["step_budget"] == 37 for task in tasks)
    assert config.budget.max_browser_steps_per_task == 37


@pytest.mark.parametrize("status", ["PENDING", "BLOCKED", "WAITING_FOR_DATA", "COMPLETED"])
def test_replan_preserves_existing_budget_without_resetting_it(tmp_path: Path, status: str) -> None:
    tools, config = tools_for(tmp_path, "D13")
    assert tools.submit_task_plan(drafts(tools))["ok"]  # type: ignore[arg-type]
    task_id = "name-validation"
    with sqlite3.connect(tools.store.database_path) as connection:
        connection.execute("UPDATE tasks SET step_budget = ?, status = ? WHERE task_id = ?", (37, status, task_id))
    original = tools.store.get_task(task_id)
    plan = drafts(tools)
    for task in plan:
        task["step_budget"] = 1
    assert tools.submit_task_plan(plan)["ok"]  # type: ignore[arg-type]
    current = tools.store.get_task(task_id)
    assert current is not None and current["step_budget"] == 37
    if status == "COMPLETED":
        assert current == original
    else:
        assert current["status"] == "PENDING"
        assert _budget_limits(config, task_steps=current["step_budget"], remaining=config.budget.model_dump()).max_task_steps == 37


def test_forward_dependency_is_compiled_in_creation_order(tmp_path: Path) -> None:
    config = load_run_config(Path("evaluation/scenarios.json"), scenario_id="D03", target_url="http://app.test/", tester_count=3)
    checks = [config.required_checks[0].model_copy(update={"check_id": "first", "goal_id": "prerequisite", "depends_on": []}), config.required_checks[0].model_copy(update={"check_id": "last", "goal_id": "dependent", "depends_on": ["first"]})]
    config = config.model_copy(update={"test_goal": "Two ordered workflows", "required_checks": checks})
    tools, _ = tools_for(tmp_path, "D03", config)
    result = tools.submit_task_plan(list(reversed(drafts(tools))))  # type: ignore[arg-type]
    assert result["ok"], result
    assert tools.store.get_task("dependent")["dependencies"] == ["prerequisite"]  # type: ignore[index]
    created = tools.store.list_events("main", event_types=("TASK_CREATED",))
    assert [event["task_id"] for event in created] == ["prerequisite", "dependent"]


def test_legacy_redirect_rejects_cycles_unknown_and_closed_dependencies(tmp_path: Path) -> None:
    tools, _ = tools_for(tmp_path, "D03")
    tools.submit_task_plan(drafts(tools))  # type: ignore[arg-type]
    tasks = tools.store.list_tasks("main")
    first, second = tasks[0]["task_id"], tasks[1]["task_id"]
    common = {"feature": tools.feature_names[0], "step_budget": 10, "scope_targets": ["/"], "required_operations": ["read"], "parent_finding": None}
    with pytest.raises(ValueError, match="not in this Run"):
        tools._validate_task_request(task_id=first, dependencies=["future"], **common)
    tools.store.update_task_plan(task_id=second, expected_status="PENDING", goal="Dependent", priority="P1", dependencies=[first], parent_finding=None, data_requirements=tasks[1]["data_requirements"])
    with pytest.raises(ValueError, match="acyclic"):
        tools._validate_task_request(task_id=first, dependencies=[second], **common)
    tools.store.update_task_status(task_id=second, expected_status="PENDING", new_status="STOPPED")
    with pytest.raises(ValueError, match="cannot complete"):
        tools._validate_task_request(task_id=first, dependencies=[second], **common)
    with pytest.raises(ValueError, match="not open"):
        tools.redirect_task(task_id=second, goal="Redirect closed", feature=tools.feature_names[0], priority="P1", dependencies=[], data_requirements={}, scope_targets=["/"], required_operations=["read"], parent_finding="unused")


def test_replan_preserves_completed_task_and_rejects_stale_submission(tmp_path: Path) -> None:
    tools, _ = tools_for(tmp_path, "D03")
    tools.submit_task_plan(drafts(tools))  # type: ignore[arg-type]
    old_plan = drafts(tools)
    task = tools.store.list_tasks("main")[0]
    tools.store.update_task_status(task_id=task["task_id"], expected_status="PENDING", new_status="COMPLETED")
    preserved = tools.store.get_task(task["task_id"])
    result = tools.submit_task_plan(old_plan)  # type: ignore[arg-type]
    assert not result["ok"]
    assert tools.store.get_task(task["task_id"]) == preserved
    assert len(tools.store.list_tasks("main")) == 3
    result = tools.submit_task_plan(drafts(tools))  # type: ignore[arg-type]
    assert result["ok"]
    assert tools.store.get_task(task["task_id"]) == preserved


def test_closed_live_session_is_not_recreated_or_split_into_setup_tasks(tmp_path: Path) -> None:
    tools, _ = tools_for(tmp_path)
    tools.submit_task_plan(drafts(tools))  # type: ignore[arg-type]
    tools.store.update_task_status(task_id="offboarded-access", expected_status="PENDING", new_status="STOPPED")
    contract = tools.planning_contract()
    assert contract["editable_goal_ids"] == []
    assert all(group["blocker"] == "CLOSED_LIVE_SESSION_CANNOT_RESUME" for group in contract["workflows"])
    assert len(tools.store.list_tasks("main")) == 2


@pytest.mark.asyncio
async def test_single_initial_request_commits_all_tasks_without_a_done_call(tmp_path: Path) -> None:
    tools, config = tools_for(tmp_path)

    class PlanClient(FunctionInvocationLayer, BaseChatClient):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def _inner_get_response(self, *, messages: Sequence[Message], stream: bool, options: Mapping[str, Any], **kwargs: Any) -> ChatResponse:
            self.calls += 1
            assert self.calls == 1
            assert [tool.name for tool in options["tools"]] == ["submit_task_plan"]
            contract = json.loads(messages[-1].text.split("Planning Contract: ", 1)[1])
            assert set(contract["editable_goal_ids"]) == {"offboarded-access", "offboard-member"}
            return ChatResponse(messages=[Message(role="assistant", contents=[Content.from_function_call("plan", "submit_task_plan", arguments={"tasks": drafts(tools)})])])

    client = PlanClient()
    agent = create_main_agent(client=client, settings=Settings(_env_file=None), tools=tools)
    legacy_tool = next(tool for tool in agent.default_options["tools"] if tool.name == "create_task")
    assert "step_budget" not in legacy_tool.parameters()["properties"]
    runner = MainAgentRunner(agent=agent, tools=tools, run_id="main", max_replans=2)
    tools.workflow_contract.clear()
    await runner.create_initial_plan(config)
    assert client.calls == 1 and len(tools.store.list_tasks("main")) == 2


@pytest.mark.asyncio
async def test_compiled_live_tasks_start_together_and_exchange_progress_in_the_same_sessions(tmp_path: Path) -> None:
    tools, config = tools_for(tmp_path)
    tools.submit_task_plan(drafts(tools))  # type: ignore[arg-type]
    started: set[str] = set()
    both_started = asyncio.Event()
    sessions: dict[str, str] = {}
    workers: dict[str, WorkerTools] = {}

    def factory(task: Mapping[str, Any]) -> WorkerInstance:
        task_id = task["task_id"]
        role = task["data_requirements"]["role"]
        tester_id = "tester-" + task_id
        identity = "identity-" + task_id
        tools.store.create_identity(identity_id=identity, run_id="main", role=role, secret_reference=None, permissions=["read"])
        tools.store.register_tester(tester_id=tester_id, run_id="main", session_reference=tester_id, identity_id=identity, role=role, data_namespace=task_id)
        runtime = SimpleNamespace(browser_session_id=None, input_values={}, request_replan=AsyncMock(), stop_task=AsyncMock(), browser_manager=SimpleNamespace(close_session=AsyncMock()))
        runtime.store = tools.store
        runtime.run_id = "main"
        runtime.task_id = task_id
        runtime.tester_id = tester_id
        runtime.required_checks = task["data_requirements"]["required_checks"]
        runtime.budget = BudgetGuard(_budget_limits(config, task_steps=task["step_budget"], remaining=config.budget.model_dump()))
        runtime.wait_for_shared_progress = MethodType(WebTestingRuntime.wait_for_shared_progress, runtime)

        async def start() -> str:
            runtime.browser_session_id = "browser-" + task_id
            sessions[task_id] = runtime.browser_session_id
            return runtime.browser_session_id

        runtime.start_session = start
        assignment = Assignment(run_id="main", task_id=task_id, tester_id=tester_id, identity_id=identity, role=role, data_namespace=task_id, scope=("/",), step_budget=90)
        workers[task_id] = WorkerTools(assignment=assignment, runtime=cast(WebTestingRuntime, runtime), store=tools.store)
        return WorkerInstance(tester_id, cast(Worker, SimpleNamespace(assignment=assignment, session=SimpleNamespace(session_id=tester_id), runtime=runtime)))

    def publish(task_id: str, signal: str) -> None:
        tools.store.append_event(event_id=signal, run_id="main", task_id=task_id, event_type="TASK_PROGRESS", tool="local", action="progress", result={"summary": signal}, latency_ms=0)

    async def execute(worker: Worker, task: Mapping[str, Any]) -> str:
        task_id = task["task_id"]
        started.add(task_id)
        if len(started) == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=3)
        if task_id == "offboarded-access":
            publish(task_id, "member-session-ready")
            waiting = await workers[task_id].wait_for_shared_progress("membership-removed", timeout_seconds=2)
            assert waiting["ready"], waiting
            assert worker.runtime.browser_session_id == sessions[task_id]
            publish(task_id, "member-delete-observed")
        else:
            waiting = await workers[task_id].wait_for_shared_progress("member-session-ready", timeout_seconds=2)
            assert waiting["ready"], waiting
            publish(task_id, "membership-removed")
            waiting = await workers[task_id].wait_for_shared_progress("member-delete-observed", timeout_seconds=2)
            assert waiting["ready"], waiting
        worker.runtime.request_replan.assert_not_awaited()  # type: ignore[attr-defined]
        return "COMPLETED"

    scheduler = LocalTesterScheduler(store=tools.store, run_id="main", tester_factory=factory, max_testers=config.budget.max_testers, max_browser_contexts=config.budget.max_parallel_browser_contexts)
    results = await scheduler.run_ready_tasks(execute)
    assert len(results) == 2 and all(result.status == "COMPLETED" for result in results), json.dumps([result.error for result in results])
    assert len(set(sessions.values())) == 2


def test_recovery_preserves_completed_check_evidence_and_original_budget(tmp_path: Path) -> None:
    tools, _ = tools_for(tmp_path, "D03")
    tools.submit_task_plan(drafts(tools))  # type: ignore[arg-type]
    with sqlite3.connect(tools.store.database_path) as connection:
        connection.execute("UPDATE tasks SET step_budget = ? WHERE task_id = ?", (37, "project-workflow"))
    original = tools.store.get_task("project-workflow")
    assert original is not None
    check = original["data_requirements"]["required_checks"][0]
    tools.store.create_identity(identity_id="record", run_id="main", role="admin", secret_reference=None, permissions=["read"])
    tools.store.register_tester(tester_id="record", run_id="main", session_reference="record", identity_id="record", role="admin", data_namespace="record")
    timestamp = datetime.now(UTC).isoformat()
    tools.store.append_event(event_id="first-check", run_id="main", task_id=original["task_id"], tester_id="record", event_type="BROWSER_ACTION", tool="local", action="assertion", result={"success": True}, latency_ms=0)
    tools.store.append_action_history(history_id="first-check", event_id="first-check", run_id="main", task_id=original["task_id"], tester_id="record", browser_session_id="local", url="http://app.test/", action="assertion", target="#original", tool="local", started_at=timestamp, ended_at=timestamp, latency_ms=0, success=True, error=None, result={"success": True}, action_data={"action_type": "assertion", "goal_check": True, "check_id": check["check_id"], "behavior_id": check["behavior_id"], "identity_reference": check["identity_reference"]})
    tools.store.update_task_status(task_id=original["task_id"], expected_status="PENDING", new_status="STOPPED")
    preserved = tools.store.get_task(original["task_id"])
    plan = drafts(tools)
    result = tools.submit_task_plan(plan)  # type: ignore[arg-type]
    assert result["ok"], result
    recovery = tools.store.get_task("project-workflow-remaining-1")
    assert recovery is not None
    assert check["check_id"] not in recovery["data_requirements"]["check_ids"]
    assert len(recovery["data_requirements"]["check_ids"]) == 3
    assert recovery["step_budget"] == tools.max_step_budget
    assert original["step_budget"] == 37
    assert tools.store.get_task(original["task_id"]) == preserved
    assert len(tools.store.list_action_history(run_id="main", task_id=original["task_id"])) == 1


def test_running_workflow_is_not_redirected_or_duplicated(tmp_path: Path) -> None:
    tools, _ = tools_for(tmp_path, "D03")
    tools.submit_task_plan(drafts(tools))  # type: ignore[arg-type]
    with sqlite3.connect(tools.store.database_path) as connection:
        connection.execute("UPDATE tasks SET step_budget = ? WHERE task_id = ?", (37, "project-workflow"))
    tools.store.create_identity(identity_id="worker", run_id="main", role="admin", secret_reference=None, permissions=["read"])
    tools.store.register_tester(tester_id="worker", run_id="main", session_reference="worker", identity_id="worker", role="admin", data_namespace="worker")
    tools.store.claim_task(task_id="project-workflow", tester_id="worker")
    original = tools.store.get_task("project-workflow")
    assert "project-workflow" not in tools.planning_contract()["editable_goal_ids"]
    result = tools.submit_task_plan(drafts(tools))  # type: ignore[arg-type]
    assert result["ok"] and tools.store.get_task("project-workflow") == original
    assert original is not None and original["step_budget"] == 37
    assert len(tools.store.list_tasks("main")) == 3


def test_live_component_waits_for_an_external_prerequisite_as_a_group(tmp_path: Path) -> None:
    config = load_run_config(Path("evaluation/scenarios.json"), scenario_id="D12", target_url="http://app.test/", tester_count=3)
    checks = [check.model_copy(update={"depends_on": ["ready"]}) if check.check_id == "D12.member-ready" else check for check in config.required_checks]
    checks.insert(0, checks[0].model_copy(update={"check_id": "ready", "goal_id": "prepare", "identity_reference": "admin", "depends_on": []}))
    config = config.model_copy(update={"required_checks": checks})
    tools, _ = tools_for(tmp_path, supplied_config=config)
    result = tools.submit_task_plan(drafts(tools))  # type: ignore[arg-type]
    assert result["ok"], result
    assert tools.store.get_task("offboarded-access")["dependencies"] == ["prepare"]  # type: ignore[index]
    assert tools.store.get_task("offboard-member")["dependencies"] == ["prepare"]  # type: ignore[index]
    assert tools.store.list_events("main", event_types=("TASK_CREATED",))[0]["task_id"] == "prepare"


@pytest.mark.asyncio
async def test_irrecoverable_live_replan_does_not_call_main_or_recreate_sessions(tmp_path: Path) -> None:
    tools, _ = tools_for(tmp_path)
    tools.submit_task_plan(drafts(tools))  # type: ignore[arg-type]
    tools.store.update_task_status(task_id="offboarded-access", expected_status="PENDING", new_status="STOPPED")

    class NeverCallClient(FunctionInvocationLayer, BaseChatClient):
        async def _inner_get_response(self, *, messages: Sequence[Message], stream: bool, options: Mapping[str, Any], **kwargs: Any) -> ChatResponse:
            raise AssertionError("Closed live sessions cannot be repaired by another planning request")

    runner = MainAgentRunner(agent=create_main_agent(client=NeverCallClient(), settings=Settings(_env_file=None), tools=tools), tools=tools, run_id="main", max_replans=2)
    assert await runner.replan("closed participant") is None
    assert runner.replan_count == 0 and len(tools.store.list_tasks("main")) == 2


@pytest.mark.asyncio
async def test_compiled_main_plan_runs_through_frozen_tester_runtime_and_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEMO_ADMIN_PASSWORD", "demo-admin")

    class MainClient(FunctionInvocationLayer, BaseChatClient):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def _inner_get_response(self, *, messages: Sequence[Message], stream: bool, options: Mapping[str, Any], **kwargs: Any) -> ChatResponse:
            self.calls += 1
            if "Final Report: " in messages[-1].text:
                assert not options.get("tools")
                return ChatResponse(messages=[Message(role="assistant", contents=["One complete role workflow passed."])])
            assert self.calls == 1
            task = {"goal_id": "rename-project", "goal": "Admin checks the assigned private Project rename and persistence", "priority": "P1", "required_operations": ["read", "update"]}
            return ChatResponse(messages=[Message(role="assistant", contents=[Content.from_function_call("main-plan", "submit_task_plan", arguments={"tasks": [task]})])])

    class WorkerClient(FunctionInvocationLayer, BaseChatClient):
        async def _inner_get_response(self, *, messages: Sequence[Message], stream: bool, options: Mapping[str, Any], **kwargs: Any) -> ChatResponse:
            contract = json.loads(messages[-1].text.split("Task Contract: ", 1)[1])
            assert contract["remaining_check_ids"] == ["D07.save-immediate", "D07.save-persisted"]
            check = {"action_type": "assertion", "control": "Name", "context": "seed_project_id", "assertion": "equals", "expected_reference": "edited_project_name", "behavior_id": "EB-PROJECT-SAVE"}
            goals = [{"goal": "Log in as the supplied admin", "operation": "login", "inputs": [{"control": "Login username", "value_reference": "admin_username"}, {"control": "Login password", "value_reference": "env:DEMO_ADMIN_PASSWORD"}]}, {"goal": "Rename the original private Project", "operation": "edit", "row_reference": "seed_project_id", "inputs": [{"control": "Project name", "value_reference": "edited_project_name"}], "checks": [{**check, "check_id": "D07.save-immediate"}]}, {"goal": "Verify the original Project after refresh", "operation": "observe", "run_operations": False, "before_steps": [{"action_type": "refresh"}], "checks": [{**check, "check_id": "D07.save-persisted"}]}]
            return ChatResponse(messages=[Message(role="assistant", contents=[Content.from_function_call("worker-plan", "execute_test_plan", arguments={"goals": goals})])])

    class LocalChoice:
        def predict(self, state: Mapping[str, Any], questions: Mapping[str, Any]) -> Mapping[str, Any]:
            assert questions["next_candidate"]["type"] == "choice"
            return {"answers": {"next_candidate": {"choice": state["legal_candidates"][0]["id"], "confidence": .95}}}

    settings = Settings(_env_file=None, langsmith_tracing=False, state_db_path=tmp_path / "system.db", artifacts_dir=tmp_path / "runs", temporary_sensitive_dir=tmp_path / "temporary")
    client = MainClient()
    with DemoAppServer(bugs=SeededBugs(b4_saved_ui_stale=False)) as app:
        config = load_run_config(Path("evaluation/scenarios.json"), scenario_id="D07", target_url=app.base_url)
        report_path = await run_system(config, settings, run_id="local-main-pipeline", main_client=client, tester_client_factory=WorkerClient, jev_selector=JevSelector(LocalChoice()))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert client.calls == 2
    assert report["test_summary"]["successful_tasks"] == 1
    assert report["task_outcomes"][0]["completed_check_count"] == 2
    assert not StateStore(settings.state_db_path).list_recent_findings("local-main-pipeline")
