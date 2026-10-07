"""Calculate project metrics from Shared State and post-run Ground Truth facts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from statistics import fmean
from typing import Any

from web_testing_system.scoring import (
    SUCCESSFUL_OUTCOMES,
    check_completion,
    score_run_tasks,
)
from web_testing_system.state import StateStore

N_A = "N/A"
MetricValue = int | float | str


@dataclass(frozen=True)
class GroundTruthComparison:
    """Post-run mapping between confirmed Findings and enabled seeded bugs."""

    enabled_bug_ids: frozenset[str]
    finding_to_bug: Mapping[str, str]
    required_check_ids: frozenset[str] = frozenset()


class MetricsCalculator:
    """Compute deterministic metrics without an Agent or model call."""

    def __init__(self, store: StateStore) -> None:
        self.store = store

    def calculate(self, run_id: str, *, ground_truth: GroundTruthComparison | None = None, final_report: Mapping[str, Any] | None = None, expected_behavior_ids: frozenset[str] | None = None) -> dict[str, MetricValue]:
        run = self.store.get_run(run_id)
        if run is None:
            raise KeyError(f"unknown run: {run_id}")
        tasks = self.store.list_tasks(run_id)
        findings = self.store.list_recent_findings(run_id, limit=1_000)
        paths = self.store.list_paths(run_id)
        events = self.store.list_events(run_id)
        budgets = self.store.list_budgets(run_id)
        confirmed = [finding for finding in findings if finding["status"] == "CONFIRMED_BUG"]
        reproduction_count = sum(int(finding["reproduction_count"]) for finding in findings)
        reproduction_successes = sum(int(finding["reproduction_success_count"]) for finding in findings)
        browser_actions = [event for event in events if event["event_type"] == "BROWSER_ACTION"]
        jev_calls = [event for event in events if event["event_type"] == "JEV_CALL"]
        llm_calls = [event for event in events if event["event_type"] == "LLM_CALL"]
        computer_results = [event for event in events if event["event_type"] == "COMPUTER_USE_RESULT"]
        final_task_count = sum(task["status"] in {"COMPLETED", "FAILED", "STOPPED", "CANCELLED"} for task in tasks)
        repeated_visits = sum(max(int(path["visited_count"]) - 1, 0) for path in paths)
        total_visits = sum(int(path["visited_count"]) for path in paths)
        estimated_cost = sum(float(budget["estimated_cost"]) for budget in budgets)
        task_outcomes = self.task_outcomes(run_id, ground_truth=ground_truth)
        scored_tasks = self.task_results(run_id, ground_truth=ground_truth)
        completed_checks, required_checks = check_completion(scored_tasks, run["scope"].get("required_checks", []), required_check_ids=ground_truth.required_check_ids if ground_truth is not None else None)
        attempted_tasks = [task for task in tasks if task["assigned_tester"] is not None or task["status"] in {"RUNNING", "COMPLETED", "FAILED"}]
        true_positives = sum(ground_truth.finding_to_bug.get(str(finding["finding_id"])) in ground_truth.enabled_bug_ids for finding in confirmed) if ground_truth is not None else 0
        detected = {ground_truth.finding_to_bug.get(str(finding["finding_id"]), "unmatched") for finding in confirmed} & ground_truth.enabled_bug_ids if ground_truth is not None else set()
        attempted_reproductions = [finding for finding in findings if int(finding["reproduction_count"]) > 0]
        tester_calls = [event for event in llm_calls if event["result"].get("agent") == "tester"]
        tester_tokens = sum(int(event["result"].get("input_tokens") or 0) + int(event["result"].get("output_tokens") or 0) for event in tester_calls)
        tester_cost = sum(float(event["cost"]) for event in tester_calls)
        usage_complete = bool(tester_calls) and all(event["result"].get("usage_source") in {"provider", "injected_client"} for event in tester_calls)
        cost_complete = bool(tester_calls) and all(event["result"].get("cost_known") is True for event in tester_calls)
        durations = [(datetime.fromisoformat(task["finished_at"]) - datetime.fromisoformat(task["started_at"])).total_seconds() for task in attempted_tasks if task["started_at"] and task["finished_at"]]
        covered_behaviors = {item.get("behavior_id") for task in scored_tasks for item in task["assertions"] if isinstance(item.get("success"), bool)}
        successful_tasks = sum(task_outcomes[str(task["task_id"])] in SUCCESSFUL_OUTCOMES for task in attempted_tasks)
        metrics: dict[str, MetricValue] = {
            "confirmed_bug_recall": self._confirmed_bug_recall(confirmed, ground_truth),
            "false_positive_rate": self._false_positive_rate(confirmed, ground_truth),
            "bug_precision": self._ratio(true_positives, len(confirmed)) if ground_truth is not None else N_A,
            "bug_recall": self._confirmed_bug_recall(confirmed, ground_truth),
            "true_positive_count": true_positives if ground_truth is not None else N_A,
            "false_positive_count": len(confirmed) - true_positives if ground_truth is not None else N_A,
            "detected_bug_count": len(detected) if ground_truth is not None else N_A,
            "enabled_bug_count": len(ground_truth.enabled_bug_ids) if ground_truth is not None else N_A,
            "missed_bug_count": len(ground_truth.enabled_bug_ids - detected) if ground_truth is not None else N_A,
            "reproduction_success_rate": self._ratio(sum(int(finding["reproduction_success_count"]) >= 2 and finding["status"] in {"REPRODUCED", "CONFIRMED_BUG"} for finding in attempted_reproductions), len(attempted_reproductions)),
            "reproduction_attempt_success_rate": self._ratio(reproduction_successes, reproduction_count),
            "duplicate_finding_rate": self._ratio(sum(finding["status"] == "DUPLICATE" for finding in findings), len(findings)),
            "exploration_duplication": self._ratio(repeated_visits, total_visits),
            "task_completion_rate": self._ratio(sum(task["status"] == "COMPLETED" for task in tasks), len(tasks)),
            "task_success_rate": self._ratio(sum(task_outcomes[str(task["task_id"])] in SUCCESSFUL_OUTCOMES for task in attempted_tasks), len(attempted_tasks)),
            "e2e_success": int(run["status"] == "COMPLETED" and bool(tasks) and all(task["status"] == "COMPLETED" for task in tasks) and successful_tasks == len(tasks) and expected_behavior_ids.issubset(covered_behaviors) and completed_checks == required_checks and required_checks > 0 and detected == ground_truth.enabled_bug_ids and len(confirmed) == true_positives) if ground_truth is not None and expected_behavior_ids is not None else N_A,
            "completed_check_count": completed_checks,
            "required_check_count": required_checks,
            "check_completion_rate": self._ratio(completed_checks, required_checks),
            "check_completion": f"{completed_checks}/{required_checks} checks completed",
            "recovered_error_count": sum(error["status"] == "RECOVERED" for task in scored_tasks for error in task["errors"]),
            "blocking_error_count": sum(error["status"] == "UNRESOLVED_BLOCKING" for task in scored_tasks for error in task["errors"]),
            "not_started_task_count": sum(task["outcome"] == "NOT_STARTED" for task in scored_tasks),
            "interrupted_task_count": sum(task["outcome"] == "INTERRUPTED" for task in scored_tasks),
            "tester_attempted_task_count": len(attempted_tasks),
            "tester_request_count": len(tester_calls),
            "tester_requests_per_task": self._ratio(len(tester_calls), len(attempted_tasks)),
            "tester_recorded_tokens": tester_tokens,
            "tester_tokens_per_task": self._ratio(tester_tokens, len(attempted_tasks)) if usage_complete else N_A,
            "tester_recorded_cost": round(tester_cost, 9),
            "tester_cost_per_task": round(tester_cost / len(attempted_tasks), 9) if cost_complete and attempted_tasks else N_A,
            "tester_usage_status": "RECORDED" if usage_complete else "INCOMPLETE",
            "tester_cost_status": "RECORDED" if cost_complete else "INCOMPLETE",
            "average_tester_request_latency_ms": self._average_latency(tester_calls),
            "tester_request_latency_seconds_per_task": round(sum(float(event["latency_ms"]) for event in tester_calls) / 1_000 / len(attempted_tasks), 6) if attempted_tasks else N_A,
            "average_tester_task_latency_seconds": round(fmean(durations), 6) if durations and len(durations) == len(attempted_tasks) else N_A,
            "jev_requests_per_task": self._ratio(len(jev_calls), len(attempted_tasks)),
            "jev_page_action_count": sum(event["event_type"] == "PAGE_GOAL_ACTION" and event["result"].get("source") == "JEV_TO_PLAYWRIGHT" for event in events),
            "task_success_unknown_count": sum(value == "UNKNOWN_INCOMPLETE" for value in task_outcomes.values()),
            "agent_execution_failure_count": sum(value == "AGENT_EXECUTION_FAILURE" for value in task_outcomes.values()),
            "application_bug_detected_task_count": sum(value == "APPLICATION_BUG_DETECTED" for value in task_outcomes.values()),
            "browser_action_success_rate": self._ratio(sum(bool(event["result"].get("success")) for event in browser_actions), len(browser_actions)),
            "average_jev_latency_ms": self._average_latency(jev_calls),
            "average_llm_latency_ms": self._average_latency(llm_calls),
            "llm_request_count": len(llm_calls),
            "llm_success_count": sum(event["result"].get("success") is True for event in llm_calls),
            "llm_failure_count": sum(event["result"].get("success") is False for event in llm_calls),
            "llm_input_tokens": sum(int(event["result"].get("input_tokens") or 0) for event in llm_calls),
            "llm_output_tokens": sum(int(event["result"].get("output_tokens") or 0) for event in llm_calls),
            "llm_models": ", ".join(sorted({str(event["result"]["model"]) for event in llm_calls if event["result"].get("model")})) or N_A,
            "total_test_time_seconds": self._total_test_time(run, events),
            "estimated_total_cost": round(estimated_cost, 6),
            "cost_per_confirmed_bug": round(estimated_cost / len(confirmed), 6) if confirmed else N_A,
            "computer_use_recovery_rate": self._ratio(sum(event["result"].get("status") == "RETURN_TO_PLAYWRIGHT" for event in computer_results), len(computer_results)),
            "overall_completion": self._ratio(final_task_count, len(tasks)),
            "invalid_action_count": self._invalid_action_count(events),
            "test_stability": self._stability(reproduction_successes, reproduction_count),
            "developer_usable_report_rate": self._report_rate(final_report),
        }
        return metrics

    def task_outcomes(self, run_id: str, *, ground_truth: GroundTruthComparison | None = None) -> dict[str, str]:
        return {task["task_id"]: task["outcome"] for task in self.task_results(run_id, ground_truth=ground_truth)}

    def task_results(self, run_id: str, *, ground_truth: GroundTruthComparison | None = None) -> list[dict[str, Any]]:
        correct_ids = {finding_id for finding_id, bug_id in ground_truth.finding_to_bug.items() if bug_id in ground_truth.enabled_bug_ids} if ground_truth is not None else None
        return score_run_tasks(self.store, run_id, correct_finding_ids=correct_ids)

    @staticmethod
    def _confirmed_bug_recall(confirmed: list[dict[str, Any]], ground_truth: GroundTruthComparison | None) -> MetricValue:
        if ground_truth is None or not ground_truth.enabled_bug_ids:
            return N_A
        found = {ground_truth.finding_to_bug[str(finding["finding_id"])] for finding in confirmed if str(finding["finding_id"]) in ground_truth.finding_to_bug}
        return MetricsCalculator._ratio(len(found & ground_truth.enabled_bug_ids), len(ground_truth.enabled_bug_ids))

    @staticmethod
    def _false_positive_rate(confirmed: list[dict[str, Any]], ground_truth: GroundTruthComparison | None) -> MetricValue:
        if not confirmed or ground_truth is None:
            return N_A
        false_positives = 0
        for finding in confirmed:
            bug_id = ground_truth.finding_to_bug.get(str(finding["finding_id"]))
            if bug_id not in ground_truth.enabled_bug_ids:
                false_positives += 1
        return MetricsCalculator._ratio(false_positives, len(confirmed))

    @staticmethod
    def _ratio(numerator: int, denominator: int) -> MetricValue:
        return round(numerator / denominator, 6) if denominator else N_A

    @staticmethod
    def _average_latency(events: list[dict[str, Any]]) -> MetricValue:
        return round(fmean(float(event["latency_ms"]) for event in events), 3) if events else N_A

    @staticmethod
    def _total_test_time(run: dict[str, Any], events: list[dict[str, Any]]) -> float:
        started = datetime.fromisoformat(str(run["started_at"]))
        if run["finished_at"] is not None:
            ended = datetime.fromisoformat(str(run["finished_at"]))
        elif events:
            ended = datetime.fromisoformat(str(events[-1]["timestamp"]))
        else:
            ended = started
        return round(max((ended - started).total_seconds(), 0), 6)

    @staticmethod
    def _invalid_action_count(events: list[dict[str, Any]]) -> int:
        invalid_codes = {"INVALID_ACTION", "ILLEGAL_CANDIDATE_ID", "FORGED_ACTION", "CANDIDATE_EXPIRED", "SCOPE_VIOLATION", "PERMISSION_DENIED", "INVALID_GATEWAY_ACTION"}
        count = 0
        for event in events:
            result = event["result"]
            values = {str(result.get("error_type") or ""), str(result.get("error") or ""), str(result.get("reason") or "")}
            if values & invalid_codes:
                count += 1
        return count

    @staticmethod
    def _stability(successes: int, attempts: int) -> MetricValue:
        if attempts == 0:
            return N_A
        failures = attempts - successes
        return round(max(successes, failures) / attempts, 6)

    @staticmethod
    def _report_rate(report: Mapping[str, Any] | None) -> MetricValue:
        if report is None:
            return N_A
        required_sections = {"test_summary", "coverage_summary", "confirmed_bugs", "needs_confirmation", "environment_issues", "cost_and_performance", "full_evaluation"}
        usable = required_sections.issubset(report)
        required_bug_fields = {"title", "expected", "actual", "stable_reproduction_steps", "reproduction_rate", "verification", "evidence", "role", "page"}
        for bug in report.get("confirmed_bugs", []):
            usable = usable and isinstance(bug, Mapping) and required_bug_fields.issubset(bug)
        return 1.0 if usable else 0.0
