from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from types import MethodType, SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from agent_framework import BaseChatClient, ChatResponse, Content, Message
from agent_framework._tools import FunctionInvocationLayer

from web_testing_system.agents.tester_agent import (
    PageGoal,
    PageStep,
    create_tester_agent,
)
from web_testing_system.agents.tester_agent import TesterAgentTools as AgentTools
from web_testing_system.agents.tester_agent import TesterAssignment as Assignment
from web_testing_system.agents.tester_agent import TesterRunner as Runner
from web_testing_system.runtime.budget import BudgetGuard, BudgetLimits
from web_testing_system.runtime.models import ActionType, InteractiveElement
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
    runtime.store = store
    runtime.run_id = "run"
    runtime.task_id = "task"
    runtime.tester_id = "tester"
    runtime.validate_assertion_contract = MethodType(WebTestingRuntime.validate_assertion_contract, runtime)
    runtime.validate_plan_assertions = MethodType(WebTestingRuntime.validate_plan_assertions, runtime)
    runtime.register_progress_plan = MethodType(WebTestingRuntime.register_progress_plan, runtime)

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
    assert "operation" in definitions["PageGoal"]["required"]
    assert "navigate" in definitions["PageGoal"]["properties"]["operation"]["enum"]
    assert "destination" in definitions["PageGoal"]["required"]
    assert definitions["PageGoal"]["allOf"][0]["then"]["properties"]["destination"]["type"] == "string"
    assert len(schema["properties"]["goals"]["allOf"]) == 2
    assert len(definitions["PlanAssertion"]["allOf"]) == 3
    assert definitions["PlanBoundary"]["properties"]["wait_ms"]["maximum"] == 2000
    assert "assertion" not in definitions["PlanBoundary"]["required"]


@pytest.mark.asyncio
async def test_unused_boundary_assertion_null_is_normalized_without_weakening_real_assertions(contract_tools: AgentTools) -> None:
    contract_tools.execute_page_goals = AsyncMock(return_value={"success": False, "reason": "LOCAL_EXECUTION_MARKER"})  # type: ignore[method-assign]
    goal = {"goal": "Verify checks after refresh", "operation": "observe", "run_operations": False, "before_steps": [{"action_type": "refresh", "assertion": None}], "checks": [assertion("C.empty"), assertion("C.valid")]}
    result = await contract_tools.execute_test_plan([goal])  # type: ignore[list-item]
    assert result["reason"] == "LOCAL_EXECUTION_MARKER", result
    assert contract_tools.execute_page_goals.call_args.args[0][0].before_steps[0].assertion == "contains"
    goal["checks"][0]["assertion"] = None
    result = await contract_tools.execute_test_plan([goal])  # type: ignore[list-item]
    assert result["reason"] == "INVALID_TEST_PLAN_SCHEMA"


@pytest.mark.asyncio
async def test_invalid_wait_is_rejected_before_any_operation(contract_tools: AgentTools) -> None:
    result = await contract_tools.execute_test_plan([{"goal": "Create then settle", "operation": "create", "inputs": [{"control": "Name", "value_reference": "valid_name"}], "checks": [assertion("C.empty"), assertion("C.valid")], "after_steps": [{"action_type": "wait", "wait_ms": 3000}]}])  # type: ignore[list-item]
    assert result["reason"] == "INVALID_WAIT_DURATION"
    contract_tools.runtime.execute_page_goal.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
@pytest.mark.parametrize("replan", [False, True])
@pytest.mark.parametrize("behavior_id", [None, "EB-name"])
@pytest.mark.parametrize("boundary", [False, True])
async def test_complete_required_ids_do_not_reject_an_unscored_auxiliary_assertion(contract_tools: AgentTools, replan: bool, behavior_id: str | None, boundary: bool) -> None:
    contract_tools.plan_started = replan
    contract_tools.execute_page_goals = AsyncMock(return_value={"success": False, "reason": "LOCAL_EXECUTION_MARKER"})  # type: ignore[method-assign]
    goal = {"goal": "Verify supplied checks and an incidental page precondition", "operation": "observe", "run_operations": False, "checks": [assertion("C.empty"), assertion("C.valid"), {"action_type": "assertion", "target": "body", "assertion": "visible", "behavior_id": behavior_id}]}
    if boundary:
        goal["before_steps"] = [{"action_type": "refresh", "behavior_id": behavior_id}]
    result = await contract_tools.execute_test_plan([goal])  # type: ignore[list-item]
    assert result["reason"] == "LOCAL_EXECUTION_MARKER", result
    contract_tools.execute_page_goals.assert_awaited_once()
    executed = contract_tools.execute_page_goals.call_args.args[0][0]
    assert executed.checks[-1].behavior_id is None and executed.checks[-1].check_id is None


@pytest.mark.asyncio
async def test_check_id_supplies_redundant_behavior_id_without_guessing_missing_checks(contract_tools: AgentTools) -> None:
    contract_tools.execute_page_goals = AsyncMock(return_value={"success": False, "reason": "LOCAL_EXECUTION_MARKER"})  # type: ignore[method-assign]
    checks = [{**assertion(check_id), "behavior_id": None} for check_id in ["C.empty", "C.valid"]]
    result = await contract_tools.execute_test_plan([{"goal": "Check assigned IDs", "operation": "observe", "checks": checks}])  # type: ignore[list-item]
    assert result["reason"] == "LOCAL_EXECUTION_MARKER"
    assert all(step.behavior_id == "EB-name" for step in contract_tools.execute_page_goals.call_args.args[0][0].checks)


@pytest.mark.asyncio
async def test_behavior_only_assertion_does_not_replace_a_missing_required_check(contract_tools: AgentTools) -> None:
    result = await contract_tools.execute_test_plan([{"goal": "Incomplete evidence", "operation": "observe", "checks": [assertion("C.empty"), {"action_type": "assertion", "target": "body", "expected": "Validation", "behavior_id": "EB-name"}]}])  # type: ignore[list-item]
    assert result["reason"] == "CHECK_ID_REQUIRED" and result["missing_check_ids"] == ["C.valid"]
    contract_tools.runtime.execute_page_goal.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_replan_must_preserve_a_completed_checks_unpublished_live_signal(contract_tools: AgentTools) -> None:
    contract_tools.runtime.register_progress_plan([{"publish_progress": "member-session-ready", "checks": [assertion("C.empty")]}])
    contract_tools.runtime.record_task_outcome.return_value["checks"][0]["completed"] = True  # type: ignore[attr-defined]
    context = await contract_tools.planning_context()
    assert context["pending_progress_signals"] == ["member-session-ready"]
    result = await contract_tools.execute_test_plan([{"goal": "Remaining check with lost continuation", "operation": "observe", "checks": [assertion("C.valid")]}])  # type: ignore[list-item]
    assert result["reason"] == "INCOMPLETE_PROGRESS_PLAN"
    assert result["missing_progress_signals"] == ["member-session-ready"]
    contract_tools.runtime.execute_page_goal.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,reason", [(None, "EXPLICIT_OPERATION_REQUIRED"), ("navigate", "NAVIGATION_DESTINATION_REQUIRED"), ("delete", "ROW_REFERENCE_REQUIRED")])
async def test_operation_intent_and_object_are_required_before_execution(contract_tools: AgentTools, operation: str | None, reason: str) -> None:
    result = await contract_tools.execute_test_plan([{"goal": "Perform assigned operation", "operation": operation, "checks": [assertion("C.empty"), assertion("C.valid")]}])  # type: ignore[list-item]
    assert result["reason"] == reason
    contract_tools.runtime.execute_page_goal.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", [False, True])
async def test_blank_or_missing_navigation_destination_rejects_entire_plan(contract_tools: AgentTools, boundary: bool) -> None:
    goals = [{"goal": "Check before navigating", "operation": "observe", "checks": [assertion("C.empty")]}, {"goal": "Reopen the view", "operation": "navigate", "destination": "   ", "checks": [assertion("C.valid")]}]
    if boundary:
        goals[1].update(operation="observe", before_steps=[{"action_type": "navigation"}])
    result = await contract_tools.execute_test_plan(goals)  # type: ignore[arg-type]
    assert result["reason"] == "NAVIGATION_DESTINATION_REQUIRED"
    contract_tools.runtime.execute_page_goal.assert_not_awaited()  # type: ignore[attr-defined]
    assert not contract_tools.store.list_events("run", event_types=("TESTER_PLAN_VALIDATED",))


@pytest.mark.asyncio
async def test_complete_plan_with_inverted_check_dependencies_is_rejected_before_actions(contract_tools: AgentTools) -> None:
    result = await contract_tools.execute_test_plan([{"goal": "Wrong order", "operation": "observe", "checks": [assertion("C.valid"), assertion("C.empty")]}])  # type: ignore[list-item]
    assert result["reason"] == "CHECK_DEPENDENCY_ORDER_REQUIRED"
    assert result["check_id"] == "C.valid" and result["unmet_dependencies"] == ["C.empty"]
    contract_tools.runtime.execute_page_goal.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_replan_cannot_drop_a_downstream_check_after_current_blocker(contract_tools: AgentTools) -> None:
    checks = contract_tools.runtime.required_checks
    checks.append({"check_id": "C.persisted", "behavior_id": "EB-name", "depends_on": ["C.valid"]})
    contract_tools.runtime.record_task_outcome.return_value["checks"] = [{**check, "completed": check["check_id"] == "C.empty"} for check in checks]  # type: ignore[attr-defined]
    contract_tools.plan_started = True
    context = await contract_tools.planning_context({"reason": "CONTROL_NOT_FOUND"})
    assert context["remaining_check_ids"] == ["C.valid", "C.persisted"]
    schema = contract_tools.plan_tool(context["current_page"]).parameters()
    assert len(schema["properties"]["goals"]["allOf"]) == 2
    result = await contract_tools.execute_test_plan([{"goal": "Fix only current blocker", "operation": "observe", "checks": [assertion("C.valid")]}])  # type: ignore[list-item]
    assert result["missing_check_ids"] == ["C.persisted"]
    contract_tools.runtime.execute_page_goal.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("project_reference", "valid_name"), ("row_reference", "valid_name"), ("context", "Contract Project")])
async def test_known_unavailable_object_cannot_be_used_for_an_operation(contract_tools: AgentTools, field: str, value: str) -> None:
    contract_tools._page_summary = AsyncMock(return_value={"object_bindings": [{"reference": "valid_name", "status": "unavailable"}]})  # type: ignore[method-assign]
    goal = {"goal": "Delete using stale object", "operation": "delete", "row_reference": "valid_name", field: value, "checks": [assertion("C.empty"), assertion("C.valid")]}
    result = await contract_tools.execute_test_plan([goal])  # type: ignore[list-item]
    assert result["reason"] == "OBJECT_REFERENCE_UNAVAILABLE"
    assert result["unavailable_object_references"] == ["valid_name"]
    contract_tools.runtime.execute_page_goal.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_completed_dependency_and_original_object_check_only_replan_remain_legal(contract_tools: AgentTools) -> None:
    contract_tools.runtime.record_task_outcome.return_value["checks"][0]["completed"] = True  # type: ignore[attr-defined]
    contract_tools._page_summary = AsyncMock(return_value={"object_bindings": [{"reference": "valid_name", "status": "unavailable"}]})  # type: ignore[method-assign]
    contract_tools.execute_page_goals = AsyncMock(return_value={"success": True})  # type: ignore[method-assign]
    contract_tools.finish_task = AsyncMock(return_value={"finished": True, "checks": []})  # type: ignore[method-assign]
    contract_tools.plan_started = True
    result = await contract_tools.execute_test_plan([{"goal": "Record original missing object's deviation", "operation": "observe", "run_operations": False, "row_reference": "valid_name", "checks": [assertion("C.valid")]}])  # type: ignore[list-item]
    assert result["success"]
    validated = contract_tools.store.list_events("run", event_types=("TESTER_PLAN_VALIDATED",))
    assert validated[-1]["result"]["remaining_check_ids"] == ["C.valid"]
    assert validated[-1]["result"]["planned_check_ids"] == ["C.valid"]
    contract_tools.runtime.execute_page_goal.assert_not_awaited()  # type: ignore[attr-defined]


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
    goal: dict[str, Any] = {"goal": "Submit the supplied name", "operation": "create", "inputs": [{"control": "Project name", "value_reference": "valid_name"}], "checks": [assertion("C.empty"), assertion("C.valid")]}
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
    goal = {"goal": "Register using the supplied inputs", "operation": "create", "inputs": [{"control": "Display name", "value_reference": "valid_name"}], "checks": [assertion("C.empty"), assertion("C.valid")]}
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


def test_entity_name_is_not_an_action_button_assertion_alias() -> None:
    button = InteractiveElement(kind="button", label="Add Member", target="#add-member")
    assert not WebTestingRuntime._field_matches(button, "Member")
    assert WebTestingRuntime._field_matches(button, "Add Member")
    assert WebTestingRuntime._field_matches(InteractiveElement(kind="select", label="Member project", target="#project"), "Project")


@pytest.mark.asyncio
@pytest.mark.parametrize("replan", [False, True])
async def test_explicit_operation_destination_is_executed_after_existing_preparation(contract_tools: AgentTools, replan: bool) -> None:
    contract_tools.plan_started = replan
    contract_tools.execute_page_goals = AsyncMock(return_value={"success": False, "reason": "LOCAL_EXECUTION_MARKER"})  # type: ignore[method-assign]
    goal = {"goal": "Create in the declared destination", "operation": "create", "destination": "Projects", "before_steps": [{"action_type": "refresh"}], "checks": [assertion("C.empty"), assertion("C.valid")]}
    result = await contract_tools.execute_test_plan([goal])  # type: ignore[list-item]
    assert result["reason"] == "LOCAL_EXECUTION_MARKER"
    compiled = contract_tools.execute_page_goals.call_args.args[0][0]
    assert [step.action_type for step in compiled.before_steps] == [ActionType.REFRESH, ActionType.NAVIGATION]
    assert compiled.before_steps[-1].control == "Projects"
    assert compiled.before_steps[-1].check_id is None


def test_permission_contract_uses_assigned_checks_even_when_main_operations_are_business_descriptions(contract_tools: AgentTools) -> None:
    contract_tools.assignment = replace(contract_tools.assignment, role="member")
    contract_tools.task_context.update(required_operations=["attempt-supplied-project-delete-or-record-protection"], expected_behaviors=[{"behavior_id": "EB-name", "applies_to": "Permission/member/delete"}, {"behavior_id": "unassigned", "applies_to": "Permission/member/delete"}])
    for check in contract_tools.runtime.required_checks:
        check["description"] = "Attempt Delete and verify rejection"
    assert contract_tools._permission_delete_checks() == {"C.empty", "C.valid"}
    assert len(contract_tools.plan_tool().parameters()["properties"]["goals"]["allOf"]) == 4


def test_permission_setup_and_preservation_checks_do_not_require_repeated_delete(contract_tools: AgentTools) -> None:
    contract_tools.assignment = replace(contract_tools.assignment, role="member")
    contract_tools.task_context["expected_behaviors"] = [{"behavior_id": "EB-name", "applies_to": "Permission/member/delete"}]
    contract_tools.runtime.required_checks = [{"check_id": "prep", "behavior_id": "EB-name", "description": "Observe the provided joined project and its admin owner"}, {"check_id": "probe", "behavior_id": "EB-name", "description": "The non-owner Delete attempt is rejected or unavailable"}, {"check_id": "project", "behavior_id": "EB-name", "description": "Refresh and verify the same project remains"}, {"check_id": "task", "behavior_id": "EB-name", "description": "Reopen Tasks and verify the same task remains"}]
    assert contract_tools._permission_delete_checks() == {"probe"}
    assert len(contract_tools.plan_tool().parameters()["properties"]["goals"]["allOf"]) == 5


@pytest.mark.asyncio
async def test_initial_permission_plan_requires_the_declared_operation_but_replan_preserves_check_only_protection(contract_tools: AgentTools) -> None:
    contract_tools.assignment = replace(contract_tools.assignment, role="member")
    contract_tools.task_context.update(required_operations=["delete"], expected_behaviors=[{"behavior_id": "EB-name", "applies_to": "Permission/member/delete"}])
    for check in contract_tools.runtime.required_checks:
        check["description"] = "Attempt Delete and verify rejection"
    schema = contract_tools.plan_tool().parameters()
    assert len(schema["properties"]["goals"]["allOf"]) == 4
    contract_tools.execute_page_goals = AsyncMock(return_value={"success": False, "reason": "LOCAL_EXECUTION_MARKER"})  # type: ignore[method-assign]
    goal = {"goal": "Verify supplied forbidden object", "operation": "observe", "run_operations": False, "checks": [assertion("C.empty"), assertion("C.valid")]}
    initial = await contract_tools.execute_test_plan([goal])  # type: ignore[list-item]
    assert initial["reason"] == "PERMISSION_OPERATION_PLAN_REQUIRED"
    contract_tools.execute_page_goals.assert_not_awaited()
    goal.update(operation="delete", run_operations=True, row_reference="valid_name")
    accepted = await contract_tools.execute_test_plan([goal])  # type: ignore[list-item]
    assert accepted["reason"] == "LOCAL_EXECUTION_MARKER"
    goal.update(operation="observe", run_operations=False, row_reference=None)
    protection = await contract_tools.execute_test_plan([goal])  # type: ignore[list-item]
    assert protection["reason"] == "LOCAL_EXECUTION_MARKER"


@pytest.mark.asyncio
@pytest.mark.parametrize("replan", [False, True])
@pytest.mark.parametrize("semantic", [False, True])
async def test_missing_assertion_type_is_unambiguous_only_with_explicit_target_and_operator(contract_tools: AgentTools, replan: bool, semantic: bool) -> None:
    contract_tools.plan_started = replan
    contract_tools.execute_page_goals = AsyncMock(return_value={"success": False, "reason": "LOCAL_EXECUTION_MARKER"})  # type: ignore[method-assign]
    checks = [assertion("C.empty"), assertion("C.valid")]
    for check in checks:
        check.pop("action_type")
        check["assertion"] = "contains"
        if semantic:
            check.pop("target")
            check["control"] = "Name"
    result = await contract_tools.execute_test_plan([{"goal": "Check explicit targets", "operation": "observe", "run_operations": False, "checks": checks}])  # type: ignore[list-item]
    assert result["reason"] == "LOCAL_EXECUTION_MARKER", result
    assert all(step.action_type == ActionType.ASSERTION for step in contract_tools.execute_page_goals.call_args.args[0][0].checks)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["explicit_null", "no_target", "boundary"])
async def test_assertion_type_default_does_not_invent_missing_targets_or_boundary_actions(contract_tools: AgentTools, bad: str) -> None:
    checks = [assertion("C.empty"), assertion("C.valid")]
    checks[0]["assertion"] = "contains"
    checks[0].pop("action_type")
    goal = {"goal": "Reject ambiguous step", "operation": "observe", "run_operations": False, "checks": checks}
    if bad == "explicit_null":
        checks[0]["action_type"] = None
    elif bad == "no_target":
        checks[0].pop("target")
    else:
        goal["before_steps"] = [{"target": "#refresh", "assertion": "visible"}]
    result = await contract_tools.execute_test_plan([goal])  # type: ignore[list-item]
    assert result["reason"] == "INVALID_TEST_PLAN_SCHEMA"
    contract_tools.runtime.execute_page_goal.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_replan_failure_keeps_exact_assertion_contract_without_nested_history(contract_tools: AgentTools) -> None:
    detail = {"check_id": "C.empty", "target": None, "control": "Member", "object": {"context": "valid_name", "project_reference": None}, "assertion": "hidden"}
    contract_tools.runtime.execute_page_goal.side_effect = None  # type: ignore[attr-defined]
    contract_tools.runtime.active_project = None  # type: ignore[attr-defined]
    contract_tools.runtime.execute_page_goal.return_value = {"success": True, "actions": []}  # type: ignore[attr-defined]
    contract_tools.execute_page_steps = AsyncMock(return_value={"success": False, "reason": "CONTROL_NOT_FOUND", "failed_check": detail, "results": []})  # type: ignore[method-assign]
    await contract_tools.execute_test_plan([{"goal": "Inspect scoped check", "operation": "observe", "checks": [assertion("C.empty"), assertion("C.valid")]}])  # type: ignore[list-item]
    failure = contract_tools.task_context["latest_plan_failure"]
    context = await contract_tools.planning_context(failure)
    assert context["latest_failure"]["failed_check"] == detail
    assert context["remaining_check_ids"] == ["C.empty", "C.valid"]
    assert "failed_execution" not in json.dumps(context)


@pytest.mark.parametrize("dependent", [False, True])
def test_pending_submission_phase_uses_explicit_dependencies_without_crossing_refresh(dependent: bool) -> None:
    checks = {"pending": {"depends_on": []}, "settled": {"depends_on": ["pending"] if dependent else []}, "persisted": {"depends_on": ["settled"]}}
    settled = PageStep(ActionType.ASSERTION, target="#rows", assertion="count", expected="1", check_id="settled")
    goal = PageGoal("Pending creation", operation="create", checks=[settled], after_steps=[PageStep(ActionType.REPEAT_SUBMIT, target="#submit", check_id="pending"), PageStep(ActionType.WAIT, wait_ms=10), PageStep(ActionType.REFRESH), PageStep(ActionType.ASSERTION, target="#rows", assertion="count", expected="1", check_id="persisted")])
    AgentTools._order_pending_submission_checks(goal, checks)
    if dependent:
        assert not goal.checks
        assert [step.action_type for step in goal.after_steps] == [ActionType.REPEAT_SUBMIT, ActionType.WAIT, ActionType.ASSERTION, ActionType.REFRESH, ActionType.ASSERTION]
        assert [step.check_id for step in goal.after_steps if step.check_id] == ["pending", "settled", "persisted"]
    else:
        assert goal.checks == [settled]
        assert [step.action_type for step in goal.after_steps] == [ActionType.REPEAT_SUBMIT, ActionType.WAIT, ActionType.REFRESH, ActionType.ASSERTION]


@pytest.mark.asyncio
async def test_pending_submission_phase_is_compiled_before_initial_and_replan_validation(contract_tools: AgentTools) -> None:
    runtime = contract_tools.runtime
    runtime.required_checks[0]["action_type"] = "repeat_submit"
    contract_tools.execute_page_goals = AsyncMock(return_value={"success": False, "reason": "LOCAL_EXECUTION_MARKER"})  # type: ignore[method-assign]
    for _ in range(2):
        goal = {"goal": "Submit then check settled state", "operation": "create", "checks": [assertion("C.valid")], "after_steps": [{"action_type": "repeat_submit", "target": "#submit", "url": "http://app.test/api/projects", "check_id": "C.empty"}, {"action_type": "wait", "wait_ms": 10}]}
        result = await contract_tools.execute_test_plan([goal])  # type: ignore[list-item]
        assert result["reason"] == "LOCAL_EXECUTION_MARKER", result
        compiled = contract_tools.execute_page_goals.call_args.args[0][0]
        assert not compiled.checks
        assert [step.check_id for step in compiled.after_steps] == ["C.empty", None, "C.valid"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["DUPLICATE_CHECK_ID", "INPUT_VALUE_UNAVAILABLE", "INPUT_REFERENCE_NOT_ALLOWED", "PLAN_BOUNDARY_ACTION_REQUIRED", "PAGE_GOAL_CHECK_REQUIRES_ASSERTION", "INCOMPLETE_TEST_PLAN", "INVALID_TEST_PLAN_SCHEMA"])
async def test_invalid_whole_plan_never_enters_jev(contract_tools: AgentTools, failure: str) -> None:
    goal = {"goal": "Validate the field", "operation": "observe", "checks": [assertion("C.empty"), assertion("C.valid")]}
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
            goal = {"goal": "Validate field", "operation": "observe", "checks": [assertion("C.empty"), assertion("C.empty")]}
            return ChatResponse(messages=[Message(role="assistant", contents=[Content.from_function_call(f"plan-{self.calls}", "execute_test_plan", arguments={"goals": [goal]})])], usage_details={"input_token_count": 4, "output_token_count": 2})

    client = InvalidClient()
    runner = Runner(agent=create_tester_agent(client=client, assignment=contract_tools.assignment, tools=contract_tools), assignment=contract_tools.assignment, runtime=contract_tools.runtime, store=contract_tools.store, budget=contract_tools.runtime.budget, budget_id="budget")
    await runner.ask_tester_llm("Execute assigned Task task: validation")
    assert client.calls == 3
    assert contract_tools.runtime.budget.usage.task_replans == 2
    assert contract_tools.runtime.budget.usage.llm_calls == 3
    assert contract_tools.runtime.task_finished
    contract_tools.runtime.execute_page_goal.assert_not_awaited()  # type: ignore[attr-defined]
