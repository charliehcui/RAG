from __future__ import annotations

import pytest
from playwright.async_api import async_playwright


@pytest.mark.browser
@pytest.mark.asyncio
async def test_chromium_starts_without_accessing_a_business_application() -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content("<title>Phase 1 smoke check</title><main>ready</main>")

        assert await page.title() == "Phase 1 smoke check"
        assert await page.locator("main").text_content() == "ready"

        await browser.close()
