"""Choose one legal candidate with Jev through OpenRouter Decisions API."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from time import perf_counter
from typing import Any, Protocol
from urllib.request import Request, urlopen

from web_testing_system.config import Settings
from web_testing_system.runtime.models import ActionCandidate, JevSelection, PageState

DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"


class JevClient(Protocol):
    def predict(self, state: Mapping[str, Any], questions: Mapping[str, Any]) -> Mapping[str, Any]: ...


class OpenRouterJevClient:
    def __init__(self, settings: Settings) -> None:
        if settings.openrouter_api_key is None or not settings.openrouter_api_key.get_secret_value():
            raise ValueError("OPENROUTER_API_KEY is required for Jev")
        self.api_key = settings.openrouter_api_key.get_secret_value()
        self.model = settings.jev_model

    def predict(self, state: Mapping[str, Any], questions: Mapping[str, Any]) -> Mapping[str, Any]:
        payload = json.dumps({"model": self.model, "state": state, "questions": questions}).encode("utf-8")
        request = Request(DECISIONS_URL, data=payload, headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}, method="POST")
        with urlopen(request, timeout=20) as response:
            result = json.load(response)
        if not isinstance(result, dict):
            raise ValueError("OpenRouter returned an invalid decision response")
        return result


class JevSelector:
    def __init__(self, client: JevClient) -> None:
        self.client = client

    @classmethod
    def openrouter(cls, settings: Settings) -> JevSelector:
        return cls(OpenRouterJevClient(settings))

    async def select(self, *, current_goal: str, page_state: PageState, candidates: Sequence[ActionCandidate]) -> JevSelection:
        started_at = perf_counter()
        if not candidates:
            return JevSelection(selected_candidate_id=None, confidence=0, latency_ms=0, cost=0, error="NO_CANDIDATES")
        state = {
            "current_goal": current_goal,
            "page_state": {**page_state.selection_summary(), "text": page_state.visible_dom[:2500]},
            "legal_candidates": [{"id": candidate.candidate_id, "action": candidate.action, "label": candidate.label, "target": candidate.target, "value_reference": candidate.value_reference} for candidate in candidates],
        }
        questions = {"next_candidate": {"type": "choice", "instructions": "Choose the legal candidate that best advances `current_goal`.", "criteria": {candidate.candidate_id: f"{candidate.action}: {candidate.label}" for candidate in candidates}}}
        try:
            response = await asyncio.to_thread(self.client.predict, state, questions)
            answer = response.get("answers", {}).get("next_candidate", {})
            selected_id = answer.get("choice")
            confidence = float(answer.get("confidence", 0)) if isinstance(selected_id, str) else 0
            usage = response.get("usage", {})
            cost = float(usage.get("cost", 0) or 0)
            input_tokens = int(usage.get("input_tokens", 0) or 0)
            output_tokens = int(usage.get("output_tokens", 0) or 0)
            model = str(response.get("model", ""))
            error = None if isinstance(selected_id, str) else "NO_SELECTION"
        except Exception as caught_error:
            selected_id = None
            confidence = 0
            cost = 0
            input_tokens = 0
            output_tokens = 0
            model = None
            error = f"JEV_ERROR: {type(caught_error).__name__}"
        latency_ms = (perf_counter() - started_at) * 1_000
        return JevSelection(selected_candidate_id=selected_id, confidence=confidence, latency_ms=latency_ms, cost=cost, input_tokens=input_tokens, output_tokens=output_tokens, model=model, error=error)
