from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from web_testing_system.runtime.laya_selector import LayaSelector
from web_testing_system.runtime.models import (
    ActionCandidate,
    InteractiveElement,
    PageState,
)


class FakeLayaClient:
    def __init__(
        self, result: Mapping[str, Any] | None = None, error: Exception | None = None
    ) -> None:
        self.result = result or {}
        self.error = error
        self.state: Mapping[str, Any] | None = None
        self.questions: Mapping[str, Any] | None = None

    def predict(
        self, state: Mapping[str, Any], questions: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        self.state = state
        self.questions = questions
        if self.error is not None:
            raise self.error
        return self.result


def page_state() -> PageState:
    return PageState(
        state_id="state-1",
        url="https://app.test/projects",
        load_state="complete",
        visible_dom="Projects Open",
        accessibility="- button Open",
        interactive_elements=(
            InteractiveElement(kind="button", label="Open", target="#open"),
        ),
    )


def candidates() -> list[ActionCandidate]:
    return [
        ActionCandidate(
            candidate_id="candidate-1",
            action="click",
            label="Open",
            target="#open",
            state_id="state-1",
        )
    ]


@pytest.mark.asyncio
async def test_laya_receives_only_goal_page_summary_and_legal_candidate_ids() -> None:
    client = FakeLayaClient(
        {
            "answers": {
                "next_candidate": {
                    "choice": "candidate-1",
                    "probabilities": {"candidate-1": 0.92},
                    "answer_confidence": 0.89,
                }
            },
            "usage": {"input_tokens": 20, "output_tokens": 1},
            "cost": 0.002,
        }
    )

    selection = await LayaSelector(client).select(
        current_goal="Open a project", page_state=page_state(), candidates=candidates()
    )

    assert selection.selected_candidate_id == "candidate-1"
    assert selection.confidence == 0.89
    assert selection.cost == 0.002
    assert set(client.state or {}) == {"current_goal", "page_state", "legal_candidates"}
    criteria = (client.questions or {})["next_candidate"]["criteria"]
    assert set(criteria) == {"candidate-1"}


@pytest.mark.asyncio
async def test_laya_failure_is_a_fallback_result_not_an_action() -> None:
    selection = await LayaSelector(
        FakeLayaClient(error=RuntimeError("offline"))
    ).select(
        current_goal="Open a project", page_state=page_state(), candidates=candidates()
    )

    assert selection.selected_candidate_id is None
    assert selection.confidence == 0
    assert selection.error == "LAYA_ERROR: offline"
