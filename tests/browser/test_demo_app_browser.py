from __future__ import annotations

import pytest
from playwright.async_api import async_playwright

from demo_app import DEMO_VERSION, DemoAppServer


@pytest.mark.browser
@pytest.mark.asyncio
async def test_demo_app_ui_has_auth_crud_modal_tables_and_one_visual_scene() -> None:
    with DemoAppServer() as app:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(app.base_url)
            await page.locator("#version").wait_for()
            assert await page.locator("#version").inner_text() == f"Version: {DEMO_VERSION}"

            await page.locator("#login-username").fill("admin")
            await page.locator("#login-password").fill("demo-admin")
            await page.get_by_role("button", name="Login").click()
            await page.locator("#projects-table").wait_for()
            assert await page.locator("#identity").inner_text() == "admin (admin)"

            await page.locator("#project-name").fill("browser-run-tester-a-task-project")
            await page.locator("#project-submit").click()
            await page.locator("#projects-table .project-name", has_text="browser-run-tester-a-task-project").wait_for()
            await page.locator("#projects-table .edit-project").last.click()
            assert await page.locator("#edit-dialog").is_visible()
            await page.locator("#edit-dialog button[value='cancel']").click()

            canvas = page.locator("#dependency-canvas")
            await canvas.click(position={"x": 380, "y": 100})
            assert await page.locator("#visual-status").inner_text() == "Selected dependency: Project B"
            assert await page.locator("canvas").count() == 1

            await page.locator("#logout").click()
            await page.locator("#auth-section").wait_for()
            await browser.close()
