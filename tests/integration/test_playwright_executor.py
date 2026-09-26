from __future__ import annotations

import pytest
from playwright.async_api import async_playwright

from web_testing_system.runtime.models import ActionType, PermissionDecision, WebAction
from web_testing_system.runtime.permissions import ExecutionPolicy, PermissionChecker
from web_testing_system.runtime.playwright_executor import PlaywrightExecutor
from web_testing_system.state import StateStore

HTML = """
<main>
  <button id="open">Open Project</button>
  <input id="name" aria-label="Project name">
  <select id="role"><option value="member">Member</option><option value="admin">Admin</option></select>
  <div id="source" draggable="true">Drag me</div>
  <div id="drop">Drop here</div>
  <p id="status">ready</p>
</main>
"""


def executor(
    store: StateStore, permissions: dict[ActionType, PermissionDecision] | None = None
) -> PlaywrightExecutor:
    checker = PermissionChecker(
        store=store,
        policy=ExecutionPolicy(
            allowed_url_prefixes=("http://app.test/",), permissions=permissions or {}
        ),
        run_id="run-1",
        task_id="task-1",
        tester_id="tester-1",
    )
    return PlaywrightExecutor(
        store=store,
        permission_checker=checker,
        run_id="run-1",
        task_id="task-1",
        tester_id="tester-1",
    )


def test_url_scope_compares_origin_and_path_boundaries(
    phase2_store: StateStore,
) -> None:
    checker = PermissionChecker(
        store=phase2_store,
        policy=ExecutionPolicy(allowed_url_prefixes=("http://app.test/projects",)),
        run_id="run-1",
        task_id="task-1",
        tester_id="tester-1",
    )

    assert checker.url_allowed("http://app.test/projects")
    assert checker.url_allowed("http://app.test/projects/one")
    assert not checker.url_allowed("http://app.test/project-settings")
    assert not checker.url_allowed("http://app.test.evil/projects")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_executor_supports_known_actions_and_persists_event_history(
    phase2_store: StateStore,
) -> None:
    phase2_store.create_resource(
        resource_id="resource-1",
        run_id="run-1",
        resource_type="project",
        owner_task="task-1",
        owner="tester-1",
        participants=["tester-1"],
        allowed_operations=["input"],
        sharing_mode="ISOLATED",
        cleanup_status="PENDING",
    )
    action_executor = executor(phase2_store)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        async def handle(route) -> None:  # type: ignore[no-untyped-def]
            await route.fulfill(status=200, content_type="text/html", body=HTML)

        await page.route("http://app.test/**", handle)
        actions = [
            WebAction(action_type=ActionType.NAVIGATION, url="http://app.test/"),
            WebAction(action_type=ActionType.CLICK, target="#open"),
            WebAction(
                action_type=ActionType.INPUT,
                target="#name",
                value="Alice",
                resource_id="resource-1",
                requires_resource=True,
            ),
            WebAction(action_type=ActionType.SELECT, target="#role", value="admin"),
            WebAction(action_type=ActionType.KEYBOARD, key="Tab"),
            WebAction(action_type=ActionType.WAIT, target="#status"),
            WebAction(action_type=ActionType.REFRESH),
            WebAction(
                action_type=ActionType.DRAG_AND_DROP, target="#source", value="#drop"
            ),
            WebAction(action_type=ActionType.DOM_INSPECTION, target="#status"),
            WebAction(
                action_type=ActionType.URL_CHECK,
                expected="http://app.test/",
                assertion="equals",
            ),
            WebAction(
                action_type=ActionType.ASSERTION,
                target="#status",
                expected="ready",
                assertion="contains",
            ),
        ]
        results = [
            await action_executor.execute(
                page=page, browser_session_id="browser-1", action=action
            )
            for action in actions
        ]

        assertion_failure = await action_executor.execute(
            page=page,
            browser_session_id="browser-1",
            action=WebAction(
                action_type=ActionType.ASSERTION,
                target="#status",
                expected="not present",
                assertion="contains",
            ),
        )
        scope_failure = await action_executor.execute(
            page=page,
            browser_session_id="browser-1",
            action=WebAction(
                action_type=ActionType.NAVIGATION, url="javascript:alert(1)"
            ),
        )
        resource_failure = await action_executor.execute(
            page=page,
            browser_session_id="browser-1",
            action=WebAction(
                action_type=ActionType.INPUT,
                target="#name",
                value="secret",
                requires_resource=True,
            ),
        )
        resource_permission_failure = await action_executor.execute(
            page=page,
            browser_session_id="browser-1",
            action=WebAction(
                action_type=ActionType.CLICK,
                target="#open",
                resource_id="resource-1",
                requires_resource=True,
            ),
        )
        target_failure = await action_executor.execute(
            page=page,
            browser_session_id="browser-1",
            action=WebAction(action_type=ActionType.CLICK, target="#missing"),
        )
        await context.close()
        await browser.close()

    assert all(result.success for result in results)
    assert (
        assertion_failure.success is False
        and assertion_failure.error_type == "ASSERTION_FAILURE"
    )
    assert (
        scope_failure.success is False and scope_failure.error_type == "SCOPE_VIOLATION"
    )
    assert (
        resource_failure.success is False
        and resource_failure.error_type == "RESOURCE_NOT_REGISTERED"
    )
    assert (
        resource_permission_failure.success is False
        and resource_permission_failure.error_type == "RESOURCE_OPERATION_DENIED"
    )
    assert (
        target_failure.success is False
        and target_failure.error_type == "INVALID_ACTION"
    )
    history = phase2_store.list_action_history(run_id="run-1", task_id="task-1")
    events = [
        event
        for event in phase2_store.list_events("run-1")
        if event["event_type"] == "BROWSER_ACTION"
    ]
    resource_conflicts = [
        event
        for event in phase2_store.list_events("run-1")
        if event["event_type"] == "RESOURCE_CONFLICT"
    ]
    assert len(history) == len(actions) + 5
    assert len(events) == len(history)
    assert len(resource_conflicts) == 2
    assert phase2_store.list_evidence(run_id="run-1") == []
    assert sum(item["success"] for item in history) == len(actions)
    assert "Alice" not in str(history)
    assert "secret" not in str(history)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_executor_enforces_confirmation_before_action(
    phase2_store: StateStore,
) -> None:
    action_executor = executor(
        phase2_store, {ActionType.CLICK: PermissionDecision.REQUIRE_CONFIRMATION}
    )
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()

        async def handle(route) -> None:  # type: ignore[no-untyped-def]
            await route.fulfill(status=200, content_type="text/html", body=HTML)

        await page.route("http://app.test/**", handle)
        await page.goto("http://app.test/")
        confirmation_denied = await action_executor.execute(
            page=page,
            browser_session_id="browser-1",
            action=WebAction(action_type=ActionType.CLICK, target="#open"),
        )
        allowed = await action_executor.execute(
            page=page,
            browser_session_id="browser-1",
            action=WebAction(
                action_type=ActionType.CLICK, target="#open", confirmed=True
            ),
        )
        policy_denied = await executor(
            phase2_store, {ActionType.CLICK: PermissionDecision.DENY}
        ).execute(
            page=page,
            browser_session_id="browser-1",
            action=WebAction(action_type=ActionType.CLICK, target="#open"),
        )
        await browser.close()

    assert (
        confirmation_denied.success is False
        and confirmation_denied.error_type == "REQUIRE_CONFIRMATION"
    )
    assert allowed.success is True
    assert (
        policy_denied.success is False
        and policy_denied.error_type == "PERMISSION_DENIED"
    )
    with pytest.raises(ValueError):
        ActionType("javascript")
