"""Controlled paid OpenRouter visual fallback for one browser action."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter
from typing import Any, Protocol
from uuid import uuid4

from openai import AsyncOpenAI
from playwright.async_api import Page

from web_testing_system.config import Settings
from web_testing_system.evidence import EvidenceStore
from web_testing_system.observability import trace_result, trace_span
from web_testing_system.runtime.budget import BudgetExceededError, BudgetGuard
from web_testing_system.runtime.candidates import PageStateReader
from web_testing_system.runtime.models import ActionType, PageState, WebAction
from web_testing_system.runtime.permissions import PermissionChecker
from web_testing_system.state import StateStore

VISUAL_COMPONENTS = {"canvas", "custom_drawing", "special_chart", "complex_visual", "unstable_dom"}
ORDINARY_COMPONENTS = {"button", "input", "checkbox", "link", "select", "table", "modal", "navigation"}
SUPPORTED_VISUAL_ACTIONS = {"click", "drag"}


@dataclass(frozen=True)
class ComputerUseRequest:
    screenshot: bytes
    goal: str
    url: str
    allowed_visual_actions: tuple[str, ...]
    remaining_budget: int


@dataclass(frozen=True)
class ComputerUseDecision:
    status: str
    action: str | None = None
    x: float | None = None
    y: float | None = None
    to_x: float | None = None
    to_y: float | None = None
    risk: str = "LOW"
    cost: float = 0
    error: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    model: str | None = None


class ComputerUseClient(Protocol):
    async def execute(self, request: ComputerUseRequest) -> ComputerUseDecision: ...


class OpenRouterComputerUseClient:
    """Request one visual action from one fixed paid model and provider."""

    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        from web_testing_system.providers import (
            OPENROUTER_BASE_URL,
            provider_preferences,
        )

        if settings.openrouter_api_key is None or not settings.openrouter_api_key.get_secret_value():
            raise ValueError("OPENROUTER_API_KEY is required")
        if settings.computer_use_model is None:
            raise ValueError("COMPUTER_USE_MODEL is required")
        self.model = settings.computer_use_model
        self.provider = settings.computer_use_provider
        self.provider_options = provider_preferences(self.provider)
        self.client: Any = client or AsyncOpenAI(api_key=settings.openrouter_api_key.get_secret_value(), base_url=OPENROUTER_BASE_URL, max_retries=0, timeout=60)

    async def execute(self, request: ComputerUseRequest) -> ComputerUseDecision:
        prompt = f"URL: {request.url}\nGoal: {request.goal}\nReturn exactly one visual_action tool call. Use normalized coordinates 0..1000. Only allowed actions: {request.allowed_visual_actions}. Never navigate, type, submit, or request safety confirmation."
        schema = {"type": "object", "properties": {"action": {"type": "string", "enum": list(request.allowed_visual_actions)}, "x": {"type": "number"}, "y": {"type": "number"}, "to_x": {"type": "number"}, "to_y": {"type": "number"}}, "required": ["action", "x", "y"], "additionalProperties": False}
        response = await self.client.chat.completions.create(model=self.model, messages=[{"role": "user", "content": [{"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(request.screenshot).decode("ascii")}}]}], tools=[{"type": "function", "function": {"name": "visual_action", "description": "One allowed low-risk visual click or drag", "parameters": schema}}], tool_choice="auto", max_tokens=256, extra_body={"provider": self.provider_options})
        usage = response.usage
        tokens: dict[str, Any] = {"input_tokens": int(getattr(usage, "prompt_tokens", 0) or 0), "output_tokens": int(getattr(usage, "completion_tokens", 0) or 0), "model": response.model, "cost": float(getattr(usage, "cost", 0) or 0)}
        calls = response.choices[0].message.tool_calls or []
        if len(calls) != 1 or calls[0].function.name != "visual_action":
            return ComputerUseDecision(status="REFUSED", error="NO_SINGLE_VISUAL_ACTION", **tokens)
        arguments = json.loads(calls[0].function.arguments)
        if arguments.get("action") not in request.allowed_visual_actions:
            return ComputerUseDecision(status="REFUSED", error="MODEL_RETURNED_DISALLOWED_ACTION", **tokens)
        width, height = self._png_size(request.screenshot)
        return ComputerUseDecision(status="ACTION", action=arguments["action"], x=self._scale(arguments.get("x"), width), y=self._scale(arguments.get("y"), height), to_x=self._scale(arguments.get("to_x"), width), to_y=self._scale(arguments.get("to_y"), height), **tokens)

    @staticmethod
    def _png_size(screenshot: bytes) -> tuple[int, int]:
        if not screenshot.startswith(b"\x89PNG\r\n\x1a\n") or len(screenshot) < 24:
            raise ValueError("a PNG screenshot is required")
        return int.from_bytes(screenshot[16:20], "big"), int.from_bytes(screenshot[20:24], "big")

    @staticmethod
    def _scale(value: Any, size: int) -> float | None:
        return float(value) * size / 1000 if value is not None else None


@dataclass(frozen=True)
class ComputerUseResult:
    status: str
    reason: str | None
    action: str | None
    before_state: PageState | None
    after_state: PageState | None
    evidence_ids: tuple[str, ...]


class ComputerUseController:
    """Validate, execute, and resynchronize one visual fallback action."""

    def __init__(self, *, client: ComputerUseClient, settings: Settings, store: StateStore, evidence_store: EvidenceStore, page_state_reader: PageStateReader, permission_checker: PermissionChecker, budget: BudgetGuard, budget_id: str, run_id: str, task_id: str, tester_id: str, max_consecutive_failures: int = 2) -> None:
        if max_consecutive_failures <= 0:
            raise ValueError("max_consecutive_failures must be positive")
        self.client = client
        self.provider = settings.computer_use_provider
        self.model = settings.computer_use_model
        self.store = store
        self.evidence_store = evidence_store
        self.page_state_reader = page_state_reader
        self.permission_checker = permission_checker
        self.budget = budget
        self.budget_id = budget_id
        self.run_id = run_id
        self.task_id = task_id
        self.tester_id = tester_id
        self.max_consecutive_failures = max_consecutive_failures
        self.consecutive_failures = 0

    async def run(self, *, page: Page, browser_session_id: str, finding_id: str, goal: str, component_type: str, playwright_failure_reason: str, allowed_visual_actions: tuple[str, ...]) -> ComputerUseResult:
        component = component_type.casefold()
        if component in ORDINARY_COMPONENTS or component not in VISUAL_COMPONENTS:
            return ComputerUseResult("REFUSED", "COMPONENT_MUST_USE_PLAYWRIGHT", None, None, None, ())
        if not playwright_failure_reason.strip():
            return ComputerUseResult("REFUSED", "PLAYWRIGHT_FAILURE_REASON_REQUIRED", None, None, None, ())
        if not self.permission_checker.url_allowed(page.url):
            return ComputerUseResult("REFUSED", "SCOPE_VIOLATION", None, None, None, ())
        if not allowed_visual_actions or any(action not in SUPPORTED_VISUAL_ACTIONS for action in allowed_visual_actions):
            return ComputerUseResult("REFUSED", "VISUAL_ACTION_NOT_ALLOWED", None, None, None, ())
        action_type = ActionType.CLICK if "click" in allowed_visual_actions else ActionType.DRAG_AND_DROP
        permission = self.permission_checker.check(WebAction(action_type=action_type), current_url=page.url)
        if not permission.allowed:
            return ComputerUseResult("REFUSED", permission.code, None, None, None, ())
        if self.consecutive_failures >= self.max_consecutive_failures:
            self._record_stop(browser_session_id, page.url, "MAX_COMPUTER_USE_FAILURES_REACHED")
            return ComputerUseResult("STOPPED", "MAX_COMPUTER_USE_FAILURES_REACHED", None, None, None, ())
        try:
            self.budget.ensure_can_start("computer_use")
        except BudgetExceededError as error:
            self._record_stop(browser_session_id, page.url, error.reason)
            return ComputerUseResult("STOPPED", error.reason, None, None, None, ())
        attempt_id = f"computer-use-{uuid4().hex}"
        before_state = await self.page_state_reader.read(page)
        before_metadata, screenshot = await self.evidence_store.capture_screenshot(page=page, run_id=self.run_id, task_id=self.task_id, finding_id=finding_id, attempt_id=attempt_id, browser_session_id=browser_session_id, name="before")
        before_evidence_id = str(before_metadata["evidence_id"])
        remaining_budget = self.budget.limits.max_computer_use_calls - self.budget.usage.computer_use_calls
        request = ComputerUseRequest(screenshot=screenshot, goal=goal, url=page.url, allowed_visual_actions=allowed_visual_actions, remaining_budget=remaining_budget)
        self.store.append_event(event_id=f"event-{uuid4().hex}", run_id=self.run_id, task_id=self.task_id, tester_id=self.tester_id, browser_session_id=browser_session_id, event_type="COMPUTER_USE_REQUEST", url=page.url, tool="ComputerUseGateway", action="request_visual_action", result={"finding_id": finding_id, "goal": goal, "component_type": component, "playwright_failure_reason": playwright_failure_reason, "allowed_visual_actions": list(allowed_visual_actions), "remaining_budget": remaining_budget}, evidence_references=[before_evidence_id], latency_ms=0)
        run = self.store.get_run(self.run_id)
        if run is None:
            raise KeyError(f"unknown run: {self.run_id}")
        real_provider = isinstance(self.client, OpenRouterComputerUseClient)
        if run["remaining_budget"].get("max_computer_use_calls", self.budget.limits.max_computer_use_calls) <= 0 or (real_provider and run["remaining_budget"].get("max_llm_calls", self.budget.limits.max_llm_calls) <= 0):
            self._record_stop(browser_session_id, page.url, "MAX_PROVIDER_CALLS_REACHED")
            return ComputerUseResult("STOPPED", "MAX_PROVIDER_CALLS_REACHED", None, None, None, ())
        self.store.update_budget(budget_id=self.budget_id, computer_use_calls=1, llm_calls=int(real_provider))
        started = perf_counter()
        started_at = datetime.now(UTC).isoformat()
        try:
            with trace_span("VisualLLM", "llm", metadata={"agent_role": "visual", "phase": "visual", "finding_id": finding_id, "model": self.model, "provider": self.provider, "ls_model_name": self.model, "ls_provider": "openrouter"}) as span:
                decision = await self.client.execute(request)
                trace_result(span, input_tokens=decision.input_tokens, output_tokens=decision.output_tokens, cost=decision.cost, success=decision.status == "ACTION")
        except Exception as error:
            duration_seconds = perf_counter() - started
            self.budget.record_computer_use_call(runtime_seconds=duration_seconds, cost=0)
            if real_provider:
                self.budget.record_llm_call(input_tokens=0, output_tokens=0, runtime_seconds=0, cost=0)
            self.store.update_budget(budget_id=self.budget_id, runtime_seconds=duration_seconds)
            if real_provider:
                self._record_provider_event(latency_ms=duration_seconds * 1_000, success=False, input_tokens=0, output_tokens=0, model=self.model, error_type=type(error).__name__)
            self.consecutive_failures += 1
            return await self._finish(page=page, browser_session_id=browser_session_id, finding_id=finding_id, attempt_id=attempt_id, before_state=before_state, before_evidence_id=before_evidence_id, status="FAILED", reason=str(error), action=None, started_at=started_at, latency_ms=duration_seconds * 1_000, cost=0)
        duration_seconds = perf_counter() - started
        recorded_cost = max(decision.cost, 0)
        self.budget.record_computer_use_call(runtime_seconds=duration_seconds, cost=recorded_cost)
        if real_provider:
            self.budget.record_llm_call(input_tokens=decision.input_tokens, output_tokens=decision.output_tokens, runtime_seconds=0, cost=0)
        self.store.update_budget(budget_id=self.budget_id, input_tokens=decision.input_tokens if real_provider else 0, output_tokens=decision.output_tokens if real_provider else 0, runtime_seconds=duration_seconds, estimated_cost=recorded_cost)
        if real_provider:
            self._record_provider_event(latency_ms=duration_seconds * 1_000, success=True, input_tokens=decision.input_tokens, output_tokens=decision.output_tokens, model=decision.model, error_type=None)
        validation_error = self._validate_decision(decision, allowed_visual_actions)
        if validation_error is None:
            validation_error = await self._validate_coordinates(page, decision)
        if validation_error is not None:
            self.consecutive_failures += 1
            status = "REFUSED" if decision.status == "REFUSED" else "FAILED"
            return await self._finish(page=page, browser_session_id=browser_session_id, finding_id=finding_id, attempt_id=attempt_id, before_state=before_state, before_evidence_id=before_evidence_id, status=status, reason=validation_error, action=decision.action, started_at=started_at, latency_ms=duration_seconds * 1_000, cost=recorded_cost)
        try:
            await self._execute_action(page, decision)
        except Exception as error:
            self.consecutive_failures += 1
            return await self._finish(page=page, browser_session_id=browser_session_id, finding_id=finding_id, attempt_id=attempt_id, before_state=before_state, before_evidence_id=before_evidence_id, status="FAILED", reason=str(error), action=decision.action, started_at=started_at, latency_ms=duration_seconds * 1_000, cost=recorded_cost)
        self.consecutive_failures = 0
        return await self._finish(page=page, browser_session_id=browser_session_id, finding_id=finding_id, attempt_id=attempt_id, before_state=before_state, before_evidence_id=before_evidence_id, status="RETURN_TO_PLAYWRIGHT", reason=None, action=decision.action, started_at=started_at, latency_ms=duration_seconds * 1_000, cost=recorded_cost)

    def _record_provider_event(self, *, latency_ms: float, success: bool, input_tokens: int, output_tokens: int, model: str | None, error_type: str | None) -> None:
        self.store.append_event(event_id=f"event-{uuid4().hex}", run_id=self.run_id, task_id=self.task_id, tester_id=self.tester_id, event_type="LLM_CALL", tool="openrouter", action="computer_use_provider_request", result={"agent": "computer_use", "phase": "visual", "provider": "openrouter", "model": model, "success": success, "error_type": error_type, "input_tokens": input_tokens, "output_tokens": output_tokens, "usage_source": "provider" if success else "unavailable"}, latency_ms=latency_ms)

    def _validate_decision(self, decision: ComputerUseDecision, allowed_actions: tuple[str, ...]) -> str | None:
        if decision.status == "REFUSED":
            return decision.error or "GATEWAY_REFUSED"
        if decision.status != "ACTION" or decision.action not in allowed_actions:
            return "INVALID_GATEWAY_ACTION"
        if decision.cost < 0:
            return "INVALID_GATEWAY_COST"
        if decision.risk != "LOW":
            return "RISK_LIMIT_EXCEEDED"
        if decision.x is None or decision.y is None:
            return "VISUAL_COORDINATES_REQUIRED"
        if decision.action == "drag" and (decision.to_x is None or decision.to_y is None):
            return "DRAG_DESTINATION_REQUIRED"
        return None

    @staticmethod
    async def _validate_coordinates(page: Page, decision: ComputerUseDecision) -> str | None:
        size = await page.evaluate("() => ({ width: window.innerWidth, height: window.innerHeight })")
        points = [(decision.x, decision.y)]
        if decision.action == "drag":
            points.append((decision.to_x, decision.to_y))
        if any(x is None or y is None or x < 0 or y < 0 or x > size["width"] or y > size["height"] for x, y in points):
            return "VISUAL_COORDINATES_OUT_OF_BOUNDS"
        return None

    @staticmethod
    async def _execute_action(page: Page, decision: ComputerUseDecision) -> None:
        assert decision.x is not None and decision.y is not None
        if decision.action == "click":
            await page.mouse.click(decision.x, decision.y)
            return
        assert decision.to_x is not None and decision.to_y is not None
        await page.mouse.move(decision.x, decision.y)
        await page.mouse.down()
        await page.mouse.move(decision.to_x, decision.to_y)
        await page.mouse.up()

    async def _finish(self, *, page: Page, browser_session_id: str, finding_id: str, attempt_id: str, before_state: PageState, before_evidence_id: str, status: str, reason: str | None, action: str | None, started_at: str, latency_ms: float, cost: float) -> ComputerUseResult:
        after_state = await self.page_state_reader.read(page)
        after_metadata, _ = await self.evidence_store.capture_screenshot(page=page, run_id=self.run_id, task_id=self.task_id, finding_id=finding_id, attempt_id=attempt_id, browser_session_id=browser_session_id, name="after")
        evidence_ids = (before_evidence_id, str(after_metadata["evidence_id"]))
        ended_at = datetime.now(UTC).isoformat()
        result_data = {"finding_id": finding_id, "status": status, "reason": reason, "before_state_id": before_state.state_id, "after_state_id": after_state.state_id, "return_route": "PLAYWRIGHT" if status == "RETURN_TO_PLAYWRIGHT" else None}
        event = self.store.append_event(event_id=f"event-{uuid4().hex}", run_id=self.run_id, task_id=self.task_id, tester_id=self.tester_id, browser_session_id=browser_session_id, event_type="COMPUTER_USE_RESULT", url=page.url, tool="ComputerUseGateway", action=action or "no_action", result=result_data, evidence_references=evidence_ids, latency_ms=latency_ms, cost=cost)
        self.store.append_action_history(history_id=f"history-{uuid4().hex}", event_id=str(event["event_id"]), run_id=self.run_id, task_id=self.task_id, tester_id=self.tester_id, browser_session_id=browser_session_id, url=page.url, action=f"computer_use:{action or 'no_action'}", target="visual-coordinate" if action else None, tool="Computer Use", started_at=started_at, ended_at=ended_at, latency_ms=latency_ms, success=status == "RETURN_TO_PLAYWRIGHT", error=reason, result=result_data, action_data={"action_type": f"computer_use:{action or 'no_action'}"})
        return ComputerUseResult(status=status, reason=reason, action=action, before_state=before_state, after_state=after_state, evidence_ids=evidence_ids)

    def _record_stop(self, browser_session_id: str, url: str, reason: str) -> None:
        self.store.append_event(event_id=f"event-{uuid4().hex}", run_id=self.run_id, task_id=self.task_id, tester_id=self.tester_id, browser_session_id=browser_session_id, event_type="COMPUTER_USE_STOPPED", url=url, tool="ComputerUseGateway", action="stop_computer_use", result={"reason": reason, "budget": self.budget.snapshot()}, latency_ms=0)
