"""Deterministic Playwright actions with checks and persistent history."""

from __future__ import annotations

from datetime import UTC, datetime
from time import perf_counter
from typing import Any
from uuid import uuid4

from playwright.async_api import Locator, Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from web_testing_system.runtime.models import ActionResult, ActionType, WebAction
from web_testing_system.runtime.permissions import PermissionChecker
from web_testing_system.state import StateStore


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class PlaywrightExecutor:
    def __init__(
        self,
        *,
        store: StateStore,
        permission_checker: PermissionChecker,
        run_id: str,
        task_id: str,
        tester_id: str,
    ) -> None:
        self.store = store
        self.permission_checker = permission_checker
        self.run_id = run_id
        self.task_id = task_id
        self.tester_id = tester_id

    async def execute(
        self, *, page: Page, browser_session_id: str, action: WebAction
    ) -> ActionResult:
        started_at = utc_now()
        started_timer = perf_counter()
        success = False
        error: str | None = None
        error_type: str | None = None
        data: dict[str, Any] = {}
        permission = self.permission_checker.check(action, current_url=page.url)
        try:
            if not permission.allowed:
                error = permission.reason
                error_type = permission.code
            else:
                locator = await self._validate_target(page, action)
                data = await self._execute_action(page, locator, action)
                success = True
        except PlaywrightTimeoutError as caught_error:
            error = str(caught_error)
            error_type = "PAGE_TIMEOUT"
        except AssertionError as caught_error:
            error = str(caught_error)
            error_type = "ASSERTION_FAILURE"
        except (KeyError, TypeError, ValueError) as caught_error:
            error = str(caught_error)
            error_type = "INVALID_ACTION"
        except Exception as caught_error:
            error = str(caught_error)
            error_type = "ACTION_ERROR"
        ended_at = utc_now()
        latency_ms = (perf_counter() - started_timer) * 1_000
        event_id = f"event-{uuid4().hex}"
        result = ActionResult(
            started_at=started_at,
            ended_at=ended_at,
            latency_ms=latency_ms,
            success=success,
            error=error,
            error_type=error_type,
            data=data,
            event_id=event_id,
        )
        self._record_action(
            page=page,
            browser_session_id=browser_session_id,
            action=action,
            result=result,
        )
        return result

    async def _validate_target(self, page: Page, action: WebAction) -> Locator | None:
        target_actions = {
            ActionType.CLICK,
            ActionType.INPUT,
            ActionType.SELECT,
            ActionType.DRAG_AND_DROP,
            ActionType.DOM_INSPECTION,
            ActionType.ASSERTION,
        }
        if action.action_type not in target_actions:
            return None
        if action.target is None:
            raise ValueError(f"{action.action_type.value} requires a target")
        locator = page.locator(action.target).first
        count = await locator.count()
        if action.action_type == ActionType.ASSERTION and action.assertion == "hidden":
            return locator
        if count == 0:
            raise ValueError("target does not exist")
        if (
            action.action_type != ActionType.DOM_INSPECTION
            and not await locator.is_visible()
        ):
            raise ValueError("target is not visible")
        if (
            action.action_type
            in {
                ActionType.CLICK,
                ActionType.INPUT,
                ActionType.SELECT,
                ActionType.DRAG_AND_DROP,
            }
            and not await locator.is_enabled()
        ):
            raise ValueError("target is disabled")
        return locator

    async def _execute_action(
        self, page: Page, locator: Locator | None, action: WebAction
    ) -> dict[str, Any]:
        if action.action_type == ActionType.NAVIGATION:
            if action.url is None:
                raise ValueError("navigation requires a URL")
            response = await page.goto(
                action.url, wait_until="domcontentloaded", timeout=action.timeout_ms
            )
            return {
                "url": page.url,
                "status": response.status if response is not None else None,
            }
        if action.action_type == ActionType.CLICK:
            assert locator is not None
            await locator.click(timeout=action.timeout_ms)
            return {"url": page.url}
        if action.action_type == ActionType.INPUT:
            assert locator is not None
            if action.value is None:
                raise ValueError("input requires a value")
            await locator.fill(action.value, timeout=action.timeout_ms)
            return {"url": page.url, "value_recorded": False}
        if action.action_type == ActionType.SELECT:
            assert locator is not None
            if action.value is None:
                raise ValueError("select requires a value")
            await locator.select_option(action.value, timeout=action.timeout_ms)
            return {"url": page.url}
        if action.action_type == ActionType.KEYBOARD:
            if action.key is None:
                raise ValueError("keyboard requires a key")
            await page.keyboard.press(action.key)
            return {"url": page.url}
        if action.action_type == ActionType.WAIT:
            if action.target is not None:
                await page.locator(action.target).first.wait_for(
                    state="visible", timeout=action.timeout_ms
                )
            else:
                await page.wait_for_timeout(action.wait_ms)
            return {"url": page.url}
        if action.action_type == ActionType.REFRESH:
            await page.reload(wait_until="domcontentloaded", timeout=action.timeout_ms)
            return {"url": page.url}
        if action.action_type == ActionType.DRAG_AND_DROP:
            if action.target is None or action.value is None:
                raise ValueError(
                    "drag and drop requires source and destination targets"
                )
            await page.drag_and_drop(
                action.target, action.value, timeout=action.timeout_ms
            )
            return {"url": page.url}
        if action.action_type == ActionType.DOM_INSPECTION:
            assert locator is not None
            return {
                "url": page.url,
                "text": await locator.inner_text(timeout=action.timeout_ms),
            }
        if action.action_type == ActionType.URL_CHECK:
            if action.expected is None:
                raise ValueError("URL check requires an expected value")
            matched = (
                page.url == action.expected
                if action.assertion == "equals"
                else action.expected in page.url
            )
            if not matched:
                raise AssertionError(f"URL assertion failed: {page.url!r}")
            return {"url": page.url, "matched": True}
        if action.action_type == ActionType.ASSERTION:
            assert locator is not None
            return await self._run_assertion(locator, action)
        raise ValueError(f"unsupported action type: {action.action_type}")

    async def _run_assertion(
        self, locator: Locator, action: WebAction
    ) -> dict[str, Any]:
        if action.assertion == "visible":
            if not await locator.is_visible():
                raise AssertionError("target is not visible")
            return {"matched": True}
        if action.assertion == "hidden":
            if await locator.count() > 0 and await locator.is_visible():
                raise AssertionError("target is visible")
            return {"matched": True}
        if action.expected is None:
            raise ValueError("text assertion requires an expected value")
        actual = await locator.inner_text(timeout=action.timeout_ms)
        matched = (
            actual == action.expected
            if action.assertion == "equals"
            else action.expected in actual
        )
        if not matched:
            raise AssertionError(f"text assertion failed: {actual!r}")
        return {"matched": True, "actual": actual}

    def _record_action(
        self,
        *,
        page: Page,
        browser_session_id: str,
        action: WebAction,
        result: ActionResult,
    ) -> None:
        assert result.event_id is not None
        event_result = {
            "success": result.success,
            "error": result.error,
            "error_type": result.error_type,
            "data": result.data,
        }
        self.store.append_event(
            event_id=result.event_id,
            run_id=self.run_id,
            task_id=self.task_id,
            tester_id=self.tester_id,
            browser_session_id=browser_session_id,
            event_type="BROWSER_ACTION",
            url=page.url,
            tool="Playwright",
            action=action.action_type.value,
            result=event_result,
            latency_ms=result.latency_ms,
        )
        self.store.append_action_history(
            history_id=f"history-{uuid4().hex}",
            event_id=result.event_id,
            run_id=self.run_id,
            task_id=self.task_id,
            tester_id=self.tester_id,
            browser_session_id=browser_session_id,
            url=page.url,
            action=action.action_type.value,
            target=action.target,
            tool="Playwright",
            started_at=result.started_at,
            ended_at=result.ended_at,
            latency_ms=result.latency_ms,
            success=result.success,
            error=result.error,
            result=event_result,
            action_data={
                "action_type": action.action_type.value,
                "target": action.target,
                "value_reference": action.value_reference,
                "url": action.url,
                "expected": action.expected,
                "assertion": action.assertion,
                "key": action.key,
                "wait_ms": action.wait_ms,
                "resource_id": action.resource_id,
                "requires_resource": action.requires_resource,
                "confirmed": action.confirmed,
                "timeout_ms": action.timeout_ms,
            },
        )
        if result.error_type is not None and result.error_type.startswith("RESOURCE_"):
            self.store.append_event(
                event_id=f"event-{uuid4().hex}",
                run_id=self.run_id,
                task_id=self.task_id,
                tester_id=self.tester_id,
                browser_session_id=browser_session_id,
                event_type="RESOURCE_CONFLICT",
                url=page.url,
                tool="PlaywrightExecutor",
                action=action.action_type.value,
                result={
                    "resource_id": action.resource_id,
                    "error_type": result.error_type,
                    "error": result.error,
                },
                latency_ms=0,
            )
