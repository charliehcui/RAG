"""The single Tester Agent definition and its constrained tools."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from time import perf_counter
from typing import Any
from uuid import uuid4

from agent_framework import (
    Agent,
    AgentResponse,
    AgentSession,
    FunctionInvocationContext,
    FunctionMiddleware,
    MiddlewareTermination,
)

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

    async def execute_known_action(
        self,
        action_type: str,
        target: str | None = None,
        value: str | None = None,
        value_reference: str | None = None,
        url: str | None = None,
        expected: str | None = None,
        assertion: str = "contains",
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
        if behavior_id is not None and behavior_id not in self.runtime.expected_behavior_ids:
            return {"success": False, "error_type": "UNKNOWN_EXPECTED_BEHAVIOR"}
        if value_reference is not None:
            if value_reference not in self.runtime.input_values:
                return {"success": False, "error_type": "INPUT_VALUE_UNAVAILABLE", "reference": value_reference}
            value = self.runtime.input_values[value_reference]
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
        }

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
        status: str,
        expected_result: str,
        actual_result: str,
        severity_hint: str | None = None,
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
        history = self.store.list_action_history(run_id=self.assignment.run_id, task_id=self.assignment.task_id)
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
        """Finish execution; Python derives PASS/FAIL/UNKNOWN from recorded goal assertions."""
        return await self.runtime.record_task_outcome(finish=True)

    def read_test_data(self, reference: str) -> dict[str, Any]:
        """Read an allowed non-secret input. Secret references can only be filled by Runtime."""
        value = self.runtime.input_values.get(reference)
        if reference.startswith("env:"):
            return {"reference": reference, "available": value is not None, "instruction": "Pass this reference to execute_known_action; secret values are never returned."}
        return {"reference": reference, "value": value, "available": value is not None}


class FinishTaskMiddleware(FunctionMiddleware):
    async def process(self, context: FunctionInvocationContext, call_next: Callable[[], Awaitable[None]]) -> None:
        await call_next()
        if context.function.name == "finish_task":
            raise MiddlewareTermination("Task outcome recorded; no final Done model round is needed.", result=context.result)


def create_tester_agent(
    *, client: Any, assignment: TesterAssignment, tools: TesterAgentTools
) -> Agent:
    instructions = (
        "You are the only Tester Agent type. Work only on the assigned Task, scope, identity, namespace, and step budget. "
        "Use execute_known_action for known actions. Use explore_unknown_path instead of inventing unknown actions. "
        "Check shared Coverage before starting a path and collaborate only through structured Shared State facts. "
        "Never change global scope, bypass permissions, create another Agent, or declare a confirmed bug. "
        "Record uncertain behavior only with record_observation. Request replan when the local path cannot continue."
        " Mark actual goal assertions with goal_check=true or their supplied behavior_id. "
        "Use value_reference for configured inputs and account secret references. Never invent unavailable inputs. "
        "Use repeat_submit with the same-origin request URL and submit-button target to check one pending repeated form operation. "
        "When recording a deviation, identify its assigned behavior_id and related_task_ids for another Task's necessary setup or membership change. Read shared facts for those task references. "
        "Call finish_task after testing the goal; Python determines success. Do not produce a final Done summary."
    )
    if tools.action_policy == "TESTER_LLM_EVERY_STEP":
        instructions += " For this evaluation route, decide one browser action per model response. Call at most one browser action tool before reasoning again."
    if tools.decision_policy == "TESTER_LLM_EVERY_DECISION":
        instructions += " For unknown paths, inspect explore_unknown_path candidates and select exactly one ID with select_candidate."
    return Agent(
        client=client,
        name="tester-agent",
        description="Executes one assigned web testing task through the controlled runtime.",
        instructions=instructions,
        middleware=[TraceChatMiddleware(), TraceToolMiddleware(), FinishTaskMiddleware()],
        tools=[
            tools.execute_known_action,
            tools.explore_unknown_path,
            tools.select_candidate,
            tools.read_coverage,
            tools.read_shared_facts,
            tools.check_path_before_exploring,
            tools.record_finding,
            tools.record_observation,
            tools.update_task_progress,
            tools.request_replan,
            tools.use_computer_fallback,
            tools.finish_task,
            tools.read_test_data,
        ],
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
    ) -> None:
        self.agent = agent
        self.assignment = assignment
        self.runtime = runtime
        self.store = store
        self.budget = budget
        self.budget_id = budget_id
        self.provider_usage_recorded = provider_usage_recorded
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
