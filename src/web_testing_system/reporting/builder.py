"""Build the fixed final report from Shared State without an LLM."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from statistics import fmean
from typing import Any

from web_testing_system.security import redact_sensitive_data
from web_testing_system.state import StateStore


class FinalReportBuilder:
    def __init__(self, store: StateStore, secrets: Sequence[str] = ()) -> None:
        self.store = store
        self.secrets = tuple(secret for secret in secrets if secret)

    def build(self, run_id: str, *, full_evaluation: bool = False) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if run is None:
            raise KeyError(f"unknown run: {run_id}")
        tasks = self.store.list_tasks(run_id)
        testers = self.store.list_testers(run_id)
        paths = self.store.list_paths(run_id)
        findings = self.store.list_recent_findings(run_id, limit=1_000)
        evidence = self.store.list_evidence(run_id=run_id)
        budgets = self.store.list_budgets(run_id)
        events = self.store.list_events(run_id)
        evidence_by_finding: dict[str, list[dict[str, Any]]] = {}
        for item in evidence:
            finding_id = item.get("finding_id")
            if finding_id is not None:
                evidence_by_finding.setdefault(str(finding_id), []).append(item)
        confirmed = [self._confirmed_finding(item, evidence_by_finding) for item in findings if item["status"] == "CONFIRMED_BUG"]
        needs_confirmation = [self._unconfirmed_finding(item, evidence_by_finding) for item in findings if item["status"] == "NEEDS_CONFIRMATION"]
        environment_issues = [self._environment_finding(item, evidence_by_finding) for item in findings if item["status"] == "ENVIRONMENT_ISSUE"]
        report = {
            "test_summary": self._test_summary(run, tasks, testers, events),
            "coverage_summary": self._coverage_summary(run, tasks, testers, paths, events),
            "confirmed_bugs": confirmed,
            "needs_confirmation": needs_confirmation,
            "environment_issues": environment_issues,
            "cost_and_performance": self._cost_and_performance(run, budgets, events, len(confirmed)),
            "full_evaluation": "Full Evaluation: RUN" if full_evaluation else "Full Evaluation: NOT RUN",
        }
        sanitized = redact_sensitive_data(report)
        assert isinstance(sanitized, dict)
        redacted = self._redact_registered_secrets(sanitized)
        assert isinstance(redacted, dict)
        return redacted

    @staticmethod
    def _test_summary(run: dict[str, Any], tasks: list[dict[str, Any]], testers: list[dict[str, Any]], events: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "application": run["application"],
            "version": run["application_version"] or "UNKNOWN",
            "run_id": run["run_id"],
            "run_status": run["status"],
            "run_time_seconds": FinalReportBuilder._run_time_seconds(run, events),
            "tester_count": len(testers),
            "total_tasks": len(tasks),
            "completed_tasks": sum(task["status"] == "COMPLETED" for task in tasks),
            "failed_tasks": sum(task["status"] == "FAILED" for task in tasks),
            "stopped_tasks": sum(task["status"] in {"STOPPED", "CANCELLED"} for task in tasks),
        }

    @staticmethod
    def _coverage_summary(run: dict[str, Any], tasks: list[dict[str, Any]], testers: list[dict[str, Any]], paths: list[dict[str, Any]], events: list[dict[str, Any]]) -> dict[str, Any]:
        tested_features = sorted({str(path["feature"]) for path in paths})
        registered_features = {str(value) for value in run["scope"].get("focus_features", [])}
        permission_tasks = [task for task in tasks if str(task["data_requirements"].get("feature", "")).casefold() == "permission"]
        negative_events = [event for event in events if event["event_type"] in {"PERMISSION_DENIED", "SCOPE_VIOLATION", "ASSERTION_FAILED"}]
        return {
            "features_tested": tested_features,
            "pages_visited": sorted({str(path["page"]) for path in paths}),
            "roles_tested": sorted({str(tester["role"]) for tester in testers}),
            "flows_tested": len(paths),
            "negative_cases": len(negative_events),
            "permission_cases": len(permission_tasks),
            "repeated_paths": sum(max(int(path["visited_count"]) - 1, 0) for path in paths),
            "untested_registered_targets": sorted(registered_features - set(tested_features)),
        }

    @staticmethod
    def _confirmed_finding(finding: dict[str, Any], evidence_by_finding: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
        return {
            "finding_id": finding["finding_id"],
            "title": finding["title"],
            "severity_hint": finding["severity_hint"],
            "expected": finding["expected_result"],
            "actual": finding["actual_result"],
            "stable_reproduction_steps": finding["reproduction_steps"],
            "reproduction_rate": finding["reproduction_rate"],
            "verification": finding["verification_result"],
            "evidence": FinalReportBuilder._evidence_references(finding, evidence_by_finding),
            "role": finding["affected_role"],
            "page": finding["affected_page"],
        }

    @staticmethod
    def _unconfirmed_finding(finding: dict[str, Any], evidence_by_finding: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
        return {
            "finding_id": finding["finding_id"],
            "observed_behavior": finding["actual_result"],
            "why_it_looks_suspicious": finding["screening_reason"],
            "missing_expected_behavior": not bool(str(finding["expected_result"]).strip()),
            "evidence": FinalReportBuilder._evidence_references(finding, evidence_by_finding),
        }

    @staticmethod
    def _environment_finding(finding: dict[str, Any], evidence_by_finding: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
        return {
            "finding_id": finding["finding_id"],
            "title": finding["title"],
            "details": finding["actual_result"],
            "evidence": FinalReportBuilder._evidence_references(finding, evidence_by_finding),
        }

    @staticmethod
    def _evidence_references(finding: dict[str, Any], evidence_by_finding: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
        return [
            {"evidence_id": item["evidence_id"], "type": item["evidence_type"], "relative_path": item["relative_file_path"]}
            for item in evidence_by_finding.get(str(finding["finding_id"]), [])
        ]

    @staticmethod
    def _cost_and_performance(run: dict[str, Any], budgets: list[dict[str, Any]], events: list[dict[str, Any]], confirmed_bug_count: int) -> dict[str, Any]:
        totals: dict[str, int | float] = {}
        for field in ("llm_calls", "jev_calls", "computer_use_calls", "input_tokens", "output_tokens", "runtime_seconds", "estimated_cost"):
            totals[field] = sum(item[field] for item in budgets)
        estimated_cost = float(totals["estimated_cost"])
        return {
            **totals,
            "total_run_time_seconds": FinalReportBuilder._run_time_seconds(run, events),
            "average_browser_action_ms": FinalReportBuilder._average_latency(events, "BROWSER_ACTION"),
            "average_jev_decision_ms": FinalReportBuilder._average_latency(events, "JEV_CALL"),
            "average_llm_ms": FinalReportBuilder._average_latency(events, "LLM_CALL"),
            "average_computer_use_ms": FinalReportBuilder._average_latency(events, "COMPUTER_USE_RESULT"),
            "cost_per_confirmed_bug": estimated_cost / confirmed_bug_count if confirmed_bug_count else "N/A",
        }

    @staticmethod
    def _average_latency(events: list[dict[str, Any]], event_type: str) -> float | str:
        values = [float(event["latency_ms"]) for event in events if event["event_type"] == event_type]
        return round(fmean(values), 3) if values else "N/A"

    @staticmethod
    def _run_time_seconds(run: dict[str, Any], events: list[dict[str, Any]]) -> float:
        started = datetime.fromisoformat(str(run["started_at"]))
        if run["finished_at"] is not None:
            ended = datetime.fromisoformat(str(run["finished_at"]))
        elif events:
            ended = datetime.fromisoformat(str(events[-1]["timestamp"]))
        else:
            ended = started
        return max((ended - started).total_seconds(), 0)

    def _redact_registered_secrets(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {key: self._redact_registered_secrets(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._redact_registered_secrets(item) for item in value]
        if isinstance(value, str):
            for secret in self.secrets:
                value = value.replace(secret, "[REDACTED]")
        return value
