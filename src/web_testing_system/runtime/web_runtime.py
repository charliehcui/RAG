"""Safe orchestration of browser actions, Jev selection, and local recovery."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin
from uuid import uuid4

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from web_testing_system.evidence import EvidenceBuffer, EvidenceStore
from web_testing_system.observability import trace_result, trace_span
from web_testing_system.runtime.browser import BrowserManager, LoginHandler
from web_testing_system.runtime.budget import BudgetExceededError, BudgetGuard
from web_testing_system.runtime.candidates import CandidateBuilder, PageStateReader
from web_testing_system.runtime.computer_use import (
    ComputerUseController,
    ComputerUseResult,
)
from web_testing_system.runtime.jev_selector import JevSelector
from web_testing_system.runtime.models import (
    ActionCandidate,
    ActionResult,
    ActionType,
    DecisionResult,
    PageState,
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
        jev_selector: JevSelector,
        budget: BudgetGuard,
        run_id: str,
        task_id: str,
        tester_id: str,
        identity_id: str,
        budget_id: str,
        jev_confidence_threshold: float = 0.6,
        max_timeout_retries: int = 1,
        computer_use_controller: ComputerUseController | None = None,
        input_values: Mapping[str, str] | None = None,
        expected_behavior_ids: tuple[str, ...] = (),
        evidence_store: EvidenceStore | None = None,
    ) -> None:
        self.store = store
        self.browser_manager = browser_manager
        self.executor = executor
        self.page_state_reader = page_state_reader
        self.candidate_builder = candidate_builder
        self.jev_selector = jev_selector
        self.budget = budget
        self.run_id = run_id
        self.task_id = task_id
        self.tester_id = tester_id
        self.identity_id = identity_id
        self.budget_id = budget_id
        self.jev_confidence_threshold = jev_confidence_threshold
        self.max_timeout_retries = max_timeout_retries
        self.computer_use_controller = computer_use_controller
        self.browser_session_id: str | None = None
        self.page_lock = asyncio.Lock()
        self.input_values = dict(input_values or {})
        self.expected_behavior_ids = expected_behavior_ids
        self.evidence_store = evidence_store
        self.evidence_buffer = EvidenceBuffer()
        self.evidence_buffer.identity_reference = getattr(executor, "identity_reference", None)
        self.evidence_marker = (0, 0)
        self.task_finished = False
        self.assertion_targets: dict[tuple[str, str], dict[str, str | None]] = {}

    def remember_assertion_targets(self, rows: Sequence[Mapping[str, Any]], context: str = "") -> None:
        for row in rows:
            aliases = {str(row["target"]), str(row["text"])}
            aliases.update(str(cell["text"]) for cell in row["cells"] if cell["text"])
            for reference, value in self.input_values.items():
                if not reference.startswith("env:") and value.strip() and self._row_matches_context(row, value):
                    aliases.add(value)
            if context and self._row_matches_context(row, context):
                aliases.add(context)
            aliases = {alias for alias in aliases if self._row_matches_context(row, alias)}
            for cell in row["cells"]:
                if not cell["header"]:
                    continue
                for alias in aliases:
                    key = (str(cell["header"]).casefold(), alias.casefold())
                    self.assertion_targets.setdefault(key, {})[str(cell["target"])] = row.get("container_target")

    def _row_matches_context(self, row: Mapping[str, Any], context: str) -> bool:
        if context == row["target"] or f"={json.dumps(context)}]" in str(row["target"]):
            return True
        normalized = " ".join(context.casefold().split())
        username = any(not reference.startswith("env:") and "username" in reference.casefold() and " ".join(value.casefold().split()) == normalized for reference, value in self.input_values.items())
        username_cells = [cell for cell in row["cells"] if "username" in str(cell["header"]).casefold()]
        if username and username_cells:
            return any(" ".join(str(cell["text"]).casefold().split()) == normalized for cell in username_cells)
        configured = any(not reference.startswith("env:") and " ".join(value.casefold().split()) == normalized for reference, value in self.input_values.items())
        if configured:
            return any(" ".join(str(cell["text"]).casefold().split()) == normalized for cell in row["cells"])
        return normalized in " ".join(str(row["text"]).casefold().split())

    async def start_session(self) -> str:
        try:
            session = await self.browser_manager.create_session(
                tester_id=self.tester_id, identity_id=self.identity_id
            )
        except BudgetExceededError as error:
            await self.stop_task(error.reason)
            raise
        self.browser_session_id = session.session_id
        if self.evidence_store is not None:
            self.evidence_buffer.attach(session.page)
        return session.session_id

    async def execute_known_action(self, action: WebAction) -> ActionResult:
        async with self.page_lock:
            if self.task_finished:
                return self._stopped_result("TASK_ALREADY_FINISHED")
            return await self._execute_known_action(action)

    async def _execute_known_action(self, action: WebAction) -> ActionResult:
        if action.action_type in {ActionType.ASSERTION, ActionType.URL_CHECK} and action.goal_check and action.behavior_id is None and len(self.expected_behavior_ids) == 1:
            action = replace(action, behavior_id=self.expected_behavior_ids[0])
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
        async with self.page_lock:
            if self.task_finished:
                return DecisionResult(source="STOP", reason="TASK_ALREADY_FINISHED")
            return await self._explore_unknown_path(current_goal)

    async def _explore_unknown_path(self, current_goal: str) -> DecisionResult:
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
        return await self._select_and_execute(current_goal, page_state, candidates)

    async def execute_page_action(self, action: WebAction, *, control: str, context: str = "") -> DecisionResult:
        """Resolve a semantic control locally or with Jev, then execute its allowed action."""
        async with self.page_lock:
            if self.task_finished:
                return DecisionResult(source="STOP", reason="TASK_ALREADY_FINISHED")
            if self.browser_session_id is None:
                raise RuntimeError("browser session has not started")
            session = self.browser_manager.get_session(self.browser_session_id)
            try:
                await session.page.wait_for_load_state("networkidle", timeout=action.timeout_ms)
            except PlaywrightTimeoutError:
                return DecisionResult(source="TESTER_LLM", reason="PAGE_NOT_SETTLED", needs_tester_llm=True)
            page_state = await self.page_state_reader.read(session.page, include_content=action.action_type == ActionType.ASSERTION)
            rows = await self.page_state_reader.read_rows(session.page)
            self.remember_assertion_targets(rows, context)
            kinds = {ActionType.INPUT: {"input", "textarea"}, ActionType.SELECT: {"select"}, ActionType.REPEAT_SUBMIT: {"button"}, ActionType.ASSERTION: {"cell"}}
            candidates = []
            exact = []
            for element in page_state.interactive_elements:
                matches_context = not context or context.casefold() in element.context.casefold() or context == element.context_target or f"={json.dumps(context)}]" in (element.context_target or "")
                row = next((row for row in rows if row["target"] == element.context_target or element.context_target is not None and str(row["target"]).endswith(" " + element.context_target) or row["text"] == element.context), None)
                if context and row is not None:
                    matches_context = self._row_matches_context(row, context)
                if not element.enabled or not matches_context:
                    continue
                if action.action_type in kinds and element.kind not in kinds[action.action_type]:
                    continue
                if action.action_type == ActionType.CLICK and element.kind in {"input", "textarea", "select", "cell"}:
                    continue
                resolved_url = action.url
                if action.action_type == ActionType.CLICK and element.href:
                    resolved_url = urljoin(page_state.url, element.href)
                resolved = replace(action, target=element.target, url=resolved_url)
                permission = self.executor.permission_checker.check(resolved, current_url=page_state.url)
                if not permission.allowed:
                    continue
                candidate = ActionCandidate(candidate_id=self.candidate_builder._candidate_id(page_state.state_id, action.action_type.value, element.target), action=action.action_type.value, label=f"{element.label} ({element.context})", target=element.target, state_id=page_state.state_id, value=action.value, value_reference=action.value_reference, url=resolved_url, resource_id=action.resource_id, requires_resource=action.requires_resource, confirmed=action.confirmed, expected=action.expected, assertion=action.assertion, behavior_id=action.behavior_id, goal_check=action.goal_check)
                candidates.append(candidate)
                if element.label.casefold() == control.casefold():
                    exact.append(candidate)
            if len(exact) == 1:
                result = await self._execute_known_action(replace(action, target=exact[0].target, url=exact[0].url))
                return DecisionResult(source="PLAYWRIGHT", reason="EXACT_CONTROL", candidate=exact[0], action_result=result)
            if not candidates:
                known = self.assertion_targets.get((control.casefold(), context.casefold()), {})
                if action.action_type == ActionType.ASSERTION and len(known) == 1:
                    target, container = next(iter(known.items()))
                    if container is None or await session.page.locator(container).is_visible():
                        # 删除后缺失的单元格仍须用此前观察到的定位器做真实断言。
                        result = await self._execute_known_action(replace(action, target=target))
                        candidate = ActionCandidate(candidate_id="recorded-assertion", action=action.action_type.value, label=control, target=target, state_id=page_state.state_id)
                        return DecisionResult(source="PLAYWRIGHT", reason="RECORDED_ASSERTION_TARGET", candidate=candidate, action_result=result)
                return DecisionResult(source="TESTER_LLM", reason="CONTROL_NOT_FOUND", needs_tester_llm=True)
            choices = (exact or candidates) + self.candidate_builder._control_candidates(page_state.state_id)
            return await self._select_and_execute(f"{action.action_type.value} the control '{control}' in '{context}'; stop if no control fits", page_state, choices)

    async def _select_and_execute(self, current_goal: str, page_state: PageState, candidates: list[ActionCandidate]) -> DecisionResult:
        assert self.browser_session_id is not None
        session = self.browser_manager.get_session(self.browser_session_id)
        try:
            self.budget.ensure_can_start("jev")
        except BudgetExceededError as error:
            await self.stop_task(error.reason)
            return DecisionResult(source="STOP", reason=error.reason)
        run = self.store.get_run(self.run_id)
        if run is None:
            raise KeyError(f"unknown run: {self.run_id}")
        remaining = run["remaining_budget"]
        if remaining.get("max_jev_calls", self.budget.limits.max_jev_calls) <= 0:
            await self.stop_task("MAX_JEV_CALLS_REACHED")
            return DecisionResult(source="STOP", reason="MAX_JEV_CALLS_REACHED")
        if remaining.get("max_input_tokens", self.budget.limits.max_input_tokens) <= 0 or remaining.get("max_output_tokens", self.budget.limits.max_output_tokens) <= 0:
            await self.stop_task("MAX_JEV_TOKENS_REACHED")
            return DecisionResult(source="STOP", reason="MAX_JEV_TOKENS_REACHED")
        self.store.update_budget(budget_id=self.budget_id, jev_calls=1)
        with trace_span("Jev", "llm", metadata={"agent_role": "jev", "phase": "jev", "model": getattr(self.jev_selector.client, "model", "typesafe/jev-1.13"), "provider": "openrouter", "ls_model_name": getattr(self.jev_selector.client, "model", "typesafe/jev-1.13"), "ls_provider": "openrouter"}) as span:
            selection = await self.jev_selector.select(current_goal=current_goal, page_state=page_state, candidates=candidates)
            trace_result(span, input_tokens=selection.input_tokens, output_tokens=selection.output_tokens, cost=selection.cost, success=selection.error is None, error_type=selection.error)
        self.budget.record_jev_call(
            runtime_seconds=selection.latency_ms / 1_000,
            cost=selection.cost,
            input_tokens=selection.input_tokens,
            output_tokens=selection.output_tokens,
        )
        self.store.update_budget(
            budget_id=self.budget_id,
            input_tokens=selection.input_tokens,
            output_tokens=selection.output_tokens,
            runtime_seconds=selection.latency_ms / 1_000,
            estimated_cost=selection.cost,
        )
        self.store.append_event(
            event_id=f"event-{uuid4().hex}",
            run_id=self.run_id,
            task_id=self.task_id,
            tester_id=self.tester_id,
            browser_session_id=session.session_id,
            event_type="JEV_CALL",
            url=page_state.url,
            tool="Jev",
            action="select_candidate",
            result={
                "selected_candidate_id": selection.selected_candidate_id,
                "confidence": selection.confidence,
                "error": selection.error,
                "model": selection.model,
                "input_tokens": selection.input_tokens,
                "output_tokens": selection.output_tokens,
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
        if candidate is None:
            return DecisionResult(
                source="TESTER_LLM",
                reason="ILLEGAL_CANDIDATE_ID",
                needs_tester_llm=True,
            )
        if selection.confidence < self.jev_confidence_threshold:
            return DecisionResult(
                source="TESTER_LLM", reason="LOW_JEV_CONFIDENCE", needs_tester_llm=True
            )
        current_page_state = await self.page_state_reader.read(session.page, include_content=candidate.action == ActionType.ASSERTION.value)
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
            await self.request_replan("JEV_REQUESTED_REPLAN")
            return DecisionResult(
                source="JEV", reason="REQUEST_REPLAN", candidate=candidate
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
                result={"reason": "JEV_SELECTED_STOP"},
                latency_ms=0,
            )
            return DecisionResult(
                source="JEV", reason="STOP_CURRENT_PATH", candidate=candidate
            )
        assert validation.action is not None
        action_result = await self._execute_known_action(validation.action)
        if action_result.success:
            self.store.record_path(
                run_id=self.run_id,
                feature=current_goal,
                page=page_state.url,
                page_state_id=page_state.state_id,
                action=candidate.action,
                result="SUCCESS",
                last_tester=self.tester_id,
            )
        return DecisionResult(
            source="JEV_TO_PLAYWRIGHT",
            reason="CANDIDATE_EXECUTED",
            candidate=candidate,
            action_result=action_result,
        )

    async def use_computer_fallback(self, *, finding_id: str, goal: str, component_type: str, playwright_failure_reason: str, allowed_visual_actions: tuple[str, ...]) -> ComputerUseResult:
        async with self.page_lock:
            if self.task_finished:
                return ComputerUseResult("STOPPED", "TASK_ALREADY_FINISHED", None, None, None, ())
            return await self._use_computer_fallback(finding_id=finding_id, goal=goal, component_type=component_type, playwright_failure_reason=playwright_failure_reason, allowed_visual_actions=allowed_visual_actions)

    async def _use_computer_fallback(self, *, finding_id: str, goal: str, component_type: str, playwright_failure_reason: str, allowed_visual_actions: tuple[str, ...]) -> ComputerUseResult:
        if self.computer_use_controller is None:
            return ComputerUseResult("REFUSED", "COMPUTER_USE_NOT_CONFIGURED", None, None, None, ())
        if self.browser_session_id is None:
            raise RuntimeError("browser session has not started")
        session = self.browser_manager.get_session(self.browser_session_id)
        return await self.computer_use_controller.run(page=session.page, browser_session_id=session.session_id, finding_id=finding_id, goal=goal, component_type=component_type, playwright_failure_reason=playwright_failure_reason, allowed_visual_actions=allowed_visual_actions)

    async def capture_finding_evidence(self, finding_id: str) -> None:
        if self.evidence_store is None or self.browser_session_id is None:
            return
        async with self.page_lock:
            if self.store.list_evidence(run_id=self.run_id, finding_id=finding_id):
                return
            session = self.browser_manager.get_session(self.browser_session_id)
            await self.evidence_buffer.flush()
            identifiers = {"run_id": self.run_id, "task_id": self.task_id, "finding_id": finding_id, "attempt_id": f"observation-{uuid4().hex}", "browser_session_id": session.session_id}
            with trace_span("Evidence", "tool", metadata={"finding_id": finding_id}):
                await self.evidence_store.capture_screenshot(page=session.page, name="observed", **identifiers)
                await self.evidence_store.capture_dom(page=session.page, **identifiers)
                self.evidence_store.capture_network(buffer=self.evidence_buffer, marker=self.evidence_marker, url=session.page.url, **identifiers)
                self.evidence_store.capture_console(buffer=self.evidence_buffer, marker=self.evidence_marker, url=session.page.url, **identifiers)
            self.evidence_marker = self.evidence_buffer.mark()

    async def record_task_outcome(self, *, finish: bool = False) -> dict[str, object]:
        """Only goal assertions establish success; ordinary completion text establishes nothing."""
        async with self.page_lock:
            histories = self.store.list_action_history(run_id=self.run_id, task_id=self.task_id)
            latest: dict[str, dict[str, object]] = {}
            for history in histories:
                action = history["action_data"]
                if history["browser_session_id"] != self.browser_session_id or action.get("action_type") not in {"assertion", "url_check"}:
                    continue
                behavior_id = action.get("behavior_id")
                if not action.get("goal_check") and behavior_id not in self.expected_behavior_ids:
                    continue
                key = str(history["event_id"])
                latest[key] = {"event_id": history["event_id"], "behavior_id": behavior_id, "success": bool(history["success"]), "error_type": history["result"].get("error_type")}
            results = list(latest.values())
            covered = {item["behavior_id"] for item in results}
            execution_failed = any(not history["success"] and history["result"].get("error_type") != "ASSERTION_FAILURE" for history in histories if history["browser_session_id"] == self.browser_session_id)
            failed_assertions = [item for item in results if not item["success"]]
            deviations: set[str] = set()
            reported_behaviors: set[str] = set()
            for event in self.store.list_events(self.run_id, event_types=("FINDING_CREATED",), task_id=self.task_id):
                finding = self.store.get_finding(str(event["result"].get("finding_id")))
                if finding is not None and finding["expected_result"].strip() and finding["actual_result"].strip():
                    deviations.add(str(event["result"].get("boundary_event_id")))
                    behavior = event["result"].get("behavior_id")
                    if behavior is None:
                        behavior = next((history["action_data"].get("behavior_id") for history in histories if history["event_id"] == event["result"].get("boundary_event_id")), None)
                    if behavior is not None:
                        reported_behaviors.add(str(behavior))
            if execution_failed:
                status, reason = "FAIL", "AGENT_EXECUTION_FAILURE"
            elif not results or not set(self.expected_behavior_ids).issubset(covered):
                status, reason = "UNKNOWN", "GOAL_ASSERTION_MISSING"
            elif failed_assertions and not all(item["event_id"] in deviations or item["behavior_id"] in reported_behaviors for item in failed_assertions):
                status, reason = "UNKNOWN", "APPLICATION_DEVIATION_UNREPORTED"
            elif failed_assertions:
                status, reason = "PASS", "APPLICATION_BUG_DETECTED"
            else:
                status, reason = "PASS", "GOAL_ASSERTIONS_PASSED"
            application_behavior = "UNKNOWN" if execution_failed else "FAIL" if failed_assertions else "PASS" if status == "PASS" else "UNKNOWN"
            self.store.record_task_outcome(task_id=self.task_id, success_status=status, reason=reason, assertion_results=results)
            self.store.append_event(event_id=f"event-{uuid4().hex}", run_id=self.run_id, task_id=self.task_id, tester_id=self.tester_id, event_type="TASK_OUTCOME", tool="Python", action="evaluate_goal_assertions", result={"success_status": status, "reason": reason, "application_behavior": application_behavior, "assertions": results}, latency_ms=0)
            if finish:
                self.task_finished = True
            return {"success_status": status, "reason": reason, "application_behavior": application_behavior, "assertions": results}

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
        self.task_finished = True
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
        if task is not None and task["success_status"] == "UNKNOWN":
            self.store.record_task_outcome(task_id=self.task_id, success_status="UNKNOWN", reason=reason, assertion_results=task["assertion_results"])
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
