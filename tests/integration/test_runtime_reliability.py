from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any
from unittest.mock import patch

import pytest
from test_bounded_decisions import ChoiceClient, form_runtime
from test_correctness_bindings import configure_checks, tools_for
from test_web_runtime import FakeJevClient, build_runtime, make_budget

from web_testing_system.agents.tester_agent import PageGoal, PageInput, PageStep
from web_testing_system.runtime.models import ActionType, WebAction
from web_testing_system.state import StateStore


@pytest.mark.asyncio
async def test_navigation_does_not_bind_an_unrequested_project(phase2_store: StateStore) -> None:
    html = '<button onclick="document.querySelector(\'main\').innerHTML=\'<select aria-label=&quot;Task project&quot;><option value=&quot;p2&quot;>Other</option></select>\'">Tasks</button><main>Projects</main>'
    client = ChoiceClient()
    runtime = await form_runtime(phase2_store, client, html)
    try:
        result = await runtime.execute_page_goal("Open Tasks", operation="navigate", destination="Tasks")
        assert result["success"], result
        assert [step["action"] for step in result["actions"]] == ["click"]
        assert runtime.active_project is None
        assert not client.states
    finally:
        await runtime.browser_manager.close()


@pytest.mark.asyncio
async def test_project_rebinds_by_stable_id_after_rename_and_selector_change(phase2_store: StateStore) -> None:
    html = '<select id="project" aria-label="Task project"><option value="p2">Other</option><option value="p1">Provided</option></select><table id="projects"><thead><tr><th>Name</th></tr></thead><tbody><tr data-project-id="p1"><td>Provided</td></tr></tbody></table>'
    runtime = await form_runtime(phase2_store, ChoiceClient(), html)
    page = runtime.browser_manager.get_session(runtime.browser_session_id).page
    try:
        runtime._bind_rows(await runtime.page_state_reader.read_rows(page), "name")
        await page.evaluate("document.querySelector('td').textContent='Renamed'; document.querySelector('option[value=p1]').textContent='Renamed'; document.querySelector('select').id='fresh-project'")
        result = await runtime.execute_page_goal("Observe original Project", operation="observe", project_reference="name")
        assert result["success"], result
        assert await page.locator("select").input_value() == "p1"
        assert runtime.active_project == {"reference": "name", "value": "p1", "target": '[id="fresh-project"]'}
        restored = build_runtime(phase2_store, runtime.budget, FakeJevClient())
        assert restored.project_ids["name"] == "p1"
        assert runtime.budget.usage.task_replans == 0
    finally:
        await runtime.browser_manager.close()


@pytest.mark.asyncio
async def test_project_temporarily_missing_is_reobserved_before_replan(phase2_store: StateStore) -> None:
    runtime = await form_runtime(phase2_store, ChoiceClient(), '<select id="project" aria-label="Task project"><option value="p2">Other</option><option value="p1">Provided</option></select>')
    read = runtime.page_state_reader.read
    calls = 0

    async def missing_once(page: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        state = await read(page, **kwargs)
        if calls == 1:
            return replace(state, interactive_elements=tuple(replace(element, options=(("Other", "p2"),)) for element in state.interactive_elements))
        return state

    runtime.page_state_reader.read = missing_once
    try:
        result = await runtime.execute_page_goal("Observe provided Project", operation="observe", project_reference="name")
        assert result["success"], result
        assert runtime.active_project["value"] == "p1"
        assert runtime.budget.usage.task_replans == 0
        assert phase2_store.list_events("run-1", event_types=("RUNTIME_RECOVERY_RESULT",))[-1]["result"]["recovered"]
    finally:
        await runtime.browser_manager.close()


@pytest.mark.asyncio
async def test_assertion_reobserves_temporarily_missing_project_option(phase2_store: StateStore) -> None:
    html = '<select id="project" aria-label="Task project"><option value="p1">Provided</option></select><table id="tasks"><thead><tr><th>Title</th></tr></thead><tbody><tr data-task-id="t1"><td>Task</td></tr></tbody></table>'
    runtime = await form_runtime(phase2_store, ChoiceClient(), html)
    runtime.input_values["title"] = "Task"
    read = runtime.page_state_reader.read
    calls = 0

    async def incomplete_once(page: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        state = await read(page, **kwargs)
        return replace(state, interactive_elements=tuple(replace(element, options=()) if element.kind == "select" else element for element in state.interactive_elements)) if calls == 1 else state

    runtime.page_state_reader.read = incomplete_once
    try:
        result = await runtime.execute_page_action(WebAction(ActionType.ASSERTION, assertion="visible"), control="Title", context="title", project_reference="name")
        assert result.action_result is not None and result.action_result.success
        assert phase2_store.list_events("run-1", event_types=("RUNTIME_RECOVERY",))[-1]["result"]["reason"] == "PROJECT_BINDING_UNAVAILABLE"
        assert runtime.budget.usage.task_replans == 0
    finally:
        await runtime.browser_manager.close()


@pytest.mark.asyncio
async def test_project_observation_does_not_bind_an_owner_username_as_a_project(phase2_store: StateStore) -> None:
    runtime = await form_runtime(phase2_store, ChoiceClient(), '<table id="projects"><thead><tr><th>Name</th><th>Owner</th></tr></thead><tbody><tr data-project-id="p1"><td>Provided</td><td>member</td></tr></tbody></table>')
    runtime.input_values.update(seed_project_id="p1", member_username="member")
    try:
        result = await runtime.execute_known_action(WebAction(ActionType.ASSERTION, target='tr[data-project-id="p1"]', assertion="visible"))
        assert result.success
        assert runtime.project_ids["seed_project_id"] == "p1"
        assert "member_username" not in runtime.project_ids
    finally:
        await runtime.browser_manager.close()


@pytest.mark.asyncio
async def test_control_rerender_recovers_and_frozen_scoring_marks_error_recovered(phase2_store: StateStore) -> None:
    configure_checks(phase2_store)
    runtime = await form_runtime(phase2_store, ChoiceClient())
    runtime.expected_behavior_ids = ("EB-flow",)
    runtime.executor.identity_reference = "member"
    execute = runtime.executor.execute
    changed = False

    async def rerender_once(**kwargs: Any) -> Any:
        nonlocal changed
        if kwargs["action"].action_type == ActionType.INPUT and not changed:
            changed = True
            await kwargs["page"].locator(kwargs["action"].target).evaluate("element => element.id='fresh-name'")
        return await execute(**kwargs)

    runtime.executor.execute = rerender_once
    tools = tools_for(runtime, phase2_store)
    try:
        checks = [PageStep(ActionType.ASSERTION, target="#status", assertion="equals", expected="Provided", behavior_id="EB-flow", check_id=check["check_id"]) for check in runtime.required_checks]
        result = await tools.execute_test_plan([PageGoal("Create the provided object", operation="create", inputs=[PageInput("Name", "name"), PageInput("Note", "note")], checks=checks)])
        assert result["success"], result
        outcome = await runtime.record_task_outcome()
        assert outcome["success_status"] == "PASS"
        assert outcome["completed_check_count"] == 2
        assert outcome["errors"][0]["status"] == "RECOVERED"
        assert runtime.budget.usage.task_replans == 0
    finally:
        await runtime.browser_manager.close()


@pytest.mark.asyncio
async def test_missing_project_is_not_replaced_by_another_option(phase2_store: StateStore) -> None:
    runtime = await form_runtime(phase2_store, ChoiceClient(), '<select aria-label="Task project"><option value="p2">Other</option></select>')
    try:
        result = await runtime.execute_page_goal("Observe provided Project", operation="observe", project_reference="name")
        assert result["reason"] == "OBJECT_UNAVAILABLE_IN_CURRENT_VIEW"
        assert runtime.current_object["availability"]["status"] == "unavailable"
        assert not result["actions"] and runtime.active_project is None
    finally:
        await runtime.browser_manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("assertion, expected", [("visible", None), ("equals", "Provided"), ("count", "1")])
async def test_assertion_without_target_is_rejected_before_playwright(phase2_store: StateStore, assertion: str, expected: str | None) -> None:
    runtime = build_runtime(phase2_store, make_budget(), FakeJevClient())
    result = await runtime.execute_known_action(WebAction(ActionType.ASSERTION, assertion=assertion, expected=expected, check_id="missing-target"))
    assert result.error_type == "ASSERTION_TARGET_REQUIRED"
    assert not phase2_store.list_action_history(run_id="run-1", task_id="task-1")
    event = phase2_store.list_events("run-1", event_types=("ASSERTION_CONTRACT_FAILED",))[-1]
    assert event["result"]["check_id"] == "missing-target"
    assert {"target", "control", "object", "reason"} <= set(event["result"])


@pytest.mark.asyncio
async def test_entire_assertion_plan_is_validated_before_navigation(phase2_store: StateStore) -> None:
    configure_checks(phase2_store)
    runtime = await form_runtime(phase2_store, ChoiceClient())
    runtime.expected_behavior_ids = ("EB-flow",)
    tools = tools_for(runtime, phase2_store)
    try:
        history_count = len(phase2_store.list_action_history(run_id="run-1", task_id="task-1"))
        checks = [PageStep(ActionType.ASSERTION, assertion="visible", behavior_id="EB-flow", check_id=check["check_id"]) for check in runtime.required_checks]
        result = await tools.execute_test_plan([PageGoal("Review provided checks", checks=checks, before_steps=[PageStep(ActionType.NAVIGATION, url="http://app.test/other")])])
        assert result["reason"] == "ASSERTION_TARGET_REQUIRED"
        assert len(phase2_store.list_action_history(run_id="run-1", task_id="task-1")) == history_count
        assert not phase2_store.list_events("run-1", event_types=("TESTER_PLAN_VALIDATED",))
    finally:
        await runtime.browser_manager.close()


@pytest.mark.asyncio
async def test_missing_replan_check_id_is_rejected_with_remaining_ids(phase2_store: StateStore) -> None:
    configure_checks(phase2_store)
    runtime = await form_runtime(phase2_store, ChoiceClient())
    runtime.expected_behavior_ids = ("EB-flow",)
    tools = tools_for(runtime, phase2_store)
    tools.plan_started = True
    try:
        result = await tools.execute_test_plan([PageGoal("Check remaining work", checks=[PageStep(ActionType.ASSERTION, target="#status", assertion="visible")])])
        assert result["reason"] == "CHECK_ID_REQUIRED"
        assert result["remaining_check_ids"] == ["local.first", "local.last"]
        assert not phase2_store.list_events("run-1", event_types=("TESTER_PLAN_VALIDATED",))
    finally:
        await runtime.browser_manager.close()


@pytest.mark.asyncio
async def test_semantic_assertion_binds_supplied_field_alias_to_original_object(phase2_store: StateStore) -> None:
    runtime = await form_runtime(phase2_store, ChoiceClient(), '<table id="projects"><thead><tr><th>Name</th></tr></thead><tbody><tr data-project-id="p1"><td>Provided</td></tr><tr data-project-id="p2"><td>Other</td></tr></tbody></table>')
    try:
        result = await runtime.execute_page_action(WebAction(ActionType.ASSERTION, assertion="equals", expected="Provided"), control="Project name", context="name")
        assert result.action_result is not None and result.action_result.success
        assert 'data-project-id="p1"' in result.candidate.target
    finally:
        await runtime.browser_manager.close()


@pytest.mark.asyncio
async def test_assertion_reselects_only_its_explicit_original_project(phase2_store: StateStore) -> None:
    html = '<select aria-label="Task project" onchange="document.querySelector(\'#tasks\').hidden=this.value!==\'p1\'"><option value="p2">Other</option><option value="p1">Provided</option></select><table id="tasks" hidden><thead><tr><th>Title</th></tr></thead><tbody><tr data-task-id="t1"><td>Provided task</td></tr></tbody></table>'
    runtime = await form_runtime(phase2_store, ChoiceClient(), html)
    runtime.input_values["title"] = "Provided task"
    try:
        result = await runtime.execute_page_action(WebAction(ActionType.ASSERTION, assertion="equals", expected="Provided task"), control="Task title", context="title", project_reference="name")
        assert result.action_result is not None and result.action_result.success
        assert 'data-task-id="t1"' in result.candidate.target
        events = phase2_store.list_events("run-1", event_types=("RUNTIME_RECOVERY",))
        assert events[-1]["action"] == "rebind_project" and events[-1]["result"]["recovered"]
        assert runtime.budget.usage.task_replans == 0
    finally:
        await runtime.browser_manager.close()


@pytest.mark.asyncio
async def test_absent_control_on_wrong_view_does_not_pass_a_hidden_assertion(phase2_store: StateStore) -> None:
    runtime = await form_runtime(phase2_store, ChoiceClient(), '<table id="projects"><thead><tr><th>Name</th></tr></thead><tbody><tr data-project-id="p1"><td>Provided</td></tr></tbody></table>')
    page = runtime.browser_manager.get_session(runtime.browser_session_id).page
    try:
        runtime._bind_rows(await runtime.page_state_reader.read_rows(page), "name")
        runtime.remember_assertion_targets(await runtime.page_state_reader.read_rows(page), "name")
        await page.evaluate("document.querySelector('table').hidden=true")
        result = await runtime.execute_page_action(WebAction(ActionType.ASSERTION, assertion="hidden", check_id="original"), control="Name", context="name")
        assert result.reason == "ASSERTION_PREREQUISITE_UNAVAILABLE"
        assert result.action_result is None
        event = phase2_store.list_events("run-1", event_types=("ASSERTION_TARGET_UNAVAILABLE",))[-1]
        assert event["result"]["check_id"] == "original"
    finally:
        await runtime.browser_manager.close()


@pytest.mark.asyncio
async def test_row_identity_survives_table_and_name_changes_then_records_real_absence(phase2_store: StateStore) -> None:
    runtime = await form_runtime(phase2_store, ChoiceClient(), '<table id="projects"><thead><tr><th>Name</th></tr></thead><tbody><tr data-project-id="p1"><td>Provided</td></tr></tbody></table>')
    page = runtime.browser_manager.get_session(runtime.browser_session_id).page
    try:
        rows = await runtime.page_state_reader.read_rows(page)
        runtime._bind_rows(rows, "name")
        runtime.remember_assertion_targets(rows, "name")
        await page.evaluate("document.querySelector('td').textContent='Renamed'; document.querySelector('table').id='fresh-table'")
        current = await runtime.execute_page_action(WebAction(ActionType.ASSERTION, assertion="equals", expected="Renamed"), control="Name", context="name")
        assert current.action_result is not None and current.action_result.success
        assert 'fresh-table' in current.candidate.target and 'data-project-id="p1"' in current.candidate.target
        await page.evaluate("document.querySelector('tr[data-project-id]').remove()")
        absent = await runtime.execute_page_action(WebAction(ActionType.ASSERTION, assertion="hidden"), control="Name", context="name")
        assert absent.action_result is not None and absent.action_result.success
        assert 'fresh-table' in absent.candidate.target
    finally:
        await runtime.browser_manager.close()


def participant(store: StateStore) -> None:
    store.create_task(task_id="producer", run_id="run-1", goal="Publish live signal", priority="P1", dependencies=[], created_by="local", step_budget=20, status="RUNNING", data_requirements={"required_checks": [{"check_id": "producer.ready"}]})


@pytest.mark.asyncio
async def test_live_signal_wakes_waiter_and_remains_available_after_producer_failure(phase2_store: StateStore) -> None:
    runtime = build_runtime(phase2_store, make_budget(), FakeJevClient())
    runtime.required_checks = [{"check_id": "consumer.after", "depends_on": ["producer.ready"]}]
    participant(phase2_store)
    waiter = asyncio.create_task(runtime.wait_for_shared_progress("membership-removed", timeout_seconds=1))
    await asyncio.sleep(0)
    assert not waiter.done()
    # Another StateStore instance commits the durable event to the same database.
    producer = StateStore(phase2_store.database_path)
    producer.append_event(event_id="signal", run_id="run-1", task_id="producer", event_type="TASK_PROGRESS", action="publish", result={"summary": "membership-removed", "progressed": True}, latency_ms=0)
    first = await asyncio.wait_for(waiter, timeout=1)
    producer.update_task_status(task_id="producer", expected_status="RUNNING", new_status="FAILED")
    second = await runtime.wait_for_shared_progress("membership-removed", timeout_seconds=0)
    assert first["ready"] and second["event_id"] == first["event_id"] == "signal"
    assert runtime.budget.usage.task_replans == 0
    assert not StateStore._task_updates


@pytest.mark.asyncio
async def test_live_wait_retains_continuation_after_probe_interval_without_more_budget(phase2_store: StateStore) -> None:
    runtime = build_runtime(phase2_store, make_budget(), FakeJevClient())
    runtime.required_checks = [{"check_id": "consumer.after", "depends_on": ["producer.ready"]}]
    participant(phase2_store)
    original_limit = runtime.budget.limits.max_runtime_seconds
    with patch("web_testing_system.runtime.web_runtime.monotonic", return_value=runtime.budget.started_at + 2):
        waiter = asyncio.create_task(runtime.wait_for_shared_progress("membership-removed", timeout_seconds=1))
        await asyncio.sleep(0)
        assert not waiter.done()
        phase2_store.append_event(event_id="delayed", run_id="run-1", task_id="producer", event_type="TASK_PROGRESS", action="publish", result={"summary": "membership-removed", "progressed": True}, latency_ms=0)
        assert (await asyncio.wait_for(waiter, timeout=1))["ready"]
    assert runtime.budget.limits.max_runtime_seconds == original_limit
    assert runtime.budget.usage.task_replans == 0


@pytest.mark.asyncio
async def test_waiter_wakes_when_producer_closes_without_signal(phase2_store: StateStore) -> None:
    runtime = build_runtime(phase2_store, make_budget(), FakeJevClient())
    runtime.required_checks = [{"check_id": "consumer.after", "depends_on": ["producer.ready"]}]
    participant(phase2_store)
    waiter = asyncio.create_task(runtime.wait_for_shared_progress("member-delete-observed"))
    await asyncio.sleep(0)
    phase2_store.update_task_status(task_id="producer", expected_status="RUNNING", new_status="STOPPED")
    assert (await asyncio.wait_for(waiter, timeout=1))["reason"] == "PROGRESS_PRODUCER_CLOSED"
    assert not StateStore._task_updates


@pytest.mark.asyncio
async def test_cancelled_live_wait_removes_subscription_and_wait_state(phase2_store: StateStore) -> None:
    runtime = build_runtime(phase2_store, make_budget(), FakeJevClient())
    runtime.required_checks = [{"check_id": "consumer.after", "depends_on": ["producer.ready"]}]
    participant(phase2_store)
    waiter = asyncio.create_task(runtime.wait_for_shared_progress("membership-removed"))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert not StateStore._task_updates
    assert phase2_store.list_events("run-1", event_types=("RUNTIME_PROGRESS_WAIT",))[-1]["result"]["status"] == "FINISHED"


@pytest.mark.asyncio
async def test_undeclared_signal_is_a_plan_problem_not_an_endless_live_wait(phase2_store: StateStore) -> None:
    runtime = build_runtime(phase2_store, make_budget(), FakeJevClient())
    runtime.required_checks = [{"check_id": "consumer.after", "depends_on": ["producer.ready"]}]
    participant(phase2_store)
    phase2_store.append_event(event_id="producer-plan", run_id="run-1", task_id="producer", event_type="RUNTIME_SIGNAL_PLAN", action="register_signals", result={"signals": [{"name": "other-signal", "check_ids": ["producer.ready"]}]}, latency_ms=0)
    result = await runtime.wait_for_shared_progress("member-delete-observed")
    assert result["reason"] == "PROGRESS_SIGNAL_UNDECLARED"
    assert not StateStore._task_updates


@pytest.mark.asyncio
async def test_signal_is_not_lost_after_more_than_100_events(phase2_store: StateStore) -> None:
    runtime = build_runtime(phase2_store, make_budget(), FakeJevClient())
    participant(phase2_store)
    phase2_store.append_event(event_id="original", run_id="run-1", task_id="producer", event_type="TASK_PROGRESS", action="publish", result={"summary": "member-session-ready", "progressed": True}, latency_ms=0)
    for index in range(101):
        phase2_store.append_event(event_id=f"noise-{index}", run_id="run-1", task_id="producer", event_type="TASK_PROGRESS", action="publish", result={"summary": "irrelevant", "progressed": True}, latency_ms=0)
    assert (await runtime.wait_for_shared_progress("member-session-ready", timeout_seconds=0))["event_id"] == "original"


@pytest.mark.asyncio
@pytest.mark.parametrize("producer_state", ["COMPLETED", "FAILED", "STOPPED"])
async def test_closed_producer_without_signal_does_not_wait_or_replan(phase2_store: StateStore, producer_state: str) -> None:
    runtime = build_runtime(phase2_store, make_budget(), FakeJevClient())
    participant(phase2_store)
    phase2_store.update_task_status(task_id="producer", expected_status="RUNNING", new_status=producer_state)
    result = await runtime.wait_for_shared_progress("member-delete-observed")
    assert result["reason"] == "PROGRESS_PRODUCER_CLOSED"
    assert runtime.budget.usage.task_replans == 0


@pytest.mark.asyncio
async def test_false_or_similar_signal_does_not_unlock_a_check(phase2_store: StateStore) -> None:
    runtime = build_runtime(phase2_store, make_budget(), FakeJevClient())
    participant(phase2_store)
    for index, summary in enumerate(["membership-removed", "membership-removed-later"]):
        phase2_store.append_event(event_id=f"invalid-{index}", run_id="run-1", task_id="producer", event_type="TASK_PROGRESS", action="publish", result={"summary": summary, "progressed": index != 0}, latency_ms=0)
    assert not (await runtime.wait_for_shared_progress("membership-removed", timeout_seconds=0))["ready"]
