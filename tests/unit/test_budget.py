from __future__ import annotations

import pytest

from web_testing_system.runtime.budget import (
    BudgetExceededError,
    BudgetGuard,
    BudgetLimits,
)


def make_budget(
    *,
    max_llm_calls: int = 1,
    max_input_tokens: int = 10,
    max_output_tokens: int = 10,
) -> BudgetGuard:
    return BudgetGuard(
        BudgetLimits(
            max_runtime_seconds=60,
            max_llm_calls=max_llm_calls,
            max_input_tokens=max_input_tokens,
            max_output_tokens=max_output_tokens,
            max_laya_calls=1,
            max_computer_use_calls=1,
            max_task_steps=1,
            max_task_replans=1,
            max_browser_contexts=1,
            max_no_progress_steps=2,
        )
    )


def test_budget_checks_every_phase_two_limit() -> None:
    budget = make_budget()
    budget.record_llm_call(
        input_tokens=10, output_tokens=10, runtime_seconds=0.1, cost=0.01
    )
    with pytest.raises(BudgetExceededError, match="MAX_LLM_CALLS_REACHED"):
        budget.ensure_can_start("llm")

    budget = make_budget(max_llm_calls=2)
    budget.record_llm_call(
        input_tokens=10, output_tokens=1, runtime_seconds=0.1, cost=0
    )
    with pytest.raises(BudgetExceededError, match="MAX_LLM_TOKENS_REACHED"):
        budget.ensure_can_start("llm")

    budget = make_budget()
    budget.record_laya_call(runtime_seconds=0.1, cost=0)
    with pytest.raises(BudgetExceededError, match="MAX_LAYA_CALLS_REACHED"):
        budget.ensure_can_start("laya")

    budget = make_budget()
    budget.record_computer_use_call(runtime_seconds=0.1, cost=0)
    with pytest.raises(BudgetExceededError, match="MAX_COMPUTER_USE_CALLS_REACHED"):
        budget.ensure_can_start("computer_use")

    budget = make_budget()
    budget.record_browser_step(runtime_seconds=0.1)
    with pytest.raises(BudgetExceededError, match="MAX_TASK_STEPS_REACHED"):
        budget.ensure_can_start("browser_step")

    budget = make_budget()
    budget.record_replan()
    with pytest.raises(BudgetExceededError, match="MAX_TASK_REPLANS_REACHED"):
        budget.ensure_can_start("replan")

    budget = make_budget()
    budget.record_context_opened()
    with pytest.raises(BudgetExceededError, match="MAX_BROWSER_CONTEXTS_REACHED"):
        budget.ensure_can_start("browser_context")


def test_budget_detects_runtime_and_repeated_no_progress() -> None:
    budget = make_budget()
    assert budget.note_progress(False) is False
    assert budget.note_progress(False) is True
    assert budget.note_progress(True) is False

    budget.started_at -= 61
    with pytest.raises(BudgetExceededError, match="MAX_RUNTIME_REACHED"):
        budget.ensure_runtime_available()
