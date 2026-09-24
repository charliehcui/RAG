"""Controlled state tools intended for later Microsoft Agent Framework tool calling."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from web_testing_system.state.store import StateStore


def build_data_namespace(run_id: str, tester_id: str, task_id: str) -> str:
    return f"run-{run_id}-tester-{tester_id}-task-{task_id}"


class StateTools:
    """Thin structured-result methods over the allowed shared-state operations."""

    def __init__(self, store: StateStore) -> None:
        self.store = store

    def append_event(self, *, event_id: str, run_id: str, event_type: str, action: str, result: Mapping[str, Any], latency_ms: float, cost: float = 0, task_id: str | None = None, tester_id: str | None = None, browser_session_id: str | None = None, url: str | None = None, tool: str | None = None, evidence_references: Sequence[str] = ()) -> dict[str, Any]:
        event = self.store.append_event(event_id=event_id, run_id=run_id, event_type=event_type, action=action, result=result, latency_ms=latency_ms, cost=cost, task_id=task_id, tester_id=tester_id, browser_session_id=browser_session_id, url=url, tool=tool, evidence_references=evidence_references)
        return {"ok": True, "event": event}

    def record_coverage(self, *, run_id: str, feature: str, page: str, state: str, action: str, result: str, last_tester: str) -> dict[str, Any]:
        path = self.store.record_path(run_id=run_id, feature=feature, page=page, state=state, action=action, result=result, last_tester=last_tester)
        return {"ok": True, "coverage": path}

    def register_resource(self, *, resource_id: str, run_id: str, resource_type: str, owner_task: str, owner: str, participants: Sequence[str], allowed_operations: Sequence[str], sharing_mode: str, cleanup_status: str) -> dict[str, Any]:
        resource = self.store.create_resource(resource_id=resource_id, run_id=run_id, resource_type=resource_type, owner_task=owner_task, owner=owner, participants=participants, allowed_operations=allowed_operations, sharing_mode=sharing_mode, cleanup_status=cleanup_status)
        return {"ok": True, "resource": resource}

    def update_budget(self, *, budget_id: str, llm_calls: int = 0, jev_calls: int = 0, computer_use_calls: int = 0, input_tokens: int = 0, output_tokens: int = 0, browser_steps: int = 0, runtime_seconds: float = 0, estimated_cost: float = 0) -> dict[str, Any]:
        budget = self.store.update_budget(budget_id=budget_id, llm_calls=llm_calls, jev_calls=jev_calls, computer_use_calls=computer_use_calls, input_tokens=input_tokens, output_tokens=output_tokens, browser_steps=browser_steps, runtime_seconds=runtime_seconds, estimated_cost=estimated_cost)
        return {"ok": True, "budget": budget}
