"""Shared deterministic Task scoring for Runtime, reports and formal Evaluation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from web_testing_system.state import StateStore

SUCCESSFUL_OUTCOMES = frozenset({"NORMAL_APPLICATION_BEHAVIOR", "APPLICATION_BUG_DETECTED"})


def task_assertions(store: StateStore, task: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    histories = store.list_action_history(run_id=str(task["run_id"]), task_id=str(task["task_id"]))
    # Replay writes the same task ID using fresh sessions after exploration has ended.
    if task.get("finished_at"):
        histories = [history for history in histories if history["started_at"] <= task["finished_at"]]
    required = task["data_requirements"].get("required_checks", [])
    behaviors = task["data_requirements"].get("expected_behavior_ids", [])
    assertions = []
    for history in histories:
        action = history["action_data"]
        spec = next((check for check in required if check["check_id"] == action.get("check_id")), None)
        pending_check = action.get("action_type") == "repeat_submit" and spec is not None and spec.get("action_type") == "repeat_submit"
        if spec is not None and spec.get("action_type") == "repeat_submit" and not pending_check:
            continue
        if action.get("action_type") not in {"assertion", "url_check"} and not pending_check:
            continue
        if not action.get("goal_check") and action.get("behavior_id") not in behaviors and not action.get("check_id"):
            continue
        if history["result"].get("error_type") not in {None, "ASSERTION_FAILURE"}:
            continue
        if pending_check:
            requests = history["result"].get("data", {}).get("requests", [])
            if len(requests) != 2 or not requests[0].get("submission_id") or requests[0]["submission_id"] != requests[1].get("submission_id") or any(item.get("pending_pair_timed_out") for item in requests):
                continue
        assertions.append({"event_id": history["event_id"], "check_id": action.get("check_id"), "behavior_id": action.get("behavior_id"), "identity_reference": action.get("identity_reference"), "success": bool(history["success"]), "error_type": history["result"].get("error_type"), "timestamp": history["ended_at"]})
    # Older persisted runs have only assertion summaries. Never synthesize new check IDs.
    if not histories and not required:
        assertions = [dict(item) for item in task.get("assertion_results", [])]
    return assertions, histories


def score_task(store: StateStore, task: Mapping[str, Any], *, live: bool = False, correct_finding_ids: set[str] | None = None) -> dict[str, Any]:
    run_id, task_id = str(task["run_id"]), str(task["task_id"])
    assertions, histories = task_assertions(store, task)
    requirements = task["data_requirements"]
    required = requirements.get("required_checks", [])
    run_checks = (store.get_run(run_id) or {}).get("scope", {}).get("required_checks", [])
    strict = bool(required or run_checks)
    if not strict:
        # Compatibility for historical/non-scenario runs; v2 Development cases declare checks.
        behavior_ids = requirements.get("expected_behavior_ids") or sorted({item.get("behavior_id") for item in assertions if item.get("behavior_id")})
        required = [{"check_id": behavior_id, "behavior_id": behavior_id, "depends_on": []} for behavior_id in behavior_ids]
        if not required:
            required = [{"check_id": item["event_id"], "behavior_id": None, "depends_on": []} for item in assertions]
    all_tasks = store.list_tasks(run_id)
    specifications = {item["check_id"]: item for other in all_tasks for item in other["data_requirements"].get("required_checks", [])}
    specifications.update({item["check_id"]: item for item in run_checks})
    records: dict[str, list[dict[str, Any]]] = {}
    for other in all_tasks:
        items = assertions if other["task_id"] == task_id else task_assertions(store, other)[0]
        for item in items:
            check_id = item.get("check_id")
            spec = specifications.get(str(check_id))
            if spec is not None and item.get("behavior_id") == spec["behavior_id"] and item.get("identity_reference") == spec["identity_reference"]:
                records.setdefault(str(check_id), []).append(item)
    valid_records: dict[str, list[dict[str, Any]]] = {}

    def valid(check_id: str, active: frozenset[str] = frozenset()) -> list[dict[str, Any]]:
        if check_id in valid_records:
            return valid_records[check_id]
        if check_id in active:
            return []
        spec = specifications.get(check_id, {})
        items = []
        for item in records.get(check_id, []):
            if all(any(before.get("timestamp", "") < item.get("timestamp", "") for before in valid(dependency, active | {check_id})) for dependency in spec.get("depends_on", [])):
                items.append(item)
        valid_records[check_id] = items
        return items

    findings = {str(item["finding_id"]): item for item in store.list_recent_findings(run_id, limit=1000)}
    finding_events = store.list_events(run_id, event_types=("FINDING_CREATED", "CHECK_FINDING_LINKED"), task_id=task_id)
    checks = []
    for spec in required:
        check_id = spec["check_id"]
        matching = valid(check_id) if strict else [item for item in assertions if (item.get("behavior_id") or item["event_id"]) == check_id]
        matching = [item for item in matching if any(item["event_id"] == own["event_id"] for own in assertions)]
        reported_events = {str(event["result"].get("boundary_event_id")) for event in finding_events}
        reported_failures = [item for item in matching if not item["success"] and str(item["event_id"]) in reported_events]
        observed = reported_failures[0] if reported_failures else matching[-1] if matching else None
        evidenced_defect = False
        reported = False
        if observed is not None and not observed["success"]:
            for event in finding_events:
                finding = findings.get(str(event["result"].get("finding_id")))
                if finding is None:
                    continue
                linked = str(event["result"].get("boundary_event_id")) == str(observed["event_id"])
                if strict:
                    linked = linked or event["result"].get("check_id") == check_id
                else:
                    linked = linked or event["result"].get("behavior_id") == spec["behavior_id"]
                if not linked or not finding["expected_result"].strip() or not finding["actual_result"].strip():
                    continue
                reported = True
                canonical_id = str(finding["duplicate_of"] or finding["finding_id"])
                canonical = findings.get(canonical_id, finding)
                confirmed = canonical["status"] == "CONFIRMED_BUG"
                evidenced_defect = evidenced_defect or (confirmed and (correct_finding_ids is None or canonical_id in correct_finding_ids))
        checks.append({"check_id": check_id, "behavior_id": spec["behavior_id"], "completed": observed is not None, "success": observed["success"] if observed else None, "event_id": observed["event_id"] if observed else None, "timestamp": observed.get("timestamp") if observed else None, "result": "PASSED" if observed and observed["success"] else "APPLICATION_BUG_DETECTED" if evidenced_defect else "NEEDS_CONFIRMATION" if reported else "UNREPORTED_DEVIATION" if observed else "NOT_COMPLETED"})
    completed = sum(check["completed"] for check in checks)
    all_completed = bool(checks) and completed == len(checks)
    all_supported = all_completed and all(check["result"] in {"PASSED", "APPLICATION_BUG_DETECTED"} for check in checks)
    last_check_time = max((check["timestamp"] or "" for check in checks), default="")
    errors = []
    for index, history in enumerate(histories):
        if history["success"] or history["result"].get("error_type") == "ASSERTION_FAILURE":
            continue
        action = history["action_data"]
        retried = any(later["success"] and all(later["action_data"].get(key) == action.get(key) for key in ("action_type", "target", "value_reference", "resource_id")) for later in histories[index + 1:])
        observed_supported = all_completed and all(check["result"] != "UNREPORTED_DEVIATION" for check in checks)
        recovered = retried or observed_supported and last_check_time > history["ended_at"]
        if history["result"].get("error_type") in {"SCOPE_VIOLATION", "PERMISSION_DENIED", "FORGED_ACTION"}:
            recovered = False
        errors.append({"event_id": history["event_id"], "error_type": history["result"].get("error_type"), "status": "RECOVERED" if recovered else "UNRESOLVED_BLOCKING"})
    blocking = any(error["status"] == "UNRESOLVED_BLOCKING" for error in errors)
    attempted = bool(task.get("started_at") or histories or task["status"] in {"RUNNING", "COMPLETED", "FAILED"})
    invalid_confirmed = correct_finding_ids is not None and any(finding["task_id"] == task_id and finding["status"] == "CONFIRMED_BUG" and finding["finding_id"] not in correct_finding_ids for finding in findings.values())
    status, reason, outcome = "UNKNOWN", "GOAL_CHECK_MISSING", "UNKNOWN_INCOMPLETE"
    if not attempted:
        reason, outcome = "TASK_NOT_STARTED", "NOT_STARTED"
    elif task["status"] == "FAILED" or blocking or invalid_confirmed:
        status, reason, outcome = "FAIL", "AGENT_EXECUTION_FAILURE", "AGENT_EXECUTION_FAILURE"
    elif not live and task["status"] in {"STOPPED", "CANCELLED"}:
        reason, outcome = "TASK_INTERRUPTED", "INTERRUPTED"
    elif all_supported and (live or task["status"] == "COMPLETED"):
        bug = any(check["result"] == "APPLICATION_BUG_DETECTED" for check in checks)
        status, reason, outcome = "PASS", "APPLICATION_BUG_DETECTED" if bug else "GOAL_ASSERTIONS_PASSED", "APPLICATION_BUG_DETECTED" if bug else "NORMAL_APPLICATION_BEHAVIOR"
    elif all_completed:
        reason = "APPLICATION_DEVIATION_UNREPORTED" if any(check["result"] == "UNREPORTED_DEVIATION" for check in checks) else "APPLICATION_DEVIATION_PENDING"
    return {"task_id": task_id, "goal": task["goal"], "feature": requirements.get("feature"), "execution_status": task["status"], "success_status": status, "reason": reason, "outcome": outcome, "application_behavior": "FAIL" if any(check["success"] is False for check in checks) else "PASS" if all_completed else "UNKNOWN", "assertions": assertions, "checks": checks, "completed_check_count": completed, "required_check_count": len(checks), "check_completion": f"{completed}/{len(checks)} checks completed", "errors": errors, "ready_to_finish": all_completed and not blocking and all(check["result"] != "UNREPORTED_DEVIATION" for check in checks)}


def score_run_tasks(store: StateStore, run_id: str, *, correct_finding_ids: set[str] | None = None) -> list[dict[str, Any]]:
    return [score_task(store, task, correct_finding_ids=correct_finding_ids) for task in store.list_tasks(run_id)]


def check_completion(results: Sequence[Mapping[str, Any]], required_checks: Sequence[Mapping[str, Any]], *, required_check_ids: frozenset[str] | None = None) -> tuple[int, int]:
    completed = {check["check_id"] for result in results for check in result["checks"] if check["completed"]}
    required = set(required_check_ids) if required_check_ids else {check["check_id"] for check in required_checks}
    if required:
        return len(completed & required), len(required)
    return sum(result["completed_check_count"] for result in results), sum(result["required_check_count"] for result in results)
