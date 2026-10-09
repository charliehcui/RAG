from __future__ import annotations

import pytest
from test_bounded_decisions import ChoiceClient, form_runtime
from test_correctness_bindings import configure_checks, tools_for

from web_testing_system.agents.tester_agent import PageGoal, PageInput, PageStep
from web_testing_system.runtime.models import ActionType, BusinessAction, WebAction
from web_testing_system.state import StateStore


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["known", "unknown", "step_limit"])
async def test_runtime_restores_only_a_known_hidden_project_container_within_the_original_budget(phase2_store: StateStore, mode: str) -> None:
    html = '<button onclick="document.querySelector(\'#projects\').hidden=false;document.querySelector(\'#tasks\').hidden=true">Projects</button><button onclick="document.querySelector(\'#projects\').hidden=true;document.querySelector(\'#tasks\').hidden=false">Tasks</button><section id="projects"><table id="project-table"><thead><tr><th>Name</th><th>Owner</th><th>Actions</th></tr></thead><tbody><tr data-project-id="p1"><td>Provided</td><td>other</td><td><button onclick="this.closest(\'tr\').remove()">Delete</button></td></tr></tbody></table></section><section id="tasks" hidden><table><tr data-task-id="t1"><td>Task</td></tr></table></section>'
    client = ChoiceClient(.1)
    runtime = await form_runtime(phase2_store, client, html)
    runtime.input_values["member_username"] = "member"
    runtime.executor.identity_reference = "member"
    try:
        page = runtime.browser_manager.get_session(runtime.browser_session_id).page
        if mode != "unknown":
            runtime._bind_rows(await runtime.page_state_reader.read_rows(page), "name")
        await runtime.execute_page_goal("Open unrelated current view", operation="navigate", destination="Tasks")
        await page.locator("tbody").first.evaluate("element => element.insertAdjacentHTML('beforeend', '<tr data-project-id=p2><td>Provided</td><td>other</td><td><button>Delete</button></td></tr>')")
        result = await runtime.execute_page_goal("Delete the same original Project", operation="delete", row_reference="name", max_steps=1 if mode == "step_limit" else 12)
        if mode == "known":
            assert result["success"], result
            assert result["object_before"]["identity"] == {"data-project-id": "p1"}
            assert result["object_before"]["owner_is_actor"] is False
            assert [action["action"] for action in result["actions"]] == ["click", "click"]
            assert await page.locator("tr[data-project-id=p1]").count() == 0
        else:
            assert result["reason"] == ("PAGE_GOAL_STEP_LIMIT" if mode == "step_limit" else "CONTROL_NOT_FOUND"), result
            assert await page.locator("tr[data-project-id=p1]").count() == 1
        assert await page.locator("tr[data-project-id=p2]").count() == 1
        assert not client.states
    finally:
        await runtime.browser_manager.close()


@pytest.mark.asyncio
async def test_nonowner_probe_cannot_execute_against_an_owned_project(phase2_store: StateStore) -> None:
    configure_checks(phase2_store)
    runtime = await form_runtime(phase2_store, ChoiceClient(), '<table><thead><tr><th>Name</th><th>Owner</th><th>Actions</th></tr></thead><tbody><tr data-project-id="p1"><td>Provided</td><td>member</td><td><button onclick="this.closest(\'tr\').remove()">Delete</button></td></tr></tbody></table>')
    runtime.input_values["member_username"] = "member"
    runtime.required_checks[0]["description"] = "Non-owner Delete attempt must be rejected"
    tools = tools_for(runtime, phase2_store)
    tools.task_context["expected_behaviors"] = [{"behavior_id": "EB-flow", "applies_to": "Permission/member/delete"}]
    try:
        goal = PageGoal("Probe the non-owner boundary", operation="delete", row_reference="name", checks=[PageStep(ActionType.ASSERTION, target='tr[data-project-id="p1"]', assertion="visible", check_id="local.first", behavior_id="EB-flow")])
        result = await tools.execute_page_goals([goal])
        assert result["reason"] == "PERMISSION_SUBJECT_IS_OWNER", result
        page = runtime.browser_manager.get_session(runtime.browser_session_id).page
        assert await page.locator("tr[data-project-id=p1]").count() == 1
        assert not any(action["action"] == "click" for action in phase2_store.list_action_history(run_id="run-1", task_id="task-1"))
    finally:
        await runtime.browser_manager.close()


@pytest.mark.asyncio
async def test_project_visibility_without_a_delete_attempt_is_not_a_destructive_permission_finding(phase2_store: StateStore) -> None:
    configure_checks(phase2_store)
    runtime = await form_runtime(phase2_store, ChoiceClient(), '<table><thead><tr><th>Name</th><th>Owner</th><th>Actions</th></tr></thead><tbody><tr data-project-id="p1"><td>Provided</td><td>other</td><td><button>Delete</button></td></tr></tbody></table>')
    runtime.expected_behavior_ids = ("EB-flow",)
    runtime.required_checks[0]["description"] = "Attempt Delete and verify rejection"
    tools = tools_for(runtime, phase2_store)
    tools.task_context["expected_behaviors"] = [{"behavior_id": "EB-flow", "applies_to": "Permission/member/delete"}]
    try:
        wrong = PageStep(ActionType.ASSERTION, target='tr[data-project-id="p1"]', assertion="count", expected="0", behavior_id="EB-flow", check_id="local.first")
        result = await tools.execute_page_steps([wrong])
        assert result["reason"] == "PERMISSION_OPERATION_EVIDENCE_REQUIRED", result
        assert not phase2_store.list_recent_findings("run-1")
        page = runtime.browser_manager.get_session(runtime.browser_session_id).page
        await page.locator("button").evaluate("element => element.remove()")
        still_wrong = await tools.execute_page_steps([wrong])
        assert still_wrong["reason"] == "PERMISSION_OPERATION_EVIDENCE_REQUIRED"
        protection = await tools.execute_page_steps([PageStep(ActionType.ASSERTION, target='tr[data-project-id="p1"] button', assertion="hidden", behavior_id="EB-flow", check_id="local.first")])
        assert protection["success"], protection
        assert not phase2_store.list_recent_findings("run-1")
    finally:
        await runtime.browser_manager.close()
@pytest.mark.parametrize("explicit", [False, True])
async def test_project_selector_descriptor_binds_only_the_supplied_select(phase2_store: StateStore, explicit: bool) -> None:
    html = '<input aria-label="Project name"><select id="project" aria-label="Member project"><option value="other">Other</option><option value="provided">Provided</option></select>'
    runtime = await form_runtime(phase2_store, ChoiceClient(), html)
    try:
        result = await runtime.execute_page_goal("Observe supplied project", operation="observe", project_reference="name" if explicit else None, inputs=[{"control": "Project selector", "value_reference": "name"}])
        assert result["success"], result
        page = runtime.browser_manager.get_session(runtime.browser_session_id).page
        assert await page.locator("select").input_value() == "provided"
        assert await page.locator("input").input_value() == ""
        assert [action["action"] for action in result["actions"]] == ["select"]
    finally:
        await runtime.browser_manager.close()


@pytest.mark.asyncio
async def test_project_selector_descriptor_keeps_multiple_visible_selects_ambiguous(phase2_store: StateStore) -> None:
    html = '<select aria-label="Member project"><option>Provided</option></select><select aria-label="Task project"><option>Provided</option></select>'
    runtime = await form_runtime(phase2_store, ChoiceClient(), html)
    try:
        result = await runtime.execute_page_goal("Observe exact project", operation="observe", inputs=[{"control": "Project dropdown", "value_reference": "name"}])
        assert result["reason"] == "AMBIGUOUS_INPUT_BINDING", result
        assert not result["actions"]
    finally:
        await runtime.browser_manager.close()


@pytest.mark.asyncio
async def test_completed_check_continuation_keeps_auxiliary_precondition_before_a_later_mutation(phase2_store: StateStore) -> None:
    configure_checks(phase2_store)
    runtime = await form_runtime(phase2_store, ChoiceClient())
    runtime.expected_behavior_ids = ("EB-flow",)
    runtime.executor.identity_reference = "member"
    tools = tools_for(runtime, phase2_store)
    try:
        for check in runtime.required_checks:
            result = await runtime.execute_known_action(WebAction(ActionType.ASSERTION, target="#status", assertion="equals", expected="Ready", behavior_id="EB-flow", check_id=check["check_id"], goal_check=True))
            assert result.success
        tools.readonly_tail = True
        goals = [PageGoal("Continue after already recorded checks", operation="observe", run_operations=False, checks=[PageStep(ActionType.ASSERTION, target="#status", assertion="equals", expected="Ready", check_id=check["check_id"]) for check in runtime.required_checks], after_steps=[PageStep(ActionType.ASSERTION, target="#missing", assertion="visible")]), PageGoal("Create only after the prerequisite", operation="create", inputs=[PageInput("Name", "name"), PageInput("Note", "note")])]
        result = await tools.execute_test_plan(goals)
        assert result["reason"] == "ASSERTION_FAILURE", result
        page = runtime.browser_manager.get_session(runtime.browser_session_id).page
        assert await page.locator("#status").inner_text() == "Ready"
        assert not tools.readonly_tail and not tools.checking_readonly_tail
    finally:
        await runtime.browser_manager.close()


@pytest.mark.asyncio
async def test_project_owner_delete_is_not_rewritten_as_a_forbidden_operation(phase2_store: StateStore) -> None:
    configure_checks(phase2_store)
    runtime = await form_runtime(phase2_store, ChoiceClient(), '<table><thead><tr><th>Name</th><th>Owner</th><th>Actions</th></tr></thead><tbody><tr data-project-id="p1"><td>Provided</td><td>member</td><td><button onclick="this.closest(\'tr\').remove()">Delete</button></td></tr></tbody></table>')
    runtime.input_values["member_username"] = "member"
    runtime.expected_behavior_ids = ("EB-flow",)
    tools = tools_for(runtime, phase2_store)
    tools.task_context.update(required_operations=["delete"], expected_behaviors=[{"behavior_id": "EB-flow", "applies_to": "Permission/member/delete"}])
    try:
        goal = PageGoal("Delete own project", operation="delete", row_reference="name", checks=[PageStep(ActionType.ASSERTION, target='tr[data-project-id="p1"] button', assertion="hidden", behavior_id="EB-flow", check_id=check["check_id"]) for check in runtime.required_checks])
        result = await tools.execute_page_goals([goal])
        assert result["success"], result
        assert not phase2_store.list_events("run-1", event_types=("PERMISSION_ASSERTION_BOUND",))
        assert not phase2_store.list_recent_findings("run-1")
    finally:
        await runtime.browser_manager.close()


@pytest.mark.asyncio
async def test_entity_assertion_uses_the_same_bound_member_row_after_it_disappears(phase2_store: StateStore) -> None:
    html = '<select aria-label="Member project"><option value="provided">Provided</option></select><button>Add Member</button><table><tr data-username="member"><td>member</td></tr></table>'
    runtime = await form_runtime(phase2_store, ChoiceClient(), html)
    runtime.input_values["member_ref"] = "member"
    try:
        visible = await runtime.execute_page_action(WebAction(ActionType.ASSERTION, assertion="visible"), control="Member", context="member_ref")
        assert visible.action_result is not None and visible.action_result.success, visible
        assert 'data-username="member"' in visible.candidate.target
        page = runtime.browser_manager.get_session(runtime.browser_session_id).page
        await page.locator("tr").evaluate("row => row.dataset.username='different'")
        absent = await runtime.execute_page_action(WebAction(ActionType.ASSERTION, assertion="hidden"), control="Member", context="member_ref")
        assert absent.action_result is not None and absent.action_result.success
        assert absent.candidate.target == visible.candidate.target
        wrong_kind = await runtime.execute_page_action(WebAction(ActionType.ASSERTION, assertion="visible"), control="Project", context="member_ref")
        assert wrong_kind.action_result is None
    finally:
        await runtime.browser_manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation_after", [False, True])
async def test_auxiliary_postcondition_can_continue_required_readonly_checks_but_never_another_mutation(phase2_store: StateStore, mutation_after: bool) -> None:
    configure_checks(phase2_store)
    html = '<table><tr data-project-id="p1"><td>Provided</td><td><button onclick="this.closest(\'tr\').remove()">Delete</button></td></tr></table><form onsubmit="event.preventDefault();document.querySelector(\'#status\').textContent=\'changed\'"><input aria-label="Name"><input aria-label="Note"><button>Create</button></form><p id="status">Ready</p>'
    runtime = await form_runtime(phase2_store, ChoiceClient(), html)
    runtime.expected_behavior_ids = ("EB-flow",)
    runtime.executor.identity_reference = "member"
    tools = tools_for(runtime, phase2_store)
    target = 'tr[data-project-id="p1"]'
    checks = [PageStep(ActionType.ASSERTION, target=target, assertion="visible"), *[PageStep(ActionType.ASSERTION, target=target, assertion="count", expected="0", check_id=check["check_id"]) for check in runtime.required_checks]]
    goals = [PageGoal("Delete supplied object then observe results", operation="delete", row_reference="name", checks=checks)]
    if mutation_after:
        goals.append(PageGoal("Create after a mandatory precondition", operation="create", inputs=[PageInput("Name", "name"), PageInput("Note", "note")]))
    try:
        result = await tools.execute_test_plan(goals)
        assert result["success"] is (not mutation_after), result
        if mutation_after:
            assert result["reason"] == "ASSERTION_FAILURE"
        else:
            assert result["outcome"]["success_status"] == "PASS"
            assert len(phase2_store.list_events("run-1", event_types=("READ_ONLY_ASSERTION_CONTINUATION",))) == 1
        page = runtime.browser_manager.get_session(runtime.browser_session_id).page
        assert await page.locator("#status").inner_text() == "Ready"
    finally:
        await runtime.browser_manager.close()


@pytest.mark.asyncio
async def test_forbidden_delete_records_original_object_preservation_instead_of_missing_button(phase2_store: StateStore) -> None:
    configure_checks(phase2_store)
    runtime = await form_runtime(phase2_store, ChoiceClient(), '<table><thead><tr><th>Name</th><th>Owner</th><th>Actions</th></tr></thead><tbody><tr data-project-id="p1"><td>Provided</td><td>other</td><td><button onclick="this.closest(\'tr\').remove()">Delete</button></td></tr></tbody></table>')
    runtime.input_values["member_username"] = "member"
    runtime.expected_behavior_ids = ("EB-flow",)
    runtime.required_checks[0]["description"] = "Attempt Delete and verify rejection"
    tools = tools_for(runtime, phase2_store)
    tools.task_context.update(required_operations=["delete"], expected_behaviors=[{"behavior_id": "EB-flow", "applies_to": "Permission/member/delete"}])
    try:
        goal = PageGoal("Attempt forbidden deletion and verify denial", operation="delete", row_reference="name", checks=[PageStep(ActionType.ASSERTION, target='tr[data-project-id="p1"] button', assertion="hidden", behavior_id="EB-flow", check_id="local.first"), PageStep(ActionType.ASSERTION, target='tr[data-project-id="p1"]', assertion="count", expected="1", behavior_id="EB-flow", check_id="local.last")])
        result = await tools.execute_page_goals([goal])
        assert result["success"], result
        scored = [action for action in phase2_store.list_action_history(run_id="run-1", task_id="task-1") if action["action_data"].get("check_id")]
        assert len(scored) == 2
        assert all(action["action_data"]["assertion"] == "count" and action["action_data"]["expected"] == "1" and 'data-project-id="p1"' in action["target"] and "button" not in action["target"] and action["result"]["data"]["actual"] == 0 for action in scored)
        assert phase2_store.list_recent_findings("run-1")
    finally:
        await runtime.browser_manager.close()


@pytest.mark.asyncio
async def test_creation_inputs_are_not_filtered_as_an_existing_future_row(phase2_store: StateStore) -> None:
    html = '<form onsubmit="event.preventDefault();document.querySelector(\'#result\').textContent=this.title.value"><select aria-label="Task project"><option value="provided">Provided</option></select><input name="title" aria-label="Task title"><button>Create Task</button></form><p id="result"></p>'
    runtime = await form_runtime(phase2_store, ChoiceClient(), html)
    try:
        result = await runtime.execute_page_goal("Create the supplied future task", operation="create", project_reference="name", row_reference="note", inputs=[{"control": "Title", "value_reference": "note"}])
        assert result["success"], result
        page = runtime.browser_manager.get_session(runtime.browser_session_id).page
        assert await page.locator("#result").inner_text() == "Note provided"
    finally:
        await runtime.browser_manager.close()


@pytest.mark.asyncio
async def test_duplicate_visible_cells_of_one_stable_row_do_not_mean_two_objects(phase2_store: StateStore) -> None:
    html = '<table><tr data-username="member"><td>member</td><td>member</td></tr></table>'
    runtime = await form_runtime(phase2_store, ChoiceClient(), html)
    try:
        action = WebAction(ActionType.ASSERTION, target='td:text-is("member")', assertion="visible", timeout_ms=50)
        result = await runtime.execute_known_action(action)
        assert result.success, result
        page = runtime.browser_manager.get_session(runtime.browser_session_id).page
        await page.locator("table").evaluate("element => element.insertAdjacentHTML('beforeend', '<tr data-username=other><td>member</td></tr>')")
        ambiguous = await runtime.execute_known_action(action)
        assert not ambiguous.success and ambiguous.error_type == "INVALID_ACTION"
    finally:
        await runtime.browser_manager.close()


@pytest.mark.asyncio
async def test_raw_project_text_resolves_only_the_selected_option_in_a_unique_visible_select(phase2_store: StateStore) -> None:
    html = '<section hidden><table><tr><td>Provided</td></tr></table><select><option>Provided</option></select></section><select id="active"><option value="p1">Provided</option><option value="p2">Other</option></select>'
    runtime = await form_runtime(phase2_store, ChoiceClient(), html)
    try:
        action = WebAction(ActionType.ASSERTION, target="text=Provided", assertion="visible", timeout_ms=50)
        result = await runtime.execute_known_action(action)
        assert result.success, result
        page = runtime.browser_manager.get_session(runtime.browser_session_id).page
        await page.locator("#active").select_option("p2")
        unavailable = await runtime.execute_known_action(action)
        assert not unavailable.success
        await page.locator("#active").select_option("p1")
        await page.locator("section").evaluate("element => element.hidden=false")
        ambiguous = await runtime.execute_known_action(action)
        assert not ambiguous.success and ambiguous.error_type == "INVALID_ACTION"
    finally:
        await runtime.browser_manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("operator", ["visible", "equals", "hidden", "count"])
async def test_raw_assertion_ignores_hidden_view_copies_but_preserves_count_semantics(phase2_store: StateStore, operator: str) -> None:
    html = '<section hidden><table><tr><td>member</td></tr></table></section><section><table><tr data-member-id="m1"><td>member</td></tr></table></section>'
    runtime = await form_runtime(phase2_store, ChoiceClient(), html)
    try:
        expected = "2" if operator == "count" else "member" if operator == "equals" else None
        action = WebAction(ActionType.ASSERTION, target='td:text-is("member")', assertion=operator, expected=expected, timeout_ms=50)
        result = await runtime.execute_known_action(action)
        assert result.success is (operator != "hidden"), result
        assert result.error_type is None if result.success else result.error_type == "ASSERTION_FAILURE"
        page = runtime.browser_manager.get_session(runtime.browser_session_id).page
        await page.locator("section").first.evaluate("element => element.hidden=false")
        if operator != "count":
            ambiguous = await runtime.execute_known_action(action)
            assert ambiguous.error_type == "INVALID_ACTION"
            assert ambiguous.error == "target matches multiple objects"
    finally:
        await runtime.browser_manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit", [False, True])
async def test_duplicate_project_bindings_have_one_effect_and_no_jev_call(phase2_store: StateStore, explicit: bool) -> None:
    html = '<button onclick="document.querySelector(\'main\').hidden=false">Tasks</button><main hidden><select id="project" aria-label="Task project"><option value="other">Other</option><option value="provided">Provided</option></select></main>'
    client = ChoiceClient(.1)
    runtime = await form_runtime(phase2_store, client, html)
    try:
        result = await runtime.execute_page_goal("Open exact Project Tasks", operation="navigate", destination="Tasks", project_reference="name" if explicit else None, inputs=[{"control": "Project", "value_reference": "name"}])
        assert result["success"], result
        assert [action["action"] for action in result["actions"]] == ["click", "select"]
        assert not client.states
        sets = phase2_store.list_events("run-1", event_types=("CANDIDATE_SET",))
        assert all(len(event["result"]["candidate_ids"]) == len(set(event["result"]["candidate_ids"])) for event in sets)
        assert runtime.active_project["value"] == "provided"
    finally:
        await runtime.browser_manager.close()


@pytest.mark.asyncio
async def test_conflicting_project_input_is_rejected_without_selection(phase2_store: StateStore) -> None:
    runtime = await form_runtime(phase2_store, ChoiceClient(), '<select aria-label="Task project"><option value="other">Other</option><option value="provided">Provided</option><option value="note">Note provided</option></select>')
    try:
        result = await runtime.execute_page_goal("Observe selected Project", operation="observe", project_reference="name", inputs=[{"control": "Project", "value_reference": "note"}])
        assert result["reason"] == "CONFLICTING_PROJECT_INPUT"
        assert not result["actions"]
        page = runtime.browser_manager.get_session(runtime.browser_session_id).page
        state = await runtime.page_state_reader.read(page)
        action = WebAction(ActionType.SELECT, target=state.interactive_elements[0].target, value="Provided", value_reference="name")
        duplicate = runtime.candidate_builder.build(goal="Choose Project", page_state=state, include_controls=False, business_actions=[BusinessAction("First", action), BusinessAction("Same effect", action)])
        assert len({candidate.candidate_id for candidate in duplicate}) == len(duplicate)
        conflicting = WebAction(ActionType.SELECT, target=action.target, value="Other", value_reference="name")
        with pytest.raises(ValueError, match="CONFLICTING_CANDIDATE_ID"):
            runtime.candidate_builder.build(goal="Choose Project", page_state=state, include_controls=False, business_actions=[BusinessAction("First", action), BusinessAction("Conflict", conflicting)])
    finally:
        await runtime.browser_manager.close()


@pytest.mark.asyncio
async def test_task_field_alias_and_selected_option_assertion(phase2_store: StateStore) -> None:
    html = '<form onsubmit="event.preventDefault(); document.querySelector(\'#result\').textContent=this.title.value"><select aria-label="Task project"><option value="provided">Provided</option><option value="other">Other</option></select><input name="title" aria-label="Task title"><button>Create Task</button></form><p id="result"></p>'
    runtime = await form_runtime(phase2_store, ChoiceClient(), html)
    try:
        result = await runtime.execute_page_goal("Create supplied Task", operation="create", project_reference="name", inputs=[{"control": "Task name", "value_reference": "note"}, {"control": "Task project", "value_reference": "name"}])
        assert result["success"], result
        page = runtime.browser_manager.get_session(runtime.browser_session_id).page
        assert await page.locator("#result").inner_text() == "Note provided"
        for expected, passed in [("Provided", True), ("Other", False)]:
            decision = await runtime.execute_page_action(WebAction(ActionType.ASSERTION, assertion="equals", expected=expected), control="Task project")
            assert decision.action_result is not None and decision.action_result.success is passed
            assert decision.action_result.data["actual"] == "Provided"
        decision = await runtime.execute_page_action(WebAction(ActionType.ASSERTION, assertion="equals", expected="Note provided"), control="Task title")
        assert decision.action_result is not None and decision.action_result.success
    finally:
        await runtime.browser_manager.close()


@pytest.mark.asyncio
async def test_table_header_is_not_a_business_assertion_target(phase2_store: StateStore) -> None:
    runtime = await form_runtime(phase2_store, ChoiceClient(), '<input aria-label="Task title"><table><thead><tr><th>Title</th></tr></thead><tbody><tr data-task-id="t1"><td>Provided</td></tr></tbody></table>')
    try:
        decision = await runtime.execute_page_action(WebAction(ActionType.ASSERTION, assertion="equals", expected="Provided"), control="Title")
        assert decision.action_result is not None and decision.action_result.success, decision
        assert "data-task-id" in decision.candidate.target
    finally:
        await runtime.browser_manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("known", [False, True])
async def test_positive_assertion_cannot_substitute_another_projects_task_when_original_parent_is_missing(phase2_store: StateStore, known: bool) -> None:
    configure_checks(phase2_store)
    html = '<select id="project" aria-label="Task project"><option value="p2">Other</option></select><table><thead><tr><th>Title</th></tr></thead><tbody><tr data-task-id="other"><td id="other-title">Provided</td></tr></tbody></table>'
    runtime = await form_runtime(phase2_store, ChoiceClient(), html)
    if known:
        runtime._remember_project("name", '[id="project"]', "p1")
    try:
        result = await runtime.execute_page_action(WebAction(ActionType.ASSERTION, assertion="equals", expected="Provided", check_id="local.first", behavior_id="EB-flow", goal_check=True), control="Title", project_reference="name")
        if known:
            assert result.action_result is not None and not result.action_result.success
            assert result.action_result.error_type == "ASSERTION_FAILURE"
            assert result.candidate.target == '[id="project"] >> option[value="p1"]'
            raw = await runtime.execute_known_action(WebAction(ActionType.ASSERTION, target="#other-title", assertion="equals", expected="Provided", check_id="local.last", behavior_id="EB-flow", goal_check=True, project_reference="name"))
            assert not raw.success and raw.error_type == "ASSERTION_FAILURE"
            history = phase2_store.list_action_history(run_id="run-1", task_id="task-1")
            assert history[-1]["action_data"]["target"] != "#other-title"
        else:
            assert result.action_result is None and result.needs_tester_llm
        page = runtime.browser_manager.get_session(runtime.browser_session_id).page
        assert await page.locator("select").input_value() == "p2"
        assert await page.locator("#other-title").inner_text() == "Provided"
    finally:
        await runtime.browser_manager.close()


@pytest.mark.asyncio
async def test_readonly_auxiliary_failure_cannot_continue_into_a_mutation(phase2_store: StateStore) -> None:
    configure_checks(phase2_store)
    runtime = await form_runtime(phase2_store, ChoiceClient())
    runtime.expected_behavior_ids = ("EB-flow",)
    tools = tools_for(runtime, phase2_store)
    try:
        result = await tools.execute_test_plan([
            PageGoal("Observe necessary precondition", operation="observe", run_operations=False, checks=[PageStep(ActionType.ASSERTION, target="#missing", assertion="visible")]),
            PageGoal("Create only after the precondition", operation="create", inputs=[PageInput("Name", "name"), PageInput("Note", "note")], checks=[PageStep(ActionType.ASSERTION, target="#status", assertion="contains", expected="Provided", check_id=check["check_id"]) for check in runtime.required_checks])
        ])
        assert result["reason"] == "ASSERTION_FAILURE", result
        page = runtime.browser_manager.get_session(runtime.browser_session_id).page
        assert await page.locator("#status").inner_text() == "Ready"
        assert not phase2_store.list_events("run-1", event_types=("READ_ONLY_ASSERTION_CONTINUATION",))
    finally:
        await runtime.browser_manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("semantic", [False, True])
async def test_permission_control_visibility_is_not_a_confirmed_authorization_bug(phase2_store: StateStore, semantic: bool) -> None:
    html = '<table><tr data-project-id="p1"><td>Provided</td><td><button id="delete">Delete</button></td></tr></table>'
    runtime = await form_runtime(phase2_store, ChoiceClient(), html)
    tools = tools_for(runtime, phase2_store)
    tools.task_context["expected_behaviors"] = [{"behavior_id": "EB-delete", "applies_to": "Permission/member/delete"}]
    step = PageStep(ActionType.ASSERTION, target=None if semantic else "#delete", control="Delete" if semantic else None, context="name", assertion="hidden", check_id="local.delete", behavior_id="EB-delete")
    try:
        result = await tools.execute_page_steps([step])
        assert result["reason"] == "PERMISSION_OPERATION_EVIDENCE_REQUIRED", result
        assert not phase2_store.list_recent_findings("run-1")
        history = phase2_store.list_action_history(run_id="run-1", task_id="task-1")
        assert not any(action["action"] == "assertion" for action in history)
        page = runtime.browser_manager.get_session(runtime.browser_session_id).page
        await page.locator("button").evaluate("element => element.remove()")
        assert not await tools._unsupported_permission_oracle(step)
    finally:
        await runtime.browser_manager.close()
