from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, call

import pytest
from agent_framework.gemini import GeminiChatClient
from agent_framework.openai import OpenAIChatCompletionClient

from web_testing_system.config import Settings
from web_testing_system.providers import (
    MainAgentFallbackMiddleware,
    create_main_chat_client,
    create_tester_chat_client,
)
from web_testing_system.runtime.computer_use import (
    ComputerUseRequest,
    GeminiComputerUseClient,
)


def test_real_provider_factories_use_configured_models_without_calling_network() -> None:
    settings = Settings(_env_file=None, gemini_api_key="fake-gemini-key", groq_api_key="fake-groq-key", main_agent_model="gemini-primary-model", main_agent_fallback_model="gemini-fallback-model", tester_agent_provider="groq", tester_agent_model="groq-test-model")

    main_client = create_main_chat_client(settings)

    assert isinstance(main_client, GeminiChatClient)
    stats = main_client.additional_properties["model_fallback_stats"]
    assert stats["primary_model"] == "gemini-primary-model"
    assert stats["fallback_model"] == "gemini-fallback-model"
    assert stats["attempts"] == 0
    assert main_client._genai_client._api_client._http_options.retry_options.attempts == 1
    assert isinstance(create_tester_chat_client(settings), OpenAIChatCompletionClient)


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
            return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[part]))])

    fake_client = SimpleNamespace(aio=SimpleNamespace(models=FakeModels()))
    settings = Settings(_env_file=None, gemini_api_key="fake-gemini-key", computer_use_model="gemini-test-model")
    client = GeminiComputerUseClient(settings, client=fake_client)
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + (800).to_bytes(4, "big") + (600).to_bytes(4, "big")

    result = await client.execute(ComputerUseRequest(screenshot=png, goal="Click Project B", url="http://demo.test", allowed_visual_actions=("click",), remaining_budget=1))

    assert result.status == "ACTION"
    assert result.action == "click"
    assert result.x == 600
    assert result.y == 300


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
