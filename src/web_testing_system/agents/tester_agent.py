"""The single Tester Agent definition and its constrained tools."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from time import perf_counter
from typing import Any, Literal
from uuid import uuid4

from agent_framework import (
    Agent,
    AgentResponse,
    AgentSession,
    FunctionInvocationContext,
    FunctionMiddleware,
    FunctionTool,
    MiddlewareTermination,
)
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from pydantic import TypeAdapter, ValidationError

from web_testing_system.observability import (
    TraceChatMiddleware,
    TraceToolMiddleware,
    trace_result,
    trace_span,
)
from web_testing_system.runtime.budget import BudgetExceededError, BudgetGuard
from web_testing_system.runtime.models import (
    ActionCandidate,
    ActionType,
    PageState,
    WebAction,
)
from web_testing_system.runtime.web_runtime import WebTestingRuntime
from web_testing_system.state import StateStore


@dataclass(frozen=True)
class TesterAssignment:
    run_id: str
    task_id: str
    tester_id: str
    identity_id: str
    role: str
    data_namespace: str
    scope: tuple[str, ...]
    step_budget: int


@dataclass
class PageStep:
    """Use control/context for semantic actions and table column checks; target is an observed locator. wait_ms is an integer delay, not a selector."""
    action_type: ActionType
    control: str | None = None
    context: str = ""
    project_reference: str | None = None
    target: str | None = None
    value_reference: str | None = None
    expected: str | None = None
    expected_reference: str | None = None
    assertion: Literal["contains", "not_contains", "equals", "visible", "hidden", "count"] = "contains"
    behavior_id: str | None = None
    check_id: str | None = None
    url: str | None = None
    key: str | None = None
    wait_ms: int = 0


@dataclass
class PageInput:
    """Bind configured data to a human input label or observed locator. Leave context empty unless it is an exact observed scope selector; never put field descriptions or instructions here."""
    control: str
    value_reference: str
    context: str = ""


@dataclass
class PageGoal:
    """A business subgoal. project_reference selects the Project; row_reference identifies an existing target row; form_context scopes inputs. References must exist in Scenario data. Check IDs determine recovery."""
    goal: str
    inputs: list[PageInput] = field(default_factory=list)
    checks: list[PageStep] = field(default_factory=list)
    context: str = ""
    project_reference: str | None = None
    row_reference: str | None = None
    form_context: str = ""
    max_steps: int = 12
    before_steps: list[PageStep] = field(default_factory=list)
    after_steps: list[PageStep] = field(default_factory=list)
    run_operations: bool = True
    wait_for_progress: str | None = None
    publish_progress: str | None = None
    operation: Literal["navigate", "create", "edit", "delete", "submit", "login", "logout", "observe"] | None = None
    destination: str | None = None


class TesterAgentTools:
    def __init__(
        self,
        *,
        assignment: TesterAssignment,
        runtime: WebTestingRuntime,
        store: StateStore,
        shared_state: bool = True,
        decision_policy: str = "JEV",
        action_policy: str = "PLAYWRIGHT",
        task_context: dict[str, Any] | None = None,
    ) -> None:
        self.assignment = assignment
        self.runtime = runtime
        self.store = store
        self.shared_state = shared_state
        self.decision_policy = decision_policy
        self.action_policy = action_policy
        self.pending_candidates: dict[str, ActionCandidate] = {}
        self.pending_page_state: PageState | None = None
        self.pending_goal: str | None = None
        self.plan_started = bool(store.list_events(assignment.run_id, task_id=assignment.task_id, event_types=("TESTER_PLAN",))) if store is not None else False
        self.executing_plan = False
        self.task_context_supplied = task_context is not None
        self.task_context = task_context or {}
        if task_context is not None:
            self.task_context["test_inputs"] = {reference: value for reference, value in runtime.input_values.items() if reference in self.allowed_input_references() and not reference.startswith("env:")}

    def allowed_input_references(self) -> set[str]:
        values = getattr(self.runtime, "input_values", {})
        selected = self.task_context.get("test_data_references")
        references = set(selected) if selected is not None else {reference for reference in values if not reference.startswith("env:")}
        username = f"{self.task_context.get('identity_reference', '')}_username"
        if username in values:
            references.add(username)
        secret = self.task_context.get("secret_reference")
        if secret:
            references.add(secret)
        elif not self.task_context_supplied:
            references.update(values)
        return references & set(values)

    def binding_scopes(self, page: dict[str, Any]) -> dict[str, list[str]]:
        references = self.allowed_input_references()
        values = getattr(self.runtime, "input_values", {})
        public = {reference for reference in references if not reference.startswith("env:") and values[reference].strip()}
        rows = {str(row["target"]) for row in page.get("rows", [])}
        forms: set[str] = set()
        for element in page.get("interactive_elements", []):
            if element.get("kind") in {"input", "textarea", "select"}:
                for key in ("form_target", "context_target"):
                    if element.get(key):
                        forms.add(str(element[key]))
        return {"object_contexts": ["", *sorted(public | {values[reference] for reference in public} | rows)], "form_contexts": ["", *sorted(forms)], "input_contexts": ["", *sorted(forms | rows)]}

    def plan_tool(self, page: dict[str, Any] | None = None) -> FunctionTool:
        """Advertise the same task-specific references and positions that preflight accepts."""
        tool = FunctionTool(name="execute_test_plan", func=self.execute_test_plan, description=self._execute_test_plan.__doc__ or "")
        schema = deepcopy(tool.parameters())
        definitions = schema["$defs"]
        assertion = deepcopy(definitions["PageStep"])
        boundary = deepcopy(definitions["PageStep"])
        assertion["properties"]["action_type"] = {"type": "string", "enum": ["assertion", "url_check"]}
        boundary["properties"]["action_type"] = {"type": "string", "enum": ["navigation", "refresh", "wait", "repeat_submit", "assertion", "url_check"]}
        definitions["PlanAssertion"] = assertion
        definitions["PlanBoundary"] = boundary
        goal = definitions["PageGoal"]["properties"]
        goal["operation"] = {"type": "string", "enum": ["navigate", "create", "edit", "delete", "submit", "login", "logout", "observe"], "description": "One explicit business operation. Runtime owns its navigation/binding/fill/submit phases; Jev only chooses current candidates. Use observe for check-only goals."}
        definitions["PageGoal"]["required"].append("operation")
        goal["destination"]["description"] = "Human navigation destination (e.g. Tasks, Members, Projects), only for navigate. Runtime finds actual submit/edit/delete buttons; do not place those labels or selectors here."
        goal["checks"]["items"] = {"$ref": "#/$defs/PlanAssertion"}
        for key in ("before_steps", "after_steps"):
            goal[key]["items"] = {"$ref": "#/$defs/PlanBoundary"}
        references = sorted(self.allowed_input_references())
        public = [reference for reference in references if not reference.startswith("env:")]
        objects = [reference for reference in public if self.runtime.input_values[reference].strip()] if references else []
        scopes = self.binding_scopes(page or {})
        goal["context"] = {"type": "string", "enum": scopes["object_contexts"], "default": "", "description": "Exact assigned entity reference/value or observed row selector only. Empty for create/login/register/check-only page goals. This is NOT a goal description, page description or form name."}
        goal["form_context"] = {"type": "string", "enum": scopes["form_contexts"], "default": "", "description": "Optional exact observed form scope. For future or uniquely labelled forms OMIT this field; do not describe a form in English."}
        goal["max_steps"] = {"type": "integer", "minimum": 1, "maximum": 30, "default": 12}
        definitions["PageInput"]["properties"]["context"] = {"type": "string", "enum": scopes["input_contexts"], "default": "", "description": "Normally omit. Only an exact observed form/row selector can disambiguate a field. Input purpose and submit instructions belong in goal, never here."}
        definitions["PageInput"]["properties"]["value_reference"] = {"type": "string", "enum": references}
        for key in ("project_reference", "row_reference"):
            goal[key] = {"enum": [None, *objects], "default": None}
        checks = getattr(self.runtime, "required_checks", [])
        for definition in (assertion, boundary):
            definition["properties"]["context"] = {"type": "string", "enum": scopes["object_contexts"], "default": "", "description": "Exact entity reference/value or observed row selector for a scoped table assertion. Never a form/page description."}
            for key, allowed in (("value_reference", references), ("expected_reference", public), ("project_reference", objects)):
                definition["properties"][key] = {"enum": [None, *allowed], "default": None}
            if checks:
                definition["properties"]["check_id"] = {"enum": [None, *[check["check_id"] for check in checks]], "description": "Use each assigned ID exactly once on its actual assertion or repeat_submit evidence, never on navigation/refresh/wait.", "default": None}
                definition["properties"]["behavior_id"] = {"enum": [None, *sorted({check["behavior_id"] for check in checks})], "default": None}
        definitions.pop("PageStep", None)
        definitions.pop("ActionType", None)
        # Dictionary schemas advertise constraints; the existing preflight remains authoritative.
        return FunctionTool(name=tool.name, description=tool.description, func=self.execute_test_plan, input_model=schema)

    async def planning_context(self, latest_failure: dict[str, Any] | None = None) -> dict[str, Any]:
        outcome = await self.runtime.record_task_outcome()
        outcome_checks = outcome["checks"]
        assert isinstance(outcome_checks, list)
        completed = sorted(check["check_id"] for check in outcome_checks if check["completed"])
        assigned = [dict(check) for check in self.runtime.required_checks]
        references = self.allowed_input_references()
        checks = [{**check, "completed": check["check_id"] in completed, "place": "before_steps or after_steps" if check.get("action_type") == "repeat_submit" else "checks (or an assertion after its refresh boundary)", "allowed_actions": ["repeat_submit"] if check.get("action_type") == "repeat_submit" else ["assertion", "url_check"]} for check in assigned]
        page = await self._page_summary() if self.runtime.browser_session_id else {}
        preparation = self.store.list_events(self.assignment.run_id, task_id=self.assignment.task_id, event_types=("TESTER_PREPARATION",))
        context = {"task_id": self.assignment.task_id, "goal": self.task_context.get("goal", ""), "identity_reference": self.task_context.get("identity_reference"), "role": self.assignment.role, "scope": list(self.assignment.scope), "target_url": self.task_context.get("target_url"), "denied_operations": self.task_context.get("denied_operations", []), "required_operations": self.task_context.get("required_operations", []), "assigned_checks": checks, "allowed_check_ids": [check["check_id"] for check in assigned], "completed_check_ids": completed, "remaining_check_ids": [check["check_id"] for check in assigned if check["check_id"] not in completed], "prepared_check_ids": sorted(self.runtime.prepared_check_ids), "allowed_input_references": sorted(references), "test_inputs": {reference: self.runtime.input_values[reference] for reference in sorted(references) if not reference.startswith("env:")}, "secret_references": [reference for reference in sorted(references) if reference.startswith("env:")], "assigned_behavior_ids": list(self.runtime.expected_behavior_ids), "preparation": preparation[-1]["result"] if preparation else {}, "current_page": page, "latest_failure": latest_failure, "plan_rules": {"checks_actions": ["assertion", "url_check"], "boundary_actions": ["navigation", "refresh", "wait", "repeat_submit", "assertion", "url_check"], "operation_actions": "Inputs belong in PageGoal.inputs; declare one operation and its constraints for Runtime. Jev only selects current candidates. No click/input/select in boundary steps.", "check_ids": "Exactly one occurrence per remaining Check ID, paired with its assigned behavior_id. Incidental setup boundaries have neither ID. A refresh then assertion uses the ID only on the assertion.", "ordering": "Honor depends_on before the dependent check. Preserve necessary refresh/reopen/logout/login/pending-submit boundaries.", "repeat_submit": "Use run_operations=true with inputs; Runtime stops after filling, then after_steps.repeat_submit supplies the sole pending-operation check ID. Subsequent DOM assertions have their own IDs.", "references": "Only the supplied exact reference keys. Blank input values are legal. Secret references may fill fields but cannot be assertion values.", "objects": "project_reference selects a Project; row_reference identifies an existing row; form_context scopes a form. Do not use prose in object reference fields."}}
        context["binding_scopes"] = self.binding_scopes(page)
        context["current_object"] = dict(self.runtime.current_object) if isinstance(self.runtime, WebTestingRuntime) else {}
        context["plan_rules"]["operations"] = "Every goal declares operation: navigate/create/edit/delete/submit/login/logout/observe. Navigate uses destination=<human navigation label>; create/submit binds supplied inputs; edit/delete uses a stable row_reference. Reopen a view with a navigate goal, then observe/check; do not hide navigation, mutations or logout inside a compound operation. Runtime phases these operations; Jev only selects legal current candidates. Preserve all explicit Check boundaries and existing budgets."
        context["plan_rules"]["contexts"] = "Context fields are binding keys, NEVER explanatory prose. For create/register/login set goal.context='', omit row_reference/project_reference, and normally omit form_context and input.context. Put all operation instructions in goal. For editing/deleting existing objects use row_reference; project_reference selects the containing Project only. Table assertions use the exact entity key/value, not a page or table description."
        context["plan_rules"]["max_steps"] = "Each goal allows 1..30; default 12. The original Task/step/Replan budgets remain binding."
        return context

    async def execute_page_goals(self, goals: list[PageGoal], related_task_ids: list[str] | None = None, finish: bool = False) -> dict[str, Any]:
        """Attach the primary check ID to page decisions without uploading goal text."""
        goals = TypeAdapter(list[PageGoal]).validate_python(goals)
        check_id = next((step.check_id for goal in goals for step in [*goal.before_steps, *goal.checks, *goal.after_steps] if step.check_id), None)
        with trace_span("PageGoal", metadata={"task_id": self.assignment.task_id, "check_id": check_id}):
            return await self._execute_page_goals(goals, related_task_ids, finish)

    async def _execute_page_goals(self, goals: list[PageGoal], related_task_ids: list[str] | None = None, finish: bool = False) -> dict[str, Any]:
        """Delegate explicit business operations to Runtime in one plan call. Runtime observes, binds, phases and recovers; Jev selects bounded alternatives. Python runs checks and determines Task success."""
        goals = TypeAdapter(list[PageGoal]).validate_python(goals)
        for goal in goals:
            if not goal.goal.strip() or not 1 <= goal.max_steps <= 30:
                return {"success": False, "error_type": "INVALID_PAGE_GOAL"}
            for binding in goal.inputs:
                if not binding.control.strip() or binding.value_reference not in self.runtime.input_values:
                    return {"success": False, "error_type": "INPUT_VALUE_UNAVAILABLE"}
            for check in goal.checks:
                if check.action_type not in {ActionType.ASSERTION, ActionType.URL_CHECK}:
                    return {"success": False, "error_type": "PAGE_GOAL_CHECK_REQUIRES_ASSERTION"}
        results: list[dict[str, Any]] = []
        for index, goal in enumerate(goals):
            goal_steps = [*goal.before_steps, *goal.checks, *goal.after_steps]
            for step in goal_steps:
                if step.project_reference is None:
                    step.project_reference = goal.project_reference
                if step.action_type == ActionType.ASSERTION and step.control and not step.context:
                    step.context = goal.row_reference or goal.context
            if goal.wait_for_progress:
                waiting = await self.wait_for_shared_progress(goal.wait_for_progress)
                if not waiting["ready"]:
                    return {"success": False, "failed_goal": index, "reason": waiting["reason"], "results": results}
            if goal.before_steps:
                preparation = await self.execute_page_steps(goal.before_steps, related_task_ids=related_task_ids)
                if not preparation["success"]:
                    return {**preparation, "failed_goal": index}
            goal_checks = {step.check_id for step in goal_steps if step.check_id}
            prepared = bool(goal_checks) and goal_checks <= self.runtime.prepared_check_ids
            operations = await self.runtime.execute_page_goal(goal.goal, inputs=[asdict(binding) for binding in goal.inputs], context=goal.context, project_reference=goal.project_reference, row_reference=goal.row_reference, form_context=goal.form_context, max_steps=goal.max_steps, stop_after_inputs=any(step.action_type == ActionType.REPEAT_SUBMIT for step in goal.after_steps), operation=goal.operation, destination=goal.destination) if goal.run_operations and not prepared else {"success": True, "reason": "CHECK_ONLY", "actions": []}
            result: dict[str, Any] = {"goal": index, "operations": operations}
            results.append(result)
            if not operations["success"]:
                return {"success": False, "failed_goal": index, "results": results, "page": await self._page_summary()}
            project_reference = goal.project_reference or (self.runtime.active_project["reference"] if self.runtime.active_project else None)
            for step in [*goal.checks, *goal.after_steps]:
                if step.project_reference is None:
                    step.project_reference = project_reference
            if goal_checks and goal.run_operations:
                self.runtime.prepared_check_ids.update(goal_checks)
                self.store.append_event(event_id=f"event-{uuid4().hex}", run_id=self.assignment.run_id, task_id=self.assignment.task_id, tester_id=self.assignment.tester_id, event_type="PLAN_GOAL_PREPARED", tool="TesterPlan", action="prepare_checks", result={"check_ids": sorted(goal_checks)}, latency_ms=0)
            if goal.checks:
                checks = await self.execute_page_steps(goal.checks, related_task_ids=related_task_ids)
                result["checks"] = checks["results"]
                if not checks["success"]:
                    return {"success": False, "failed_goal": index, "reason": checks.get("reason") or checks.get("error_type"), "results": results, "page": checks.get("page", {})}
            if goal.after_steps:
                after = await self.execute_page_steps(goal.after_steps, related_task_ids=related_task_ids)
                result["after_steps"] = after["results"]
                if not after["success"]:
                    return {"success": False, "failed_goal": index, "reason": after.get("reason") or after.get("error_type"), "results": results, "page": after.get("page", {})}
            if goal.publish_progress:
                await self.update_task_progress(True, goal.publish_progress)
        if finish:
            return {"success": True, "results": results, "outcome": await self.finish_task()}
        return {"success": True, "results": results, "page": await self._page_summary()}

    async def execute_test_plan(self, goals: list[PageGoal], related_task_ids: list[str] | None = None) -> dict[str, Any]:
        """Trace the initial plan and each exceptional remaining-plan replacement."""
        phase = "tester_exception_replan" if self.plan_started else "tester_initial_plan"
        name = "TesterExceptionReplan" if self.plan_started else "TesterInitialPlan"
        with trace_span(name, metadata={"phase": phase, "task_id": self.assignment.task_id, "agent_role": "tester"}) as span:
            try:
                result = await self._execute_test_plan(goals, related_task_ids)
            except ValidationError as error:
                result = {"success": False, "reason": "INVALID_TEST_PLAN_SCHEMA", "validation_errors": error.errors(include_input=False, include_context=False, include_url=False)}
                self.store.append_event(event_id=f"event-{uuid4().hex}", run_id=self.assignment.run_id, task_id=self.assignment.task_id, tester_id=self.assignment.tester_id, event_type="TESTER_PLAN", tool="TesterPlan", action="exception_replan" if phase == "tester_exception_replan" else "initial_plan", result={"schema_rejected": True}, latency_ms=0)
            failure = {key: result[key] for key in ("reason", "failed_goal", "unavailable_references", "missing_check_ids", "missing_behavior_ids", "validation_errors", "binding_field", "allowed_contexts", "instruction") if key in result} if not result["success"] else None
            if failure is not None:
                execution = result.get("failed_execution", {})
                operations = (execution.get("results") or [{}])[-1].get("operations", {})
                failure["pending_inputs"] = operations.get("pending_inputs", [])
                last_action = (operations.get("actions") or [{}])[-1]
                failure["last_action"] = {key: last_action[key] for key in ("action", "control", "value_reference") if key in last_action} or None
            self.task_context["latest_plan_failure"] = failure
            self.task_context["last_plan_submitted"] = True
            trace_result(span, success=result["success"], failure_reason=result.get("reason"))
            return result

    async def _execute_test_plan(self, goals: list[PageGoal], related_task_ids: list[str] | None = None) -> dict[str, Any]:
        """Submit the COMPLETE remaining Task plan once. Runtime/Playwright execute operations and checks; Jev only selects current bounded alternatives. No Tester model turns between goals. Call again only to replace an exceptional blocked plan; successful goals must not be repeated."""
        if self.plan_started:
            try:
                self.runtime.budget.ensure_can_start("replan")
            except BudgetExceededError as error:
                await self.runtime.stop_task(error.reason)
                return {"success": False, "reason": error.reason}
            self.runtime.budget.record_replan()
        phase = "exception_replan" if self.plan_started else "initial_plan"
        self.plan_started = True
        goals = TypeAdapter(list[PageGoal]).validate_python(goals)
        self.store.append_event(event_id=f"event-{uuid4().hex}", run_id=self.assignment.run_id, task_id=self.assignment.task_id, tester_id=self.assignment.tester_id, event_type="TESTER_PLAN", tool="TesterPlan", action=phase, result={"phase": phase, "goal_count": len(goals), "goals": [{"goal": goal.goal, "inputs": [asdict(binding) for binding in goal.inputs], "context": goal.context, "project_reference": goal.project_reference, "row_reference": goal.row_reference, "form_context": goal.form_context, "operation": goal.operation, "destination": goal.destination, "run_operations": goal.run_operations} for goal in goals]}, latency_ms=0)
        current_outcome = await self.runtime.record_task_outcome()
        current_assertions = current_outcome["assertions"]
        assert isinstance(current_assertions, list)
        recorded = {check.get("behavior_id") for check in current_assertions}
        planned: set[str] = set()
        planned_checks: set[str] = set()
        check_specs = {check["check_id"]: check for check in self.runtime.required_checks}
        boundaries = {ActionType.NAVIGATION, ActionType.REFRESH, ActionType.REPEAT_SUBMIT, ActionType.WAIT, ActionType.ASSERTION, ActionType.URL_CHECK}
        scopes = self.binding_scopes(await self._page_summary())
        names: set[str] = set()
        for goal in goals:
            if not goal.goal.strip() or goal.goal in names or not 1 <= goal.max_steps <= 30:
                return {"success": False, "reason": "INVALID_TEST_PLAN", "instruction": "Use distinct, nonempty business goals and valid operation limits."}
            names.add(goal.goal)
            steps = [*goal.before_steps, *goal.checks, *goal.after_steps]
            references = [binding.value_reference for binding in goal.inputs] + [reference for reference in (goal.project_reference, goal.row_reference) if reference] + [reference for step in steps for reference in (step.value_reference, step.expected_reference, step.project_reference) if reference]
            unavailable = [reference for reference in references if reference not in self.runtime.input_values]
            unavailable.extend(binding.value_reference for binding in goal.inputs if not binding.control.strip())
            if unavailable:
                return {"success": False, "reason": "INPUT_VALUE_UNAVAILABLE", "unavailable_references": unavailable, "available_references": sorted(self.allowed_input_references()), "instruction": "Use existing input references only. Do not invent credentials or extend the assigned Task with unsupported extra tests. If the assigned Task truly needs missing data, report the blocker rather than pretending to run the check."}
            disallowed = sorted(set(references) - self.allowed_input_references())
            if disallowed:
                return {"success": False, "reason": "INPUT_REFERENCE_NOT_ALLOWED", "unavailable_references": disallowed, "available_references": sorted(self.allowed_input_references())}
            if any(reference.startswith("env:") or not self.runtime.input_values[reference].strip() for reference in (goal.project_reference, goal.row_reference) if reference):
                return {"success": False, "reason": "INVALID_OBJECT_REFERENCE"}
            if self.task_context_supplied and goal.run_operations and goal.operation is None:
                return {"success": False, "reason": "EXPLICIT_OPERATION_REQUIRED", "instruction": "Declare one operation per goal; navigate uses a human destination. Runtime, not Jev, manages operation phases."}
            if goal.operation == "navigate" and not goal.destination:
                return {"success": False, "reason": "NAVIGATION_DESTINATION_REQUIRED"}
            if goal.operation in {"edit", "delete"} and not (goal.row_reference or goal.context):
                return {"success": False, "reason": "ROW_REFERENCE_REQUIRED"}
            contexts = [("goal.context", goal.context, "object_contexts"), ("goal.form_context", goal.form_context, "form_contexts")]
            contexts.extend(("input.context", binding.context, "input_contexts") for binding in goal.inputs)
            contexts.extend(("step.context", step.context, "object_contexts") for step in steps)
            for binding_field, context, scope in contexts:
                if context not in scopes[scope]:
                    return {"success": False, "reason": "INVALID_BINDING_CONTEXT", "binding_field": binding_field, "allowed_contexts": scopes[scope], "instruction": "Use an exact supplied binding key. Leave optional context empty for future/unambiguous forms; put operation and field descriptions in goal."}
            if goal.inputs and not goal.run_operations:
                return {"success": False, "reason": "INPUTS_REQUIRE_PAGE_OPERATIONS"}
            if any(check.action_type not in {ActionType.ASSERTION, ActionType.URL_CHECK} for check in goal.checks):
                return {"success": False, "reason": "PAGE_GOAL_CHECK_REQUIRES_ASSERTION"}
            if any(step.action_type not in boundaries for step in [*goal.before_steps, *goal.after_steps]):
                return {"success": False, "reason": "PLAN_BOUNDARY_ACTION_REQUIRED", "instruction": "Move INPUT/SELECT bindings into PageGoal.inputs using control/value_reference. Describe click operations in PageGoal.goal so Jev chooses their targets. before_steps/after_steps accept only navigation, refresh, wait, assertions, URL checks and repeat_submit; never a click/input/select sequence."}
            for step in [*goal.before_steps, *goal.checks, *goal.after_steps]:
                if step.expected_reference and step.expected_reference.startswith("env:"):
                    return {"success": False, "reason": "ASSERTION_VALUE_UNAVAILABLE"}
                if step.check_id is not None:
                    if step.check_id not in check_specs or step.behavior_id != check_specs[step.check_id]["behavior_id"]:
                        return {"success": False, "reason": "UNKNOWN_OR_INVALID_CHECK_ID"}
                    expected_action = check_specs[step.check_id].get("action_type", "assertion")
                    allowed_actions = {"assertion", "url_check"} if expected_action == "assertion" else {expected_action}
                    if step.action_type.value not in allowed_actions:
                        return {"success": False, "reason": "UNKNOWN_OR_INVALID_CHECK_ID"}
                    if step.check_id in planned_checks:
                        return {"success": False, "reason": "DUPLICATE_CHECK_ID"}
                    planned_checks.add(step.check_id)
                elif check_specs and step.behavior_id:
                    return {"success": False, "reason": "CHECK_ID_REQUIRED"}
                if step.behavior_id:
                    if step.action_type not in {ActionType.ASSERTION, ActionType.URL_CHECK, ActionType.REPEAT_SUBMIT} or step.behavior_id not in self.runtime.expected_behavior_ids:
                        return {"success": False, "reason": "UNKNOWN_OR_INVALID_BEHAVIOR_CHECK", "invalid_behavior_id": step.behavior_id, "assigned_behavior_ids": list(self.runtime.expected_behavior_ids), "instruction": "Use only the actual assigned behavior IDs for goal checks. Do not invent IDs for setup checks."}
                    planned.add(step.behavior_id)
        missing = sorted(set(self.runtime.expected_behavior_ids) - recorded - planned)
        scored_checks = current_outcome["checks"]
        assert isinstance(scored_checks, list)
        recorded_checks = {check["check_id"] for check in scored_checks if check["completed"]}
        missing_checks = sorted(set(check_specs) - recorded_checks - planned_checks)
        if missing_checks:
            return {"success": False, "reason": "INCOMPLETE_TEST_PLAN", "missing_check_ids": missing_checks}
        if not goals and current_outcome["ready_to_finish"]:
            return {"success": True, "outcome": await self.finish_task(), "completed_check_ids": sorted(recorded_checks)}
        if not goals or missing:
            return {"success": False, "reason": "INCOMPLETE_TEST_PLAN", "missing_behavior_ids": missing, "instruction": "Submit every remaining required operation and actual assertion before execution."}
        self.executing_plan = True
        try:
            for index, goal in enumerate(goals):
                current_outcome = await self.runtime.record_task_outcome()
                outcome_checks = current_outcome["checks"]
                assert isinstance(outcome_checks, list)
                recorded_checks = {check["check_id"] for check in outcome_checks if check["completed"]}
                goal_steps = [*goal.before_steps, *goal.checks, *goal.after_steps]
                goal_checks = {step.check_id for step in goal_steps if step.check_id}
                if goal_checks and goal_checks <= recorded_checks:
                    continue
                if not check_specs:
                    behaviors = {step.behavior_id for step in goal_steps if step.behavior_id}
                    recorded_behaviors = {check["behavior_id"] for check in outcome_checks if check["completed"]}
                    if behaviors and behaviors <= recorded_behaviors:
                        continue
                goal = replace(goal, before_steps=[step for step in goal.before_steps if step.check_id not in recorded_checks], checks=[step for step in goal.checks if step.check_id not in recorded_checks], after_steps=[step for step in goal.after_steps if step.check_id not in recorded_checks])
                result = await self.execute_page_goals([goal], related_task_ids=related_task_ids)
                operations = (result.get("results") or [{}])[-1].get("operations", {})
                if not result["success"]:
                    reason = result.get("reason") or result.get("error_type") or operations.get("reason") or "PLAN_EXECUTION_BLOCKED"
                    if str(reason).startswith("JEV_ERROR:"):
                        await self.runtime.stop_task(str(reason))
                    outcome = await self.runtime.record_task_outcome()
                    outcome_checks = outcome["checks"]
                    assert isinstance(outcome_checks, list)
                    completed = sorted(check["check_id"] for check in outcome_checks if check["completed"])
                    return {"success": False, "reason": reason, "failed_goal": index, "completed_check_ids": completed, "remaining_check_ids": sorted(set(check_specs) - set(completed)), "failed_execution": result, "instruction": "Replan only the remaining checks from this current page. Do not repeat completed goals or successful actions."}
            outcome = await self.finish_task()
            outcome_checks = outcome["checks"]
            assert isinstance(outcome_checks, list)
            return {"success": bool(outcome["finished"]), "outcome": outcome, "completed_check_ids": sorted(check["check_id"] for check in outcome_checks if check["completed"])}
        finally:
            self.executing_plan = False

    async def execute_page_steps(self, steps: list[PageStep], related_task_ids: list[str] | None = None, finish: bool = False) -> dict[str, Any]:
        """Execute ordered semantic steps; resolve controls with Playwright/Jev. Inputs require references. Assertion failures are recorded immediately and execution continues; other failures stop the batch. finish=True checks and finishes the Task."""
        steps = TypeAdapter(list[PageStep]).validate_python(steps)
        results: list[dict[str, Any]] = []
        for index, step in enumerate(steps):
            if step.behavior_id is not None and step.action_type not in {ActionType.ASSERTION, ActionType.URL_CHECK, ActionType.REPEAT_SUBMIT}:
                return {"success": False, "failed_step": index, "error_type": "BEHAVIOR_REQUIRES_ASSERTION", "results": results}
            if step.expected_reference is not None:
                if step.expected_reference.startswith("env:") or step.expected_reference not in self.runtime.input_values:
                    return {"success": False, "failed_step": index, "error_type": "ASSERTION_VALUE_UNAVAILABLE", "results": results}
                step.expected = self.runtime.input_values[step.expected_reference]
            if step.action_type == ActionType.ASSERTION and step.assertion in {"visible", "hidden"} and step.expected is not None:
                return {"success": False, "failed_step": index, "error_type": "VISIBILITY_DOES_NOT_COMPARE_TEXT", "instruction": "Use contains/equals for expected text; visible/hidden checks only the target locator.", "results": results}
            if step.action_type == ActionType.ASSERTION and step.assertion == "count" and step.control is not None:
                return {"success": False, "failed_step": index, "error_type": "COUNT_REQUIRES_ROW_SELECTOR", "instruction": "Use a target matching all relevant rows, not one selected cell.", "results": results}
            if step.action_type in {ActionType.INPUT, ActionType.SELECT} and step.value_reference is None:
                return {"success": False, "failed_step": index, "error_type": "INPUT_REFERENCE_REQUIRED", "results": results}
            if step.control is not None:
                if step.action_type not in {ActionType.INPUT, ActionType.SELECT, ActionType.CLICK, ActionType.REPEAT_SUBMIT, ActionType.ASSERTION}:
                    return {"success": False, "failed_step": index, "error_type": "CONTROL_ACTION_UNSUPPORTED", "results": results}
                if step.value_reference is not None and step.value_reference not in self.runtime.input_values:
                    return {"success": False, "failed_step": index, "error_type": "INPUT_VALUE_UNAVAILABLE", "results": results}
                if step.behavior_id is not None and step.behavior_id not in self.runtime.expected_behavior_ids:
                    return {"success": False, "failed_step": index, "error_type": "UNKNOWN_EXPECTED_BEHAVIOR", "results": results}
                action = WebAction(action_type=step.action_type, value_reference=step.value_reference, value=self.runtime.input_values.get(step.value_reference or ""), url=step.url, expected=step.expected, assertion=step.assertion, behavior_id=step.behavior_id, check_id=step.check_id, goal_check=step.behavior_id is not None)
                decision = await self.runtime.execute_page_action(action, control=step.control, context=step.context, project_reference=step.project_reference)
                result = decision.action_result.to_dict() if decision.action_result is not None else {"success": False, "error_type": decision.reason}
                result["source"] = decision.source
                result["resolved_target"] = decision.candidate.target if decision.candidate is not None else None
            else:
                result = await self.execute_known_action(step.action_type, target=step.target, value_reference=step.value_reference, url=step.url, expected=step.expected, assertion=step.assertion, behavior_id=step.behavior_id, check_id=step.check_id, project_reference=step.project_reference, goal_check=step.behavior_id is not None, key=step.key, wait_ms=step.wait_ms)
                result["source"] = "PLAYWRIGHT"
                result["resolved_target"] = step.target
            self.store.append_event(event_id=f"event-{uuid4().hex}", run_id=self.assignment.run_id, task_id=self.assignment.task_id, tester_id=self.assignment.tester_id, event_type="PAGE_STEP", tool="TesterPageSteps", action=step.action_type.value, result={"source": result["source"], "semantic_control": step.control is not None, "success": result["success"], "behavior_id": step.behavior_id, "browser_event_id": result.get("event_id")}, latency_ms=0)
            compact = {"step": index, "action": step.action_type.value, "success": result["success"], "source": result["source"]}
            if step.behavior_id:
                compact["behavior_id"] = step.behavior_id
            if not result["success"]:
                compact["error_type"] = result.get("error_type")
                compact["actual"] = result.get("data", {}).get("actual")
                if result.get("error_type") == "ASSERTION_FAILURE" and step.behavior_id:
                    session_id = self.runtime.browser_session_id
                    assert session_id is not None
                    page = self.runtime.browser_manager.get_session(session_id).page
                    expected_result = f"{result.get('resolved_target') or 'body'} {step.assertion} {step.expected or ''}"
                    finding = next((finding for finding in self.store.list_recent_findings(self.assignment.run_id, limit=1000) if finding["task_id"] == self.assignment.task_id and finding["action"] == step.behavior_id and finding["expected_result"] == expected_result), None)
                    if finding is None:
                        finding = await self.record_finding(title=f"{step.behavior_id}: {step.assertion} check failed", status="ANOMALY", expected_result=expected_result, actual_result=str(compact["actual"]), affected_page=page.url, action=step.behavior_id, behavior_id=step.behavior_id, check_id=step.check_id, related_task_ids=related_task_ids)
                    compact["finding_id"] = finding["finding_id"]
                    self.store.append_event(event_id=f"event-{uuid4().hex}", run_id=self.assignment.run_id, task_id=self.assignment.task_id, tester_id=self.assignment.tester_id, event_type="CHECK_FINDING_LINKED", tool="Python", action="link_check_evidence", result={"finding_id": finding["finding_id"], "check_id": step.check_id, "behavior_id": step.behavior_id, "boundary_event_id": result.get("event_id")}, latency_ms=0)
                elif result.get("error_type") != "ASSERTION_FAILURE":
                    results.append(compact)
                    return {"success": False, "failed_step": index, "reason": result.get("error_type") or "PAGE_STEP_BLOCKED", "results": results, "page": await self._page_summary()}
            results.append(compact)
        if finish:
            return {"success": True, "results": results, "outcome": await self.finish_task()}
        return {"success": True, "results": results, "page": await self._page_summary()}

    async def _page_summary(self) -> dict[str, Any]:
        if self.runtime.browser_session_id is None:
            return {}
        page = self.runtime.browser_manager.get_session(self.runtime.browser_session_id).page
        state = await self.runtime.page_state_reader.read(page)
        requests = [{key: record[key] for key in ("method", "url", "status") if key in record} for record in self.runtime.evidence_buffer.network_records[-12:]]
        return {**state.selection_summary(), "text": state.visible_dom[:3500], "rows": await self.runtime.page_state_reader.read_rows(page), "observed_requests": requests}

    async def execute_known_action(
        self,
        action_type: ActionType,
        target: str | None = None,
        value: str | None = None,
        value_reference: str | None = None,
        url: str | None = None,
        expected: str | None = None,
        assertion: Literal["contains", "not_contains", "equals", "visible", "hidden", "count"] = "contains",
        key: str | None = None,
        wait_ms: int = 0,
        resource_id: str | None = None,
        requires_resource: bool = False,
        confirmed: bool = False,
        behavior_id: str | None = None,
        goal_check: bool = False,
        check_id: str | None = None,
        project_reference: str | None = None,
    ) -> dict[str, Any]:
        """Execute one known action through the permission-checked Web Testing Runtime."""
        try:
            parsed_action = ActionType(action_type)
        except ValueError:
            return {
                "success": False,
                "error_type": "INVALID_ACTION",
                "error": f"unsupported action: {action_type}",
            }
        if target is None and (parsed_action == ActionType.DOM_INSPECTION or parsed_action == ActionType.ASSERTION and assertion in {"contains", "not_contains", "equals"}):
            target = "body"
        if behavior_id is not None and behavior_id not in self.runtime.expected_behavior_ids:
            return {"success": False, "error_type": "UNKNOWN_EXPECTED_BEHAVIOR"}
        if parsed_action == ActionType.WAIT:
            if target is not None and target.isdecimal() and wait_ms == 0:
                wait_ms, target = int(target), None
            if wait_ms < 0 or wait_ms > 2_000:
                return {"success": False, "error_type": "INVALID_WAIT_DURATION", "instruction": "Use wait_ms up to 2000 for a short page wait; use wait_for_shared_progress for collaboration."}
        if value_reference is not None:
            if value_reference not in self.runtime.input_values:
                return {"success": False, "error_type": "INPUT_VALUE_UNAVAILABLE", "reference": value_reference}
            value = self.runtime.input_values[value_reference]
        elif parsed_action in {ActionType.INPUT, ActionType.SELECT} and value is not None:
            references = [reference for reference, configured in self.runtime.input_values.items() if configured == value]
            if len(references) != 1:
                return {"success": False, "error_type": "INPUT_REFERENCE_REQUIRED"}
            value_reference = references[0]
        action = WebAction(
            action_type=parsed_action,
            target=target,
            value=value,
            value_reference=value_reference,
            url=url,
            expected=expected,
            assertion=assertion,
            key=key,
            wait_ms=wait_ms,
            resource_id=resource_id,
            requires_resource=requires_resource,
            confirmed=confirmed,
            behavior_id=behavior_id,
            goal_check=goal_check,
            check_id=check_id,
            project_reference=project_reference,
        )
        return (await self.runtime.execute_known_action(action)).to_dict()

    async def explore_unknown_path(self, current_goal: str) -> dict[str, Any]:
        """Use Candidate Builder and Jev for an unknown legal path."""
        if self.decision_policy == "TESTER_LLM_EVERY_DECISION":
            if self.runtime.browser_session_id is None:
                raise RuntimeError("browser session has not started")
            session = self.runtime.browser_manager.get_session(self.runtime.browser_session_id)
            page_state = await self.runtime.page_state_reader.read(session.page)
            candidates = self.runtime.candidate_builder.build(goal=current_goal, page_state=page_state, explored_targets=set())
            self.pending_candidates = {candidate.candidate_id: candidate for candidate in candidates}
            self.pending_page_state = page_state
            self.pending_goal = current_goal
            return {"source": "TESTER_LLM", "candidates": [{"candidate_id": candidate.candidate_id, "action": candidate.action, "label": candidate.label} for candidate in candidates], "instruction": "Choose one Candidate ID with select_candidate. The Runtime will revalidate it."}
        return asdict(await self.runtime.explore_unknown_path(current_goal))

    async def select_candidate(self, candidate_id: str) -> dict[str, Any]:
        """Execute one LLM-selected Candidate after Runtime revalidation."""
        async with self.runtime.page_lock:
            if self.runtime.task_finished:
                return {"success": False, "error_type": "TASK_ALREADY_FINISHED"}
            return await self._select_candidate(candidate_id)

    async def _select_candidate(self, candidate_id: str) -> dict[str, Any]:
        if self.decision_policy != "TESTER_LLM_EVERY_DECISION" or self.pending_page_state is None or self.runtime.browser_session_id is None:
            return {"success": False, "error_type": "CANDIDATE_NOT_AVAILABLE"}
        candidate = self.pending_candidates.get(candidate_id)
        if candidate is None:
            return {"success": False, "error_type": "ILLEGAL_CANDIDATE_ID"}
        session = self.runtime.browser_manager.get_session(self.runtime.browser_session_id)
        current_page_state = await self.runtime.page_state_reader.read(session.page)
        validation = self.runtime.candidate_builder.validate(candidate=candidate, current_page_state=current_page_state)
        self.pending_candidates = {}
        if not validation.valid:
            return {"success": False, "error_type": validation.reason}
        if candidate.action == "request_replan":
            await self.runtime.request_replan("TESTER_LLM_REQUESTED_REPLAN")
            return {"success": True, "action": "request_replan"}
        if candidate.action == "stop_current_path":
            return {"success": True, "action": "stop_current_path"}
        assert validation.action is not None
        result = await self.runtime._execute_known_action(validation.action)
        if result.success:
            self.runtime.store.record_path(run_id=self.assignment.run_id, feature=self.pending_goal or "", page=self.pending_page_state.url, page_state_id=self.pending_page_state.state_id, action=candidate.action, result="SUCCESS", last_tester=self.assignment.tester_id)
        return result.to_dict()

    def read_coverage(self) -> list[dict[str, Any]]:
        """Read coverage already recorded for the current run."""
        paths = self.store.list_paths(self.assignment.run_id)
        return paths if self.shared_state else [path for path in paths if path["last_tester"] == self.assignment.tester_id]

    def read_shared_facts(self) -> dict[str, Any]:
        """Read structured collaboration facts without another Tester's conversation."""
        progress = self.store.list_events(self.assignment.run_id, event_types=("TASK_PROGRESS",), limit=50)
        return {
            "current_task": self.store.get_task(self.assignment.task_id),
            "coverage": self.read_coverage(),
            "recent_findings": [finding for finding in self.store.list_recent_findings(self.assignment.run_id) if self.shared_state or finding["first_seen_by"] == self.assignment.tester_id],
            "tester_progress": [event for event in progress if self.shared_state or event["tester_id"] == self.assignment.tester_id],
            "tasks": [{key: task[key] for key in ("task_id", "goal", "status", "dependencies", "assigned_tester")} for task in self.store.list_tasks(self.assignment.run_id) if self.shared_state or task["task_id"] == self.assignment.task_id],
        }

    async def wait_for_shared_progress(self, summary_contains: str, task_id: str | None = None, timeout_seconds: int = 30) -> dict[str, Any]:
        """Wait for a structured progress signal without model polling. Stop a live-session deadlock when the required participant depends on this Task."""
        if not self.shared_state:
            return {"ready": False, "reason": "SHARED_STATE_DISABLED"}
        started = perf_counter()
        timeout_seconds = min(max(timeout_seconds, 0), 60)
        while True:
            for event in self.store.list_events(self.assignment.run_id, event_types=("TASK_PROGRESS",), limit=100):
                if event["task_id"] != self.assignment.task_id and (task_id is None or event["task_id"] == task_id) and summary_contains in str(event["result"].get("summary", "")):
                    return {"ready": True, "task_id": event["task_id"], "event_id": event["event_id"]}
            participants = [task for task in self.store.list_tasks(self.assignment.run_id) if task["task_id"] != self.assignment.task_id and (task_id is None or task["task_id"] == task_id)]
            if participants and all(task["status"] == "PENDING" and self.assignment.task_id in task["dependencies"] for task in participants):
                await self.runtime.request_replan("LIVE_PARTICIPANT_DEPENDS_ON_OBSERVER")
                await self.runtime.stop_task("LIVE_PARTICIPANT_DEPENDS_ON_OBSERVER")
                return {"ready": False, "reason": "LIVE_PARTICIPANT_DEPENDS_ON_OBSERVER"}
            if perf_counter() - started >= timeout_seconds:
                return {"ready": False, "reason": "PROGRESS_SIGNAL_NOT_AVAILABLE", "tasks": participants}
            await asyncio.sleep(0.2)

    def check_path_before_exploring(
        self,
        feature: str,
        page: str,
        page_state_id: str,
        action: str,
        new_reason: str | None = None,
    ) -> dict[str, Any]:
        """Check exact shared Coverage before starting a potentially duplicate path."""
        existing = self.store.get_path(
            run_id=self.assignment.run_id,
            feature=feature,
            page=page,
            page_state_id=page_state_id,
            action=action,
        )
        if existing is not None and not self.shared_state and existing["last_tester"] != self.assignment.tester_id:
            existing = None
        should_explore = existing is None or bool(new_reason and new_reason.strip())
        if not should_explore:
            assert existing is not None
            self.store.append_event(
                event_id=f"event-{uuid4().hex}",
                run_id=self.assignment.run_id,
                task_id=self.assignment.task_id,
                tester_id=self.assignment.tester_id,
                browser_session_id=self.runtime.browser_session_id,
                event_type="DUPLICATE_EXPLORATION",
                tool="TesterAgent",
                action="skip_covered_path",
                result={
                    "feature": feature,
                    "page": page,
                    "page_state_id": page_state_id,
                    "action": action,
                    "existing_path_id": existing["path_id"],
                },
                latency_ms=0,
            )
        return {
            "should_explore": should_explore,
            "existing_coverage": existing,
            "reason": "NEW_REASON" if should_explore and existing is not None else "NOT_COVERED" if existing is None else "ALREADY_COVERED",
        }

    async def record_finding(
        self,
        title: str,
        status: Literal["OBSERVATION", "ANOMALY", "SUSPECTED_ISSUE"],
        expected_result: str,
        actual_result: str,
        severity_hint: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"] | None = None,
        affected_page: str | None = None,
        action: str | None = None,
        error_text: str | None = None,
        reproduction_steps: list[dict[str, Any]] | None = None,
        behavior_id: str | None = None,
        related_task_ids: list[str] | None = None,
        check_id: str | None = None,
    ) -> dict[str, Any]:
        """Record an early Finding without declaring a confirmed bug."""
        if status not in {"OBSERVATION", "ANOMALY", "SUSPECTED_ISSUE"}:
            raise ValueError("Tester may only create an early Finding status")
        if behavior_id is not None and behavior_id not in self.runtime.expected_behavior_ids:
            raise ValueError("Finding must reference an assigned expected behavior")
        related = sorted(set(related_task_ids or []))
        for task_id in related:
            task = self.store.get_task(task_id)
            if task is None or task["run_id"] != self.assignment.run_id:
                raise ValueError("Related replay tasks must belong to this Run")
        if severity_hint is not None and severity_hint not in {
            "LOW",
            "MEDIUM",
            "HIGH",
            "CRITICAL",
        }:
            raise ValueError("unsupported severity hint")
        history = self.store.list_action_history(run_id=self.assignment.run_id, task_id=self.assignment.task_id)
        boundary = history[-1] if history else None
        if status in {"ANOMALY", "SUSPECTED_ISSUE"} and boundary is not None and not boundary["success"] and boundary["action_data"].get("action_type") in {"assertion", "url_check"}:
            assigned_behavior = behavior_id or boundary["action_data"].get("behavior_id")
            for event in self.store.list_events(self.assignment.run_id, event_types=("FINDING_CREATED",), task_id=self.assignment.task_id):
                if event["result"].get("boundary_event_id") == boundary["event_id"] and event["result"].get("behavior_id") == assigned_behavior:
                    existing = self.store.get_finding(str(event["result"]["finding_id"]))
                    if existing is not None and existing["status"] in {"ANOMALY", "SUSPECTED_ISSUE"}:
                        return existing
        finding = self.store.create_finding(
            finding_id=f"finding-{uuid4().hex}",
            run_id=self.assignment.run_id,
            task_id=self.assignment.task_id,
            title=title,
            status=status,
            expected_result=expected_result,
            actual_result=actual_result,
            first_seen_by=self.assignment.tester_id,
            severity_hint=severity_hint,
            needs_confirmation=True,
            affected_role=self.assignment.role,
            affected_page=affected_page,
            action=action,
            error_text=error_text,
            reproduction_steps=reproduction_steps or [],
        )
        self.store.append_event(
            event_id=f"event-{uuid4().hex}",
            run_id=self.assignment.run_id,
            task_id=self.assignment.task_id,
            tester_id=self.assignment.tester_id,
            browser_session_id=self.runtime.browser_session_id,
            event_type="FINDING_CREATED",
            url=affected_page,
            tool="TesterAgent",
            action="record_finding",
            result={
                "finding_id": finding["finding_id"],
                "status": finding["status"],
                "severity_hint": finding["severity_hint"],
                "action": finding["action"],
                "boundary_event_id": history[-1]["event_id"] if history else None,
                "behavior_id": behavior_id,
                "check_id": check_id or (history[-1]["action_data"].get("check_id") if history else None),
                "related_task_ids": related,
            },
            latency_ms=0,
        )
        await self.runtime.capture_finding_evidence(str(finding["finding_id"]))
        return finding

    async def record_observation(
        self,
        title: str,
        expected_result: str,
        actual_result: str,
        affected_page: str | None = None,
        action: str | None = None,
        error_text: str | None = None,
    ) -> dict[str, Any]:
        """Record an observation without declaring a confirmed bug."""
        return await self.record_finding(
            title=title,
            status="OBSERVATION",
            expected_result=expected_result,
            actual_result=actual_result,
            affected_page=affected_page,
            action=action,
            error_text=error_text,
        )

    async def update_task_progress(
        self, progressed: bool, summary: str
    ) -> dict[str, object]:
        """Record local Task progress and trigger replan after repeated no-progress steps."""
        return await self.runtime.note_progress(progressed=progressed, summary=summary)

    async def request_replan(self, reason: str) -> dict[str, object]:
        """Request Main Agent replanning without changing global scope."""
        return await self.runtime.request_replan(reason)

    async def use_computer_fallback(self, finding_id: str, goal: str, component_type: str, playwright_failure_reason: str, allowed_visual_actions: list[str]) -> dict[str, Any]:
        """Request one controlled visual action after a recorded Playwright limitation."""
        result = await self.runtime.use_computer_fallback(finding_id=finding_id, goal=goal, component_type=component_type, playwright_failure_reason=playwright_failure_reason, allowed_visual_actions=tuple(allowed_visual_actions))
        return {"status": result.status, "reason": result.reason, "action": result.action, "before_state_id": result.before_state.state_id if result.before_state else None, "after_state_id": result.after_state.state_id if result.after_state else None, "evidence_ids": list(result.evidence_ids)}

    async def finish_task(self) -> dict[str, object]:
        """Finish only after explicit assertions cover assigned behaviors and failed checks have Findings; otherwise return missing checks and keep the Task open."""
        outcome = await self.runtime.record_task_outcome()
        assertions = outcome["assertions"]
        assert isinstance(assertions, list)
        covered = {item.get("behavior_id") for item in assertions}
        missing = sorted(set(self.runtime.expected_behavior_ids) - covered)
        if missing or not outcome["ready_to_finish"]:
            return {**outcome, "finished": False, "missing_behavior_ids": missing, "instruction": "Record actual assertion checks with the supplied behavior_id values. DOM Inspection alone is not a goal check. Record Findings for failed application assertions, then call finish_task."}
        return {**await self.runtime.record_task_outcome(finish=True), "finished": True}

    def read_test_data(self, reference: str) -> dict[str, Any]:
        """Read an allowed non-secret input. Secret references can only be filled by Runtime."""
        value = self.runtime.input_values.get(reference)
        if reference.startswith("env:"):
            return {"reference": reference, "available": value is not None, "instruction": "Pass this reference to execute_known_action; secret values are never returned."}
        return {"reference": reference, "value": value, "available": value is not None}


class FinishTaskMiddleware(FunctionMiddleware):
    def __init__(self, runtime: WebTestingRuntime, tools: TesterAgentTools | None = None) -> None:
        self.runtime = runtime
        self.tools = tools

    async def process(self, context: FunctionInvocationContext, call_next: Callable[[], Awaitable[None]]) -> None:
        if self.tools is not None and self.tools.plan_started and not self.tools.executing_plan and context.function.name != "execute_test_plan":
            context.result = {"success": False, "reason": "TEST_PLAN_REQUIRED", "instruction": "Replace the remaining test plan with execute_test_plan; do not operate the browser one action at a time."}
            await self.runtime.stop_task("TEST_PLAN_REQUIRED")
            raise MiddlewareTermination("Tester returned to individual tools instead of exceptional replanning.", result=context.result)
        await call_next()
        if self.tools is not None and self.tools.plan_started and not self.runtime.task_finished and self.runtime.budget.usage.task_replans >= self.runtime.budget.limits.max_task_replans:
            await self.runtime.stop_task("MAX_TASK_REPLANS_REACHED")
        if self.runtime.task_finished:
            raise MiddlewareTermination("Task outcome recorded; no final Done model round is needed.", result=context.result)
        if self.tools is not None and context.function.name == "execute_test_plan":
            raise MiddlewareTermination("Plan returned; rebuild the exception context before another Tester request.", result=context.result)


def create_tester_agent(
    *, client: Any, assignment: TesterAssignment, tools: TesterAgentTools
) -> Agent:
    client.function_invocation_configuration["allow_concurrent_invocation"] = False
    instructions = (
        "You are the only Tester Agent type. Work only on the assigned Task, scope, identity, namespace, and step budget. "
        "Work in short subgoals. For known targets, issue the ordered execute_known_action calls for one safe subgoal in the same response; the Runtime serializes page actions. "
        "For example, fill the known username and password fields, submit, then inspect in one response. Stop the batch before an unknown target or action. "
        "Inspect at page or subgoal boundaries, not after each successful fill or click. Use explore_unknown_path instead of inventing unknown actions. "
        "Check shared Coverage before starting a path and collaborate only through structured Shared State facts. "
        "Never change global scope, bypass permissions, create another Agent, or declare a confirmed bug. "
        "Record uncertain behavior only with record_observation. Request replan when the local path cannot continue."
        " Mark every actual goal assertion with goal_check=true and its assigned behavior_id; keep setup checks untagged. Use a locator visibility check for inputs/buttons, not a body-text check for placeholders or aria-labels. "
        "For INPUT and SELECT from configured data, always pass value_reference, including blank values and usernames; do not copy the returned value into the value argument. Account secrets use their supplied references directly. "
        "Read nonsecret assertion values once and reuse them. Never invent unavailable inputs. "
        "Use repeat_submit with the same-origin request URL and submit-button target to check one pending repeated form operation. "
        "Immediately record a failed goal check before another browser action, with the same behavior_id. Include related_task_ids for another Task's necessary setup or membership change. Read shared facts for those task references. "
        "If the same completion error or wait repeats without progress, report no progress and request replan rather than repeating identical Findings or waiting indefinitely. "
        "Call finish_task after testing the goal; Python determines success. Do not produce a final Done summary."
    )
    if tools.decision_policy == "JEV" and tools.action_policy == "PLAYWRIGHT":
        instructions = (
            'Test ONLY the operations and checks in the assigned Task goal. Task Contract is authoritative; reference specifications do not add tests or inputs. '
            'You generate one complete initial plan and only exceptional remaining-plan replacements. Call execute_test_plan once with every remaining Check ID. Jev owns continuous page decisions; Playwright executes them. Never issue individual browser actions or return after a subgoal. '
            'Task Contract supplies assigned checks with exact check_id, behavior_id, dependencies and required evidence action; completed and remaining IDs; permitted input keys; preparation and the current page. Do not guess IDs or input references. '
            'Each remaining check_id occurs EXACTLY ONCE in the whole plan on its real assertion (or repeat_submit evidence). Multiple checks may share a behavior_id. A refresh followed by an assertion carries the check_id ONLY on the assertion; the refresh is an untagged setup boundary. Incidental setup checks have neither ID. Preserve required depends_on order. '
            'PageGoal.goal describes a distinct business operation, never a repeated feature/goal_id label. Use distinct goal descriptions for different inputs and stages. Put actual field bindings in inputs with control and value_reference. Every goal declares one operation: navigate/create/edit/delete/submit/login/logout/observe. Navigate uses destination=<human navigation label>. Runtime owns phases, binding, observation and completion; Jev only selects current candidates. run_operations=true delegates execution to Runtime. Do not split locating a form from filling it. Use run_operations=false only for a check-only or boundary-only goal with NO inputs. '
            'checks accepts ONLY assertion or url_check. before_steps and after_steps accept ONLY navigation, refresh, wait, repeat_submit, assertion or url_check. Input/select/click belongs to Runtime-generated operation candidates, never a boundary. Logout/login/reopen are business operations in goals; deterministic refresh/wait are boundary steps. '
            'Use EXACT permitted reference keys, including blank values. Secret references can bind inputs; their values are hidden and cannot be assertions. Current page lists observed human control labels and locators. Use semantic human field labels for future forms; Runtime binds the actual fields. Never invent a CSS selector for an unseen page. '
            'project_reference selects the Project; row_reference identifies the stable existing row; form_context is an optional exact OBSERVED form selector. Context fields are binding keys, not explanations: normally OMIT input.context and form_context. Put field purpose, page description and action instructions ONLY in goal. Use configured nonsecret reference keys for Project/row objects, not prose or a form name. Omit existing-row context for registration/create/login. '
            'Assertions compare the supplied business values, scoped to the assigned object. Prefer control=<observed table column>, context=<row reference> and expected_reference=<provided value>. For visible/hidden specify a real target or scoped column, with NO expected value. Counts need a selector for all relevant rows, never a single cell control. '
            'For pending repeat submission, bind inputs in a run_operations=true goal. Runtime stops after filling before submit. Put repeat_submit in after_steps, using the observed submit target and same-origin collection request URL, with its unique pending-operation check_id. Later DOM checks carry distinct IDs. Two completed ordinary creates are separate goals, not repeat_submit. '
            'Preparation and prepared_check_ids are already established. Preserve completed checks and successful object preparation. An expected application assertion failure creates evidence/Finding locally; continue remaining applicable checks rather than replan for that assertion. Missing prerequisites are execution blockers, not bugs. '
            'For genuine exceptions use latest_failure, current_page and remaining IDs to repair only the remaining complete plan. No unrelated history is needed. Shared coordination uses publish_progress/wait_for_progress and related_task_ids for necessary participants, never model polling. '
            'Keep scope, identity, inputs and budgets unchanged. Never bypass permissions, create Agents, invent tests, duplicate auto-recorded Findings or declare confirmed bugs. Python determines Task Success and verifies deterministic Replay; execute_test_plan finishes the Task without a final Done model call.'
        )
    if tools.action_policy == "TESTER_LLM_EVERY_STEP":
        instructions += " For this evaluation route, decide one browser action per model response. Call at most one browser action tool before reasoning again."
    if tools.decision_policy == "TESTER_LLM_EVERY_DECISION":
        instructions += " For unknown paths, inspect explore_unknown_path candidates and select exactly one ID with select_candidate."
    exposed_tools: list[Any] = [tools.execute_known_action, tools.explore_unknown_path, tools.read_shared_facts, tools.record_finding, tools.record_observation, tools.update_task_progress, tools.request_replan, tools.finish_task, tools.read_test_data]
    if tools.decision_policy == "JEV" and tools.action_policy == "PLAYWRIGHT":
        exposed_tools.insert(0, tools.plan_tool())
        exposed_tools.insert(0, tools.execute_page_goals)
        exposed_tools.insert(0, tools.execute_page_steps)
        exposed_tools.append(tools.wait_for_shared_progress)
    else:
        exposed_tools.extend([tools.read_coverage, tools.check_path_before_exploring])
    if tools.decision_policy == "TESTER_LLM_EVERY_DECISION":
        exposed_tools.append(tools.select_candidate)
    if getattr(tools.runtime, "computer_use_controller", None) is not None:
        exposed_tools.append(tools.use_computer_fallback)
    return Agent(
        client=client,
        name="tester-agent",
        description="Executes one assigned web testing task through the controlled runtime.",
        instructions=instructions,
        middleware=[TraceChatMiddleware(), TraceToolMiddleware(), FinishTaskMiddleware(tools.runtime, tools)],
        tools=exposed_tools,
        additional_properties={"assignment": asdict(assignment), "task_context": tools.task_context},
    )


class TesterRunner:
    def __init__(
        self,
        *,
        agent: Agent,
        assignment: TesterAssignment,
        runtime: WebTestingRuntime,
        store: StateStore,
        budget: BudgetGuard,
        budget_id: str,
        provider_usage_recorded: bool = False,
        decision_policy: str = "JEV",
        action_policy: str = "PLAYWRIGHT",
        prepare_task_page: bool = False,
    ) -> None:
        self.agent = agent
        self.assignment = assignment
        self.runtime = runtime
        self.store = store
        self.budget = budget
        self.budget_id = budget_id
        self.provider_usage_recorded = provider_usage_recorded
        self.prepare_task_page = prepare_task_page
        self.decision_policy = decision_policy
        self.action_policy = action_policy
        self.session = AgentSession(session_id=f"tester-session-{uuid4().hex}")
        self.plan_tools: TesterAgentTools | None = None
        for tool in agent.default_options.get("tools", []):
            owner = getattr(getattr(tool, "func", None), "__self__", None)
            if tool.name == "execute_test_plan" and isinstance(owner, TesterAgentTools):
                self.plan_tools = owner

    async def execute_known(self, action: WebAction) -> dict[str, Any]:
        """Known Replay and assertions bypass the LLM and go directly to Playwright."""
        if self.action_policy == "TESTER_LLM_EVERY_STEP":
            answer = await self.ask_tester_llm(f"Decide whether the next scoped known action is safe: {action.action_type.value}. Reply APPROVE or STOP without calling tools.")
            if answer.startswith("STOPPED:"):
                return {"success": False, "error_type": "BUDGET_STOP", "error": answer}
            if not answer.strip().upper().startswith("APPROVE"):
                return {"success": False, "error_type": "MODEL_STEP_STOP", "error": answer}
        return (await self.runtime.execute_known_action(action)).to_dict()

    async def explore(self, current_goal: str) -> dict[str, Any]:
        if self.decision_policy == "TESTER_LLM_EVERY_DECISION":
            return {"tester_llm": await self.ask_tester_llm(f"Choose the next legal action for: {current_goal}. Use only the provided runtime tools and permissions.")}
        decision = await self.runtime.explore_unknown_path(current_goal)
        result: dict[str, Any] = {"decision": asdict(decision)}
        if decision.needs_tester_llm:
            result["tester_llm"] = await self.ask_tester_llm(
                f"The constrained selector could not continue. Goal: {current_goal}. Reason: {decision.reason}. Use only the provided tools or request replan."
            )
        return result

    async def ask_tester_llm(self, prompt: str) -> str:
        try:
            self.budget.ensure_can_start("llm")
        except BudgetExceededError as error:
            await self.runtime.stop_task(error.reason)
            return f"STOPPED: {error.reason}"
        if self.prepare_task_page and self.decision_policy == "JEV" and self.action_policy == "PLAYWRIGHT" and prompt.startswith("Execute assigned Task "):
            prompt = await self._prepare_task_prompt(prompt)
            if self.runtime.task_finished:
                return "STOPPED: TASK_PREPARATION_STOPPED"
        if self.plan_tools is not None and self.decision_policy == "JEV" and self.action_policy == "PLAYWRIGHT" and prompt.startswith("Execute assigned Task "):
            return await self._run_complete_plan(prompt)
        started_at = perf_counter()
        try:
            response = await self.agent.run(prompt, session=self.session)
        except BudgetExceededError as error:
            await self.runtime.stop_task(error.reason)
            return f"STOPPED: {error.reason}"
        assert isinstance(response, AgentResponse)
        if self.provider_usage_recorded:
            return response.text
        latency_seconds = perf_counter() - started_at
        usage = response.usage_details or {}
        input_tokens = int(usage.get("input_token_count") or 0)
        output_tokens = int(usage.get("output_token_count") or 0)
        cost = float((response.additional_properties or {}).get("cost", 0) or 0)
        self.budget.record_llm_call(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            runtime_seconds=latency_seconds,
            cost=cost,
        )
        self.store.update_budget(
            budget_id=self.budget_id,
            llm_calls=1,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            runtime_seconds=latency_seconds,
            estimated_cost=cost,
        )
        self.store.append_event(
            event_id=f"event-{uuid4().hex}",
            run_id=self.assignment.run_id,
            task_id=self.assignment.task_id,
            tester_id=self.assignment.tester_id,
            browser_session_id=self.runtime.browser_session_id,
            event_type="LLM_CALL",
            tool="Microsoft Agent Framework",
            action="complex_reasoning",
            result={
                "agent": "tester",
                "finish_reason": str(response.finish_reason)
                if response.finish_reason is not None
                else None
            },
            latency_ms=latency_seconds * 1_000,
            cost=cost,
        )
        return response.text

    async def _run_complete_plan(self, prompt: str) -> str:
        assert self.plan_tools is not None
        tools = self.plan_tools
        if not tools.task_context.get("goal"):
            tools.task_context["goal"] = prompt.split("\nTask Context:")[0].split("\nInitial Page")[0].replace("Finish with finish_task.", "")
        original_tools = self.agent.default_options.get("tools")
        latest_failure = tools.task_context.get("latest_plan_failure")
        missing_response = False
        try:
            for attempt in range(self.budget.limits.max_task_replans + 1):
                self.budget.ensure_can_start("llm")
                if attempt:
                    self.budget.ensure_can_start("replan")
                    if not tools.plan_started:
                        self.budget.record_replan()
                contract = await tools.planning_context(latest_failure)
                self.session = AgentSession(session_id=self.session.session_id)
                tools.task_context["last_plan_submitted"] = False
                self.agent.default_options["tools"] = [tools.plan_tool(contract["current_page"])]
                request = "Submit ONE complete executable plan for ALL remaining checks via execute_test_plan. Use this authoritative Task Contract; do not invent references, repeat completed checks, or perform individual browser actions.\nTask Contract: " + json.dumps(contract, ensure_ascii=False)
                phase = "tester_exception_replan" if attempt else "tester_initial_plan"
                started_at = perf_counter()
                with trace_span("TesterPlanGeneration", metadata={"agent_role": "tester", "task_id": self.assignment.task_id, "phase": phase}):
                    response = await self.agent.run(request, session=self.session, options={"tool_choice": "auto"})
                if not self.provider_usage_recorded:
                    usage = response.usage_details or {}
                    elapsed = perf_counter() - started_at
                    inputs = int(usage.get("input_token_count") or 0)
                    outputs = int(usage.get("output_token_count") or 0)
                    cost = float((response.additional_properties or {}).get("cost", 0) or 0)
                    self.budget.record_llm_call(input_tokens=inputs, output_tokens=outputs, runtime_seconds=elapsed, cost=cost)
                    self.store.update_budget(budget_id=self.budget_id, llm_calls=1, input_tokens=inputs, output_tokens=outputs, runtime_seconds=elapsed, estimated_cost=cost)
                    self.store.append_event(event_id=f"event-{uuid4().hex}", run_id=self.assignment.run_id, task_id=self.assignment.task_id, tester_id=self.assignment.tester_id, event_type="LLM_CALL", tool="Microsoft Agent Framework", action="plan_generation", result={"agent": "tester", "phase": phase}, latency_ms=elapsed * 1000, cost=cost)
                if self.runtime.task_finished:
                    return response.text
                if not tools.task_context["last_plan_submitted"]:
                    if missing_response:
                        await self.runtime.stop_task("TEST_PLAN_REQUIRED")
                        return "STOPPED: TEST_PLAN_REQUIRED"
                    missing_response = True
                    latest_failure = {"reason": "TEST_PLAN_REQUIRED", "instruction": "CALL execute_test_plan with all remaining checks; prose does not execute testing."}
                    self.store.append_event(event_id=f"event-{uuid4().hex}", run_id=self.assignment.run_id, task_id=self.assignment.task_id, tester_id=self.assignment.tester_id, event_type="TESTER_PLAN_REJECTED", tool="TesterPlan", action="missing_plan", result=latest_failure, latency_ms=0)
                else:
                    latest_failure = tools.task_context.get("latest_plan_failure") or {"reason": "PLAN_DID_NOT_COMPLETE"}
                if self.budget.usage.task_replans >= self.budget.limits.max_task_replans:
                    await self.runtime.stop_task("MAX_TASK_REPLANS_REACHED")
                    return "STOPPED: MAX_TASK_REPLANS_REACHED"
            await self.runtime.stop_task("MAX_TASK_REPLANS_REACHED")
            return "STOPPED: MAX_TASK_REPLANS_REACHED"
        except BudgetExceededError as error:
            await self.runtime.stop_task(error.reason)
            return f"STOPPED: {error.reason}"
        finally:
            self.agent.default_options["tools"] = original_tools

    async def _prepare_task_prompt(self, prompt: str) -> str:
        """Move known initial navigation inspection and account setup out of model turns."""
        if self.runtime.browser_session_id is None:
            return prompt
        context = self.agent.additional_properties.get("task_context", {})
        page = self.runtime.browser_manager.get_session(self.runtime.browser_session_id).page
        state = await self.runtime.page_state_reader.read(page)
        username_reference = f"{context.get('identity_reference', '')}_username"
        secret_reference = context.get("secret_reference")
        preparation = "LOGIN_NOT_REQUESTED"
        if "register" not in context.get("required_operations", []) and username_reference in self.runtime.input_values and secret_reference in self.runtime.input_values:
            usernames = [element for element in state.interactive_elements if element.kind == "input" and "login" in element.label.casefold() and "username" in element.label.casefold()]
            passwords = [element for element in state.interactive_elements if element.kind == "input" and "login" in element.label.casefold() and "password" in element.label.casefold()]
            buttons = [element for element in state.interactive_elements if element.kind == "button" and element.label.casefold() == "login"]
            if len(usernames) == len(passwords) == len(buttons) == 1:
                actions = [WebAction(action_type=ActionType.INPUT, target=usernames[0].target, value_reference=username_reference, value=self.runtime.input_values[username_reference]), WebAction(action_type=ActionType.INPUT, target=passwords[0].target, value_reference=secret_reference, value=self.runtime.input_values[secret_reference]), WebAction(action_type=ActionType.CLICK, target=buttons[0].target)]
                preparation = "LOGIN_SUBMITTED"
                for action in actions:
                    result = await self.runtime.execute_known_action(action)
                    if not result.success:
                        preparation = result.error_type or "LOGIN_SETUP_FAILED"
                        break
        if self.runtime.browser_session_id is None:
            return prompt
        try:
            await page.wait_for_load_state("networkidle", timeout=5_000)
        except PlaywrightTimeoutError:
            preparation = "PAGE_NOT_SETTLED"
        inspection = await self.runtime.execute_known_action(WebAction(action_type=ActionType.DOM_INSPECTION, target="body"))
        self.runtime.remember_assertion_targets(inspection.data.get("rows", []))
        if preparation == "LOGIN_SUBMITTED":
            logged_in = self.runtime.input_values[username_reference] in str(inspection.data.get("text", "")) and any(element["label"].casefold() == "logout" for element in inspection.data.get("interactive_elements", []))
            preparation = "AUTHENTICATED" if logged_in else "LOGIN_NOT_ESTABLISHED"
        self.store.append_event(event_id=f"event-{uuid4().hex}", run_id=self.assignment.run_id, task_id=self.assignment.task_id, tester_id=self.assignment.tester_id, event_type="TESTER_PREPARATION", tool="Playwright", action="prepare_task_page", result={"status": preparation, "inspection_success": inspection.success}, latency_ms=0)
        initial = {"preparation": preparation, "page": inspection.data}
        return prompt + "\nInitial Page (navigation and this inspection are already complete): " + json.dumps(initial, ensure_ascii=False)
