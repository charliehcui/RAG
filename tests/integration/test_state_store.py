from __future__ import annotations

import sqlite3
import tempfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import pytest

from web_testing_system.security import REDACTED
from web_testing_system.state import (
    StateConflictError,
    StateStore,
    build_data_namespace,
)
from web_testing_system.state.store import FINDING_STATUSES, TASK_STATUSES


@pytest.fixture
def state_temp_path() -> Path:
    temporary_root = Path(".pytest-temp")
    temporary_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=temporary_root) as directory:
        yield Path(directory)


def prepared_store(state_temp_path: Path) -> StateStore:
    store = StateStore(state_temp_path / "shared-state.db")
    store.initialize()
    store.create_run(
        run_id="run-1",
        application="https://example.test",
        application_version="UNKNOWN",
        test_goal="Check accounts",
        scope={"paths": ["/accounts"]},
        status="READY",
        global_budget={"browser_steps": 100},
        remaining_budget={"browser_steps": 100},
    )
    store.create_identity(identity_id="identity-1", run_id="run-1", role="member", secret_reference="env:TEST_MEMBER_PASSWORD", permissions=["read", "create"])
    store.register_tester(
        tester_id="tester-1",
        run_id="run-1",
        session_reference="session-1",
        identity_id="identity-1",
        role="member",
        data_namespace=build_data_namespace("run-1", "tester-1", "task-1"),
    )
    return store


@pytest.mark.integration
def test_initialize_uses_wal_and_temporary_database(state_temp_path: Path) -> None:
    database_path = state_temp_path / "state" / "shared-state.db"
    store = StateStore(database_path)

    store.initialize()

    assert database_path.exists()
    with closing(sqlite3.connect(database_path)) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        table_names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"runs", "tasks", "testers", "identities", "resources", "explored_paths", "findings", "evidence", "budgets", "events"} <= table_names


@pytest.mark.integration
def test_atomic_task_claim_and_conditional_status_update(state_temp_path: Path) -> None:
    store = prepared_store(state_temp_path)
    store.register_tester(
        tester_id="tester-2",
        run_id="run-1",
        session_reference="session-2",
        identity_id="identity-1",
        role="member",
        data_namespace=build_data_namespace("run-1", "tester-2", "task-1"),
    )
    store.create_task(task_id="task-1", run_id="run-1", goal="Create account", priority="P0", dependencies=[], created_by="main", step_budget=20)

    def try_claim(tester_id: str) -> str:
        try:
            return store.claim_task(task_id="task-1", tester_id=tester_id)["assigned_tester"]
        except StateConflictError:
            return "CONFLICT"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(try_claim, ("tester-1", "tester-2")))

    assert results.count("CONFLICT") == 1
    assert len({result for result in results if result != "CONFLICT"}) == 1
    completed = store.update_task_status(task_id="task-1", expected_status="RUNNING", new_status="COMPLETED")
    assert completed["status"] == "COMPLETED"
    with pytest.raises(StateConflictError):
        store.update_task_status(task_id="task-1", expected_status="RUNNING", new_status="FAILED")


@pytest.mark.integration
def test_state_entities_and_controlled_tools_round_trip(state_temp_path: Path) -> None:
    store = prepared_store(state_temp_path)
    store.create_task(task_id="task-1", run_id="run-1", goal="Create account", priority="P1", dependencies=[], created_by="main", step_budget=20)

    resource_result = store.create_resource(
        resource_id="resource-1",
        run_id="run-1",
        resource_type="project",
        owner_task="task-1",
        owner="tester-1",
        participants=["tester-1"],
        allowed_operations=["create", "read"],
        sharing_mode="ISOLATED",
        cleanup_status="PENDING",
    )
    first_path = store.record_path(run_id="run-1", feature="accounts", page="/accounts", page_state_id="empty", action="open", result="loaded", last_tester="tester-1")
    second_path = store.record_path(run_id="run-1", feature="accounts", page="/accounts", page_state_id="empty", action="open", result="loaded again", last_tester="tester-1")
    finding = store.create_finding(
        finding_id="finding-1",
        run_id="run-1",
        task_id="task-1",
        title="Example anomaly",
        status="ANOMALY",
        expected_result="Account is visible",
        actual_result="Account is missing",
        first_seen_by="tester-1",
        needs_confirmation=True,
        affected_role="member",
        affected_page="/accounts",
    )
    evidence = store.add_evidence(
        evidence_id="evidence-1",
        run_id="run-1",
        task_id="task-1",
        finding_id="finding-1",
        evidence_type="SCREENSHOT",
        relative_file_path="evidence/account-missing.png",
        url="https://example.test/accounts",
        browser_session_id="browser-1",
    )
    store.create_budget(budget_id="budget-1", run_id="run-1", task_id="task-1")
    budget_result = store.update_budget(budget_id="budget-1", llm_calls=1, input_tokens=120, output_tokens=30, browser_steps=2, runtime_seconds=1.5, estimated_cost=0.01)
    event_result = store.append_event(
        event_id="event-1",
        run_id="run-1",
        task_id="task-1",
        tester_id="tester-1",
        browser_session_id="browser-1",
        event_type="BROWSER_ACTION",
        url="https://example.test/accounts",
        tool="Playwright",
        action="open accounts",
        result={"status": "ok", "api_key": "fake-key-that-must-not-be-stored"},
        evidence_references=["evidence-1"],
        latency_ms=12.5,
        cost=0,
    )

    assert resource_result["owner"] == "tester-1"
    assert resource_result["sharing_mode"] == "ISOLATED"
    assert first_path["visited_count"] == 1
    assert second_path["visited_count"] == 2
    assert finding["status"] == "ANOMALY"
    assert evidence["relative_file_path"] == "evidence/account-missing.png"
    assert budget_result["llm_calls"] == 1
    assert budget_result["input_tokens"] == 120
    assert budget_result["browser_steps"] == 2
    assert budget_result["runtime_seconds"] == 1.5
    assert budget_result["estimated_cost"] == 0.01
    run = store.get_run("run-1")
    assert run is not None and run["remaining_budget"]["browser_steps"] == 98
    assert event_result["url"] == "https://example.test/accounts"
    assert event_result["action"] == "open accounts"
    assert event_result["tool"] == "Playwright"
    assert event_result["result"]["api_key"] == REDACTED
    assert store.list_open_tasks("run-1")[0]["task_id"] == "task-1"
    assert store.list_recent_findings("run-1")[0]["finding_id"] == "finding-1"
    assert store.list_events("run-1")[0]["event_id"] == "event-1"


@pytest.mark.integration
def test_all_declared_task_and_finding_statuses_are_supported(state_temp_path: Path) -> None:
    store = prepared_store(state_temp_path)
    for index, status in enumerate(sorted(TASK_STATUSES)):
        store.create_task(task_id=f"task-{index}", run_id="run-1", goal=status, priority="P2", dependencies=[], created_by="main", step_budget=1, status=status)
    for index, status in enumerate(sorted(FINDING_STATUSES)):
        store.create_finding(
            finding_id=f"finding-{index}",
            run_id="run-1",
            task_id="task-0",
            title=status,
            status=status,
            expected_result="expected",
            actual_result="actual",
            first_seen_by="tester-1",
        )

    assert {store.get_task(f"task-{index}")["status"] for index in range(len(TASK_STATUSES))} == TASK_STATUSES  # type: ignore[index]
    assert {store.get_finding(f"finding-{index}")["status"] for index in range(len(FINDING_STATUSES))} == FINDING_STATUSES  # type: ignore[index]
