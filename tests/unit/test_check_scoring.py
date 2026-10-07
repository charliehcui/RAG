from __future__ import annotations

from pathlib import Path

import pytest

from web_testing_system.config import RunConfig
from web_testing_system.evaluation.metrics import (
    GroundTruthComparison,
    MetricsCalculator,
)
from web_testing_system.reporting import FinalReportBuilder
from web_testing_system.scoring import score_task
from web_testing_system.state import StateStore


def make_store(path: Path, *, dependencies: bool = False, started: bool = True, pending_proof: bool = False) -> StateStore:
    checks = [{"check_id": "case.create", "goal_id": "workflow", "behavior_id": "EB-lifecycle", "identity_reference": "member", "description": "Create"}, {"check_id": "case.persist", "goal_id": "workflow", "behavior_id": "EB-lifecycle", "identity_reference": "member", "description": "Persistence", "depends_on": ["case.create"] if dependencies else []}]
    if pending_proof:
        checks[0]["action_type"] = "repeat_submit"
    store = StateStore(path)
    store.initialize()
    store.create_run(run_id="run", application="http://app.test", application_version="demo-1", test_goal="Check lifecycle", scope={"required_checks": checks}, status="RUNNING", global_budget={}, remaining_budget={})
    store.create_identity(identity_id="identity", run_id="run", role="member", secret_reference=None, permissions=["read"])
    store.register_tester(tester_id="tester", run_id="run", session_reference="session", identity_id="identity", role="member", data_namespace="run-tester")
    store.create_task(task_id="task", run_id="run", goal="Lifecycle", priority="P1", dependencies=[], created_by="main", step_budget=20, data_requirements={"required_checks": checks, "expected_behavior_ids": ["EB-lifecycle"], "identity_reference": "member"})
    if started:
        store.claim_task(task_id="task", tester_id="tester")
    return store


def record(store: StateStore, event_id: str, *, check_id: str | None = None, success: bool = True, action: str = "assertion", target: str = "#item", identity: str = "member", behavior: str = "EB-lifecycle", second: int = 1) -> None:
    timestamp = f"2026-10-07T00:00:{second:02d}+00:00"
    result = {"success": success, "error_type": None if success else "ASSERTION_FAILURE" if action == "assertion" else "INVALID_ACTION"}
    store.append_event(event_id=event_id, run_id="run", task_id="task", browser_session_id="browser", event_type="BROWSER_ACTION", action=action, result=result, latency_ms=0)
    store.append_action_history(history_id="history-" + event_id, event_id=event_id, run_id="run", task_id="task", tester_id="tester", browser_session_id="browser", url="http://app.test", action=action, target=target, tool="Playwright", started_at=timestamp, ended_at=timestamp, latency_ms=0, success=success, error=None, result=result, action_data={"action_type": action, "target": target, "goal_check": action == "assertion", "behavior_id": behavior if action == "assertion" else None, "check_id": check_id, "identity_reference": identity})


def finish(store: StateStore) -> None:
    store.update_task_status(task_id="task", expected_status="RUNNING", new_status="COMPLETED")
    store.finish_run(run_id="run", status="COMPLETED")


def test_same_behavior_does_not_replace_missing_independent_check(tmp_path: Path) -> None:
    store = make_store(tmp_path / "checks.db")
    record(store, "create", check_id="case.create")
    record(store, "duplicate", check_id="case.create", second=2)
    finish(store)
    result = score_task(store, store.get_task("task"))
    assert result["check_completion"] == "1/2 checks completed"
    assert result["success_status"] == "UNKNOWN"
    metrics = MetricsCalculator(store).calculate("run", ground_truth=GroundTruthComparison(frozenset(), {}), expected_behavior_ids=frozenset({"EB-lifecycle"}))
    assert metrics["task_success_rate"] == metrics["e2e_success"] == 0
    assert metrics["check_completion_rate"] == 0.5


@pytest.mark.parametrize("retry", [True, False])
def test_recovered_error_preserves_history_and_final_success(tmp_path: Path, retry: bool) -> None:
    store = make_store(tmp_path / "recovered.db")
    record(store, "bad", success=False, action="click", target="#old-control")
    if retry:
        record(store, "retry", action="click", target="#old-control", second=2)
    record(store, "create", check_id="case.create", second=3)
    record(store, "persist", check_id="case.persist", second=4)
    finish(store)
    result = score_task(store, store.get_task("task"))
    assert result["success_status"] == "PASS"
    assert result["errors"] == [{"event_id": "bad", "error_type": "INVALID_ACTION", "status": "RECOVERED"}]
    assert not store.list_action_history(run_id="run", task_id="task")[0]["success"]
    report = FinalReportBuilder(store).build("run")
    metrics = MetricsCalculator(store).calculate("run", ground_truth=GroundTruthComparison(frozenset(), {}), expected_behavior_ids=frozenset({"EB-lifecycle"}))
    assert report["test_summary"]["successful_tasks"] == 1
    assert metrics["task_success_rate"] == metrics["e2e_success"] == 1
    assert report["task_outcomes"][0] == MetricsCalculator(store).task_results("run")[0]


def test_unrelated_action_does_not_recover_blocker_with_incomplete_goal(tmp_path: Path) -> None:
    store = make_store(tmp_path / "blocking.db")
    record(store, "bad", success=False, action="click", target="#missing")
    record(store, "other", action="click", target="#unrelated", second=2)
    record(store, "create", check_id="case.create", second=3)
    finish(store)
    result = score_task(store, store.get_task("task"))
    assert result["success_status"] == "FAIL"
    assert result["errors"][0]["status"] == "UNRESOLVED_BLOCKING"


@pytest.mark.parametrize("identity,behavior", [("other-member", "EB-lifecycle"), ("member", "EB-other")])
def test_check_must_match_assigned_identity_and_behavior(tmp_path: Path, identity: str, behavior: str) -> None:
    store = make_store(tmp_path / "identity.db")
    record(store, "create", check_id="case.create", identity=identity, behavior=behavior)
    finish(store)
    assert score_task(store, store.get_task("task"))["completed_check_count"] == 0


def test_required_checkpoint_order_is_enforced(tmp_path: Path) -> None:
    store = make_store(tmp_path / "order.db", dependencies=True)
    record(store, "persist", check_id="case.persist")
    record(store, "create", check_id="case.create", second=2)
    finish(store)
    result = score_task(store, store.get_task("task"))
    assert result["completed_check_count"] == 1
    assert result["success_status"] == "UNKNOWN"


def test_pending_operation_proof_cannot_be_replaced_by_url_or_dom_assertion(tmp_path: Path) -> None:
    store = make_store(tmp_path / "pending-proof.db", pending_proof=True)
    record(store, "not-operation-proof", check_id="case.create")
    finish(store)
    assert score_task(store, store.get_task("task"))["completed_check_count"] == 0


def test_post_run_answers_keep_omitted_checks_in_denominator(tmp_path: Path) -> None:
    store = make_store(tmp_path / "omitted.db")
    record(store, "create", check_id="case.create")
    record(store, "persist", check_id="case.persist", second=2)
    finish(store)
    comparison = GroundTruthComparison(frozenset(), {}, frozenset({"case.create", "case.persist", "case.omitted"}))
    calculator = MetricsCalculator(store)
    metrics = calculator.calculate("run", ground_truth=comparison, expected_behavior_ids=frozenset({"EB-lifecycle"}))
    report = FinalReportBuilder(store).build("run", required_check_ids=comparison.required_check_ids)
    assert metrics["check_completion"] == report["test_summary"]["check_completion"] == "2/3 checks completed"
    assert metrics["e2e_success"] == 0


@pytest.mark.parametrize("status,outcome", [("STOPPED", "INTERRUPTED"), ("FAILED", "AGENT_EXECUTION_FAILURE"), ("PENDING", "NOT_STARTED")])
def test_interrupted_unstarted_failed_are_distinct(tmp_path: Path, status: str, outcome: str) -> None:
    store = make_store(tmp_path / "status.db", started=status != "PENDING")
    if status != "PENDING":
        store.update_task_status(task_id="task", expected_status="RUNNING", new_status=status)
    assert score_task(store, store.get_task("task"))["outcome"] == outcome


@pytest.mark.parametrize("confirmed", [False, True])
def test_application_failure_needs_exact_check_confirmation(tmp_path: Path, confirmed: bool) -> None:
    store = make_store(tmp_path / "bug.db")
    record(store, "create", check_id="case.create", success=False)
    record(store, "persist", check_id="case.persist", second=2)
    store.create_finding(finding_id="finding", run_id="run", task_id="task", title="Actual defect", status="ANOMALY", expected_result="created item", actual_result="item absent", first_seen_by="tester")
    store.append_event(event_id="finding-event", run_id="run", task_id="task", event_type="FINDING_CREATED", action="record_finding", result={"finding_id": "finding", "check_id": "case.create", "boundary_event_id": "create"}, latency_ms=0)
    if confirmed:
        store.update_finding_verification(finding_id="finding", status="CONFIRMED_BUG", verification_result="FAIL", details={})
    finish(store)
    result = score_task(store, store.get_task("task"), correct_finding_ids={"finding"} if confirmed else set())
    assert result["completed_check_count"] == 2
    assert result["success_status"] == ("PASS" if confirmed else "UNKNOWN")


def test_scenario_and_answers_have_same_explicit_check_ids() -> None:
    import json

    root = Path(__file__).resolve().parents[2]
    dataset = json.loads((root / "evaluation/scenarios.json").read_text(encoding="utf-8"))
    answers = json.loads((root / "evaluation/ground_truth.json").read_text(encoding="utf-8"))
    development = [case for case in dataset["scenarios"] if case["split"] == "development"]
    assert [case["scenario_id"] for case in development] == ["D01", "D02", "D03", "D04", "D05", "D06", "D07", "D08", "D12", "D13"]
    assert dataset["evaluation_version"] == answers["evaluation_version"]
    assert {case["scenario_id"] for case in dataset["scenarios"]} == {case["scenario_id"] for case in answers["scenarios"]}
    assert {bug for case in development for bug in case["enabled_seeded_bugs"]} == {"B1", "B2", "B3", "B4", "B5", "B6"}
    from web_testing_system.evaluation.scenarios import load_run_config

    for case in development:
        answer = next(item for item in answers["scenarios"] if item["scenario_id"] == case["scenario_id"])
        assert answer["required_check_ids"] == [check["check_id"] for check in case["required_checks"]]
        config = load_run_config(root / "evaluation/scenarios.json", scenario_id=case["scenario_id"], target_url="http://127.0.0.1:8000")
        assert isinstance(config, RunConfig)
        assert len(config.required_checks) == len(answer["required_check_ids"])
        assert set(answer["required_goal_ids"]) == {check.goal_id for check in config.required_checks}
