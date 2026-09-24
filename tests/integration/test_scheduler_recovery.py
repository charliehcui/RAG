from __future__ import annotations

import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from agent_framework import BaseChatClient, ChatResponse, Message
from agent_framework._tools import FunctionInvocationLayer

from web_testing_system.agents.tester_agent import TesterAgentTools as AgentTools
from web_testing_system.agents.tester_agent import TesterAssignment as Assignment
from web_testing_system.agents.tester_agent import TesterRunner as Runner
from web_testing_system.agents.tester_agent import create_tester_agent
from web_testing_system.runtime.budget import BudgetGuard, BudgetLimits
from web_testing_system.scheduler import LocalTesterScheduler
from web_testing_system.scheduler import TesterInstance as ScheduledInstance
from web_testing_system.state import StateStore, build_data_namespace


class IdleClient(FunctionInvocationLayer, BaseChatClient):
    async def _inner_get_response(
        self,
        *,
        messages: Sequence[Message],
        stream: bool,
        options: Mapping[str, Any],
        **kwargs: Any,
    ) -> ChatResponse:
        del messages, stream, options, kwargs
        return ChatResponse(messages=[Message(role="assistant", contents=["Done."])])


class FakeBrowserManager:
    async def close_session(self, session_id: str) -> None:
        return None


class FakeRuntime:
    def __init__(self, tester_id: str) -> None:
        self.browser_session_id: str | None = None
        self.browser_manager = FakeBrowserManager()
        self.tester_id = tester_id

    async def start_session(self) -> str:
        self.browser_session_id = f"browser-{self.tester_id}"
        return self.browser_session_id


def budget() -> BudgetGuard:
    return BudgetGuard(
        BudgetLimits(
            max_runtime_seconds=30,
            max_llm_calls=1,
            max_input_tokens=100,
            max_output_tokens=100,
            max_laya_calls=1,
            max_computer_use_calls=0,
            max_task_steps=5,
            max_task_replans=1,
            max_browser_contexts=1,
        )
    )


def make_runner(
    store: StateStore, tester_id: str, identity_id: str, task_id: str
) -> tuple[Runner, AgentTools]:
    runtime = FakeRuntime(tester_id)
    assignment = Assignment(
        run_id="run-recovery",
        task_id=task_id,
        tester_id=tester_id,
        identity_id=identity_id,
        role="member",
        data_namespace=build_data_namespace("run-recovery", tester_id, task_id),
        scope=("http://app.test/",),
        step_budget=5,
    )
    tools = AgentTools(
        assignment=assignment,
        runtime=runtime,  # type: ignore[arg-type]
        store=store,
    )
    agent = create_tester_agent(
        client=IdleClient(), assignment=assignment, tools=tools
    )
    runner = Runner(
        agent=agent,
        assignment=assignment,
        runtime=runtime,  # type: ignore[arg-type]
        store=store,
        budget=budget(),
        budget_id=f"budget-{task_id}",
    )
    return runner, tools


@pytest.mark.integration
@pytest.mark.asyncio
async def test_tester_failure_preserves_facts_for_a_new_session() -> None:
    temporary_root = Path(".pytest-temp")
    temporary_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=temporary_root) as directory:
        store = StateStore(Path(directory) / "scheduler-recovery.db")
        store.initialize()
        store.create_run(
            run_id="run-recovery",
            application="http://app.test/",
            application_version="test",
            test_goal="Test recovery",
            scope={"allowed_scope": ["/"]},
            status="RUNNING",
            global_budget={"browser_steps": 10},
            remaining_budget={"browser_steps": 10},
        )
        runners: list[Runner] = []
        tools_by_tester: dict[str, AgentTools] = {}
        for suffix in ("a", "b"):
            tester_id = f"tester-{suffix}"
            identity_id = f"identity-{suffix}"
            task_id = f"task-{suffix}"
            store.create_identity(
                identity_id=identity_id,
                run_id="run-recovery",
                role="member",
                secret_reference=f"env:MEMBER_{suffix.upper()}_PASSWORD",
                permissions=["read"],
            )
            store.create_task(
                task_id=task_id,
                run_id="run-recovery",
                goal=f"Run {suffix}",
                priority="P1",
                dependencies=[],
                created_by="main-agent",
                step_budget=5,
                data_requirements={"role": "member"},
            )
            runner, tools = make_runner(store, tester_id, identity_id, task_id)
            store.register_tester(
                tester_id=tester_id,
                run_id="run-recovery",
                session_reference=runner.session.session_id,
                identity_id=identity_id,
                role="member",
                data_namespace=runner.assignment.data_namespace,
            )
            store.create_budget(
                budget_id=f"budget-{task_id}",
                run_id="run-recovery",
                task_id=task_id,
            )
            runners.append(runner)
            tools_by_tester[tester_id] = tools

        scheduler = LocalTesterScheduler(
            store=store,
            run_id="run-recovery",
            testers=[
                ScheduledInstance("tester-a", runners[0]),
                ScheduledInstance("tester-b", runners[1]),
            ],
            max_testers=2,
            max_browser_contexts=2,
        )

        async def execute_task(runner: Runner, task: Mapping[str, Any]) -> str:
            if runner.assignment.tester_id == "tester-a":
                tools_by_tester["tester-a"].record_finding(
                    title="Saved before Tester failure",
                    status="OBSERVATION",
                    expected_result="Tester completes",
                    actual_result="Tester session failed",
                )
                raise RuntimeError("fake Tester session failure")
            return "COMPLETED"

        results = await scheduler.run_ready_tasks(execute_task)

        failed = next(result for result in results if result.tester_id == "tester-a")
        assert failed.status == "FAILED"
        assert failed.error == "fake Tester session failure"
        assert store.get_task("task-a")["finished_at"] is not None  # type: ignore[index]
        saved_finding = store.list_recent_findings("run-recovery")[0]
        assert saved_finding["title"] == "Saved before Tester failure"

        old_session_id = runners[0].session.session_id
        new_runner, new_tools = make_runner(
            store, "tester-a", "identity-a", "task-a"
        )
        assert new_runner.session.session_id != old_session_id
        assert new_runner.session.state == {}
        assert new_tools.read_shared_facts()["recent_findings"][0]["finding_id"] == saved_finding["finding_id"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_scheduler_does_not_claim_tasks_after_run_budget_is_exhausted() -> None:
    temporary_root = Path(".pytest-temp")
    temporary_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=temporary_root) as directory:
        store = StateStore(Path(directory) / "scheduler-budget.db")
        store.initialize()
        store.create_run(
            run_id="run-recovery",
            application="http://app.test/",
            application_version="test",
            test_goal="Test budget stop",
            scope={"allowed_scope": ["/"]},
            status="RUNNING",
            global_budget={"browser_steps": 2},
            remaining_budget={"browser_steps": 0},
        )
        scheduled_instances: list[ScheduledInstance] = []
        for suffix in ("a", "b"):
            tester_id = f"tester-{suffix}"
            identity_id = f"identity-{suffix}"
            task_id = f"task-{suffix}"
            store.create_identity(
                identity_id=identity_id,
                run_id="run-recovery",
                role="member",
                secret_reference=f"env:MEMBER_{suffix.upper()}_PASSWORD",
                permissions=["read"],
            )
            store.create_task(
                task_id=task_id,
                run_id="run-recovery",
                goal=f"Run {suffix}",
                priority="P1",
                dependencies=[],
                created_by="main-agent",
                step_budget=5,
                data_requirements={"role": "member"},
            )
            runner, _ = make_runner(store, tester_id, identity_id, task_id)
            store.register_tester(
                tester_id=tester_id,
                run_id="run-recovery",
                session_reference=runner.session.session_id,
                identity_id=identity_id,
                role="member",
                data_namespace=runner.assignment.data_namespace,
            )
            scheduled_instances.append(ScheduledInstance(tester_id, runner))

        scheduler = LocalTesterScheduler(
            store=store,
            run_id="run-recovery",
            testers=scheduled_instances,
            max_testers=2,
            max_browser_contexts=2,
        )

        async def must_not_run(runner: Runner, task: Mapping[str, Any]) -> str:
            raise AssertionError(f"budget-exhausted task ran: {runner}, {task}")

        assert await scheduler.run_ready_tasks(must_not_run) == []
        assert all(
            store.get_task(f"task-{suffix}")["status"] == "PENDING"  # type: ignore[index]
            for suffix in ("a", "b")
        )
