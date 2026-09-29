"""Controlled Gemini Computer Use fallback for one visual action."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter
from typing import Any, Protocol
from uuid import uuid4

from google import genai
from google.genai import types
from playwright.async_api import Page

from web_testing_system.config import Settings
from web_testing_system.evidence import EvidenceStore
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


class ComputerUseClient(Protocol):
    async def execute(self, request: ComputerUseRequest) -> ComputerUseDecision: ...


class GeminiComputerUseClient:
    """Request one Gemini browser action and convert it to the runtime boundary."""

    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        if settings.computer_use_provider != "gemini":
            raise ValueError("Computer Use provider must be Gemini")
        if settings.gemini_api_key is None or not settings.gemini_api_key.get_secret_value():
            raise ValueError("GEMINI_API_KEY is required")
        if settings.computer_use_model is None or not settings.computer_use_model.strip():
            raise ValueError("COMPUTER_USE_MODEL is required")
        self.model = settings.computer_use_model
        self.client = client or genai.Client(api_key=settings.gemini_api_key.get_secret_value())

    async def execute(self, request: ComputerUseRequest) -> ComputerUseDecision:
        excluded_actions = self._excluded_actions(request.allowed_visual_actions)
        prompt = (
            f"Current URL: {request.url}\nGoal: {request.goal}\n"
            f"Return exactly one low-risk browser action from: {', '.join(request.allowed_visual_actions)}. "
            "Do not navigate, type, submit data, or choose any other action."
        )
        response = await self.client.aio.models.generate_content(
            model=self.model,
            contents=[types.Content(role="user", parts=[types.Part(text=prompt), types.Part.from_bytes(data=request.screenshot, mime_type="image/png")])],
            config=types.GenerateContentConfig(
                tools=[types.Tool(computer_use=types.ComputerUse(environment=types.Environment.ENVIRONMENT_BROWSER, excluded_predefined_functions=excluded_actions, enable_prompt_injection_detection=True))]
            ),
        )
        function_call = self._first_function_call(response)
        if function_call is None:
            return ComputerUseDecision(status="REFUSED", error="NO_COMPUTER_USE_ACTION")
        arguments = dict(function_call.args or {})
        safety_error = self._safety_error(arguments)
        if safety_error is not None:
            return ComputerUseDecision(status="REFUSED", error=safety_error)
        width, height = self._png_size(request.screenshot)
        if function_call.name in {"click", "click_at"} and "click" in request.allowed_visual_actions:
            return ComputerUseDecision(status="ACTION", action="click", x=self._scale(arguments.get("x"), width), y=self._scale(arguments.get("y"), height))
        if function_call.name in {"drag", "drag_and_drop"} and "drag" in request.allowed_visual_actions:
            return ComputerUseDecision(
                status="ACTION",
                action="drag",
                x=self._scale(arguments.get("start_x", arguments.get("x")), width),
                y=self._scale(arguments.get("start_y", arguments.get("y")), height),
                to_x=self._scale(arguments.get("end_x", arguments.get("destination_x")), width),
                to_y=self._scale(arguments.get("end_y", arguments.get("destination_y")), height),
            )
        return ComputerUseDecision(status="REFUSED", error="MODEL_RETURNED_DISALLOWED_ACTION")

    @staticmethod
    def _first_function_call(response: Any) -> Any | None:
        for candidate in response.candidates or []:
            if candidate.content is None:
                continue
            for part in candidate.content.parts or []:
                if part.function_call is not None:
                    return part.function_call
        return None

    @staticmethod
    def _safety_error(arguments: dict[str, Any]) -> str | None:
        safety = arguments.get("safety_decision")
        if not isinstance(safety, dict):
            return None
        decision = str(safety.get("decision", "")).casefold()
        if decision in {"require_confirmation", "blocked", "deny", "denied"}:
            return f"COMPUTER_USE_SAFETY_{decision.upper()}"
        return None

    @staticmethod
    def _png_size(screenshot: bytes) -> tuple[int, int]:
        if len(screenshot) < 24 or screenshot[:8] != b"\x89PNG\r\n\x1a\n":
            raise ValueError("Computer Use screenshot must be a PNG")
        return int.from_bytes(screenshot[16:20], "big"), int.from_bytes(screenshot[20:24], "big")

    @staticmethod
    def _scale(value: Any, size: int) -> float | None:
        if not isinstance(value, int | float):
            return None
        return float(value) / 1_000 * size

    @staticmethod
    def _excluded_actions(allowed_actions: tuple[str, ...]) -> list[str]:
        actions = {"click", "double_click", "triple_click", "middle_click", "right_click", "move", "type", "navigate", "go_back", "go_forward", "wait", "press_key", "key_down", "key_up", "hotkey", "take_screenshot", "scroll", "drag_and_drop"}
        retained = {"click" if action == "click" else "drag_and_drop" for action in allowed_actions}
        return sorted(actions - retained)


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
        if settings.computer_use_provider != "gemini":
            raise ValueError("Computer Use provider must be Gemini")
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
        started = perf_counter()
        started_at = datetime.now(UTC).isoformat()
        try:
            decision = await self.client.execute(request)
        except Exception as error:
            duration_seconds = perf_counter() - started
            self.budget.record_computer_use_call(runtime_seconds=duration_seconds, cost=0)
            self.store.update_budget(budget_id=self.budget_id, computer_use_calls=1, runtime_seconds=duration_seconds)
            self.consecutive_failures += 1
            return await self._finish(page=page, browser_session_id=browser_session_id, finding_id=finding_id, attempt_id=attempt_id, before_state=before_state, before_evidence_id=before_evidence_id, status="FAILED", reason=str(error), action=None, started_at=started_at, latency_ms=duration_seconds * 1_000, cost=0)
        duration_seconds = perf_counter() - started
        recorded_cost = max(decision.cost, 0)
        self.budget.record_computer_use_call(runtime_seconds=duration_seconds, cost=recorded_cost)
        self.store.update_budget(budget_id=self.budget_id, computer_use_calls=1, runtime_seconds=duration_seconds, estimated_cost=recorded_cost)
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
