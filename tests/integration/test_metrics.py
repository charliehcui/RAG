from __future__ import annotations

from pathlib import Path

import pytest

from web_testing_system.evaluation import GroundTruthComparison, MetricsCalculator
from web_testing_system.reporting import FinalReportBuilder
from web_testing_system.state import StateStore


def create_metrics_store(path: Path) -> StateStore:
    store = StateStore(path)
    store.initialize()
    store.create_run(run_id="run-metrics", application="http://demo.test", application_version="demo-1", test_goal="Measure a fake run", scope={"focus_features": ["Task"]}, status="RUNNING", global_budget={}, remaining_budget={})
    store.create_identity(identity_id="identity-1", run_id="run-metrics", role="member", secret_reference="env:TEST_PASSWORD", permissions=["read", "delete"])
    store.register_tester(tester_id="tester-1", run_id="run-metrics", session_reference="fake-session", identity_id="identity-1", role="member", data_namespace="run-metrics-tester-1")
    for task_id, status in (("task-1", "COMPLETED"), ("task-2", "COMPLETED"), ("task-3", "FAILED")):
        store.create_task(task_id=task_id, run_id="run-metrics", goal=task_id, priority="P1", dependencies=[], created_by="main-agent", step_budget=5, status=status)
    store.create_budget(budget_id="budget-1", run_id="run-metrics", task_id="task-1")
    store.update_budget(budget_id="budget-1", estimated_cost=0.6)

    steps = [{"action_type": "refresh"}]
    for finding_id in ("finding-b1", "finding-b2", "finding-false"):
        store.create_finding(finding_id=finding_id, run_id="run-metrics", task_id="task-1", title=finding_id, status="ANOMALY", expected_result="expected", actual_result="actual", first_seen_by="tester-1", affected_role="member", affected_page="/tasks", reproduction_steps=steps)
    store.record_reproduction_attempt(finding_id="finding-b1", status="REPRODUCED", success=True, stable_steps=steps)
    store.record_reproduction_attempt(finding_id="finding-b1", status="REPRODUCED", success=True, stable_steps=steps)
    store.update_finding_verification(finding_id="finding-b1", status="CONFIRMED_BUG", verification_result="FAIL", details={"result": "reproduced"})
    store.record_reproduction_attempt(finding_id="finding-b2", status="REPRODUCED", success=True, stable_steps=steps)
    store.record_reproduction_attempt(finding_id="finding-b2", status="NOT_REPRODUCED", success=False)
    store.update_finding_verification(finding_id="finding-b2", status="CONFIRMED_BUG", verification_result="FAIL", details={"result": "reproduced once"})
    store.update_finding_verification(finding_id="finding-false", status="CONFIRMED_BUG", verification_result="FAIL", details={"result": "not in ground truth"})
    store.create_finding(finding_id="finding-duplicate", run_id="run-metrics", task_id="task-1", title="duplicate", status="DUPLICATE", expected_result="expected", actual_result="actual", first_seen_by="tester-1", duplicate_of="finding-b1")

    store.record_path(run_id="run-metrics", feature="Task", page="/tasks", page_state_id="list", action="refresh", result="success", last_tester="tester-1")
    store.record_path(run_id="run-metrics", feature="Task", page="/tasks", page_state_id="list", action="refresh", result="success", last_tester="tester-1")
    store.record_path(run_id="run-metrics", feature="Task", page="/tasks", page_state_id="details", action="open", result="success", last_tester="tester-1")

    events = (
        ("action-1", "BROWSER_ACTION", {"success": True}, 4.0),
        ("action-2", "BROWSER_ACTION", {"success": True}, 6.0),
        ("action-3", "BROWSER_ACTION", {"success": False, "error_type": "INVALID_ACTION"}, 5.0),
        ("jev-1", "JEV_CALL", {"selected": "candidate-1"}, 10.0),
        ("jev-2", "JEV_CALL", {"selected": "candidate-2"}, 20.0),
        ("llm-1", "LLM_CALL", {"success": True}, 30.0),
        ("computer-1", "COMPUTER_USE_RESULT", {"status": "RETURN_TO_PLAYWRIGHT"}, 40.0),
        ("computer-2", "COMPUTER_USE_RESULT", {"status": "FAILED"}, 50.0),
    )
    for event_id, event_type, result, latency_ms in events:
        store.append_event(event_id=event_id, run_id="run-metrics", event_type=event_type, action=event_type.casefold(), result=result, latency_ms=latency_ms, task_id="task-1", tester_id="tester-1")
    store.finish_run(run_id="run-metrics", status="COMPLETED")
    return store


@pytest.mark.integration
def test_metrics_are_calculated_from_state_and_post_run_ground_truth(tmp_path: Path) -> None:
    store = create_metrics_store(tmp_path / "metrics.db")
    report = FinalReportBuilder(store).build("run-metrics", full_evaluation=False)
    ground_truth = GroundTruthComparison(enabled_bug_ids=frozenset({"B1", "B2", "B3"}), finding_to_bug={"finding-b1": "B1", "finding-b2": "B2", "finding-false": "NOT_A_SEEDED_BUG"})

    metrics = MetricsCalculator(store).calculate("run-metrics", ground_truth=ground_truth, final_report=report)

    assert metrics["confirmed_bug_recall"] == pytest.approx(2 / 3)
    assert metrics["false_positive_rate"] == pytest.approx(1 / 3)
    assert metrics["reproduction_success_rate"] == 0.75
    assert metrics["duplicate_finding_rate"] == 0.25
    assert metrics["exploration_duplication"] == pytest.approx(1 / 3)
    assert metrics["task_completion_rate"] == pytest.approx(2 / 3)
    assert metrics["browser_action_success_rate"] == pytest.approx(2 / 3)
    assert metrics["average_jev_latency_ms"] == 15.0
    assert metrics["average_llm_latency_ms"] == 30.0
    assert float(metrics["total_test_time_seconds"]) >= 0
    assert metrics["estimated_total_cost"] == 0.6
    assert metrics["cost_per_confirmed_bug"] == 0.2
    assert metrics["computer_use_recovery_rate"] == 0.5
    assert metrics["overall_completion"] == 1.0
    assert metrics["invalid_action_count"] == 1
    assert metrics["test_stability"] == 0.75
    assert metrics["developer_usable_report_rate"] == 1.0


@pytest.mark.integration
def test_metrics_return_na_when_a_rate_has_no_denominator(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "empty-metrics.db")
    store.initialize()
    store.create_run(run_id="empty-run", application="http://demo.test", application_version="demo-1", test_goal="Empty fake run", scope={}, status="RUNNING", global_budget={}, remaining_budget={})
    store.finish_run(run_id="empty-run", status="COMPLETED")

    metrics = MetricsCalculator(store).calculate("empty-run", ground_truth=GroundTruthComparison(enabled_bug_ids=frozenset(), finding_to_bug={}))

    for name in ("confirmed_bug_recall", "false_positive_rate", "reproduction_success_rate", "duplicate_finding_rate", "exploration_duplication", "task_completion_rate", "browser_action_success_rate", "average_jev_latency_ms", "average_llm_latency_ms", "cost_per_confirmed_bug", "computer_use_recovery_rate", "overall_completion", "test_stability", "developer_usable_report_rate"):
        assert metrics[name] == "N/A"
    assert metrics["estimated_total_cost"] == 0
    assert metrics["invalid_action_count"] == 0
