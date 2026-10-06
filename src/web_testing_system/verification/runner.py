"""Verify known reproduction steps and expected behavior without another Agent."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any
from uuid import uuid4

from web_testing_system.findings import FindingService
from web_testing_system.reproduction.replay import (
    DeterministicReplay,
    PreparePage,
    ReplayAttempt,
    ReplayPlan,
    ResetHook,
)
from web_testing_system.runtime.models import ActionType
from web_testing_system.state import StateStore


@dataclass(frozen=True)
class VerificationResult:
    result: str
    reason: str | None
    attempt: ReplayAttempt | None


class VerificationRunner:
    def __init__(self, *, store: StateStore, finding_service: FindingService, replay: DeterministicReplay) -> None:
        self.store = store
        self.finding_service = finding_service
        self.replay = replay

    async def run(self, *, finding_id: str, plan: ReplayPlan, reset_hook: ResetHook | None = None, prepare_page: PreparePage | None = None) -> VerificationResult:
        finding = self.store.get_finding(finding_id)
        if finding is None or finding["run_id"] != plan.run_id:
            raise KeyError(f"Finding is not part of this Run: {finding_id}")
        if not finding["expected_result"].strip():
            return self._needs_confirmation(finding, "EXPECTED_BEHAVIOR_MISSING", None)
        if not plan.steps:
            return self._needs_confirmation(finding, "NEEDS_AI_ASSISTANCE", None)
        if not any(step.action_type in {ActionType.ASSERTION, ActionType.URL_CHECK} for step in plan.steps):
            return self._needs_confirmation(finding, "DETERMINISTIC_ASSERTION_MISSING", None)
        creation: dict[str, Any] = next((event["result"] for event in self.store.list_events(plan.run_id, event_types=("FINDING_CREATED",), task_id=plan.task_id) if event["result"].get("finding_id") == finding_id), {})
        behavior_id = creation.get("behavior_id")
        target = next((index for index in reversed(plan.assertion_positions) if not plan.steps[index].expected_success and (behavior_id is None or plan.steps[index].behavior_id == behavior_id)), None)
        verification_steps = tuple(replace(step, expected_success=True, expected_error_type=None) if index == target else step for index, step in enumerate(plan.steps))
        verification_plan = replace(plan, steps=verification_steps)
        attempt = await self.replay.run_attempt(finding_id=finding_id, plan=verification_plan, purpose="VERIFICATION", reset_hook=reset_hook, prepare_page=prepare_page)
        if attempt.needs_ai_assistance or attempt.environment_issue or attempt.reason in {"MAX_RUNTIME_REACHED", "MAX_TASK_STEPS_REACHED", "MAX_BROWSER_CONTEXTS_REACHED"}:
            reason = "NEEDS_AI_ASSISTANCE" if attempt.needs_ai_assistance else attempt.reason or "ENVIRONMENT_ISSUE"
            return self._needs_confirmation(finding, reason, attempt)
        result = "PASS" if attempt.matched else "FAIL"
        if result == "FAIL" and (not attempt.action_results or attempt.action_results[-1].get("error_type") != "ASSERTION_FAILURE"):
            return self._needs_confirmation(finding, "VERIFICATION_PATH_FAILED", attempt)
        details = {"reason": attempt.reason, "attempt_id": attempt.attempt_id, "duration_ms": attempt.duration_ms, "evidence_ids": list(attempt.evidence_ids)}
        self.finding_service.finish_verification(finding_id, result, details)
        self.store.append_event(event_id=f"event-{uuid4().hex}", run_id=plan.run_id, task_id=plan.task_id, tester_id=plan.tester_id, browser_session_id=attempt.browser_session_id, event_type="VERIFICATION_RESULT", tool="VerificationRunner", action="verify_known_steps", result={"finding_id": finding_id, "result": result, **details}, evidence_references=attempt.evidence_ids, latency_ms=attempt.duration_ms)
        return VerificationResult(result=result, reason=attempt.reason, attempt=attempt)

    def _needs_confirmation(self, finding: dict[str, object], reason: str, attempt: ReplayAttempt | None) -> VerificationResult:
        details = {"reason": reason, "attempt_id": attempt.attempt_id if attempt else None, "duration_ms": attempt.duration_ms if attempt else 0, "evidence_ids": list(attempt.evidence_ids) if attempt else [], "assigned_tester": finding["first_seen_by"]}
        self.finding_service.finish_verification(str(finding["finding_id"]), "NEEDS_CONFIRMATION", details)
        self.store.append_event(event_id=f"event-{uuid4().hex}", run_id=str(finding["run_id"]), task_id=str(finding["task_id"]), tester_id=str(finding["first_seen_by"]), browser_session_id=attempt.browser_session_id if attempt else None, event_type="NEEDS_AI_ASSISTANCE" if reason == "NEEDS_AI_ASSISTANCE" else "VERIFICATION_RESULT", tool="VerificationRunner", action="request_local_path_fix" if reason == "NEEDS_AI_ASSISTANCE" else "verify_known_steps", result={"finding_id": finding["finding_id"], "result": "NEEDS_CONFIRMATION", **details}, evidence_references=attempt.evidence_ids if attempt else (), latency_ms=attempt.duration_ms if attempt else 0)
        return VerificationResult(result="NEEDS_CONFIRMATION", reason=reason, attempt=attempt)
