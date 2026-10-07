from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from playwright.async_api import Page, Route

from web_testing_system.agents import TesterAgentTools as AgentTools
from web_testing_system.agents import TesterAssignment as Assignment
from web_testing_system.runtime.browser import BrowserManager
from web_testing_system.runtime.budget import (
    BudgetExceededError,
    BudgetGuard,
    BudgetLimits,
)
from web_testing_system.runtime.candidates import CandidateBuilder, PageStateReader
from web_testing_system.runtime.jev_selector import JevSelector
from web_testing_system.runtime.models import ActionResult, ActionType, WebAction
from web_testing_system.runtime.permissions import ExecutionPolicy, PermissionChecker
from web_testing_system.runtime.playwright_executor import PlaywrightExecutor
from web_testing_system.runtime.web_runtime import WebTestingRuntime
from web_testing_system.state import StateStore

HTML = "<main><button id='open'>Open Project</button><p id='status'>ready</p></main>"


class FakeJevClient:
    def __init__(self) -> None:
        self.mode = "success"
        self.call_count = 0

    def predict(
        self, state: Mapping[str, Any], questions: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        self.call_count += 1
        if self.mode == "failure":
            raise RuntimeError("selector unavailable")
        criteria = questions["next_candidate"]["criteria"]
        selected_id = next(
            candidate_id
            for candidate_id, description in criteria.items()
            if "Open Project" in description
        )
        if self.mode == "illegal":
            selected_id = "candidate-forged"
        confidence = 0.2 if self.mode == "low" else 0.95
        return {
            "answers": {
                "next_candidate": {
                    "choice": selected_id,
                    "confidence": confidence,
                    "probabilities": {selected_id: confidence},
                }
            },
            "model": "typesafe/jev-1.13-test",
            "usage": {"input_tokens": 12, "output_tokens": 1, "cost": 0.001},
        }


def make_budget(max_steps: int = 20, max_contexts: int = 1) -> BudgetGuard:
    return BudgetGuard(
        BudgetLimits(
            max_runtime_seconds=60,
            max_llm_calls=2,
            max_input_tokens=1_000,
            max_output_tokens=1_000,
            max_jev_calls=10,
            max_computer_use_calls=0,
            max_task_steps=max_steps,
            max_task_replans=2,
            max_browser_contexts=max_contexts,
            max_no_progress_steps=2,
        )
    )


def build_runtime(
    store: StateStore, budget: BudgetGuard, jev_client: FakeJevClient
) -> WebTestingRuntime:
    checker = PermissionChecker(
        store=store,
        policy=ExecutionPolicy(allowed_url_prefixes=("http://app.test/",)),
        run_id="run-1",
        task_id="task-1",
        tester_id="tester-1",
    )
    manager = BrowserManager(budget)
    return WebTestingRuntime(
        store=store,
        browser_manager=manager,
        executor=PlaywrightExecutor(
            store=store,
            permission_checker=checker,
            run_id="run-1",
            task_id="task-1",
            tester_id="tester-1",
        ),
        page_state_reader=PageStateReader(),
        candidate_builder=CandidateBuilder(checker),
        jev_selector=JevSelector(jev_client),
        budget=budget,
        run_id="run-1",
        task_id="task-1",
        tester_id="tester-1",
        identity_id="identity-1",
        budget_id="budget-1",
    )


async def install_page(page: Page) -> None:
    async def handle(route: Route) -> None:
        await route.fulfill(status=200, content_type="text/html", body=HTML)

    await page.route("http://app.test/**", handle)


@pytest.mark.asyncio
async def test_default_inspection_and_finish_contract_require_actual_goal_checks(phase2_store: StateStore) -> None:
    runtime = build_runtime(phase2_store, make_budget(), FakeJevClient())
    runtime.expected_behavior_ids = ("EB-check",)
    session_id = await runtime.start_session()
    session = runtime.browser_manager.get_session(session_id)
    await install_page(session.page)
    assignment = Assignment(run_id="run-1", task_id="task-1", tester_id="tester-1", identity_id="identity-1", role="admin", data_namespace="test", scope=("http://app.test/",), step_budget=20)
    tools = AgentTools(assignment=assignment, runtime=runtime, store=phase2_store)
    try:
        await runtime.execute_known_action(WebAction(action_type=ActionType.NAVIGATION, url="http://app.test/projects"))
        inspection = await tools.execute_known_action(ActionType.DOM_INSPECTION)
        assert inspection["success"]
        assert inspection["data"]["interactive_elements"]
        rejected_finish = await tools.finish_task()
        assert rejected_finish["missing_behavior_ids"] == ["EB-check"]
        assert rejected_finish["finished"] is False
        assert runtime.task_finished is False
        check = await tools.execute_known_action(ActionType.ASSERTION, target="#status", expected="ready", goal_check=True)
        assert check["success"]
        assert phase2_store.list_action_history(run_id="run-1", task_id="task-1")[-1]["action_data"]["behavior_id"] == "EB-check"
        completed = await tools.finish_task()
        assert completed["finished"] is True
        assert completed["success_status"] == "PASS"
        assert runtime.task_finished is True
    finally:
        await runtime.browser_manager.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_llm_decision_route_uses_candidate_id_and_runtime_revalidation(phase2_store: StateStore) -> None:
    jev_client = FakeJevClient()
    runtime = build_runtime(phase2_store, make_budget(), jev_client)
    session_id = await runtime.start_session()
    session = runtime.browser_manager.get_session(session_id)
    await install_page(session.page)
    assignment = Assignment(run_id="run-1", task_id="task-1", tester_id="tester-1", identity_id="identity-1", role="admin", data_namespace="test", scope=("http://app.test/",), step_budget=20)
    tools = AgentTools(assignment=assignment, runtime=runtime, store=phase2_store, decision_policy="TESTER_LLM_EVERY_DECISION")
    try:
        await runtime.execute_known_action(WebAction(action_type=ActionType.NAVIGATION, url="http://app.test/projects"))
        choices = await tools.explore_unknown_path("Open Project")
        candidate_id = next(candidate["candidate_id"] for candidate in choices["candidates"] if candidate["label"] == "Open Project")
        assert (await tools.select_candidate("candidate-forged"))["error_type"] == "ILLEGAL_CANDIDATE_ID"
        assert (await tools.select_candidate(candidate_id))["success"] is True
        assert jev_client.call_count == 0
        choices = await tools.explore_unknown_path("Open Project")
        candidate_id = next(candidate["candidate_id"] for candidate in choices["candidates"] if candidate["label"] == "Open Project")
        await session.page.set_content("<main>Changed</main>")
        assert (await tools.select_candidate(candidate_id))["error_type"] == "CANDIDATE_EXPIRED"
    finally:
        await runtime.browser_manager.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_layered_decision_jev_fallback_checkpoint_and_crash_recovery(
    phase2_store: StateStore,
) -> None:
    budget = make_budget()
    jev_client = FakeJevClient()
    runtime = build_runtime(phase2_store, budget, jev_client)
    session_id = await runtime.start_session()
    session = runtime.browser_manager.get_session(session_id)
    await install_page(session.page)
    try:
        navigation = await runtime.execute_known_action(
            WebAction(action_type=ActionType.NAVIGATION, url="http://app.test/projects")
        )
        success = await runtime.explore_unknown_path("Open Project")

        assert navigation.success is True
        inspection = await runtime.execute_known_action(WebAction(action_type=ActionType.DOM_INSPECTION, target="body"))
        control = next(item for item in inspection.data["interactive_elements"] if item["label"] == "Open Project")
        assert await session.page.locator(control["target"]).count() == 1
        assert "value" not in control
        assert success.source == "JEV_TO_PLAYWRIGHT"
        assert (
            success.action_result is not None and success.action_result.success is True
        )
        assert jev_client.call_count == 1
        first_jev_event = next(event for event in phase2_store.list_events("run-1") if event["event_type"] == "JEV_CALL")
        assert first_jev_event["result"]["input_tokens"] == 12
        assert first_jev_event["result"]["model"] == "typesafe/jev-1.13-test"
        saved_budget = phase2_store.get_budget("budget-1")
        assert saved_budget is not None
        assert budget.snapshot()["input_tokens"] == saved_budget["input_tokens"]
        assert budget.snapshot()["jev_calls"] == saved_budget["jev_calls"]
        assert budget.snapshot()["runtime_seconds"] == pytest.approx(saved_budget["runtime_seconds"])

        for mode, expected_reason in (
            ("low", "LOW_JEV_CONFIDENCE"),
            ("illegal", "ILLEGAL_CANDIDATE_ID"),
            ("failure", "JEV_ERROR: RuntimeError"),
        ):
            jev_client.mode = mode
            fallback = await runtime.explore_unknown_path("Open Project")
            assert fallback.source == "TESTER_LLM"
            assert fallback.needs_tester_llm is True
            assert fallback.reason == expected_reason

        old_session_id = runtime.browser_session_id
        assert old_session_id is not None
        await runtime.browser_manager.get_session(old_session_id).context.close()

        async def login(page: Page) -> None:
            await install_page(page)
            await page.context.add_cookies(
                [{"name": "login", "value": "restored", "url": "http://app.test"}]
            )

        recovered = await runtime.recover_after_browser_crash(login)
        assert recovered is not None
        assert recovered["browser_session_id"] != old_session_id
        restored_session = runtime.browser_manager.get_session(
            runtime.browser_session_id or ""
        )
        assert (await restored_session.context.cookies())[0]["value"] == "restored"

        await runtime.note_progress(progressed=False, summary="same page")
        await runtime.note_progress(progressed=False, summary="still same page")
        await runtime.stop_task("USER_STOP")

        assert phase2_store.get_task("task-1")["status"] == "STOPPED"  # type: ignore[index]
        assert phase2_store.list_action_history(run_id="run-1", task_id="task-1")
        assert phase2_store.list_paths("run-1")
        event_types = {
            event["event_type"] for event in phase2_store.list_events("run-1")
        }
        assert {
            "BROWSER_ACTION",
            "JEV_CALL",
            "SAFE_CHECKPOINT",
            "REQUEST_REPLAN",
            "STOP_CONDITION",
        } <= event_types
        assert runtime.browser_session_id is None
        assert runtime.task_finished
        after_stop = await runtime.execute_known_action(WebAction(action_type=ActionType.CLICK, target="#open"))
        assert after_stop.error == "TASK_ALREADY_FINISHED"
        assert "storage_state" not in str(phase2_store.list_events("run-1")).lower()
    finally:
        await runtime.browser_manager.close()


class FakePage:
    url = "http://app.test/"


class FakeSession:
    session_id = "browser-1"
    page = FakePage()


class FakeBrowserManager:
    def __init__(self) -> None:
        self.closed = False

    def get_session(self, session_id: str) -> FakeSession:
        return FakeSession()

    async def close_session(self, session_id: str) -> None:
        self.closed = True


class FakeExecutor:
    def __init__(self, error_type: str) -> None:
        self.error_type = error_type
        self.call_count = 0

    async def execute(self, **kwargs: Any) -> ActionResult:
        self.call_count += 1
        return ActionResult(
            started_at="start",
            ended_at="end",
            latency_ms=1,
            success=False,
            error=self.error_type,
            error_type=self.error_type,
        )


def fake_runtime(
    store: StateStore, executor: FakeExecutor, budget: BudgetGuard
) -> WebTestingRuntime:
    runtime = WebTestingRuntime(
        store=store,
        browser_manager=FakeBrowserManager(),  # type: ignore[arg-type]
        executor=executor,  # type: ignore[arg-type]
        page_state_reader=PageStateReader(),
        candidate_builder=None,  # type: ignore[arg-type]
        jev_selector=None,  # type: ignore[arg-type]
        budget=budget,
        run_id="run-1",
        task_id="task-1",
        tester_id="tester-1",
        identity_id="identity-1",
        budget_id="budget-1",
        max_timeout_retries=1,
    )
    runtime.browser_session_id = "browser-1"
    return runtime


@pytest.mark.integration
@pytest.mark.asyncio
async def test_timeout_retries_once_but_assertion_failure_does_not_retry(
    phase2_store: StateStore,
) -> None:
    timeout_executor = FakeExecutor("PAGE_TIMEOUT")
    runtime = fake_runtime(phase2_store, timeout_executor, make_budget())

    timeout_result = await runtime.execute_known_action(
        WebAction(action_type=ActionType.CLICK, target="#late")
    )

    assert timeout_result.success is False
    assert timeout_executor.call_count == 2
    assert (
        phase2_store.list_recent_findings("run-1")[0]["status"] == "ENVIRONMENT_ISSUE"
    )

    assertion_executor = FakeExecutor("ASSERTION_FAILURE")
    runtime.executor = assertion_executor  # type: ignore[assignment]
    assertion_result = await runtime.execute_known_action(
        WebAction(action_type=ActionType.ASSERTION, target="#status", expected="ready")
    )

    assert assertion_result.success is False
    assert assertion_executor.call_count == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_budget_exhaustion_stops_before_starting_action(
    phase2_store: StateStore,
) -> None:
    action_executor = FakeExecutor("SHOULD_NOT_RUN")
    runtime = fake_runtime(phase2_store, action_executor, make_budget(max_steps=0))

    result = await runtime.execute_known_action(
        WebAction(action_type=ActionType.CLICK, target="#open")
    )

    assert result.error_type == "BUDGET_EXCEEDED"
    assert action_executor.call_count == 0
    assert phase2_store.get_task("task-1")["status"] == "STOPPED"  # type: ignore[index]
    stop_event = next(
        event
        for event in phase2_store.list_events("run-1")
        if event["event_type"] == "STOP_CONDITION"
    )
    assert stop_event["result"]["reason"] == "MAX_TASK_STEPS_REACHED"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_context_budget_exhaustion_records_stop_before_browser_start(
    phase2_store: StateStore,
) -> None:
    runtime = build_runtime(
        phase2_store,
        make_budget(max_contexts=0),
        FakeJevClient(),
    )

    with pytest.raises(BudgetExceededError, match="MAX_BROWSER_CONTEXTS_REACHED"):
        await runtime.start_session()

    assert runtime.browser_manager.browser is None
    assert phase2_store.get_task("task-1")["status"] == "STOPPED"  # type: ignore[index]
    stop_event = next(
        event
        for event in phase2_store.list_events("run-1")
        if event["event_type"] == "STOP_CONDITION"
    )
    assert stop_event["result"]["reason"] == "MAX_BROWSER_CONTEXTS_REACHED"


@pytest.mark.asyncio
async def test_semantic_steps_execute_in_one_model_request_and_record_failed_check_before_next_action(phase2_store: StateStore) -> None:
    from collections.abc import Sequence

    from agent_framework import BaseChatClient, ChatResponse, Content, Message
    from agent_framework._tools import FunctionInvocationLayer

    from web_testing_system.agents.tester_agent import create_tester_agent
    from web_testing_system.reproduction import ReplayPlanBuilder

    class BatchClient(FunctionInvocationLayer, BaseChatClient):
        def __init__(self) -> None:
            super().__init__()
            self.call_count = 0

        async def _inner_get_response(self, *, messages: Sequence[Message], stream: bool, options: Mapping[str, Any], **kwargs: Any) -> ChatResponse:
            self.call_count += 1
            assert self.call_count == 1
            steps = [{"action_type": "input", "control": "Project name", "value_reference": "name"}, {"action_type": "click", "control": "Store project"}, {"action_type": "assertion", "target": "#status", "assertion": "equals", "expected": "other", "behavior_id": "EB-check"}, {"action_type": "refresh"}, {"action_type": "assertion", "target": "#status", "assertion": "equals", "expected": "ready", "behavior_id": "EB-check"}]
            return ChatResponse(messages=[Message(role="assistant", contents=[Content.from_function_call("batch", "execute_page_steps", arguments={"steps": steps, "finish": True})])])

    client = BatchClient()
    jev = FakeJevClient()
    runtime = build_runtime(phase2_store, make_budget(), jev)
    runtime.expected_behavior_ids = ("EB-check",)
    runtime.input_values = {"name": "configured-project"}
    session_id = await runtime.start_session()
    page = runtime.browser_manager.get_session(session_id).page
    html = '<input id="name" aria-label="Project name"><button onclick="document.querySelector(\'#status\').textContent=\'saved\'">Store project</button><p id="status">ready</p>'

    async def handle(route: Route) -> None:
        await route.fulfill(status=200, content_type="text/html", body=html)

    await page.route("http://app.test/**", handle)
    assignment = Assignment(run_id="run-1", task_id="task-1", tester_id="tester-1", identity_id="identity-1", role="member", data_namespace="test", scope=("http://app.test/",), step_budget=20)
    tools = AgentTools(assignment=assignment, runtime=runtime, store=phase2_store)
    try:
        await runtime.execute_known_action(WebAction(action_type=ActionType.NAVIGATION, url="http://app.test/"))
        await create_tester_agent(client=client, assignment=assignment, tools=tools).run("Execute the Task")
        assert client.call_count == 1
        assert jev.call_count == 0
        assert runtime.task_finished
        findings = phase2_store.list_recent_findings("run-1")
        assert len(findings) == 1
        events = phase2_store.list_events("run-1")
        created = next(event for event in events if event["event_type"] == "FINDING_CREATED")
        history = phase2_store.list_action_history(run_id="run-1", task_id="task-1")
        failed = next(item for item in history if not item["success"])
        assert created["result"]["boundary_event_id"] == failed["event_id"]
        assert created["result"]["behavior_id"] == "EB-check"
        assert history[1]["action_data"]["value_reference"] == "name"
        plan = ReplayPlanBuilder(phase2_store).from_finding(finding_id=findings[0]["finding_id"], input_values=runtime.input_values)
        assert plan.plan is not None
        assert plan.plan.steps[-1].expected_success is False
        assert plan.plan.steps[-1].behavior_id == "EB-check"
        assert phase2_store.get_task("task-1")["success_reason"] == "APPLICATION_BUG_DETECTED"  # type: ignore[index]
    finally:
        await runtime.browser_manager.close()


@pytest.mark.asyncio
async def test_semantic_resolution_uses_jev_only_for_ambiguity_and_honors_confidence(phase2_store: StateStore) -> None:
    jev = FakeJevClient()
    runtime = build_runtime(phase2_store, make_budget(), jev)
    session_id = await runtime.start_session()
    page = runtime.browser_manager.get_session(session_id).page
    await install_page(page)
    try:
        await runtime.execute_known_action(WebAction(action_type=ActionType.NAVIGATION, url="http://app.test/"))
        exact = await runtime.execute_page_action(WebAction(action_type=ActionType.CLICK), control="Open Project")
        assert exact.source == "PLAYWRIGHT"
        assert jev.call_count == 0
        selected = await runtime.execute_page_action(WebAction(action_type=ActionType.CLICK), control="open the workspace")
        assert selected.source == "JEV_TO_PLAYWRIGHT"
        assert jev.call_count == 1
        previous = len(phase2_store.list_action_history(run_id="run-1", task_id="task-1"))
        jev.mode = "low"
        refused = await runtime.execute_page_action(WebAction(action_type=ActionType.CLICK), control="open the workspace")
        assert refused.reason == "LOW_JEV_CONFIDENCE"
        assert len(phase2_store.list_action_history(run_id="run-1", task_id="task-1")) == previous
        await page.set_content('<a href="https://evil.test/outside">External</a>')
        external = await runtime.execute_page_action(WebAction(action_type=ActionType.CLICK), control="External")
        assert external.reason == "CONTROL_NOT_FOUND"
        assert jev.call_count == 2
    finally:
        await runtime.browser_manager.close()


@pytest.mark.asyncio
async def test_batch_rejects_missing_inputs_and_stops_before_later_actions(phase2_store: StateStore) -> None:
    from web_testing_system.agents.tester_agent import PageStep

    runtime = build_runtime(phase2_store, make_budget(), FakeJevClient())
    runtime.input_values = {"name": "configured"}
    session_id = await runtime.start_session()
    page = runtime.browser_manager.get_session(session_id).page
    await install_page(page)
    assignment = Assignment(run_id="run-1", task_id="task-1", tester_id="tester-1", identity_id="identity-1", role="member", data_namespace="test", scope=("http://app.test/",), step_budget=20)
    tools = AgentTools(assignment=assignment, runtime=runtime, store=phase2_store)
    try:
        await runtime.execute_known_action(WebAction(action_type=ActionType.NAVIGATION, url="http://app.test/"))
        rejected = await tools.execute_page_steps([PageStep(ActionType.INPUT, control="name")])
        assert rejected["error_type"] == "INPUT_REFERENCE_REQUIRED"
        failed = await tools.execute_page_steps([PageStep(ActionType.CLICK, target="#missing"), PageStep(ActionType.CLICK, target="#open")])
        assert failed["failed_step"] == 0
        assert len(phase2_store.list_action_history(run_id="run-1", task_id="task-1")) == 2
    finally:
        await runtime.browser_manager.close()


@pytest.mark.asyncio
async def test_shared_wait_detects_planning_deadlock_without_polling_the_model(phase2_store: StateStore) -> None:
    runtime = build_runtime(phase2_store, make_budget(), FakeJevClient())
    phase2_store.create_task(task_id="participant", run_id="run-1", goal="Publish removal", priority="P0", dependencies=["task-1"], created_by="main", step_budget=10)
    assignment = Assignment(run_id="run-1", task_id="task-1", tester_id="tester-1", identity_id="identity-1", role="member", data_namespace="test", scope=("http://app.test/",), step_budget=20)
    tools = AgentTools(assignment=assignment, runtime=runtime, store=phase2_store)
    result = await tools.wait_for_shared_progress("membership-removed", task_id="participant")
    assert result["reason"] == "LIVE_PARTICIPANT_DEPENDS_ON_OBSERVER"
    assert runtime.task_finished
    assert phase2_store.get_task("task-1")["status"] == "STOPPED"  # type: ignore[index]
    assert runtime.budget.snapshot()["llm_calls"] == 0


@pytest.mark.asyncio
async def test_shared_wait_reads_the_other_task_signal_without_browser_actions(phase2_store: StateStore) -> None:
    runtime = build_runtime(phase2_store, make_budget(), FakeJevClient())
    phase2_store.create_task(task_id="participant", run_id="run-1", goal="Publish removal", priority="P0", dependencies=[], created_by="main", step_budget=10)
    phase2_store.append_event(event_id="signal", run_id="run-1", task_id="participant", event_type="TASK_PROGRESS", tool="TesterAgent", action="update_progress", result={"summary": "membership-removed"}, latency_ms=0)
    assignment = Assignment(run_id="run-1", task_id="task-1", tester_id="tester-1", identity_id="identity-1", role="member", data_namespace="test", scope=("http://app.test/",), step_budget=20)
    result = await AgentTools(assignment=assignment, runtime=runtime, store=phase2_store).wait_for_shared_progress("membership-removed", timeout_seconds=0)
    assert result["ready"]
    assert result["event_id"] == "signal"
    assert not runtime.task_finished
    assert not phase2_store.list_action_history(run_id="run-1", task_id="task-1")


@pytest.mark.asyncio
async def test_modal_binding_column_assertions_and_duplicate_checks(phase2_store: StateStore) -> None:
    from web_testing_system.agents.tester_agent import PageStep

    runtime = build_runtime(phase2_store, make_budget(), FakeJevClient())
    runtime.input_values = {"new_name": "new-project"}
    runtime.expected_behavior_ids = ("EB-check",)
    session_id = await runtime.start_session()
    page = runtime.browser_manager.get_session(session_id).page
    html = '<input id="background" aria-label="Project name"><table id="items"><thead><tr><th>Name</th><th>Owner</th></tr></thead><tbody><tr data-key="entity-7"><td>old-project</td><td>member</td></tr></tbody></table><dialog id="editor"><input id="edit" aria-label="Edit value"><button onclick="document.querySelector(\'#editor\').close()">Save</button></dialog>'

    async def handle(route: Route) -> None:
        await route.fulfill(status=200, content_type="text/html", body=html)

    await page.route("http://app.test/**", handle)
    assignment = Assignment(run_id="run-1", task_id="task-1", tester_id="tester-1", identity_id="identity-1", role="member", data_namespace="test", scope=("http://app.test/",), step_budget=20)
    tools = AgentTools(assignment=assignment, runtime=runtime, store=phase2_store)
    try:
        await runtime.execute_known_action(WebAction(action_type=ActionType.NAVIGATION, url="http://app.test/"))
        await page.locator("#editor").evaluate("dialog => dialog.showModal()")
        state = await runtime.page_state_reader.read(page)
        assert {element.label for element in state.interactive_elements} == {"Edit value", "Save"}
        invalid = await tools.execute_known_action(ActionType.INPUT, target="#background", value_reference="new_name")
        assert invalid["error_type"] == "INVALID_ACTION"
        completed = await tools.execute_page_steps([PageStep(ActionType.INPUT, control="Edit value", value_reference="new_name"), PageStep(ActionType.CLICK, control="Save"), PageStep(ActionType.ASSERTION, control="Name", context="entity-7", expected_reference="new_name", assertion="equals", behavior_id="EB-check")])
        assert completed["results"][-1]["actual"] == "old-project"
        assert await page.locator("#edit").input_value() == "new-project"
        assert await page.locator("#background").input_value() == ""
        await tools.execute_page_steps([PageStep(ActionType.ASSERTION, control="Name", context="entity-7", expected_reference="new_name", assertion="equals", behavior_id="EB-check")])
        assert len(phase2_store.list_recent_findings("run-1")) == 1
        rejected = await tools.execute_page_steps([PageStep(ActionType.ASSERTION, target="body", assertion="visible", expected="new-project", behavior_id="EB-check")])
        assert rejected["error_type"] == "VISIBILITY_DOES_NOT_COMPARE_TEXT"
        waited = await tools.execute_page_steps([PageStep(ActionType.WAIT, wait_ms=5)])
        assert waited["success"]
        await tools.execute_known_action(ActionType.WAIT, target="5")
        assert phase2_store.list_action_history(run_id="run-1", task_id="task-1")[-1]["action_data"]["wait_ms"] == 5
        await page.locator('tr[data-key="entity-7"]').evaluate("row => row.remove()")
        missing = await tools.execute_page_steps([PageStep(ActionType.ASSERTION, control="Name", context="entity-7", expected_reference="new_name", assertion="equals", behavior_id="EB-check")])
        assert missing["results"][-1]["error_type"] == "ASSERTION_FAILURE"
        assert missing["results"][-1]["actual"] == ""
        assert len(phase2_store.list_recent_findings("run-1")) == 1
    finally:
        await runtime.browser_manager.close()


@pytest.mark.asyncio
async def test_prepared_login_handoff_finishes_with_one_fake_model_call_and_keeps_replay_references(phase2_store: StateStore) -> None:
    from collections.abc import Sequence

    from agent_framework import BaseChatClient, ChatResponse, Content, Message
    from agent_framework._tools import FunctionInvocationLayer

    from web_testing_system.agents.tester_agent import TesterRunner as Runner
    from web_testing_system.agents.tester_agent import create_tester_agent

    class PreparedClient(FunctionInvocationLayer, BaseChatClient):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def _inner_get_response(self, *, messages: Sequence[Message], stream: bool, options: Mapping[str, Any], **kwargs: Any) -> ChatResponse:
            self.calls += 1
            assert self.calls == 1
            text = "\n".join(message.text for message in messages)
            assert "AUTHENTICATED" in text
            assert "member (member)" in text
            assert "secret-value" not in text
            step = {"action_type": "assertion", "control": "Name", "context": "entity-7", "expected_reference": "name", "assertion": "equals", "behavior_id": "EB-check"}
            return ChatResponse(messages=[Message(role="assistant", contents=[Content.from_function_call("prepared", "execute_page_steps", arguments={"steps": [step], "finish": True})])])

    runtime = build_runtime(phase2_store, make_budget(), FakeJevClient())
    runtime.expected_behavior_ids = ("EB-check",)
    runtime.input_values = {"member_username": "member", "env:TEST_PASSWORD": "secret-value", "name": "named-project"}
    session_id = await runtime.start_session()
    page = runtime.browser_manager.get_session(session_id).page
    html = """<form id="login"><input id="username" aria-label="Login username"><input id="password" aria-label="Login password"><button type="button" onclick="document.body.innerHTML=document.querySelector('#logged-in').innerHTML">Login</button></form><template id="logged-in"><p>member (member)</p><button>Logout</button><table id="items"><thead><tr><th>Name</th></tr></thead><tbody><tr data-key="entity-7"><td>named-project</td></tr></tbody></table></template>"""

    async def handle(route: Route) -> None:
        await route.fulfill(status=200, content_type="text/html", body=html)

    await page.route("http://app.test/**", handle)
    assignment = Assignment(run_id="run-1", task_id="task-1", tester_id="tester-1", identity_id="identity-1", role="member", data_namespace="test", scope=("http://app.test/",), step_budget=20)
    tools = AgentTools(assignment=assignment, runtime=runtime, store=phase2_store, task_context={"identity_reference": "member", "secret_reference": "env:TEST_PASSWORD", "required_operations": ["read"]})
    client = PreparedClient()
    runner = Runner(agent=create_tester_agent(client=client, assignment=assignment, tools=tools), assignment=assignment, runtime=runtime, store=phase2_store, budget=runtime.budget, budget_id="budget-1", prepare_task_page=True)
    try:
        await runtime.execute_known_action(WebAction(action_type=ActionType.NAVIGATION, url="http://app.test/"))
        await runner.ask_tester_llm("Execute assigned Task task-1: check the project")
        assert client.calls == 1
        assert runtime.task_finished
        histories = phase2_store.list_action_history(run_id="run-1", task_id="task-1")
        assert [item["action_data"]["value_reference"] for item in histories if item["action"] == "input"] == ["member_username", "env:TEST_PASSWORD"]
        assert phase2_store.get_task("task-1")["success_status"] == "PASS"  # type: ignore[index]
        steps = phase2_store.list_events("run-1", event_types=("PAGE_STEP",))
        assert steps[-1]["result"]["semantic_control"]
    finally:
        await runtime.browser_manager.close()


@pytest.mark.asyncio
async def test_removed_member_assertion_does_not_bind_to_member2(phase2_store: StateStore) -> None:
    runtime = build_runtime(phase2_store, make_budget(), FakeJevClient())
    runtime.input_values = {"removed_username": "member"}
    session_id = await runtime.start_session()
    page = runtime.browser_manager.get_session(session_id).page
    html = '<table id="members"><thead><tr><th>Username</th><th>Name</th><th>Role</th></tr></thead><tbody><tr data-username="member2"><td>member2</td><td>Member Two</td><td>member</td></tr><tr data-username="member"><td>member</td><td>Member One</td><td>member</td></tr></tbody></table>'

    async def handle(route: Route) -> None:
        await route.fulfill(status=200, content_type="text/html", body=html)

    await page.route("http://app.test/**", handle)
    try:
        await runtime.execute_known_action(WebAction(action_type=ActionType.NAVIGATION, url="http://app.test/"))
        runtime.remember_assertion_targets(await runtime.page_state_reader.read_rows(page))
        await page.locator('tr[data-username="member"]').evaluate("row => row.remove()")
        result = await runtime.execute_page_action(WebAction(action_type=ActionType.ASSERTION, assertion="hidden"), control="Name", context="member")
        assert result.action_result is not None and result.action_result.success
        assert result.candidate is not None and 'data-username="member"' in str(result.candidate.target)
    finally:
        await runtime.browser_manager.close()
