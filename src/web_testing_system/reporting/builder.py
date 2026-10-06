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
        needs_confirmation = [self._unconfirmed_finding(item, evidence_by_finding) for item in findings if item["status"] in {"OBSERVATION", "ANOMALY", "REPRODUCING", "REPRODUCED", "NOT_REPRODUCED", "NEEDS_CONFIRMATION", "SUSPECTED_ISSUE"}]
        environment_issues = [self._environment_finding(item, evidence_by_finding) for item in findings if item["status"] == "ENVIRONMENT_ISSUE"]
        report = {
            "test_summary": self._test_summary(run, tasks, testers, events),
            "coverage_summary": self._coverage_summary(run, tasks, testers, paths, events),
            "confirmed_bugs": confirmed,
            "needs_confirmation": needs_confirmation,
            "environment_issues": environment_issues,
            "findings": [{"finding_id": item["finding_id"], "status": item["status"], "title": item["title"], "expected": item["expected_result"], "actual": item["actual_result"], "screening_reason": item["screening_reason"], "verification": item["verification_result"], "reproduction_steps": item["reproduction_steps"], "reproduction_rate": item["reproduction_rate"], "evidence": self._evidence_references(item, evidence_by_finding)} for item in findings],
            "task_outcomes": [{"task_id": task["task_id"], "goal": task["goal"], "feature": task["data_requirements"].get("feature"), "execution_status": task["status"], "success_status": task["success_status"], "reason": task["success_reason"], "assertions": task["assertion_results"]} for task in tasks],
            "model_configuration": run["scope"].get("model_configuration", {}),
            "cost_and_performance": self._cost_and_performance(run, budgets, events, len(confirmed)),
            "main_final_summary": self._final_summary(events),
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
            "test_goal": run["test_goal"],
            "run_time_seconds": FinalReportBuilder._run_time_seconds(run, events),
            "tester_count": len(testers),
            "configured_tester_capacity": run["scope"].get("configured_tester_capacity"),
            "total_tasks": len(tasks),
            "completed_tasks": sum(task["status"] == "COMPLETED" for task in tasks),
            "successful_tasks": sum(task["success_status"] == "PASS" for task in tasks),
            "unsuccessful_tasks": sum(task["success_status"] == "FAIL" for task in tasks),
            "unknown_success_tasks": sum(task["success_status"] == "UNKNOWN" for task in tasks),
            "peak_concurrent_testers": FinalReportBuilder._peak_concurrency(events),
            "failed_tasks": sum(task["status"] == "FAILED" for task in tasks),
            "stopped_tasks": sum(task["status"] in {"STOPPED", "CANCELLED"} for task in tasks),
        }

    @staticmethod
    def _coverage_summary(run: dict[str, Any], tasks: list[dict[str, Any]], testers: list[dict[str, Any]], paths: list[dict[str, Any]], events: list[dict[str, Any]]) -> dict[str, Any]:
        tested_features = sorted({str(path["feature"]) for path in paths})
        registered_features = {str(value) for value in run["scope"].get("focus_features", [])}
        permission_tasks = [task for task in tasks if str(task["data_requirements"].get("feature", "")).casefold() == "permission"]
        negative_events = [event for event in events if event["event_type"] == "BROWSER_ACTION" and event["result"].get("error_type") in {"PERMISSION_DENIED", "SCOPE_VIOLATION", "ASSERTION_FAILURE"}]
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
            "status": finding["status"],
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
        llm_events = [event for event in events if event["event_type"] == "LLM_CALL"]
        unknown_cost = any(event["result"].get("cost_known") is False for event in llm_events)
        phase_metrics = {}
        for phase in ("main_planning", "main_replan", "tester", "visual", "main_final_summary"):
            calls = [event for event in llm_events if event["result"].get("phase", event["result"].get("agent")) == phase]
            phase_metrics[phase] = {"requests": len(calls), "latency_ms": sum(float(event["latency_ms"]) for event in calls), "input_tokens": sum(int(event["result"].get("input_tokens") or 0) for event in calls), "output_tokens": sum(int(event["result"].get("output_tokens") or 0) for event in calls), "recorded_cost": sum(float(event["cost"]) for event in calls), "cost_status": "INCOMPLETE" if any(event["result"].get("cost_known") is False for event in calls) else "RECORDED"}
        return {
            **totals,
            "cost_status": "INCOMPLETE" if unknown_cost else "RECORDED",
            "llm_request_count": len(llm_events),
            "llm_success_count": sum(event["result"].get("success") is True for event in llm_events),
            "llm_failure_count": sum(event["result"].get("success") is False for event in llm_events),
            "llm_input_tokens": sum(int(event["result"].get("input_tokens") or 0) for event in llm_events),
            "llm_output_tokens": sum(int(event["result"].get("output_tokens") or 0) for event in llm_events),
            "llm_models": sorted({str(event["result"]["model"]) for event in llm_events if event["result"].get("model")}),
            "llm_by_phase": phase_metrics,
            "total_run_time_seconds": FinalReportBuilder._run_time_seconds(run, events),
            "average_browser_action_ms": FinalReportBuilder._average_latency(events, "BROWSER_ACTION"),
            "average_jev_decision_ms": FinalReportBuilder._average_latency(events, "JEV_CALL"),
            "average_llm_ms": FinalReportBuilder._average_latency(events, "LLM_CALL"),
            "average_computer_use_ms": FinalReportBuilder._average_latency(events, "COMPUTER_USE_RESULT"),
            "cost_per_confirmed_bug": estimated_cost / confirmed_bug_count if confirmed_bug_count and not unknown_cost else "N/A",
        }

    @staticmethod
    def _final_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
        summaries = [event for event in events if event["event_type"] in {"FINAL_REPORT_SUMMARY", "FINAL_REPORT_SUMMARY_FAILED"}]
        if not summaries:
            return {"status": "NOT_RUN"}
        result = summaries[-1]["result"]
        return {"status": result["status"], "error_type": result.get("error_type"), "latency_ms": result.get("latency_ms"), "relative_path": "summary.md" if result["status"] == "COMPLETED" else None, "facts_stage": "before_main_final_summary"}

    @staticmethod
    def _peak_concurrency(events: list[dict[str, Any]]) -> int:
        active: set[str] = set()
        peak = 0
        for event in events:
            if event["event_type"] == "TASK_STARTED":
                active.add(str(event["task_id"]))
                peak = max(peak, len(active))
            elif event["event_type"] in {"TASK_FINISHED", "TESTER_FAILED"}:
                active.discard(str(event["task_id"]))
        return peak

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
