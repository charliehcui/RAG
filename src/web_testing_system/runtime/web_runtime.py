"""Safe orchestration of browser actions, Laya selection, and local recovery."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import uuid4

from web_testing_system.runtime.browser import BrowserManager, LoginHandler
from web_testing_system.runtime.budget import BudgetExceededError, BudgetGuard
from web_testing_system.runtime.candidates import CandidateBuilder, PageStateReader
from web_testing_system.runtime.laya_selector import LayaSelector
from web_testing_system.runtime.models import (
    ActionResult,
    ActionType,
    DecisionResult,
    WebAction,
)
from web_testing_system.runtime.playwright_executor import PlaywrightExecutor
from web_testing_system.state import StateStore


class WebTestingRuntime:
    def __init__(
        self,
        *,
        store: StateStore,
        browser_manager: BrowserManager,
        executor: PlaywrightExecutor,
        page_state_reader: PageStateReader,
        candidate_builder: CandidateBuilder,
        laya_selector: LayaSelector,
        budget: BudgetGuard,
        run_id: str,
        task_id: str,
        tester_id: str,
        identity_id: str,
        budget_id: str,
        laya_confidence_threshold: float = 0.6,
        max_timeout_retries: int = 1,
    ) -> None:
        self.store = store
        self.browser_manager = browser_manager
        self.executor = executor
        self.page_state_reader = page_state_reader
        self.candidate_builder = candidate_builder
        self.laya_selector = laya_selector
        self.budget = budget
        self.run_id = run_id
        self.task_id = task_id
        self.tester_id = tester_id
        self.identity_id = identity_id
        self.budget_id = budget_id
        self.laya_confidence_threshold = laya_confidence_threshold
        self.max_timeout_retries = max_timeout_retries
        self.browser_session_id: str | None = None

    async def start_session(self) -> str:
        try:
            session = await self.browser_manager.create_session(
                tester_id=self.tester_id, identity_id=self.identity_id
            )
        except BudgetExceededError as error:
            await self.stop_task(error.reason)
            raise
        self.browser_session_id = session.session_id
        return session.session_id

    async def execute_known_action(self, action: WebAction) -> ActionResult:
        if self.browser_session_id is None:
            raise RuntimeError("browser session has not started")
        session = self.browser_manager.get_session(self.browser_session_id)
        attempts = 0
        while True:
            try:
                self.budget.ensure_can_start("browser_step")
            except BudgetExceededError as error:
                await self.stop_task(error.reason)
                return self._stopped_result(error.reason)
            result = await self.executor.execute(
                page=session.page, browser_session_id=session.session_id, action=action
            )
            self.budget.record_browser_step(runtime_seconds=result.latency_ms / 1_000)
            self.store.update_budget(
                budget_id=self.budget_id,
                browser_steps=1,
                runtime_seconds=result.latency_ms / 1_000,
            )
            if result.success:
                self.save_checkpoint(
                    url=session.page.url, last_action=action.action_type.value
                )
                return result
            if (
                result.error_type != "PAGE_TIMEOUT"
                or attempts >= self.max_timeout_retries
            ):
                if result.error_type == "PAGE_TIMEOUT":
                    self._record_environment_issue(result.error or "page timeout")
                return result
            attempts += 1

    async def explore_unknown_path(self, current_goal: str) -> DecisionResult:
        if self.browser_session_id is None:
            raise RuntimeError("browser session has not started")
        session = self.browser_manager.get_session(self.browser_session_id)
        page_state = await self.page_state_reader.read(session.page)
        explored_targets = {
            str(history["target"])
            for history in self.store.list_action_history(
                run_id=self.run_id, task_id=self.task_id
            )
            if history["target"] is not None
        }
        candidates = self.candidate_builder.build(
            goal=current_goal,
            page_state=page_state,
            explored_targets=explored_targets,
        )
        if not candidates:
            return DecisionResult(
                source="TESTER_LLM", reason="NO_LEGAL_CANDIDATES", needs_tester_llm=True
            )
        try:
            self.budget.ensure_can_start("laya")
        except BudgetExceededError as error:
            await self.stop_task(error.reason)
            return DecisionResult(source="STOP", reason=error.reason)
        selection = await self.laya_selector.select(
            current_goal=current_goal, page_state=page_state, candidates=candidates
        )
        self.budget.record_laya_call(
            runtime_seconds=selection.latency_ms / 1_000, cost=selection.cost
        )
        self.store.update_budget(
            budget_id=self.budget_id,
            jev_calls=1,
            runtime_seconds=selection.latency_ms / 1_000,
            estimated_cost=selection.cost,
        )
        self.store.append_event(
            event_id=f"event-{uuid4().hex}",
            run_id=self.run_id,
            task_id=self.task_id,
            tester_id=self.tester_id,
            browser_session_id=session.session_id,
            event_type="LAYA_CALL",
            url=page_state.url,
            tool="Laya",
            action="select_candidate",
            result={
                "selected_candidate_id": selection.selected_candidate_id,
                "confidence": selection.confidence,
                "error": selection.error,
            },
            latency_ms=selection.latency_ms,
            cost=selection.cost,
        )
        candidate_map = {candidate.candidate_id: candidate for candidate in candidates}
        candidate = candidate_map.get(selection.selected_candidate_id or "")
        if selection.error is not None:
            return DecisionResult(
                source="TESTER_LLM", reason=selection.error, needs_tester_llm=True
            )
        if selection.confidence < self.laya_confidence_threshold:
            return DecisionResult(
                source="TESTER_LLM", reason="LOW_LAYA_CONFIDENCE", needs_tester_llm=True
            )
        if candidate is None:
            return DecisionResult(
                source="TESTER_LLM",
                reason="ILLEGAL_CANDIDATE_ID",
                needs_tester_llm=True,
            )
        current_page_state = await self.page_state_reader.read(session.page)
        validation = self.candidate_builder.validate(
            candidate=candidate, current_page_state=current_page_state
        )
        if not validation.valid:
            return DecisionResult(
                source="TESTER_LLM",
                reason=validation.reason,
                candidate=candidate,
                needs_tester_llm=True,
            )
        if candidate.action == "request_replan":
            await self.request_replan("LAYA_REQUESTED_REPLAN")
            return DecisionResult(
                source="LAYA", reason="REQUEST_REPLAN", candidate=candidate
            )
        if candidate.action == "stop_current_path":
            self.store.append_event(
                event_id=f"event-{uuid4().hex}",
                run_id=self.run_id,
                task_id=self.task_id,
                tester_id=self.tester_id,
                browser_session_id=session.session_id,
                event_type="CURRENT_PATH_STOPPED",
                url=current_page_state.url,
                tool="WebTestingRuntime",
                action="stop_current_path",
                result={"reason": "LAYA_SELECTED_STOP"},
                latency_ms=0,
            )
            return DecisionResult(
                source="LAYA", reason="STOP_CURRENT_PATH", candidate=candidate
            )
        assert validation.action is not None
        action_result = await self.execute_known_action(validation.action)
        if action_result.success:
            self.store.record_path(
                run_id=self.run_id,
                feature=current_goal,
                page=page_state.url,
                state=page_state.state_id,
                action=candidate.action,
                result="SUCCESS",
                last_tester=self.tester_id,
            )
        return DecisionResult(
            source="LAYA_TO_PLAYWRIGHT",
            reason="CANDIDATE_EXECUTED",
            candidate=candidate,
            action_result=action_result,
        )

    def save_checkpoint(self, *, url: str, last_action: str) -> dict[str, object]:
        if self.browser_session_id is None:
            raise RuntimeError("browser session has not started")
        return self.store.append_event(
            event_id=f"event-{uuid4().hex}",
            run_id=self.run_id,
            task_id=self.task_id,
            tester_id=self.tester_id,
            browser_session_id=self.browser_session_id,
            event_type="SAFE_CHECKPOINT",
            url=url,
            tool="WebTestingRuntime",
            action="save_checkpoint",
            result={"url": url, "last_action": last_action},
            latency_ms=0,
        )

    async def recover_after_browser_crash(
        self, login_handler: LoginHandler | None = None
    ) -> Mapping[str, object] | None:
        if self.browser_session_id is None:
            raise RuntimeError("browser session has not started")
        checkpoint = self.store.get_latest_checkpoint(
            run_id=self.run_id, task_id=self.task_id
        )
        if checkpoint is None:
            await self.stop_task("BROWSER_CRASH_WITHOUT_CHECKPOINT")
            return None
        new_session = await self.browser_manager.recreate_session(
            self.browser_session_id, login_handler
        )
        self.browser_session_id = new_session.session_id
        checkpoint_url = checkpoint["result"]["url"]
        result = await self.execute_known_action(
            WebAction(action_type=ActionType.NAVIGATION, url=checkpoint_url)
        )
        if not result.success:
            await self.stop_task("BROWSER_RECOVERY_FAILED")
            return None
        return {
            "browser_session_id": new_session.session_id,
            "checkpoint": checkpoint["result"],
        }

    async def note_progress(
        self, *, progressed: bool, summary: str
    ) -> dict[str, object]:
        event = self.store.append_event(
            event_id=f"event-{uuid4().hex}",
            run_id=self.run_id,
            task_id=self.task_id,
            tester_id=self.tester_id,
            browser_session_id=self.browser_session_id,
            event_type="TASK_PROGRESS",
            tool="TesterAgent",
            action="update_progress",
            result={"progressed": progressed, "summary": summary},
            latency_ms=0,
        )
        if self.budget.note_progress(progressed):
            await self.request_replan("CONSECUTIVE_NO_PROGRESS")
        return event

    async def request_replan(self, reason: str) -> dict[str, object]:
        try:
            self.budget.ensure_can_start("replan")
        except BudgetExceededError as error:
            await self.stop_task(error.reason)
            return {"stopped": True, "reason": error.reason}
        self.budget.record_replan()
        return self.store.append_event(
            event_id=f"event-{uuid4().hex}",
            run_id=self.run_id,
            task_id=self.task_id,
            tester_id=self.tester_id,
            browser_session_id=self.browser_session_id,
            event_type="REQUEST_REPLAN",
            tool="TesterAgent",
            action="request_replan",
            result={"reason": reason},
            latency_ms=0,
        )

    async def stop_task(self, reason: str) -> None:
        self.store.append_event(
            event_id=f"event-{uuid4().hex}",
            run_id=self.run_id,
            task_id=self.task_id,
            tester_id=self.tester_id,
            browser_session_id=self.browser_session_id,
            event_type="STOP_CONDITION",
            tool="WebTestingRuntime",
            action="stop_task",
            result={"reason": reason, "budget": self.budget.snapshot()},
            latency_ms=0,
        )
        task = self.store.get_task(self.task_id)
        if task is not None and task["status"] in {
            "PENDING",
            "RUNNING",
            "BLOCKED",
            "WAITING_FOR_DATA",
        }:
            self.store.update_task_status(
                task_id=self.task_id,
                expected_status=task["status"],
                new_status="STOPPED",
            )
        if self.browser_session_id is not None:
            await self.browser_manager.close_session(self.browser_session_id)
            self.browser_session_id = None

    async def finish_task(self, status: str = "COMPLETED") -> None:
        task = self.store.get_task(self.task_id)
        if task is not None and task["status"] != status:
            self.store.update_task_status(
                task_id=self.task_id, expected_status=task["status"], new_status=status
            )
        if self.browser_session_id is not None:
            await self.browser_manager.close_session(self.browser_session_id)
            self.browser_session_id = None

    def _record_environment_issue(self, error: str) -> None:
        self.store.create_finding(
            finding_id=f"finding-{uuid4().hex}",
            run_id=self.run_id,
            task_id=self.task_id,
            title="Page timeout",
            status="ENVIRONMENT_ISSUE",
            expected_result="Page responds within the configured timeout",
            actual_result=error,
            first_seen_by=self.tester_id,
            needs_confirmation=False,
        )

    @staticmethod
    def _stopped_result(reason: str) -> ActionResult:
        timestamp = datetime.now(UTC).isoformat()
        return ActionResult(
            started_at=timestamp,
            ended_at=timestamp,
            latency_ms=0,
            success=False,
            error=reason,
            error_type="BUDGET_EXCEEDED",
        )
