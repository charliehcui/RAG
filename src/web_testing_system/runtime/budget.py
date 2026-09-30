"""Per-Tester budget and stop-condition checks."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from time import monotonic
from typing import Any


class BudgetExceededError(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class BudgetLimits:
    max_runtime_seconds: float
    max_llm_calls: int
    max_input_tokens: int
    max_output_tokens: int
    max_jev_calls: int
    max_computer_use_calls: int
    max_task_steps: int
    max_task_replans: int
    max_browser_contexts: int
    max_no_progress_steps: int = 2


@dataclass
class BudgetUsage:
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    jev_calls: int = 0
    computer_use_calls: int = 0
    task_steps: int = 0
    task_replans: int = 0
    browser_contexts: int = 0
    no_progress_steps: int = 0
    runtime_seconds: float = 0
    estimated_cost: float = 0


class BudgetGuard:
    def __init__(self, limits: BudgetLimits) -> None:
        self.limits = limits
        self.usage = BudgetUsage()
        self.started_at = monotonic()

    def ensure_runtime_available(self) -> None:
        elapsed = monotonic() - self.started_at
        if elapsed >= self.limits.max_runtime_seconds:
            raise BudgetExceededError("MAX_RUNTIME_REACHED")

    def ensure_can_start(self, operation: str) -> None:
        self.ensure_runtime_available()
        limits = {
            "llm": (
                self.usage.llm_calls,
                self.limits.max_llm_calls,
                "MAX_LLM_CALLS_REACHED",
            ),
            "jev": (
                self.usage.jev_calls,
                self.limits.max_jev_calls,
                "MAX_JEV_CALLS_REACHED",
            ),
            "computer_use": (
                self.usage.computer_use_calls,
                self.limits.max_computer_use_calls,
                "MAX_COMPUTER_USE_CALLS_REACHED",
            ),
            "browser_step": (
                self.usage.task_steps,
                self.limits.max_task_steps,
                "MAX_TASK_STEPS_REACHED",
            ),
            "replan": (
                self.usage.task_replans,
                self.limits.max_task_replans,
                "MAX_TASK_REPLANS_REACHED",
            ),
            "browser_context": (
                self.usage.browser_contexts,
                self.limits.max_browser_contexts,
                "MAX_BROWSER_CONTEXTS_REACHED",
            ),
        }
        if operation not in limits:
            raise ValueError(f"unsupported budget operation: {operation}")
        used, limit, reason = limits[operation]
        if used >= limit:
            raise BudgetExceededError(reason)
        if operation in {"llm", "jev"} and (
            self.usage.input_tokens >= self.limits.max_input_tokens
            or self.usage.output_tokens >= self.limits.max_output_tokens
        ):
            raise BudgetExceededError("MAX_LLM_TOKENS_REACHED" if operation == "llm" else "MAX_JEV_TOKENS_REACHED")

    def record_browser_step(self, *, runtime_seconds: float) -> None:
        self.usage.task_steps += 1
        self._record_runtime(runtime_seconds)

    def record_jev_call(self, *, runtime_seconds: float, cost: float, input_tokens: int = 0, output_tokens: int = 0) -> None:
        self.usage.jev_calls += 1
        self.usage.input_tokens += max(input_tokens, 0)
        self.usage.output_tokens += max(output_tokens, 0)
        self.usage.estimated_cost += max(cost, 0)
        self._record_runtime(runtime_seconds)

    def record_llm_call(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        runtime_seconds: float,
        cost: float,
    ) -> None:
        self.usage.llm_calls += 1
        self.usage.input_tokens += max(input_tokens, 0)
        self.usage.output_tokens += max(output_tokens, 0)
        self.usage.estimated_cost += max(cost, 0)
        self._record_runtime(runtime_seconds)

    def record_computer_use_call(self, *, runtime_seconds: float, cost: float) -> None:
        self.usage.computer_use_calls += 1
        self.usage.estimated_cost += max(cost, 0)
        self._record_runtime(runtime_seconds)

    def record_replan(self) -> None:
        self.usage.task_replans += 1

    def record_context_opened(self) -> None:
        self.usage.browser_contexts += 1

    def record_context_closed(self) -> None:
        self.usage.browser_contexts = max(self.usage.browser_contexts - 1, 0)

    def note_progress(self, progressed: bool) -> bool:
        if progressed:
            self.usage.no_progress_steps = 0
            return False
        self.usage.no_progress_steps += 1
        return self.usage.no_progress_steps >= self.limits.max_no_progress_steps

    def snapshot(self) -> dict[str, Any]:
        return asdict(self.usage)

    def _record_runtime(self, runtime_seconds: float) -> None:
        self.usage.runtime_seconds += max(runtime_seconds, 0)
