from __future__ import annotations

import json
from collections.abc import Mapping
from contextlib import AsyncExitStack
from dataclasses import replace
from typing import Any

import pytest
from test_manager_workers_tracing import RecordingTraceClient
from test_web_runtime import build_runtime, make_budget

from web_testing_system import observability
from web_testing_system.agents.tester_agent import PageGoal, PageInput, PageStep
from web_testing_system.agents.tester_agent import TesterAgentTools as AgentTools
from web_testing_system.agents.tester_agent import TesterAssignment as Assignment
from web_testing_system.config import Settings
from web_testing_system.evaluation.metrics import MetricsCalculator
from web_testing_system.reporting.builder import FinalReportBuilder
from web_testing_system.reproduction.replay import ReplayStep
from web_testing_system.runtime.models import ActionType, WebAction
from web_testing_system.state import StateStore

MEMBERS_HTML = """<form id="member-form" onsubmit="event.preventDefault(); document.querySelector('#status').textContent='Added '+this.username.value+' to '+this.project.value">
<select id="project" name="project" aria-label="Member project" onchange="document.querySelector('#members').hidden=this.value!=='p2'"><option value="p1">Seed Project</option><option value="p2">Target Project</option></select>
<input id="username" name="username" placeholder="Member username"><button>Add Member</button></form>
<table id="members" hidden><thead><tr><th>Username</th><th>Name</th><th>Actions</th></tr></thead><tbody>
<tr data-username="admin"><td>admin</td><td>Admin</td><td><button onclick="document.querySelector('#status').textContent='Wrong row'">Edit</button></td></tr>
<tr data-username="member2"><td>member2</td><td id="member-name">Before</td><td><button onclick="document.querySelector('#edit-dialog').showModal()">Edit</button></td></tr>
</tbody></table><dialog id="edit-dialog"><form id="edit-form" onsubmit="event.preventDefault(); document.querySelector('#member-name').textContent=this.value.value; this.closest('dialog').close(); document.querySelector('#status').textContent='Saved p2/member2'">
<input id="edit-value" name="value" aria-label="Edit value"><button>Save</button></form></dialog><p id="status">Ready</p>"""

PROJECT_HTML = """<form id="project-form" onsubmit="event.preventDefault(); document.querySelector('#status').textContent='Wrong form'"><input aria-label="Project name"><button>Create Project</button></form>
<table id="projects"><thead><tr><th>Name</th><th>Actions</th></tr></thead><tbody><tr data-project-id="p7"><td id="project-name">Before</td><td><button onclick="document.querySelector('#edit-dialog').showModal()">Edit</button></td></tr></tbody></table>
<dialog id="edit-dialog"><form id="edit-form" onsubmit="event.preventDefault(); document.querySelector('#project-name').textContent=document.querySelector('#edit-value').value; document.querySelector('#status').textContent='Saved'; this.closest('dialog').close()"><input id="edit-value" aria-label="Edit value"><button>Save</button></form></dialog><p id="status">Ready</p>"""


class LocalJev:
    def __init__(self, labels: list[str]) -> None:
        self.labels = list(labels)
        self.states: list[Mapping[str, Any]] = []

    def predict(self, state: Mapping[str, Any], questions: Mapping[str, Any]) -> Mapping[str, Any]:
        self.states.append(state)
        label = self.labels.pop(0)
        if label == "stop":
            selected = next(item for item in state["legal_candidates"] if item["action"] == "stop_current_path")
        else:
            selected = next(item for item in state["legal_candidates"] if label in item["label"])
        return {"answers": {"next_candidate": {"choice": selected["id"], "confidence": 0.95}}}


def tools_for(runtime: Any, store: StateStore) -> AgentTools:
    assignment = Assignment(run_id="run-1", task_id="task-1", tester_id="tester-1", identity_id="identity-1", role="member", data_namespace="local", scope=("http://app.test/",), step_budget=20)
    return AgentTools(assignment=assignment, runtime=runtime, store=store)


def configure_checks(store: StateStore) -> list[dict[str, Any]]:
    checks = [{"check_id": "local.first", "goal_id": "flow", "behavior_id": "EB-flow", "identity_reference": "member", "description": "First check"}, {"check_id": "local.last", "goal_id": "flow", "behavior_id": "EB-flow", "identity_reference": "member", "description": "Last check", "depends_on": ["local.first"]}]
    store.update_task_plan(task_id="task-1", expected_status="RUNNING", goal="Flow", priority="P0", dependencies=[], parent_finding=None, data_requirements={"required_checks": checks, "expected_behavior_ids": ["EB-flow"]})
    return checks


@pytest.mark.asyncio
async def test_complete_plan_uses_observed_form_scope_and_keeps_object_assertions_separate(phase2_store: StateStore) -> None:
    configure_checks(phase2_store)
    selector = LocalJev(["Fill Project name", "Create Project", "stop"])
    runtime = build_runtime(phase2_store, make_budget(), selector)  # type: ignore[arg-type]
    runtime.input_values = {"project_name": "Provided Project"}
    runtime.expected_behavior_ids = ("EB-flow",)
    runtime.executor.identity_reference = "member"
    session = await runtime.start_session()
    page = runtime.browser_manager.get_session(session).page
    html = '<form id="other"><input aria-label="Project name"><button>Wrong submit</button></form><form id="create" onsubmit="event.preventDefault(); document.querySelector(\'#status\').textContent=this.querySelector(\'input\').value"><input name="name" aria-label="Project name"><button>Create Project</button></form><p id="status">Ready</p>'
    await page.route("http://app.test/**", lambda route: route.fulfill(body=html, content_type="text/html"))
    try:
        await runtime.execute_known_action(WebAction(ActionType.NAVIGATION, url="http://app.test/"))
        tools = tools_for(runtime, phase2_store)
        contract = await tools.planning_context()
        form = next(element["form_target"] for element in contract["current_page"]["interactive_elements"] if element["kind"] == "input" and "create" in (element["form_target"] or ""))
        checks = [PageStep(ActionType.ASSERTION, target="#status", assertion="equals", expected_reference="project_name", behavior_id="EB-flow", check_id=check["check_id"]) for check in runtime.required_checks]
        result = await tools.execute_test_plan([PageGoal("Create the supplied Project", inputs=[PageInput("Project name", "project_name")], form_context=form, checks=checks)])
        assert result["success"], result
        assert await page.locator("#other input").input_value() == ""
        assert await page.locator("#create input").input_value() == "Provided Project"
        assert result["outcome"]["success_status"] == "PASS"
        assert result["completed_check_ids"] == ["local.first", "local.last"]
        assert len(selector.states) == 3
    finally:
        await runtime.browser_manager.close()


@pytest.mark.asyncio
async def test_project_row_and_edit_field_have_separate_bindings(phase2_store: StateStore) -> None:
    selector = LocalJev(["Select Member project", "Edit", "Fill Edit value", "Save", "stop"])
    runtime = build_runtime(phase2_store, make_budget(), selector)  # type: ignore[arg-type]
    runtime.input_values = {"project_name": "Target Project", "member_username": "member2", "new_name": "After"}
    session = await runtime.start_session()
    page = runtime.browser_manager.get_session(session).page
    await page.route("http://app.test/**", lambda route: route.fulfill(body=MEMBERS_HTML, content_type="text/html"))
    try:
        await runtime.execute_known_action(WebAction(ActionType.NAVIGATION, url="http://app.test/"))
        result = await runtime.execute_page_goal("Edit assigned member", project_reference="project_name", row_reference="member_username", inputs=[{"control": "Display name", "value_reference": "new_name"}])
        assert result["success"], result
        assert await page.locator("#project").input_value() == "p2"
        assert await page.locator("#member-name").inner_text() == "After"
        assert await page.locator('#members tr[data-username="admin"] td').first.inner_text() == "admin"
        assert all("admin" not in item["target"] for state in selector.states for item in state["legal_candidates"] if item["target"] and "Edit" in item["label"])
        assert "row_reference" in selector.states[1]["current_goal"]
        check = await runtime.execute_page_action(WebAction(ActionType.ASSERTION, assertion="equals", expected="After"), control="Name", context="member_username")
        assert check.action_result is not None and check.action_result.success
        await page.locator("#project").select_option("p1")
        wrong_project = await runtime.execute_page_action(WebAction(ActionType.ASSERTION, assertion="hidden"), control="Name", context="member_username")
        assert wrong_project.reason == "OBJECT_PROJECT_MISMATCH" and wrong_project.action_result is None
        raw = await runtime.execute_known_action(WebAction(ActionType.ASSERTION, target="#member-name", expected="After", project_reference="project_name"))
        assert not raw.success and raw.error_type == "OBJECT_PROJECT_MISMATCH"
        assert 'data-username="member2"' in str(check.candidate.target)  # type: ignore[union-attr]
    finally:
        await runtime.browser_manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("reference", ["project_name", "member_branch_name"])
async def test_project_context_generates_select_and_username_uses_observed_field_alias(phase2_store: StateStore, reference: str) -> None:
    selector = LocalJev(["Select Member project", "Fill Member username", "Add Member", "stop"])
    runtime = build_runtime(phase2_store, make_budget(), selector)  # type: ignore[arg-type]
    runtime.input_values = {reference: "Target Project", "member_username": "member2"}
    session = await runtime.start_session()
    page = runtime.browser_manager.get_session(session).page
    await page.route("http://app.test/**", lambda route: route.fulfill(body=MEMBERS_HTML.replace('name="username"', ''), content_type="text/html"))
    try:
        await runtime.execute_known_action(WebAction(ActionType.NAVIGATION, url="http://app.test/"))
        result = await runtime.execute_page_goal("Add supplied member", context=reference, inputs=[{"control": "Username", "value_reference": "member_username"}])
        assert result["success"], result
        assert await page.locator("#status").inner_text() == "Added member2 to p2"
        assert not any(item["action"] == "click" and "Add Member" in item["label"] for item in selector.states[0]["legal_candidates"])
        assert not any(item["action"] == "click" and "Add Member" in item["label"] for item in selector.states[1]["legal_candidates"])
        assert not any("Edit" in item["label"] for state in selector.states for item in state["legal_candidates"])
        checked = await runtime.execute_page_action(WebAction(ActionType.ASSERTION, assertion="equals", expected="Before"), control="Name", context="member_username")
        assert checked.action_result is not None and checked.action_result.success
        await page.locator("#project").select_option("p1")
        wrong = await runtime.execute_page_action(WebAction(ActionType.ASSERTION, assertion="hidden"), control="Name", context="member_username")
        assert wrong.reason == "OBJECT_PROJECT_MISMATCH"
    finally:
        await runtime.browser_manager.close()


@pytest.mark.asyncio
async def test_edit_cannot_fill_creation_form_and_assertion_retains_row_after_rename(phase2_store: StateStore) -> None:
    selector = LocalJev(["Edit", "Fill Edit value", "Save", "stop"])
    runtime = build_runtime(phase2_store, make_budget(), selector)  # type: ignore[arg-type]
    runtime.input_values = {"project_name": "Before", "renamed_project": "After"}
    session = await runtime.start_session()
    page = runtime.browser_manager.get_session(session).page
    await page.route("http://app.test/**", lambda route: route.fulfill(body=PROJECT_HTML, content_type="text/html"))
    try:
        await runtime.execute_known_action(WebAction(ActionType.NAVIGATION, url="http://app.test/"))
        result = await runtime.execute_page_goal("Rename assigned project", context="project_name", inputs=[{"control": "Project name", "value_reference": "renamed_project"}])
        assert result["success"], result
        assert not any(item["action"] == "input" for item in selector.states[0]["legal_candidates"])
        assert await page.locator("#project-form input").input_value() == ""
        check = await runtime.execute_page_action(WebAction(ActionType.ASSERTION, assertion="equals", expected="After"), control="Name", context="project_name")
        assert check.action_result is not None and check.action_result.success
        assert 'data-project-id="p7"' in str(check.candidate.target)  # type: ignore[union-attr]
        restored = build_runtime(phase2_store, runtime.budget, selector)  # type: ignore[arg-type]
        assert restored.object_targets == runtime.object_targets
        assert ("name", "project_name") in restored.assertion_targets
    finally:
        await runtime.browser_manager.close()


@pytest.mark.asyncio
async def test_same_username_in_two_projects_has_distinct_row_bindings(phase2_store: StateStore) -> None:
    selector = LocalJev(["Select Member project", "Edit", "Fill Edit value", "Save", "stop", "Edit", "Fill Edit value", "Save", "stop"])
    runtime = build_runtime(phase2_store, make_budget(), selector)  # type: ignore[arg-type]
    runtime.input_values = {"project_name": "Target Project", "seed_project": "Seed Project", "member_username": "member2", "first_name": "Target After", "second_name": "Seed After"}
    session = await runtime.start_session()
    page = runtime.browser_manager.get_session(session).page
    await page.route("http://app.test/**", lambda route: route.fulfill(body=MEMBERS_HTML, content_type="text/html"))
    try:
        await runtime.execute_known_action(WebAction(ActionType.NAVIGATION, url="http://app.test/"))
        first = await runtime.execute_page_goal("Edit target member", project_reference="project_name", row_reference="member_username", inputs=[{"control": "Display name", "value_reference": "first_name"}])
        assert first["success"], first
        await page.locator("#project").select_option("p1")
        await page.locator("#members").evaluate("table => { table.hidden=false; table.querySelector('#member-name').textContent='Seed Before'; }")
        second = await runtime.execute_page_goal("Edit seed member", project_reference="seed_project", row_reference="member_username", inputs=[{"control": "Display name", "value_reference": "second_name"}])
        assert second["success"], second
        checked = await runtime.execute_page_action(WebAction(ActionType.ASSERTION, assertion="equals", expected="Seed After"), control="Name", context="member_username", project_reference="seed_project")
        assert checked.action_result is not None and checked.action_result.success
        assert {project for (alias, _, project) in runtime.object_targets if alias == "member_username"} == {"p1", "p2"}
    finally:
        await runtime.browser_manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("reference_location", ["input", "project", "row", "expected", "boundary"])
async def test_unknown_references_rejected_before_any_browser_operation(phase2_store: StateStore, reference_location: str) -> None:
    configure_checks(phase2_store)
    selector = LocalJev([])
    runtime = build_runtime(phase2_store, make_budget(), selector)  # type: ignore[arg-type]
    runtime.expected_behavior_ids = ("EB-flow",)
    steps = [PageStep(ActionType.ASSERTION, target="#status", expected="Ready", behavior_id="EB-flow", check_id=check["check_id"]) for check in runtime.required_checks]
    goal = PageGoal("Inspect", checks=steps)
    if reference_location == "input":
        goal.inputs = [PageInput("Name", "missing")]
    elif reference_location == "project":
        goal.project_reference = "missing"
    elif reference_location == "row":
        goal.row_reference = "missing"
    elif reference_location == "expected":
        goal.checks[-1].expected_reference = "missing"
    else:
        goal.before_steps = [PageStep(ActionType.WAIT, value_reference="missing")]
    result = await tools_for(runtime, phase2_store).execute_test_plan([goal])
    assert result["reason"] == "INPUT_VALUE_UNAVAILABLE"
    assert result["unavailable_references"] == ["missing"]
    assert not phase2_store.list_action_history(run_id="run-1", task_id="task-1")
    assert not selector.states


@pytest.mark.asyncio
async def test_missing_field_never_offers_submit(phase2_store: StateStore) -> None:
    selector = LocalJev([])
    runtime = build_runtime(phase2_store, make_budget(), selector)  # type: ignore[arg-type]
    runtime.input_values = {"name": "Provided"}
    session = await runtime.start_session()
    page = runtime.browser_manager.get_session(session).page
    await page.route("http://app.test/**", lambda route: route.fulfill(body='<form><input aria-label="Actual field"><button>Save</button></form>', content_type="text/html"))
    try:
        await runtime.execute_known_action(WebAction(ActionType.NAVIGATION, url="http://app.test/"))
        result = await runtime.execute_page_goal("Save supplied data", inputs=[{"control": "Nonexistent field", "value_reference": "name"}])
        assert not result["success"] and result["pending_inputs"] == ["Nonexistent field"]
        assert not selector.states
        assert not any(action["action"] == "click" for action in phase2_store.list_action_history(run_id="run-1", task_id="task-1"))
    finally:
        await runtime.browser_manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["omitted", "partial", "changed", "ambiguous"])
async def test_submit_requires_actual_bound_inputs_and_unique_fields(phase2_store: StateStore, mode: str) -> None:
    selector = LocalJev([])
    runtime = build_runtime(phase2_store, make_budget(), selector)  # type: ignore[arg-type]
    runtime.input_values = {"name": "Provided", "other": "Second"}
    session = await runtime.start_session()
    page = runtime.browser_manager.get_session(session).page
    html = '<form id="form" onsubmit="event.preventDefault(); document.querySelector(\'#status\').textContent=\'Submitted\'"><input id="name" aria-label="Name"><input id="other" aria-label="Other"><button id="submit">Save</button></form><p id="status">Ready</p>'
    if mode == "ambiguous":
        html = html.replace('aria-label="Other"', 'aria-label="Name"')
    await page.route("http://app.test/**", lambda route: route.fulfill(body=html, content_type="text/html"))
    try:
        await runtime.execute_known_action(WebAction(ActionType.NAVIGATION, url="http://app.test/"))
        if mode == "ambiguous":
            result = await runtime.execute_page_goal("Save", inputs=[{"control": "Name", "value_reference": "name"}])
            assert result["reason"] == "AMBIGUOUS_INPUT_BINDING"
            assert not selector.states
        else:
            if mode != "omitted":
                assert (await runtime.execute_known_action(WebAction(ActionType.INPUT, target="#name", value="Provided", value_reference="name"))).success
            if mode == "changed":
                assert (await runtime.execute_known_action(WebAction(ActionType.INPUT, target="#other", value="Second", value_reference="other"))).success
                await page.locator("#name").fill("Unexpected")
            result = await runtime.execute_known_action(WebAction(ActionType.CLICK, target="#submit"))
            assert not result.success and result.error_type == "UNBOUND_REQUIRED_INPUT"
        assert await page.locator("#status").inner_text() == "Ready"
    finally:
        await runtime.browser_manager.close()


@pytest.mark.asyncio
async def test_input_reference_rejected_at_runtime_boundary(phase2_store: StateStore) -> None:
    selector = LocalJev([])
    runtime = build_runtime(phase2_store, make_budget(), selector)  # type: ignore[arg-type]
    runtime.input_values = {"name": "Provided"}
    unavailable = await runtime.execute_known_action(WebAction(ActionType.INPUT, target="#name", value="Invented", value_reference="missing"))
    mismatch = await runtime.execute_known_action(WebAction(ActionType.INPUT, target="#name", value="Invented", value_reference="name"))
    assert unavailable.error_type == "INPUT_VALUE_UNAVAILABLE"
    assert mismatch.error_type == "INPUT_REFERENCE_VALUE_MISMATCH"
    assert not phase2_store.list_action_history(run_id="run-1", task_id="task-1")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["valid_form", "missing_form", "missing_row"])
async def test_form_and_row_context_cannot_fall_back_to_unrelated_fields(phase2_store: StateStore, mode: str) -> None:
    selector = LocalJev(["Fill Name", "Save", "stop"] if mode == "valid_form" else [])
    runtime = build_runtime(phase2_store, make_budget(), selector)  # type: ignore[arg-type]
    runtime.input_values = {"name": "Provided", "row": "Absent"}
    session = await runtime.start_session()
    page = runtime.browser_manager.get_session(session).page
    html = '<form id="first" onsubmit="event.preventDefault(); document.querySelector(\'#status\').textContent=\'Saved\'"><input id="first-input" aria-label="Name"><button>Save</button></form><form id="second"><input aria-label="Name"><button>Save</button></form><p id="status">Ready</p>'
    await page.route("http://app.test/**", lambda route: route.fulfill(body=html, content_type="text/html"))
    try:
        await runtime.execute_known_action(WebAction(ActionType.NAVIGATION, url="http://app.test/"))
        result = await runtime.execute_page_goal("Save requested data", inputs=[{"control": "Name", "value_reference": "name"}], form_context="#missing" if mode == "missing_form" else "#first", row_reference="row" if mode == "missing_row" else None)
        assert result["success"] == (mode == "valid_form"), result
        assert await page.locator("#first-input").input_value() == ("Provided" if mode == "valid_form" else "")
        assert await page.locator("#second input").input_value() == ""
        if mode != "valid_form":
            assert not selector.states
    finally:
        await runtime.browser_manager.close()


@pytest.mark.asyncio
async def test_no_progress_stops_repeated_ineffective_actions_without_spending_replan(phase2_store: StateStore) -> None:
    selector = LocalJev(["First", "Second"])
    runtime = build_runtime(phase2_store, make_budget(), selector)  # type: ignore[arg-type]
    session = await runtime.start_session()
    page = runtime.browser_manager.get_session(session).page
    await page.route("http://app.test/**", lambda route: route.fulfill(body='<button>First</button><button>Second</button><button>Third</button>', content_type="text/html"))
    try:
        await runtime.execute_known_action(WebAction(ActionType.NAVIGATION, url="http://app.test/"))
        result = await runtime.execute_page_goal("Complete the requested operation")
        assert result["reason"] == "CONSECUTIVE_NO_PROGRESS"
        assert len(result["actions"]) == len(selector.states) == 2
        assert runtime.budget.usage.task_replans == 0
        assert not phase2_store.list_events("run-1", event_types=("REQUEST_REPLAN",))
    finally:
        await runtime.browser_manager.close()


@pytest.mark.asyncio
async def test_no_progress_detects_cycles_of_previously_seen_pages(phase2_store: StateStore) -> None:
    selector = LocalJev(["First", "Second", "Third"])
    runtime = build_runtime(phase2_store, make_budget(), selector)  # type: ignore[arg-type]
    session = await runtime.start_session()
    page = runtime.browser_manager.get_session(session).page
    html = '<p id="status">A</p>' + ''.join(f'<button onclick="document.querySelector(\'#status\').textContent=document.querySelector(\'#status\').textContent===\'A\'?\'B\':\'A\'">{label}</button>' for label in ("First", "Second", "Third"))
    await page.route("http://app.test/**", lambda route: route.fulfill(body=html, content_type="text/html"))
    try:
        await runtime.execute_known_action(WebAction(ActionType.NAVIGATION, url="http://app.test/"))
        result = await runtime.execute_page_goal("Complete the requested operation")
        assert result["reason"] == "CONSECUTIVE_NO_PROGRESS" and len(result["actions"]) == 3
        assert runtime.budget.usage.task_replans == 0
    finally:
        await runtime.browser_manager.close()


@pytest.mark.asyncio
async def test_select_requires_unique_scenario_option_and_page_freshness(phase2_store: StateStore) -> None:
    selector = LocalJev(["Select Member project"])
    runtime = build_runtime(phase2_store, make_budget(), selector)  # type: ignore[arg-type]
    runtime.input_values = {"project_name": "Target Project", "missing": "Absent", "duplicate": "Same"}
    session = await runtime.start_session()
    page = runtime.browser_manager.get_session(session).page
    html = '<select id="project" aria-label="Member project"><option value="p1">Seed</option><option value="p2">Target Project</option><option value="p3">Same</option><option value="p4">Same</option></select>'
    await page.route("http://app.test/**", lambda route: route.fulfill(body=html, content_type="text/html"))
    try:
        await runtime.execute_known_action(WebAction(ActionType.NAVIGATION, url="http://app.test/"))
        absent = await runtime.execute_known_action(WebAction(ActionType.SELECT, target="#project", value="Absent", value_reference="missing"))
        ambiguous = await runtime.execute_known_action(WebAction(ActionType.SELECT, target="#project", value="Same", value_reference="duplicate"))
        assert absent.error_type == "SELECT_OPTION_UNAVAILABLE"
        assert ambiguous.error_type == "AMBIGUOUS_SELECT_OPTION"
        select = runtime.jev_selector.select

        async def change_project(**kwargs: Any) -> Any:
            selection = await select(**kwargs)
            await page.locator("#project").select_option("p2")
            return selection

        monkey_select = runtime.jev_selector
        monkey_select.select = change_project  # type: ignore[method-assign]
        expired = await runtime.execute_page_goal("Select assigned project", project_reference="project_name", max_steps=1)
        assert expired["reason"] == "CANDIDATE_EXPIRED"
        assert not any(history["action"] == "select" for history in phase2_store.list_action_history(run_id="run-1", task_id="task-1"))
    finally:
        await runtime.browser_manager.close()


def test_replay_preserves_project_reference_and_rejects_missing_data() -> None:
    step = ReplayStep(ActionType.ASSERTION, target="#name", project_reference="project_name", check_id="local.check")
    assert step.to_record()["project_reference"] == "project_name"
    assert step.to_action({"project_name": "Provided"}).project_reference == "project_name"
    with pytest.raises(ValueError, match="missing replay project reference"):
        step.to_action({})


@pytest.mark.asyncio
async def test_replan_restores_remaining_check_ids_and_shared_final_score(phase2_store: StateStore, monkeypatch: pytest.MonkeyPatch) -> None:
    backend = RecordingTraceClient()
    monkeypatch.setattr(observability, "Client", lambda **kwargs: backend)
    traces = AsyncExitStack()
    configure_checks(phase2_store)
    selector = LocalJev(["stop"])
    runtime = build_runtime(phase2_store, make_budget(), selector)  # type: ignore[arg-type]
    runtime.expected_behavior_ids = ("EB-flow",)
    runtime.executor.identity_reference = "member"
    session = await runtime.start_session()
    page = runtime.browser_manager.get_session(session).page
    await page.route("http://app.test/**", lambda route: route.fulfill(body='<p id="status">Ready</p>', content_type="text/html"))
    first = PageStep(ActionType.ASSERTION, target="#status", expected="Ready", behavior_id="EB-flow", check_id="local.first")
    last = replace(first, check_id="local.last")
    try:
        await traces.enter_async_context(observability.trace_run(Settings(_env_file=None, langsmith_tracing=True, langsmith_api_key="local-trace-secret"), "run-1", scenario_id="local-correctness", secrets=("private-input",)))
        traces.enter_context(observability.trace_span("Tester", metadata={"task_id": "task-1"}))
        await runtime.execute_known_action(WebAction(ActionType.NAVIGATION, url="http://app.test/"))
        tools = tools_for(runtime, phase2_store)
        blocked = await tools.execute_test_plan([PageGoal("Original wording", checks=[first], after_steps=[PageStep(ActionType.WAIT, target="#missing"), last])])
        assert blocked["reason"] == "PAGE_TIMEOUT"
        assert blocked["completed_check_ids"] == ["local.first"]
        assert blocked["remaining_check_ids"] == ["local.last"]
        assert (await runtime.record_task_outcome())["check_completion"] == "1/2 checks completed"
        await page.locator("body").evaluate("element => element.insertAdjacentHTML('beforeend', '<p id=missing>Recovered</p>')")
        restored_tools = tools_for(runtime, phase2_store)
        completed = await restored_tools.execute_test_plan([PageGoal("Completely different wording", checks=[first, last])])
        assert completed["success"], completed
        assert completed["completed_check_ids"] == ["local.first", "local.last"]
        assert len(selector.states) == 1
        history = phase2_store.list_action_history(run_id="run-1", task_id="task-1")
        assert sum(item["action_data"].get("check_id") == "local.first" for item in history) == 1
        phase2_store.update_task_status(task_id="task-1", expected_status="RUNNING", new_status="COMPLETED")
        report = FinalReportBuilder(phase2_store).build("run-1")
        assert report["task_outcomes"] == MetricsCalculator(phase2_store).task_results("run-1")
        assert completed["outcome"]["success_status"] == report["task_outcomes"][0]["success_status"] == "PASS"
        assert all(error["status"] == "RECOVERED" for error in report["task_outcomes"][0]["errors"])
        assert len(report["task_outcomes"][0]["errors"]) == 2
        assert restored_tools.plan_started and runtime.budget.usage.task_replans == 1
    finally:
        await traces.aclose()
        await runtime.browser_manager.close()
    spans = {span["id"]: span for span in backend.created}
    assert len([span for span in spans.values() if span["name"] == "Run"]) == 1
    initial = next(span for span in spans.values() if span["name"] == "TesterInitialPlan")
    replan = next(span for span in spans.values() if span["name"] == "TesterExceptionReplan")
    assert spans[initial["parent_run_id"]]["name"] == spans[replan["parent_run_id"]]["name"] == "Tester"
    initial_result = next(span for span in backend.updated if str(span.get("run_id")) == str(initial["id"]))
    assert initial_result["extra"]["metadata"]["failure_reason"] == "PAGE_TIMEOUT"
    assert any(span["name"] == "Jev" and spans[span["parent_run_id"]]["parent_run_id"] == initial["id"] for span in spans.values())
    replan_goal = next(span for span in spans.values() if span["name"] == "PageGoal" and span["parent_run_id"] == replan["id"])
    assert any(span["name"] == "BrowserAction" and span["parent_run_id"] == replan_goal["id"] and span["extra"]["metadata"]["check_id"] == "local.last" for span in spans.values())
    assert all(span.get("parent_run_id") in spans for span in spans.values() if span.get("parent_run_id"))
    assert all(span["extra"]["metadata"]["case_id"] == "local-correctness" for span in spans.values())
    payload = json.dumps([backend.created, backend.updated], default=str)
    assert "local-trace-secret" not in payload and "private-input" not in payload
