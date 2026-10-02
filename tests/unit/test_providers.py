from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, call

import pytest
from agent_framework import ChatResponse
from agent_framework.gemini import GeminiChatClient
from agent_framework.openai import OpenAIChatCompletionClient

from web_testing_system.config import BudgetConfig, Settings
from web_testing_system.evaluation.metrics import MetricsCalculator
from web_testing_system.providers import (
    MainAgentFallbackMiddleware,
    ProviderUsageMiddleware,
    create_main_chat_client,
    create_tester_chat_client,
)
from web_testing_system.reporting import FinalReportBuilder
from web_testing_system.runtime.budget import BudgetGuard, BudgetLimits
from web_testing_system.runtime.computer_use import (
    ComputerUseRequest,
    GeminiComputerUseClient,
)
from web_testing_system.state import StateStore


def test_real_provider_factories_use_configured_models_without_calling_network() -> None:
    settings = Settings(_env_file=None, gemini_api_key="fake-gemini-key", groq_api_key="fake-groq-key", main_agent_model="gemini-primary-model", main_agent_fallback_model="gemini-fallback-model", tester_agent_provider="groq", tester_agent_model="groq-test-model")

    main_client = create_main_chat_client(settings)

    assert isinstance(main_client, GeminiChatClient)
    stats = main_client.additional_properties["model_fallback_stats"]
    assert stats["primary_model"] == "gemini-primary-model"
    assert stats["fallback_model"] == "gemini-fallback-model"
    assert stats["attempts"] == 0
    assert main_client._genai_client._api_client._http_options.retry_options.attempts == 1
    tester_client = create_tester_chat_client(settings)
    assert isinstance(tester_client, OpenAIChatCompletionClient)
    assert tester_client.client.max_retries == 0


def test_real_provider_factories_require_keys_and_model_names() -> None:
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        create_main_chat_client(Settings(_env_file=None, main_agent_model="gemini-test-model", main_agent_fallback_model="gemini-fallback-model"))
    with pytest.raises(ValueError, match="MAIN_AGENT_FALLBACK_MODEL"):
        create_main_chat_client(Settings(_env_file=None, gemini_api_key="fake-gemini-key", main_agent_model="gemini-test-model"))
    with pytest.raises(ValueError, match="TESTER_AGENT_MODEL"):
        create_tester_chat_client(Settings(_env_file=None, tester_agent_provider="groq", groq_api_key="fake-groq-key"))


@pytest.mark.asyncio
async def test_gemini_computer_use_client_maps_normalized_click_to_pixels() -> None:
    class FakeModels:
        async def generate_content(self, **kwargs: object) -> object:
            del kwargs
            function_call = SimpleNamespace(name="click", args={"x": 750, "y": 500})
            part = SimpleNamespace(function_call=function_call)
            return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[part]))], usage_metadata=SimpleNamespace(prompt_token_count=13, candidates_token_count=2), model_version="gemini-provider-model")

    fake_client = SimpleNamespace(aio=SimpleNamespace(models=FakeModels()))
    settings = Settings(_env_file=None, gemini_api_key="fake-gemini-key", computer_use_model="gemini-test-model")
    client = GeminiComputerUseClient(settings, client=fake_client)
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + (800).to_bytes(4, "big") + (600).to_bytes(4, "big")

    result = await client.execute(ComputerUseRequest(screenshot=png, goal="Click Project B", url="http://demo.test", allowed_visual_actions=("click",), remaining_budget=1))

    assert result.status == "ACTION"
    assert result.action == "click"
    assert result.x == 600
    assert result.y == 300
    assert (result.input_tokens, result.output_tokens, result.model) == (13, 2, "gemini-provider-model")


@pytest.mark.asyncio
async def test_main_agent_fallback_is_bounded_and_only_handles_temporary_statuses(monkeypatch: pytest.MonkeyPatch) -> None:
    class ProviderError(Exception):
        def __init__(self, status_code: int) -> None:
            self.status_code = status_code

    sleep = AsyncMock()
    monkeypatch.setattr("web_testing_system.providers.asyncio.sleep", sleep)
    stats: dict[str, Any] = {
        "primary_model": "primary",
        "fallback_model": "fallback",
        "active_model": "primary",
        "final_model": "primary",
        "attempts": 0,
        "retry_count": 0,
        "fallback_count": 0,
        "attempts_by_model": {"primary": 0, "fallback": 0},
        "events": [],
    }
    middleware = MainAgentFallbackMiddleware("primary", "fallback", stats)
    context = cast(Any, SimpleNamespace(result="stale", options={}))
    models: list[str] = []

    async def temporary_then_fallback() -> None:
        model = str(context.options["model"])
        models.append(model)
        if model == "primary":
            raise ProviderError(503)
        context.result = "fallback-response"

    await middleware.process(context, temporary_then_fallback)

    assert models == ["primary", "primary", "fallback"]
    assert context.result == "fallback-response"
    assert stats["attempts"] == 3
    assert stats["retry_count"] == 1
    assert stats["fallback_count"] == 1
    assert stats["final_model"] == "fallback"
    assert stats["attempts_by_model"] == {"primary": 2, "fallback": 1}
    assert stats["events"][0]["original_model"] == "primary"
    assert stats["events"][0]["fallback_model"] == "fallback"
    assert stats["events"][0]["outcome"] == "PASS"
    assert sleep.await_args_list == [call(1)]

    async def permanent_failure() -> None:
        raise ProviderError(400)

    sleep.reset_mock()
    permanent_stats = {
        "primary_model": "primary",
        "fallback_model": "fallback",
        "active_model": "primary",
        "final_model": "primary",
        "attempts": 0,
        "retry_count": 0,
        "fallback_count": 0,
        "attempts_by_model": {"primary": 0, "fallback": 0},
        "events": [],
    }
    permanent_middleware = MainAgentFallbackMiddleware("primary", "fallback", permanent_stats)
    with pytest.raises(ProviderError):
        await permanent_middleware.process(context, permanent_failure)
    assert permanent_stats["attempts"] == 1
    assert permanent_stats["fallback_count"] == 0
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_provider_attempts_use_returned_usage_and_reach_metrics_and_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class TemporaryError(Exception):
        status_code = 503

    store = StateStore(tmp_path / "state.db")
    store.initialize()
    budget = BudgetConfig().model_dump()
    store.create_run(run_id="provider-run", application="http://app.test", application_version="test", test_goal="usage", scope={}, status="RUNNING", global_budget=budget, remaining_budget=budget)
    store.create_budget(budget_id="main-budget", run_id="provider-run")
    local_budget = BudgetGuard(BudgetLimits(max_runtime_seconds=60, max_llm_calls=20, max_input_tokens=100, max_output_tokens=100, max_jev_calls=0, max_computer_use_calls=0, max_task_steps=1, max_task_replans=0, max_browser_contexts=1))
    usage = ProviderUsageMiddleware(store=store, run_id="provider-run", budget_id="main-budget", provider="gemini", model="primary", agent="main", budget=local_budget)
    stats: dict[str, Any] = {"active_model": "primary", "final_model": "primary", "attempts": 0, "retry_count": 0, "fallback_count": 0, "attempts_by_model": {"primary": 0, "fallback": 0}, "events": []}
    fallback = MainAgentFallbackMiddleware("primary", "fallback", stats)
    context = cast(Any, SimpleNamespace(result=None, options={}))
    attempted_models: list[str] = []
    monkeypatch.setattr("web_testing_system.providers.asyncio.sleep", AsyncMock())

    async def provider_request() -> None:
        model = str(context.options["model"])
        attempted_models.append(model)
        if model == "primary":
            raise TemporaryError("temporary")
        context.result = ChatResponse(model="provider-fallback-model", usage_details={"input_token_count": 17, "output_token_count": 5})

    async def recorded_request() -> None:
        await usage.process(context, provider_request)

    await fallback.process(context, recorded_request)

    assert attempted_models == ["primary", "primary", "fallback"]
    recorded = store.get_budget("main-budget")
    assert recorded is not None
    assert (recorded["llm_calls"], recorded["input_tokens"], recorded["output_tokens"]) == (3, 17, 5)
    assert (local_budget.usage.llm_calls, local_budget.usage.input_tokens, local_budget.usage.output_tokens) == (3, 17, 5)
    run = store.get_run("provider-run")
    assert run is not None and run["remaining_budget"]["max_llm_calls"] == budget["max_llm_calls"] - 3
    events = [event for event in store.list_events("provider-run") if event["event_type"] == "LLM_CALL"]
    assert sum(event["result"]["success"] is False for event in events) == 2
    assert sum(event["result"]["success"] is True for event in events) == 1
    assert any(event["result"]["model"] == "provider-fallback-model" for event in events)
    metrics = MetricsCalculator(store).calculate("provider-run")
    report = FinalReportBuilder(store).build("provider-run")
    assert (metrics["llm_request_count"], metrics["llm_input_tokens"], metrics["llm_output_tokens"]) == (3, 17, 5)
    assert report["cost_and_performance"]["llm_models"] == ["primary", "provider-fallback-model"]
    assert report["cost_and_performance"]["llm_failure_count"] == 2
