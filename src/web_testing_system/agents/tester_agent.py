"""The single Tester Agent definition and its constrained tools."""

from __future__ import annotations

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
    InteractiveElement,
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
        self.readonly_tail = False
        self.checking_readonly_tail = False
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
        goal["destination"] = {"anyOf": [{"type": "string", "minLength": 1, "pattern": "\\S"}, {"type": "null"}], "description": "REQUIRED field on every goal: a human navigation destination for navigate (e.g. Tasks, Members, Projects), otherwise null. Never infer a destination from goal prose."}
        definitions["PageGoal"]["required"].append("destination")
        definitions["PageGoal"]["allOf"] = [{"if": {"properties": {"operation": {"const": "navigate"}}, "required": ["operation"]}, "then": {"properties": {"destination": {"type": "string", "minLength": 1, "pattern": "\\S"}}}}]
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
        completed = self.task_context.get("completed_check_ids", [])
        remaining = [check["check_id"] for check in checks if check["check_id"] not in completed]
        coverage: list[dict[str, Any]] = []
        for check_id in remaining:
            locations: list[dict[str, Any]] = []
            for location in ("before_steps", "checks", "after_steps"):
                locations.append({"required": [location], "properties": {location: {"contains": {"required": ["check_id"], "properties": {"check_id": {"const": check_id}}}}}})
            coverage.append({"contains": {"anyOf": locations}})
        for check_id in sorted(self._permission_delete_checks() - self._attempted_permission_checks() - set(completed)) if not self.plan_started else []:
            locations = [{"required": [location], "properties": {location: {"contains": {"required": ["check_id"], "properties": {"check_id": {"const": check_id}}}}}} for location in ("checks", "after_steps")]
            coverage.append({"contains": {"required": ["operation"], "properties": {"operation": {"const": "delete"}, "run_operations": {"const": True}}, "anyOf": locations}})
        if coverage:
            schema["properties"]["goals"]["allOf"] = coverage
            schema["properties"]["goals"]["description"] = f"Complete remaining plan. Must contain evidence for EVERY ID: {', '.join(remaining)}. No browser action runs until the entire plan passes preflight."
        for definition in (assertion, boundary):
            if definition is assertion:
                definition["required"] = list(dict.fromkeys([*definition["required"], "target", "control", "assertion", "expected", "expected_reference"]))
            nonempty = {"type": "string", "minLength": 1, "pattern": "\\S"}
            target_required = {"anyOf": [{"required": [name], "properties": {name: nonempty}} for name in ("target", "control")]}
            expected_required = {"anyOf": [{"required": ["expected"], "properties": {"expected": {"type": "string"}}}, {"required": ["expected_reference"], "properties": {"expected_reference": {"type": "string"}}}]}
            definition["allOf"] = [
                {"if": {"properties": {"action_type": {"const": "assertion"}}, "required": ["action_type"]}, "then": target_required},
                {"if": {"anyOf": [{"properties": {"action_type": {"const": "url_check"}}, "required": ["action_type"]}, {"properties": {"action_type": {"const": "assertion"}, "assertion": {"enum": ["contains", "not_contains", "equals", "count"]}}, "required": ["action_type"]}]}, "then": expected_required},
                {"if": {"properties": {"action_type": {"const": "navigation"}}, "required": ["action_type"]}, "then": {"anyOf": [{"required": [name], "properties": {name: nonempty}} for name in ("url", "control")]}}
            ]
            definition["properties"]["wait_ms"] = {"type": "integer", "minimum": 0, "maximum": 2000, "default": 0}
            definition["properties"]["target"]["description"] = "Required target OR control for assertions. Use an observed stable locator. Never omit both."
            definition["properties"]["expected"]["description"] = "Required expected OR expected_reference for text/count/URL assertions. For visible/hidden omit both. Select text means the SELECTED option label, never all options."
            definition["properties"]["context"] = {"type": "string", "enum": scopes["object_contexts"], "default": "", "description": "Exact entity reference/value or observed row selector for a scoped table assertion. Never a form/page description."}
            for key, allowed in (("value_reference", references), ("expected_reference", public), ("project_reference", objects)):
                definition["properties"][key] = {"enum": [None, *allowed], "default": None}
            if checks:
                definition["properties"]["check_id"] = {"enum": [None, *[check["check_id"] for check in checks]], "description": "Use each assigned ID exactly once on its actual assertion or repeat_submit evidence, never on navigation/refresh/wait.", "default": None}
                definition["properties"]["behavior_id"] = {"enum": [None, *sorted({check["behavior_id"] for check in checks})], "default": None}
        definitions.pop("PageStep", None)
        boundary["properties"]["assertion"] = {"enum": [None, "contains", "not_contains", "equals", "visible", "hidden", "count"], "default": "contains", "description": "Only used by assertion/url_check. Omit for navigation/refresh/wait/repeat_submit; null on those actions is ignored."}
        boundary["allOf"].append({"if": {"properties": {"action_type": {"enum": ["assertion", "url_check"]}}, "required": ["action_type"]}, "then": {"required": ["assertion"], "properties": {"assertion": assertion["properties"]["assertion"]}}})
        definitions.pop("ActionType", None)
        # Dictionary schemas advertise constraints; the existing preflight remains authoritative.
        return FunctionTool(name=tool.name, description=tool.description, func=self.execute_test_plan, input_model=schema)

    def _permission_delete_checks(self) -> set[str]:
        if self.assignment.role.casefold() == "admin":
            return set()
        behaviors = {behavior["behavior_id"] for behavior in self.task_context.get("expected_behaviors", []) if behavior.get("applies_to", "").casefold() == f"permission/{self.assignment.role.casefold()}/delete"}
        checks = set()
        for check in self.runtime.required_checks:
            words = {word.strip(".,;:()") for word in check.get("description", "").casefold().replace("-", " ").split()}
            if check["behavior_id"] in behaviors and "delete" in words and words & {"reject", "rejected", "rejection", "deny", "denied", "denial", "unavailable"}:
                checks.add(check["check_id"])
        return checks

    def _attempted_permission_checks(self) -> set[str]:
        return {check_id for event in self.store.list_events(self.assignment.run_id, task_id=self.assignment.task_id, event_types=("PLAN_GOAL_PREPARED",)) if event["result"].get("operation_binding", {}).get("operation") == "delete" for check_id in event["result"].get("check_ids", [])}

    def _bind_permission_result_assertions(self, goal: PageGoal) -> None:
        checks = self._permission_delete_checks()
        prepared = self.store.list_events(self.assignment.run_id, task_id=self.assignment.task_id, event_types=("PLAN_GOAL_PREPARED",))
        for location in ("checks", "after_steps"):
            steps = getattr(goal, location)
            for index, step in enumerate(steps):
                if step.check_id not in checks or step.action_type != ActionType.ASSERTION:
                    continue
                attempt = next((event["result"] for event in reversed(prepared) if step.check_id in event["result"].get("check_ids", []) and event["result"].get("operation_binding", {}).get("operation") == "delete"), None)
                if attempt is None or not attempt.get("forbidden_delete_object"):
                    continue
                operation = attempt["operation_binding"]
                reference = operation.get("row_reference") or operation.get("context")
                project_reference = operation.get("project_reference")
                target = attempt["forbidden_delete_object"]["target"]
                # 明确禁止删除的已执行操作检查原对象保留，按钮消失不能证明操作被拒绝。
                steps[index] = replace(step, target=target, control=None, context="", project_reference=project_reference, assertion="count", expected="1", expected_reference=None)
                self.store.append_event(event_id=f"event-{uuid4().hex}", run_id=self.assignment.run_id, task_id=self.assignment.task_id, tester_id=self.assignment.tester_id, event_type="PERMISSION_ASSERTION_BOUND", tool="TesterPlan", action="preservation", result={"check_id": step.check_id, "target": target, "row_reference": reference, "project_reference": project_reference}, latency_ms=0)

    async def planning_context(self, latest_failure: dict[str, Any] | None = None) -> dict[str, Any]:
        outcome = await self.runtime.record_task_outcome()
        outcome_checks = outcome["checks"]
        assert isinstance(outcome_checks, list)
        completed = sorted(check["check_id"] for check in outcome_checks if check["completed"])
        self.task_context["completed_check_ids"] = completed
        assigned = [dict(check) for check in self.runtime.required_checks]
        references = self.allowed_input_references()
        checks = [{**check, "completed": check["check_id"] in completed, "place": "before_steps or after_steps" if check.get("action_type") == "repeat_submit" else "checks (or an assertion after its refresh boundary)", "allowed_actions": ["repeat_submit"] if check.get("action_type") == "repeat_submit" else ["assertion", "url_check"]} for check in assigned]
        page = await self._page_summary() if self.runtime.browser_session_id else {}
        preparation = self.store.list_events(self.assignment.run_id, task_id=self.assignment.task_id, event_types=("TESTER_PREPARATION",))
        context = {"task_id": self.assignment.task_id, "goal": self.task_context.get("goal", ""), "identity_reference": self.task_context.get("identity_reference"), "role": self.assignment.role, "scope": list(self.assignment.scope), "target_url": self.task_context.get("target_url"), "denied_operations": self.task_context.get("denied_operations", []), "required_operations": self.task_context.get("required_operations", []), "assigned_checks": checks, "allowed_check_ids": [check["check_id"] for check in assigned], "completed_check_ids": completed, "remaining_check_ids": [check["check_id"] for check in assigned if check["check_id"] not in completed], "prepared_check_ids": sorted(self.runtime.prepared_check_ids), "allowed_input_references": sorted(references), "test_inputs": {reference: self.runtime.input_values[reference] for reference in sorted(references) if not reference.startswith("env:")}, "secret_references": [reference for reference in sorted(references) if reference.startswith("env:")], "assigned_behavior_ids": list(self.runtime.expected_behavior_ids), "preparation": preparation[-1]["result"] if preparation else {}, "current_page": page, "latest_failure": latest_failure, "plan_rules": {"checks_actions": ["assertion", "url_check"], "boundary_actions": ["navigation", "refresh", "wait", "repeat_submit", "assertion", "url_check"], "operation_actions": "Inputs belong in PageGoal.inputs; declare one operation and its constraints for Runtime. Jev only selects current candidates. No click/input/select in boundary steps.", "check_ids": "Exactly one occurrence per remaining Check ID, paired with its assigned behavior_id. Incidental setup boundaries have neither ID. A refresh then assertion uses the ID only on the assertion.", "ordering": "Honor depends_on before the dependent check. Preserve necessary refresh/reopen/logout/login/pending-submit boundaries.", "repeat_submit": "Use run_operations=true with inputs; Runtime stops after filling, then after_steps.repeat_submit supplies the sole pending-operation check ID. Subsequent DOM assertions have their own IDs.", "references": "Only the supplied exact reference keys. Blank input values are legal. Secret references may fill fields but cannot be assertion values.", "objects": "project_reference selects a Project; row_reference identifies an existing row; form_context scopes a form. Do not use prose in object reference fields."}}
        prepared = self.store.list_events(self.assignment.run_id, task_id=self.assignment.task_id, event_types=("PLAN_GOAL_PREPARED",))
        context["prepared_operations"] = [event["result"] for event in prepared if event["result"].get("operation_binding") and set(event["result"].get("check_ids", [])) & set(context["remaining_check_ids"])]
        context["binding_scopes"] = self.binding_scopes(page)
        context["current_object"] = page.get("current_object", {})
        context["object_bindings"] = page.get("object_bindings", [])
        context["pending_progress_signals"] = self._pending_progress_signals()
        context["plan_rules"]["completeness"] = "All remaining_check_ids must have actual evidence in this replacement, including downstream checks unrelated to the latest blocker. Required local dependencies must precede their checks. Navigation must have a nonblank destination; never rely on goal prose."
        context["plan_rules"]["object_availability"] = "Use current observed object_bindings, not cached intent. unavailable means the object cannot be selected/mutated now; not_observed is not proof of deletion. Navigate without binding to an unavailable object to reobserve the view. Check-only assertions may still record a deviation against the original missing object; never substitute a different object or invent successful evidence."
        context["plan_rules"]["operations"] = "Every goal declares operation: navigate/create/edit/delete/submit/login/logout/observe. Navigate uses destination=<human navigation label>; create/submit binds supplied inputs; edit/delete uses a stable row_reference. Reopen a view with a navigate goal, then observe/check; do not hide navigation, mutations or logout inside a compound operation. Runtime phases these operations; Jev only selects legal current candidates. Preserve all explicit Check boundaries and existing budgets."
        context["plan_rules"]["contexts"] = "Context fields are binding keys, NEVER explanatory prose. For create/register/login set goal.context='', omit row_reference/project_reference, and normally omit form_context and input.context. Put all operation instructions in goal. For editing/deleting existing objects use row_reference; project_reference selects the containing Project only. Table assertions use the exact entity key/value, not a page or table description."
        context["expected_behaviors"] = self.task_context.get("expected_behaviors", [])
        context["plan_rules"]["permission_evidence"] = "A visible enabled button is NOT evidence that its operation is permitted. If a forbidden control is absent, assert that protection. If it is available, attempt the scoped operation and assert denial/result/object preservation. Never report a permission bug solely from button visibility."
        context["plan_rules"]["max_steps"] = "Each goal allows 1..30; default 12. The original Task/step/Replan budgets remain binding."
        return context

    async def execute_page_goals(self, goals: list[PageGoal], related_task_ids: list[str] | None = None, finish: bool = False) -> dict[str, Any]:
        """Attach the primary check ID to page decisions without uploading goal text."""
        goals = TypeAdapter(list[PageGoal]).validate_python(goals)
        check_id = next((step.check_id for goal in goals for step in [*goal.before_steps, *goal.checks, *goal.after_steps] if step.check_id), None)
        with trace_span("PageGoal", metadata={"task_id": self.assignment.task_id, "check_id": check_id}):
            return await self._execute_page_goals(goals, related_task_ids, finish)

    @staticmethod
    def _bind_assertion_objects(goal: PageGoal) -> None:
        for location in ("before_steps", "checks", "after_steps"):
            for step in getattr(goal, location):
                if step.action_type != ActionType.ASSERTION or not step.control or step.context or step.target:
                    continue
                step.context = goal.row_reference or goal.context
                if step.context or location == "before_steps" or not goal.run_operations or goal.operation not in {"create", "edit", "submit"}:
                    continue
                matches = {binding.value_reference for binding in goal.inputs if not binding.value_reference.startswith("env:") and WebTestingRuntime._field_matches(InteractiveElement(kind="cell", label=binding.control, target=""), step.control)}
                if len(matches) == 1:
                    step.context = next(iter(matches))

    async def _execute_page_goals(self, goals: list[PageGoal], related_task_ids: list[str] | None = None, finish: bool = False) -> dict[str, Any]:
        """Delegate explicit business operations to Runtime in one plan call. Runtime observes, binds, phases and recovers; Jev selects bounded alternatives. Python runs checks and determines Task success."""
        goals = TypeAdapter(list[PageGoal]).validate_python(goals)
        for goal in goals:
            self._bind_assertion_objects(goal)
        failure = self.runtime.validate_plan_assertions([asdict(goal) for goal in goals])
        if failure:
            return failure
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
            prepared = self._operation_prepared(goal)
            forbidden_delete_object = None
            if goal.operation == "delete" and not prepared and goal_checks & self._permission_delete_checks() and self.runtime.browser_session_id:
                page = self.runtime.browser_manager.get_session(self.runtime.browser_session_id).page
                rows = await self.runtime.page_state_reader.read_rows(page)
                matching = [row for row in rows if self.runtime._row_matches_context(row, goal.row_reference or goal.context) and row.get("identity", {}).get("data-project-id")]
                actor = self.runtime.input_values.get(self.task_context.get("username_reference") or f"{self.task_context.get('identity_reference', self.assignment.role)}_username")
                if len(matching) == 1 and actor:
                    owner = next((cell["text"] for cell in matching[0]["cells"] if cell["header"].casefold() == "owner"), None)
                    if owner == actor:
                        return {"success": False, "failed_goal": index, "reason": "PERMISSION_SUBJECT_IS_OWNER", "check_id": next(iter(goal_checks & self._permission_delete_checks())), "instruction": "This assigned non-owner rejection check cannot target the actor's owned Project. Use the supplied non-owner object; preserve completed owner cleanup."}
                    if owner and owner != actor:
                        forbidden_delete_object = {"target": matching[0]["target"], "identity": matching[0]["identity"]}
            operations = await self.runtime.execute_page_goal(goal.goal, inputs=[asdict(binding) for binding in goal.inputs], context=goal.context, project_reference=goal.project_reference, row_reference=goal.row_reference, form_context=goal.form_context, max_steps=goal.max_steps, stop_after_inputs=any(step.action_type == ActionType.REPEAT_SUBMIT for step in goal.after_steps), operation=goal.operation, destination=goal.destination) if goal.run_operations and not prepared else {"success": True, "reason": "CHECK_ONLY", "actions": []}
            result: dict[str, Any] = {"goal": index, "operations": operations}
            results.append(result)
            if not operations["success"] and operations.get("reason") == "OBJECT_UNAVAILABLE_IN_CURRENT_VIEW" and goal.operation in {"observe", "navigate"} and not goal.inputs and goal.checks:
                targets = []
                for check in goal.checks:
                    action = WebAction(check.action_type, target=check.target, assertion=check.assertion, expected=self.runtime.input_values.get(check.expected_reference) if check.expected_reference else check.expected, project_reference=check.project_reference, check_id=check.check_id, behavior_id=check.behavior_id)
                    targets.append(await self.runtime.original_absence_target(action, control=check.control, context=check.context, allow_parent=True))
                if all(targets):
                    operations.update(success=True, reason="ORIGINAL_OBJECT_ABSENCE_CHECK")
                    self.runtime._record_goal_recovery("OBJECT_UNAVAILABLE_IN_CURRENT_VIEW", "ORIGINAL_OBJECT_ABSENCE_CHECK", True)
            if not operations["success"]:
                return {"success": False, "failed_goal": index, "results": results, "page": await self._page_summary()}
            observed = operations.get("object_before")
            if goal_checks & self._permission_delete_checks() and observed and observed.get("owner_is_actor") is False:
                forbidden_delete_object = {"target": observed["target"], "identity": observed["identity"]}
            project_reference = goal.project_reference or (self.runtime.active_project["reference"] if self.runtime.active_project else None)
            for step in [*goal.checks, *goal.after_steps]:
                if step.project_reference is None:
                    step.project_reference = project_reference
            if goal_checks and goal.run_operations and not prepared:
                self.runtime.prepared_check_ids.update(goal_checks)
                self.store.append_event(event_id=f"event-{uuid4().hex}", run_id=self.assignment.run_id, task_id=self.assignment.task_id, tester_id=self.assignment.tester_id, event_type="PLAN_GOAL_PREPARED", tool="TesterPlan", action="prepare_checks", result={"check_ids": sorted(goal_checks), "operation_binding": self._operation_binding(goal), "forbidden_delete_object": forbidden_delete_object}, latency_ms=0)
            self._bind_permission_result_assertions(goal)
            safe_tail = self.readonly_tail and not any(step.action_type == ActionType.REPEAT_SUBMIT for step in goal.after_steps)
            if goal.checks:
                self.checking_readonly_tail = safe_tail
                try:
                    checks = await self.execute_page_steps(goal.checks, related_task_ids=related_task_ids)
                finally:
                    self.checking_readonly_tail = False
                result["checks"] = checks["results"]
                if not checks["success"]:
                    return {"success": False, "failed_goal": index, "reason": checks.get("reason") or checks.get("error_type"), "failed_check": checks.get("failed_check"), "auxiliary_assertion_failure": checks.get("auxiliary_assertion_failure", False), "results": results, "page": checks.get("page", {})}
            if goal.after_steps:
                self.checking_readonly_tail = safe_tail
                try:
                    after = await self.execute_page_steps(goal.after_steps, related_task_ids=related_task_ids)
                finally:
                    self.checking_readonly_tail = False
                result["after_steps"] = after["results"]
                if not after["success"]:
                    return {"success": False, "failed_goal": index, "reason": after.get("reason") or after.get("error_type"), "failed_check": after.get("failed_check"), "auxiliary_assertion_failure": after.get("auxiliary_assertion_failure", False), "results": results, "page": after.get("page", {})}
            if goal.publish_progress:
                published = self.store.list_events(self.assignment.run_id, task_id=self.assignment.task_id, event_types=("TASK_PROGRESS",))
                if not any(event["result"].get("progressed") is True and event["result"].get("summary") == goal.publish_progress for event in published):
                    await self.update_task_progress(True, goal.publish_progress)
        if finish:
            return {"success": True, "results": results, "outcome": await self.finish_task()}
        return {"success": True, "results": results, "page": await self._page_summary()}

    @staticmethod
    def _order_pending_submission_checks(goal: PageGoal, check_specs: dict[str, Any]) -> None:
        submissions = [(index, step) for index, step in enumerate(goal.after_steps) if step.action_type == ActionType.REPEAT_SUBMIT]
        if len(submissions) != 1 or not goal.checks:
            return
        index, submission = submissions[0]
        if submission.check_id not in check_specs:
            return
        for check in goal.checks:
            if check.check_id not in check_specs:
                return
            dependencies = list(check_specs[check.check_id].get("depends_on", []))
            visited: set[str] = set()
            while dependencies:
                dependency = dependencies.pop()
                if dependency in visited:
                    continue
                visited.add(dependency)
                dependencies.extend(check_specs.get(dependency, {}).get("depends_on", []))
            if submission.check_id not in visited:
                return
        boundary = index + 1
        while boundary < len(goal.after_steps) and goal.after_steps[boundary].action_type == ActionType.WAIT:
            boundary += 1
        # 显式依赖提交的结果检查应在提交及其等待之后、刷新边界之前执行。
        goal.after_steps = [*goal.after_steps[:boundary], *goal.checks, *goal.after_steps[boundary:]]
        goal.checks = []

    @staticmethod
    def _operation_binding(goal: PageGoal) -> dict[str, Any]:
        return {"operation": goal.operation, "destination": goal.destination, "context": goal.context, "project_reference": goal.project_reference, "row_reference": goal.row_reference, "form_context": goal.form_context, "inputs": [asdict(binding) for binding in goal.inputs], "pending_submission": any(step.action_type == ActionType.REPEAT_SUBMIT for step in goal.after_steps)}

    def _operation_prepared(self, goal: PageGoal) -> bool:
        if goal.operation not in {"create", "edit", "delete", "submit"}:
            return False
        checks = {step.check_id for step in [*goal.before_steps, *goal.checks, *goal.after_steps] if step.check_id}
        binding = self._operation_binding(goal)
        events = self.store.list_events(self.assignment.run_id, task_id=self.assignment.task_id, event_types=("PLAN_GOAL_PREPARED",))
        return bool(checks) and any(checks <= set(event["result"].get("check_ids", [])) and event["result"].get("operation_binding") == binding for event in events)

    def _pending_progress_signals(self) -> list[str]:
        plans = self.store.list_events(self.assignment.run_id, task_id=self.assignment.task_id, event_types=("RUNTIME_SIGNAL_PLAN",))
        progress = self.store.list_events(self.assignment.run_id, task_id=self.assignment.task_id, event_types=("TASK_PROGRESS",))
        published = {event["result"].get("summary") for event in progress if event["result"].get("progressed") is True}
        return sorted({signal["name"] for event in plans for signal in event["result"].get("signals", [])} - published)

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
            failure = {key: result[key] for key in ("reason", "failed_goal", "unavailable_references", "unavailable_object_references", "missing_check_ids", "missing_behavior_ids", "missing_progress_signals", "check_id", "unmet_dependencies", "validation_errors", "binding_field", "allowed_contexts", "instruction") if key in result} if not result["success"] else None
            if failure is not None:
                execution = result.get("failed_execution", {})
                if execution.get("failed_check"):
                    failure["failed_check"] = execution["failed_check"]
                operations = (execution.get("results") or [{}])[-1].get("operations", {})
                failure["pending_inputs"] = operations.get("pending_inputs", [])
                last_action = (operations.get("actions") or [{}])[-1]
                failure["last_action"] = {key: last_action[key] for key in ("action", "control", "value_reference") if key in last_action} or None
                if self.store is not None:
                    self.store.append_event(event_id=f"event-{uuid4().hex}", run_id=self.assignment.run_id, task_id=self.assignment.task_id, tester_id=self.assignment.tester_id, event_type="TESTER_PLAN_FAILED", tool="TesterPlan", action=phase, result=failure, latency_ms=0)
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
        for goal in goals:
            if isinstance(goal, dict):
                for step in goal.get("checks", []):
                    if "action_type" not in step and (step.get("target") or step.get("control")) and step.get("assertion") in {"contains", "not_contains", "equals", "visible", "hidden", "count"}:
                        step["action_type"] = "assertion"
                for step in [*goal.get("before_steps", []), *goal.get("checks", []), *goal.get("after_steps", [])]:
                    if step.get("action_type") not in {"assertion", "url_check"} and step.get("assertion") is None:
                        step.pop("assertion", None)
        goals = TypeAdapter(list[PageGoal]).validate_python(goals)
        for goal in goals:
            self._bind_assertion_objects(goal)
            if goal.operation != "navigate" and goal.destination and (not goal.before_steps or goal.before_steps[-1].action_type != ActionType.NAVIGATION or goal.before_steps[-1].control != goal.destination):
                goal.before_steps.append(PageStep(ActionType.NAVIGATION, control=goal.destination, project_reference=goal.project_reference))
        self.store.append_event(event_id=f"event-{uuid4().hex}", run_id=self.assignment.run_id, task_id=self.assignment.task_id, tester_id=self.assignment.tester_id, event_type="TESTER_PLAN", tool="TesterPlan", action=phase, result={"phase": phase, "goal_count": len(goals), "goals": [{"goal": goal.goal, "inputs": [asdict(binding) for binding in goal.inputs], "context": goal.context, "project_reference": goal.project_reference, "row_reference": goal.row_reference, "form_context": goal.form_context, "operation": goal.operation, "destination": goal.destination, "run_operations": goal.run_operations, "check_ids": [step.check_id for step in [*goal.before_steps, *goal.checks, *goal.after_steps] if step.check_id]} for goal in goals]}, latency_ms=0)
        current_outcome = await self.runtime.record_task_outcome()
        current_assertions = current_outcome["assertions"]
        assert isinstance(current_assertions, list)
        recorded = {check.get("behavior_id") for check in current_assertions}
        planned: set[str] = set()
        planned_checks: set[str] = set()
        check_specs = {check["check_id"]: check for check in self.runtime.required_checks}
        boundaries = {ActionType.NAVIGATION, ActionType.REFRESH, ActionType.REPEAT_SUBMIT, ActionType.WAIT, ActionType.ASSERTION, ActionType.URL_CHECK}
        page = await self._page_summary()
        scopes = self.binding_scopes(page)
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
            if goal.operation == "navigate" and not (goal.destination and goal.destination.strip()):
                return {"success": False, "reason": "NAVIGATION_DESTINATION_REQUIRED"}
            if any(step.action_type == ActionType.NAVIGATION and not (step.url and step.url.strip() or step.control and step.control.strip()) for step in [*goal.before_steps, *goal.after_steps]):
                return {"success": False, "reason": "NAVIGATION_DESTINATION_REQUIRED", "instruction": "Each navigation boundary needs an explicit URL or observed control; goal text is not a destination."}
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
                    if step.check_id not in check_specs or step.behavior_id is not None and step.behavior_id != check_specs[step.check_id]["behavior_id"]:
                        return {"success": False, "reason": "UNKNOWN_OR_INVALID_CHECK_ID", "check_id": step.check_id}
                    step.behavior_id = check_specs[step.check_id]["behavior_id"]
                    expected_action = check_specs[step.check_id].get("action_type", "assertion")
                    allowed_actions = {"assertion", "url_check"} if expected_action == "assertion" else {expected_action}
                    if step.action_type.value not in allowed_actions:
                        return {"success": False, "reason": "UNKNOWN_OR_INVALID_CHECK_ID"}
                    if step.check_id in planned_checks:
                        return {"success": False, "reason": "DUPLICATE_CHECK_ID"}
                    planned_checks.add(step.check_id)
                if check_specs and step.action_type == ActionType.REPEAT_SUBMIT and step.check_id is None:
                    return {"success": False, "reason": "CHECK_ID_REQUIRED", "instruction": "A pending repeated submission must carry its assigned repeat_submit Check ID."}
                if step.behavior_id:
                    if step.behavior_id not in self.runtime.expected_behavior_ids or not check_specs and step.action_type not in {ActionType.ASSERTION, ActionType.URL_CHECK, ActionType.REPEAT_SUBMIT}:
                        return {"success": False, "reason": "UNKNOWN_OR_INVALID_BEHAVIOR_CHECK", "invalid_behavior_id": step.behavior_id, "assigned_behavior_ids": list(self.runtime.expected_behavior_ids), "instruction": "Use only the actual assigned behavior IDs for goal checks. Do not invent IDs for setup checks."}
                    planned.add(step.behavior_id)
        missing = sorted(set(self.runtime.expected_behavior_ids) - recorded - planned)
        scored_checks = current_outcome["checks"]
        assert isinstance(scored_checks, list)
        recorded_checks = {check["check_id"] for check in scored_checks if check["completed"]}
        for check_id in sorted(self._permission_delete_checks() - self._attempted_permission_checks() - recorded_checks) if phase == "initial_plan" else []:
            if not any(goal.operation == "delete" and goal.run_operations and any(step.check_id == check_id for step in [*goal.checks, *goal.after_steps]) for goal in goals):
                return {"success": False, "reason": "PERMISSION_OPERATION_PLAN_REQUIRED", "check_id": check_id, "instruction": "This explicit forbidden Delete check needs a scoped delete operation, not button visibility. Runtime checks the same original object's preservation after an attempt; an unavailable forbidden control can record protection without fabricating a click."}
        missing_checks = sorted(set(check_specs) - recorded_checks - planned_checks)
        if missing_checks:
            if any(step.check_id is None and (step.behavior_id or phase == "exception_replan") for goal in goals for step in goal.checks):
                return {"success": False, "reason": "CHECK_ID_REQUIRED", "missing_check_ids": missing_checks, "remaining_check_ids": sorted(set(check_specs) - recorded_checks), "instruction": "Attach the exact missing required IDs to their actual checks; preserve every remaining Check."}
            return {"success": False, "reason": "INCOMPLETE_TEST_PLAN", "missing_check_ids": missing_checks}
        if check_specs:
            # 完整性先按真实 Check ID 校验，辅助断言不能冒充或阻塞必测检查的 ID。
            for goal in goals:
                for step in [*goal.before_steps, *goal.checks, *goal.after_steps]:
                    if step.behavior_id and step.check_id is None:
                        self.store.append_event(event_id=f"event-{uuid4().hex}", run_id=self.assignment.run_id, task_id=self.assignment.task_id, tester_id=self.assignment.tester_id, event_type="PLAN_AUXILIARY_ASSERTION", tool="TesterPlan", action="unscored_assertion", result={"behavior_id": step.behavior_id, "target": step.target, "control": step.control, "context": step.context}, latency_ms=0)
                        step.behavior_id = None
        missing_signals = sorted(set(self._pending_progress_signals()) - {goal.publish_progress for goal in goals})
        if missing_signals:
            return {"success": False, "reason": "INCOMPLETE_PROGRESS_PLAN", "missing_progress_signals": missing_signals, "instruction": "Preserve unpublished live-session continuations from the accepted plan, including those whose checks already completed."}
        if not goals and current_outcome["ready_to_finish"]:
            return {"success": True, "outcome": await self.finish_task(), "completed_check_ids": sorted(recorded_checks)}
        if not goals or missing:
            return {"success": False, "reason": "INCOMPLETE_TEST_PLAN", "missing_behavior_ids": missing, "instruction": "Submit every remaining required operation and actual assertion before execution."}
        seen_checks = set(recorded_checks)
        for goal in goals:
            self._order_pending_submission_checks(goal, check_specs)
            for step in [*goal.before_steps, *goal.checks, *goal.after_steps]:
                if step.check_id in check_specs and step.check_id not in recorded_checks:
                    unmet = sorted(set(check_specs[step.check_id].get("depends_on", [])) & set(check_specs) - seen_checks)
                    if unmet:
                        return {"success": False, "reason": "CHECK_DEPENDENCY_ORDER_REQUIRED", "check_id": step.check_id, "unmet_dependencies": unmet}
                    seen_checks.add(step.check_id)
        failure = self.runtime.validate_plan_assertions([asdict(goal) for goal in goals])
        if failure:
            return failure
        self.runtime.register_progress_plan([asdict(goal) for goal in goals])
        self.executing_plan = True
        self.store.append_event(event_id=f"event-{uuid4().hex}", run_id=self.assignment.run_id, task_id=self.assignment.task_id, tester_id=self.assignment.tester_id, event_type="TESTER_PLAN_VALIDATED", tool="TesterPlan", action=phase, result={"remaining_check_ids": sorted(set(check_specs) - recorded_checks), "planned_check_ids": sorted(planned_checks), "state_id": page.get("state_id")}, latency_ms=0)
        try:
            for index, goal in enumerate(goals):
                self.readonly_tail = all(item.operation in {"navigate", "observe"} and not item.inputs and all(step.action_type in {ActionType.NAVIGATION, ActionType.REFRESH, ActionType.WAIT, ActionType.ASSERTION, ActionType.URL_CHECK} for step in [*item.before_steps, *item.after_steps]) for item in goals[index + 1:])
                current_outcome = await self.runtime.record_task_outcome()
                outcome_checks = current_outcome["checks"]
                assert isinstance(outcome_checks, list)
                recorded_checks = {check["check_id"] for check in outcome_checks if check["completed"]}
                goal_steps = [*goal.before_steps, *goal.checks, *goal.after_steps]
                goal_checks = {step.check_id for step in goal_steps if step.check_id}
                if goal_checks and goal_checks <= recorded_checks:
                    # Check 完成不代表后续边界或 live-session 信号已完成。
                    continuation = replace(goal, run_operations=False, inputs=[], wait_for_progress=None, before_steps=[], checks=[], after_steps=[step for step in goal.after_steps if step.check_id not in recorded_checks])
                    result = await self.execute_page_goals([continuation], related_task_ids=related_task_ids)
                    if not result["success"]:
                        return {"success": False, "reason": result.get("reason", "PLAN_EXECUTION_BLOCKED"), "failed_goal": index, "failed_execution": result}
                    continue
                if not check_specs:
                    behaviors = {step.behavior_id for step in goal_steps if step.behavior_id}
                    recorded_behaviors = {check["behavior_id"] for check in outcome_checks if check["completed"]}
                    if behaviors and behaviors <= recorded_behaviors:
                        continue
                goal = replace(goal, before_steps=[step for step in goal.before_steps if step.check_id not in recorded_checks], checks=[step for step in goal.checks if step.check_id not in recorded_checks], after_steps=[step for step in goal.after_steps if step.check_id not in recorded_checks])
                unavailable_objects = self._unavailable_plan_objects(goal, await self._page_summary())
                if unavailable_objects:
                    return {"success": False, "reason": "OBJECT_REFERENCE_UNAVAILABLE", "failed_goal": index, "unavailable_object_references": unavailable_objects, "completed_check_ids": sorted(recorded_checks), "remaining_check_ids": sorted(set(check_specs) - recorded_checks)}
                result = await self.execute_page_goals([goal], related_task_ids=related_task_ids)
                operations = (result.get("results") or [{}])[-1].get("operations", {})
                if not result["success"]:
                    remaining = [goal, *goals[index + 1:]]
                    readonly = all(item.operation in {"navigate", "observe"} and not item.inputs and all(step.action_type in {ActionType.NAVIGATION, ActionType.REFRESH, ActionType.WAIT, ActionType.ASSERTION, ActionType.URL_CHECK} for step in [*item.before_steps, *item.after_steps]) for item in remaining)
                    if result.get("auxiliary_assertion_failure") and readonly and not goal_checks and not goal.publish_progress and not goal.after_steps:
                        self.store.append_event(event_id=f"event-{uuid4().hex}", run_id=self.assignment.run_id, task_id=self.assignment.task_id, tester_id=self.assignment.tester_id, event_type="READ_ONLY_ASSERTION_CONTINUATION", tool="TesterPlan", action="continue_observation", result={"failed_goal": index, "reason": result.get("reason"), "remaining_check_ids": sorted(set(check_specs) - recorded_checks)}, latency_ms=0)
                        continue
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
            self.readonly_tail = False
            self.checking_readonly_tail = False

    def _unavailable_plan_objects(self, goal: PageGoal, page: dict[str, Any]) -> list[str]:
        if any(step.action_type == ActionType.NAVIGATION for step in goal.before_steps):
            return []
        if not goal.run_operations or goal.operation in {"navigate", "observe", "create", "login", "logout"} or self._operation_prepared(goal):
            return []
        unavailable = {binding["reference"] for binding in page.get("object_bindings", []) if binding["status"] == "unavailable"}
        references = {goal.project_reference, goal.row_reference, goal.context} - {None, ""}
        return sorted(reference for reference in unavailable if reference in references or self.runtime.input_values.get(reference) in references)

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
            if await self._unsupported_permission_oracle(step):
                return {"success": False, "failed_step": index, "reason": "PERMISSION_OPERATION_EVIDENCE_REQUIRED", "check_id": step.check_id, "target": step.target, "control": step.control, "failed_check": {"check_id": step.check_id, "target": step.target, "control": step.control, "object": {"context": step.context, "project_reference": step.project_reference}, "assertion": step.assertion}, "instruction": "The available control does not prove authorization. Attempt the scoped forbidden operation and verify denial or object preservation; do not report button visibility as a bug.", "results": results, "page": await self._page_summary()}
            if step.control is not None and step.action_type == ActionType.NAVIGATION:
                navigation = await self.runtime.execute_page_goal("Navigate to the explicit boundary destination", operation="navigate", destination=step.control, project_reference=step.project_reference)
                result = {"success": navigation["success"], "error_type": None if navigation["success"] else navigation.get("reason"), "source": "RUNTIME", "resolved_target": None}
            elif step.control is not None and (step.target is None or step.action_type != ActionType.ASSERTION):
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
            self.store.append_event(event_id=f"event-{uuid4().hex}", run_id=self.assignment.run_id, task_id=self.assignment.task_id, tester_id=self.assignment.tester_id, event_type="PAGE_STEP", tool="TesterPageSteps", action=step.action_type.value, result={"source": result["source"], "semantic_control": step.control is not None, "success": result["success"], "behavior_id": step.behavior_id, "check_id": step.check_id, "target": result.get("resolved_target"), "control": step.control, "object": {"context": step.context, "project_reference": step.project_reference}, "failure_reason": result.get("error_type"), "browser_event_id": result.get("event_id")}, latency_ms=0)
            compact = {"step": index, "action": step.action_type.value, "success": result["success"], "source": result["source"], "check_id": step.check_id, "control": step.control, "target": result.get("resolved_target"), "object": {"context": step.context, "project_reference": step.project_reference}}
            if step.behavior_id:
                compact["behavior_id"] = step.behavior_id
            if not result["success"]:
                compact["error_type"] = result.get("error_type")
                compact["actual"] = result.get("data", {}).get("actual")
                if result.get("error_type") == "ASSERTION_FAILURE" and step.behavior_id:
                    session_id = self.runtime.browser_session_id
                    assert session_id is not None
                    page = self.runtime.browser_manager.get_session(session_id).page
                    observed_action: dict[str, Any] = next((item["action_data"] for item in self.store.list_action_history(run_id=self.assignment.run_id, task_id=self.assignment.task_id) if item["event_id"] == result.get("event_id")), {})
                    observed_assertion = observed_action.get("assertion", step.assertion)
                    expected_result = f"{observed_action.get('target') or result.get('resolved_target') or 'body'} {observed_assertion} {observed_action.get('expected', step.expected) or ''}"
                    finding = next((finding for finding in self.store.list_recent_findings(self.assignment.run_id, limit=1000) if finding["task_id"] == self.assignment.task_id and finding["action"] == step.behavior_id and finding["expected_result"] == expected_result), None)
                    if finding is None:
                        finding = await self.record_finding(title=f"{step.behavior_id}: {observed_assertion} check failed", status="ANOMALY", expected_result=expected_result, actual_result=json.dumps(compact["actual"], ensure_ascii=False), affected_page=page.url, action=step.behavior_id, behavior_id=step.behavior_id, check_id=step.check_id, related_task_ids=related_task_ids)
                    compact["finding_id"] = finding["finding_id"]
                    self.store.append_event(event_id=f"event-{uuid4().hex}", run_id=self.assignment.run_id, task_id=self.assignment.task_id, tester_id=self.assignment.tester_id, event_type="CHECK_FINDING_LINKED", tool="Python", action="link_check_evidence", result={"finding_id": finding["finding_id"], "check_id": step.check_id, "behavior_id": step.behavior_id, "boundary_event_id": result.get("event_id")}, latency_ms=0)
                else:
                    if result.get("error_type") == "ASSERTION_FAILURE" and step.check_id is None and step.behavior_id is None and self.checking_readonly_tail:
                        self.store.append_event(event_id=f"event-{uuid4().hex}", run_id=self.assignment.run_id, task_id=self.assignment.task_id, tester_id=self.assignment.tester_id, event_type="READ_ONLY_ASSERTION_CONTINUATION", tool="TesterPlan", action="continue_checks", result={"target": compact["target"], "control": step.control, "assertion": step.assertion, "browser_event_id": result.get("event_id")}, latency_ms=0)
                        results.append(compact)
                        continue
                    results.append(compact)
                    return {"success": False, "failed_step": index, "reason": result.get("error_type") or "PAGE_STEP_BLOCKED", "failed_check": {"check_id": step.check_id, "target": step.target, "control": step.control, "object": {"context": step.context, "project_reference": step.project_reference}, "assertion": step.assertion}, "auxiliary_assertion_failure": result.get("error_type") == "ASSERTION_FAILURE" and step.check_id is None and step.behavior_id is None, "results": results, "page": await self._page_summary()}
            results.append(compact)
        if finish:
            return {"success": True, "results": results, "outcome": await self.finish_task()}
        return {"success": True, "results": results, "page": await self._page_summary()}

    async def _unsupported_permission_oracle(self, step: PageStep) -> bool:
        if step.action_type == ActionType.ASSERTION and step.check_id in self._permission_delete_checks() and step.check_id not in self._attempted_permission_checks() and self.runtime.browser_session_id:
            page = self.runtime.browser_manager.get_session(self.runtime.browser_session_id).page
            state = await self.runtime.page_state_reader.read(page)
            rows = await self.runtime.page_state_reader.read_rows(page)
            self.runtime.remember_assertion_targets(rows, step.context)
            scoped = [row for row in rows if row.get("identity", {}).get("data-project-id") and (step.context and self.runtime._row_matches_context(row, step.context) or step.target and all(f"[{attribute}={json.dumps(value)}]" in step.target.replace("'", '"') for attribute, value in row["identity"].items()))]
            controls = [element for element in state.interactive_elements if element.enabled and element.label.casefold() in {"delete", "remove"} and any(element.context_target and str(row["target"]).endswith(element.context_target) for row in scoped)]
            if controls:
                return True
            absence = step.assertion == "hidden" or step.assertion == "count" and (step.expected == "0" or step.expected_reference and self.runtime.input_values.get(step.expected_reference) == "0")
            if absence and step.target:
                targets = [(target, container) for known in self.runtime.assertion_targets.values() for target, container in known.items() if '[data-project-id=' in target and '[data-project-id=' in step.target]
                known_scope = bool(scoped)
                for cached_target, container in targets:
                    identity = '[data-project-id=' + cached_target.split('[data-project-id=', 1)[1].split(']', 1)[0] + ']'
                    if container and identity in step.target.replace("'", '"') and await page.locator(container).is_visible():
                        known_scope = True
                        break
                original = page.locator(step.target)
                if known_scope and (await original.count() == 0 or step.assertion == "hidden" and not await original.first.is_visible()):
                    return False
            if absence and step.control and step.control.casefold() in {"delete", "remove"} and len(scoped) == 1:
                return False
            return True
        if step.action_type != ActionType.ASSERTION or step.assertion != "hidden" or not step.check_id or self.runtime.browser_session_id is None:
            return False
        behaviors = self.task_context.get("expected_behaviors", [])
        if not any(behavior["behavior_id"] == step.behavior_id and behavior.get("applies_to", "").casefold().startswith("permission/") for behavior in behaviors):
            return False
        page = self.runtime.browser_manager.get_session(self.runtime.browser_session_id).page
        selector = step.target
        if selector is None and step.control:
            state = await self.runtime.page_state_reader.read(page)
            rows = await self.runtime.page_state_reader.read_rows(page)
            matches = [element for element in state.interactive_elements if self.runtime._field_matches(element, step.control) and (not step.context or any(row["target"].endswith(element.context_target or "missing") and self.runtime._row_matches_context(row, step.context) for row in rows))]
            if len(matches) == 1:
                selector = matches[0].target
        if selector is None:
            return False
        target = page.locator(selector)
        if await target.count() != 1 or not await target.is_visible() or not await target.is_enabled():
            return False
        return bool(await target.evaluate("element => element.matches('button, a, input[type=submit], [role=button]')"))

    async def _page_summary(self) -> dict[str, Any]:
        if self.runtime.browser_session_id is None:
            return {}
        page = self.runtime.browser_manager.get_session(self.runtime.browser_session_id).page
        state = await self.runtime.page_state_reader.read(page)
        requests = [{key: record[key] for key in ("method", "url", "status") if key in record} for record in self.runtime.evidence_buffer.network_records[-12:]]
        rows = await self.runtime.page_state_reader.read_rows(page)
        summary = {**state.selection_summary(), "text": state.visible_dom[:3500], "rows": rows, "observed_requests": requests}
        current = dict(self.runtime.current_object)
        selected = current.get("selected_project") or self.runtime.active_project
        current["selected_project"] = selected
        project_selects = [element for element in state.interactive_elements if element.kind == "select" and "project" in element.label.casefold()]
        if selected and not any(element.target == selected["target"] and element.selected_value == selected["value"] and any(option == selected["value"] for _, option in element.options) for element in project_selects):
            current["selected_project"] = None
        if current.get("modal_row") and not any(element.context_kind == "dialog" for element in state.interactive_elements):
            current["modal_row"] = None
        current["row_targets"] = [target for target in current.get("row_targets", []) if await page.locator(target).is_visible()]
        bindings = []
        references = self.allowed_input_references()
        for (alias, container, project_value), target in self.runtime.object_targets.items():
            aliases = [reference for reference in references if not reference.startswith("env:") and alias == reference.casefold()]
            if not aliases:
                aliases = [reference for reference in references if not reference.startswith("env:") and alias == self.runtime.input_values[reference].casefold()]
                if len(aliases) != 1:
                    aliases = []
            if not aliases:
                continue
            status = "not_observed"
            selected_project = current.get("selected_project")
            same_project = project_value is None or selected_project is not None and project_value == selected_project["value"]
            if same_project and container and await page.locator(container).is_visible():
                status = "present" if await page.locator(target).is_visible() else "unavailable"
            for reference in aliases:
                bindings.append({"reference": reference, "kind": "row", "target": target, "status": status})
        project_references: set[str] = set()
        project_reference = current.get("project_reference")
        if isinstance(project_reference, str):
            project_references.add(project_reference)
        project_references.update(binding["reference"] for binding in self.runtime.object_projects.values())
        if selected:
            project_references.add(selected["reference"])
        if project_selects:
            for reference in sorted(project_references & references):
                present = any(self.runtime._project_options(element, reference) for element in project_selects)
                bindings.append({"reference": reference, "kind": "project", "status": "present" if present else "unavailable"})
        summary["current_object"] = current
        summary["object_bindings"] = bindings
        unavailable = {binding["reference"] for binding in bindings if binding["status"] == "unavailable"}
        for key in ("project_reference", "row_reference"):
            if current.get(key) in unavailable:
                current[key] = None
        return summary

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
        """Delegate the signal lifecycle and live continuation to Runtime."""
        if not self.shared_state:
            return {"ready": False, "reason": "SHARED_STATE_DISABLED"}
        return await self.runtime.wait_for_shared_progress(summary_contains, task_id=task_id, timeout_seconds=timeout_seconds)

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
