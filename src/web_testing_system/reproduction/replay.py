"""Convert stored action history into deterministic Playwright replay steps."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any
from uuid import uuid4

from playwright.async_api import Page

from web_testing_system.evidence import EvidenceBuffer, EvidenceStore
from web_testing_system.runtime.browser import BrowserManager
from web_testing_system.runtime.budget import BudgetExceededError, BudgetGuard
from web_testing_system.runtime.models import ActionType, WebAction
from web_testing_system.runtime.playwright_executor import PlaywrightExecutor
from web_testing_system.state import StateStore

PreparePage = Callable[[Page], Awaitable[None]]
ResetHook = Callable[[], Awaitable[None]]
SUPPORTED_REPLAY_ACTIONS = {action.value for action in ActionType}


@dataclass(frozen=True)
class ReplayStep:
    action_type: ActionType
    target: str | None = None
    value_reference: str | None = None
    url: str | None = None
    expected: str | None = None
    assertion: str = "contains"
    key: str | None = None
    wait_ms: int = 0
    resource_id: str | None = None
    requires_resource: bool = False
    confirmed: bool = False
    timeout_ms: int = 2_000
    expected_success: bool = True
    expected_error_type: str | None = None

    def to_action(self, input_values: Mapping[str, str]) -> WebAction:
        value = None
        if self.value_reference is not None:
            try:
                value = input_values[self.value_reference]
            except KeyError as error:
                raise ValueError(f"missing replay input reference: {self.value_reference}") from error
        return WebAction(action_type=self.action_type, target=self.target, value=value, value_reference=self.value_reference, url=self.url, expected=self.expected, assertion=self.assertion, key=self.key, wait_ms=self.wait_ms, resource_id=self.resource_id, requires_resource=self.requires_resource, confirmed=self.confirmed, timeout_ms=self.timeout_ms)

    def to_record(self) -> dict[str, Any]:
        return {"action_type": self.action_type.value, "target": self.target, "value_reference": self.value_reference, "url": self.url, "expected": self.expected, "assertion": self.assertion, "key": self.key, "wait_ms": self.wait_ms, "resource_id": self.resource_id, "requires_resource": self.requires_resource, "confirmed": self.confirmed, "timeout_ms": self.timeout_ms, "expected_success": self.expected_success, "expected_error_type": self.expected_error_type}


@dataclass(frozen=True)
class ReplayPlan:
    run_id: str
    task_id: str
    tester_id: str
    identity_id: str
    data_requirements: dict[str, Any]
    steps: tuple[ReplayStep, ...]
    input_values: Mapping[str, str] = field(default_factory=dict, repr=False)

    @property
    def assertion_positions(self) -> tuple[int, ...]:
        return tuple(index for index, step in enumerate(self.steps) if step.action_type in {ActionType.ASSERTION, ActionType.URL_CHECK})


@dataclass(frozen=True)
class ReplayBuildResult:
    status: str
    plan: ReplayPlan | None
    reason: str | None = None


@dataclass(frozen=True)
class ReplayAttempt:
    attempt_id: str
    purpose: str
    fresh_data_mode: str
    browser_session_id: str | None
    matched: bool
    environment_issue: bool
    needs_ai_assistance: bool
    duration_ms: float
    action_results: tuple[dict[str, Any], ...]
    evidence_ids: tuple[str, ...]
    data_namespace: str | None = None
    reason: str | None = None


class ReplayPlanBuilder:
    def __init__(self, store: StateStore) -> None:
        self.store = store

    def from_action_history(self, *, run_id: str, task_id: str, input_values: Mapping[str, str], through_event_id: str | None = None) -> ReplayBuildResult:
        task = self.store.get_task(task_id)
        if task is None or task["run_id"] != run_id:
            return ReplayBuildResult("NEEDS_AI_ASSISTANCE", None, "TASK_MISSING")
        histories = self.store.list_action_history(run_id=run_id, task_id=task_id)
        steps: list[ReplayStep] = []
        boundary_found = through_event_id is None
        replay_tester_id: str | None = None
        for history in histories:
            replay_tester_id = str(history["tester_id"])
            action_data = history["action_data"]
            action_name = str(action_data.get("action_type", ""))
            if action_name not in SUPPORTED_REPLAY_ACTIONS:
                return ReplayBuildResult("NEEDS_AI_ASSISTANCE", None, f"UNSUPPORTED_RECORDED_ACTION:{action_name or history['action']}")
            value_reference = action_data.get("value_reference")
            if action_name in {ActionType.INPUT.value, ActionType.SELECT.value} and value_reference is None:
                return ReplayBuildResult("NEEDS_AI_ASSISTANCE", None, "RECORDED_INPUT_REFERENCE_MISSING")
            if value_reference is not None and value_reference not in input_values:
                return ReplayBuildResult("NEEDS_AI_ASSISTANCE", None, f"INPUT_VALUE_UNAVAILABLE:{value_reference}")
            steps.append(ReplayStep(action_type=ActionType(action_name), target=action_data.get("target"), value_reference=value_reference, url=action_data.get("url"), expected=action_data.get("expected"), assertion=str(action_data.get("assertion", "contains")), key=action_data.get("key"), wait_ms=int(action_data.get("wait_ms", 0)), resource_id=action_data.get("resource_id"), requires_resource=bool(action_data.get("requires_resource", False)), confirmed=bool(action_data.get("confirmed", False)), timeout_ms=int(action_data.get("timeout_ms", 2_000)), expected_success=bool(history["success"]), expected_error_type=history["result"].get("error_type")))
            if through_event_id is not None and history["event_id"] == through_event_id:
                boundary_found = True
                break
        if not steps or not boundary_found:
            return ReplayBuildResult("NEEDS_AI_ASSISTANCE", None, "REPLAY_BOUNDARY_NOT_FOUND")
        assert replay_tester_id is not None
        tester = self.store.get_tester(replay_tester_id)
        if tester is None:
            return ReplayBuildResult("NEEDS_AI_ASSISTANCE", None, "TESTER_IDENTITY_MISSING")
        data_requirements = dict(task["data_requirements"])
        data_requirements.setdefault("data_namespace", tester["data_namespace"])
        plan = ReplayPlan(run_id=run_id, task_id=task_id, tester_id=replay_tester_id, identity_id=str(tester["identity_id"]), data_requirements=data_requirements, steps=tuple(steps), input_values=dict(input_values))
        return ReplayBuildResult("READY", plan)

    def from_finding(self, *, finding_id: str, input_values: Mapping[str, str]) -> ReplayBuildResult:
        finding = self.store.get_finding(finding_id)
        if finding is None:
            return ReplayBuildResult("NEEDS_AI_ASSISTANCE", None, "FINDING_MISSING")
        task = self.store.get_task(str(finding["task_id"]))
        tester = self.store.get_tester(str(finding["first_seen_by"]))
        if task is None or tester is None:
            return ReplayBuildResult("NEEDS_AI_ASSISTANCE", None, "TASK_OR_TESTER_MISSING")
        steps: list[ReplayStep] = []
        for step_data in finding["reproduction_steps"]:
            action_name = str(step_data.get("action_type", ""))
            if action_name not in SUPPORTED_REPLAY_ACTIONS:
                return ReplayBuildResult("NEEDS_AI_ASSISTANCE", None, f"UNSUPPORTED_RECORDED_ACTION:{action_name}")
            value_reference = step_data.get("value_reference")
            if value_reference is not None and value_reference not in input_values:
                return ReplayBuildResult("NEEDS_AI_ASSISTANCE", None, f"INPUT_VALUE_UNAVAILABLE:{value_reference}")
            steps.append(ReplayStep(action_type=ActionType(action_name), target=step_data.get("target"), value_reference=value_reference, url=step_data.get("url"), expected=step_data.get("expected"), assertion=str(step_data.get("assertion", "contains")), key=step_data.get("key"), wait_ms=int(step_data.get("wait_ms", 0)), resource_id=step_data.get("resource_id"), requires_resource=bool(step_data.get("requires_resource", False)), confirmed=bool(step_data.get("confirmed", False)), timeout_ms=int(step_data.get("timeout_ms", 2_000)), expected_success=bool(step_data.get("expected_success", True)), expected_error_type=step_data.get("expected_error_type")))
        if not steps:
            return ReplayBuildResult("NEEDS_AI_ASSISTANCE", None, "STABLE_REPRODUCTION_STEPS_MISSING")
        data_requirements = dict(task["data_requirements"])
        data_requirements.setdefault("data_namespace", tester["data_namespace"])
        plan = ReplayPlan(run_id=str(finding["run_id"]), task_id=str(finding["task_id"]), tester_id=str(finding["first_seen_by"]), identity_id=str(tester["identity_id"]), data_requirements=data_requirements, steps=tuple(steps), input_values=dict(input_values))
        return ReplayBuildResult("READY", plan)


class DeterministicReplay:
    """Execute one known replay attempt in a fresh Browser Context."""

    def __init__(self, *, store: StateStore, browser_manager: BrowserManager, executor: PlaywrightExecutor, evidence_store: EvidenceStore, budget: BudgetGuard, budget_id: str) -> None:
        self.store = store
        self.browser_manager = browser_manager
        self.executor = executor
        self.evidence_store = evidence_store
        self.budget = budget
        self.budget_id = budget_id

    async def run_attempt(self, *, finding_id: str, plan: ReplayPlan, purpose: str, reset_hook: ResetHook | None = None, prepare_page: PreparePage | None = None) -> ReplayAttempt:
        attempt_id = f"{purpose.lower()}-{uuid4().hex}"
        base_namespace = str(plan.data_requirements.get("data_namespace") or f"run-{plan.run_id}-tester-{plan.tester_id}-task-{plan.task_id}")
        data_namespace = f"{base_namespace}-{attempt_id}"
        fresh_data_mode = "FRESH_DATA_ONLY"
        browser_session_id: str | None = None
        started = perf_counter()
        results: list[dict[str, Any]] = []
        evidence_ids: list[str] = []
        matched = False
        environment_issue = False
        needs_ai_assistance = False
        reason: str | None = None
        if reset_hook is not None:
            await reset_hook()
            fresh_data_mode = "RESET_AND_FRESH_DATA"
        try:
            session = await self.browser_manager.create_session(tester_id=plan.tester_id, identity_id=plan.identity_id)
            browser_session_id = session.session_id
            buffer = EvidenceBuffer()
            buffer.attach(session.page)
            marker = buffer.mark()
            await self.evidence_store.start_trace(session.context)
            if prepare_page is not None:
                await prepare_page(session.page)
            matched = True
            for step in plan.steps:
                try:
                    action = step.to_action(plan.input_values)
                except ValueError as error:
                    matched = False
                    needs_ai_assistance = True
                    reason = str(error)
                    break
                try:
                    self.budget.ensure_can_start("browser_step")
                except BudgetExceededError as error:
                    matched = False
                    reason = error.reason
                    break
                action_result = await self.executor.execute(page=session.page, browser_session_id=session.session_id, action=action)
                self.budget.record_browser_step(runtime_seconds=action_result.latency_ms / 1_000)
                self.store.update_budget(budget_id=self.budget_id, browser_steps=1, runtime_seconds=action_result.latency_ms / 1_000)
                result_data = action_result.to_dict()
                results.append(result_data)
                if action_result.error_type in {"PAGE_TIMEOUT", "ACTION_ERROR"} and action_result.error_type != step.expected_error_type:
                    matched = False
                    environment_issue = True
                    reason = action_result.error_type
                    break
                if action_result.success != step.expected_success or (not step.expected_success and step.expected_error_type is not None and action_result.error_type != step.expected_error_type):
                    matched = False
                    reason = "RECORDED_OUTCOME_MISMATCH"
                    break
            screenshot, _ = await self.evidence_store.capture_screenshot(page=session.page, run_id=plan.run_id, task_id=plan.task_id, finding_id=finding_id, attempt_id=attempt_id, browser_session_id=session.session_id, name=f"{purpose.lower()}-screenshot")
            evidence_ids.append(str(screenshot["evidence_id"]))
            dom = await self.evidence_store.capture_dom(page=session.page, run_id=plan.run_id, task_id=plan.task_id, finding_id=finding_id, attempt_id=attempt_id, browser_session_id=session.session_id)
            evidence_ids.append(str(dom["evidence_id"]))
            network = self.evidence_store.capture_network(buffer=buffer, marker=marker, run_id=plan.run_id, task_id=plan.task_id, finding_id=finding_id, attempt_id=attempt_id, browser_session_id=session.session_id, url=session.page.url)
            evidence_ids.append(str(network["evidence_id"]))
            console = self.evidence_store.capture_console(buffer=buffer, marker=marker, run_id=plan.run_id, task_id=plan.task_id, finding_id=finding_id, attempt_id=attempt_id, browser_session_id=session.session_id, url=session.page.url)
            evidence_ids.append(str(console["evidence_id"]))
            trace = await self.evidence_store.capture_trace(context=session.context, run_id=plan.run_id, task_id=plan.task_id, finding_id=finding_id, attempt_id=attempt_id, browser_session_id=session.session_id, url=session.page.url)
            evidence_ids.append(str(trace["evidence_id"]))
        except BudgetExceededError as error:
            matched = False
            environment_issue = False
            needs_ai_assistance = False
            reason = error.reason
        except Exception as error:
            matched = False
            environment_issue = True
            needs_ai_assistance = False
            reason = f"{type(error).__name__}: {error}"
        finally:
            if browser_session_id is not None:
                await self.browser_manager.close_session(browser_session_id)
        duration_ms = (perf_counter() - started) * 1_000
        attempt = ReplayAttempt(attempt_id=attempt_id, purpose=purpose, fresh_data_mode=fresh_data_mode, browser_session_id=browser_session_id, matched=matched, environment_issue=environment_issue, needs_ai_assistance=needs_ai_assistance, duration_ms=duration_ms, action_results=tuple(results), evidence_ids=tuple(evidence_ids), data_namespace=data_namespace, reason=reason)
        self.store.append_event(event_id=f"event-{uuid4().hex}", run_id=plan.run_id, task_id=plan.task_id, tester_id=plan.tester_id, browser_session_id=browser_session_id, event_type=f"{purpose}_ATTEMPT", tool="DeterministicReplay", action="replay_known_steps", result={"finding_id": finding_id, "attempt_id": attempt_id, "fresh_data_mode": fresh_data_mode, "matched": matched, "environment_issue": environment_issue, "needs_ai_assistance": needs_ai_assistance, "duration_ms": duration_ms, "reason": reason, "action_results": results, "identity_id": plan.identity_id, "data_namespace": data_namespace, "data_requirements": plan.data_requirements, "assertion_positions": plan.assertion_positions}, evidence_references=evidence_ids, latency_ms=duration_ms)
        return attempt
