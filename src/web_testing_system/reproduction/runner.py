"""Automatic reproduction using deterministic replay only."""

from __future__ import annotations

from dataclasses import dataclass, replace

from web_testing_system.findings import FindingService
from web_testing_system.reproduction.replay import (
    DeterministicReplay,
    PreparePage,
    ReplayAttempt,
    ReplayPlan,
    ReplayStep,
    ResetHook,
)
from web_testing_system.state import StateStore


@dataclass(frozen=True)
class ReproductionResult:
    status: str
    attempts: tuple[ReplayAttempt, ...]
    stable_steps: tuple[dict[str, object], ...]
    reproduction_count: int
    reproduction_success_count: int
    reproduction_rate: float
    reason: str | None = None


class ReproductionRunner:
    def __init__(self, *, store: StateStore, finding_service: FindingService, replay: DeterministicReplay) -> None:
        self.store = store
        self.finding_service = finding_service
        self.replay = replay

    async def run(self, *, finding_id: str, plan: ReplayPlan, max_attempts: int, stable_successes: int = 2, max_minimization_attempts: int = 2, reset_hook: ResetHook | None = None, prepare_page: PreparePage | None = None) -> ReproductionResult:
        if max_attempts <= 0 or stable_successes <= 0 or stable_successes > max_attempts:
            raise ValueError("reproduction attempt limits are invalid")
        self.finding_service.start_reproduction(finding_id)
        attempts: list[ReplayAttempt] = []
        successes = 0
        stable_steps = list(plan.steps)
        final_status = "NOT_REPRODUCED"
        reason: str | None = None
        for index in range(max_attempts):
            attempt = await self.replay.run_attempt(finding_id=finding_id, plan=plan, purpose="REPRODUCTION", reset_hook=reset_hook, prepare_page=prepare_page)
            attempts.append(attempt)
            if attempt.needs_ai_assistance:
                final_status = "NEEDS_AI_ASSISTANCE"
                reason = attempt.reason
                self.store.update_finding_status(finding_id=finding_id, status="SUSPECTED_ISSUE", screening_reason="NEEDS_AI_ASSISTANCE", needs_confirmation=True)
                break
            if attempt.environment_issue:
                final_status = "ENVIRONMENT_ISSUE"
                reason = attempt.reason
                self.finding_service.finish_reproduction(finding_id, reproduced=False, environment_issue=True)
                break
            if attempt.matched:
                successes += 1
            stable = successes >= stable_successes
            exhausted = index == max_attempts - 1 and not stable
            updated = self.finding_service.finish_reproduction(finding_id, reproduced=attempt.matched, stable=stable, stable_steps=[step.to_record() for step in stable_steps] if stable else None, attempts_exhausted=exhausted)
            if stable:
                final_status = str(updated["status"])
                break
        if final_status in {"REPRODUCED", "NEEDS_CONFIRMATION"} and max_minimization_attempts > 0:
            stable_steps, minimization_attempts = await self._minimize(finding_id=finding_id, plan=plan, steps=stable_steps, max_attempts=max_minimization_attempts, reset_hook=reset_hook, prepare_page=prepare_page)
            attempts.extend(minimization_attempts)
        finding = self.store.get_finding(finding_id)
        assert finding is not None
        return ReproductionResult(status=final_status, attempts=tuple(attempts), stable_steps=tuple(step.to_record() for step in stable_steps), reproduction_count=int(finding["reproduction_count"]), reproduction_success_count=int(finding["reproduction_success_count"]), reproduction_rate=float(finding["reproduction_rate"]), reason=reason)

    async def _minimize(self, *, finding_id: str, plan: ReplayPlan, steps: list[ReplayStep], max_attempts: int, reset_hook: ResetHook | None, prepare_page: PreparePage | None) -> tuple[list[ReplayStep], list[ReplayAttempt]]:
        attempts: list[ReplayAttempt] = []
        candidate_steps = list(steps)
        tested = 0
        index = 0
        while index < len(candidate_steps) - 1 and tested < max_attempts:
            shortened = candidate_steps[:index] + candidate_steps[index + 1:]
            candidate_plan = replace(plan, steps=tuple(shortened))
            attempt = await self.replay.run_attempt(finding_id=finding_id, plan=candidate_plan, purpose="MINIMIZATION", reset_hook=reset_hook, prepare_page=prepare_page)
            attempts.append(attempt)
            tested += 1
            current = self.store.get_finding(finding_id)
            assert current is not None
            if attempt.matched:
                candidate_steps = shortened
                self.store.record_reproduction_attempt(finding_id=finding_id, status=str(current["status"]), success=True, stable_steps=[step.to_record() for step in candidate_steps])
            else:
                self.store.record_reproduction_attempt(finding_id=finding_id, status=str(current["status"]), success=False)
                index += 1
        return candidate_steps, attempts
