"""A small, explicit SQLite store for shared test facts."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from web_testing_system.security import redact_sensitive_data

TASK_STATUSES = {
    "PENDING",
    "RUNNING",
    "BLOCKED",
    "WAITING_FOR_DATA",
    "COMPLETED",
    "FAILED",
    "STOPPED",
    "CANCELLED",
}
FINDING_STATUSES = {
    "OBSERVATION",
    "ANOMALY",
    "SUSPECTED_ISSUE",
    "REPRODUCING",
    "REPRODUCED",
    "NOT_REPRODUCED",
    "CONFIRMED_BUG",
    "NEEDS_CONFIRMATION",
    "ENVIRONMENT_ISSUE",
    "TESTER_ERROR",
    "DUPLICATE",
    "CLOSED",
}
TASK_PRIORITIES = {"P0", "P1", "P2", "P3"}
SEVERITY_HINTS = {"LOW", "MEDIUM", "HIGH", "CRITICAL"}
VERIFICATION_RESULTS = {"PASS", "FAIL", "NEEDS_CONFIRMATION"}


class StateConflictError(RuntimeError):
    """Raised when a conditional state update would overwrite another committed fact."""


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def encode_json(value: Any) -> str:
    return json.dumps(redact_sensitive_data(value), ensure_ascii=False, separators=(",", ":"))


def decode_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    result: dict[str, Any] = {}
    for key in row.keys():
        value = row[key]
        if key.endswith("_json"):
            result[key.removesuffix("_json")] = json.loads(value) if value is not None else None
        else:
            result[key] = value
    return result


class StateStore:
    """Expose only the state operations needed by the testing workflow."""

    def __init__(self, database_path: Path, busy_timeout_ms: int = 5_000) -> None:
        if busy_timeout_ms <= 0:
            raise ValueError("busy_timeout_ms must be positive")
        self.database_path = database_path
        self.busy_timeout_ms = busy_timeout_ms

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=self.busy_timeout_ms / 1_000)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    application TEXT NOT NULL,
                    application_version TEXT NOT NULL,
                    test_goal TEXT NOT NULL,
                    scope_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    global_budget_json TEXT NOT NULL,
                    remaining_budget_json TEXT NOT NULL,
                    stop_reason TEXT
                );

                CREATE TABLE IF NOT EXISTS identities (
                    identity_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    role TEXT NOT NULL,
                    secret_reference TEXT,
                    permissions_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS testers (
                    tester_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    session_reference TEXT NOT NULL,
                    identity_id TEXT NOT NULL REFERENCES identities(identity_id),
                    role TEXT NOT NULL,
                    data_namespace TEXT NOT NULL,
                    status TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    goal TEXT NOT NULL,
                    priority TEXT NOT NULL CHECK (priority IN ('P0', 'P1', 'P2', 'P3')),
                    assigned_tester TEXT REFERENCES testers(tester_id),
                    status TEXT NOT NULL CHECK (status IN ('PENDING', 'RUNNING', 'BLOCKED', 'WAITING_FOR_DATA', 'COMPLETED', 'FAILED', 'STOPPED', 'CANCELLED')),
                    dependencies_json TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    parent_finding TEXT,
                    step_budget INTEGER NOT NULL CHECK (step_budget >= 0),
                    data_requirements_json TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT
                );

                CREATE TABLE IF NOT EXISTS resources (
                    resource_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    resource_type TEXT NOT NULL,
                    owner_task TEXT NOT NULL REFERENCES tasks(task_id),
                    owner TEXT NOT NULL,
                    participants_json TEXT NOT NULL,
                    allowed_operations_json TEXT NOT NULL,
                    sharing_mode TEXT NOT NULL CHECK (sharing_mode IN ('SHARED', 'ISOLATED')),
                    cleanup_status TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS explored_paths (
                    path_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    feature TEXT NOT NULL,
                    page TEXT NOT NULL,
                    state TEXT NOT NULL,
                    action TEXT NOT NULL,
                    result TEXT NOT NULL,
                    visited_count INTEGER NOT NULL CHECK (visited_count > 0),
                    last_tester TEXT NOT NULL REFERENCES testers(tester_id),
                    UNIQUE (run_id, feature, page, state, action)
                );

                CREATE TABLE IF NOT EXISTS findings (
                    finding_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    title TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('OBSERVATION', 'ANOMALY', 'SUSPECTED_ISSUE', 'REPRODUCING', 'REPRODUCED', 'NOT_REPRODUCED', 'CONFIRMED_BUG', 'NEEDS_CONFIRMATION', 'ENVIRONMENT_ISSUE', 'TESTER_ERROR', 'DUPLICATE', 'CLOSED')),
                    expected_result TEXT NOT NULL,
                    actual_result TEXT NOT NULL,
                    first_seen_by TEXT NOT NULL REFERENCES testers(tester_id),
                    reproduction_count INTEGER NOT NULL DEFAULT 0,
                    reproduction_success_count INTEGER NOT NULL DEFAULT 0,
                    severity_hint TEXT,
                    needs_confirmation INTEGER NOT NULL DEFAULT 0,
                    duplicate_of TEXT REFERENCES findings(finding_id),
                    affected_role TEXT,
                    affected_page TEXT,
                    action TEXT,
                    error_text TEXT,
                    screening_reason TEXT,
                    reproduction_steps_json TEXT NOT NULL,
                    reproduction_rate REAL NOT NULL DEFAULT 0,
                    verification_result TEXT CHECK (verification_result IN ('PASS', 'FAIL', 'NEEDS_CONFIRMATION')),
                    verification_details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS evidence (
                    evidence_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    finding_id TEXT REFERENCES findings(finding_id),
                    evidence_type TEXT NOT NULL,
                    attempt_id TEXT,
                    relative_file_path TEXT NOT NULL,
                    url TEXT,
                    timestamp TEXT NOT NULL,
                    browser_session_id TEXT
                );

                CREATE TABLE IF NOT EXISTS budgets (
                    budget_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    task_id TEXT REFERENCES tasks(task_id),
                    llm_calls INTEGER NOT NULL DEFAULT 0,
                    jev_calls INTEGER NOT NULL DEFAULT 0,
                    computer_use_calls INTEGER NOT NULL DEFAULT 0,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    browser_steps INTEGER NOT NULL DEFAULT 0,
                    runtime_seconds REAL NOT NULL DEFAULT 0,
                    estimated_cost REAL NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    timestamp TEXT NOT NULL,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    task_id TEXT REFERENCES tasks(task_id),
                    tester_id TEXT REFERENCES testers(tester_id),
                    browser_session_id TEXT,
                    event_type TEXT NOT NULL,
                    url TEXT,
                    tool TEXT,
                    action TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    evidence_references_json TEXT NOT NULL,
                    latency_ms REAL NOT NULL CHECK (latency_ms >= 0),
                    cost REAL NOT NULL CHECK (cost >= 0)
                );

                CREATE TABLE IF NOT EXISTS action_history (
                    history_id TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL UNIQUE REFERENCES events(event_id),
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    tester_id TEXT NOT NULL REFERENCES testers(tester_id),
                    browser_session_id TEXT NOT NULL,
                    url TEXT,
                    action TEXT NOT NULL,
                    target TEXT,
                    tool TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    ended_at TEXT NOT NULL,
                    latency_ms REAL NOT NULL CHECK (latency_ms >= 0),
                    success INTEGER NOT NULL,
                    error TEXT,
                    action_data_json TEXT NOT NULL,
                    result_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_tasks_run_status ON tasks(run_id, status, priority);
                CREATE INDEX IF NOT EXISTS idx_findings_run_status ON findings(run_id, status);
                CREATE INDEX IF NOT EXISTS idx_events_run_timestamp ON events(run_id, timestamp);
                CREATE INDEX IF NOT EXISTS idx_paths_run_feature ON explored_paths(run_id, feature);
                CREATE INDEX IF NOT EXISTS idx_action_history_task ON action_history(run_id, task_id, started_at);
                """
            )
            task_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(tasks)")
            }
            if "data_requirements_json" not in task_columns:
                connection.execute(
                    "ALTER TABLE tasks ADD COLUMN data_requirements_json TEXT NOT NULL DEFAULT '{}'"
                )
            finding_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(findings)")
            }
            finding_column_definitions = {
                "action": "TEXT",
                "error_text": "TEXT",
                "screening_reason": "TEXT",
                "reproduction_steps_json": "TEXT NOT NULL DEFAULT '[]'",
                "reproduction_rate": "REAL NOT NULL DEFAULT 0",
                "verification_result": "TEXT",
                "verification_details_json": "TEXT NOT NULL DEFAULT '{}'",
            }
            for column, definition in finding_column_definitions.items():
                if column not in finding_columns:
                    connection.execute(
                        f"ALTER TABLE findings ADD COLUMN {column} {definition}"
                    )
            evidence_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(evidence)")
            }
            if "attempt_id" not in evidence_columns:
                connection.execute("ALTER TABLE evidence ADD COLUMN attempt_id TEXT")
            history_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(action_history)")
            }
            if "action_data_json" not in history_columns:
                connection.execute(
                    "ALTER TABLE action_history ADD COLUMN action_data_json TEXT NOT NULL DEFAULT '{}'"
                )

    def create_run(self, *, run_id: str, application: str, application_version: str, test_goal: str, scope: Mapping[str, Any], status: str, global_budget: Mapping[str, Any], remaining_budget: Mapping[str, Any]) -> dict[str, Any]:
        started_at = utc_now()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO runs (run_id, application, application_version, test_goal, scope_json, status, started_at, global_budget_json, remaining_budget_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, application, application_version, test_goal, encode_json(scope), status, started_at, encode_json(global_budget), encode_json(remaining_budget)),
            )
        result = self.get_run(run_id)
        assert result is not None
        return result

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            return decode_row(connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone())

    def finish_run(self, *, run_id: str, status: str, stop_reason: str | None = None) -> dict[str, Any]:
        if status not in {"COMPLETED", "FAILED", "STOPPED", "CANCELLED"}:
            raise ValueError("Run must finish with a final status")
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE runs SET status = ?, finished_at = ?, stop_reason = ? WHERE run_id = ? AND finished_at IS NULL",
                (status, utc_now(), stop_reason, run_id),
            )
            if cursor.rowcount != 1:
                raise StateConflictError(f"run {run_id!r} is missing or already finished")
        result = self.get_run(run_id)
        assert result is not None
        return result

    def create_identity(self, *, identity_id: str, run_id: str, role: str, secret_reference: str | None, permissions: Sequence[str]) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO identities (identity_id, run_id, role, secret_reference, permissions_json) VALUES (?, ?, ?, ?, ?)",
                (identity_id, run_id, role, secret_reference, encode_json(permissions)),
            )
        result = self.get_identity(identity_id)
        assert result is not None
        return result

    def get_identity(self, identity_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            return decode_row(connection.execute("SELECT * FROM identities WHERE identity_id = ?", (identity_id,)).fetchone())

    def register_tester(self, *, tester_id: str, run_id: str, session_reference: str, identity_id: str, role: str, data_namespace: str, status: str = "READY") -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO testers (tester_id, run_id, session_reference, identity_id, role, data_namespace, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (tester_id, run_id, session_reference, identity_id, role, data_namespace, status),
            )
        result = self.get_tester(tester_id)
        assert result is not None
        return result

    def get_tester(self, tester_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            return decode_row(connection.execute("SELECT * FROM testers WHERE tester_id = ?", (tester_id,)).fetchone())

    def list_testers(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM testers WHERE run_id = ? ORDER BY tester_id", (run_id,)
            ).fetchall()
        return [decoded for row in rows if (decoded := decode_row(row)) is not None]

    def update_tester_status(self, *, tester_id: str, expected_status: str, new_status: str) -> dict[str, Any]:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE testers SET status = ? WHERE tester_id = ? AND status = ?",
                (new_status, tester_id, expected_status),
            )
            if cursor.rowcount != 1:
                raise StateConflictError(
                    f"tester {tester_id!r} was not in expected status {expected_status!r}"
                )
        result = self.get_tester(tester_id)
        assert result is not None
        return result

    def create_task(self, *, task_id: str, run_id: str, goal: str, priority: str, dependencies: Sequence[str], created_by: str, step_budget: int, parent_finding: str | None = None, status: str = "PENDING", data_requirements: Mapping[str, Any] | None = None) -> dict[str, Any]:
        self._validate_task_status(status)
        if priority not in TASK_PRIORITIES:
            raise ValueError(f"unsupported task priority: {priority}")
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO tasks (task_id, run_id, goal, priority, status, dependencies_json, created_by, parent_finding, step_budget, data_requirements_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (task_id, run_id, goal, priority, status, encode_json(dependencies), created_by, parent_finding, step_budget, encode_json(data_requirements or {})),
            )
        result = self.get_task(task_id)
        assert result is not None
        return result

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            return decode_row(connection.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone())

    def list_tasks(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM tasks WHERE run_id = ? ORDER BY priority, task_id",
                (run_id,),
            ).fetchall()
        return [decoded for row in rows if (decoded := decode_row(row)) is not None]

    def list_open_tasks(self, run_id: str) -> list[dict[str, Any]]:
        open_statuses = ("PENDING", "RUNNING", "BLOCKED", "WAITING_FOR_DATA")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM tasks WHERE run_id = ? AND status IN (?, ?, ?, ?) ORDER BY priority, task_id",
                (run_id, *open_statuses),
            ).fetchall()
        return [decoded for row in rows if (decoded := decode_row(row)) is not None]

    def claim_task(self, *, task_id: str, tester_id: str) -> dict[str, Any]:
        started_at = utc_now()
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE tasks SET assigned_tester = ?, status = 'RUNNING', started_at = ? WHERE task_id = ? AND status = 'PENDING' AND (assigned_tester IS NULL OR assigned_tester = ?) AND NOT EXISTS (SELECT 1 FROM tasks AS active WHERE active.assigned_tester = ? AND active.status = 'RUNNING')",
                (tester_id, started_at, task_id, tester_id, tester_id),
            )
            if cursor.rowcount != 1:
                raise StateConflictError(f"task {task_id!r} is not available for claim")
        result = self.get_task(task_id)
        assert result is not None
        return result

    def update_task_plan(self, *, task_id: str, expected_status: str, goal: str, priority: str, dependencies: Sequence[str], parent_finding: str | None, data_requirements: Mapping[str, Any]) -> dict[str, Any]:
        self._validate_task_status(expected_status)
        if priority not in TASK_PRIORITIES:
            raise ValueError(f"unsupported task priority: {priority}")
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE tasks SET goal = ?, priority = ?, dependencies_json = ?, parent_finding = ?, data_requirements_json = ? WHERE task_id = ? AND status = ?",
                (goal, priority, encode_json(dependencies), parent_finding, encode_json(data_requirements), task_id, expected_status),
            )
            if cursor.rowcount != 1:
                raise StateConflictError(
                    f"task {task_id!r} was not in expected status {expected_status!r}"
                )
        result = self.get_task(task_id)
        assert result is not None
        return result

    def reassign_task(self, *, task_id: str, expected_tester: str, new_tester: str) -> dict[str, Any]:
        with self._connect() as connection:
            tester = connection.execute(
                "SELECT run_id FROM testers WHERE tester_id = ?", (new_tester,)
            ).fetchone()
            if tester is None:
                raise KeyError(f"unknown tester: {new_tester}")
            cursor = connection.execute(
                "UPDATE tasks SET assigned_tester = ?, status = 'PENDING' WHERE task_id = ? AND assigned_tester = ? AND run_id = ? AND status IN ('BLOCKED', 'WAITING_FOR_DATA')",
                (new_tester, task_id, expected_tester, tester["run_id"]),
            )
            if cursor.rowcount != 1:
                raise StateConflictError(
                    f"task {task_id!r} is not assigned to {expected_tester!r}"
                )
        result = self.get_task(task_id)
        assert result is not None
        return result

    def update_task_status(self, *, task_id: str, expected_status: str, new_status: str) -> dict[str, Any]:
        self._validate_task_status(expected_status)
        self._validate_task_status(new_status)
        finished_at = utc_now() if new_status in {"COMPLETED", "FAILED", "STOPPED", "CANCELLED"} else None
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE tasks SET status = ?, finished_at = COALESCE(?, finished_at) WHERE task_id = ? AND status = ?",
                (new_status, finished_at, task_id, expected_status),
            )
            if cursor.rowcount != 1:
                raise StateConflictError(f"task {task_id!r} was not in expected status {expected_status!r}")
        result = self.get_task(task_id)
        assert result is not None
        return result

    def create_resource(self, *, resource_id: str, run_id: str, resource_type: str, owner_task: str, owner: str, participants: Sequence[str], allowed_operations: Sequence[str], sharing_mode: str, cleanup_status: str) -> dict[str, Any]:
        if sharing_mode not in {"SHARED", "ISOLATED"}:
            raise ValueError("sharing_mode must be SHARED or ISOLATED")
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO resources (resource_id, run_id, resource_type, owner_task, owner, participants_json, allowed_operations_json, sharing_mode, cleanup_status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (resource_id, run_id, resource_type, owner_task, owner, encode_json(participants), encode_json(allowed_operations), sharing_mode, cleanup_status),
            )
        result = self.get_resource(resource_id)
        assert result is not None
        return result

    def get_resource(self, resource_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            return decode_row(connection.execute("SELECT * FROM resources WHERE resource_id = ?", (resource_id,)).fetchone())

    def record_path(self, *, run_id: str, feature: str, page: str, state: str, action: str, result: str, last_tester: str) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO explored_paths (run_id, feature, page, state, action, result, visited_count, last_tester)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?)
                ON CONFLICT (run_id, feature, page, state, action)
                DO UPDATE SET result = excluded.result, visited_count = explored_paths.visited_count + 1, last_tester = excluded.last_tester
                """,
                (run_id, feature, page, state, action, result, last_tester),
            )
            row = connection.execute(
                "SELECT * FROM explored_paths WHERE run_id = ? AND feature = ? AND page = ? AND state = ? AND action = ?",
                (run_id, feature, page, state, action),
            ).fetchone()
        decoded = decode_row(row)
        assert decoded is not None
        return decoded

    def list_paths(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM explored_paths WHERE run_id = ? ORDER BY path_id", (run_id,)).fetchall()
        return [decoded for row in rows if (decoded := decode_row(row)) is not None]

    def get_path(self, *, run_id: str, feature: str, page: str, state: str, action: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM explored_paths WHERE run_id = ? AND feature = ? AND page = ? AND state = ? AND action = ?",
                (run_id, feature, page, state, action),
            ).fetchone()
        return decode_row(row)

    def create_finding(self, *, finding_id: str, run_id: str, task_id: str, title: str, status: str, expected_result: str, actual_result: str, first_seen_by: str, severity_hint: str | None = None, needs_confirmation: bool = False, duplicate_of: str | None = None, affected_role: str | None = None, affected_page: str | None = None, action: str | None = None, error_text: str | None = None, screening_reason: str | None = None, reproduction_steps: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
        self._validate_finding_status(status)
        if severity_hint is not None and severity_hint not in SEVERITY_HINTS:
            raise ValueError(f"unsupported severity hint: {severity_hint}")
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO findings (finding_id, run_id, task_id, title, status, expected_result, actual_result, first_seen_by, severity_hint, needs_confirmation, duplicate_of, affected_role, affected_page, action, error_text, screening_reason, reproduction_steps_json, verification_details_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (finding_id, run_id, task_id, title, status, expected_result, actual_result, first_seen_by, severity_hint, int(needs_confirmation), duplicate_of, affected_role, affected_page, action, error_text, screening_reason, encode_json(reproduction_steps), encode_json({}), utc_now()),
            )
        result = self.get_finding(finding_id)
        assert result is not None
        return result

    def get_finding(self, finding_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            return decode_row(connection.execute("SELECT * FROM findings WHERE finding_id = ?", (finding_id,)).fetchone())

    def list_recent_findings(self, run_id: str, limit: int = 20) -> list[dict[str, Any]]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM findings WHERE run_id = ? ORDER BY created_at DESC LIMIT ?", (run_id, limit)).fetchall()
        return [decoded for row in rows if (decoded := decode_row(row)) is not None]

    def update_finding_status(self, *, finding_id: str, status: str, screening_reason: str | None = None, needs_confirmation: bool | None = None, duplicate_of: str | None = None) -> dict[str, Any]:
        self._validate_finding_status(status)
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE findings SET status = ?, screening_reason = COALESCE(?, screening_reason), needs_confirmation = COALESCE(?, needs_confirmation), duplicate_of = COALESCE(?, duplicate_of) WHERE finding_id = ?",
                (status, screening_reason, int(needs_confirmation) if needs_confirmation is not None else None, duplicate_of, finding_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"unknown finding: {finding_id}")
        result = self.get_finding(finding_id)
        assert result is not None
        return result

    def record_reproduction_attempt(self, *, finding_id: str, status: str, success: bool, stable_steps: Sequence[Mapping[str, Any]] | None = None) -> dict[str, Any]:
        self._validate_finding_status(status)
        with self._connect() as connection:
            finding = connection.execute(
                "SELECT reproduction_count, reproduction_success_count FROM findings WHERE finding_id = ?",
                (finding_id,),
            ).fetchone()
            if finding is None:
                raise KeyError(f"unknown finding: {finding_id}")
            reproduction_count = int(finding["reproduction_count"]) + 1
            success_count = int(finding["reproduction_success_count"]) + int(success)
            reproduction_rate = success_count / reproduction_count
            cursor = connection.execute(
                "UPDATE findings SET status = ?, reproduction_count = ?, reproduction_success_count = ?, reproduction_rate = ?, reproduction_steps_json = COALESCE(?, reproduction_steps_json) WHERE finding_id = ?",
                (status, reproduction_count, success_count, reproduction_rate, encode_json(stable_steps) if stable_steps is not None else None, finding_id),
            )
            if cursor.rowcount != 1:
                raise StateConflictError(f"finding {finding_id!r} changed during reproduction")
        result = self.get_finding(finding_id)
        assert result is not None
        return result

    def update_finding_verification(self, *, finding_id: str, status: str, verification_result: str, details: Mapping[str, Any]) -> dict[str, Any]:
        self._validate_finding_status(status)
        if verification_result not in VERIFICATION_RESULTS:
            raise ValueError(f"unsupported verification result: {verification_result}")
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE findings SET status = ?, verification_result = ?, verification_details_json = ?, needs_confirmation = ? WHERE finding_id = ?",
                (status, verification_result, encode_json(details), int(status == "NEEDS_CONFIRMATION"), finding_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"unknown finding: {finding_id}")
        result = self.get_finding(finding_id)
        assert result is not None
        return result

    def add_evidence(self, *, evidence_id: str, run_id: str, task_id: str, finding_id: str | None, evidence_type: str, relative_file_path: str, url: str | None, browser_session_id: str | None, attempt_id: str | None = None) -> dict[str, Any]:
        timestamp = utc_now()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO evidence (evidence_id, run_id, task_id, finding_id, evidence_type, attempt_id, relative_file_path, url, timestamp, browser_session_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (evidence_id, run_id, task_id, finding_id, evidence_type, attempt_id, relative_file_path, url, timestamp, browser_session_id),
            )
        with self._connect() as connection:
            result = decode_row(connection.execute("SELECT * FROM evidence WHERE evidence_id = ?", (evidence_id,)).fetchone())
        assert result is not None
        return result

    def list_evidence(self, *, run_id: str, finding_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM evidence WHERE run_id = ?"
        parameters: tuple[Any, ...] = (run_id,)
        if finding_id is not None:
            query += " AND finding_id = ?"
            parameters = (run_id, finding_id)
        query += " ORDER BY timestamp, evidence_id"
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [decoded for row in rows if (decoded := decode_row(row)) is not None]

    def create_budget(self, *, budget_id: str, run_id: str, task_id: str | None = None) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("INSERT INTO budgets (budget_id, run_id, task_id) VALUES (?, ?, ?)", (budget_id, run_id, task_id))
        result = self.get_budget(budget_id)
        assert result is not None
        return result

    def get_budget(self, budget_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            return decode_row(connection.execute("SELECT * FROM budgets WHERE budget_id = ?", (budget_id,)).fetchone())

    def list_budgets(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM budgets WHERE run_id = ? ORDER BY budget_id", (run_id,)
            ).fetchall()
        return [decoded for row in rows if (decoded := decode_row(row)) is not None]

    def update_budget(self, *, budget_id: str, llm_calls: int = 0, jev_calls: int = 0, computer_use_calls: int = 0, input_tokens: int = 0, output_tokens: int = 0, browser_steps: int = 0, runtime_seconds: float = 0, estimated_cost: float = 0) -> dict[str, Any]:
        values = (llm_calls, jev_calls, computer_use_calls, input_tokens, output_tokens, browser_steps, runtime_seconds, estimated_cost)
        if any(value < 0 for value in values):
            raise ValueError("budget increments must not be negative")
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE budgets SET llm_calls = llm_calls + ?, jev_calls = jev_calls + ?, computer_use_calls = computer_use_calls + ?, input_tokens = input_tokens + ?, output_tokens = output_tokens + ?, browser_steps = browser_steps + ?, runtime_seconds = runtime_seconds + ?, estimated_cost = estimated_cost + ? WHERE budget_id = ?",
                (*values, budget_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"unknown budget: {budget_id}")
        result = self.get_budget(budget_id)
        assert result is not None
        return result

    def append_event(self, *, event_id: str, run_id: str, event_type: str, action: str, result: Mapping[str, Any], latency_ms: float, cost: float = 0, task_id: str | None = None, tester_id: str | None = None, browser_session_id: str | None = None, url: str | None = None, tool: str | None = None, evidence_references: Sequence[str] = ()) -> dict[str, Any]:
        if latency_ms < 0 or cost < 0:
            raise ValueError("latency and cost must not be negative")
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO events (event_id, timestamp, run_id, task_id, tester_id, browser_session_id, event_type, url, tool, action, result_json, evidence_references_json, latency_ms, cost) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (event_id, utc_now(), run_id, task_id, tester_id, browser_session_id, event_type, url, tool, action, encode_json(result), encode_json(evidence_references), latency_ms, cost),
            )
        with self._connect() as connection:
            event = decode_row(connection.execute("SELECT * FROM events WHERE event_id = ?", (event_id,)).fetchone())
        assert event is not None
        return event

    def list_events(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM events WHERE run_id = ? ORDER BY timestamp, event_id", (run_id,)).fetchall()
        return [decoded for row in rows if (decoded := decode_row(row)) is not None]

    def get_latest_checkpoint(self, *, run_id: str, task_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM events WHERE run_id = ? AND task_id = ? AND event_type = 'SAFE_CHECKPOINT' ORDER BY timestamp DESC, event_id DESC LIMIT 1",
                (run_id, task_id),
            ).fetchone()
        return decode_row(row)

    def append_action_history(self, *, history_id: str, event_id: str, run_id: str, task_id: str, tester_id: str, browser_session_id: str, url: str | None, action: str, target: str | None, tool: str, started_at: str, ended_at: str, latency_ms: float, success: bool, error: str | None, result: Mapping[str, Any], action_data: Mapping[str, Any] | None = None) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO action_history (history_id, event_id, run_id, task_id, tester_id, browser_session_id, url, action, target, tool, started_at, ended_at, latency_ms, success, error, action_data_json, result_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (history_id, event_id, run_id, task_id, tester_id, browser_session_id, url, action, target, tool, started_at, ended_at, latency_ms, int(success), error, encode_json(action_data or {}), encode_json(result)),
            )
        with self._connect() as connection:
            history = decode_row(connection.execute("SELECT * FROM action_history WHERE history_id = ?", (history_id,)).fetchone())
        assert history is not None
        return history

    def list_action_history(self, *, run_id: str, task_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM action_history WHERE run_id = ? AND task_id = ? ORDER BY started_at, history_id", (run_id, task_id)).fetchall()
        return [decoded for row in rows if (decoded := decode_row(row)) is not None]

    @staticmethod
    def _validate_task_status(status: str) -> None:
        if status not in TASK_STATUSES:
            raise ValueError(f"unsupported task status: {status}")

    @staticmethod
    def _validate_finding_status(status: str) -> None:
        if status not in FINDING_STATUSES:
            raise ValueError(f"unsupported finding status: {status}")
