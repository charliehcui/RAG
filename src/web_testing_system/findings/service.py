"""Rule-based Finding lifecycle handling without additional Agents."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from web_testing_system.state import StateStore


@dataclass(frozen=True)
class ScreeningSignals:
    page_loading: bool = False
    network_failure: bool = False
    wrong_target: bool = False
    timed_out: bool = False
    assertion_failed: bool = False
    environment_issue: bool = False


class FindingService:
    """Apply explicit screening, duplicate, reproduction, and verification rules."""

    def __init__(self, store: StateStore, run_id: str, *, shared_state: bool = True) -> None:
        self.store = store
        self.run_id = run_id
        self.shared_state = shared_state

    def screen(self, finding_id: str, signals: ScreeningSignals, limited_decision: str | None = None, limited_duplicate_of: str | None = None) -> dict[str, Any]:
        finding = self._get_finding(finding_id)
        duplicate = self.find_duplicate(finding_id)
        if duplicate is not None:
            updated = self.store.update_finding_status(finding_id=finding_id, status="DUPLICATE", screening_reason="RULE_DUPLICATE", needs_confirmation=False, duplicate_of=str(duplicate["finding_id"]))
            self._append_event(finding_id, "FINDING_DUPLICATE", {"duplicate_of": duplicate["finding_id"], "reason": "RULE_DUPLICATE"})
            return updated
        if limited_decision == "DUPLICATE":
            if limited_duplicate_of is None:
                raise ValueError("limited duplicate decision requires duplicate_of")
            original = self._get_finding(limited_duplicate_of)
            updated = self.store.update_finding_status(finding_id=finding_id, status="DUPLICATE", screening_reason="LIMITED_AGENT_DECISION", needs_confirmation=False, duplicate_of=str(original["finding_id"]))
            self._append_event(finding_id, "FINDING_DUPLICATE", {"duplicate_of": original["finding_id"], "reason": "LIMITED_AGENT_DECISION"})
            return updated
        if signals.page_loading:
            status, reason = "OBSERVATION", "PAGE_STILL_LOADING"
        elif signals.network_failure:
            status, reason = "ENVIRONMENT_ISSUE", "NETWORK_FAILURE"
        elif signals.wrong_target:
            status, reason = "TESTER_ERROR", "WRONG_TARGET"
        elif signals.timed_out:
            status, reason = "ENVIRONMENT_ISSUE", "TIMEOUT"
        elif signals.environment_issue:
            status, reason = "ENVIRONMENT_ISSUE", "ENVIRONMENT_ISSUE"
        elif signals.assertion_failed:
            status, reason = "ANOMALY", "ASSERTION_FAILURE"
        elif finding["expected_result"].strip() and finding["expected_result"].strip() != finding["actual_result"].strip():
            status, reason = "ANOMALY", "EXPECTED_ACTUAL_MISMATCH"
        elif limited_decision == "ANOMALY":
            status, reason = "ANOMALY", "LIMITED_AGENT_DECISION"
        else:
            status, reason = "OBSERVATION", "NO_RULE_CONFIRMED_ANOMALY"
        updated = self.store.update_finding_status(finding_id=finding_id, status=status, screening_reason=reason, needs_confirmation=status in {"OBSERVATION", "ANOMALY"})
        self._append_event(finding_id, "FINDING_SCREENED", {"old_status": finding["status"], "new_status": status, "reason": reason})
        return updated

    def find_duplicate(self, finding_id: str) -> dict[str, Any] | None:
        candidate = self._get_finding(finding_id)
        for existing in self.store.list_recent_findings(self.run_id):
            if existing["finding_id"] == finding_id or existing["status"] == "DUPLICATE":
                continue
            if existing["created_at"] >= candidate["created_at"]:
                continue
            if not self.shared_state and existing["first_seen_by"] != candidate["first_seen_by"]:
                continue
            same_context = self._normalize(existing["affected_page"]) == self._normalize(candidate["affected_page"]) and self._normalize(existing["action"]) == self._normalize(candidate["action"])
            same_error = bool(candidate["error_text"]) and self._normalize(existing["error_text"]) == self._normalize(candidate["error_text"])
            same_outcome = self._normalize(existing["expected_result"]) == self._normalize(candidate["expected_result"]) and self._normalize(existing["actual_result"]) == self._normalize(candidate["actual_result"])
            same_steps = bool(candidate["reproduction_steps"]) and self._stable_json(existing["reproduction_steps"]) == self._stable_json(candidate["reproduction_steps"])
            if same_context and (same_error or same_outcome or same_steps):
                return existing
        return None

    def start_reproduction(self, finding_id: str) -> dict[str, Any]:
        finding = self._get_finding(finding_id)
        if finding["status"] not in {"ANOMALY", "SUSPECTED_ISSUE", "NOT_REPRODUCED"}:
            raise ValueError("Finding is not eligible for reproduction")
        return self.store.update_finding_status(finding_id=finding_id, status="REPRODUCING", screening_reason="DETERMINISTIC_REPLAY_STARTED", needs_confirmation=True)

    def finish_reproduction(self, finding_id: str, *, reproduced: bool, stable: bool = False, stable_steps: list[dict[str, Any]] | None = None, environment_issue: bool = False, attempts_exhausted: bool = False) -> dict[str, Any]:
        finding = self._get_finding(finding_id)
        if environment_issue:
            status = "ENVIRONMENT_ISSUE"
        elif reproduced and stable:
            status = "REPRODUCED" if finding["expected_result"].strip() else "NEEDS_CONFIRMATION"
        elif attempts_exhausted:
            status = "NOT_REPRODUCED"
        else:
            status = "REPRODUCING"
        return self.store.record_reproduction_attempt(finding_id=finding_id, status=status, success=reproduced, stable_steps=stable_steps)

    def finish_verification(self, finding_id: str, result: str, details: dict[str, Any]) -> dict[str, Any]:
        finding = self._get_finding(finding_id)
        if result == "FAIL" and finding["status"] == "REPRODUCED" and finding["expected_result"].strip():
            status = "CONFIRMED_BUG"
        elif result == "PASS":
            status = "CLOSED"
        else:
            status = "NEEDS_CONFIRMATION"
        return self.store.update_finding_verification(finding_id=finding_id, status=status, verification_result=result, details=details)

    def _get_finding(self, finding_id: str) -> dict[str, Any]:
        finding = self.store.get_finding(finding_id)
        if finding is None or finding["run_id"] != self.run_id:
            raise KeyError(f"Finding is not part of this Run: {finding_id}")
        return finding

    def _append_event(self, finding_id: str, event_type: str, result: dict[str, Any]) -> None:
        finding = self._get_finding(finding_id)
        self.store.append_event(event_id=f"event-{uuid4().hex}", run_id=self.run_id, task_id=str(finding["task_id"]), tester_id=str(finding["first_seen_by"]), event_type=event_type, tool="FindingService", action="screen_finding", result={"finding_id": finding_id, **result}, latency_ms=0)

    @staticmethod
    def _normalize(value: Any) -> str:
        return " ".join(str(value or "").casefold().split())

    @staticmethod
    def _stable_json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
