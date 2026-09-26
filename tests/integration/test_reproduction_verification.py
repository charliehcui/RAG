from __future__ import annotations

import pytest
from playwright.async_api import Page, Route

from web_testing_system.evidence import EvidenceStore
from web_testing_system.findings import FindingService
from web_testing_system.reproduction import (
    ReplayPlan,
    ReplayPlanBuilder,
    ReproductionRunner,
)
from web_testing_system.reproduction.replay import DeterministicReplay, ReplayAttempt
from web_testing_system.runtime.browser import BrowserManager
from web_testing_system.runtime.budget import BudgetGuard, BudgetLimits
from web_testing_system.runtime.models import ActionType, WebAction
from web_testing_system.runtime.permissions import ExecutionPolicy, PermissionChecker
from web_testing_system.runtime.playwright_executor import PlaywrightExecutor
from web_testing_system.state import StateStore
from web_testing_system.verification import VerificationRunner

HTML = "<main><p id='status'>ready</p></main>"


class FakeReplay:
    def __init__(self, *, environment_issue: bool = False) -> None:
        self.environment_issue = environment_issue

    async def run_attempt(self, **kwargs: object) -> ReplayAttempt:
        del kwargs
        reason = "PAGE_TIMEOUT" if self.environment_issue else "RECORDED_OUTCOME_MISMATCH"
        return ReplayAttempt(attempt_id="fake-attempt", purpose="REPRODUCTION", fresh_data_mode="FRESH_DATA_ONLY", browser_session_id="fake-browser", matched=False, environment_issue=self.environment_issue, needs_ai_assistance=False, duration_ms=1, action_results=(), evidence_ids=(), data_namespace="fake-namespace", reason=reason)


def replay_budget() -> BudgetGuard:
    return BudgetGuard(BudgetLimits(max_runtime_seconds=60, max_llm_calls=0, max_input_tokens=0, max_output_tokens=0, max_laya_calls=0, max_computer_use_calls=0, max_task_steps=30, max_task_replans=0, max_browser_contexts=1))


def replay_executor(store: StateStore) -> PlaywrightExecutor:
    checker = PermissionChecker(store=store, policy=ExecutionPolicy(allowed_url_prefixes=("http://app.test/",)), run_id="run-1", task_id="task-1", tester_id="tester-1")
    return PlaywrightExecutor(store=store, permission_checker=checker, run_id="run-1", task_id="task-1", tester_id="tester-1")


async def prepare_page(page: Page) -> None:
    async def handle(route: Route) -> None:
        await route.fulfill(status=200, content_type="text/html", body=HTML)

    await page.route("http://app.test/**", handle)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_finding_reproduces_in_fresh_context_then_verification_confirms_bug(phase2_store: StateStore) -> None:
    budget = replay_budget()
    manager = BrowserManager(budget)
    executor = replay_executor(phase2_store)
    original_session = await manager.create_session(tester_id="tester-1", identity_id="identity-1")
    await prepare_page(original_session.page)
    navigation = await executor.execute(page=original_session.page, browser_session_id=original_session.session_id, action=WebAction(action_type=ActionType.NAVIGATION, url="http://app.test/"))
    inspection = await executor.execute(page=original_session.page, browser_session_id=original_session.session_id, action=WebAction(action_type=ActionType.DOM_INSPECTION, target="#status"))
    assertion = await executor.execute(page=original_session.page, browser_session_id=original_session.session_id, action=WebAction(action_type=ActionType.ASSERTION, target="#status", expected="deleted", assertion="contains"))
    assert navigation.success and inspection.success
    assert assertion.error_type == "ASSERTION_FAILURE"
    original_history_ids = {item["history_id"] for item in phase2_store.list_action_history(run_id="run-1", task_id="task-1")}
    await manager.close_session(original_session.session_id)

    phase2_store.create_finding(finding_id="finding-replay", run_id="run-1", task_id="task-1", title="Deleted Project remains visible", status="ANOMALY", expected_result="deleted", actual_result="ready", first_seen_by="tester-1", severity_hint="HIGH", needs_confirmation=True, affected_page="/", action="assertion", error_text="text assertion failed")
    build_result = ReplayPlanBuilder(phase2_store).from_action_history(run_id="run-1", task_id="task-1", input_values={}, through_event_id=assertion.event_id)
    assert build_result.status == "READY"
    assert build_result.plan is not None
    assert build_result.plan.assertion_positions == (2,)

    test_root = phase2_store.database_path.parent
    evidence_store = EvidenceStore(store=phase2_store, artifacts_root=test_root / "artifacts" / "runs", temporary_sensitive_root=test_root / "temporary-sensitive")
    deterministic_replay = DeterministicReplay(store=phase2_store, browser_manager=manager, executor=executor, evidence_store=evidence_store, budget=budget, budget_id="budget-1")
    finding_service = FindingService(phase2_store, "run-1")
    reproduction = await ReproductionRunner(store=phase2_store, finding_service=finding_service, replay=deterministic_replay).run(finding_id="finding-replay", plan=build_result.plan, max_attempts=2, stable_successes=2, max_minimization_attempts=2, prepare_page=prepare_page)

    assert reproduction.status == "REPRODUCED"
    assert reproduction.reproduction_count == 4
    assert reproduction.reproduction_success_count == 3
    assert reproduction.reproduction_rate == pytest.approx(0.75)
    assert len(reproduction.stable_steps) == 2
    assert all(attempt.fresh_data_mode == "FRESH_DATA_ONLY" for attempt in reproduction.attempts)
    assert len({attempt.data_namespace for attempt in reproduction.attempts}) == 4
    assert len({attempt.browser_session_id for attempt in reproduction.attempts}) == 4
    assert original_history_ids <= {item["history_id"] for item in phase2_store.list_action_history(run_id="run-1", task_id="task-1")}

    verification_build = ReplayPlanBuilder(phase2_store).from_finding(finding_id="finding-replay", input_values={})
    assert verification_build.status == "READY"
    assert verification_build.plan is not None
    reset_calls = 0

    async def reset_hook() -> None:
        nonlocal reset_calls
        reset_calls += 1

    verification = await VerificationRunner(store=phase2_store, finding_service=finding_service, replay=deterministic_replay).run(finding_id="finding-replay", plan=verification_build.plan, reset_hook=reset_hook, prepare_page=prepare_page)
    await manager.close()

    assert verification.result == "FAIL"
    assert reset_calls == 1
    assert verification.attempt is not None
    assert verification.attempt.fresh_data_mode == "RESET_AND_FRESH_DATA"
    confirmed = phase2_store.get_finding("finding-replay")
    assert confirmed is not None
    assert confirmed["status"] == "CONFIRMED_BUG"
    assert confirmed["verification_result"] == "FAIL"
    assert confirmed["verification_details"]["duration_ms"] > 0
    event_types = [event["event_type"] for event in phase2_store.list_events("run-1")]
    assert event_types.count("REPRODUCTION_ATTEMPT") == 2
    assert event_types.count("MINIMIZATION_ATTEMPT") == 2
    assert event_types.count("VERIFICATION_ATTEMPT") == 1
    assert "VERIFICATION_RESULT" in event_types
    assert "LAYA_CALL" not in event_types
    assert "TESTER_LLM_CALL" not in event_types


@pytest.mark.integration
@pytest.mark.asyncio
async def test_replay_missing_input_and_verification_missing_expected_need_confirmation(phase2_store: StateStore) -> None:
    phase2_store.append_event(event_id="event-input", run_id="run-1", task_id="task-1", tester_id="tester-1", browser_session_id="browser-original", event_type="BROWSER_ACTION", tool="Playwright", action="input", result={"success": True}, latency_ms=0)
    phase2_store.append_action_history(history_id="history-input", event_id="event-input", run_id="run-1", task_id="task-1", tester_id="tester-1", browser_session_id="browser-original", url="http://app.test/", action="input", target="#name", tool="Playwright", started_at="2026-01-01T00:00:00+00:00", ended_at="2026-01-01T00:00:00+00:00", latency_ms=0, success=True, error=None, result={"success": True}, action_data={"action_type": "input", "target": "#name"})
    build_result = ReplayPlanBuilder(phase2_store).from_action_history(run_id="run-1", task_id="task-1", input_values={})
    assert build_result.status == "NEEDS_AI_ASSISTANCE"
    assert build_result.reason == "RECORDED_INPUT_REFERENCE_MISSING"

    phase2_store.create_finding(finding_id="finding-unknown-expected", run_id="run-1", task_id="task-1", title="Unknown expected behavior", status="REPRODUCED", expected_result="", actual_result="unexpected", first_seen_by="tester-1", needs_confirmation=True)
    budget = replay_budget()
    manager = BrowserManager(budget)
    test_root = phase2_store.database_path.parent
    replay = DeterministicReplay(store=phase2_store, browser_manager=manager, executor=replay_executor(phase2_store), evidence_store=EvidenceStore(store=phase2_store, artifacts_root=test_root / "artifacts" / "runs", temporary_sensitive_root=test_root / "temporary-sensitive"), budget=budget, budget_id="budget-1")
    empty_plan = ReplayPlan(run_id="run-1", task_id="task-1", tester_id="tester-1", identity_id="identity-1", data_requirements={}, steps=())
    result = await VerificationRunner(store=phase2_store, finding_service=FindingService(phase2_store, "run-1"), replay=replay).run(finding_id="finding-unknown-expected", plan=empty_plan)
    await manager.close()

    assert result.result == "NEEDS_CONFIRMATION"
    assert result.reason == "EXPECTED_BEHAVIOR_MISSING"
    assert result.attempt is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_failed_reproduction_and_invalid_verification_steps_do_not_confirm_bug(phase2_store: StateStore) -> None:
    phase2_store.create_finding(finding_id="finding-not-reproduced", run_id="run-1", task_id="task-1", title="Intermittent observation", status="ANOMALY", expected_result="expected", actual_result="actual", first_seen_by="tester-1", needs_confirmation=True)
    plan = ReplayPlan(run_id="run-1", task_id="task-1", tester_id="tester-1", identity_id="identity-1", data_requirements={}, steps=())
    service = FindingService(phase2_store, "run-1")
    reproduction = await ReproductionRunner(store=phase2_store, finding_service=service, replay=FakeReplay()).run(finding_id="finding-not-reproduced", plan=plan, max_attempts=2, stable_successes=2, max_minimization_attempts=0)  # type: ignore[arg-type]

    assert reproduction.status == "NOT_REPRODUCED"
    assert reproduction.reproduction_count == 2
    assert reproduction.reproduction_success_count == 0
    assert phase2_store.get_finding("finding-not-reproduced")["status"] == "NOT_REPRODUCED"  # type: ignore[index]

    phase2_store.update_finding_status(finding_id="finding-not-reproduced", status="REPRODUCED")
    verification = await VerificationRunner(store=phase2_store, finding_service=service, replay=FakeReplay()).run(finding_id="finding-not-reproduced", plan=plan)  # type: ignore[arg-type]
    assert verification.result == "NEEDS_CONFIRMATION"
    assert verification.reason == "NEEDS_AI_ASSISTANCE"
    assert phase2_store.get_finding("finding-not-reproduced")["status"] == "NEEDS_CONFIRMATION"  # type: ignore[index]
    assistance_event = next(event for event in phase2_store.list_events("run-1") if event["event_type"] == "NEEDS_AI_ASSISTANCE")
    assert assistance_event["tester_id"] == "tester-1"

    phase2_store.create_finding(finding_id="finding-environment", run_id="run-1", task_id="task-1", title="Timeout", status="ANOMALY", expected_result="page loads", actual_result="timeout", first_seen_by="tester-1")
    environment_result = await ReproductionRunner(store=phase2_store, finding_service=service, replay=FakeReplay(environment_issue=True)).run(finding_id="finding-environment", plan=plan, max_attempts=1, stable_successes=1, max_minimization_attempts=0)  # type: ignore[arg-type]
    assert environment_result.status == "ENVIRONMENT_ISSUE"
    assert phase2_store.get_finding("finding-environment")["status"] == "ENVIRONMENT_ISSUE"  # type: ignore[index]
