from __future__ import annotations

import pytest

from web_testing_system.findings import FindingService, ScreeningSignals
from web_testing_system.state import StateStore


def create_finding(store: StateStore, finding_id: str, *, expected: str = "Project is deleted", page: str = "/projects", action: str = "delete", error_text: str = "Project remained after refresh") -> dict[str, object]:
    return store.create_finding(finding_id=finding_id, run_id="run-1", task_id="task-1", title="Project deletion anomaly", status="OBSERVATION", expected_result=expected, actual_result="Project is still visible", first_seen_by="tester-1", severity_hint="CRITICAL", needs_confirmation=True, affected_role="member", affected_page=page, action=action, error_text=error_text, reproduction_steps=[{"action_type": "refresh", "target": None}])


@pytest.mark.integration
def test_independent_tester_findings_do_not_cross_deduplicate(phase2_store: StateStore) -> None:
    create_finding(phase2_store, "finding-a")
    phase2_store.register_tester(tester_id="tester-2", run_id="run-1", session_reference="session-2", identity_id="identity-1", role="member", data_namespace="tester-2-data")
    phase2_store.create_task(task_id="task-2", run_id="run-1", goal="Check Project deletion", priority="P1", dependencies=[], created_by="main", step_budget=10)
    phase2_store.create_finding(finding_id="finding-b", run_id="run-1", task_id="task-2", title="Project deletion anomaly", status="OBSERVATION", expected_result="Project is deleted", actual_result="Project is still visible", first_seen_by="tester-2", affected_page="/projects", action="delete", error_text="Project remained after refresh")

    assert FindingService(phase2_store, "run-1").find_duplicate("finding-b") is not None
    assert FindingService(phase2_store, "run-1", shared_state=False).find_duplicate("finding-b") is None


@pytest.mark.integration
def test_finding_screening_duplicate_rules_and_state_transitions(phase2_store: StateStore) -> None:
    service = FindingService(phase2_store, "run-1")
    original = create_finding(phase2_store, "finding-original")
    phase2_store.add_evidence(evidence_id="evidence-original", run_id="run-1", task_id="task-1", finding_id="finding-original", evidence_type="DOM", attempt_id="observation", relative_file_path="run-1/evidence/finding-original/observation/dom.html", url="http://app.test/projects", browser_session_id="browser-1")

    screened = service.screen("finding-original", ScreeningSignals(assertion_failed=True))

    assert original["severity_hint"] == "CRITICAL"
    assert screened["status"] == "ANOMALY"
    assert screened["screening_reason"] == "ASSERTION_FAILURE"
    assert screened["status"] != "CONFIRMED_BUG"

    create_finding(phase2_store, "finding-duplicate")
    phase2_store.add_evidence(evidence_id="evidence-duplicate", run_id="run-1", task_id="task-1", finding_id="finding-duplicate", evidence_type="SCREENSHOT", attempt_id="observation", relative_file_path="run-1/evidence/finding-duplicate/observation/page.png", url="http://app.test/projects", browser_session_id="browser-1")
    duplicate = service.screen("finding-duplicate", ScreeningSignals(assertion_failed=True))

    assert duplicate["status"] == "DUPLICATE"
    assert duplicate["duplicate_of"] == "finding-original"
    assert {item["evidence_id"] for item in phase2_store.list_evidence(run_id="run-1")} == {"evidence-original", "evidence-duplicate"}

    environment = create_finding(phase2_store, "finding-network", page="/members", action="load", error_text="503")
    assert environment["status"] == "OBSERVATION"
    assert service.screen("finding-network", ScreeningSignals(network_failure=True))["status"] == "ENVIRONMENT_ISSUE"

    wrong_target = create_finding(phase2_store, "finding-wrong-target", page="/tasks", action="click", error_text="wrong row")
    assert wrong_target["status"] == "OBSERVATION"
    assert service.screen("finding-wrong-target", ScreeningSignals(wrong_target=True))["status"] == "TESTER_ERROR"

    no_expected = create_finding(phase2_store, "finding-no-expected", expected="", page="/permissions", action="delete", error_text="unexpected access")
    assert no_expected["status"] == "OBSERVATION"
    phase2_store.update_finding_status(finding_id="finding-no-expected", status="ANOMALY")
    service.start_reproduction("finding-no-expected")
    reproduced_without_expected = service.finish_reproduction("finding-no-expected", reproduced=True, stable=True, stable_steps=[{"action_type": "click"}])
    assert reproduced_without_expected["status"] == "NEEDS_CONFIRMATION"

    with pytest.raises(ValueError, match="severity"):
        phase2_store.create_finding(finding_id="finding-bad-severity", run_id="run-1", task_id="task-1", title="bad", status="OBSERVATION", expected_result="expected", actual_result="actual", first_seen_by="tester-1", severity_hint="URGENT")
