from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from agent_framework import BaseChatClient, ChatResponse, Content, Message
from agent_framework._middleware import ChatMiddlewareLayer
from agent_framework._tools import FunctionInvocationLayer

from demo_app.app import DemoAppServer
from demo_app.bugs import SeededBugs
from web_testing_system.config import (
    AccountReference,
    BudgetConfig,
    ExpectedBehavior,
    ExpectedBehaviorSource,
    ResetHookConfig,
    RunConfig,
    Settings,
)
from web_testing_system.orchestration.runner import run
from web_testing_system.runtime.budget import BudgetGuard, BudgetLimits
from web_testing_system.runtime.jev_selector import JevSelector
from web_testing_system.runtime.models import ActionResult, ActionType, WebAction
from web_testing_system.runtime.web_runtime import WebTestingRuntime
from web_testing_system.state import StateStore


class PlanningClient(FunctionInvocationLayer, ChatMiddlewareLayer, BaseChatClient):
    def __init__(self, tasks: list[dict[str, Any]]) -> None:
        super().__init__()
        self.tasks = tasks
        self.calls = 0
        self.summary_facts: dict[str, Any] | None = None
        self.summary_calls = 0

    async def _inner_get_response(self, *, messages: Sequence[Message], stream: bool, options: Mapping[str, Any], **kwargs: Any) -> ChatResponse:
        self.calls += 1
        if "Final Report: " in messages[-1].text:
            assert not options.get("tools"), "Manager final review must have no mutating tools"
            self.summary_calls += 1
            self.summary_facts = json.loads(messages[-1].text.split("Final Report: ", 1)[1])
            summary = self.summary_facts["test_summary"]
            return ChatResponse(messages=[Message(role="assistant", contents=[f"Tested {summary['total_tasks']} tasks; {summary['successful_tasks']} passed, {summary['unknown_success_tasks']} uncertain. See report.json for findings and evidence."])])
        if self.calls <= len(self.tasks):
            content = Content.from_function_call(f"plan-{self.calls}", "create_task", arguments=self.tasks[self.calls - 1])
        else:
            content = Content.from_text("Plan recorded.")
        return ChatResponse(messages=[Message(role="assistant", contents=[content])])


def task(task_id: str, *, feature: str = "Project", role: str = "admin", requirements: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"task_id": task_id, "goal": f"Test {feature}", "feature": feature, "priority": "P1", "dependencies": [], "step_budget": 50, "data_requirements": {"role": role, **(requirements or {})}, "scope_targets": ["/"], "required_operations": ["read"]}


def config(url: str, *, bug: bool = False) -> RunConfig:
    return RunConfig(target_url=url, test_goal="Test browser workflows", focus_features=["Task" if bug else "Project"], allowed_scope=["/"], account_references=[AccountReference(identity_reference="member-id" if bug else "admin-id", role="member" if bug else "admin", permissions=["read", "delete"], secret_reference="env:ARCH_TEST_PASSWORD" if bug else None)], test_data={"member_username": "member"} if bug else {}, denied_operations=["delete production data"], expected_behaviors=[ExpectedBehavior(behavior_id="EB-delete", description="Deleted Task stays absent after refresh", applies_to="Task/member/delete", source=ExpectedBehaviorSource.USER_PROVIDED)] if bug else [], reset_hook=ResetHookConfig(hook_type="HTTP_POST", target="/test/reset") if bug else None, budget=BudgetConfig(max_testers=2, max_parallel_browser_contexts=2))


def settings(path: Path) -> Settings:
    return Settings(_env_file=None, state_db_path=path / "state.db", artifacts_dir=path / "runs", temporary_sensitive_dir=path / "temporary")


class FakeJev:
    def predict(self, state: Mapping[str, Any], questions: Mapping[str, Any]) -> Mapping[str, Any]:
        candidates = state["legal_candidates"]
        selected = next(item for item in candidates if item["label"] == "Tasks")
        return {"answers": {"next_candidate": {"choice": selected["id"], "confidence": 1}}}


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("conflicting_role", [False, True])
async def test_identity_reference_selects_its_role_without_a_duplicate_role_field(tmp_path: Path, conflicting_role: bool) -> None:
    class WorkerClient(FunctionInvocationLayer, BaseChatClient):
        async def _inner_get_response(self, *, messages: Sequence[Message], stream: bool, options: Mapping[str, Any], **kwargs: Any) -> ChatResponse:
            return ChatResponse(messages=[Message(role="assistant", contents=["Done."])])

    references = {"admin-ref": "admin", "member-ref": "member", "other-member-ref": "member"}
    plans = []
    for reference in references:
        planned = task(reference, requirements={"identity_reference": reference})
        planned["data_requirements"].pop("role")
        plans.append(planned)
    if conflicting_role:
        plans[1]["data_requirements"]["role"] = "admin"
    with DemoAppServer() as app:
        run_config = config(app.base_url).model_copy(update={"account_references": [AccountReference(identity_reference=reference, role=role, permissions=["read"]) for reference, role in references.items()], "budget": BudgetConfig(max_testers=3, max_parallel_browser_contexts=3)})
        if conflicting_role:
            with pytest.raises(ValueError, match="unambiguous identity_reference for role admin"):
                await run(run_config, settings(tmp_path), main_client=PlanningClient(plans), tester_client_factory=WorkerClient, jev_selector=JevSelector(FakeJev()))
            return
        report_path = await run(run_config, settings(tmp_path), main_client=PlanningClient(plans), tester_client_factory=WorkerClient, jev_selector=JevSelector(FakeJev()))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    run_id = report["test_summary"]["run_id"]
    testers = StateStore(tmp_path / "state.db").list_testers(run_id)
    assert report["test_summary"]["run_status"] == "COMPLETED"
    assert len(testers) == 3
    for tester in testers:
        reference = tester["identity_id"].removeprefix(f"{run_id}-")
        assert tester["role"] == references[reference]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ordered_known_actions_share_one_model_turn_and_preserve_goal_checks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARCH_BATCH_PASSWORD", "demo-admin")

    class WorkerClient(FunctionInvocationLayer, ChatMiddlewareLayer, BaseChatClient):
        calls = 0

        async def _inner_get_response(self, *, messages: Sequence[Message], stream: bool, options: Mapping[str, Any], **kwargs: Any) -> ChatResponse:
            type(self).calls += 1
            assert type(self).calls <= 2
            if type(self).calls == 2:
                return ChatResponse(messages=[Message(role="assistant", contents=[Content.from_function_call("finish", "finish_task", arguments={})])])
            actions = [
                {"action_type": "input", "target": "#login-username", "value_reference": "admin_username"},
                {"action_type": "input", "target": "#login-password", "value_reference": "env:ARCH_BATCH_PASSWORD"},
                {"action_type": "click", "target": "#login-form button[type='submit']"},
                {"action_type": "assertion", "target": "#app-section", "assertion": "visible", "goal_check": True, "behavior_id": "EB-session"},
            ]
            contents = [Content.from_function_call(f"action-{index}", "execute_known_action", arguments=arguments) for index, arguments in enumerate(actions)]
            return ChatResponse(messages=[Message(role="assistant", contents=contents)])

    with DemoAppServer() as app:
        run_config = config(app.base_url).model_copy(update={"account_references": [AccountReference(identity_reference="admin-id", role="admin", permissions=["login", "read"], secret_reference="env:ARCH_BATCH_PASSWORD")], "test_data": {"admin_username": "admin"}, "expected_behaviors": [ExpectedBehavior(behavior_id="EB-session", description="Valid login displays the app", applies_to="Project/admin/session", source=ExpectedBehaviorSource.USER_PROVIDED)]})
        report_path = await run(run_config, settings(tmp_path), main_client=PlanningClient([task("batch-login", requirements={"identity_reference": "admin-id", "expected_behavior_ids": ["EB-session"]})]), tester_client_factory=WorkerClient, jev_selector=JevSelector(FakeJev()))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    history = StateStore(tmp_path / "state.db").list_action_history(run_id=report["test_summary"]["run_id"], task_id="batch-login")
    assert [step["action"] for step in history[-4:]] == ["input", "input", "click", "assertion"]
    assert all(step["success"] for step in history[-4:])
    assert report["task_outcomes"][0]["success_status"] == "PASS"
    assert WorkerClient.calls == 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_formal_scheduler_refills_before_slow_sibling_finishes(tmp_path: Path) -> None:
    third_started = asyncio.Event()
    order: list[str] = []

    class WorkerClient(FunctionInvocationLayer, BaseChatClient):
        async def _inner_get_response(self, *, messages: Sequence[Message], stream: bool, options: Mapping[str, Any], **kwargs: Any) -> ChatResponse:
            text = "\n".join(message.text for message in messages)
            number = re.search(r"Execute assigned Task task-(\d)", text).group(1)
            order.append(f"start-{number}")
            if number == "0":
                await asyncio.wait_for(third_started.wait(), timeout=5)
            if number == "2":
                third_started.set()
            order.append(f"end-{number}")
            return ChatResponse(messages=[Message(role="assistant", contents=["Done."])])

    with DemoAppServer() as app:
        report_path = await run(config(app.base_url), settings(tmp_path), main_client=PlanningClient([task(f"task-{number}") for number in range(4)]), tester_client_factory=WorkerClient, jev_selector=JevSelector(FakeJev()))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert order.index("start-2") < order.index("end-0")
    assert report["test_summary"]["peak_concurrent_testers"] == 2
    assert report["test_summary"]["completed_tasks"] == 4
    assert report["test_summary"]["successful_tasks"] == 0
    assert report["test_summary"]["unknown_success_tasks"] == 4


@pytest.mark.integration
@pytest.mark.asyncio
async def test_formal_entry_reproduces_verifies_and_reports_seeded_bug(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARCH_TEST_PASSWORD", "demo-member")
    row = "#tasks-table tr[data-task-id='task-1']"
    actions = [
        ("execute_known_action", {"action_type": "input", "target": "#login-username", "value_reference": "member_username"}),
        ("execute_known_action", {"action_type": "input", "target": "#login-password", "value_reference": "env:ARCH_TEST_PASSWORD"}),
        ("execute_known_action", {"action_type": "click", "target": "#login-form button[type='submit']"}),
        ("execute_known_action", {"action_type": "wait", "target": "#app-section"}),
        ("explore_unknown_path", {"current_goal": "Open Tasks"}),
        ("execute_known_action", {"action_type": "wait", "target": row}),
        ("execute_known_action", {"action_type": "click", "target": row + " .delete-task"}),
        ("execute_known_action", {"action_type": "assertion", "target": row, "assertion": "hidden"}),
        ("execute_known_action", {"action_type": "refresh"}),
        ("execute_known_action", {"action_type": "wait", "target": "#app-section"}),
        ("execute_known_action", {"action_type": "click", "target": "#nav-tasks"}),
        ("execute_known_action", {"action_type": "wait", "target": row}),
        ("execute_known_action", {"action_type": "assertion", "target": row, "assertion": "hidden", "behavior_id": "EB-delete"}),
        ("record_finding", {"title": "Deleted Task reappears", "status": "OBSERVATION", "expected_result": "Deleted Task stays absent after refresh", "actual_result": "Deleted Task is visible after refresh", "affected_page": "/", "action": "refresh"}),
        ("finish_task", {}),
    ]

    class TestingClient(FunctionInvocationLayer, ChatMiddlewareLayer, BaseChatClient):
        calls = 0
        prompts: list[str] = []

        async def _inner_get_response(self, *, messages: Sequence[Message], stream: bool, options: Mapping[str, Any], **kwargs: Any) -> ChatResponse:
            assert type(self).calls < len(actions), "finish_task must terminate without an extra Done request"
            type(self).prompts.append("\n".join(message.text for message in messages))
            name, arguments = actions[type(self).calls]
            type(self).calls += 1
            return ChatResponse(messages=[Message(role="assistant", contents=[Content.from_function_call(f"action-{type(self).calls}", name, arguments=arguments)])])

    requirements = {"identity_reference": "member-id", "expected_behavior_ids": ["EB-delete"], "test_data_keys": ["member_username"]}
    main_client = PlanningClient([task("task-delete", feature="Task", role="member", requirements=requirements)])
    with DemoAppServer(bugs=SeededBugs(b1_deleted_task_reappears=True)) as app:
        report_path = await run(config(app.base_url, bug=True), settings(tmp_path), main_client=main_client, tester_client_factory=TestingClient, jev_selector=JevSelector(FakeJev()))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert len(report["confirmed_bugs"]) == 1
    assert main_client.summary_calls == 1
    assert main_client.summary_facts["confirmed_bugs"] == report["confirmed_bugs"]
    assert main_client.summary_facts["task_outcomes"] == report["task_outcomes"]
    assert report["main_final_summary"]["status"] == "COMPLETED"
    assert report["cost_and_performance"]["llm_by_phase"]["main_final_summary"]["requests"] == 1
    assert report_path.with_name("summary.md").is_file()
    assert report["confirmed_bugs"][0]["verification"] == "FAIL"
    assert report["confirmed_bugs"][0]["stable_reproduction_steps"]
    assert report["task_outcomes"][0]["execution_status"] == "COMPLETED"
    assert report["task_outcomes"][0]["success_status"] == "PASS"
    assert report["task_outcomes"][0]["application_behavior"] == "FAIL"
    assert TestingClient.calls == len(actions)
    assert all("demo-member" not in prompt for prompt in TestingClient.prompts)
    assert "EB-delete" in TestingClient.prompts[0] and "member_username" in TestingClient.prompts[0]
    assert "target_url" in TestingClient.prompts[0]
    evidence = report["confirmed_bugs"][0]["evidence"]
    assert {item["type"] for item in evidence} >= {"SCREENSHOT", "DOM", "NETWORK", "CONSOLE", "TRACE"}
    assert all((settings(tmp_path).artifacts_dir / item["relative_path"]).is_file() for item in evidence)
    store = StateStore(settings(tmp_path).state_db_path)
    histories = store.list_action_history(run_id=report["test_summary"]["run_id"], task_id="task-delete")
    assert "demo-member" not in json.dumps(histories)
    assert len(store.list_events(report["test_summary"]["run_id"], event_types=("JEV_CALL",))) == 1
    assert len(store.list_events(report["test_summary"]["run_id"], event_types=("REPRODUCTION_ATTEMPT",))) == 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_page_mutations_are_serialized_and_goal_success_is_deterministic(phase2_store: StateStore) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    calls: list[str] = []

    class Executor:
        async def execute(self, *, page: object, browser_session_id: str, action: WebAction) -> ActionResult:
            calls.append(action.action_type.value)
            if action.action_type == ActionType.CLICK:
                entered.set()
                await release.wait()
            return ActionResult(started_at="start", ended_at="end", latency_ms=0, success=True, error=None, error_type=None)

    budget = BudgetGuard(BudgetLimits(max_runtime_seconds=60, max_llm_calls=10, max_input_tokens=1000, max_output_tokens=1000, max_jev_calls=5, max_computer_use_calls=0, max_task_steps=50, max_task_replans=2, max_browser_contexts=2))
    session = SimpleNamespace(page=SimpleNamespace(url="http://app.test"), session_id="browser-1")
    manager = SimpleNamespace(get_session=lambda identifier: session)
    runtime = WebTestingRuntime(store=phase2_store, browser_manager=manager, executor=Executor(), page_state_reader=None, candidate_builder=None, jev_selector=None, budget=budget, run_id="run-1", task_id="task-1", tester_id="tester-1", identity_id="identity-1", budget_id="budget-1")
    runtime.browser_session_id = "browser-1"
    first = asyncio.create_task(runtime.execute_known_action(WebAction(action_type=ActionType.CLICK, target="#save")))
    await entered.wait()
    second = asyncio.create_task(runtime.execute_known_action(WebAction(action_type=ActionType.REFRESH)))
    await asyncio.sleep(0)
    assert calls == ["click"]
    release.set()
    await asyncio.gather(first, second)
    assert calls == ["click", "refresh"]
    assert (await runtime.record_task_outcome())["success_status"] == "UNKNOWN"
    phase2_store.append_event(event_id="goal-event", run_id="run-1", task_id="task-1", tester_id="tester-1", event_type="BROWSER_ACTION", action="assertion", result={"success": True}, latency_ms=0)
    phase2_store.append_action_history(history_id="goal-history", event_id="goal-event", run_id="run-1", task_id="task-1", tester_id="tester-1", browser_session_id="browser-1", url="http://app.test", action="assertion", target="#result", tool="Playwright", started_at="start", ended_at="end", latency_ms=0, success=True, error=None, result={"success": True}, action_data={"action_type": "assertion", "goal_check": True})
    assert (await runtime.record_task_outcome())["success_status"] == "PASS"
