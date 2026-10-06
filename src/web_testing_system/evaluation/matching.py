"""Post-run evidence matching; no model calls or runtime answer loading."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from web_testing_system.evaluation.metrics import GroundTruthComparison
from web_testing_system.state import StateStore


def read_ground_truth(path: Path, *, scenario_id: str, run_id: str, store: StateStore) -> dict[str, Any]:
    run = store.get_run(run_id)
    if run is None:
        raise KeyError(f"unknown run: {run_id}")
    if run["status"] not in {"COMPLETED", "FAILED", "STOPPED", "CANCELLED"} or run["finished_at"] is None:
        raise PermissionError("evaluation answers are unavailable while the Run is active")
    dataset = json.loads(path.read_text(encoding="utf-8"))
    if run["application_version"] != dataset["demo_version"]:
        raise ValueError("Run and evaluation answers use different application versions")
    matches: list[dict[str, Any]] = [case for case in dataset["scenarios"] if case["scenario_id"] == scenario_id]
    if len(matches) != 1:
        raise ValueError("scenario_id must identify exactly one answer record")
    return matches[0]


def match_ground_truth(path: Path, *, scenario_id: str, run_id: str, store: StateStore, artifacts_root: Path, test_data: Mapping[str, Any]) -> tuple[GroundTruthComparison, dict[str, Any]]:
    case = read_ground_truth(path, scenario_id=scenario_id, run_id=run_id, store=store)
    mapping: dict[str, str] = {}
    reasons: dict[str, str] = {}
    root = artifacts_root.resolve()
    for finding in store.list_recent_findings(run_id, limit=1000):
        if finding["status"] != "CONFIRMED_BUG":
            continue
        finding_id = str(finding["finding_id"])
        mapping[finding_id] = "unmatched"
        attempts = [{**event["result"], "evidence_ids": event["evidence_references"]} for event in store.list_events(run_id, event_types=("VERIFICATION_ATTEMPT",), task_id=str(finding["task_id"])) if event["result"].get("finding_id") == finding_id]
        attempt_id = finding["verification_details"].get("attempt_id")
        attempt = next((item for item in attempts if item.get("attempt_id") == attempt_id), None)
        if finding["verification_result"] != "FAIL" or int(finding["reproduction_success_count"]) < 2 or attempt is None:
            reasons[finding_id] = "stable reproduction and fresh verification evidence missing"
            continue
        results = attempt.get("action_results", [])
        positions = attempt.get("assertion_positions", [])
        if not results or results[-1].get("error_type") != "ASSERTION_FAILURE":
            reasons[finding_id] = "no verified application assertion failure"
            continue
        # 验证在当前缺陷的断言处停止；此前其他缺陷保留各自已记录的结果。
        steps = finding["reproduction_steps"]
        failed_index = len(results) - 1
        if failed_index not in positions or failed_index >= len(steps):
            reasons[finding_id] = "verified assertion does not match the recorded path"
            continue
        step = steps[failed_index]
        affected_page = str(finding["affected_page"] or "")
        page, separator, annotation = affected_page.partition(" (")
        if separator and ")" in annotation:
            # 页面说明不属于路径；只接受独立验证实际成功导航过的同一路径。
            page_path = urlsplit(page).path
            if any(recorded.get("action_type") == "navigation" and result.get("success") is True and urlsplit(str(result.get("data", {}).get("url", ""))).path == page_path for recorded, result in zip(steps, results, strict=False)):
                affected_page = page
        records: list[dict[str, Any]] = []
        for evidence in store.list_evidence(run_id=run_id, finding_id=finding_id):
            if evidence["evidence_type"] != "NETWORK" or str(evidence["evidence_id"]) not in attempt.get("evidence_ids", []):
                continue
            evidence_path = (root / evidence["relative_file_path"]).resolve()
            if not evidence_path.is_relative_to(root):
                raise ValueError("evidence path leaves the run artifact directory")
            if evidence_path.is_file():
                content = json.loads(evidence_path.read_text(encoding="utf-8"))
                if isinstance(content, list):
                    records.extend({**item, "browser_session_id": evidence["browser_session_id"]} for item in content if isinstance(item, dict))
        records.sort(key=lambda item: str(item.get("captured_at", "")))
        candidates = []
        for bug in case["bugs"]:
            if step.get("behavior_id") != bug["behavior_id"] or finding["affected_role"] != bug["applicable_role"]:
                continue
            if urlsplit(affected_page).path != bug["page"]:
                continue
            if step.get("identity_reference") != bug["identity_reference"]:
                continue
            if _matches_behavior(bug, records, results[-1].get("data", {}), test_data):
                candidates.append(bug["bug_id"])
        if len(candidates) == 1:
            mapping[finding_id] = candidates[0]
            reasons[finding_id] = "behavior, role, identity, path/entity and ordered verification evidence match"
        else:
            reasons[finding_id] = "missing, contradictory or ambiguous behavior evidence"
    enabled = frozenset(case["enabled_bug_ids"])
    found = set(mapping.values()) & enabled
    comparison = GroundTruthComparison(enabled_bug_ids=enabled, finding_to_bug=mapping)
    summary = {"scenario_id": scenario_id, "finding_to_bug": mapping, "match_reasons": reasons, "tp": sum(value in enabled for value in mapping.values()), "fp": sum(value not in enabled for value in mapping.values()), "missed_bug_ids": sorted(enabled - found), "detected_bug_count": len(found), "expected_bug_count": len(enabled)}
    return comparison, summary


def _project_in_scope(bug: Mapping[str, Any], project_id: str, records: Sequence[Mapping[str, Any]], test_data: Mapping[str, Any]) -> bool:
    reference = str(bug["project_reference"])
    if re.fullmatch(r"project-\d+", reference):
        return reference == project_id
    names = {str(value).strip() for key, value in test_data.items() if key in reference}
    for record in records:
        data = record.get("response_data", {})
        projects = data.get("projects", []) if isinstance(data, dict) else []
        if isinstance(data, dict) and isinstance(data.get("project"), dict):
            projects = [*projects, data["project"]]
        if any(project.get("project_id") == project_id and str(project.get("name", "")).strip() in names for project in projects):
            return True
    return False


def _matches_behavior(bug: Mapping[str, Any], records: Sequence[Mapping[str, Any]], assertion: Mapping[str, Any], test_data: Mapping[str, Any]) -> bool:
    actor = bug["identity_reference"]
    own = [record for record in records if record.get("identity_reference") == actor]
    bug_id = bug["bug_id"]
    for index, record in enumerate(own):
        path = urlsplit(str(record.get("url", ""))).path
        parts = path.strip("/").split("/")
        method = record.get("method")
        status = record.get("status")
        data = record.get("response_data", {})
        request = record.get("request_data", {})
        later = own[index + 1:]
        if bug_id == "B5" and method == "POST" and path == "/api/projects" and status == 201 and request.get("submission_id"):
            project = data.get("project", {})
            if not _project_in_scope(bug, str(project.get("project_id", "")), records, test_data):
                continue
            for second in later:
                second_project = second.get("response_data", {}).get("project", {})
                if second.get("method") == "POST" and urlsplit(str(second.get("url", ""))).path == path and second.get("status") == 201 and second.get("request_data", {}).get("submission_id") == request["submission_id"] and second_project.get("project_id") and second_project["project_id"] != project.get("project_id"):
                    return True
        if bug_id == "B3" and method in {"POST", "PATCH"} and "name" in request and not str(request["name"]).strip() and status in {200, 201}:
            project = data.get("project", {})
            if project.get("name") == "" and _project_in_scope(bug, str(project.get("project_id", "")), records, test_data):
                return True
        if len(parts) < 3 or parts[:2] != ["api", "projects"]:
            continue
        project_id = parts[2]
        if not _project_in_scope(bug, project_id, records, test_data):
            continue
        if bug_id == "B1" and len(parts) == 5 and parts[3] == "tasks" and method == "DELETE" and status == 200:
            task_id = parts[4]
            task_reference = str(bug.get("task_reference", ""))
            if re.fullmatch(r"task-\d+", task_reference) and task_reference != task_id:
                continue
            if not re.fullmatch(r"task-\d+", task_reference):
                titles = {str(value) for key, value in test_data.items() if key in task_reference}
                if not any(item.get("response_data", {}).get("task", {}).get("task_id") == task_id and item.get("response_data", {}).get("task", {}).get("title") in titles for item in own[:index]):
                    continue
            if any(item.get("method") == "GET" and urlsplit(str(item.get("url", ""))).path == f"/api/projects/{project_id}/tasks" and any(task.get("task_id") == task_id for task in item.get("response_data", {}).get("tasks", [])) for item in later):
                return True
        if bug_id == "B2" and len(parts) == 3 and method == "DELETE" and status == 200:
            visible = [project for item in own[:index] for project in item.get("response_data", {}).get("projects", []) if project.get("project_id") == project_id]
            username = next((item.get("response_data", {}).get("user", {}).get("username") for item in own[:index] if urlsplit(str(item.get("url", ""))).path == "/api/login"), None)
            if username and any(project.get("owner") != username for project in visible) and any(item.get("method") == "GET" and urlsplit(str(item.get("url", ""))).path == "/api/projects" and "projects" in item.get("response_data", {}) and all(project.get("project_id") != project_id for project in item.get("response_data", {}).get("projects", [])) for item in later):
                return True
        if bug_id == "B4" and len(parts) == 3 and method == "PATCH" and status == 200:
            old_name = data.get("project", {}).get("name")
            new_name = request.get("name")
            persisted = any(any(project.get("project_id") == project_id and project.get("name") == new_name for project in item.get("response_data", {}).get("projects", [])) or (item.get("response_data", {}).get("project", {}).get("project_id") == project_id and item.get("response_data", {}).get("project", {}).get("name") == new_name) for item in later)
            if new_name and old_name != new_name and assertion.get("actual") == old_name and persisted:
                return True
    if bug_id == "B6":
        for removal in records:
            parts = urlsplit(str(removal.get("url", ""))).path.strip("/").split("/")
            if len(parts) != 5 or parts[:2] != ["api", "projects"] or parts[3] != "members" or removal.get("method") != "DELETE" or removal.get("status") != 200 or removal.get("identity_reference") == actor:
                continue
            project_id, username = parts[2], parts[4]
            if not _project_in_scope(bug, project_id, records, test_data):
                continue
            for session_id in {item.get("browser_session_id") for item in own}:
                before = [item for item in own if item.get("browser_session_id") == session_id and str(item.get("captured_at", "")) < str(removal.get("captured_at", ""))]
                after = [item for item in own if item.get("browser_session_id") == session_id and str(item.get("captured_at", "")) > str(removal.get("captured_at", ""))]
                logged_in = any(urlsplit(str(item.get("url", ""))).path == "/api/login" and item.get("response_data", {}).get("user", {}).get("username") == username for item in before)
                accessible = any(any(project.get("project_id") == project_id for project in item.get("response_data", {}).get("projects", [])) for item in before)
                reauthenticated = any(urlsplit(str(item.get("url", ""))).path in {"/api/login", "/api/logout"} for item in after)
                persisted = any(item.get("method") == "GET" and item.get("status") == 200 and (any(project.get("project_id") == project_id for project in item.get("response_data", {}).get("projects", [])) or urlsplit(str(item.get("url", ""))).path in {f"/api/projects/{project_id}/tasks", f"/api/projects/{project_id}/members"}) for item in after)
                if logged_in and accessible and not reauthenticated and persisted:
                    return True
    return False
