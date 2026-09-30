"""Calculate project metrics from Shared State and post-run Ground Truth facts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from statistics import fmean
from typing import Any

from web_testing_system.state import StateStore

N_A = "N/A"
MetricValue = int | float | str


@dataclass(frozen=True)
class GroundTruthComparison:
    """Post-run mapping between confirmed Findings and enabled seeded bugs."""

    enabled_bug_ids: frozenset[str]
    finding_to_bug: Mapping[str, str]


class MetricsCalculator:
    """Compute deterministic metrics without an Agent or model call."""

    def __init__(self, store: StateStore) -> None:
        self.store = store

    def calculate(self, run_id: str, *, ground_truth: GroundTruthComparison | None = None, final_report: Mapping[str, Any] | None = None) -> dict[str, MetricValue]:
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
        metrics: dict[str, MetricValue] = {
            "confirmed_bug_recall": self._confirmed_bug_recall(confirmed, ground_truth),
            "false_positive_rate": self._false_positive_rate(confirmed, ground_truth),
            "reproduction_success_rate": self._ratio(reproduction_successes, reproduction_count),
            "duplicate_finding_rate": self._ratio(sum(finding["status"] == "DUPLICATE" for finding in findings), len(findings)),
            "exploration_duplication": self._ratio(repeated_visits, total_visits),
            "task_completion_rate": self._ratio(sum(task["status"] == "COMPLETED" for task in tasks), len(tasks)),
            "browser_action_success_rate": self._ratio(sum(bool(event["result"].get("success")) for event in browser_actions), len(browser_actions)),
            "average_jev_latency_ms": self._average_latency(jev_calls),
            "average_llm_latency_ms": self._average_latency(llm_calls),
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
