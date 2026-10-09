"""Safe orchestration of browser actions, Jev selection, and local recovery."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from time import monotonic
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
    BusinessAction,
    DecisionResult,
    InteractiveElement,
    PageState,
    WebAction,
)
from web_testing_system.runtime.playwright_executor import PlaywrightExecutor
from web_testing_system.scoring import score_task
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
        task = store.get_task(task_id)
        self.required_checks = (task or {}).get("data_requirements", {}).get("required_checks", [])
        self.evidence_store = evidence_store
        self.evidence_buffer = EvidenceBuffer()
        self.evidence_buffer.identity_reference = getattr(executor, "identity_reference", None)
        self.evidence_marker = (0, 0)
        self.task_finished = False
        self.assertion_targets: dict[tuple[str, str], dict[str, str | None]] = {}
        self.object_targets: dict[tuple[str, str | None, str | None], str] = {}
        self.object_projects: dict[tuple[str, str | None, str | None], dict[str, str]] = {}
        self.object_identities: dict[tuple[str, str | None], dict[str, str]] = {}
        self.project_ids: dict[str, str] = {}
        self.last_project_reference: str | None = None
        self.input_targets: dict[str, str] = {}
        self.active_project: dict[str, str] | None = None
        self.modal_row: str | None = None
        self.last_decision_state_id: str | None = None
        self.current_object: dict[str, Any] = {}
        for history in store.list_action_history(run_id=run_id, task_id=task_id):
            action = history["action_data"]
            if history["success"] and action.get("action_type") in {"input", "select"} and action.get("target") and action.get("value_reference"):
                self.input_targets[action["target"]] = action["value_reference"]
        for event in store.list_events(run_id, task_id=task_id, event_types=("OBJECT_BOUND",)):
            binding = event["result"]
            project_binding = binding.get("project_binding")
            key = (binding["reference"].casefold(), binding.get("container_target"), project_binding["value"] if project_binding else None)
            self.object_targets[key] = binding["target"]
            if binding.get("identity"):
                self.object_identities[(key[0], key[2])] = binding["identity"]
            if binding.get("identity", {}).get("data-project-id"):
                self.project_ids[binding["reference"]] = binding["identity"]["data-project-id"]
            if binding.get("project_binding"):
                self.object_projects[key] = binding["project_binding"]
                self.project_ids[binding["project_binding"]["reference"]] = binding["project_binding"]["value"]
            for cell in binding.get("cells", []):
                self.assertion_targets.setdefault((cell["header"].casefold(), binding["reference"].casefold()), {})[cell["target"]] = binding.get("container_target")
        self.prepared_check_ids = {check_id for event in store.list_events(run_id, task_id=task_id, event_types=("PLAN_GOAL_PREPARED",)) for check_id in event["result"].get("check_ids", [])}
        for event in store.list_events(run_id, task_id=task_id, event_types=("PROJECT_BOUND",)):
            self.project_ids[event["result"]["reference"]] = event["result"]["object_id"]
            self.last_project_reference = event["result"]["reference"]

    def remember_assertion_targets(self, rows: Sequence[Mapping[str, Any]], context: str = "") -> None:
        for row in rows:
            aliases = {str(row["target"]), str(row["text"])}
            aliases.update(str(cell["text"]) for cell in row["cells"] if cell["text"])
            for reference, value in self.input_values.items():
                if not reference.startswith("env:") and value.strip() and self._row_matches_context(row, value):
                    aliases.add(value)
                    aliases.add(reference)
            if context and self._row_matches_context(row, context):
                aliases.add(context)
            references = {reference.casefold(): reference for reference in self.input_values if not reference.startswith("env:")}
            aliases = {alias for alias in aliases if self._row_matches_context(row, references.get(alias.casefold(), alias))}
            for cell in row["cells"]:
                if not cell["header"]:
                    continue
                for alias in aliases:
                    key = (str(cell["header"]).casefold(), alias.casefold())
                    targets = self.assertion_targets.setdefault(key, {})
                    for old_target in list(targets):
                        if old_target.startswith(str(row["target"]) + " >>") and old_target != str(cell["target"]):
                            targets.pop(old_target)
                    self.assertion_targets.setdefault(key, {})[str(cell["target"])] = row.get("container_target")

    def _row_matches_context(self, row: Mapping[str, Any], context: str) -> bool:
        project_value = self.active_project["value"] if self.active_project else None
        identity = self.object_identities.get((context.casefold(), project_value))
        if identity and row.get("identity"):
            return all(row["identity"].get(attribute) == value for attribute, value in identity.items())
        bound = self.object_targets.get((context.casefold(), row.get("container_target"), project_value))
        if bound is not None:
            return str(row["target"]) == bound
        if not context.startswith("env:") and context in self.input_values:
            context = self.input_values[context]
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

    def _bind_rows(self, rows: Sequence[Mapping[str, Any]], context: str) -> None:
        if not context:
            return
        containers = {row.get("container_target") for row in rows}
        for container in containers:
            matches = [row for row in rows if row.get("container_target") == container and self._row_matches_context(row, context)]
            if len(matches) == 1 and " >> nth=" not in str(matches[0]["target"]):
                key = (context.casefold(), container, self.active_project["value"] if self.active_project else None)
                if key not in self.object_targets:
                    row = matches[0]
                    self.object_targets[key] = str(row["target"])
                    if row.get("identity"):
                        self.object_identities[(context.casefold(), key[2])] = dict(row["identity"])
                    if row.get("identity", {}).get("data-project-id"):
                        self.project_ids[context] = row["identity"]["data-project-id"]
                    if self.active_project:
                        self.object_projects[key] = dict(self.active_project)
                    self.store.append_event(event_id=f"event-{uuid4().hex}", run_id=self.run_id, task_id=self.task_id, tester_id=self.tester_id, event_type="OBJECT_BOUND", tool="WebTestingRuntime", action="bind_row", result={"reference": context, "container_target": container, "target": row["target"], "identity": row.get("identity", {}), "project_binding": self.active_project, "cells": [{"header": cell["header"], "target": cell["target"]} for cell in row["cells"]]}, latency_ms=0)

    def _project_options(self, element: InteractiveElement, reference: str | None) -> list[tuple[str, str]]:
        stable_id = self.project_ids.get(reference or "")
        if stable_id is not None:
            return [(label, value) for label, value in element.options if value == stable_id]
        configured = self.input_values.get(reference or "")
        return [(label, value) for label, value in element.options if configured in {label, value}]

    def _remember_project(self, reference: str, target: str, value: str) -> None:
        changed = self.last_project_reference != reference or self.project_ids.get(reference) != value
        self.project_ids[reference] = value
        self.last_project_reference = reference
        if changed:
            self.store.append_event(event_id=f"event-{uuid4().hex}", run_id=self.run_id, task_id=self.task_id, tester_id=self.tester_id, event_type="PROJECT_BOUND", tool="WebTestingRuntime", action="bind_project", result={"reference": reference, "object_id": value, "target": target}, latency_ms=0)

    def validate_assertion_contract(self, action: WebAction, *, control: str | None = None, context: str = "") -> dict[str, Any] | None:
        if action.action_type not in {ActionType.ASSERTION, ActionType.URL_CHECK}:
            return None
        reason = None
        if action.action_type == ActionType.ASSERTION and not (action.target and action.target.strip() or control and control.strip()):
            reason = "ASSERTION_TARGET_REQUIRED"
        elif action.action_type == ActionType.URL_CHECK and action.expected is None or action.action_type == ActionType.ASSERTION and action.assertion in {"contains", "not_contains", "equals", "count"} and action.expected is None:
            reason = "ASSERTION_EXPECTED_STATE_REQUIRED"
        if reason is None:
            return None
        failure = {"success": False, "reason": reason, "check_id": action.check_id, "target": action.target, "control": control, "object": {"context": context, "project_reference": action.project_reference}, "expected_state": action.assertion}
        self.store.append_event(event_id=f"event-{uuid4().hex}", run_id=self.run_id, task_id=self.task_id, tester_id=self.tester_id, event_type="ASSERTION_CONTRACT_FAILED", tool="WebTestingRuntime", action="validate_assertion", result=failure, latency_ms=0)
        return failure

    def validate_plan_assertions(self, goals: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
        for goal in goals:
            for step in [*goal.get("before_steps", []), *goal.get("checks", []), *goal.get("after_steps", [])]:
                action = WebAction(action_type=ActionType(step["action_type"]), target=step.get("target"), assertion=step.get("assertion", "contains"), expected=self.input_values.get(step["expected_reference"]) if step.get("expected_reference") else step.get("expected"), check_id=step.get("check_id"), behavior_id=step.get("behavior_id"), project_reference=step.get("project_reference") or goal.get("project_reference"))
                failure = self.validate_assertion_contract(action, control=step.get("control"), context=step.get("context") or goal.get("row_reference") or goal.get("context", ""))
                if failure:
                    return failure
        return None

    def register_progress_plan(self, goals: Sequence[Mapping[str, Any]]) -> None:
        signals = [{"name": goal["publish_progress"], "check_ids": [step["check_id"] for step in [*goal.get("before_steps", []), *goal.get("checks", []), *goal.get("after_steps", [])] if step.get("check_id")]} for goal in goals if goal.get("publish_progress")]
        self.store.append_event(event_id=f"event-{uuid4().hex}", run_id=self.run_id, task_id=self.task_id, tester_id=self.tester_id, event_type="RUNTIME_SIGNAL_PLAN", tool="WebTestingRuntime", action="register_signals", result={"signals": signals}, latency_ms=0)

    async def wait_for_shared_progress(self, signal: str, *, task_id: str | None = None, timeout_seconds: int = 30) -> dict[str, Any]:
        """Retain a live continuation until its producer signals, closes, or exhausts the original run budget."""
        updates = self.store.subscribe_task_updates(self.run_id)
        wait_recorded = False
        started = monotonic()
        try:
            while True:
                updates.clear()
                tasks = [task for task in self.store.list_tasks(self.run_id) if task["task_id"] != self.task_id and (task_id is None or task["task_id"] == task_id)]
                plans = {event["task_id"]: event["result"] for event in self.store.list_events(self.run_id, event_types=("RUNTIME_SIGNAL_PLAN",))}
                declared = {producer_id for producer_id, plan in plans.items() if any(item["name"] == signal for item in plan["signals"])}
                dependencies = {dependency for check in self.required_checks for dependency in check.get("depends_on", [])} - {check["check_id"] for check in self.required_checks}
                owners = {task["task_id"] for task in tasks if any(check["check_id"] in dependencies for check in task["data_requirements"].get("required_checks", []))}
                producers = [task for task in tasks if (not owners or task["task_id"] in owners) and (not declared or task["task_id"] in declared)]
                producer_ids = {task["task_id"] for task in producers}
                for event in self.store.list_events(self.run_id, event_types=("TASK_PROGRESS",)):
                    if event["task_id"] in producer_ids and event["result"].get("progressed") is not False and str(event["result"].get("summary", "")).strip() == signal.strip():
                        return {"ready": True, "task_id": event["task_id"], "event_id": event["event_id"]}
                if producers and all(task["task_id"] in plans for task in producers) and not declared:
                    return {"ready": False, "reason": "PROGRESS_SIGNAL_UNDECLARED", "producer_task_ids": sorted(producer_ids), "signal": signal}
                if producers and all(task["status"] == "PENDING" and self.task_id in task["dependencies"] for task in producers):
                    await self.request_replan("LIVE_PARTICIPANT_DEPENDS_ON_OBSERVER")
                    await self.stop_task("LIVE_PARTICIPANT_DEPENDS_ON_OBSERVER")
                    return {"ready": False, "reason": "LIVE_PARTICIPANT_DEPENDS_ON_OBSERVER"}
                if not producers or all(task["status"] in {"COMPLETED", "FAILED", "STOPPED", "CANCELLED"} for task in producers):
                    return {"ready": False, "reason": "PROGRESS_PRODUCER_CLOSED" if producers else "PROGRESS_PRODUCER_UNAVAILABLE", "producer_task_ids": sorted(producer_ids), "signal": signal}
                if timeout_seconds == 0:
                    return {"ready": False, "reason": "PROGRESS_SIGNAL_NOT_AVAILABLE", "signal": signal}
                if not wait_recorded:
                    self.store.append_event(event_id=f"event-{uuid4().hex}", run_id=self.run_id, task_id=self.task_id, tester_id=self.tester_id, event_type="RUNTIME_PROGRESS_WAIT", tool="WebTestingRuntime", action="wait", result={"signal": signal, "producer_task_ids": sorted(producer_ids), "status": "WAITING"}, latency_ms=0)
                    wait_recorded = True
                    continue
                waits = {event["task_id"]: event["result"] for event in self.store.list_events(self.run_id, event_types=("RUNTIME_PROGRESS_WAIT",))}
                published = self.store.list_events(self.run_id, event_types=("TASK_PROGRESS",))
                blocked = []
                for producer in producers:
                    waiting = waits.get(producer["task_id"], {})
                    available = any(event["task_id"] in waiting.get("producer_task_ids", []) and event["result"].get("progressed") is not False and event["result"].get("summary") == waiting.get("signal") for event in published)
                    blocked.append(waiting.get("status") == "WAITING" and self.task_id in waiting.get("producer_task_ids", []) and not available)
                if producers and all(blocked):
                    return {"ready": False, "reason": "LIVE_PROGRESS_DEADLOCK", "signal": signal}
                try:
                    self.budget.ensure_runtime_available()
                    remaining = self.budget.limits.max_runtime_seconds - (monotonic() - self.budget.started_at)
                    run = self.store.get_run(self.run_id)
                    if run and "max_runtime_seconds" in run["global_budget"]:
                        remaining = min(remaining, run["global_budget"]["max_runtime_seconds"] - (datetime.now(UTC) - datetime.fromisoformat(run["started_at"])).total_seconds())
                    if not dependencies and not declared:
                        remaining = min(remaining, max(0, min(timeout_seconds, 60) - (monotonic() - started)))
                    if remaining <= 0:
                        if dependencies or declared:
                            await self.stop_task("MAX_RUNTIME_REACHED")
                            return {"ready": False, "reason": "MAX_RUNTIME_REACHED"}
                        return {"ready": False, "reason": "PROGRESS_SIGNAL_NOT_AVAILABLE"}
                    await asyncio.wait_for(updates.wait(), timeout=remaining)
                except TimeoutError:
                    continue
                except BudgetExceededError as error:
                    await self.stop_task(error.reason)
                    return {"ready": False, "reason": error.reason}
        finally:
            self.store.unsubscribe_task_updates(self.run_id, updates)
            if wait_recorded:
                self.store.append_event(event_id=f"event-{uuid4().hex}", run_id=self.run_id, task_id=self.task_id, tester_id=self.tester_id, event_type="RUNTIME_PROGRESS_WAIT", tool="WebTestingRuntime", action="resume", result={"signal": signal, "status": "FINISHED"}, latency_ms=0)

    @staticmethod
    def _field_matches(element: InteractiveElement, control: str) -> bool:
        names = (element.label, element.target, *element.field_names)
        normalized = " ".join(control.casefold().split())
        return any(normalized == " ".join(name.casefold().split()) or " " not in normalized and " ".join(name.casefold().split()).endswith(" " + normalized) for name in names)

    async def _form_inputs_bound(self, elements: Sequence[InteractiveElement], submit: InteractiveElement) -> bool:
        assert self.browser_session_id is not None
        page = self.browser_manager.get_session(self.browser_session_id).page
        fields = [element for element in elements if element.enabled and element.kind in {"input", "textarea", "select"} and (element.form_target or element.context_target) == (submit.form_target or submit.context_target)]
        for element in fields:
            reference = self.input_targets.get(element.target)
            if element.kind == "select" and self.active_project and element.target == self.active_project["target"]:
                reference = self.active_project["reference"]
            if reference is None and element.kind == "select" and not element.required and "project" not in element.label.casefold():
                continue
            if reference not in self.input_values:
                return False
            value = self.input_values[reference]
            actual = await page.locator(element.target).input_value()
            if element.kind == "select":
                options = self._project_options(element, reference) if self.active_project and element.target == self.active_project["target"] else [(label, option) for label, option in element.options if value in {label, option}]
                if len(options) != 1 or actual != options[0][1]:
                    return False
            elif actual != value:
                return False
        return True

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
        failure = self.validate_assertion_contract(action)
        if failure:
            return self._stopped_result(failure["reason"])
        if action.action_type in {ActionType.INPUT, ActionType.SELECT}:
            if action.value_reference is None or action.value_reference not in self.input_values:
                return self._stopped_result("INPUT_VALUE_UNAVAILABLE")
            if action.value != self.input_values[action.value_reference]:
                return self._stopped_result("INPUT_REFERENCE_VALUE_MISMATCH")
        if self.required_checks and (action.check_id or action.action_type in {ActionType.ASSERTION, ActionType.URL_CHECK} and (action.goal_check or action.behavior_id)):
            spec = next((check for check in self.required_checks if check["check_id"] == action.check_id), None)
            expected_action = (spec or {}).get("action_type", "assertion")
            allowed_actions = {"assertion", "url_check"} if expected_action == "assertion" else {expected_action}
            if spec is None or spec["behavior_id"] != action.behavior_id or action.action_type.value not in allowed_actions:
                return self._stopped_result("UNKNOWN_OR_MISSING_CHECK_ID")
        if action.action_type in {ActionType.ASSERTION, ActionType.URL_CHECK} and action.goal_check and action.behavior_id is None and len(self.expected_behavior_ids) == 1:
            action = replace(action, behavior_id=self.expected_behavior_ids[0])
        if self.browser_session_id is None:
            raise RuntimeError("browser session has not started")
        session = self.browser_manager.get_session(self.browser_session_id)
        if action.action_type == ActionType.ASSERTION and action.target and isinstance(self.executor, PlaywrightExecutor):
            rows = await self.page_state_reader.read_rows(session.page)
            for reference, value in self.input_values.items():
                if not reference.startswith("env:") and value.strip() and ("project" in reference.casefold() or reference == action.project_reference or any(row.get("identity", {}).get("data-project-id") == value for row in rows)) and any(row.get("identity", {}).get("data-project-id") and self._row_matches_context(row, reference) for row in rows):
                    self._bind_rows(rows, reference)
            matching_projects = [reference for reference, stable_id in self.project_ids.items() if f'data-project-id={json.dumps(stable_id)}' in action.target]
            if matching_projects:
                self.last_project_reference = matching_projects[0]
        if action.action_type == ActionType.SELECT and action.target:
            locator = session.page.locator(action.target)
            if await locator.count() == 1 and await locator.evaluate("element => element.tagName === 'SELECT'"):
                options = await locator.locator("option").evaluate_all("options => options.filter(option => !option.disabled && !option.closest('optgroup')?.disabled).map(option => [option.label, option.value])")
                matching = [option for option in options if action.value in option]
                stable_id = self.project_ids.get(action.value_reference or "")
                if stable_id:
                    matching = [option for option in options if option[1] == stable_id]
                if len(matching) != 1:
                    return self._stopped_result("AMBIGUOUS_SELECT_OPTION" if matching else "SELECT_OPTION_UNAVAILABLE")
                action = replace(action, value=matching[0][1])
        if action.project_reference and action.action_type not in {ActionType.NAVIGATION, ActionType.REFRESH}:
            if action.project_reference not in self.input_values:
                return self._stopped_result("INPUT_VALUE_UNAVAILABLE")
            observed = await self.page_state_reader.read(session.page)
            project_selects = [element for element in observed.interactive_elements if element.kind == "select" and "project" in element.label.casefold()]
            if any(len(self._project_options(element, action.project_reference)) != 1 or element.selected_value != self._project_options(element, action.project_reference)[0][1] for element in project_selects):
                return self._stopped_result("OBJECT_PROJECT_MISMATCH")
        if action.action_type in {ActionType.CLICK, ActionType.REPEAT_SUBMIT} and action.target and isinstance(self.executor, PlaywrightExecutor):
            observed = await self.page_state_reader.read(session.page)
            locator = session.page.locator(action.target)
            if await locator.count() == 1:
                form_target = await locator.evaluate("""element => {
                    const form = element.form;
                    if (!form || element.type !== 'submit' || element.value === 'cancel') return null;
                    return form.id ? '[id=' + JSON.stringify(form.id) + ']' : 'form >> nth=' + Array.from(document.querySelectorAll('form')).indexOf(form);
                }""")
                if form_target:
                    submit = next((element for element in observed.interactive_elements if element.form_target == form_target and element.is_submit), None)
                    if submit is None or not await self._form_inputs_bound(observed.interactive_elements, submit):
                        return self._stopped_result("UNBOUND_REQUIRED_INPUT")
        attempts = 0
        while True:
            try:
                self.budget.ensure_can_start("browser_step")
            except BudgetExceededError as error:
                await self.stop_task(error.reason)
                return self._stopped_result(error.reason, error_type="BUDGET_EXCEEDED")
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
                if action.action_type in {ActionType.INPUT, ActionType.SELECT} and action.target and action.value_reference:
                    self.input_targets[action.target] = action.value_reference
                    element_id = await session.page.locator(action.target).get_attribute("id")
                    if element_id:
                        self.input_targets[f"[id={json.dumps(element_id)}]"] = action.value_reference
                    if action.action_type == ActionType.SELECT and "project" in (await session.page.locator(action.target).get_attribute("aria-label") or "").casefold():
                        self._remember_project(action.value_reference, action.target, await session.page.locator(action.target).input_value())
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

    async def execute_page_goal(self, goal: str, *, inputs: Sequence[Mapping[str, str]] = (), context: str = "", project_reference: str | None = None, row_reference: str | None = None, form_context: str = "", max_steps: int = 12, stop_after_inputs: bool = False, operation: str | None = None, destination: str | None = None) -> dict[str, Any]:
        """Runtime observes/binds/phases operations; Jev chooses only current legal alternatives."""
        async with self.page_lock:
            if self.task_finished:
                return {"success": False, "reason": "TASK_ALREADY_FINISHED", "actions": []}
            if self.browser_session_id is None:
                raise RuntimeError("browser session has not started")
            if not 1 <= max_steps <= 30:
                return {"success": False, "reason": "INVALID_PAGE_GOAL", "actions": []}
            references = [binding["value_reference"] for binding in inputs] + [reference for reference in (project_reference, row_reference) if reference]
            unavailable = [reference for reference in references if reference not in self.input_values]
            if unavailable:
                return {"success": False, "reason": "INPUT_VALUE_UNAVAILABLE", "unavailable_references": unavailable, "actions": []}
            if any(reference.startswith("env:") or not self.input_values[reference].strip() for reference in (project_reference, row_reference) if reference):
                return {"success": False, "reason": "INVALID_OBJECT_REFERENCE", "actions": []}
            if project_reference is None and context:
                project_reference = next((reference for reference, value in self.input_values.items() if "project" in reference.casefold() and not reference.startswith("env:") and context in {reference, value}), None)
            row_context = row_reference or context
            session = self.browser_manager.get_session(self.browser_session_id)
            actions: list[dict[str, Any]] = []
            completed_inputs: dict[int, str] = {}
            excluded: set[str] = set()
            previous_state: str | None = None
            seen_states: set[str] = set()
            input_progress = False
            modal_row = self.modal_row
            operation_done = False
            refresh_reason: str | None = None
            previous_candidates: tuple[str, ...] = ()
            # 末次动作后再观察一次；到达 max_steps 时禁止新的候选选择或执行。
            for step_index in range(max_steps + 1):
                try:
                    await session.page.wait_for_load_state("networkidle", timeout=2_000)
                except PlaywrightTimeoutError:
                    return {"success": False, "reason": "PAGE_NOT_SETTLED", "actions": actions}
                page_state = await self.page_state_reader.read(session.page)
                if not any(element.context_kind == "dialog" for element in page_state.interactive_elements):
                    modal_row = None
                    self.modal_row = None
                rows = await self.page_state_reader.read_rows(session.page)
                for reference, value in self.input_values.items():
                    project_input = any(binding["value_reference"] == reference and "project" in binding["control"].casefold() for binding in inputs)
                    if not reference.startswith("env:") and value.strip() and ("project" in reference.casefold() or reference == project_reference or project_input or any(row.get("identity", {}).get("data-project-id") == value for row in rows)) and any(row.get("identity", {}).get("data-project-id") and self._row_matches_context(row, reference) for row in rows):
                        self._bind_rows(rows, reference)
                if operation and not operation_done:
                    confirmed_before = len(completed_inputs)
                    for index, target in list(completed_inputs.items()):
                        field = next((element for element in page_state.interactive_elements if element.target == target), None)
                        if field is None:
                            del completed_inputs[index]
                            continue
                        actual = await session.page.locator(target).input_value()
                        expected = self.input_values[inputs[index]["value_reference"]]
                        options = self._project_options(field, inputs[index]["value_reference"]) if field.kind == "select" and "project" in field.label.casefold() else [(label, option) for label, option in field.options if label == expected]
                        valid = actual == expected or field.kind == "select" and any(option == actual for _, option in options)
                        if not valid:
                            action_type = ActionType.SELECT if field.kind == "select" else ActionType.INPUT
                            excluded.discard(self.candidate_builder._candidate_id(page_state.state_id, action_type.value, f"{target}:{inputs[index]['value_reference']}"))
                            del completed_inputs[index]
                    if input_progress:
                        input_progress = len(completed_inputs) == confirmed_before
                if previous_state is not None and refresh_reason is None and not (operation and operation_done) and self.budget.note_progress(input_progress or page_state.state_id not in seen_states):
                    self.store.append_event(event_id=f"event-{uuid4().hex}", run_id=self.run_id, task_id=self.task_id, tester_id=self.tester_id, event_type="PAGE_GOAL_BLOCKED", tool="WebTestingRuntime", action="no_progress", result={"reason": "CONSECUTIVE_NO_PROGRESS", "no_progress_steps": self.budget.usage.no_progress_steps}, latency_ms=0)
                    return {"success": False, "reason": "CONSECUTIVE_NO_PROGRESS", "actions": actions}
                seen_states.add(page_state.state_id)
                project_selects = [element for element in page_state.interactive_elements if element.kind == "select" and "project" in element.label.casefold()]
                if not modal_row:
                    self.active_project = None
                project_ready = not project_selects
                project_actions: list[BusinessAction] = []
                if project_selects:
                    if project_reference is None:
                        bound = {binding["value_reference"] for binding in inputs if not binding["value_reference"].startswith("env:") and any(self._field_matches(element, binding["control"]) for element in project_selects)}
                        if len(bound) == 1:
                            project_reference = next(iter(bound))
                    if project_reference is None and context:
                        references = [reference for reference, value in self.input_values.items() if not reference.startswith("env:") and context in {reference, value} and any(value in {label, option} for element in project_selects for label, option in element.options)]
                        if len(references) == 1:
                            project_reference = references[0]
                    if project_reference is None:
                        saved_references = {self.input_targets[element.target] for element in project_selects if element.target in self.input_targets}
                        if len(saved_references) == 1:
                            project_reference = next(iter(saved_references))
                        elif self.last_project_reference in self.input_values:
                            project_reference = self.last_project_reference
                    project_ready = project_reference is not None
                    for element in project_selects:
                        project_value = self.input_values.get(project_reference or "")
                        options = self._project_options(element, project_reference)
                        if len(options) != 1:
                            project_ready = False
                            continue
                        if element.selected_value != options[0][1]:
                            project_ready = False
                            project_actions.append(BusinessAction(label=f"Select {element.label} using {project_reference}", action=WebAction(action_type=ActionType.SELECT, target=element.target, value=project_value, value_reference=project_reference)))
                        elif project_reference is not None:
                            self.active_project = {"reference": project_reference, "target": element.target, "value": options[0][1]}
                            self.input_targets[element.target] = project_reference
                            self._remember_project(project_reference, element.target, options[0][1])
                if project_ready:
                    matching_rows = [row for row in rows if row_context and self._row_matches_context(row, row_context)]
                    if any(sum(row.get("container_target") == container for row in matching_rows) > 1 for container in {row.get("container_target") for row in matching_rows}):
                        return {"success": False, "reason": "AMBIGUOUS_ROW_BINDING", "actions": actions}
                    self._bind_rows(rows, row_context)
                    self.remember_assertion_targets(rows, row_context)
                elements = []
                for element in page_state.interactive_elements:
                    row = next((row for row in rows if row["target"] == element.context_target or element.context_target is not None and str(row["target"]).endswith(" " + element.context_target)), None)
                    if row is not None:
                        project_only = row_reference is None and project_reference is not None and row_context in {project_reference, self.input_values[project_reference]}
                        if not project_ready or not row_context or project_only and project_selects or not self._row_matches_context(row, row_context):
                            continue
                    elements.append(element)
                business_actions = list(project_actions)
                input_candidates: dict[str, int] = {}
                for index, binding in enumerate(inputs):
                    if index in completed_inputs:
                        continue
                    available = [element for element in elements if element.enabled and element.kind in {"input", "textarea", "select"}]
                    binding_context = form_context or binding.get("context", "")
                    scoped = [element for element in available if binding_context and (binding_context in {element.context, element.context_target, element.form_target} or element.form_target == f"[id={json.dumps(binding_context.removeprefix('#'))}]")]
                    if scoped or form_context or binding_context.startswith(("#", "[", "form >>")):
                        available = scoped
                    matched = [element for element in available if self._field_matches(element, binding["control"])]
                    bound_rows = [row for row in rows if row_context and self._row_matches_context(row, row_context)]
                    editing = row_reference is not None or bound_rows and binding["value_reference"] not in {row_context, project_reference}
                    if editing and modal_row is None:
                        matched = [element for element in matched if element.context_kind == "tr"]
                    if not matched and modal_row is not None:
                        modal_fields = [element for element in available if element.context_kind == "dialog" and element.kind in {"input", "textarea"}]
                        if len(modal_fields) == 1 and modal_fields[0].label.casefold() == "edit value" and binding["control"].casefold() in {"project name", "task title", "display name", "member name", "name", "title"}:
                            matched = modal_fields
                    if len(matched) > 1:
                        return {"success": False, "reason": "AMBIGUOUS_INPUT_BINDING", "control": binding["control"], "actions": actions}
                    for element in matched:
                        action_type = ActionType.SELECT if element.kind == "select" else ActionType.INPUT
                        reference = binding["value_reference"]
                        action = WebAction(action_type=action_type, target=element.target, value=self.input_values[reference], value_reference=reference)
                        business_actions.append(BusinessAction(label=f"Fill {element.label} using {reference} ({element.context})", action=action))
                        candidate_id = self.candidate_builder._candidate_id(page_state.state_id, action_type.value, f"{element.target}:{reference}")
                        input_candidates[candidate_id] = index
                pending = [binding["control"] for index, binding in enumerate(inputs) if index not in completed_inputs]
                # 提交只接受当前表单中已经绑定并实际填写的输入。
                for element in list(elements):
                    if not element.is_submit:
                        continue
                    form_ready = not pending and project_ready
                    if form_context:
                        form_ready = form_ready and (form_context in {element.form_target, element.context_target, element.context} or element.form_target == f"[id={json.dumps(form_context.removeprefix('#'))}]")
                    if inputs:
                        form_ready = form_ready and all(any(item.target == target and (item.form_target or item.context_target) == (element.form_target or element.context_target) for item in elements) for target in completed_inputs.values())
                    if row_reference:
                        form_ready = form_ready and element.context_kind == "dialog" and modal_row is not None
                    if form_ready:
                        form_ready = await self._form_inputs_bound(elements, element)
                    if not form_ready:
                        elements.remove(element)
                bindings = {"project_reference": project_reference, "row_reference": row_reference, "form_context": form_context}
                self.current_object = {**bindings, "operation": operation, "destination": destination, "selected_project": self.active_project, "modal_row": self.modal_row, "row_targets": [target for (alias, _, project_value), target in self.object_targets.items() if alias == row_context.casefold() and project_value == (self.active_project["value"] if self.active_project else None)]}
                phase = "legacy"
                eligible = list(elements)
                if operation:
                    if operation_done and operation == "navigate" and project_reference is None or operation_done and project_ready and not pending or operation == "observe" and project_ready and not pending:
                        if refresh_reason:
                            self._record_goal_recovery(refresh_reason, "OPERATION_EXECUTED_AND_OBSERVED", True)
                        self.store.append_event(event_id=f"event-{uuid4().hex}", run_id=self.run_id, task_id=self.task_id, tester_id=self.tester_id, event_type="PAGE_GOAL_OPERATION_COMPLETED", tool="WebTestingRuntime", action=operation, result={"operation": operation, "state_id": page_state.state_id, "assertions_pending": True}, latency_ms=0)
                        return {"success": True, "reason": "OPERATION_EXECUTED_AND_OBSERVED", "actions": actions}
                    if operation == "navigate" and not operation_done:
                        phase = "navigate"
                        eligible = [element for element in elements if element.kind not in {"input", "select", "textarea"} and element.context_kind not in {"tr", "form", "dialog"} and self._field_matches(element, destination or "")]
                    elif not project_ready:
                        phase = "project"
                        eligible = project_selects
                    elif operation == "edit" and modal_row is None:
                        phase = "open_edit"
                        eligible = [element for element in elements if element.context_kind == "tr" and element.label.casefold() in {"edit", "modify"}]
                    elif pending:
                        phase = "fill"
                        eligible = [element for element in elements if any(business.action.target == element.target and business.action.action_type in {ActionType.INPUT, ActionType.SELECT} for business in business_actions)]
                    elif operation in {"create", "submit", "login", "edit"}:
                        phase = "submit"
                        eligible = [element for element in elements if element.is_submit]
                    elif operation in {"delete", "logout"}:
                        phase = operation
                        names = {"delete", "remove"} if operation == "delete" else {"logout", "log out", "sign out"}
                        eligible = [element for element in elements if element.label.casefold() in names and (operation != "delete" or element.context_kind == "tr" and bool(row_context))]
                    else:
                        eligible = []
                allowed_targets = {element.target for element in eligible}
                if step_index == max_steps:
                    return {"success": False, "reason": "PAGE_GOAL_STEP_LIMIT", "actions": actions}
                current_business = [business for business in business_actions if business.action.target in allowed_targets]
                candidates = self.candidate_builder.build(goal=goal, page_state=replace(page_state, interactive_elements=tuple(eligible)), include_controls=False, business_actions=current_business, click_inputs=False, excluded_candidates=excluded)
                candidates = [candidate for candidate in candidates if not candidate.requires_confirmation]
                if not operation and not pending and project_ready and (not inputs or actions and actions[-1]["action"] not in {"input", "select"}):
                    candidates.append(ActionCandidate(candidate_id=f"candidate-stop-{page_state.state_id}", action="stop_current_path", label="Return for the supplied assertions; this does not declare Task success.", target=None, state_id=page_state.state_id))
                signature = tuple(candidate.candidate_id for candidate in candidates)
                self.store.append_event(event_id=f"event-{uuid4().hex}", run_id=self.run_id, task_id=self.task_id, tester_id=self.tester_id, event_type="CANDIDATE_SET", tool="WebTestingRuntime", action=phase, result={"operation": operation, "state_id": page_state.state_id, "candidate_ids": list(signature), "bindings": bindings}, latency_ms=0)
                if not candidates or refresh_reason in {"LOW_JEV_CONFIDENCE", "NO_SELECTION"} and signature == previous_candidates:
                    reason = refresh_reason or ("UNBOUND_REQUIRED_INPUT" if pending else "PROJECT_BINDING_UNAVAILABLE" if not project_ready else "CONTROL_NOT_FOUND")
                    if refresh_reason is None and step_index + 1 < max_steps:
                        refresh_reason = reason
                        previous_candidates = signature
                        self.store.append_event(event_id=f"event-{uuid4().hex}", run_id=self.run_id, task_id=self.task_id, tester_id=self.tester_id, event_type="RUNTIME_RECOVERY", tool="WebTestingRuntime", action="refresh_candidates", result={"reason": reason, "phase": phase, "state_id": page_state.state_id, "candidate_count": len(candidates)}, latency_ms=0)
                        continue
                    if refresh_reason:
                        if reason == "PROJECT_BINDING_UNAVAILABLE" and project_reference and project_selects and all(not self._project_options(element, project_reference) for element in project_selects):
                            reason = "OBJECT_UNAVAILABLE_IN_CURRENT_VIEW"
                            self.current_object["availability"] = {"reference": project_reference, "status": "unavailable", "scope": "current_view", "state_id": page_state.state_id}
                        self._record_goal_recovery(refresh_reason, reason, False)
                    return {"success": False, "reason": reason, "pending_inputs": pending, "actions": actions}
                current_goal = f"Current decision: {phase}. " + (f"Operation: {operation}; destination: {destination}. " if operation else goal + ". ") + f"Bindings: {json.dumps(bindings)}. Pending fields: {json.dumps(pending)}. Choose only a supplied candidate or none."
                decision = await self._execute_candidate(current_goal, page_state, candidates[0], source="RUNTIME_TO_PLAYWRIGHT") if operation and len(candidates) == 1 else await self._select_and_execute(current_goal, page_state, candidates)
                if decision.reason in {"CONTROL_NOT_FOUND", "CANDIDATE_EXPIRED", "TARGET_INVALID", "LOW_JEV_CONFIDENCE", "NO_SELECTION", "PROJECT_BINDING_UNAVAILABLE"} and refresh_reason is None and step_index + 1 < max_steps:
                    refresh_reason = decision.reason
                    previous_candidates = signature
                    self.store.append_event(event_id=f"event-{uuid4().hex}", run_id=self.run_id, task_id=self.task_id, tester_id=self.tester_id, event_type="RUNTIME_RECOVERY", tool="WebTestingRuntime", action="refresh_candidates", result={"reason": decision.reason, "phase": phase, "state_id": page_state.state_id, "candidate_count": len(candidates)}, latency_ms=0)
                    continue
                if decision.reason == "STOP_CURRENT_PATH":
                    return {"success": True, "reason": "PAGE_GOAL_STOPPED", "actions": actions}
                if decision.action_result is None or not decision.action_result.success:
                    failure_reason = decision.action_result.error_type if decision.action_result is not None else decision.reason
                    if refresh_reason:
                        self._record_goal_recovery(refresh_reason, failure_reason, False)
                    return {"success": False, "reason": failure_reason, "actions": actions}
                if refresh_reason:
                    self._record_goal_recovery(refresh_reason, decision.reason, True)
                candidate = decision.candidate
                assert candidate is not None
                excluded.add(candidate.candidate_id)
                refresh_reason = None
                if operation and phase in {"navigate", "submit", "delete", "logout"}:
                    operation_done = True
                previous_state = page_state.state_id
                input_progress = candidate.action in {"input", "select"}
                if candidate.candidate_id in input_candidates:
                    assert candidate.target is not None
                    completed_inputs[input_candidates[candidate.candidate_id]] = candidate.target
                clicked = next((element for element in elements if element.target == candidate.target), None)
                if candidate.action == "click" and clicked is not None and clicked.context_kind == "tr":
                    modal_row = clicked.context_target
                    self.modal_row = modal_row
                actions.append({"action": candidate.action, "control": candidate.label, "value_reference": candidate.value_reference})
                self.store.append_event(event_id=f"event-{uuid4().hex}", run_id=self.run_id, task_id=self.task_id, tester_id=self.tester_id, event_type="PAGE_GOAL_ACTION", tool="TesterPageGoals", action=candidate.action, result={"source": decision.source, "success": True, "browser_event_id": decision.action_result.event_id}, latency_ms=0)
                if stop_after_inputs and inputs and len(completed_inputs) == len(inputs):
                    return {"success": True, "reason": "INPUTS_READY_FOR_PENDING_SUBMISSION", "actions": actions}
            return {"success": False, "reason": "PAGE_GOAL_STEP_LIMIT", "actions": actions}

    def _record_goal_recovery(self, reason: str, final_reason: str | None, recovered: bool) -> None:
        self.store.append_event(event_id=f"event-{uuid4().hex}", run_id=self.run_id, task_id=self.task_id, tester_id=self.tester_id, event_type="RUNTIME_RECOVERY_RESULT", tool="WebTestingRuntime", action="refresh_candidates", result={"reason": reason, "final_reason": final_reason, "recovered": recovered}, latency_ms=0)

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

    async def execute_page_action(self, action: WebAction, *, control: str, context: str = "", project_reference: str | None = None) -> DecisionResult:
        """Reobserve and rebuild a missing/stale control once before escalating."""
        async with self.page_lock:
            failure = self.validate_assertion_contract(action, control=control, context=context)
            if failure:
                return DecisionResult(source="RUNTIME", reason=failure["reason"], needs_tester_llm=True)
            first_reason = None
            for attempt in range(2):
                result = await self._resolve_page_action(action, control=control, context=context, project_reference=project_reference, refused_state_id=self.last_decision_state_id if attempt and first_reason in {"LOW_JEV_CONFIDENCE", "NO_SELECTION"} else None)
                if attempt:
                    self.store.append_event(event_id=f"event-{uuid4().hex}", run_id=self.run_id, task_id=self.task_id, tester_id=self.tester_id, event_type="RUNTIME_RECOVERY", tool="WebTestingRuntime", action="reobserve_control", result={"reason": first_reason, "final_reason": result.reason, "recovered": result.action_result is not None and result.action_result.success}, latency_ms=0)
                if result.reason not in {"CONTROL_NOT_FOUND", "CANDIDATE_EXPIRED", "TARGET_INVALID", "LOW_JEV_CONFIDENCE", "NO_SELECTION", "PROJECT_BINDING_UNAVAILABLE"}:
                    return result
                first_reason = result.reason
            if result.reason == "PROJECT_BINDING_UNAVAILABLE":
                self.current_object["availability"] = {"reference": project_reference, "status": "unavailable", "scope": "current_view"}
                result = replace(result, reason="OBJECT_UNAVAILABLE_IN_CURRENT_VIEW")
            return replace(result, source="TESTER_LLM", needs_tester_llm=True)

    async def _resolve_page_action(self, action: WebAction, *, control: str, context: str, project_reference: str | None, refused_state_id: str | None = None) -> DecisionResult:
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
        action = replace(action, project_reference=project_reference or action.project_reference)
        if project_reference and project_reference not in self.input_values:
            return DecisionResult(source="TESTER_LLM", reason="INPUT_VALUE_UNAVAILABLE", needs_tester_llm=True)
        if action.action_type in {ActionType.INPUT, ActionType.SELECT} and action.value_reference not in self.input_values:
            return DecisionResult(source="TESTER_LLM", reason="INPUT_VALUE_UNAVAILABLE", needs_tester_llm=True)
        project_selects = [element for element in page_state.interactive_elements if element.kind == "select" and "project" in element.label.casefold()]
        self.active_project = None
        if project_reference:
            for element in project_selects:
                options = self._project_options(element, project_reference)
                if len(options) != 1:
                    return DecisionResult(source="RUNTIME", reason="PROJECT_BINDING_UNAVAILABLE" if not options else "AMBIGUOUS_PROJECT_BINDING", needs_tester_llm=True)
                if element.selected_value != options[0][1]:
                    result = await self._execute_known_action(WebAction(ActionType.SELECT, target=element.target, value=self.input_values[project_reference], value_reference=project_reference))
                    self.store.append_event(event_id=f"event-{uuid4().hex}", run_id=self.run_id, task_id=self.task_id, tester_id=self.tester_id, event_type="RUNTIME_RECOVERY", tool="WebTestingRuntime", action="rebind_project", result={"reason": "OBJECT_PROJECT_MISMATCH", "project_reference": project_reference, "object_id": options[0][1], "recovered": result.success}, latency_ms=0)
                    if not result.success:
                        return DecisionResult(source="RUNTIME", reason=result.error_type or "PROJECT_BINDING_UNAVAILABLE", action_result=result, needs_tester_llm=True)
                    page_state = await self.page_state_reader.read(session.page, include_content=action.action_type == ActionType.ASSERTION)
                    project_selects = [item for item in page_state.interactive_elements if item.kind == "select" and "project" in item.label.casefold()]
            if len(project_selects) == 1:
                selected_value = project_selects[0].selected_value
                if selected_value is not None:
                    self.active_project = {"reference": project_reference, "target": project_selects[0].target, "value": selected_value}
        elif context:
            projects = [project for (alias, _, _), project in self.object_projects.items() if alias == context.casefold()]
            current_projects = [project for project in projects if any(element.target == project["target"] and element.selected_value == project["value"] for element in project_selects)]
            if project_selects and projects and not current_projects:
                return DecisionResult(source="TESTER_LLM", reason="OBJECT_PROJECT_MISMATCH", needs_tester_llm=True)
            if current_projects:
                self.active_project = dict(current_projects[0])
            elif len(project_selects) == 1:
                element = project_selects[0]
                reference = self.input_targets.get(element.target)
                bound_value = self.input_values.get(reference or "")
                if reference and any(element.selected_value == option and bound_value in {label, option} for label, option in element.options):
                    assert element.selected_value is not None
                    self.active_project = {"reference": reference, "target": element.target, "value": element.selected_value}
        rows = await self.page_state_reader.read_rows(session.page)
        self._bind_rows(rows, context)
        for reference, value in self.input_values.items():
            if not reference.startswith("env:") and value.strip() and ("project" in reference.casefold() or reference == project_reference or any(row.get("identity", {}).get("data-project-id") == value for row in rows)) and any(row.get("identity", {}).get("data-project-id") and self._row_matches_context(row, reference) for row in rows):
                self._bind_rows(rows, reference)
        if action.action_type == ActionType.ASSERTION and action.target:
            matching_projects = [reference for reference, stable_id in self.project_ids.items() if f'data-project-id={json.dumps(stable_id)}' in action.target]
            if matching_projects:
                self.last_project_reference = matching_projects[0]
        self.remember_assertion_targets(rows, context)
        kinds = {ActionType.INPUT: {"input", "textarea"}, ActionType.SELECT: {"select"}, ActionType.REPEAT_SUBMIT: {"button"}, ActionType.ASSERTION: {"cell", "input", "textarea", "select"} if action.assertion in {"visible", "hidden"} else {"cell"}}
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
            candidate = ActionCandidate(candidate_id=self.candidate_builder._candidate_id(page_state.state_id, action.action_type.value, element.target), action=action.action_type.value, label=f"{element.label} ({element.context})", target=element.target, state_id=page_state.state_id, value=action.value, value_reference=action.value_reference, url=resolved_url, resource_id=action.resource_id, requires_resource=action.requires_resource, confirmed=action.confirmed, expected=action.expected, assertion=action.assertion, behavior_id=action.behavior_id, goal_check=action.goal_check, check_id=action.check_id, project_reference=action.project_reference)
            matches_control = self._field_matches(element, control)
            if action.action_type == ActionType.ASSERTION and element.kind == "cell" and row is not None:
                identity = row.get("identity", {})
                aliases = {"Name": {"Project name"} if "data-project-id" in identity else {"Display name", "Member name"} if "data-username" in identity else set(), "Title": {"Task title"} if "data-task-id" in identity else set(), "Username": {"Member username"} if "data-username" in identity else set()}
                matches_control = matches_control or control.casefold() in {name.casefold() for name in aliases.get(element.label, set())}
            words = set(control.casefold().split()) - {"the", "a", "an", "in", "on", "to", "button", "control"}
            if matches_control or action.action_type == ActionType.CLICK and words & set(element.label.casefold().split()):
                candidates.append(candidate)
            if matches_control:
                exact.append(candidate)
        if action.action_type in {ActionType.INPUT, ActionType.SELECT} and len(exact) != 1:
            return DecisionResult(source="TESTER_LLM", reason="AMBIGUOUS_INPUT_BINDING" if len(exact) > 1 else "CONTROL_NOT_FOUND", needs_tester_llm=True)
        if action.action_type == ActionType.ASSERTION and len(exact) > 1:
            return DecisionResult(source="TESTER_LLM", reason="AMBIGUOUS_ASSERTION_TARGET", needs_tester_llm=True)
        if len(exact) == 1:
            return await self._execute_candidate(control, page_state, exact[0], source="PLAYWRIGHT")
        if not candidates:
            known = self.assertion_targets.get((control.casefold(), context.casefold()), {})
            available_known = {target: container for target, container in known.items() if container is None or await session.page.locator(container).is_visible()}
            if available_known:
                known = available_known
            if action.action_type == ActionType.ASSERTION and len(known) == 1:
                target, container = next(iter(known.items()))
                if container is None or await session.page.locator(container).is_visible():
                    # 删除后缺失的单元格仍须用此前观察到的定位器做真实断言。
                    result = await self._execute_known_action(replace(action, target=target))
                    candidate = ActionCandidate(candidate_id="recorded-assertion", action=action.action_type.value, label=control, target=target, state_id=page_state.state_id)
                    return DecisionResult(source="PLAYWRIGHT", reason="RECORDED_ASSERTION_TARGET", candidate=candidate, action_result=result)
                self.store.append_event(event_id=f"event-{uuid4().hex}", run_id=self.run_id, task_id=self.task_id, tester_id=self.tester_id, event_type="ASSERTION_TARGET_UNAVAILABLE", tool="WebTestingRuntime", action="resolve_control", result={"check_id": action.check_id, "target": target, "control": control, "object": {"context": context, "project_reference": project_reference}, "failure_reason": "ASSERTION_PREREQUISITE_UNAVAILABLE", "state_id": page_state.state_id}, latency_ms=0)
                return DecisionResult(source="RUNTIME", reason="ASSERTION_PREREQUISITE_UNAVAILABLE", needs_tester_llm=True)
            failure = {"check_id": action.check_id, "target": action.target, "control": control, "object": {"context": context, "project_reference": project_reference}, "failure_reason": "CONTROL_NOT_FOUND", "state_id": page_state.state_id}
            self.store.append_event(event_id=f"event-{uuid4().hex}", run_id=self.run_id, task_id=self.task_id, tester_id=self.tester_id, event_type="ASSERTION_TARGET_UNAVAILABLE" if action.action_type == ActionType.ASSERTION else "CONTROL_UNAVAILABLE", tool="WebTestingRuntime", action="resolve_control", result=failure, latency_ms=0)
            return DecisionResult(source="TESTER_LLM", reason="CONTROL_NOT_FOUND", needs_tester_llm=True)
        choices = exact or candidates
        if refused_state_id == page_state.state_id:
            return DecisionResult(source="RUNTIME", reason="LOW_JEV_CONFIDENCE")
        return await self._select_and_execute(f"{action.action_type.value} the control '{control}' in '{context}'; stop if no control fits", page_state, choices)

    async def _select_and_execute(self, current_goal: str, page_state: PageState, candidates: list[ActionCandidate]) -> DecisionResult:
        self.last_decision_state_id = page_state.state_id
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
        with trace_span("Jev", "llm", metadata={"agent_role": "jev", "phase": "jev_decision", "decision_type": "choice", "candidate_count": len(candidates), "model": getattr(self.jev_selector.client, "model", "typesafe/jev-1.13"), "provider": "openrouter", "ls_model_name": getattr(self.jev_selector.client, "model", "typesafe/jev-1.13"), "ls_provider": "openrouter"}) as span:
            selection = await self.jev_selector.select(current_goal=current_goal, page_state=page_state, candidates=candidates)
            failure_reason = selection.error
            if failure_reason is None and selection.selected_candidate_id not in {candidate.candidate_id for candidate in candidates}:
                failure_reason = "ILLEGAL_CANDIDATE_ID"
            if failure_reason is None and selection.confidence < self.jev_confidence_threshold:
                failure_reason = "LOW_JEV_CONFIDENCE"
            trace_result(span, input_tokens=selection.input_tokens, output_tokens=selection.output_tokens, cost=selection.cost, success=failure_reason is None, error_type=failure_reason, failure_reason=failure_reason)
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
                "decision_type": "choice",
                "candidate_ids": [candidate.candidate_id for candidate in candidates],
                "failure_reason": failure_reason,
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
        return await self._execute_candidate(current_goal, page_state, candidate, source="JEV_TO_PLAYWRIGHT")

    async def _execute_candidate(self, current_goal: str, page_state: PageState, candidate: ActionCandidate, *, source: str) -> DecisionResult:
        assert self.browser_session_id is not None
        session = self.browser_manager.get_session(self.browser_session_id)
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
        if action_result.error_type == "INVALID_ACTION" and action_result.error in {"target does not exist", "target is not visible"}:
            return DecisionResult(source="RUNTIME", reason="CONTROL_NOT_FOUND", candidate=candidate, action_result=action_result)
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
            source=source,
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
            task = self.store.get_task(self.task_id)
            assert task is not None
            task["data_requirements"].setdefault("expected_behavior_ids", list(self.expected_behavior_ids))
            outcome = score_task(self.store, task, live=True)
            self.store.record_task_outcome(task_id=self.task_id, success_status=outcome["success_status"], reason=outcome["reason"], assertion_results=outcome["assertions"])
            self.store.append_event(event_id=f"event-{uuid4().hex}", run_id=self.run_id, task_id=self.task_id, tester_id=self.tester_id, event_type="TASK_OUTCOME", tool="Python", action="evaluate_goal_assertions", result=outcome, latency_ms=0)
            if finish:
                self.task_finished = True
            return outcome

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
    def _stopped_result(reason: str, *, error_type: str | None = None) -> ActionResult:
        timestamp = datetime.now(UTC).isoformat()
        return ActionResult(
            started_at=timestamp,
            ended_at=timestamp,
            latency_ms=0,
            success=False,
            error=reason,
            error_type=error_type or reason,
        )
