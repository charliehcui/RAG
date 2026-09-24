"""The single Tester Agent definition and its constrained tools."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from time import perf_counter
from typing import Any
from uuid import uuid4

from agent_framework import Agent, AgentResponse, AgentSession

from web_testing_system.runtime.budget import BudgetExceededError, BudgetGuard
from web_testing_system.runtime.models import ActionType, WebAction
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
    ) -> None:
        self.assignment = assignment
        self.runtime = runtime
        self.store = store

    async def execute_known_action(
        self,
        action_type: str,
        target: str | None = None,
        value: str | None = None,
        url: str | None = None,
        expected: str | None = None,
        assertion: str = "contains",
        key: str | None = None,
        wait_ms: int = 0,
        resource_id: str | None = None,
        requires_resource: bool = False,
        confirmed: bool = False,
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
        action = WebAction(
            action_type=parsed_action,
            target=target,
            value=value,
            url=url,
            expected=expected,
            assertion=assertion,
            key=key,
            wait_ms=wait_ms,
            resource_id=resource_id,
            requires_resource=requires_resource,
            confirmed=confirmed,
        )
        return (await self.runtime.execute_known_action(action)).to_dict()

    async def explore_unknown_path(self, current_goal: str) -> dict[str, Any]:
        """Use Candidate Builder and Laya for an unknown legal path."""
        return asdict(await self.runtime.explore_unknown_path(current_goal))

    def read_coverage(self) -> list[dict[str, Any]]:
        """Read coverage already recorded for the current run."""
        return self.store.list_paths(self.assignment.run_id)

    def record_observation(
        self,
        title: str,
        expected_result: str,
        actual_result: str,
        affected_page: str | None = None,
    ) -> dict[str, Any]:
        """Record an observation without declaring a confirmed bug."""
        return self.store.create_finding(
            finding_id=f"finding-{uuid4().hex}",
            run_id=self.assignment.run_id,
            task_id=self.assignment.task_id,
            title=title,
            status="OBSERVATION",
            expected_result=expected_result,
            actual_result=actual_result,
            first_seen_by=self.assignment.tester_id,
            needs_confirmation=True,
            affected_role=self.assignment.role,
            affected_page=affected_page,
        )

    async def update_task_progress(
        self, progressed: bool, summary: str
    ) -> dict[str, object]:
        """Record local Task progress and trigger replan after repeated no-progress steps."""
        return await self.runtime.note_progress(progressed=progressed, summary=summary)

    async def request_replan(self, reason: str) -> dict[str, object]:
        """Request Main Agent replanning without changing global scope."""
        return await self.runtime.request_replan(reason)


def create_tester_agent(
    *, client: Any, assignment: TesterAssignment, tools: TesterAgentTools
) -> Agent:
    instructions = (
        "You are the only Tester Agent type. Work only on the assigned Task, scope, identity, namespace, and step budget. "
        "Use execute_known_action for known actions. Use explore_unknown_path instead of inventing unknown actions. "
        "Never change global scope, bypass permissions, create another Agent, or declare a confirmed bug. "
        "Record uncertain behavior only with record_observation. Request replan when the local path cannot continue."
    )
    return Agent(
        client=client,
        name="tester-agent",
        description="Executes one assigned web testing task through the controlled runtime.",
        instructions=instructions,
        tools=[
            tools.execute_known_action,
            tools.explore_unknown_path,
            tools.read_coverage,
            tools.record_observation,
            tools.update_task_progress,
            tools.request_replan,
        ],
        additional_properties={"assignment": asdict(assignment)},
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
    ) -> None:
        self.agent = agent
        self.assignment = assignment
        self.runtime = runtime
        self.store = store
        self.budget = budget
        self.budget_id = budget_id
        self.session = AgentSession(session_id=f"tester-session-{uuid4().hex}")

    async def execute_known(self, action: WebAction) -> dict[str, Any]:
        """Known Replay and assertions bypass the LLM and go directly to Playwright."""
        return (await self.runtime.execute_known_action(action)).to_dict()

    async def explore(self, current_goal: str) -> dict[str, Any]:
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
        response = await self.agent.run(prompt, session=self.session)
        assert isinstance(response, AgentResponse)
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
            event_type="TESTER_LLM_CALL",
            tool="Microsoft Agent Framework",
            action="complex_reasoning",
            result={
                "finish_reason": str(response.finish_reason)
                if response.finish_reason is not None
                else None
            },
            latency_ms=latency_seconds * 1_000,
            cost=cost,
        )
        return response.text
