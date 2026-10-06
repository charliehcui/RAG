"""Trusted dataset projection and post-run-only access to evaluation answers."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from web_testing_system.config import RunConfig
from web_testing_system.state import StateStore

FINAL_STATUSES = {"COMPLETED", "FAILED", "STOPPED", "CANCELLED"}
ANSWER_MARKERS = re.compile(r"\bB[1-6]\b|ground[_ -]?truth|enabled[_ -]?(?:seeded[_ -]?)?bugs|expected[_ -]?bug[_ -]?count|DEMO_BUG_", re.IGNORECASE)


def load_run_config(path: Path, *, scenario_id: str, target_url: str, tester_count: int = 3) -> RunConfig:
    """Project a trusted case onto the existing RunConfig; never pass a raw case to an Agent."""
    if tester_count not in {1, 3}:
        raise ValueError("scenario comparisons support one or three Testers")
    dataset = json.loads(path.read_text(encoding="utf-8"))
    matches: list[dict[str, Any]] = [case for case in dataset["scenarios"] if case["scenario_id"] == scenario_id]
    if len(matches) != 1:
        raise ValueError("scenario_id must identify exactly one case")
    case = matches[0]
    if tester_count == 1 and case["expected_parallelism"]["target_testers"] > 1 and not case["expected_parallelism"]["compare_1_vs_3"]:
        raise ValueError("this coupled workflow requires simultaneous identities and is not a one-Tester comparison")
    goals = []
    for goal in case["required_test_goals"]:
        goals.append({key: goal[key] for key in ("goal_id", "description", "feature", "identity_reference", "expected_behavior_ids", "test_data_keys", "independent_group")})
    budget = dict(dataset["budget"])
    budget.update(max_testers=tester_count, max_parallel_browser_contexts=tester_count)
    payload = {
        "target_url": target_url,
        "test_goal": case["user_goal"] + "\nRequired checks: " + json.dumps(goals, ensure_ascii=False) + "\nCompletion requirements: " + json.dumps(case["success_criteria"], ensure_ascii=False),
        "focus_features": case["focus_features"],
        "allowed_scope": case["scope"],
        "account_references": case["allowed_roles"],
        "test_data": case["required_test_data"],
        "denied_operations": dataset["denied_operations"],
        "expected_behaviors": case["expected_behaviors"],
        "application_version": dataset["demo_version"],
        "reset_hook": {key: case["reset_requirements"][key] for key in ("hook_type", "target")},
        "budget": budget,
    }
    if ANSWER_MARKERS.search(json.dumps(payload, ensure_ascii=False)):
        raise ValueError("evaluation answer metadata is forbidden in Agent input")
    return RunConfig.model_validate(payload)


def read_ground_truth(path: Path, *, scenario_id: str, run_id: str, store: StateStore) -> dict[str, Any]:
    """Check persisted Run state before opening the answer file, including for failed Runs."""
    run = store.get_run(run_id)
    if run is None:
        raise KeyError(f"unknown run: {run_id}")
    if run["status"] not in FINAL_STATUSES or run["finished_at"] is None:
        raise PermissionError("evaluation answers are unavailable while the Run is active")
    dataset = json.loads(path.read_text(encoding="utf-8"))
    if run["application_version"] != dataset["demo_version"]:
        raise ValueError("Run and evaluation answers use different application versions")
    matches: list[dict[str, Any]] = [case for case in dataset["scenarios"] if case["scenario_id"] == scenario_id]
    if len(matches) != 1:
        raise ValueError("scenario_id must identify exactly one answer record")
    return matches[0]
