from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from test_bounded_decisions import ChoiceClient, form_runtime
from test_correctness_bindings import configure_checks, tools_for

from web_testing_system.agents.tester_agent import PageGoal, PageInput, PageStep
from web_testing_system.runtime.candidates import CandidateBuilder
from web_testing_system.runtime.models import ActionCandidate, ActionType
from web_testing_system.state import StateStore


class NoDecision(ChoiceClient):
    def predict(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("A uniquely bound action must not invoke Jev")


def test_equivalent_candidates_are_deduplicated_without_relying_on_id() -> None:
    first = ActionCandidate("first", "input", "Username", "#username", "current", value="member", value_reference="username")
    alias = replace(first, candidate_id="alias", label="Fill username")
    assert CandidateBuilder.deduplicate([first, alias, first]) == [first]


@pytest.mark.parametrize("difference", [{"value": "other"}, {"value_reference": "another_reference"}, {"check_id": "another_check"}, {"project_reference": "another_project"}, {"requires_confirmation": True}, {"state_id": "expired"}])
def test_candidate_dedup_preserves_independent_action_contracts(difference: dict[str, Any]) -> None:
    first = ActionCandidate("first", "input", "Username", "#username", "current", value="member", value_reference="username")
    second = replace(first, candidate_id="second", **difference)
    assert CandidateBuilder.deduplicate([first, second]) == [first, second]


def test_conflicting_candidate_id_is_still_rejected() -> None:
    first = ActionCandidate("same", "input", "Username", "#username", "current", value="member", value_reference="username")
    with pytest.raises(ValueError, match="CONFLICTING_CANDIDATE_ID"):
        CandidateBuilder.deduplicate([first, replace(first, value="other")])


@pytest.mark.asyncio
@pytest.mark.parametrize("duplicate", [False, True])
@pytest.mark.parametrize("rerender", [False, True])
@pytest.mark.parametrize("candidate_limit", [1, 8])
async def test_d02_registration_has_one_current_field_and_never_calls_jev(phase2_store: StateStore, duplicate: bool, rerender: bool, candidate_limit: int) -> None:
    change = "oninput=\"document.querySelector('#register-name').id='fresh-name'\"" if rerender else ""
    html = '<form id="login-form"><input aria-label="Login username"><input aria-label="Login password"><button>Login</button></form>'
    html += f'<form id="register-form" onsubmit="event.preventDefault(); document.querySelector(\'#status\').textContent=\'Registered\'"><input id="register-username" aria-label="Register username" {change}><input id="register-name" aria-label="Display name"><input id="register-password" type="password" aria-label="Register password"><button>Register</button></form><p id="status">Ready</p>'
    runtime = await form_runtime(phase2_store, NoDecision(), html)
    runtime.candidate_builder.max_candidates = candidate_limit
    runtime.input_values = {"new_member_username": "new-member", "new_member_display_name": "New Member", "env:LOCAL_PASSWORD": "local-only-secret"}
    inputs = [{"control": "Register username", "value_reference": "new_member_username"}, {"control": "Display name", "value_reference": "new_member_display_name"}, {"control": "Register password", "value_reference": "env:LOCAL_PASSWORD"}]
    if duplicate:
        inputs.insert(1, dict(inputs[0]))
    page = runtime.browser_manager.get_session(runtime.browser_session_id).page
    try:
        result = await runtime.execute_page_goal("Register the supplied account", operation="create", form_context='[id="register-form"]', inputs=inputs)
        assert result["success"], result
        assert [action["action"] for action in result["actions"]] == ["input", "input", "input", "click"]
        assert [action["value_reference"] for action in result["actions"][:3]] == ["new_member_username", "new_member_display_name", "env:LOCAL_PASSWORD"]
        assert await page.locator("#status").inner_text() == "Registered"
        assert await page.locator("#login-form input").nth(0).input_value() == ""
        assert await page.locator("#login-form input").nth(1).input_value() == ""
        assert await page.locator("#fresh-name" if rerender else "#register-name").input_value() == "New Member"
        events = phase2_store.list_events("run-1", event_types=("CANDIDATE_SET",))
        assert len(events) == 4
        assert all(len(event["result"]["candidate_ids"]) == 1 for event in events)
        assert runtime.budget.usage.jev_calls == 0 and runtime.budget.usage.task_replans == 0
    finally:
        await runtime.browser_manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("confidence", [.95, .1])
async def test_multiple_legal_submits_keep_bounded_choice_and_strict_confidence(phase2_store: StateStore, confidence: float) -> None:
    client = ChoiceClient(confidence)
    html = '<form onsubmit="event.preventDefault(); document.querySelector(\'#status\').textContent=\'Submitted\'"><input aria-label="Name"><input aria-label="Note"><button>Create A</button><button>Create B</button></form><p id="status">Ready</p>'
    runtime = await form_runtime(phase2_store, client, html)
    page = runtime.browser_manager.get_session(runtime.browser_session_id).page
    try:
        result = await runtime.execute_page_goal("Create supplied record", operation="create", inputs=[{"control": "Name", "value_reference": "name"}, {"control": "Note", "value_reference": "note"}])
        assert len(client.states) == 1
        choices = client.states[0]["legal_candidates"]
        assert len(choices) == 2 and all(choice["action"] == "click" for choice in choices)
        if confidence < .8:
            assert not result["success"] and result["reason"] == "LOW_JEV_CONFIDENCE"
            assert await page.locator("#status").inner_text() == "Ready"
            assert not any(action["action"] == "click" for action in result["actions"])
        else:
            assert result["success"], result
            assert await page.locator("#status").inner_text() == "Submitted"
        assert runtime.budget.usage.task_replans == 0
    finally:
        await runtime.browser_manager.close()


PROJECTS = '<form id="create" onsubmit="event.preventDefault(); document.querySelector(\'#rows\').insertAdjacentHTML(\'beforeend\',\'<tr data-project-id=&quot;new&quot;><td>\'+this.querySelector(\'input\').value+\'</td></tr>\')"><input aria-label="Project name"><button>Create Project</button></form><table id="projects"><thead><tr><th>Name</th></tr></thead><tbody id="rows"><tr data-project-id="one"><td>Other One</td></tr><tr data-project-id="two"><td>Other Two</td></tr></tbody></table>'


@pytest.mark.asyncio
@pytest.mark.parametrize("form_scope", [False, True])
async def test_d03_auxiliary_name_assertion_binds_created_object_and_preserves_checks(phase2_store: StateStore, form_scope: bool) -> None:
    checks = configure_checks(phase2_store)
    runtime = await form_runtime(phase2_store, NoDecision(), PROJECTS)
    runtime.expected_behavior_ids = ("EB-flow",)
    runtime.executor.identity_reference = "member"
    runtime.input_values["task_branch_name"] = "d03-task-branch"
    tools = tools_for(runtime, phase2_store)
    auxiliary = PageStep(ActionType.ASSERTION, control="Name", expected_reference="task_branch_name")
    required = [PageStep(ActionType.ASSERTION, control="Name", expected_reference="task_branch_name", check_id=check["check_id"], behavior_id="EB-flow") for check in checks]
    try:
        goal = PageGoal("Create the task branch", operation="create", form_context='[id="create"]' if form_scope else "", inputs=[PageInput("Project name", "task_branch_name")], checks=[auxiliary, *required])
        result = await tools.execute_test_plan([goal])
        assert result["success"], result
        assert result["completed_check_ids"] == ["local.first", "local.last"]
        actions = phase2_store.list_action_history(run_id="run-1", task_id="task-1")
        assertions = [action for action in actions if action["action"] == "assertion"]
        assert len(assertions) == 3 and all('data-project-id="new"' in action["target"] for action in assertions)
        assert {action["action_data"]["check_id"] for action in assertions} == {None, "local.first", "local.last"}
        assert not phase2_store.list_events("run-1", event_types=("TESTER_PLAN_FAILED",))
        assert runtime.budget.usage.jev_calls == 0 and runtime.budget.usage.task_replans == 0
    finally:
        await runtime.browser_manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["plan", "page_goals"])
@pytest.mark.parametrize("form_scope", [False, True])
async def test_unscoped_later_assertion_is_rejected_before_earlier_mutations(phase2_store: StateStore, entry: str, form_scope: bool) -> None:
    runtime = await form_runtime(phase2_store, NoDecision(), PROJECTS)
    page = runtime.browser_manager.get_session(runtime.browser_session_id).page
    tools = tools_for(runtime, phase2_store)
    goals = [PageGoal("Create the supplied Project", operation="create", inputs=[PageInput("Project name", "name")]), PageGoal("Inspect an unspecified Name", operation="observe", form_context='[id="create"]' if form_scope else "", run_operations=False, checks=[PageStep(ActionType.ASSERTION, control="Name", expected="Provided")])]
    try:
        before = len(phase2_store.list_action_history(run_id="run-1", task_id="task-1"))
        result = await (tools.execute_test_plan(goals) if entry == "plan" else tools.execute_page_goals(goals))
        assert not result["success"] and result["reason"] == "ASSERTION_OBJECT_REQUIRED", result
        assert len(phase2_store.list_action_history(run_id="run-1", task_id="task-1")) == before
        assert await page.locator("#create input").input_value() == ""
        assert await page.locator("#rows tr").count() == 2
        event = phase2_store.list_events("run-1", event_types=("ASSERTION_CONTRACT_FAILED",))[-1]
        assert event["result"]["control"] == "Name" and event["result"]["target"] is None
    finally:
        await runtime.browser_manager.close()


@pytest.mark.asyncio
async def test_d03_scope_does_not_choose_another_object_to_make_expected_text_pass(phase2_store: StateStore) -> None:
    runtime = await form_runtime(phase2_store, NoDecision(), PROJECTS)
    tools = tools_for(runtime, phase2_store)
    try:
        goal = PageGoal("Create the supplied Project", operation="create", inputs=[PageInput("Project name", "name")], checks=[PageStep(ActionType.ASSERTION, control="Name", expected="Other One")])
        result = await tools.execute_page_goals([goal])
        assert not result["success"] and result["reason"] == "ASSERTION_FAILURE", result
        last = phase2_store.list_action_history(run_id="run-1", task_id="task-1")[-1]
        assert 'data-project-id="new"' in last["target"]
        assert last["action_data"]["expected"] == "Other One"
        assert not phase2_store.list_recent_findings("run-1")
    finally:
        await runtime.browser_manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_target", [False, True])
async def test_explicit_object_scope_is_preserved_with_same_name_rows(phase2_store: StateStore, explicit_target: bool) -> None:
    runtime = await form_runtime(phase2_store, NoDecision(), PROJECTS.replace("Other Two", "Other One"))
    tools = tools_for(runtime, phase2_store)
    try:
        goal = PageGoal("Inspect the explicit second object", operation="observe", run_operations=False, checks=[PageStep(ActionType.ASSERTION, control="Name", target='[id="projects"] tr[data-project-id="two"] td:nth-child(1)' if explicit_target else None, context="" if explicit_target else '[id="projects"] tr[data-project-id="two"]', expected="Other One")])
        result = await tools.execute_page_goals([goal])
        assert result["success"], result
        last = phase2_store.list_action_history(run_id="run-1", task_id="task-1")[-1]
        assert 'data-project-id="two"' in last["target"]
    finally:
        await runtime.browser_manager.close()
