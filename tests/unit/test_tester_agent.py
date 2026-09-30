from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pytest
from agent_framework import BaseChatClient, ChatResponse, Content, Message
from agent_framework._tools import FunctionInvocationLayer

from web_testing_system.agents.tester_agent import TesterAgentTools as AgentTools
from web_testing_system.agents.tester_agent import TesterAssignment as Assignment
from web_testing_system.agents.tester_agent import TesterRunner as Runner
from web_testing_system.agents.tester_agent import create_tester_agent
from web_testing_system.runtime.budget import BudgetGuard, BudgetLimits
from web_testing_system.runtime.models import ActionResult, ActionType, WebAction


class FakeToolCallingClient(FunctionInvocationLayer, BaseChatClient):
    def __init__(self) -> None:
        self.call_count = 0
        super().__init__()

    async def _inner_get_response(
        self,
        *,
        messages: Sequence[Message],
        stream: bool,
        options: Mapping[str, Any],
        **kwargs: Any,
    ) -> ChatResponse:
        self.call_count += 1
        if self.call_count == 1:
            return ChatResponse(
                messages=[
                    Message(
                        role="assistant",
                        contents=[
                            Content.from_function_call(
                                "call-1",
                                "request_replan",
                                arguments={"reason": "no useful path"},
                            )
                        ],
                    )
                ],
                usage_details={
                    "input_token_count": 4,
                    "output_token_count": 1,
                    "total_token_count": 5,
                },
            )
        return ChatResponse(
            messages=[Message(role="assistant", contents=["Replan requested."])],
            usage_details={
                "input_token_count": 2,
                "output_token_count": 2,
                "total_token_count": 4,
            },
        )


class FakeRuntime:
    def __init__(self) -> None:
        self.browser_session_id = "browser-1"
        self.replan_reasons: list[str] = []
        self.stop_reasons: list[str] = []
        self.known_calls = 0

    async def request_replan(self, reason: str) -> dict[str, object]:
        self.replan_reasons.append(reason)
        return {"requested": True, "reason": reason}

    async def stop_task(self, reason: str) -> None:
        self.stop_reasons.append(reason)

    async def execute_known_action(self, action: WebAction) -> ActionResult:
        self.known_calls += 1
        return ActionResult(
            started_at="start",
            ended_at="end",
            latency_ms=1,
            success=True,
            error=None,
            error_type=None,
        )


class FakeStore:
    def list_paths(self, run_id: str) -> list[dict[str, Any]]:
        return []

    def create_finding(self, **finding: Any) -> dict[str, Any]:
        return finding


def assignment() -> Assignment:
    return Assignment(
        run_id="run-1",
        task_id="task-1",
        tester_id="tester-1",
        identity_id="identity-1",
        role="member",
        data_namespace="run-run-1-tester-tester-1-task-task-1",
        scope=("https://app.test/",),
        step_budget=10,
    )


def budget(max_llm_calls: int = 2) -> BudgetGuard:
    return BudgetGuard(
        BudgetLimits(
            max_runtime_seconds=60,
            max_llm_calls=max_llm_calls,
            max_input_tokens=100,
            max_output_tokens=100,
            max_jev_calls=5,
            max_computer_use_calls=0,
            max_task_steps=10,
            max_task_replans=2,
            max_browser_contexts=1,
        )
    )


@pytest.mark.asyncio
async def test_maf_fake_llm_invokes_only_the_exposed_replan_tool() -> None:
    fake_runtime = FakeRuntime()
    fake_store = FakeStore()
    client = FakeToolCallingClient()
    tester_tools = AgentTools(
        assignment=assignment(), runtime=fake_runtime, store=fake_store
    )  # type: ignore[arg-type]
    agent = create_tester_agent(
        client=client, assignment=assignment(), tools=tester_tools
    )

    response = await agent.run("The local path cannot continue")

    assert response.text == "Replan requested."
    assert fake_runtime.replan_reasons == ["no useful path"]
    assert client.call_count == 2


@pytest.mark.asyncio
async def test_known_action_bypasses_llm_and_llm_budget_stops_before_call() -> None:
    fake_runtime = FakeRuntime()
    fake_store = FakeStore()
    client = FakeToolCallingClient()
    tester_tools = AgentTools(
        assignment=assignment(), runtime=fake_runtime, store=fake_store
    )  # type: ignore[arg-type]
    agent = create_tester_agent(
        client=client, assignment=assignment(), tools=tester_tools
    )
    runner = Runner(
        agent=agent,
        assignment=assignment(),
        runtime=fake_runtime,
        store=fake_store,
        budget=budget(max_llm_calls=0),
        budget_id="budget-1",
    )  # type: ignore[arg-type]

    known_result = await runner.execute_known(
        WebAction(action_type=ActionType.ASSERTION, target="#status", expected="ready")
    )
    stopped_result = await runner.ask_tester_llm("Think about this")

    assert known_result["success"] is True
    assert fake_runtime.known_calls == 1
    assert stopped_result == "STOPPED: MAX_LLM_CALLS_REACHED"
    assert fake_runtime.stop_reasons == ["MAX_LLM_CALLS_REACHED"]
    assert client.call_count == 0
