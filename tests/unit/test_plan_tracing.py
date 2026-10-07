from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from web_testing_system import observability
from web_testing_system.agents.tester_agent import TesterAgentTools as AgentTools
from web_testing_system.config import RunConfig, Settings
from web_testing_system.evaluation.runner import (
    EvaluationControls,
    EvaluationMode,
    FormalRunExecutor,
    build_evaluation_plan,
    build_execution_route,
)


class TraceClient:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []

    def create_run(self, **kwargs: Any) -> None:
        self.created.append(copy.deepcopy(kwargs))

    def update_run(self, **kwargs: Any) -> None:
        self.updated.append(copy.deepcopy(kwargs))

    def flush(self, **kwargs: Any) -> None:
        return None


@pytest.mark.asyncio
async def test_plan_replan_decisions_actions_have_parentage_and_safe_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = TraceClient()
    monkeypatch.setattr(observability, "Client", lambda **kwargs: backend)
    assignment = SimpleNamespace(run_id="run", task_id="task", tester_id="tester")
    tools = AgentTools(assignment=assignment, runtime=None, store=None)  # type: ignore[arg-type]

    async def execute(goals: list[Any], related: list[str] | None) -> dict[str, Any]:
        replanning = tools.plan_started
        tools.plan_started = True
        with observability.trace_span("Jev", "llm", metadata={"phase": "jev_decision", "check_id": "D08.pending-operation"}):
            pass
        with observability.trace_span("BrowserAction", "tool", metadata={"action": "repeat_submit", "check_id": "D08.pending-operation"}):
            pass
        return {"success": replanning, "reason": None if replanning else "LOW_JEV_CONFIDENCE"}

    monkeypatch.setattr(tools, "_execute_test_plan", execute)
    async with observability.trace_run(Settings(_env_file=None, langsmith_tracing=True, langsmith_api_key="trace-secret"), "run", scenario_id="D08", secrets=("private-input",)):
        with observability.trace_span("Tester", metadata={"task_id": "task"}):
            await tools.execute_test_plan([])
            await tools.execute_test_plan([])
        with observability.trace_span("Reproduction"):
            with observability.trace_span("BrowserAction", "tool"):
                pass
        with observability.trace_span("Verification"):
            with observability.trace_span("BrowserAction", "tool"):
                pass
    roots = [span for span in backend.created if span["name"] == "Run"]
    assert len(roots) == 1
    spans = {span["id"]: span for span in backend.created}
    for name in ("TesterInitialPlan", "TesterExceptionReplan"):
        plan = next(span for span in spans.values() if span["name"] == name)
        assert spans[plan["parent_run_id"]]["name"] == "Tester"
        children = [span for span in spans.values() if span.get("parent_run_id") == plan["id"]]
        assert {span["name"] for span in children} == {"Jev", "BrowserAction"}
        assert all(span["extra"]["metadata"]["case_id"] == "D08" for span in children)
        assert all(span["extra"]["metadata"]["check_id"] == "D08.pending-operation" for span in children)
    initial_id = next(span["id"] for span in backend.created if span["name"] == "TesterInitialPlan")
    initial = next(span for span in backend.updated if str(span.get("run_id")) == str(initial_id))
    assert initial["extra"]["metadata"]["failure_reason"] == "LOW_JEV_CONFIDENCE"
    assert all(span.get("parent_run_id") in spans for span in spans.values() if span.get("parent_run_id"))
    payload = json.dumps([backend.created, backend.updated], default=str)
    assert "trace-secret" not in payload and "private-input" not in payload
    assert all(span["inputs"] == {} for span in spans.values())


@pytest.mark.asyncio
async def test_real_formal_entry_enables_tracing_without_dispatching_models(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = RunConfig(target_url="http://app.test", test_goal="Test", focus_features=["Project"], allowed_scope=["/"], account_references=[{"identity_reference": "admin", "role": "admin", "permissions": ["read"]}], test_data={}, denied_operations=["production"])

    async def fake_run(run_config: RunConfig, settings: Settings, **kwargs: Any) -> Path:
        assert settings.langsmith_tracing is True
        path = settings.artifacts_dir / kwargs["run_id"] / "report.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"test_summary": {"run_status": "COMPLETED"}}), encoding="utf-8")
        return path

    monkeypatch.setattr("web_testing_system.orchestration.runner.run", fake_run)
    monkeypatch.setattr("web_testing_system.evaluation.runner.MetricsCalculator.calculate", lambda self, run_id, **kwargs: {})
    monkeypatch.setattr("web_testing_system.evaluation.runner.FinalReportBuilder.build", lambda self, run_id, **kwargs: {"test_summary": {"run_status": "COMPLETED"}, "task_outcomes": []})
    controls = EvaluationControls(demo_version="demo", seeded_bugs=(), model_configuration=(), token_budget=10, time_budget_seconds=10, accounts=("admin",), initial_data=(), test_scope=("/",))
    variant = build_evaluation_plan(EvaluationMode.TESTER_COUNT, controls).variants[0]
    route = build_execution_route(variant, full_evaluation_enabled=False, fake_provider=True)
    executor = FormalRunExecutor(config, Settings(_env_file=None, langsmith_tracing=False))
    result = await executor.execute(config=variant, route=route, run_number=1, run_id="D01", evidence_directory=tmp_path / "run" / "D01" / "evidence")
    assert result.status == "COMPLETED"
