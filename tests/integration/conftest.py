from __future__ import annotations

import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from web_testing_system.state import StateStore, build_data_namespace


@pytest.fixture
def phase2_store() -> Iterator[StateStore]:
    temporary_root = Path(".pytest-temp")
    temporary_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=temporary_root) as directory:
        store = StateStore(Path(directory) / "phase2-state.db")
        store.initialize()
        store.create_run(
            run_id="run-1",
            application="http://app.test",
            application_version="test",
            test_goal="Test projects",
            scope={"paths": ["http://app.test/"]},
            status="RUNNING",
            global_budget={"browser_steps": 100},
            remaining_budget={"browser_steps": 100},
        )
        store.create_identity(
            identity_id="identity-1",
            run_id="run-1",
            role="member",
            secret_reference="env:TEST_PASSWORD",
            permissions=["read", "create"],
        )
        store.register_tester(
            tester_id="tester-1",
            run_id="run-1",
            session_reference="maf-session-1",
            identity_id="identity-1",
            role="member",
            data_namespace=build_data_namespace("run-1", "tester-1", "task-1"),
        )
        store.create_task(
            task_id="task-1",
            run_id="run-1",
            goal="Test projects",
            priority="P0",
            dependencies=[],
            created_by="main",
            step_budget=50,
        )
        store.claim_task(task_id="task-1", tester_id="tester-1")
        store.create_budget(budget_id="budget-1", run_id="run-1", task_id="task-1")
        yield store
