from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from web_testing_system.evaluation.metrics import (
    GroundTruthComparison,
    MetricsCalculator,
)
from web_testing_system.evaluation.scenarios import load_run_config
from web_testing_system.reporting import FinalReportBuilder
from web_testing_system.scoring import score_task
from web_testing_system.state import StateStore

ROOT = Path(__file__).resolve().parents[2]
SCENARIOS = ROOT / "evaluation/scenarios.json"
ANSWERS = ROOT / "evaluation/ground_truth.json"
HOLDOUT_IDS = [f"H{number:02d}" for number in range(1, 9)]


def test_holdout_finalization_preserves_frozen_development_records() -> None:
    expected = {"scenarios.json": "59136d9e20f4f0bd45e1578be107592744ad7e0c83e3661961e6769cd70e0362", "ground_truth.json": "7bb560eeb0322b99d501095033ba9d834499d5198dae7443a8626e15bb6d6c03"}
    for name, digest in expected.items():
        data = json.loads((ROOT / "evaluation" / name).read_text(encoding="utf-8"))
        records = [case for case in data["scenarios"] if case["scenario_id"].startswith("D")]
        assert hashlib.sha256(json.dumps(records, sort_keys=True, separators=(",", ":")).encode()).hexdigest() == digest
        assert data["evaluation_version"] == "development-v2-checks-20261008"
        assert data["holdout_evaluation_version"] == "holdout-v2-checks-20261008"


@pytest.mark.parametrize("case_id", HOLDOUT_IDS)
def test_holdout_has_exact_scoped_checks_inputs_and_ground_truth(case_id: str) -> None:
    dataset = json.loads(SCENARIOS.read_text(encoding="utf-8"))
    answers = json.loads(ANSWERS.read_text(encoding="utf-8"))
    case = next(case for case in dataset["scenarios"] if case["scenario_id"] == case_id)
    truth = next(case for case in answers["scenarios"] if case["scenario_id"] == case_id)
    config = load_run_config(SCENARIOS, scenario_id=case_id, target_url="http://app.test/")
    checks = case["required_checks"]
    assert len(checks) >= 3 and len({item["check_id"] for item in checks}) == len(checks)
    assert truth["required_checks"] == checks
    assert truth["required_check_ids"] == [item["check_id"] for item in checks]
    assert set(truth["enabled_bug_ids"]) == set(case["enabled_seeded_bugs"])
    assert truth["expected_bug_count"] == len(truth["enabled_bug_ids"])
    assert truth["required_goal_ids"] == [goal["goal_id"] for goal in case["required_test_goals"]]
    assert len(config.required_checks) == len(checks)
    identities = {account["identity_reference"] for account in case["allowed_roles"]}
    for goal in case["required_test_goals"]:
        assigned = [check for check in checks if check["goal_id"] == goal["goal_id"]]
        assert goal["identity_reference"] in identities
        assert set(goal["test_data_keys"]) <= set(case["required_test_data"])
        assert goal["check_ids"] == [check["check_id"] for check in assigned]
        assert {check["behavior_id"] for check in assigned} == set(goal["expected_behavior_ids"])
        assert {check["identity_reference"] for check in assigned} == {goal["identity_reference"]}
        assert "Reference behavior descriptions do not add unlisted tests or inputs" in goal["description"]
    for bug in truth["bugs"]:
        assert bug["preconditions"]["acting_identity_reference"] == bug["identity_reference"]
        assert bug["preconditions"]["project_reference"] == bug["project_reference"]
        assert bug["preconditions"]["required_check_ids"]
    assert truth["stable_reproduction"] == {"matching_attempts": 2, "max_attempts": 3, "reset_before_each_attempt": True, "fresh_verification_required": True}


@pytest.mark.parametrize("case_id", HOLDOUT_IDS)
def test_one_holdout_check_never_completes_a_complex_behavior(tmp_path: Path, case_id: str) -> None:
    config = load_run_config(SCENARIOS, scenario_id=case_id, target_url="http://app.test/")
    checks = [check.model_dump(mode="json") for check in config.required_checks]
    first = checks[0]
    assigned = [check for check in checks if check["goal_id"] == first["goal_id"]]
    store = StateStore(tmp_path / "checks.db")
    store.initialize()
    store.create_run(run_id="run", application="http://app.test", application_version=config.application_version, test_goal=config.test_goal, scope={"required_checks": checks}, status="RUNNING", global_budget={}, remaining_budget={})
    store.create_identity(identity_id="identity", run_id="run", role="member", secret_reference=None, permissions=["read"])
    store.register_tester(tester_id="tester", run_id="run", session_reference="session", identity_id="identity", role="member", data_namespace="holdout-test")
    store.create_task(task_id="task", run_id="run", goal="One assigned workflow", priority="P1", dependencies=[], created_by="main", step_budget=20, data_requirements={"required_checks": assigned, "expected_behavior_ids": list({check["behavior_id"] for check in assigned})})
    store.claim_task(task_id="task", tester_id="tester")
    for number in (1, 2):
        event_id = f"check-{number}"
        timestamp = f"2026-10-08T00:00:0{number}+00:00"
        action = first["action_type"]
        result = {"success": True, "error_type": None, "data": {"requests": [{"submission_id": "same-operation"}, {"submission_id": "same-operation"}]}}
        store.append_event(event_id=event_id, run_id="run", task_id="task", browser_session_id="browser", event_type="BROWSER_ACTION", action=action, result=result, latency_ms=0)
        store.append_action_history(history_id=event_id, event_id=event_id, run_id="run", task_id="task", tester_id="tester", browser_session_id="browser", url="http://app.test/", action=action, target="#observed", tool="Playwright", started_at=timestamp, ended_at=timestamp, latency_ms=0, success=True, error=None, result=result, action_data={"action_type": action, "check_id": first["check_id"], "behavior_id": first["behavior_id"], "identity_reference": first["identity_reference"], "goal_check": True})
    outcome = score_task(store, store.get_task("task"), live=True)
    assert outcome["completed_check_count"] == 1
    assert outcome["required_check_count"] == len(assigned)
    assert not outcome["ready_to_finish"] and outcome["success_status"] != "PASS"
    comparison = GroundTruthComparison(frozenset(), {}, frozenset(check["check_id"] for check in checks))
    metrics = MetricsCalculator(store).calculate("run", ground_truth=comparison, expected_behavior_ids=frozenset())
    assert metrics["completed_check_count"] == 1 and metrics["required_check_count"] == len(checks)
    assert metrics["task_success_rate"] == metrics["e2e_success"] == 0
    assert FinalReportBuilder(store).build("run")["task_outcomes"] == MetricsCalculator(store).task_results("run")


def test_holdout_keeps_required_session_and_pending_operation_dependencies() -> None:
    dataset = json.loads(SCENARIOS.read_text(encoding="utf-8"))
    holdout = {case["scenario_id"]: case for case in dataset["scenarios"] if case["split"] == "holdout"}
    assert list(holdout) == HOLDOUT_IDS
    assert sum(len(case["required_checks"]) for case in holdout.values()) == 66
    for case_id in ("H04", "H06"):
        checks = {check["check_id"]: check for check in holdout[case_id]["required_checks"]}
        assert checks[f"{case_id}.member-removed"]["depends_on"] == [f"{case_id}.member-ready"]
        assert all(f"{case_id}.removal-persisted" in check["depends_on"] for check in checks.values() if ".revoked-" in check["check_id"])
    for case_id in ("H06", "H08"):
        proof = next(check for check in holdout[case_id]["required_checks"] if check["check_id"].endswith(".pending-operation"))
        assert proof["action_type"] == "repeat_submit"
    assert {goal["goal_id"] for goal in holdout["H07"]["required_test_goals"]} == {"private-task", "joined-deletion"}
    assert holdout["H07"]["enabled_seeded_bugs"] == ["B1", "B2"]
    assert all("membership-workflow" not in goal["description"] for goal in holdout["H07"]["required_test_goals"])
