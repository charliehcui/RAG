"""Chromium lifecycle and isolated Tester browser sessions."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import uuid4

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

from web_testing_system.runtime.budget import BudgetGuard

LoginHandler = Callable[[Page], Awaitable[None]]


@dataclass
class BrowserSession:
    session_id: str
    tester_id: str
    identity_id: str
    context: BrowserContext
    page: Page


class BrowserManager:
    def __init__(self, budget: BudgetGuard) -> None:
        self.budget = budget
        self.playwright: Playwright | None = None
        self.browser: Browser | None = None
        self.sessions: dict[str, BrowserSession] = {}

    async def start(self) -> None:
        if self.browser is not None:
            return
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True)

    async def create_session(
        self, *, tester_id: str, identity_id: str
    ) -> BrowserSession:
        self.budget.ensure_can_start("browser_context")
        await self.start()
        assert self.browser is not None
        context = await self.browser.new_context()
        page = await context.new_page()
        session = BrowserSession(
            session_id=f"browser-{uuid4().hex}",
            tester_id=tester_id,
            identity_id=identity_id,
            context=context,
            page=page,
        )
        self.sessions[session.session_id] = session
        self.budget.record_context_opened()
        return session

    def get_session(self, session_id: str) -> BrowserSession:
        try:
            return self.sessions[session_id]
        except KeyError as error:
            raise KeyError(f"unknown browser session: {session_id}") from error

    async def clear_storage(self, session_id: str) -> None:
        session = self.get_session(session_id)
        await session.context.clear_cookies()
        for page in session.context.pages:
            if not page.is_closed():
                try:
                    await page.evaluate(
                        "() => { localStorage.clear(); sessionStorage.clear(); }"
                    )
                except Exception:
                    if page.url != "about:blank":
                        raise

    async def recreate_session(
        self, session_id: str, login_handler: LoginHandler | None = None
    ) -> BrowserSession:
        old_session = self.get_session(session_id)
        tester_id = old_session.tester_id
        identity_id = old_session.identity_id
        await self.close_session(session_id)
        new_session = await self.create_session(
            tester_id=tester_id, identity_id=identity_id
        )
        if login_handler is not None:
            await login_handler(new_session.page)
        return new_session

    async def close_session(self, session_id: str) -> None:
        session = self.sessions.pop(session_id, None)
        if session is None:
            return
        try:
            await session.context.close()
        finally:
            self.budget.record_context_closed()

    async def close(self) -> None:
        for session_id in list(self.sessions):
            await self.close_session(session_id)
        if self.browser is not None:
            await self.browser.close()
            self.browser = None
        if self.playwright is not None:
            await self.playwright.stop()
            self.playwright = None
