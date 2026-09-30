from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from playwright.async_api import Page, Route

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
