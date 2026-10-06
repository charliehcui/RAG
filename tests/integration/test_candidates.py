from __future__ import annotations

import pytest
from playwright.async_api import async_playwright

from web_testing_system.runtime.candidates import CandidateBuilder, PageStateReader
from web_testing_system.runtime.models import (
    ActionCandidate,
    ActionType,
    BusinessAction,
    PermissionDecision,
    WebAction,
)
from web_testing_system.runtime.permissions import ExecutionPolicy, PermissionChecker
from web_testing_system.state import StateStore


@pytest.mark.integration
@pytest.mark.asyncio
async def test_page_state_and_candidate_builder_filter_and_revalidate_actions(
    phase2_store: StateStore,
) -> None:
    checker = PermissionChecker(
        store=phase2_store,
        policy=ExecutionPolicy(
            allowed_url_prefixes=("http://app.test/",),
            permissions={ActionType.INPUT: PermissionDecision.REQUIRE_CONFIRMATION},
        ),
        run_id="run-1",
        task_id="task-1",
        tester_id="tester-1",
    )
    reader = PageStateReader()
    builder = CandidateBuilder(checker)
    html = """
    <main>
      <button>Open Project</button>
      <a href="/settings">Project Settings</a>
      <a href="https://evil.test/admin">External Admin</a>
      <input aria-label="Project name">
      <button style="display:none">Hidden</button>
      <button disabled>Disabled</button>
      <div role="tab">Details</div>
    </main>
    """
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()

        async def handle(route) -> None:  # type: ignore[no-untyped-def]
            await route.fulfill(status=200, content_type="text/html", body=html)

        await page.route("http://app.test/**", handle)
        await page.goto("http://app.test/projects")
        state = await reader.read(page)
        settings_target = next(
            element.target
            for element in state.interactive_elements
            if element.label == "Project Settings"
        )
        candidates = builder.build(
            goal="Open project settings",
            page_state=state,
            business_actions=[
                BusinessAction(
                    label="Refresh project data",
                    action=WebAction(action_type=ActionType.REFRESH),
                )
            ],
            explored_targets={settings_target},
        )

        labels = {candidate.label for candidate in candidates}
        assert state.url == "http://app.test/projects"
        assert state.load_state == "complete"
        assert "Open Project" in state.visible_dom
        assert state.accessibility
        assert "Open Project" in labels
        assert "Project Settings" in labels
        assert "Project name" in labels
        assert "Details" in labels
        assert "Refresh project data" in labels
        assert "External Admin" not in labels
        assert "Hidden" not in labels
        assert "Disabled" not in labels
        assert {"request_replan", "stop_current_path"}.issubset(
            {candidate.action for candidate in candidates}
        )
        assert len({candidate.candidate_id for candidate in candidates}) == len(
            candidates
        )
        ranked_dom_labels = [
            candidate.label for candidate in candidates if candidate.target is not None
        ]
        assert ranked_dom_labels.index("Open Project") < ranked_dom_labels.index(
            "Project Settings"
        )

        forged = ActionCandidate(
            candidate_id="forged",
            action="javascript",
            label="Run script",
            target="#x",
            state_id=state.state_id,
        )
        assert (
            builder.validate(candidate=forged, current_page_state=state).reason
            == "FORGED_ACTION"
        )

        selected = next(
            candidate for candidate in candidates if candidate.label == "Open Project"
        )
        await page.locator("main").evaluate(
            "element => element.insertAdjacentHTML('beforeend', '<button>New state</button>')"
        )
        changed_state = await reader.read(page)
        assert (
            builder.validate(
                candidate=selected, current_page_state=changed_state
            ).reason
            == "CANDIDATE_EXPIRED"
        )
        await browser.close()


@pytest.mark.asyncio
async def test_hidden_controls_do_not_hide_visible_candidates_beyond_the_limit() -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content('<div hidden>' + '<input placeholder="hidden">' * 35 + '</div><button id="edit">Edit task</button><button>Delete task</button>')
        state = await PageStateReader(max_elements=1).read(page)
        assert [element.label for element in state.interactive_elements] == ["Edit task"]
        await page.locator(state.interactive_elements[0].target).click()
        assert await page.locator(state.interactive_elements[0].target).get_attribute("id") == "edit"
        await browser.close()
