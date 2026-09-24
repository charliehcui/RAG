from __future__ import annotations

import pytest

from web_testing_system.agents import MainAgentTools
from web_testing_system.agents.tester_agent import TesterAgentTools as AgentTools
from web_testing_system.agents.tester_agent import TesterAssignment as Assignment
from web_testing_system.runtime.models import ActionType, WebAction
from web_testing_system.runtime.permissions import ExecutionPolicy, PermissionChecker
from web_testing_system.state import (
    StateConflictError,
    StateStore,
    build_data_namespace,
)


class FakeRuntime:
    browser_session_id = "browser-tester-2"


@pytest.mark.integration
def test_structured_coordination_scope_resource_and_plan_controls(
    phase2_store: StateStore,
) -> None:
    phase2_store.create_identity(
        identity_id="identity-2",
        run_id="run-1",
        role="member",
        secret_reference="env:MEMBER_2_PASSWORD",
        permissions=["read", "delete"],
    )
    phase2_store.register_tester(
        tester_id="tester-2",
        run_id="run-1",
        session_reference="maf-session-2",
        identity_id="identity-2",
        role="member",
        data_namespace=build_data_namespace("run-1", "tester-2", "task-2"),
    )
    phase2_store.create_task(
        task_id="task-2",
        run_id="run-1",
        goal="Check member permissions",
        priority="P1",
        dependencies=[],
        created_by="main-agent",
        step_budget=10,
        data_requirements={"feature": "Permission", "role": "member"},
    )
    phase2_store.claim_task(task_id="task-2", tester_id="tester-2")
    assignment = Assignment(
        run_id="run-1",
        task_id="task-2",
        tester_id="tester-2",
        identity_id="identity-2",
        role="member",
        data_namespace=build_data_namespace("run-1", "tester-2", "task-2"),
        scope=("http://app.test/",),
        step_budget=10,
    )
    tester_tools = AgentTools(
        assignment=assignment,
        runtime=FakeRuntime(),  # type: ignore[arg-type]
        store=phase2_store,
    )

    phase2_store.record_path(
        run_id="run-1",
        feature="Permission",
        page="/projects",
        state="project-list",
        action="open",
        result="SUCCESS",
        last_tester="tester-1",
    )
    duplicate = tester_tools.check_path_before_exploring(
        "Permission", "/projects", "project-list", "open"
    )
    justified_repeat = tester_tools.check_path_before_exploring(
        "Permission",
        "/projects",
        "project-list",
        "open",
        new_reason="new cross-user Finding",
    )
    finding = tester_tools.record_finding(
        title="Member can delete another user's Project",
        status="ANOMALY",
        expected_result="Cross-user delete is denied",
        actual_result="Cross-user delete was accepted",
        severity_hint="HIGH",
        affected_page="/projects",
    )
    phase2_store.append_event(
        event_id="event-progress-tester-2",
        run_id="run-1",
        task_id="task-2",
        tester_id="tester-2",
        event_type="TASK_PROGRESS",
        tool="TesterAgent",
        action="update_progress",
        result={"progressed": True, "summary": "permission anomaly recorded"},
        latency_ms=0,
    )

    assert duplicate["should_explore"] is False
    assert justified_repeat["should_explore"] is True
    shared_facts = tester_tools.read_shared_facts()
    assert shared_facts["recent_findings"][0]["finding_id"] == finding["finding_id"]
    assert shared_facts["tester_progress"][0]["tester_id"] == "tester-2"
    with pytest.raises(ValueError, match="early Finding"):
        tester_tools.record_finding(
            title="Unverified issue",
            status="CONFIRMED_BUG",
            expected_result="expected",
            actual_result="actual",
        )

    phase2_store.create_resource(
        resource_id="shared-project",
        run_id="run-1",
        resource_type="project",
        owner_task="task-1",
        owner="tester-1",
        participants=["tester-1", "tester-2"],
        allowed_operations=["click"],
        sharing_mode="SHARED",
        cleanup_status="PENDING",
    )
    phase2_store.create_resource(
        resource_id="isolated-project",
        run_id="run-1",
        resource_type="project",
        owner_task="task-1",
        owner="tester-1",
        participants=["tester-1"],
        allowed_operations=["click"],
        sharing_mode="ISOLATED",
        cleanup_status="PENDING",
    )
    checker = PermissionChecker(
        store=phase2_store,
        policy=ExecutionPolicy(allowed_url_prefixes=("http://app.test/",)),
        run_id="run-1",
        task_id="task-2",
        tester_id="tester-2",
    )
    shared_check = checker.check(
        WebAction(
            action_type=ActionType.CLICK,
            target="#project",
            resource_id="shared-project",
            requires_resource=True,
        ),
        current_url="http://app.test/projects",
    )
    isolated_check = checker.check(
        WebAction(
            action_type=ActionType.CLICK,
            target="#project",
            resource_id="isolated-project",
            requires_resource=True,
        ),
        current_url="http://app.test/projects",
    )
    assert shared_check.allowed is True
    assert isolated_check.code == "RESOURCE_OWNER_MISMATCH"

    main_tools = MainAgentTools(
        store=phase2_store,
        run_id="run-1",
        target_url="http://app.test/",
        focus_features=["Project", "Permission"],
        allowed_scope=["/projects"],
        denied_operations=["delete production data"],
        max_step_budget=20,
    )
    snapshot = main_tools.read_coordination_snapshot()
    assert snapshot["recent_findings"][0]["finding_id"] == finding["finding_id"]
    assert snapshot["duplicate_work"][0]["event_type"] == "DUPLICATE_EXPLORATION"
    assert snapshot["tester_progress"][0]["event_id"] == "event-progress-tester-2"

    with pytest.raises(ValueError, match="outside the configured focus"):
        main_tools.create_task(
            task_id="task-outside-focus",
            goal="Test Billing",
            feature="Billing",
            priority="P1",
            dependencies=[],
            step_budget=5,
            data_requirements={},
            scope_targets=["/projects"],
            required_operations=["read"],
        )
    with pytest.raises(ValueError, match="denied operation"):
        main_tools.create_task(
            task_id="task-denied-operation",
            goal="Delete production data",
            feature="Project",
            priority="P0",
            dependencies=[],
            step_budget=5,
            data_requirements={},
            scope_targets=["/projects"],
            required_operations=["delete production data"],
        )
    assert phase2_store.get_task("task-outside-focus") is None
    assert phase2_store.get_task("task-denied-operation") is None

    low_risk = phase2_store.create_finding(
        finding_id="finding-layout",
        run_id="run-1",
        task_id="task-2",
        title="Button color looks different",
        status="OBSERVATION",
        expected_result="Blue button",
        actual_result="Slightly lighter blue button",
        first_seen_by="tester-2",
        severity_hint="HIGH",
    )
    with pytest.raises(ValueError, match="high-risk"):
        main_tools.create_task(
            task_id="task-layout-replan",
            goal="Replan all testing for button color",
            feature="Project",
            priority="P0",
            dependencies=[],
            step_budget=5,
            data_requirements={},
            scope_targets=["/projects"],
            required_operations=["read"],
            parent_finding=str(low_risk["finding_id"]),
        )

    changed = main_tools.change_task_priority(
        "task-2", "P0", "cross-user permission risk"
    )
    assert changed["task"]["priority"] == "P0"

    phase2_store.create_identity(
        identity_id="identity-3",
        run_id="run-1",
        role="member",
        secret_reference="env:MEMBER_3_PASSWORD",
        permissions=["read", "delete"],
    )
    phase2_store.register_tester(
        tester_id="tester-3",
        run_id="run-1",
        session_reference="maf-session-3",
        identity_id="identity-3",
        role="member",
        data_namespace=build_data_namespace("run-1", "tester-3", "task-2"),
    )
    paused_for_reassignment = main_tools.pause_task(
        "task-2", "move permission work to an available Tester"
    )
    assert paused_for_reassignment["task"]["status"] == "BLOCKED"
    reassigned = main_tools.reassign_task(
        "task-2", "tester-2", "tester-3", "specialized permission check"
    )
    assert reassigned["task"]["assigned_tester"] == "tester-3"
    assert reassigned["task"]["status"] == "PENDING"
    with pytest.raises(StateConflictError):
        phase2_store.claim_task(task_id="task-2", tester_id="tester-2")
    claimed_by_new_tester = phase2_store.claim_task(
        task_id="task-2", tester_id="tester-3"
    )
    assert claimed_by_new_tester["status"] == "RUNNING"

    phase2_store.create_task(
        task_id="task-low-value",
        run_id="run-1",
        goal="Review minor copy",
        priority="P3",
        dependencies=[],
        created_by="main-agent",
        step_budget=2,
        data_requirements={"feature": "Project"},
    )
    phase2_store.create_task(
        task_id="task-stop",
        run_id="run-1",
        goal="Low-value duplicate",
        priority="P3",
        dependencies=[],
        created_by="main-agent",
        step_budget=2,
        data_requirements={"feature": "Project"},
    )
    assert main_tools.pause_task("task-low-value", "higher-risk work exists")["task"]["status"] == "BLOCKED"
    assert main_tools.stop_task("task-stop", "duplicate work")["task"]["status"] == "STOPPED"
    event_types = {event["event_type"] for event in phase2_store.list_events("run-1")}
    assert {"DUPLICATE_EXPLORATION", "PRIORITY_CHANGE", "TASK_REASSIGNED", "PLAN_CHANGE"} <= event_types
