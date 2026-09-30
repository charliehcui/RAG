from __future__ import annotations

import pytest
from playwright.async_api import Page, Route

from web_testing_system.runtime.browser import BrowserManager
from web_testing_system.runtime.budget import (
    BudgetExceededError,
    BudgetGuard,
    BudgetLimits,
)


def browser_budget(max_contexts: int = 2) -> BudgetGuard:
    return BudgetGuard(
        BudgetLimits(
            max_runtime_seconds=60,
            max_llm_calls=0,
            max_input_tokens=0,
            max_output_tokens=0,
            max_jev_calls=0,
            max_computer_use_calls=0,
            max_task_steps=10,
            max_task_replans=0,
            max_browser_contexts=max_contexts,
        )
    )


async def install_test_page(page: Page) -> None:
    async def handle(route: Route) -> None:
        await route.fulfill(
            status=200, content_type="text/html", body="<main>isolated session</main>"
        )

    await page.route("http://app.test/**", handle)
    await page.goto("http://app.test/")


@pytest.mark.browser
@pytest.mark.asyncio
async def test_tester_contexts_isolate_and_clear_browser_storage() -> None:
    budget = browser_budget()
    manager = BrowserManager(budget)
    first = await manager.create_session(tester_id="tester-1", identity_id="identity-1")
    second = await manager.create_session(
        tester_id="tester-2", identity_id="identity-2"
    )
    try:
        await install_test_page(first.page)
        await install_test_page(second.page)
        await first.context.add_cookies(
            [{"name": "session", "value": "first", "url": "http://app.test"}]
        )
        await second.context.add_cookies(
            [{"name": "session", "value": "second", "url": "http://app.test"}]
        )
        await first.page.evaluate(
            "() => { localStorage.setItem('owner', 'first'); sessionStorage.setItem('owner', 'first'); }"
        )
        await second.page.evaluate(
            "() => { localStorage.setItem('owner', 'second'); sessionStorage.setItem('owner', 'second'); }"
        )

        assert (await first.context.cookies())[0]["value"] == "first"
        assert (await second.context.cookies())[0]["value"] == "second"
        assert (
            await first.page.evaluate("() => localStorage.getItem('owner')") == "first"
        )
        assert (
            await second.page.evaluate("() => localStorage.getItem('owner')")
            == "second"
        )

        with pytest.raises(BudgetExceededError, match="MAX_BROWSER_CONTEXTS_REACHED"):
            await manager.create_session(tester_id="tester-3", identity_id="identity-3")

        await manager.clear_storage(first.session_id)
        assert await first.context.cookies() == []
        assert await first.page.evaluate("() => localStorage.getItem('owner')") is None
        assert (
            await first.page.evaluate("() => sessionStorage.getItem('owner')") is None
        )
        assert (await second.context.cookies())[0]["value"] == "second"

        await manager.close_session(first.session_id)
        replacement = await manager.create_session(
            tester_id="tester-3", identity_id="identity-3"
        )
        assert replacement.session_id != first.session_id
        assert replacement.context != second.context
    finally:
        await manager.close()

    assert budget.usage.browser_contexts == 0
