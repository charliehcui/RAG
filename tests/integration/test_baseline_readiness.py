from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from agent_framework import BaseChatClient, ChatResponse, Content, Message
from agent_framework._middleware import ChatMiddlewareLayer
from agent_framework._tools import FunctionInvocationLayer
from playwright.async_api import Route, async_playwright
from test_architecture_fixes import FakeJev, PlanningClient

from demo_app import DemoAppServer, SeededBugs
from web_testing_system.config import Settings
from web_testing_system.evaluation import (
    EvaluationControls,
    EvaluationMode,
    FormalRunExecutor,
    build_evaluation_plan,
    build_execution_route,
)
from web_testing_system.evaluation.matching import read_ground_truth
from web_testing_system.evaluation.runner import (
    EvaluationRunRecord,
    summarize_evaluation_runs,
)
from web_testing_system.evaluation.scenarios import load_run_config
from web_testing_system.evidence import EvidenceStore
from web_testing_system.reproduction.replay import (
    DeterministicReplay,
    ReplayPlan,
    ReplayStep,
)
from web_testing_system.runtime.browser import BrowserManager
from web_testing_system.runtime.budget import BudgetGuard, BudgetLimits
from web_testing_system.runtime.jev_selector import JevSelector
from web_testing_system.runtime.models import ActionType, WebAction
from web_testing_system.runtime.permissions import ExecutionPolicy, PermissionChecker
from web_testing_system.runtime.playwright_executor import PlaywrightExecutor
from web_testing_system.state import StateStore

ROOT = Path(__file__).resolve().parents[2]
SCENARIOS = ROOT / "evaluation/scenarios.json"
ANSWERS = ROOT / "evaluation/ground_truth.json"
BLOCKED_IDS = ["D08", "D09", "D12", "D13", "D15", "H04", "H06", "H08"]


def known(action_type: str, **arguments: Any) -> tuple[str, dict[str, Any]]:
    return "execute_known_action", {"action_type": action_type, **arguments}


def check(target: str, behavior_id: str, *, assertion: str = "visible", expected: str | None = None, related: list[str] | None = None) -> list[tuple[str, dict[str, Any]]]:
    return [known("assertion", target=target, assertion=assertion, expected=expected, behavior_id=behavior_id), ("report_if_failed", {"title": f"Observed {behavior_id} deviation", "status": "ANOMALY", "affected_page": "/", "action": "assertion", "behavior_id": behavior_id, "related_task_ids": related or []})]


def login(identity: str) -> list[tuple[str, dict[str, Any]]]:
    return [known("input", target="#login-username", value_reference=identity + "_username"), known("input", target="#login-password", value_reference=f"env:DEMO_{identity.upper()}_PASSWORD"), known("click", target="#login-form button[type='submit']"), known("wait", target="#app-section"), known("wait", wait_ms=50)]


def create_project(reference: str, name: str) -> list[tuple[str, dict[str, Any]]]:
    return [known("input", target="#project-name", value_reference=reference), known("click", target="#project-submit"), known("wait", target=f"#projects-table .project-name:text-is('{name}')")]


def repeat_project(identity: str, reference: str, name: str, base_url: str) -> list[tuple[str, dict[str, Any]]]:
    return [*login(identity), known("input", target="#project-name", value_reference=reference), known("repeat_submit", target="#project-submit", url=base_url + "/api/projects"), known("wait", target=f"#projects-table .project-name:text-is('{name}')"), *check(f"#projects-table .project-name:text-is('{name}')", "EB-PROJECT-SUBMIT", assertion="count", expected="1")]


def task_workflow(identity: str, project_reference: str, title: str, edited: str, *, create_reference: str | None = None, project_name: str | None = None, delete: bool = True) -> list[tuple[str, dict[str, Any]]]:
    steps = login(identity)
    if create_reference is not None and project_name is not None:
        steps.extend(create_project(create_reference, project_name))
    steps.extend([("explore_unknown_path", {"current_goal": "Open Tasks"}), known("select", target="#task-project", value_reference=project_reference), known("wait", wait_ms=50), known("input", target="#task-title", value_reference="task_title"), known("click", target="#task-form button[type='submit']"), known("wait", target=f"#tasks-table tr:has-text('{title}')"), known("click", target=f"#tasks-table tr:has-text('{title}') .edit-task"), known("input", target="#edit-value", value_reference="edited_task_title"), known("click", target="#edit-save"), known("wait", target=f"#tasks-table tr:has-text('{edited}')"), *check(f"#tasks-table tr:has-text('{edited}')", "EB-TASK-LIFECYCLE")])
    if delete:
        row = f"#tasks-table tr:has-text('{edited}')"
        steps.extend([known("click", target=row + " .delete-task"), known("wait", wait_ms=50), known("refresh"), known("wait", target="#app-section"), known("wait", wait_ms=50), known("click", target="#nav-tasks"), known("select", target="#task-project", value_reference=project_reference), known("wait", wait_ms=50), *check(row, "EB-TASK-DELETE", assertion="hidden")])
    return steps


def member_workflow(identity: str, data: Mapping[str, str]) -> list[tuple[str, dict[str, Any]]]:
    reference = "member_project_name"
    return [*login(identity), *create_project(reference, data[reference]), known("click", target="#nav-members"), known("wait", wait_ms=50), known("select", target="#member-project", value_reference=reference), known("wait", wait_ms=50), known("input", target="#member-username", value_reference="invited_username"), known("click", target="#member-form button[type='submit']"), known("wait", target=f"#members-table tr[data-username='{data['invited_username']}']"), known("click", target=f"#members-table tr[data-username='{data['invited_username']}'] .edit-member"), known("input", target="#edit-value", value_reference="member_display_name"), known("click", target="#edit-save"), known("wait", wait_ms=50), known("click", target=f"#members-table tr[data-username='{data['invited_username']}'] .remove-member"), known("wait", wait_ms=50), *check(f"#members-table tr[data-username='{data['invited_username']}']", "EB-MEMBER-LIFECYCLE", assertion="hidden")]


def scripts_for(case: Mapping[str, Any], base_url: str) -> dict[str, list[tuple[str, dict[str, Any]]]]:
    data = case["required_test_data"]
    scenario_id = case["scenario_id"]
    scripts: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for goal in case["required_test_goals"]:
        goal_id = goal["goal_id"]
        identity = goal["identity_reference"]
        behaviors = goal["expected_behavior_ids"]
        if scenario_id == "D01":
            steps = task_workflow(identity, "seed_project_id", data["task_title"], data["edited_task_title"])
            steps[5:5] = check("#identity", "EB-AUTH-SESSION", assertion="equals", expected="member (member)")
            steps.extend([known("click", target="#logout"), known("wait", target="#auth-section"), *check("#auth-section", "EB-AUTH-SESSION")])
            scripts[goal_id] = steps
        elif "EB-PROJECT-SUBMIT" in behaviors:
            reference = "project_name" if scenario_id == "D08" else "submission_project_name"
            scripts[goal_id] = repeat_project(identity, reference, data[reference], base_url)
        elif "EB-MEMBER-ACCESS" in behaviors:
            project_id = data.get("private_project_id", data.get("seed_project_id"))
            admin_goal = next(item["goal_id"] for item in case["required_test_goals"] if item["identity_reference"] == "admin" and "EB-MEMBER-LIFECYCLE" in item["expected_behavior_ids"])
            before_login = [("pause", {"name": "invited"})] if scenario_id == "H04" else []
            scripts[goal_id] = [*before_login, *login(identity), known("wait", target=f"#projects-table tr[data-project-id='{project_id}']"), ("update_task_progress", {"progressed": True, "summary": "member-session-ready"}), ("signal", {"name": "member-ready"}), ("pause", {"name": "removed"}), ("read_shared_facts", {}), known("refresh"), known("wait", target="#app-section"), known("wait", wait_ms=50), *check(f"#projects-table tr[data-project-id='{project_id}']", "EB-MEMBER-ACCESS", assertion="hidden", related=[admin_goal])]
            if "EB-PROJECT-DELETE-AUTH" in behaviors:
                scripts[goal_id].extend([known("click", target=f"#projects-table tr[data-project-id='{project_id}'] .delete-project"), known("wait", wait_ms=50), *check(f"#projects-table tr[data-project-id='{project_id}']", "EB-PROJECT-DELETE-AUTH", related=[admin_goal])])
        elif "EB-MEMBER-LIFECYCLE" in behaviors and scenario_id in {"D09", "D12", "H04", "H06"}:
            project_reference = "private_project_id" if scenario_id == "H04" else "seed_project_id"
            removed = "member2" if scenario_id == "H04" else "member"
            steps = [*login(identity), known("click", target="#nav-members"), known("wait", wait_ms=50), known("select", target="#member-project", value_reference=project_reference), known("wait", wait_ms=50)]
            if scenario_id == "H04":
                steps.extend([known("input", target="#member-username", value_reference="invited_username"), known("click", target="#member-form button[type='submit']"), known("wait", target="#members-table tr[data-username='member2']"), ("update_task_progress", {"progressed": True, "summary": "invitation-ready"}), ("signal", {"name": "invited"})])
            steps.extend([("pause", {"name": "member-ready"}), known("click", target=f"#members-table tr[data-username='{removed}'] .remove-member"), known("wait", wait_ms=50), *check(f"#members-table tr[data-username='{removed}']", "EB-MEMBER-LIFECYCLE", assertion="hidden"), ("update_task_progress", {"progressed": True, "summary": "membership-removed"}), ("signal", {"name": "removed"})])
            scripts[goal_id] = steps
        elif "EB-PROJECT-NAME" in behaviors:
            scripts[goal_id] = [*login(identity), known("input", target="#project-name", value_reference="empty_project_name"), known("click", target="#project-submit"), known("wait", wait_ms=50), *check("#message", "EB-PROJECT-NAME", assertion="contains", expected="project name is required")]
        elif "EB-PROJECT-SAVE" in behaviors:
            reference = "owned_project_name"
            scripts[goal_id] = [*login(identity), *create_project(reference, data[reference]), *check("#projects-table tbody tr", "EB-PROJECT-LIFECYCLE", assertion="count", expected="1"), known("click", target="#projects-table .edit-project"), known("input", target="#edit-value", value_reference="edited_project_name"), known("click", target="#edit-save"), known("wait", wait_ms=50), *check("#projects-table .project-name", "EB-PROJECT-SAVE", assertion="equals", expected=data["edited_project_name"])]
        elif "EB-TASK-LIFECYCLE" in behaviors:
            reference = "task_project_name" if scenario_id == "D13" else "seed_project_id" if scenario_id == "D15" else "joined_project_id"
            scripts[goal_id] = task_workflow(identity, reference, data["task_title"], data["edited_task_title"], create_reference=reference if scenario_id == "D13" else None, project_name=data.get(reference) if scenario_id == "D13" else None, delete="EB-TASK-DELETE" in behaviors)
        elif "EB-MEMBER-LIFECYCLE" in behaviors:
            scripts[goal_id] = member_workflow(identity, data)
        else:
            raise AssertionError(f"readiness script missing for {goal_id}")
        scripts[goal_id].append(("finish_task", {}))
    return scripts


class ScriptedWorker(FunctionInvocationLayer, ChatMiddlewareLayer, BaseChatClient):
    def __init__(self, scripts: Mapping[str, list[tuple[str, dict[str, Any]]]], signals: Mapping[str, asyncio.Event], behaviors: Mapping[str, str]) -> None:
        super().__init__()
        self.scripts = scripts
        self.signals = signals
        self.behaviors = behaviors
        self.script: list[tuple[str, dict[str, Any]]] | None = None
        self.position = 0
        self.calls = 0

    async def _inner_get_response(self, *, messages: Sequence[Message], stream: bool, options: Mapping[str, Any], **kwargs: Any) -> ChatResponse:
        text = "\n".join(message.text for message in messages)
        assert not re.search(r"\bB[1-6]\b|enabled_seeded_bugs|expected_bug_count|ground_truth", text)
        assert "demo-admin" not in text and "demo-member" not in text
        if self.script is None:
            task_id = re.search(r"Execute assigned Task ([^:]+):", text).group(1)
            self.script = self.scripts[task_id]
        last: dict[str, Any] = {}
        for message in reversed(messages):
            content = next((item for item in reversed(message.contents) if item.type == "function_result"), None)
            if content is not None:
                last = json.loads(content.result)
                break
        while self.position < len(self.script):
            name, arguments = self.script[self.position]
            self.position += 1
            if name == "pause":
                await asyncio.wait_for(self.signals[arguments["name"]].wait(), timeout=15)
                continue
            if name == "signal":
                self.signals[arguments["name"]].set()
                continue
            if name == "report_if_failed":
                if last.get("error_type") != "ASSERTION_FAILURE":
                    continue
                name = "record_finding"
                arguments = {**arguments, "expected_result": self.behaviors[arguments["behavior_id"]], "actual_result": json.dumps(last.get("data", {}))}
            self.calls += 1
            return ChatResponse(messages=[Message(role="assistant", contents=[Content.from_function_call(f"readiness-{self.calls}", name, arguments=arguments)])])
        raise AssertionError("finish_task did not terminate the worker")


def test_all_24_scenarios_prepare_without_answer_loading(monkeypatch: pytest.MonkeyPatch) -> None:
    original = Path.read_text

    def read(path: Path, *args: Any, **kwargs: Any) -> str:
        assert path.resolve() != ANSWERS.resolve(), "runtime preparation read the answer file"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    dataset = json.loads(SCENARIOS.read_text(encoding="utf-8"))
    assert len(dataset["scenarios"]) == 24
    assert sum(case["split"] == "development" for case in dataset["scenarios"]) == 16
    assert sum(case["split"] == "holdout" for case in dataset["scenarios"]) == 8
    for case in dataset["scenarios"]:
        config = load_run_config(SCENARIOS, scenario_id=case["scenario_id"], target_url="http://127.0.0.1:8000")
        assert config.budget.max_testers == config.budget.max_parallel_browser_contexts == 3
        assert len(config.expected_behaviors) == len(case["expected_behaviors"])
        assert not re.search(r"\bB[1-6]\b|enabled_seeded_bugs|expected_bug_count|ground_truth", config.model_dump_json())


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("scenario_id,mode", [(scenario_id, "bugs") for scenario_id in BLOCKED_IDS] + [("D01", "normal"), ("D01", "false_positive"), ("D08", "incomplete"), ("D01", "execution_failure")])
async def test_blocked_cases_reproduce_verify_and_match_with_fake_providers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario_id: str, mode: str) -> None:
    dataset = json.loads(SCENARIOS.read_text(encoding="utf-8"))
    case = next(item for item in dataset["scenarios"] if item["scenario_id"] == scenario_id)
    bug_fields = {"B1": "b1_deleted_task_reappears", "B2": "b2_member_deletes_other_project", "B3": "b3_empty_project_name", "B4": "b4_saved_ui_stale", "B5": "b5_double_submit_duplicates", "B6": "b6_removed_member_session_active"}
    bugs = SeededBugs(**{field: bug_id in case["enabled_seeded_bugs"] for bug_id, field in bug_fields.items()})
    for identity in ["admin", "member", "member2"]:
        monkeypatch.setenv(f"DEMO_{identity.upper()}_PASSWORD", "demo-" + identity)
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    tasks = [{"task_id": goal["goal_id"], "goal": goal["description"], "feature": goal["feature"], "priority": "P1", "dependencies": [], "step_budget": 90, "data_requirements": {"identity_reference": goal["identity_reference"], "role": next(account["role"] for account in case["allowed_roles"] if account["identity_reference"] == goal["identity_reference"]), "expected_behavior_ids": goal["expected_behavior_ids"], "test_data_keys": goal["test_data_keys"]}, "scope_targets": ["/"], "required_operations": ["read", "create", "update", "delete"]} for goal in case["required_test_goals"]]
    main = PlanningClient(tasks)
    signals = {name: asyncio.Event() for name in ["invited", "member-ready", "removed"]}
    workers: list[ScriptedWorker] = []
    with DemoAppServer(bugs=bugs) as app:
        config = load_run_config(SCENARIOS, scenario_id=scenario_id, target_url=app.base_url)
        scripts = scripts_for(case, app.base_url)
        goal_id = case["required_test_goals"][0]["goal_id"]
        if mode == "false_positive":
            scripts[goal_id] = [*login("member"), *check("#identity", "EB-AUTH-SESSION", assertion="equals", expected="member (member)"), known("click", target="#nav-tasks"), known("wait", target="#tasks-table tr[data-task-id='task-1']"), *check("#tasks-table tr[data-task-id='task-1']", "EB-TASK-LIFECYCLE"), *check("#tasks-table tr[data-task-id='task-1']", "EB-TASK-DELETE", assertion="hidden"), ("finish_task", {})]
        elif mode == "incomplete":
            scripts[goal_id] = [*login("member2"), ("finish_task", {})]
        elif mode == "execution_failure":
            scripts[goal_id] = [*login("member"), known("click", target="#does-not-exist"), ("finish_task", {})]

        def worker_factory() -> ScriptedWorker:
            worker = ScriptedWorker(scripts, signals, {behavior.behavior_id: behavior.description for behavior in config.expected_behaviors})
            workers.append(worker)
            return worker

        settings = Settings(_env_file=None, langsmith_tracing=False, artifacts_dir=tmp_path, state_db_path=tmp_path / "state.db")
        executor = FormalRunExecutor(config, settings, scenario_id=scenario_id, ground_truth_path=ANSWERS, main_client_factory=lambda: main, tester_client_factory=worker_factory, jev_selector_factory=lambda: JevSelector(FakeJev()))
        controls = EvaluationControls(demo_version=dataset["demo_version"], seeded_bugs=tuple(case["enabled_seeded_bugs"]), model_configuration=(), token_budget=100000, time_budget_seconds=900, accounts=tuple(account["identity_reference"] for account in case["allowed_roles"]), initial_data=(), test_scope=("/",))
        variant = build_evaluation_plan(EvaluationMode.TESTER_COUNT, controls).variants[1]
        route = build_execution_route(variant, full_evaluation_enabled=False, fake_provider=True)
        result = await executor.execute(config=variant, route=route, run_number=1, run_id="readiness-" + scenario_id, evidence_directory=tmp_path / "series" / "run-1" / "readiness" / "evidence")
    matching_path = tmp_path / "series" / "run-1" / ("readiness-" + scenario_id) / "ground_truth_matching.json"
    matching = json.loads(matching_path.read_text(encoding="utf-8"))
    assert result.status == "COMPLETED", result.error
    if mode == "false_positive":
        assert matching["tp"] == 0 and matching["fp"] == 1
        assert matching["missed_bug_ids"] == []
        assert result.metrics["bug_precision"] == 0.0 and result.metrics["bug_recall"] == "N/A"
        assert result.metrics["task_success_rate"] == 0.0
        assert matching["task_outcomes"][goal_id] == "AGENT_EXECUTION_FAILURE"
        return
    if mode in {"incomplete", "execution_failure"}:
        assert matching["finding_to_bug"] == {}
        assert matching["task_outcomes"][goal_id] == ("UNKNOWN_INCOMPLETE" if mode == "incomplete" else "AGENT_EXECUTION_FAILURE")
        assert result.metrics["task_success_rate"] == 0.0
        if mode == "incomplete":
            assert matching["missed_bug_ids"] == ["B5"] and result.metrics["bug_recall"] == 0.0
        return
    assert matching["fp"] == 0, matching
    assert set(matching["finding_to_bug"].values()) == set(case["enabled_seeded_bugs"]), matching
    assert matching["missed_bug_ids"] == [], matching
    if mode == "normal":
        assert matching["tp"] == 0
        assert result.metrics["bug_precision"] == result.metrics["bug_recall"] == "N/A"
    else:
        assert result.metrics["bug_precision"] == result.metrics["bug_recall"] == 1.0
        assert result.metrics["reproduction_success_rate"] == 1.0
    assert set(matching["task_outcomes"].values()) <= {"APPLICATION_BUG_DETECTED", "NORMAL_APPLICATION_BEHAVIOR"}, matching
    assert result.metrics["task_success_rate"] == 1.0
    assert len(workers) == len(case["required_test_goals"])
    store = StateStore(tmp_path / "series" / "run-1" / "state.db")
    for finding in store.list_recent_findings("readiness-" + scenario_id):
        assert finding["status"] in {"CONFIRMED_BUG", "DUPLICATE"}
    for goal in case["required_test_goals"]:
        histories = store.list_action_history(run_id="readiness-" + scenario_id, task_id=goal["goal_id"])
        assert "demo-admin" not in json.dumps(histories) and "demo-member" not in json.dumps(histories)


def test_answer_reader_rejects_active_runs_before_opening_file(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    store.initialize()
    store.create_run(run_id="active", application="http://demo.test", application_version="phase5-demo-1.0", test_goal="Active local check", scope={}, status="RUNNING", global_budget={}, remaining_budget={})
    with pytest.raises(PermissionError):
        read_ground_truth(tmp_path / "missing.json", scenario_id="D08", run_id="active", store=store)


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_repeated_pending_submit_records_identity_stably(phase2_store: StateStore, enabled: bool) -> None:
    budget = BudgetGuard(BudgetLimits(max_runtime_seconds=60, max_llm_calls=0, max_input_tokens=0, max_output_tokens=0, max_jev_calls=0, max_computer_use_calls=0, max_task_steps=40, max_task_replans=0, max_browser_contexts=1))
    manager = BrowserManager(budget)
    try:
        with DemoAppServer(bugs=SeededBugs(b5_double_submit_duplicates=enabled)) as app:
            checker = PermissionChecker(store=phase2_store, policy=ExecutionPolicy(allowed_url_prefixes=(app.base_url + "/",)), run_id="run-1", task_id="task-1", tester_id="tester-1")
            executor = PlaywrightExecutor(store=phase2_store, permission_checker=checker, run_id="run-1", task_id="task-1", tester_id="tester-1", identity_reference="member2")
            session = await manager.create_session(tester_id="tester-1", identity_id="identity-1")
            actions = [WebAction(action_type=ActionType.NAVIGATION, url=app.base_url), WebAction(action_type=ActionType.INPUT, target="#login-username", value="member2", value_reference="username"), WebAction(action_type=ActionType.INPUT, target="#login-password", value="demo-member2", value_reference="env:TEST_PASSWORD"), WebAction(action_type=ActionType.CLICK, target="#login-form button[type='submit']"), WebAction(action_type=ActionType.WAIT, target="#app-section"), WebAction(action_type=ActionType.INPUT, target="#project-name", value="repeat-check", value_reference="project_name")]
            for action in actions:
                assert (await executor.execute(page=session.page, browser_session_id=session.session_id, action=action)).success
            previous = None
            for count in [1, 2]:
                result = await executor.execute(page=session.page, browser_session_id=session.session_id, action=WebAction(action_type=ActionType.REPEAT_SUBMIT, target="#project-submit", url=app.base_url + "/api/projects"))
                assert result.success, result.error
                requests = result.data["requests"]
                assert len(requests) == 2 and requests[0]["submission_id"] == requests[1]["submission_id"]
                assert requests[0]["submission_id"] != previous
                previous = requests[0]["submission_id"]
                identities = {request["response_data"]["project"]["project_id"] for request in requests}
                assert len(identities) == (2 if enabled else 1)
                await session.page.wait_for_function("count => document.querySelectorAll('#projects-table tbody tr').length === count", arg=count * (2 if enabled else 1))
            assert "demo-member2" not in json.dumps(phase2_store.list_action_history(run_id="run-1", task_id="task-1"))
    finally:
        await manager.close()


def test_failed_and_interrupted_runs_remain_in_bug_denominators() -> None:
    records = [EvaluationRunRecord(mode="local", variant_id="same", run_number=index, run_id=str(index), status=status, metrics={"true_positive_count": tp, "false_positive_count": fp, "detected_bug_count": tp, "enabled_bug_count": enabled}, evidence_directory="local") for index, (status, tp, fp, enabled) in enumerate([("COMPLETED", 1, 0, 1), ("FAILED", 0, 0, 1), ("INTERRUPTED", 0, 1, 0)], 1)]
    quality = summarize_evaluation_runs(records)["same"]["bug_quality"]
    assert quality == {"tp": 1.0, "fp": 1.0, "missed_bug_count": 1.0, "bug_precision": 0.5, "bug_recall": 0.5}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_replay_keeps_distinct_sessions_for_the_same_identity(phase2_store: StateStore) -> None:
    budget = BudgetGuard(BudgetLimits(max_runtime_seconds=60, max_llm_calls=0, max_input_tokens=0, max_output_tokens=0, max_jev_calls=0, max_computer_use_calls=0, max_task_steps=40, max_task_replans=0, max_browser_contexts=2))
    manager = BrowserManager(budget)
    phase2_store.create_finding(finding_id="session-check", run_id="run-1", task_id="task-1", title="Session separation check", status="OBSERVATION", expected_result="Private session per recorded workflow", actual_result="Check fresh-session authentication", first_seen_by="tester-1")
    root = phase2_store.database_path.parent
    try:
        with DemoAppServer() as app:
            checker = PermissionChecker(store=phase2_store, policy=ExecutionPolicy(allowed_url_prefixes=(app.base_url + "/",)), run_id="run-1", task_id="task-1", tester_id="tester-1")
            executor = PlaywrightExecutor(store=phase2_store, permission_checker=checker, run_id="run-1", task_id="task-1", tester_id="tester-1")
            steps = []
            for session_reference in ["setup", "check"]:
                actions = [ReplayStep(action_type=ActionType.NAVIGATION, url=app.base_url), ReplayStep(action_type=ActionType.WAIT, wait_ms=50), ReplayStep(action_type=ActionType.ASSERTION, target="#auth-section", assertion="visible"), ReplayStep(action_type=ActionType.INPUT, target="#login-username", value_reference="username"), ReplayStep(action_type=ActionType.INPUT, target="#login-password", value_reference="env:TEST_PASSWORD"), ReplayStep(action_type=ActionType.CLICK, target="#login-form button[type='submit']"), ReplayStep(action_type=ActionType.WAIT, target="#app-section")]
                steps.extend(replace(action, identity_reference="member", session_reference=session_reference) for action in actions)
            plan = ReplayPlan(run_id="run-1", task_id="task-1", tester_id="tester-1", identity_id="identity-1", data_requirements={"identity_reference": "member"}, steps=tuple(steps), input_values={"username": "member", "env:TEST_PASSWORD": "demo-member"}, identities={"member": "identity-1"})
            replay = DeterministicReplay(store=phase2_store, browser_manager=manager, executor=executor, evidence_store=EvidenceStore(store=phase2_store, artifacts_root=root / "runs", temporary_sensitive_root=root / "temporary", secrets=("demo-member",)), budget=budget, budget_id="budget-1")
            result = await replay.run_attempt(finding_id="session-check", plan=plan, purpose="REPRODUCTION")
            assert result.matched and not result.environment_issue, result.reason
            histories = phase2_store.list_action_history(run_id="run-1", task_id="task-1")
            assert len({item["browser_session_id"] for item in histories}) == 2
    finally:
        await manager.close()


@pytest.mark.browser
@pytest.mark.asyncio
@pytest.mark.parametrize("view,expected", [("members", 2), ("tasks", 1)])
async def test_overlapping_view_loads_do_not_duplicate_seed_rows(view: str, expected: int) -> None:
    with DemoAppServer() as app:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(app.base_url)
                await page.locator("#login-username").fill("admin")
                await page.locator("#login-password").fill("demo-admin")
                await page.locator("#login-form button[type='submit']").click()
                await page.locator("#projects-table tr[data-project-id='project-1']").wait_for()
                first_started = asyncio.Event()
                release_first = asyncio.Event()
                requests = 0

                async def reversed_responses(route: Route) -> None:
                    nonlocal requests
                    requests += 1
                    number = requests
                    response = await route.fetch()
                    if number == 1:
                        first_started.set()
                        await asyncio.wait_for(release_first.wait(), timeout=5)
                    await route.fulfill(response=response)
                    if number == 2:
                        release_first.set()

                await page.route(f"**/api/projects/project-1/{view}", reversed_responses)
                await page.locator(f"#nav-{view}").click()
                await asyncio.wait_for(first_started.wait(), timeout=5)
                selector = "#member-project" if view == "members" else "#task-project"
                await page.locator(selector).select_option("project-1")
                await asyncio.wait_for(release_first.wait(), timeout=5)
                await page.wait_for_load_state("networkidle")
                assert await page.locator(f"#{view}-table tbody tr").count() == expected
            finally:
                await browser.close()
