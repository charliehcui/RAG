"""The single Tester Agent definition and its constrained tools."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from time import perf_counter
from typing import Any, Literal
from uuid import uuid4

from agent_framework import (
    Agent,
    AgentResponse,
    AgentSession,
    FunctionInvocationContext,
    FunctionMiddleware,
    MiddlewareTermination,
)
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from pydantic import TypeAdapter

from web_testing_system.observability import TraceChatMiddleware, TraceToolMiddleware
from web_testing_system.runtime.budget import BudgetExceededError, BudgetGuard
from web_testing_system.runtime.models import (
    ActionCandidate,
    ActionType,
    PageState,
    WebAction,
)
from web_testing_system.runtime.web_runtime import WebTestingRuntime
from web_testing_system.state import StateStore


@dataclass(frozen=True)
class TesterAssignment:
    run_id: str
    task_id: str
    tester_id: str
    identity_id: str
    role: str
    data_namespace: str
    scope: tuple[str, ...]
    step_budget: int


@dataclass
class PageStep:
    """Use control/context for semantic actions and table column checks; target is an observed locator. wait_ms is an integer delay, not a selector."""
    action_type: ActionType
    control: str | None = None
    context: str = ""
    target: str | None = None
    value_reference: str | None = None
    expected: str | None = None
    expected_reference: str | None = None
    assertion: Literal["contains", "not_contains", "equals", "visible", "hidden", "count"] = "contains"
    behavior_id: str | None = None
    url: str | None = None
    key: str | None = None
    wait_ms: int = 0


class TesterAgentTools:
    def __init__(
        self,
        *,
        assignment: TesterAssignment,
        runtime: WebTestingRuntime,
        store: StateStore,
        shared_state: bool = True,
        decision_policy: str = "JEV",
        action_policy: str = "PLAYWRIGHT",
        task_context: dict[str, Any] | None = None,
    ) -> None:
        self.assignment = assignment
        self.runtime = runtime
        self.store = store
        self.shared_state = shared_state
        self.decision_policy = decision_policy
        self.action_policy = action_policy
        self.pending_candidates: dict[str, ActionCandidate] = {}
        self.pending_page_state: PageState | None = None
        self.pending_goal: str | None = None
        self.task_context = task_context or {}
        if task_context is not None:
            self.task_context["test_inputs"] = {reference: value for reference, value in runtime.input_values.items() if not reference.startswith("env:")}

    async def execute_page_steps(self, steps: list[PageStep], related_task_ids: list[str] | None = None, finish: bool = False) -> dict[str, Any]:
        """Execute ordered semantic steps; resolve controls with Playwright/Jev. Inputs require references. Assertion failures are recorded immediately and execution continues; other failures stop the batch. finish=True checks and finishes the Task."""
        steps = TypeAdapter(list[PageStep]).validate_python(steps)
        results: list[dict[str, Any]] = []
        for index, step in enumerate(steps):
            if step.behavior_id is not None and step.action_type not in {ActionType.ASSERTION, ActionType.URL_CHECK}:
                return {"success": False, "failed_step": index, "error_type": "BEHAVIOR_REQUIRES_ASSERTION", "results": results}
            if step.expected_reference is not None:
                if step.expected_reference.startswith("env:") or step.expected_reference not in self.runtime.input_values:
                    return {"success": False, "failed_step": index, "error_type": "ASSERTION_VALUE_UNAVAILABLE", "results": results}
                step.expected = self.runtime.input_values[step.expected_reference]
            if step.action_type == ActionType.ASSERTION and step.assertion in {"visible", "hidden"} and step.expected is not None:
                return {"success": False, "failed_step": index, "error_type": "VISIBILITY_DOES_NOT_COMPARE_TEXT", "instruction": "Use contains/equals for expected text; visible/hidden checks only the target locator.", "results": results}
            if step.action_type == ActionType.ASSERTION and step.assertion == "count" and step.control is not None:
                return {"success": False, "failed_step": index, "error_type": "COUNT_REQUIRES_ROW_SELECTOR", "instruction": "Use a target matching all relevant rows, not one selected cell.", "results": results}
            if step.action_type in {ActionType.INPUT, ActionType.SELECT} and step.value_reference is None:
                return {"success": False, "failed_step": index, "error_type": "INPUT_REFERENCE_REQUIRED", "results": results}
            if step.control is not None:
                if step.action_type not in {ActionType.INPUT, ActionType.SELECT, ActionType.CLICK, ActionType.REPEAT_SUBMIT, ActionType.ASSERTION}:
                    return {"success": False, "failed_step": index, "error_type": "CONTROL_ACTION_UNSUPPORTED", "results": results}
                if step.value_reference is not None and step.value_reference not in self.runtime.input_values:
                    return {"success": False, "failed_step": index, "error_type": "INPUT_VALUE_UNAVAILABLE", "results": results}
                if step.behavior_id is not None and step.behavior_id not in self.runtime.expected_behavior_ids:
                    return {"success": False, "failed_step": index, "error_type": "UNKNOWN_EXPECTED_BEHAVIOR", "results": results}
                action = WebAction(action_type=step.action_type, value_reference=step.value_reference, value=self.runtime.input_values.get(step.value_reference or ""), url=step.url, expected=step.expected, assertion=step.assertion, behavior_id=step.behavior_id, goal_check=step.behavior_id is not None)
                decision = await self.runtime.execute_page_action(action, control=step.control, context=step.context)
                result = decision.action_result.to_dict() if decision.action_result is not None else {"success": False, "error_type": decision.reason}
                result["source"] = decision.source
                result["resolved_target"] = decision.candidate.target if decision.candidate is not None else None
            else:
                result = await self.execute_known_action(step.action_type, target=step.target, value_reference=step.value_reference, url=step.url, expected=step.expected, assertion=step.assertion, behavior_id=step.behavior_id, goal_check=step.behavior_id is not None, key=step.key, wait_ms=step.wait_ms)
                result["source"] = "PLAYWRIGHT"
                result["resolved_target"] = step.target
            self.store.append_event(event_id=f"event-{uuid4().hex}", run_id=self.assignment.run_id, task_id=self.assignment.task_id, tester_id=self.assignment.tester_id, event_type="PAGE_STEP", tool="TesterPageSteps", action=step.action_type.value, result={"source": result["source"], "semantic_control": step.control is not None, "success": result["success"], "behavior_id": step.behavior_id, "browser_event_id": result.get("event_id")}, latency_ms=0)
            compact = {"step": index, "action": step.action_type.value, "success": result["success"], "source": result["source"]}
            if step.behavior_id:
                compact["behavior_id"] = step.behavior_id
            if not result["success"]:
                compact["error_type"] = result.get("error_type")
                compact["actual"] = result.get("data", {}).get("actual")
                if result.get("error_type") == "ASSERTION_FAILURE" and step.behavior_id:
                    session_id = self.runtime.browser_session_id
                    assert session_id is not None
                    page = self.runtime.browser_manager.get_session(session_id).page
                    expected_result = f"{result.get('resolved_target') or 'body'} {step.assertion} {step.expected or ''}"
                    finding = next((finding for finding in self.store.list_recent_findings(self.assignment.run_id, limit=1000) if finding["task_id"] == self.assignment.task_id and finding["action"] == step.behavior_id and finding["expected_result"] == expected_result), None)
                    if finding is None:
                        finding = await self.record_finding(title=f"{step.behavior_id}: {step.assertion} check failed", status="ANOMALY", expected_result=expected_result, actual_result=str(compact["actual"]), affected_page=page.url, action=step.behavior_id, behavior_id=step.behavior_id, related_task_ids=related_task_ids)
                    compact["finding_id"] = finding["finding_id"]
                elif result.get("error_type") != "ASSERTION_FAILURE":
                    results.append(compact)
                    return {"success": False, "failed_step": index, "results": results, "page": await self._page_summary()}
            results.append(compact)
        if finish:
            return {"success": True, "results": results, "outcome": await self.finish_task()}
        return {"success": True, "results": results, "page": await self._page_summary()}

    async def _page_summary(self) -> dict[str, Any]:
        if self.runtime.browser_session_id is None:
            return {}
        page = self.runtime.browser_manager.get_session(self.runtime.browser_session_id).page
        state = await self.runtime.page_state_reader.read(page)
        return {**state.selection_summary(), "text": state.visible_dom[:3500], "rows": await self.runtime.page_state_reader.read_rows(page)}

    async def execute_known_action(
        self,
        action_type: ActionType,
        target: str | None = None,
        value: str | None = None,
        value_reference: str | None = None,
        url: str | None = None,
        expected: str | None = None,
        assertion: Literal["contains", "not_contains", "equals", "visible", "hidden", "count"] = "contains",
        key: str | None = None,
        wait_ms: int = 0,
        resource_id: str | None = None,
        requires_resource: bool = False,
        confirmed: bool = False,
        behavior_id: str | None = None,
        goal_check: bool = False,
    ) -> dict[str, Any]:
        """Execute one known action through the permission-checked Web Testing Runtime."""
        try:
            parsed_action = ActionType(action_type)
        except ValueError:
            return {
                "success": False,
                "error_type": "INVALID_ACTION",
                "error": f"unsupported action: {action_type}",
            }
        if target is None and (parsed_action == ActionType.DOM_INSPECTION or parsed_action == ActionType.ASSERTION and assertion in {"contains", "not_contains", "equals"}):
            target = "body"
        if behavior_id is not None and behavior_id not in self.runtime.expected_behavior_ids:
            return {"success": False, "error_type": "UNKNOWN_EXPECTED_BEHAVIOR"}
        if parsed_action == ActionType.WAIT:
            if target is not None and target.isdecimal() and wait_ms == 0:
                wait_ms, target = int(target), None
            if wait_ms < 0 or wait_ms > 2_000:
                return {"success": False, "error_type": "INVALID_WAIT_DURATION", "instruction": "Use wait_ms up to 2000 for a short page wait; use wait_for_shared_progress for collaboration."}
        if value_reference is not None:
            if value_reference not in self.runtime.input_values:
                return {"success": False, "error_type": "INPUT_VALUE_UNAVAILABLE", "reference": value_reference}
            value = self.runtime.input_values[value_reference]
        elif parsed_action in {ActionType.INPUT, ActionType.SELECT} and value is not None:
            references = [reference for reference, configured in self.runtime.input_values.items() if configured == value]
            if len(references) != 1:
                return {"success": False, "error_type": "INPUT_REFERENCE_REQUIRED"}
            value_reference = references[0]
        action = WebAction(
            action_type=parsed_action,
            target=target,
            value=value,
            value_reference=value_reference,
            url=url,
            expected=expected,
            assertion=assertion,
            key=key,
            wait_ms=wait_ms,
            resource_id=resource_id,
            requires_resource=requires_resource,
            confirmed=confirmed,
            behavior_id=behavior_id,
            goal_check=goal_check,
        )
        return (await self.runtime.execute_known_action(action)).to_dict()

    async def explore_unknown_path(self, current_goal: str) -> dict[str, Any]:
        """Use Candidate Builder and Jev for an unknown legal path."""
        if self.decision_policy == "TESTER_LLM_EVERY_DECISION":
            if self.runtime.browser_session_id is None:
                raise RuntimeError("browser session has not started")
            session = self.runtime.browser_manager.get_session(self.runtime.browser_session_id)
            page_state = await self.runtime.page_state_reader.read(session.page)
            candidates = self.runtime.candidate_builder.build(goal=current_goal, page_state=page_state, explored_targets=set())
            self.pending_candidates = {candidate.candidate_id: candidate for candidate in candidates}
            self.pending_page_state = page_state
            self.pending_goal = current_goal
            return {"source": "TESTER_LLM", "candidates": [{"candidate_id": candidate.candidate_id, "action": candidate.action, "label": candidate.label} for candidate in candidates], "instruction": "Choose one Candidate ID with select_candidate. The Runtime will revalidate it."}
        return asdict(await self.runtime.explore_unknown_path(current_goal))

    async def select_candidate(self, candidate_id: str) -> dict[str, Any]:
        """Execute one LLM-selected Candidate after Runtime revalidation."""
        async with self.runtime.page_lock:
            if self.runtime.task_finished:
                return {"success": False, "error_type": "TASK_ALREADY_FINISHED"}
            return await self._select_candidate(candidate_id)

    async def _select_candidate(self, candidate_id: str) -> dict[str, Any]:
        if self.decision_policy != "TESTER_LLM_EVERY_DECISION" or self.pending_page_state is None or self.runtime.browser_session_id is None:
            return {"success": False, "error_type": "CANDIDATE_NOT_AVAILABLE"}
        candidate = self.pending_candidates.get(candidate_id)
        if candidate is None:
            return {"success": False, "error_type": "ILLEGAL_CANDIDATE_ID"}
        session = self.runtime.browser_manager.get_session(self.runtime.browser_session_id)
        current_page_state = await self.runtime.page_state_reader.read(session.page)
        validation = self.runtime.candidate_builder.validate(candidate=candidate, current_page_state=current_page_state)
        self.pending_candidates = {}
        if not validation.valid:
            return {"success": False, "error_type": validation.reason}
        if candidate.action == "request_replan":
            await self.runtime.request_replan("TESTER_LLM_REQUESTED_REPLAN")
            return {"success": True, "action": "request_replan"}
        if candidate.action == "stop_current_path":
            return {"success": True, "action": "stop_current_path"}
        assert validation.action is not None
        result = await self.runtime._execute_known_action(validation.action)
        if result.success:
            self.runtime.store.record_path(run_id=self.assignment.run_id, feature=self.pending_goal or "", page=self.pending_page_state.url, page_state_id=self.pending_page_state.state_id, action=candidate.action, result="SUCCESS", last_tester=self.assignment.tester_id)
        return result.to_dict()

    def read_coverage(self) -> list[dict[str, Any]]:
        """Read coverage already recorded for the current run."""
        paths = self.store.list_paths(self.assignment.run_id)
        return paths if self.shared_state else [path for path in paths if path["last_tester"] == self.assignment.tester_id]

    def read_shared_facts(self) -> dict[str, Any]:
        """Read structured collaboration facts without another Tester's conversation."""
        progress = self.store.list_events(self.assignment.run_id, event_types=("TASK_PROGRESS",), limit=50)
        return {
            "current_task": self.store.get_task(self.assignment.task_id),
            "coverage": self.read_coverage(),
            "recent_findings": [finding for finding in self.store.list_recent_findings(self.assignment.run_id) if self.shared_state or finding["first_seen_by"] == self.assignment.tester_id],
            "tester_progress": [event for event in progress if self.shared_state or event["tester_id"] == self.assignment.tester_id],
            "tasks": [{key: task[key] for key in ("task_id", "goal", "status", "dependencies", "assigned_tester")} for task in self.store.list_tasks(self.assignment.run_id) if self.shared_state or task["task_id"] == self.assignment.task_id],
        }

    async def wait_for_shared_progress(self, summary_contains: str, task_id: str | None = None, timeout_seconds: int = 30) -> dict[str, Any]:
        """Wait for a structured progress signal without model polling. Stop a live-session deadlock when the required participant depends on this Task."""
        if not self.shared_state:
            return {"ready": False, "reason": "SHARED_STATE_DISABLED"}
        started = perf_counter()
        timeout_seconds = min(max(timeout_seconds, 0), 60)
        while True:
            for event in self.store.list_events(self.assignment.run_id, event_types=("TASK_PROGRESS",), limit=100):
                if event["task_id"] != self.assignment.task_id and (task_id is None or event["task_id"] == task_id) and summary_contains in str(event["result"].get("summary", "")):
                    return {"ready": True, "task_id": event["task_id"], "event_id": event["event_id"]}
            participants = [task for task in self.store.list_tasks(self.assignment.run_id) if task["task_id"] != self.assignment.task_id and (task_id is None or task["task_id"] == task_id)]
            if participants and all(task["status"] == "PENDING" and self.assignment.task_id in task["dependencies"] for task in participants):
                await self.runtime.request_replan("LIVE_PARTICIPANT_DEPENDS_ON_OBSERVER")
                await self.runtime.stop_task("LIVE_PARTICIPANT_DEPENDS_ON_OBSERVER")
                return {"ready": False, "reason": "LIVE_PARTICIPANT_DEPENDS_ON_OBSERVER"}
            if perf_counter() - started >= timeout_seconds:
                return {"ready": False, "reason": "PROGRESS_SIGNAL_NOT_AVAILABLE", "tasks": participants}
            await asyncio.sleep(0.2)

    def check_path_before_exploring(
        self,
        feature: str,
        page: str,
        page_state_id: str,
        action: str,
        new_reason: str | None = None,
    ) -> dict[str, Any]:
        """Check exact shared Coverage before starting a potentially duplicate path."""
        existing = self.store.get_path(
            run_id=self.assignment.run_id,
            feature=feature,
            page=page,
            page_state_id=page_state_id,
            action=action,
        )
        if existing is not None and not self.shared_state and existing["last_tester"] != self.assignment.tester_id:
            existing = None
        should_explore = existing is None or bool(new_reason and new_reason.strip())
        if not should_explore:
            assert existing is not None
            self.store.append_event(
                event_id=f"event-{uuid4().hex}",
                run_id=self.assignment.run_id,
                task_id=self.assignment.task_id,
                tester_id=self.assignment.tester_id,
                browser_session_id=self.runtime.browser_session_id,
                event_type="DUPLICATE_EXPLORATION",
                tool="TesterAgent",
                action="skip_covered_path",
                result={
                    "feature": feature,
                    "page": page,
                    "page_state_id": page_state_id,
                    "action": action,
                    "existing_path_id": existing["path_id"],
                },
                latency_ms=0,
            )
        return {
            "should_explore": should_explore,
            "existing_coverage": existing,
            "reason": "NEW_REASON" if should_explore and existing is not None else "NOT_COVERED" if existing is None else "ALREADY_COVERED",
        }

    async def record_finding(
        self,
        title: str,
        status: Literal["OBSERVATION", "ANOMALY", "SUSPECTED_ISSUE"],
        expected_result: str,
        actual_result: str,
        severity_hint: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"] | None = None,
        affected_page: str | None = None,
        action: str | None = None,
        error_text: str | None = None,
        reproduction_steps: list[dict[str, Any]] | None = None,
        behavior_id: str | None = None,
        related_task_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Record an early Finding without declaring a confirmed bug."""
        if status not in {"OBSERVATION", "ANOMALY", "SUSPECTED_ISSUE"}:
            raise ValueError("Tester may only create an early Finding status")
        if behavior_id is not None and behavior_id not in self.runtime.expected_behavior_ids:
            raise ValueError("Finding must reference an assigned expected behavior")
        related = sorted(set(related_task_ids or []))
        for task_id in related:
            task = self.store.get_task(task_id)
            if task is None or task["run_id"] != self.assignment.run_id:
                raise ValueError("Related replay tasks must belong to this Run")
        if severity_hint is not None and severity_hint not in {
            "LOW",
            "MEDIUM",
            "HIGH",
            "CRITICAL",
        }:
            raise ValueError("unsupported severity hint")
        history = self.store.list_action_history(run_id=self.assignment.run_id, task_id=self.assignment.task_id)
        boundary = history[-1] if history else None
        if status in {"ANOMALY", "SUSPECTED_ISSUE"} and boundary is not None and not boundary["success"] and boundary["action_data"].get("action_type") in {"assertion", "url_check"}:
            assigned_behavior = behavior_id or boundary["action_data"].get("behavior_id")
            for event in self.store.list_events(self.assignment.run_id, event_types=("FINDING_CREATED",), task_id=self.assignment.task_id):
                if event["result"].get("boundary_event_id") == boundary["event_id"] and event["result"].get("behavior_id") == assigned_behavior:
                    existing = self.store.get_finding(str(event["result"]["finding_id"]))
                    if existing is not None and existing["status"] in {"ANOMALY", "SUSPECTED_ISSUE"}:
                        return existing
        finding = self.store.create_finding(
            finding_id=f"finding-{uuid4().hex}",
            run_id=self.assignment.run_id,
            task_id=self.assignment.task_id,
            title=title,
            status=status,
            expected_result=expected_result,
            actual_result=actual_result,
            first_seen_by=self.assignment.tester_id,
            severity_hint=severity_hint,
            needs_confirmation=True,
            affected_role=self.assignment.role,
            affected_page=affected_page,
            action=action,
            error_text=error_text,
            reproduction_steps=reproduction_steps or [],
        )
        self.store.append_event(
            event_id=f"event-{uuid4().hex}",
            run_id=self.assignment.run_id,
            task_id=self.assignment.task_id,
            tester_id=self.assignment.tester_id,
            browser_session_id=self.runtime.browser_session_id,
            event_type="FINDING_CREATED",
            url=affected_page,
            tool="TesterAgent",
            action="record_finding",
            result={
                "finding_id": finding["finding_id"],
                "status": finding["status"],
                "severity_hint": finding["severity_hint"],
                "action": finding["action"],
                "boundary_event_id": history[-1]["event_id"] if history else None,
                "behavior_id": behavior_id,
                "related_task_ids": related,
            },
            latency_ms=0,
        )
        await self.runtime.capture_finding_evidence(str(finding["finding_id"]))
        return finding

    async def record_observation(
        self,
        title: str,
        expected_result: str,
        actual_result: str,
        affected_page: str | None = None,
        action: str | None = None,
        error_text: str | None = None,
    ) -> dict[str, Any]:
        """Record an observation without declaring a confirmed bug."""
        return await self.record_finding(
            title=title,
            status="OBSERVATION",
            expected_result=expected_result,
            actual_result=actual_result,
            affected_page=affected_page,
            action=action,
            error_text=error_text,
        )

    async def update_task_progress(
        self, progressed: bool, summary: str
    ) -> dict[str, object]:
        """Record local Task progress and trigger replan after repeated no-progress steps."""
        return await self.runtime.note_progress(progressed=progressed, summary=summary)

    async def request_replan(self, reason: str) -> dict[str, object]:
        """Request Main Agent replanning without changing global scope."""
        return await self.runtime.request_replan(reason)

    async def use_computer_fallback(self, finding_id: str, goal: str, component_type: str, playwright_failure_reason: str, allowed_visual_actions: list[str]) -> dict[str, Any]:
        """Request one controlled visual action after a recorded Playwright limitation."""
        result = await self.runtime.use_computer_fallback(finding_id=finding_id, goal=goal, component_type=component_type, playwright_failure_reason=playwright_failure_reason, allowed_visual_actions=tuple(allowed_visual_actions))
        return {"status": result.status, "reason": result.reason, "action": result.action, "before_state_id": result.before_state.state_id if result.before_state else None, "after_state_id": result.after_state.state_id if result.after_state else None, "evidence_ids": list(result.evidence_ids)}

    async def finish_task(self) -> dict[str, object]:
        """Finish only after explicit assertions cover assigned behaviors and failed checks have Findings; otherwise return missing checks and keep the Task open."""
        outcome = await self.runtime.record_task_outcome()
        assertions = outcome["assertions"]
        assert isinstance(assertions, list)
        covered = {item.get("behavior_id") for item in assertions}
        missing = sorted(set(self.runtime.expected_behavior_ids) - covered)
        if missing or outcome["reason"] == "APPLICATION_DEVIATION_UNREPORTED":
            return {**outcome, "finished": False, "missing_behavior_ids": missing, "instruction": "Record actual assertion checks with the supplied behavior_id values. DOM Inspection alone is not a goal check. Record Findings for failed application assertions, then call finish_task."}
        return {**await self.runtime.record_task_outcome(finish=True), "finished": True}

    def read_test_data(self, reference: str) -> dict[str, Any]:
        """Read an allowed non-secret input. Secret references can only be filled by Runtime."""
        value = self.runtime.input_values.get(reference)
        if reference.startswith("env:"):
            return {"reference": reference, "available": value is not None, "instruction": "Pass this reference to execute_known_action; secret values are never returned."}
        return {"reference": reference, "value": value, "available": value is not None}


class FinishTaskMiddleware(FunctionMiddleware):
    def __init__(self, runtime: WebTestingRuntime) -> None:
        self.runtime = runtime

    async def process(self, context: FunctionInvocationContext, call_next: Callable[[], Awaitable[None]]) -> None:
        await call_next()
        if self.runtime.task_finished:
            raise MiddlewareTermination("Task outcome recorded; no final Done model round is needed.", result=context.result)


def create_tester_agent(
    *, client: Any, assignment: TesterAssignment, tools: TesterAgentTools
) -> Agent:
    client.function_invocation_configuration["allow_concurrent_invocation"] = False
    instructions = (
        "You are the only Tester Agent type. Work only on the assigned Task, scope, identity, namespace, and step budget. "
        "Work in short subgoals. For known targets, issue the ordered execute_known_action calls for one safe subgoal in the same response; the Runtime serializes page actions. "
        "For example, fill the known username and password fields, submit, then inspect in one response. Stop the batch before an unknown target or action. "
        "Inspect at page or subgoal boundaries, not after each successful fill or click. Use explore_unknown_path instead of inventing unknown actions. "
        "Check shared Coverage before starting a path and collaborate only through structured Shared State facts. "
        "Never change global scope, bypass permissions, create another Agent, or declare a confirmed bug. "
        "Record uncertain behavior only with record_observation. Request replan when the local path cannot continue."
        " Mark every actual goal assertion with goal_check=true and its assigned behavior_id; keep setup checks untagged. Use a locator visibility check for inputs/buttons, not a body-text check for placeholders or aria-labels. "
        "For INPUT and SELECT from configured data, always pass value_reference, including blank values and usernames; do not copy the returned value into the value argument. Account secrets use their supplied references directly. "
        "Read nonsecret assertion values once and reuse them. Never invent unavailable inputs. "
        "Use repeat_submit with the same-origin request URL and submit-button target to check one pending repeated form operation. "
        "Immediately record a failed goal check before another browser action, with the same behavior_id. Include related_task_ids for another Task's necessary setup or membership change. Read shared facts for those task references. "
        "If the same completion error or wait repeats without progress, report no progress and request replan rather than repeating identical Findings or waiting indefinitely. "
        "Call finish_task after testing the goal; Python determines success. Do not produce a final Done summary."
    )
    if tools.decision_policy == "JEV" and tools.action_policy == "PLAYWRIGHT":
        instructions = (
            "You are a Tester. Decide what to test and what the expected application behavior means. Work only on your assigned Task and identity. "
            "Read the supplied Initial Page and preparation status. Python has already navigated, inspected, and prepared an existing account login when possible. Do not repeat successful setup. "
            "Prefer ONE execute_page_steps call for the whole workflow, including operations, checks, refresh and finish=True. Inspect again only if a batch cannot resolve a control. "
            "PageStep control is a human control label, and context is text from its surrounding form or row. Python resolves unique exact controls; Jev resolves ambiguity. You do not need numeric positions or CSS for future controls. "
            "For example: input control='Login username' value_reference=<username key>, input control='Login password' value_reference=<secret_reference>, click control='Login'. "
            "For Edit/Delete/Remove use context=<the exact entity row text/name or observed ID> to distinguish repeated buttons. In a dialog use control='Edit value' and control='Save'; background controls are unavailable. "
            "For INPUT/SELECT always use value_reference. All nonsecret test_inputs are already in Task Context. SELECT accepts a configured option label. "
            "Assertions use actual observed CSS selectors, a literal expected string or expected_reference, and the assigned behavior_id. Default text target is body. "
            "visible/hidden checks test a locator, not the expected argument. To check a name use contains/equals/not_contains with its actual text. Never put English instructions in expected. "
            "Registration is checked by its success message and a working login; display names need not be in the login header. "
            "Assert the immediate result and persistence where required. For a saved table field use action_type='assertion', control=<actual column heading, e.g. Name>, context=<entity name/observed ID>, assertion='equals', expected_reference=<saved value key>, behavior_id=<assigned ID>. Python binds the actual cell. "
            "Check assigned entities; parallel Tasks can create unrelated rows. For count use a target matching every relevant row, not a control selecting one cell. Compare existing entity IDs/names rather than unrelated global row counts. Use wait_ms for a short wait, never a numeric target. "
            "Use repeat_submit with the observed same-origin submission URL to establish two requests for the SAME pending form submission; two ordinary clicks after completion are a different test. "
            "Failed goal assertions create an early Finding and evidence immediately; finish all remaining applicable checks. Python alone determines Task success and deterministic replay verifies bugs. "
            "Do not duplicate auto-created Findings, claim confirmed bugs, repeat a failed check without a new reason, or spend model turns on each fill/click. "
            "Read shared facts only for necessary setup/collaboration. Include related_task_ids in execute_page_steps for prerequisites outside your Task. "
            "Publish live-session readiness with update_task_progress; use wait_for_shared_progress for another participant's signal instead of model polling. "
            "Never change scope, bypass permissions, or create Agents. Use record_observation for uncertainty. Call finish_task if the last batch did not finish."
        )
    if tools.action_policy == "TESTER_LLM_EVERY_STEP":
        instructions += " For this evaluation route, decide one browser action per model response. Call at most one browser action tool before reasoning again."
    if tools.decision_policy == "TESTER_LLM_EVERY_DECISION":
        instructions += " For unknown paths, inspect explore_unknown_path candidates and select exactly one ID with select_candidate."
    exposed_tools = [tools.execute_known_action, tools.explore_unknown_path, tools.read_shared_facts, tools.record_finding, tools.record_observation, tools.update_task_progress, tools.request_replan, tools.finish_task, tools.read_test_data]
    if tools.decision_policy == "JEV" and tools.action_policy == "PLAYWRIGHT":
        exposed_tools.insert(0, tools.execute_page_steps)
        exposed_tools.append(tools.wait_for_shared_progress)
    else:
        exposed_tools.extend([tools.read_coverage, tools.check_path_before_exploring])
    if tools.decision_policy == "TESTER_LLM_EVERY_DECISION":
        exposed_tools.append(tools.select_candidate)
    if getattr(tools.runtime, "computer_use_controller", None) is not None:
        exposed_tools.append(tools.use_computer_fallback)
    return Agent(
        client=client,
        name="tester-agent",
        description="Executes one assigned web testing task through the controlled runtime.",
        instructions=instructions,
        middleware=[TraceChatMiddleware(), TraceToolMiddleware(), FinishTaskMiddleware(tools.runtime)],
        tools=exposed_tools,
        additional_properties={"assignment": asdict(assignment), "task_context": tools.task_context},
    )


class TesterRunner:
    def __init__(
        self,
        *,
        agent: Agent,
        assignment: TesterAssignment,
        runtime: WebTestingRuntime,
        store: StateStore,
        budget: BudgetGuard,
        budget_id: str,
        provider_usage_recorded: bool = False,
        decision_policy: str = "JEV",
        action_policy: str = "PLAYWRIGHT",
        prepare_task_page: bool = False,
    ) -> None:
        self.agent = agent
        self.assignment = assignment
        self.runtime = runtime
        self.store = store
        self.budget = budget
        self.budget_id = budget_id
        self.provider_usage_recorded = provider_usage_recorded
        self.prepare_task_page = prepare_task_page
        self.decision_policy = decision_policy
        self.action_policy = action_policy
        self.session = AgentSession(session_id=f"tester-session-{uuid4().hex}")

    async def execute_known(self, action: WebAction) -> dict[str, Any]:
        """Known Replay and assertions bypass the LLM and go directly to Playwright."""
        if self.action_policy == "TESTER_LLM_EVERY_STEP":
            answer = await self.ask_tester_llm(f"Decide whether the next scoped known action is safe: {action.action_type.value}. Reply APPROVE or STOP without calling tools.")
            if answer.startswith("STOPPED:"):
                return {"success": False, "error_type": "BUDGET_STOP", "error": answer}
            if not answer.strip().upper().startswith("APPROVE"):
                return {"success": False, "error_type": "MODEL_STEP_STOP", "error": answer}
        return (await self.runtime.execute_known_action(action)).to_dict()

    async def explore(self, current_goal: str) -> dict[str, Any]:
        if self.decision_policy == "TESTER_LLM_EVERY_DECISION":
            return {"tester_llm": await self.ask_tester_llm(f"Choose the next legal action for: {current_goal}. Use only the provided runtime tools and permissions.")}
        decision = await self.runtime.explore_unknown_path(current_goal)
        result: dict[str, Any] = {"decision": asdict(decision)}
        if decision.needs_tester_llm:
            result["tester_llm"] = await self.ask_tester_llm(
                f"The constrained selector could not continue. Goal: {current_goal}. Reason: {decision.reason}. Use only the provided tools or request replan."
            )
        return result

    async def ask_tester_llm(self, prompt: str) -> str:
        try:
            self.budget.ensure_can_start("llm")
        except BudgetExceededError as error:
            await self.runtime.stop_task(error.reason)
            return f"STOPPED: {error.reason}"
        if self.prepare_task_page and self.decision_policy == "JEV" and self.action_policy == "PLAYWRIGHT" and prompt.startswith("Execute assigned Task "):
            prompt = await self._prepare_task_prompt(prompt)
            if self.runtime.task_finished:
                return "STOPPED: TASK_PREPARATION_STOPPED"
        started_at = perf_counter()
        try:
            response = await self.agent.run(prompt, session=self.session)
        except BudgetExceededError as error:
            await self.runtime.stop_task(error.reason)
            return f"STOPPED: {error.reason}"
        assert isinstance(response, AgentResponse)
        if self.provider_usage_recorded:
            return response.text
        latency_seconds = perf_counter() - started_at
        usage = response.usage_details or {}
        input_tokens = int(usage.get("input_token_count") or 0)
        output_tokens = int(usage.get("output_token_count") or 0)
        cost = float((response.additional_properties or {}).get("cost", 0) or 0)
        self.budget.record_llm_call(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            runtime_seconds=latency_seconds,
            cost=cost,
        )
        self.store.update_budget(
            budget_id=self.budget_id,
            llm_calls=1,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            runtime_seconds=latency_seconds,
            estimated_cost=cost,
        )
        self.store.append_event(
            event_id=f"event-{uuid4().hex}",
            run_id=self.assignment.run_id,
            task_id=self.assignment.task_id,
            tester_id=self.assignment.tester_id,
            browser_session_id=self.runtime.browser_session_id,
            event_type="LLM_CALL",
            tool="Microsoft Agent Framework",
            action="complex_reasoning",
            result={
                "agent": "tester",
                "finish_reason": str(response.finish_reason)
                if response.finish_reason is not None
                else None
            },
            latency_ms=latency_seconds * 1_000,
            cost=cost,
        )
        return response.text

    async def _prepare_task_prompt(self, prompt: str) -> str:
        """Move known initial navigation inspection and account setup out of model turns."""
        if self.runtime.browser_session_id is None:
            return prompt
        context = self.agent.additional_properties.get("task_context", {})
        page = self.runtime.browser_manager.get_session(self.runtime.browser_session_id).page
        state = await self.runtime.page_state_reader.read(page)
        username_reference = f"{context.get('identity_reference', '')}_username"
        secret_reference = context.get("secret_reference")
        preparation = "LOGIN_NOT_REQUESTED"
        if "register" not in context.get("required_operations", []) and username_reference in self.runtime.input_values and secret_reference in self.runtime.input_values:
            usernames = [element for element in state.interactive_elements if element.kind == "input" and "login" in element.label.casefold() and "username" in element.label.casefold()]
            passwords = [element for element in state.interactive_elements if element.kind == "input" and "login" in element.label.casefold() and "password" in element.label.casefold()]
            buttons = [element for element in state.interactive_elements if element.kind == "button" and element.label.casefold() == "login"]
            if len(usernames) == len(passwords) == len(buttons) == 1:
                actions = [WebAction(action_type=ActionType.INPUT, target=usernames[0].target, value_reference=username_reference, value=self.runtime.input_values[username_reference]), WebAction(action_type=ActionType.INPUT, target=passwords[0].target, value_reference=secret_reference, value=self.runtime.input_values[secret_reference]), WebAction(action_type=ActionType.CLICK, target=buttons[0].target)]
                preparation = "LOGIN_SUBMITTED"
                for action in actions:
                    result = await self.runtime.execute_known_action(action)
                    if not result.success:
                        preparation = result.error_type or "LOGIN_SETUP_FAILED"
                        break
        if self.runtime.browser_session_id is None:
            return prompt
        try:
            await page.wait_for_load_state("networkidle", timeout=5_000)
        except PlaywrightTimeoutError:
            preparation = "PAGE_NOT_SETTLED"
        inspection = await self.runtime.execute_known_action(WebAction(action_type=ActionType.DOM_INSPECTION, target="body"))
        self.runtime.remember_assertion_targets(inspection.data.get("rows", []))
        if preparation == "LOGIN_SUBMITTED":
            logged_in = self.runtime.input_values[username_reference] in str(inspection.data.get("text", "")) and any(element["label"].casefold() == "logout" for element in inspection.data.get("interactive_elements", []))
            preparation = "AUTHENTICATED" if logged_in else "LOGIN_NOT_ESTABLISHED"
        self.store.append_event(event_id=f"event-{uuid4().hex}", run_id=self.assignment.run_id, task_id=self.assignment.task_id, tester_id=self.assignment.tester_id, event_type="TESTER_PREPARATION", tool="Playwright", action="prepare_task_page", result={"status": preparation, "inspection_success": inspection.success}, latency_ms=0)
        initial = {"preparation": preparation, "page": inspection.data}
        return prompt + "\nInitial Page (navigation and this inspection are already complete): " + json.dumps(initial, ensure_ascii=False)
