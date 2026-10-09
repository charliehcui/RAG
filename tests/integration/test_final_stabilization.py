from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from agent_framework import BaseChatClient, ChatResponse, Content, Message
from agent_framework._tools import FunctionInvocationLayer

from demo_app import DemoAppServer, SeededBugs
from web_testing_system.config import Settings
from web_testing_system.evaluation.matching import match_ground_truth
from web_testing_system.evaluation.metrics import MetricsCalculator
from web_testing_system.evaluation.scenarios import load_run_config
from web_testing_system.orchestration.runner import run
from web_testing_system.runtime.jev_selector import JevSelector
from web_testing_system.state import StateStore


def assertion(check_id: str | None, target: str, *, assertion: str = "hidden", expected: str | None = None, behavior_id: str | None = None) -> dict[str, Any]:
    return {"action_type": "assertion", "target": target, "assertion": assertion, "expected": expected, "check_id": check_id, "behavior_id": behavior_id}


class LocalMain(FunctionInvocationLayer, BaseChatClient):
    async def _inner_get_response(self, *, messages: Sequence[Message], stream: bool, options: Mapping[str, Any], **kwargs: Any) -> ChatResponse:
        if "Final Report: " in messages[-1].text:
            return ChatResponse(messages=[Message(role="assistant", contents=["Both retained role workflows completed."])])
        tasks = [{"goal_id": goal_id, "goal": "Complete only this role's assigned offboarding workflow", "priority": "P1", "required_operations": operations} for goal_id, operations in [("offboarded-access", ["read", "delete"]), ("offboard-member", ["read", "delete"])]]
        return ChatResponse(messages=[Message(role="assistant", contents=[Content.from_function_call("main", "submit_task_plan", arguments={"tasks": tasks})])])


class LocalTester(FunctionInvocationLayer, BaseChatClient):
    async def _inner_get_response(self, *, messages: Sequence[Message], stream: bool, options: Mapping[str, Any], **kwargs: Any) -> ChatResponse:
        contract = json.loads(messages[-1].text.split("Task Contract: ", 1)[1])
        identity = contract["identity_reference"]
        remaining = contract["remaining_check_ids"]
        login = {"goal": "Authenticate assigned identity", "operation": "login", "inputs": [{"control": "Login username", "value_reference": identity + "_username"}, {"control": "Login password", "value_reference": "env:DEMO_" + identity.upper() + "_PASSWORD"}], "checks": [assertion(None, "#app-section", assertion="visible", behavior_id="EB-MEMBER-ACCESS" if identity == "member" else "EB-MEMBER-LIFECYCLE")]}
        if identity == "member" and len(remaining) == 1:
            assert remaining == ["D12.delete-rejected"]
            assert set(contract["completed_check_ids"]) == {"D12.member-ready", "D12.revoked-project", "D12.revoked-tasks"}
            goals = [{"goal": "Record unavailable forbidden control on original object", "operation": "observe", "run_operations": False, "checks": [assertion("D12.delete-rejected", '#projects-table tr[data-project-id="project-1"] .delete-project')], "publish_progress": "member-delete-observed"}]
        elif identity == "member":
            goals = [login,
                {"goal": "Establish same member session task access", "operation": "navigate", "destination": "Tasks", "project_reference": "seed_project_id", "checks": [assertion("D12.member-ready", '#tasks-table tr[data-task-id="task-1"]', assertion="visible")], "publish_progress": "member-session-ready"},
                {"goal": "Record original Project read boundary", "operation": "navigate", "destination": "Projects", "before_steps": [{"action_type": "refresh"}], "wait_for_progress": "membership-removed", "checks": [assertion("D12.revoked-project", '#projects-table tr[data-project-id="project-1"]')]},
                {"goal": "Record task access without selecting another Project", "operation": "navigate", "destination": "Tasks", "checks": [assertion("D12.revoked-tasks", '#tasks-table tr[data-task-id="task-1"]')]},
                {"goal": "Return to the destructive boundary", "operation": "navigate", "destination": "Projects"},
                {"goal": "Attempt only the original Project Delete", "operation": "delete", "row_reference": "seed_project_id", "checks": [assertion("D12.delete-rejected", '#projects-table tr[data-project-id="project-1"]', assertion="contains", expected="Seed Project")], "publish_progress": "member-delete-observed"}]
        elif identity == "admin" and remaining == ["D12.delete-preservation"]:
            goals = [{"goal": "Record original preservation deviation after object disappearance", "operation": "navigate", "destination": "Tasks", "checks": [assertion("D12.delete-preservation", '#task-project option[value="project-1"], #tasks-table tr[data-task-id="task-1"]', assertion="count", expected="2")]}]
        else:
            goals = [login,
                {"goal": "Open original Project membership", "operation": "navigate", "destination": "Members", "project_reference": "seed_project_id", "wait_for_progress": "member-session-ready"},
                {"goal": "Remove only the supplied membership", "operation": "delete", "project_reference": "seed_project_id", "row_reference": "removed_username", "checks": [assertion("D12.membership-removed", '#members-table tr[data-username="member"]')]},
                {"goal": "Verify durable membership removal", "operation": "navigate", "destination": "Members", "project_reference": "seed_project_id", "before_steps": [{"action_type": "refresh"}], "checks": [assertion("D12.removal-persisted", '#members-table tr[data-username="member"]')], "publish_progress": "membership-removed"},
                {"goal": "Observe original Project preservation before task data", "operation": "navigate", "destination": "Projects", "project_reference": "seed_project_id", "before_steps": [{"action_type": "refresh"}], "wait_for_progress": "member-delete-observed", "checks": [assertion(None, '#projects-table tr[data-project-id="project-1"]', assertion="visible")]},
                {"goal": "Preserve original Project and Seed Task in retained admin session", "operation": "navigate", "destination": "Tasks", "project_reference": "seed_project_id", "checks": [{"action_type": "assertion", "control": "Title", "expected": "Seed Task", "assertion": "equals", "check_id": "D12.delete-preservation", "behavior_id": "EB-PROJECT-DELETE-AUTH"}]}]
        return ChatResponse(messages=[Message(role="assistant", contents=[Content.from_function_call("tester", "execute_test_plan", arguments={"goals": goals})])])


class LocalChoice:
    def predict(self, state: Mapping[str, Any], questions: Mapping[str, Any]) -> Mapping[str, Any]:
        assert questions["next_candidate"]["type"] == "choice"
        return {"answers": {"next_candidate": {"choice": state["legal_candidates"][0]["id"], "confidence": .95}}}


@pytest.mark.asyncio
async def test_runtime_object_view_recovery_completes_live_permission_flow_without_tester_replan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class RecoveryTester(LocalTester):
        async def _inner_get_response(self, **kwargs: Any) -> ChatResponse:
            response = await super()._inner_get_response(**kwargs)
            goals = response.messages[0].contents[0].arguments["goals"]
            if goals[-1]["goal"] == "Attempt only the original Project Delete":
                goals.pop(-2)
                goals[-1]["checks"] = [assertion("D12.delete-rejected", '#projects-table tr[data-project-id="project-1"] .delete-project')]
            return response

    for identity in ["ADMIN", "MEMBER"]:
        monkeypatch.setenv("DEMO_" + identity + "_PASSWORD", "demo-" + identity.lower())
    settings = Settings(_env_file=None, langsmith_tracing=False, state_db_path=tmp_path / "state.db", artifacts_dir=tmp_path / "runs", temporary_sensitive_dir=tmp_path / "temporary")
    with DemoAppServer(bugs=SeededBugs(b2_member_deletes_other_project=True, b6_removed_member_session_active=True)) as app:
        config = load_run_config(Path("evaluation/scenarios.json"), scenario_id="D12", target_url=app.base_url)
        report_path = await run(config, settings, run_id="local-runtime-view-recovery", main_client=LocalMain(), tester_client_factory=RecoveryTester, jev_selector=JevSelector(LocalChoice()))
    store = StateStore(settings.state_db_path)
    comparison, _ = match_ground_truth(Path("evaluation/ground_truth.json"), scenario_id="D12", run_id="local-runtime-view-recovery", store=store, artifacts_root=settings.artifacts_dir, test_data=config.test_data)
    metrics = MetricsCalculator(store).calculate("local-runtime-view-recovery", ground_truth=comparison)
    assert metrics["task_success_rate"] == 1, report_path.read_text(encoding="utf-8")
    assert metrics["completed_check_count"] == 7
    assert metrics["false_positive_count"] == 0 and metrics["bug_recall"] == 1
    assert len(store.list_events("local-runtime-view-recovery", event_types=("TESTER_PLAN_VALIDATED",))) == 2
    assert any(event["result"].get("reason") == "OBJECT_NOT_VISIBLE_IN_CURRENT_VIEW" and event["result"]["recovered"] for event in store.list_events("local-runtime-view-recovery", event_types=("RUNTIME_RECOVERY_RESULT",)))


@pytest.mark.asyncio
@pytest.mark.parametrize("bugs", [False, True])
async def test_explicit_delete_destination_and_permission_evidence_complete_the_retained_workflow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bugs: bool) -> None:
    class DestinationTester(LocalTester):
        async def _inner_get_response(self, **kwargs: Any) -> ChatResponse:
            response = await super()._inner_get_response(**kwargs)
            goals = response.messages[0].contents[0].arguments["goals"]
            if goals[-1]["goal"] == "Attempt only the original Project Delete":
                goals.pop(-2)
                goals[-1]["destination"] = "Projects"
            return response

    for identity in ["ADMIN", "MEMBER"]:
        monkeypatch.setenv("DEMO_" + identity + "_PASSWORD", "demo-" + identity.lower())
    settings = Settings(_env_file=None, langsmith_tracing=False, state_db_path=tmp_path / "state.db", artifacts_dir=tmp_path / "runs", temporary_sensitive_dir=tmp_path / "temporary")
    with DemoAppServer(bugs=SeededBugs(b2_member_deletes_other_project=bugs, b6_removed_member_session_active=bugs)) as app:
        config = load_run_config(Path("evaluation/scenarios.json"), scenario_id="D12", target_url=app.base_url)
        report_path = await run(config, settings, run_id="local-operation-destination", main_client=LocalMain(), tester_client_factory=DestinationTester, jev_selector=JevSelector(LocalChoice()))
    store = StateStore(settings.state_db_path)
    comparison, _ = match_ground_truth(Path("evaluation/ground_truth.json"), scenario_id="D12", run_id="local-operation-destination", store=store, artifacts_root=settings.artifacts_dir, test_data=config.test_data)
    metrics = MetricsCalculator(store).calculate("local-operation-destination", ground_truth=comparison)
    assert metrics["task_success_rate"] == 1, report_path.read_text(encoding="utf-8")
    assert metrics["completed_check_count"] == 7
    assert metrics["false_positive_count"] == 0
    assert len(store.list_events("local-operation-destination", event_types=("TESTER_PLAN_VALIDATED",))) == (2 if bugs else 3)
    if bugs:
        assert metrics["bug_recall"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("bugs", [False, True])
async def test_live_permission_result_uses_bound_objects_and_keeps_auxiliary_failures_unscored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bugs: bool) -> None:
    class ScopedTester(LocalTester):
        async def _inner_get_response(self, **kwargs: Any) -> ChatResponse:
            response = await super()._inner_get_response(**kwargs)
            goals = response.messages[0].contents[0].arguments["goals"]
            for goal in goals:
                if goal["goal"] == "Open original Project membership":
                    goal["checks"] = [{"action_type": "assertion", "control": "Member", "context": "removed_username", "assertion": "visible"}]
                if goal["goal"] == "Attempt only the original Project Delete":
                    goal["checks"] = [assertion(None, '#projects-table tr[data-project-id="project-1"]', assertion="visible"), assertion("D12.delete-rejected", '#projects-table tr[data-project-id="project-1"] .delete-project')]
            return response

    for identity in ["ADMIN", "MEMBER"]:
        monkeypatch.setenv("DEMO_" + identity + "_PASSWORD", "demo-" + identity.lower())
    settings = Settings(_env_file=None, langsmith_tracing=False, state_db_path=tmp_path / "state.db", artifacts_dir=tmp_path / "runs", temporary_sensitive_dir=tmp_path / "temporary")
    with DemoAppServer(bugs=SeededBugs(b2_member_deletes_other_project=bugs, b6_removed_member_session_active=bugs)) as app:
        config = load_run_config(Path("evaluation/scenarios.json"), scenario_id="D12", target_url=app.base_url)
        report_path = await run(config, settings, run_id="local-scoped-permission", main_client=LocalMain(), tester_client_factory=ScopedTester, jev_selector=JevSelector(LocalChoice()))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    store = StateStore(settings.state_db_path)
    comparison, _ = match_ground_truth(Path("evaluation/ground_truth.json"), scenario_id="D12", run_id="local-scoped-permission", store=store, artifacts_root=settings.artifacts_dir, test_data=config.test_data)
    metrics = MetricsCalculator(store).calculate("local-scoped-permission", ground_truth=comparison)
    assert metrics["task_success_rate"] == 1, json.dumps(report["task_outcomes"])
    assert metrics["completed_check_count"] == 7
    assert metrics["false_positive_count"] == 0
    assert len(store.list_events("local-scoped-permission", event_types=("TESTER_PLAN_VALIDATED",))) == (2 if bugs else 3)
    assert [event["result"]["summary"] for event in store.list_events("local-scoped-permission", event_types=("TASK_PROGRESS",))] == ["member-session-ready", "membership-removed", "member-delete-observed"]
    if bugs:
        assert metrics["bug_recall"] == 1
        assert len(store.list_events("local-scoped-permission", event_types=("PERMISSION_ASSERTION_BOUND",))) == 1
        assert store.list_events("local-scoped-permission", event_types=("READ_ONLY_ASSERTION_CONTINUATION",))


@pytest.mark.asyncio
async def test_live_replan_receives_failed_check_and_accepts_unambiguous_assertion_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class ContractTester(LocalTester):
        async def _inner_get_response(self, **kwargs: Any) -> ChatResponse:
            contract = json.loads(kwargs["messages"][-1].text.split("Task Contract: ", 1)[1])
            response = await super()._inner_get_response(**kwargs)
            goals = response.messages[0].contents[0].arguments["goals"]
            if contract["identity_reference"] == "admin":
                if contract["latest_failure"]:
                    detail = contract["latest_failure"]["failed_check"]
                    assert detail["check_id"] == "D12.membership-removed"
                    assert detail["control"] == "Member" and detail["assertion"] == "hidden"
                    assert detail["object"]["context"] == "Seed Project"
                    preservation = goals[-2:]
                    goals = [{"goal": "Check original removed row without replaying removal", "operation": "observe", "run_operations": False, "checks": [assertion("D12.membership-removed", '#members-table tr[data-username="member"]')]}, goals[3], *preservation]
                    response.messages[0].contents[0].arguments["goals"] = goals
                else:
                    goals[2]["checks"][0].update(target=None, control="Member", context="Seed Project")
            for goal in goals:
                for check in goal.get("checks", []):
                    check.pop("action_type")
            return response

    for identity in ["ADMIN", "MEMBER"]:
        monkeypatch.setenv("DEMO_" + identity + "_PASSWORD", "demo-" + identity.lower())
    settings = Settings(_env_file=None, langsmith_tracing=False, state_db_path=tmp_path / "state.db", artifacts_dir=tmp_path / "runs", temporary_sensitive_dir=tmp_path / "temporary")
    with DemoAppServer(bugs=SeededBugs(b2_member_deletes_other_project=True, b6_removed_member_session_active=True)) as app:
        config = load_run_config(Path("evaluation/scenarios.json"), scenario_id="D12", target_url=app.base_url)
        report_path = await run(config, settings, run_id="local-contract-replan", main_client=LocalMain(), tester_client_factory=ContractTester, jev_selector=JevSelector(LocalChoice()))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    store = StateStore(settings.state_db_path)
    assert report["test_summary"]["successful_tasks"] == 2, json.dumps(report["task_outcomes"])
    assert sum(outcome["completed_check_count"] for outcome in report["task_outcomes"]) == 7
    assert len(store.list_events("local-contract-replan", event_types=("TESTER_PLAN_VALIDATED",))) == 3
    failures = store.list_events("local-contract-replan", event_types=("TESTER_PLAN_FAILED",))
    assert [event["result"]["reason"] for event in failures] == ["CONTROL_NOT_FOUND"]
    history = store.list_action_history(run_id="local-contract-replan", task_id="offboard-member")
    assert sum(action["browser_session_id"] == history[0]["browser_session_id"] and action["action"] == "click" and 'button:text-is("Remove")' in (action["target"] or "") and 'data-username="member"' in (action["target"] or "") for action in history) == 1
    assert all(finding["status"] == "CONFIRMED_BUG" for finding in store.list_recent_findings("local-contract-replan"))
    comparison, _ = match_ground_truth(Path("evaluation/ground_truth.json"), scenario_id="D12", run_id="local-contract-replan", store=store, artifacts_root=settings.artifacts_dir, test_data=config.test_data)
    metrics = MetricsCalculator(store).calculate("local-contract-replan", ground_truth=comparison)
    assert metrics["false_positive_count"] == 0
    assert metrics["task_success_rate"] == 1
    assert metrics["bug_recall"] == 1


@pytest.mark.asyncio
async def test_live_pipeline_accepts_unique_visible_targets_from_failed_checkpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class CheckpointTester(LocalTester):
        async def _inner_get_response(self, **kwargs: Any) -> ChatResponse:
            response = await super()._inner_get_response(**kwargs)
            goals = response.messages[0].contents[0].arguments["goals"]
            for goal in goals:
                for check in goal.get("checks", []):
                    if check.get("check_id") == "D12.member-ready":
                        check["target"] = "text=Seed Project"
                if goal["goal"] == "Open original Project membership":
                    goal["checks"] = [assertion(None, 'td:text-is("member")', assertion="visible")]
            return response

    for identity in ["ADMIN", "MEMBER"]:
        monkeypatch.setenv("DEMO_" + identity + "_PASSWORD", "demo-" + identity.lower())
    settings = Settings(_env_file=None, langsmith_tracing=False, state_db_path=tmp_path / "state.db", artifacts_dir=tmp_path / "runs", temporary_sensitive_dir=tmp_path / "temporary")
    with DemoAppServer(bugs=SeededBugs(b2_member_deletes_other_project=True, b6_removed_member_session_active=True)) as app:
        config = load_run_config(Path("evaluation/scenarios.json"), scenario_id="D12", target_url=app.base_url)
        report_path = await run(config, settings, run_id="local-checkpoint-targets", main_client=LocalMain(), tester_client_factory=CheckpointTester, jev_selector=JevSelector(LocalChoice()))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    store = StateStore(settings.state_db_path)
    assert report["test_summary"]["successful_tasks"] == 2, json.dumps(report["task_outcomes"])
    assert sum(outcome["completed_check_count"] for outcome in report["task_outcomes"]) == 7
    assert len(store.list_events("local-checkpoint-targets", event_types=("TESTER_PLAN_VALIDATED",))) == 2
    assert all(finding["status"] == "CONFIRMED_BUG" for finding in store.list_recent_findings("local-checkpoint-targets"))
    assert [event["result"]["summary"] for event in store.list_events("local-checkpoint-targets", event_types=("TASK_PROGRESS",))] == ["member-session-ready", "membership-removed", "member-delete-observed"]
    assert not any(event["result"].get("error_type") == "INVALID_ACTION" for event in store.list_events("local-checkpoint-targets", event_types=("BROWSER_ACTION",)))


@pytest.mark.asyncio
@pytest.mark.parametrize("bugs", [False, True])
async def test_complete_live_pipeline_replans_only_real_object_absence_and_preserves_all_checks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bugs: bool) -> None:
    for identity in ["ADMIN", "MEMBER"]:
        monkeypatch.setenv("DEMO_" + identity + "_PASSWORD", "demo-" + identity.lower())
    settings = Settings(_env_file=None, langsmith_tracing=False, state_db_path=tmp_path / "state.db", artifacts_dir=tmp_path / "runs", temporary_sensitive_dir=tmp_path / "temporary")
    with DemoAppServer(bugs=SeededBugs(b2_member_deletes_other_project=bugs, b6_removed_member_session_active=bugs)) as app:
        config = load_run_config(Path("evaluation/scenarios.json"), scenario_id="D12", target_url=app.base_url)
        report_path = await run(config, settings, run_id="local-final-live", main_client=LocalMain(), tester_client_factory=LocalTester, jev_selector=JevSelector(LocalChoice()))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    store = StateStore(settings.state_db_path)
    assert report["test_summary"]["successful_tasks"] == 2, json.dumps(report["task_outcomes"])
    assert sum(outcome["completed_check_count"] for outcome in report["task_outcomes"]) == 7
    findings = store.list_recent_findings("local-final-live")
    assert all(finding["status"] == "CONFIRMED_BUG" for finding in findings)
    assert bool(findings) == bugs
    signals = [event["result"]["summary"] for event in store.list_events("local-final-live", event_types=("TASK_PROGRESS",))]
    assert signals == ["member-session-ready", "membership-removed", "member-delete-observed"]
    plans = store.list_events("local-final-live", event_types=("TESTER_PLAN_VALIDATED",))
    assert len(plans) == (2 if bugs else 3)
    assert len(store.list_events("local-final-live", event_types=("PLAN_AUXILIARY_ASSERTION",))) == 2
    assert all(task["step_budget"] == 90 for task in store.list_tasks("local-final-live"))
    assert all(outcome["completed_check_count"] == outcome["required_check_count"] for outcome in report["task_outcomes"])
    evaluated = MetricsCalculator(store).calculate("local-final-live")
    assert evaluated["task_success_rate"] == 1
    assert not StateStore._task_updates
