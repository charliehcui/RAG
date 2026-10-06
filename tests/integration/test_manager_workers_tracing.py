from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from agent_framework import BaseChatClient, ChatResponse, Content, Message
from agent_framework._middleware import ChatMiddlewareLayer
from agent_framework._tools import FunctionInvocationLayer

from demo_app.app import DemoAppServer
from tests.integration import test_architecture_fixes
from tests.integration.test_architecture_fixes import (
    FakeJev,
    PlanningClient,
    config,
    task,
)
from web_testing_system import observability
from web_testing_system.config import BudgetConfig, Settings
from web_testing_system.orchestration.runner import run
from web_testing_system.runtime.jev_selector import JevSelector
from web_testing_system.state import StateStore


class RecordingTraceClient:
    def __init__(self, *, failure: str = "", **kwargs: Any) -> None:
        self.failure = failure
        self.created: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []
        self.flushed = False

    def create_run(self, **kwargs: Any) -> None:
        if self.failure == "post":
            raise ConnectionError("secret must never reach the error trace")
        self.created.append(copy.deepcopy(kwargs))

    def update_run(self, **kwargs: Any) -> None:
        if self.failure == "patch":
            raise ConnectionError("secret must never reach the error trace")
        self.updated.append(copy.deepcopy(kwargs))

    def flush(self, timeout: float | None = None) -> None:
        self.flushed = True
        if self.failure == "flush":
            raise ConnectionError("secret must never reach the error trace")


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["", "initialize", "post", "patch", "flush"])
async def test_three_workers_share_sqlite_and_tracing_is_nonfatal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    backend = RecordingTraceClient(failure=failure)

    def trace_client(**kwargs: Any) -> RecordingTraceClient:
        assert kwargs["hide_inputs"] is True and kwargs["hide_outputs"] is True
        if failure == "initialize":
            raise ConnectionError("sensitive trace API failure")
        return backend

    monkeypatch.setattr(observability, "Client", trace_client)
    started = 0
    all_started = asyncio.Event()
    progress_written = 0
    all_progress_written = asyncio.Event()
    worker_sessions: set[int] = set()

    class WorkerClient(FunctionInvocationLayer, ChatMiddlewareLayer, BaseChatClient):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def _inner_get_response(self, *, messages: Sequence[Message], stream: bool, options: Mapping[str, Any], **kwargs: Any) -> ChatResponse:
            nonlocal started, progress_written
            self.calls += 1
            if self.calls == 1:
                started += 1
                worker_sessions.add(id(self))
                if started == 3:
                    all_started.set()
                await asyncio.wait_for(all_started.wait(), timeout=5)
                name, arguments = "update_task_progress", {"progressed": True, "summary": "Worker started its assigned task"}
            elif self.calls == 2:
                progress_written += 1
                if progress_written == 3:
                    all_progress_written.set()
                await asyncio.wait_for(all_progress_written.wait(), timeout=5)
                name, arguments = "read_shared_facts", {}
            elif self.calls == 3:
                results = [item for message in messages for item in message.contents if item.type == "function_result"]
                assert any("task-0" in str(item.result) and "task-2" in str(item.result) for item in results)
                name, arguments = "execute_known_action", {"action_type": "assertion", "target": "#login-form", "assertion": "visible", "goal_check": True}
            elif self.calls == 4:
                name, arguments = "finish_task", {}
            else:
                raise AssertionError("No extra Worker Done call is allowed")
            return ChatResponse(messages=[Message(role="assistant", contents=[Content.from_function_call(f"worker-{self.calls}", name, arguments=arguments)])], usage_details={"input_token_count": 3, "output_token_count": 1})

    main_client = PlanningClient([task(f"task-{number}") for number in range(3)])
    settings = Settings(_env_file=None, langsmith_tracing=True, langsmith_api_key="trace-api-secret", state_db_path=tmp_path / "state.db", artifacts_dir=tmp_path / "runs", temporary_sensitive_dir=tmp_path / "temporary")
    with DemoAppServer() as app:
        run_config = config(app.base_url)
        run_config.budget = BudgetConfig()
        run_config.test_data = {"email": "private-test-value@example.test"}
        report_path = await run(run_config, settings, run_id="trace-worker-smoke", scenario_id="three-workers", main_client=main_client, tester_client_factory=WorkerClient, jev_selector=JevSelector(FakeJev()))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert run_config.budget.max_testers == run_config.budget.max_parallel_browser_contexts == 3
    assert started == len(worker_sessions) == 3
    assert report["test_summary"]["peak_concurrent_testers"] == 3
    assert report["test_summary"]["tester_count"] == 3
    assert report["test_summary"]["configured_tester_capacity"] == 3
    assert report["test_summary"]["successful_tasks"] == 3
    assert report["test_summary"]["run_status"] == "COMPLETED"
    assert main_client.calls == 5 and main_client.summary_calls == 1
    assert main_client.summary_facts["task_outcomes"] == report["task_outcomes"]
    assert report_path.with_name("summary.md").read_text(encoding="utf-8").startswith("Tested 3 tasks; 3 passed")
    assert report["cost_and_performance"]["llm_by_phase"]["main_final_summary"]["requests"] == 1
    store = StateStore(settings.state_db_path)
    testers = store.list_testers("trace-worker-smoke")
    assert len({item["session_reference"] for item in testers}) == 3
    events = store.list_events("trace-worker-smoke", event_types=("TASK_STARTED",))
    assert len({item["browser_session_id"] for item in events}) == 3
    payload = json.dumps([backend.created, backend.updated], default=str)
    assert "trace-api-secret" not in payload
    assert "private-test-value@example.test" not in payload
    assert "secret must never" not in payload
    if not failure:
        assert backend.flushed
        roots = [span for span in backend.created if span["name"] == "Run"]
        assert len(roots) == 1
        stages = [span for span in backend.created if span.get("parent_run_id") == roots[0]["id"]]
        assert sorted(span["name"] for span in stages) == ["MainFinalSummary", "MainPlanning", "Tester", "Tester", "Tester"]
        assert sum(span["run_type"] == "llm" for span in backend.created) == 17
        assert all(span["inputs"] == {} for span in backend.created)
        assert all(span.get("outputs") in ({}, None) for span in backend.updated)
        summary_span = next(span for span in backend.created if span["name"] == "MainFinalSummary")
        assert sum(span.get("parent_run_id") == summary_span["id"] and span["run_type"] == "llm" for span in backend.created) == 1


@pytest.mark.asyncio
async def test_trace_error_contains_only_exception_type(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = RecordingTraceClient()
    monkeypatch.setattr(observability, "Client", lambda **kwargs: backend)
    with pytest.raises(ValueError):
        async with observability.trace_run(Settings(_env_file=None, langsmith_tracing=True, langsmith_api_key="trace-api-secret"), "safe-run"):
            with observability.trace_span("BrowserAction", "tool"):
                raise ValueError("password=do-not-upload-this-value")
    assert [span["error"] for span in backend.updated] == ["ValueError", "ValueError"]
    assert "do-not-upload" not in json.dumps(backend.updated, default=str)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_trace_covers_jev_evidence_reproduction_and_verification(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    backend = RecordingTraceClient()
    monkeypatch.setattr(observability, "Client", lambda **kwargs: backend)
    original_settings = test_architecture_fixes.settings
    monkeypatch.setattr(test_architecture_fixes, "settings", lambda path: original_settings(path).model_copy(update={"langsmith_tracing": True, "langsmith_api_key": Settings(_env_file=None, langsmith_api_key="trace-api-secret").langsmith_api_key}))
    await test_architecture_fixes.test_formal_entry_reproduces_verifies_and_reports_seeded_bug(tmp_path, monkeypatch)
    names = {span["name"] for span in backend.created}
    assert {"MainPlanning", "Tester", "Jev", "Evidence", "Reproduction", "Verification", "MainFinalSummary"} <= names
    for stage in ("Reproduction", "Verification"):
        span = next(span for span in backend.created if span["name"] == stage)
        assert span["extra"]["metadata"]["finding_id"]
        assert any(child.get("parent_run_id") == span["id"] and child["name"] == "BrowserAction" for child in backend.created)
    assert sum(span["run_type"] == "llm" for span in backend.created) == 19
    payload = json.dumps([backend.created, backend.updated], default=str)
    assert "demo-member" not in payload and "trace-api-secret" not in payload
