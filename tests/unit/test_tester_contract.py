from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from agent_framework import BaseChatClient, ChatResponse, Content, Message
from agent_framework._tools import FunctionInvocationLayer

from web_testing_system.agents.tester_agent import TesterAgentTools as AgentTools
from web_testing_system.agents.tester_agent import TesterAssignment as Assignment
from web_testing_system.agents.tester_agent import TesterRunner as Runner
from web_testing_system.agents.tester_agent import create_tester_agent
from web_testing_system.runtime.budget import BudgetGuard, BudgetLimits
from web_testing_system.runtime.web_runtime import WebTestingRuntime
from web_testing_system.state import StateStore


@pytest.fixture
def contract_tools(tmp_path: Any) -> AgentTools:
    store = StateStore(tmp_path / "contract.db")
    store.initialize()
    checks = [{"check_id": "C.empty", "behavior_id": "EB-name", "depends_on": []}, {"check_id": "C.valid", "behavior_id": "EB-name", "depends_on": ["C.empty"]}]
    store.create_run(run_id="run", application="http://app.test", application_version="test", test_goal="Validation", scope={}, status="RUNNING", global_budget={}, remaining_budget={})
    store.create_identity(identity_id="admin", run_id="run", role="admin", secret_reference="env:PASSWORD", permissions=["read", "create"])
    store.register_tester(tester_id="tester", run_id="run", session_reference="session", identity_id="admin", role="admin", data_namespace="contract")
    store.create_task(task_id="task", run_id="run", goal="Validation", priority="P1", dependencies=[], created_by="main", step_budget=20, data_requirements={"required_checks": checks})
    store.create_budget(budget_id="budget", run_id="run", task_id="task")
    budget = BudgetGuard(BudgetLimits(max_runtime_seconds=60, max_llm_calls=3, max_input_tokens=10000, max_output_tokens=10000, max_jev_calls=10, max_computer_use_calls=0, max_task_steps=20, max_task_replans=2, max_browser_contexts=1))
    outcome = {"checks": [{**check, "completed": False} for check in checks], "assertions": [], "ready_to_finish": False}
    runtime = SimpleNamespace(input_values={"empty_name": "", "valid_name": "Contract Project", "other_name": "Unassigned", "admin_username": "admin", "env:PASSWORD": "secret-never-in-context"}, required_checks=checks, expected_behavior_ids=("EB-name",), prepared_check_ids=set(), browser_session_id=None, task_finished=False, budget=budget, record_task_outcome=AsyncMock(return_value=outcome), execute_page_goal=AsyncMock(side_effect=AssertionError("Invalid plans must not reach Jev")))

    async def stop(reason: str) -> None:
        runtime.task_finished = True

    runtime.stop_task = AsyncMock(side_effect=stop)
    assignment = Assignment(run_id="run", task_id="task", tester_id="tester", identity_id="admin", role="admin", data_namespace="contract", scope=("/",), step_budget=20)
    return AgentTools(assignment=assignment, runtime=cast(WebTestingRuntime, runtime), store=store, task_context={"goal": "Validate empty then valid name", "identity_reference": "admin", "secret_reference": "env:PASSWORD", "test_data_references": ["empty_name", "valid_name"], "required_operations": ["create"]})


def test_plan_schema_advertises_task_ids_and_position_specific_actions(contract_tools: AgentTools) -> None:
    schema = contract_tools.plan_tool().parameters()
    definitions = schema["$defs"]
    assert set(definitions["PlanAssertion"]["properties"]["action_type"]["enum"]) == {"assertion", "url_check"}
    assert "click" not in definitions["PlanBoundary"]["properties"]["action_type"]["enum"]
    assert "repeat_submit" in definitions["PlanBoundary"]["properties"]["action_type"]["enum"]
    assert definitions["PlanAssertion"]["properties"]["check_id"]["enum"] == [None, "C.empty", "C.valid"]
    assert set(definitions["PageInput"]["properties"]["value_reference"]["enum"]) == {"empty_name", "valid_name", "admin_username", "env:PASSWORD"}
    assert "env:PASSWORD" not in definitions["PlanAssertion"]["properties"]["expected_reference"]["enum"]
    assert "empty_name" not in definitions["PageGoal"]["properties"]["row_reference"]["enum"]


def test_schema_separates_observed_form_scopes_from_entity_bindings(contract_tools: AgentTools) -> None:
    page = {"rows": [{"target": '[data-project-id="p1"]'}], "interactive_elements": [{"kind": "input", "target": "#project-name", "form_target": "#create-form", "context_target": "#create-form"}]}
    definitions = contract_tools.plan_tool(page).parameters()["$defs"]
    assert definitions["PageGoal"]["properties"]["form_context"]["enum"] == ["", "#create-form"]
    assert "#project-name" not in definitions["PageInput"]["properties"]["context"]["enum"]
    assert "valid_name" in definitions["PageGoal"]["properties"]["context"]["enum"]
    assert "#create-form" not in definitions["PlanAssertion"]["properties"]["context"]["enum"]
    assert "secret-never-in-context" not in json.dumps(definitions)


@pytest.mark.asyncio
@pytest.mark.parametrize("location", ["goal.context", "goal.form_context", "input.context", "step.context"])
async def test_prose_binding_context_is_rejected_before_jev(contract_tools: AgentTools, location: str) -> None:
    goal: dict[str, Any] = {"goal": "Submit the supplied name", "inputs": [{"control": "Project name", "value_reference": "valid_name"}], "checks": [assertion("C.empty"), assertion("C.valid")]}
    prose = "Current authenticated Projects page, fill and submit the Create Project form"
    if location.startswith("goal."):
        goal[location.split(".")[1]] = prose
    elif location == "input.context":
        goal["inputs"][0]["context"] = prose
    else:
        goal["checks"][0]["context"] = prose
    result = await contract_tools.execute_test_plan([goal])  # type: ignore[list-item]
    assert result["reason"] == "INVALID_BINDING_CONTEXT"
    assert result["binding_field"] == location
    assert "" in result["allowed_contexts"]
    contract_tools.runtime.execute_page_goal.assert_not_awaited()  # type: ignore[attr-defined]
    assert not contract_tools.store.list_action_history(run_id="run", task_id="task")


@pytest.mark.asyncio
async def test_exception_context_retains_pending_fields_without_raw_values_or_history(contract_tools: AgentTools) -> None:
    runtime = contract_tools.runtime
    runtime.execute_page_goal.side_effect = None  # type: ignore[attr-defined]
    runtime.execute_page_goal.return_value = {"success": False, "reason": "UNBOUND_REQUIRED_INPUT", "pending_inputs": ["Display name"], "actions": [{"action": "input", "control": "Register password", "value_reference": "env:PASSWORD", "value": "secret-never-in-context"}]}  # type: ignore[attr-defined]
    goal = {"goal": "Register using the supplied inputs", "inputs": [{"control": "Display name", "value_reference": "valid_name"}], "checks": [assertion("C.empty"), assertion("C.valid")]}
    await contract_tools.execute_test_plan([goal])  # type: ignore[list-item]
    failure = contract_tools.task_context["latest_plan_failure"]
    context = await contract_tools.planning_context(failure)
    assert failure["reason"] == "UNBOUND_REQUIRED_INPUT"
    assert failure["pending_inputs"] == ["Display name"]
    assert failure["last_action"] == {"action": "input", "control": "Register password", "value_reference": "env:PASSWORD"}
    assert "failed_execution" not in json.dumps(context)
    assert "secret-never-in-context" not in json.dumps(context)


@pytest.mark.asyncio
async def test_contract_preserves_independent_check_state_and_hides_unassigned_inputs(contract_tools: AgentTools) -> None:
    runtime = contract_tools.runtime
    runtime.record_task_outcome.return_value["checks"][0]["completed"] = True  # type: ignore[attr-defined]
    runtime.prepared_check_ids.add("C.valid")
    context = await contract_tools.planning_context({"reason": "DUPLICATE_CHECK_ID"})
    assert context["completed_check_ids"] == ["C.empty"]
    assert context["remaining_check_ids"] == ["C.valid"]
    assert context["prepared_check_ids"] == ["C.valid"]
    assert context["assigned_checks"][1]["depends_on"] == ["C.empty"]
    assert context["test_inputs"]["empty_name"] == ""
    assert "secret-never-in-context" not in json.dumps(context)
    assert "other_name" not in json.dumps(context)


def assertion(check_id: str) -> dict[str, Any]:
    return {"action_type": "assertion", "target": "body", "expected": "Validation", "behavior_id": "EB-name", "check_id": check_id}


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["DUPLICATE_CHECK_ID", "INPUT_VALUE_UNAVAILABLE", "INPUT_REFERENCE_NOT_ALLOWED", "PLAN_BOUNDARY_ACTION_REQUIRED", "PAGE_GOAL_CHECK_REQUIRES_ASSERTION", "INCOMPLETE_TEST_PLAN", "INVALID_TEST_PLAN_SCHEMA"])
async def test_invalid_whole_plan_never_enters_jev(contract_tools: AgentTools, failure: str) -> None:
    goal = {"goal": "Validate the field", "checks": [assertion("C.empty"), assertion("C.valid")]}
    if failure == "DUPLICATE_CHECK_ID":
        goal["checks"] = [assertion("C.empty"), assertion("C.empty")]
    elif failure in {"INPUT_VALUE_UNAVAILABLE", "INPUT_REFERENCE_NOT_ALLOWED"}:
        goal["inputs"] = [{"control": "Name", "value_reference": "invented" if failure == "INPUT_VALUE_UNAVAILABLE" else "other_name"}]
    elif failure == "PLAN_BOUNDARY_ACTION_REQUIRED":
        goal["before_steps"] = [{"action_type": "click", "target": "#submit"}]
    elif failure == "PAGE_GOAL_CHECK_REQUIRES_ASSERTION":
        goal["checks"][0]["action_type"] = "refresh"
    elif failure == "INCOMPLETE_TEST_PLAN":
        goal["checks"] = [assertion("C.empty")]
    else:
        goal["checks"][0]["action_type"] = "invented-action"
    result = await contract_tools.execute_test_plan([goal])  # type: ignore[list-item]
    assert result["reason"] == failure
    contract_tools.runtime.execute_page_goal.assert_not_awaited()  # type: ignore[attr-defined]
    assert not contract_tools.store.list_action_history(run_id="run", task_id="task")
    assert len(contract_tools.store.list_events("run", event_types=("TESTER_PLAN",))) == 1


@pytest.mark.asyncio
async def test_three_failed_plans_use_fresh_context_and_original_two_replans(contract_tools: AgentTools) -> None:
    class InvalidClient(FunctionInvocationLayer, BaseChatClient):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def _inner_get_response(self, *, messages: Sequence[Message], stream: bool, options: Mapping[str, Any], **kwargs: Any) -> ChatResponse:
            self.calls += 1
            assert self.calls <= 3
            assert len(messages) == 1 and messages[0].role == "user"
            context = json.loads(messages[0].text.split("Task Contract: ", 1)[1])
            assert context["remaining_check_ids"] == ["C.empty", "C.valid"]
            assert context["latest_failure"] is None if self.calls == 1 else context["latest_failure"]["reason"] == "DUPLICATE_CHECK_ID"
            assert "secret-never-in-context" not in messages[0].text
            assert [tool.name for tool in options["tools"]] == ["execute_test_plan"]
            goal = {"goal": "Validate field", "checks": [assertion("C.empty"), assertion("C.empty")]}
            return ChatResponse(messages=[Message(role="assistant", contents=[Content.from_function_call(f"plan-{self.calls}", "execute_test_plan", arguments={"goals": [goal]})])], usage_details={"input_token_count": 4, "output_token_count": 2})

    client = InvalidClient()
    runner = Runner(agent=create_tester_agent(client=client, assignment=contract_tools.assignment, tools=contract_tools), assignment=contract_tools.assignment, runtime=contract_tools.runtime, store=contract_tools.store, budget=contract_tools.runtime.budget, budget_id="budget")
    await runner.ask_tester_llm("Execute assigned Task task: validation")
    assert client.calls == 3
    assert contract_tools.runtime.budget.usage.task_replans == 2
    assert contract_tools.runtime.budget.usage.llm_calls == 3
    assert contract_tools.runtime.task_finished
    contract_tools.runtime.execute_page_goal.assert_not_awaited()  # type: ignore[attr-defined]
