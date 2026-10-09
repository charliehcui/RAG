from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

import pytest
from agent_framework import BaseChatClient, ChatResponse, Content, Message
from agent_framework._tools import FunctionInvocationLayer
from test_correctness_bindings import configure_checks, tools_for
from test_web_runtime import build_runtime, make_budget

from web_testing_system.agents.tester_agent import TesterRunner as Runner
from web_testing_system.agents.tester_agent import create_tester_agent
from web_testing_system.runtime.models import ActionType, WebAction
from web_testing_system.state import StateStore

FORM = '<form id="create" onsubmit="event.preventDefault(); document.querySelector(\'#status\').textContent=this.name.value"><input id="name" name="name" aria-label="Name"><input id="note" aria-label="Note"><button>Create</button></form><button>Logout</button><p id="status">Ready</p>'


class ChoiceClient:
    def __init__(self, confidence: float = .95) -> None:
        self.confidence = confidence
        self.states: list[Mapping[str, Any]] = []

    def predict(self, state: Mapping[str, Any], questions: Mapping[str, Any]) -> Mapping[str, Any]:
        self.states.append(state)
        candidates = state["legal_candidates"]
        assert all(item["action"] not in {"stop_current_path", "request_replan"} for item in candidates)
        assert questions["next_candidate"]["type"] == "choice"
        assert set(questions["next_candidate"]["criteria"]) == {"none", *[item["id"] for item in candidates]}
        assert "interactive_elements" not in state["page_state"]
        assert "Already executed" not in state["current_goal"]
        return {"answers": {"next_candidate": {"choice": candidates[0]["id"], "confidence": self.confidence}}}


async def form_runtime(store: StateStore, client: ChoiceClient, html: str = FORM) -> Any:
    runtime = build_runtime(store, make_budget(), client)  # type: ignore[arg-type]
    runtime.input_values = {"name": "Provided", "note": "Note provided"}
    session = await runtime.start_session()
    page = runtime.browser_manager.get_session(session).page
    await page.route("http://app.test/**", lambda route: route.fulfill(body=html, content_type="text/html"))
    await runtime.execute_known_action(WebAction(ActionType.NAVIGATION, url="http://app.test/"))
    return runtime


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["LOW_JEV_CONFIDENCE", "CANDIDATE_EXPIRED"])
async def test_changed_page_regenerates_candidates_and_recovers_before_tester(phase2_store: StateStore, failure: str) -> None:
    client = ChoiceClient(.1 if failure == "LOW_JEV_CONFIDENCE" else .95)
    runtime = await form_runtime(phase2_store, client)
    page = runtime.browser_manager.get_session(runtime.browser_session_id).page
    original = runtime.jev_selector.select

    async def change_once(**kwargs: Any) -> Any:
        result = await original(**kwargs)
        if len(client.states) == 1:
            await page.locator("#name").evaluate("element => element.id='new-name'")
            client.confidence = .95
        return result

    runtime.jev_selector.select = change_once
    try:
        result = await runtime.execute_page_goal("Create supplied record", operation="create", inputs=[{"control": "Name", "value_reference": "name"}, {"control": "Note", "value_reference": "note"}])
        assert result["success"], result
        assert result["reason"] == "OPERATION_EXECUTED_AND_OBSERVED"
        assert runtime.budget.usage.task_replans == 0
        assert len(client.states) == 2
        assert client.states[0]["page_state"]["state_id"] != client.states[1]["page_state"]["state_id"]
        history = phase2_store.list_action_history(run_id="run-1", task_id="task-1")
        assert not any(action["action"] == "input" and action["target"] == '[id="name"]' for action in history)
        assert [action["action"] for action in result["actions"]] == ["input", "input", "click"]
        assert phase2_store.list_events("run-1", event_types=("RUNTIME_RECOVERY",))[0]["result"]["reason"] == failure
    finally:
        await runtime.browser_manager.close()


@pytest.mark.asyncio
async def test_replan_context_reobserves_deleted_project_without_losing_original_assertion_targets(phase2_store: StateStore) -> None:
    client = ChoiceClient()
    html = '<select id="project" aria-label="Task project"><option value="p1">Provided</option></select><table id="projects"><thead><tr><th>Name</th></tr></thead><tbody><tr data-project-id="p1"><td>Provided</td></tr></tbody></table>'
    runtime = await form_runtime(phase2_store, client, html)
    page = runtime.browser_manager.get_session(runtime.browser_session_id).page
    try:
        runtime.active_project = {"reference": "name", "target": '[id="project"]', "value": "p1"}
        rows = await runtime.page_state_reader.read_rows(page)
        runtime._bind_rows(rows, "name")
        runtime.remember_assertion_targets(rows, "name")
        target = next(row["target"] for row in rows if "data-project-id" in row["target"])
        runtime.current_object = {"project_reference": "name", "row_reference": "name", "selected_project": dict(runtime.active_project), "row_targets": [target]}
        original_targets = deepcopy(runtime.assertion_targets)
        tools = tools_for(runtime, phase2_store)
        before = await tools.planning_context()
        await page.evaluate("document.querySelector('tr[data-project-id]').remove(); document.querySelector('option').remove()")
        action_count = len(phase2_store.list_action_history(run_id="run-1", task_id="task-1"))
        after = await tools.planning_context({"reason": "PROJECT_BINDING_UNAVAILABLE"})
        assert before["current_page"]["state_id"] != after["current_page"]["state_id"]
        assert after["current_object"]["selected_project"] is None
        assert after["current_object"]["project_reference"] is None
        assert after["current_object"]["row_targets"] == []
        assert {binding["status"] for binding in after["object_bindings"] if binding["reference"] == "name"} == {"unavailable", "not_observed"}
        assert runtime.assertion_targets == original_targets
        assert len(phase2_store.list_action_history(run_id="run-1", task_id="task-1")) == action_count
        assert not client.states
    finally:
        await runtime.browser_manager.close()


@pytest.mark.asyncio
async def test_object_snapshot_preserves_stable_rename_and_distinguishes_an_unopened_view(phase2_store: StateStore) -> None:
    client = ChoiceClient()
    runtime = await form_runtime(phase2_store, client, '<table id="records"><thead><tr><th>Name</th></tr></thead><tbody><tr data-record="one"><td>Provided</td></tr></tbody></table>')
    page = runtime.browser_manager.get_session(runtime.browser_session_id).page
    try:
        rows = await runtime.page_state_reader.read_rows(page)
        runtime._bind_rows(rows, "name")
        tools = tools_for(runtime, phase2_store)
        runtime.input_values["unrelated"] = "name"
        await page.locator("td").evaluate("element => element.textContent='Renamed'")
        renamed = await tools.planning_context()
        assert {binding["reference"] for binding in renamed["object_bindings"]} == {"name"}
        assert renamed["object_bindings"][0]["status"] == "present"
        assert renamed["object_bindings"][0]["target"] == next(row["target"] for row in rows if "data-record" in row["target"])
        await page.locator("table").evaluate("element => element.hidden=true")
        hidden = await tools.planning_context()
        assert hidden["object_bindings"][0]["status"] == "not_observed"
        assert not client.states
    finally:
        await runtime.browser_manager.close()


@pytest.mark.asyncio
async def test_disappearance_between_goals_blocks_stale_operation_then_allows_original_object_evidence(phase2_store: StateStore) -> None:
    configure_checks(phase2_store)
    client = ChoiceClient()
    html = '<button>Projects</button><table id="records"><thead><tr><th>Name</th><th>Actions</th></tr></thead><tbody><tr data-record="one"><td>Provided</td><td><button onclick="this.closest(\'tr\').remove()">Delete</button></td></tr></tbody></table>'
    runtime = await form_runtime(phase2_store, client, html)
    runtime.expected_behavior_ids = ("EB-flow",)
    runtime.executor.identity_reference = "member"
    tools = tools_for(runtime, phase2_store)
    try:
        first = {"action_type": "assertion", "control": "Name", "context": "name", "assertion": "hidden", "behavior_id": "EB-flow", "check_id": "local.first"}
        last = {**first, "check_id": "local.last", "assertion": "visible"}
        result = await tools.execute_test_plan([{"goal": "Delete original object", "operation": "delete", "row_reference": "name", "checks": [first]}, {"goal": "Reopen using the old object", "operation": "navigate", "destination": "Projects", "row_reference": "name", "checks": [last]}])  # type: ignore[list-item]
        assert result["reason"] == "OBJECT_REFERENCE_UNAVAILABLE"
        assert result["remaining_check_ids"] == ["local.last"]
        assert len(phase2_store.list_events("run-1", event_types=("PAGE_GOAL_OPERATION_COMPLETED",))) == 1
        result = await tools.execute_test_plan([{"goal": "Record the original missing object", "operation": "observe", "run_operations": False, "checks": [last]}])  # type: ignore[list-item]
        assert result["success"], result
        assert result["completed_check_ids"] == ["local.first", "local.last"]
        assert len(phase2_store.list_recent_findings("run-1")) == 1
        assert runtime.budget.usage.task_replans == 1
        assert not client.states
    finally:
        await runtime.browser_manager.close()


@pytest.mark.asyncio
async def test_unchanged_low_confidence_does_not_execute_or_repeat_useless_jev_call(phase2_store: StateStore) -> None:
    client = ChoiceClient(.1)
    runtime = await form_runtime(phase2_store, client)
    try:
        result = await runtime.execute_page_goal("Create supplied record", operation="create", inputs=[{"control": "Name", "value_reference": "name"}, {"control": "Note", "value_reference": "note"}])
        assert result["reason"] == "LOW_JEV_CONFIDENCE"
        assert not result["actions"]
        assert len(client.states) == 1
        assert len(phase2_store.list_events("run-1", event_types=("CANDIDATE_SET",))) == 2
        assert runtime.budget.usage.task_replans == 0
    finally:
        await runtime.browser_manager.close()


@pytest.mark.asyncio
async def test_missing_control_is_reobserved_and_recovers_without_unrelated_candidates(phase2_store: StateStore) -> None:
    client = ChoiceClient()
    runtime = await form_runtime(phase2_store, client)
    read = runtime.page_state_reader.read
    observations = 0

    async def missing_once(page: Any, **kwargs: Any) -> Any:
        nonlocal observations
        observations += 1
        if observations == 1:
            await page.locator("#name").evaluate("element => element.hidden=true")
            state = await read(page, **kwargs)
            await page.locator("#name").evaluate("element => element.hidden=false")
            return state
        return await read(page, **kwargs)

    runtime.page_state_reader.read = missing_once
    try:
        result = await runtime.execute_page_action(WebAction(ActionType.ASSERTION, assertion="visible"), control="Name")
        assert result.action_result is not None and result.action_result.success
        assert not client.states
        recovery = phase2_store.list_events("run-1", event_types=("RUNTIME_RECOVERY",))[0]["result"]
        assert recovery["reason"] == "CONTROL_NOT_FOUND" and recovery["recovered"]
        assert runtime.budget.usage.task_replans == 0
    finally:
        await runtime.browser_manager.close()


@pytest.mark.asyncio
async def test_observed_input_reset_triggers_existing_no_progress_guard(phase2_store: StateStore) -> None:
    client = ChoiceClient()
    runtime = await form_runtime(phase2_store, client, '<form><input id="name" aria-label="Name" oninput="this.value=\'\'"><button>Create</button></form>')
    try:
        result = await runtime.execute_page_goal("Create supplied record", operation="create", inputs=[{"control": "Name", "value_reference": "name"}])
        assert result["reason"] == "CONSECUTIVE_NO_PROGRESS"
        assert len(result["actions"]) == 2
        assert all(action["action"] == "input" for action in result["actions"])
        assert runtime.budget.usage.task_replans == 0
    finally:
        await runtime.browser_manager.close()


@pytest.mark.asyncio
async def test_navigation_observes_new_view_then_selects_bound_project_before_completion(phase2_store: StateStore) -> None:
    client = ChoiceClient()
    html = '<button onclick="document.body.innerHTML=document.querySelector(\'template\').innerHTML">Tasks</button><form><input aria-label="Name"><button>Wrong create</button></form><template><select id="project" aria-label="Task project"><option value="p1">Wrong Project</option><option value="p2">Target Project</option></select><p>Tasks</p></template>'
    runtime = await form_runtime(phase2_store, client, html)
    runtime.input_values["project_name"] = "Target Project"
    try:
        result = await runtime.execute_page_goal("Open assigned task view", operation="navigate", destination="Tasks", project_reference="project_name")
        assert result["success"], result
        page = runtime.browser_manager.get_session(runtime.browser_session_id).page
        assert await page.locator("#project").input_value() == "p2"
        assert [action["action"] for action in result["actions"]] == ["click", "select"]
        assert runtime.active_project["reference"] == "project_name"
        assert runtime.current_object["project_reference"] == "project_name"
        assert (await tools_for(runtime, phase2_store).planning_context())["current_object"]["selected_project"]["value"] == "p2"
        assert not client.states
        assert runtime.budget.usage.task_replans == 0
    finally:
        await runtime.browser_manager.close()


@pytest.mark.asyncio
async def test_assertion_without_a_stable_object_cannot_delegate_row_guessing_to_jev(phase2_store: StateStore) -> None:
    client = ChoiceClient()
    runtime = await form_runtime(phase2_store, client, '<table><thead><tr><th>Name</th></tr></thead><tbody><tr><td>Provided</td></tr><tr><td>Other</td></tr></tbody></table>')
    try:
        result = await runtime.execute_page_action(WebAction(ActionType.ASSERTION, assertion="equals", expected="Provided"), control="Name")
        assert result.reason == "AMBIGUOUS_ASSERTION_TARGET"
        assert result.action_result is None
        assert not client.states
        assert not phase2_store.list_recent_findings("run-1")
    finally:
        await runtime.browser_manager.close()


@pytest.mark.asyncio
async def test_last_allowed_action_is_observed_without_spending_an_extra_action(phase2_store: StateStore) -> None:
    client = ChoiceClient()
    runtime = await form_runtime(phase2_store, client, '<form><input id="name" aria-label="Name"><button>Create</button></form>')
    try:
        result = await runtime.execute_page_goal("Create supplied record", operation="create", inputs=[{"control": "Name", "value_reference": "name"}], max_steps=2)
        assert result["success"], result
        assert len(result["actions"]) == 2
        assert runtime.budget.usage.task_steps == 3
        assert not client.states
    finally:
        await runtime.browser_manager.close()


@pytest.mark.asyncio
async def test_one_initial_plan_executes_create_edit_delete_and_checks_without_jev_workflow_planning(phase2_store: StateStore) -> None:
    checks = configure_checks(phase2_store)
    phase2_store.update_task_plan(task_id="task-1", expected_status="RUNNING", goal="Lifecycle", priority="P0", dependencies=[], parent_finding=None, data_requirements={"required_checks": [checks[0], {**checks[1], "check_id": "local.edit"}, {**checks[1], "depends_on": ["local.edit"]}], "expected_behavior_ids": ["EB-flow"]})
    client = ChoiceClient()
    html = '''<script>function createRecord(form) {
        const row = document.createElement('tr'); row.dataset.record = 'one';
        const cell = document.createElement('td'); cell.id = 'value'; cell.textContent = form.querySelector('input').value;
        const actions = document.createElement('td');
        const edit = document.createElement('button'); edit.textContent = 'Edit'; edit.onclick = () => document.querySelector('dialog').showModal();
        const remove = document.createElement('button'); remove.textContent = 'Delete'; remove.onclick = () => row.remove();
        actions.append(edit, remove); row.append(cell, actions); document.querySelector('#rows').append(row);
    }</script><form id="create" onsubmit="event.preventDefault(); createRecord(this)"><input id="name" aria-label="Name"><button>Create</button></form><table id="records"><thead><tr><th>Name</th><th>Actions</th></tr></thead><tbody id="rows"></tbody></table><dialog><form onsubmit="event.preventDefault(); document.querySelector('#value').textContent=this.querySelector('input').value; this.closest('dialog').close()"><input aria-label="Edit value"><button>Save</button></form></dialog>'''
    runtime = await form_runtime(phase2_store, client, html)
    runtime.input_values["new_name"] = "Edited"
    runtime.expected_behavior_ids = ("EB-flow",)
    runtime.executor.identity_reference = "member"
    tools = tools_for(runtime, phase2_store)

    class PlanClient(FunctionInvocationLayer, BaseChatClient):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def _inner_get_response(self, *, messages: Sequence[Message], stream: bool, options: Mapping[str, Any], **kwargs: Any) -> ChatResponse:
            self.calls += 1
            assert self.calls == 1, json.loads(messages[0].text.split("Task Contract: ", 1)[1])["latest_failure"]
            goals = []
            for operation, reference, check_id in [("create", "name", "local.first"), ("edit", "new_name", "local.edit"), ("delete", None, "local.last")]:
                goal = {"goal": operation + " the supplied record", "operation": operation, "inputs": [{"control": "Name", "value_reference": reference}] if reference else [], "row_reference": "name" if operation != "create" else None, "checks": [{"action_type": "assertion", "control": "Name", "context": "name", "assertion": "equals" if reference else "hidden", "expected_reference": reference, "behavior_id": "EB-flow", "check_id": check_id}]}
                goals.append(goal)
            return ChatResponse(messages=[Message(role="assistant", contents=[Content.from_function_call("plan", "execute_test_plan", arguments={"goals": goals})])])

    planner = PlanClient()
    runner = Runner(agent=create_tester_agent(client=planner, assignment=tools.assignment, tools=tools), assignment=tools.assignment, runtime=runtime, store=phase2_store, budget=runtime.budget, budget_id="budget-1")
    try:
        await runner.ask_tester_llm("Execute assigned Task task-1: complete supplied record lifecycle")
        outcome = phase2_store.get_task("task-1")
        assert outcome is not None and outcome["success_status"] == "PASS"
        assert planner.calls == 1 and runtime.budget.usage.task_replans == 0
        assert not client.states
        assert len(phase2_store.list_events("run-1", event_types=("PAGE_GOAL_OPERATION_COMPLETED",))) == 3
        assert len(outcome["assertion_results"]) == 3
        assert not phase2_store.list_recent_findings("run-1")
        assert "stop_current_path" not in json.dumps(phase2_store.list_events("run-1", event_types=("CANDIDATE_SET",)))
    finally:
        await runtime.browser_manager.close()
