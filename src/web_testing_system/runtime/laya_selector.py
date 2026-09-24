"""Restricted Laya choice selection for unknown legal paths."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from time import perf_counter
from typing import Any, Protocol

from web_testing_system.runtime.models import ActionCandidate, LayaSelection, PageState


class LayaClient(Protocol):
    def predict(
        self, state: Mapping[str, Any], questions: Mapping[str, Any]
    ) -> Mapping[str, Any]: ...


class LayaSelector:
    def __init__(self, client: LayaClient) -> None:
        self.client = client

    @classmethod
    def local(cls) -> LayaSelector:
        from laya import Router  # type: ignore[import-untyped]

        return cls(Router())

    async def select(
        self,
        *,
        current_goal: str,
        page_state: PageState,
        candidates: Sequence[ActionCandidate],
    ) -> LayaSelection:
        started_at = perf_counter()
        candidate_map = {candidate.candidate_id: candidate for candidate in candidates}
        if not candidate_map:
            return LayaSelection(
                selected_candidate_id=None,
                confidence=0,
                latency_ms=0,
                cost=0,
                error="NO_CANDIDATES",
            )
        state = {
            "current_goal": current_goal,
            "page_state": page_state.necessary_summary(),
            "legal_candidates": [
                {
                    "id": candidate.candidate_id,
                    "action": candidate.action,
                    "label": candidate.label,
                    "target": candidate.target,
                }
                for candidate in candidates
            ],
        }
        questions = {
            "next_candidate": {
                "type": "choice",
                "instructions": "Choose the legal candidate that best advances the current goal.",
                "criteria": {
                    candidate.candidate_id: f"{candidate.action}: {candidate.label}"
                    for candidate in candidates
                },
            }
        }
        try:
            raw_result = await asyncio.to_thread(self.client.predict, state, questions)
            answer = raw_result.get("answers", {}).get("next_candidate", {})
            selected_id = answer.get("choice")
            probabilities = answer.get("probabilities", {})
            confidence = (
                float(
                    answer.get(
                        "answer_confidence",
                        probabilities.get(selected_id, answer.get("confidence", 0)),
                    )
                )
                if selected_id is not None
                else 0
            )
            usage = raw_result.get("usage", {})
            cost = float(raw_result.get("cost", usage.get("cost", 0)) or 0)
            error = None if isinstance(selected_id, str) else "NO_SELECTION"
        except Exception as caught_error:
            selected_id = None
            confidence = 0
            cost = 0
            error = f"LAYA_ERROR: {caught_error}"
        latency_ms = (perf_counter() - started_at) * 1_000
        return LayaSelection(
            selected_candidate_id=selected_id,
            confidence=confidence,
            latency_ms=latency_ms,
            cost=cost,
            error=error,
        )
