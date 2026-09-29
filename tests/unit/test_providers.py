from __future__ import annotations

from types import SimpleNamespace

import pytest
from agent_framework.gemini import GeminiChatClient
from agent_framework.openai import OpenAIChatCompletionClient

from web_testing_system.config import Settings
from web_testing_system.providers import (
    create_main_chat_client,
    create_tester_chat_client,
)
from web_testing_system.runtime.computer_use import (
    ComputerUseRequest,
    GeminiComputerUseClient,
)


def test_real_provider_factories_use_configured_models_without_calling_network() -> None:
    settings = Settings(_env_file=None, gemini_api_key="fake-gemini-key", groq_api_key="fake-groq-key", main_agent_model="gemini-test-model", tester_agent_provider="groq", tester_agent_model="groq-test-model")

    assert isinstance(create_main_chat_client(settings), GeminiChatClient)
    assert isinstance(create_tester_chat_client(settings), OpenAIChatCompletionClient)


def test_real_provider_factories_require_keys_and_model_names() -> None:
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        create_main_chat_client(Settings(_env_file=None, main_agent_model="gemini-test-model"))
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
