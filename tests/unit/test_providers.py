from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from agent_framework import Agent, FunctionTool, Message
from agent_framework.openai import OpenAIChatCompletionClient
from openai import AsyncOpenAI

from web_testing_system.config import BudgetConfig, Settings
from web_testing_system.providers import (
    ProviderUsageMiddleware,
    create_main_chat_client,
    create_tester_chat_client,
)
from web_testing_system.reporting import FinalReportBuilder
from web_testing_system.runtime.computer_use import (
    ComputerUseRequest,
    OpenRouterComputerUseClient,
)
from web_testing_system.state import StateStore


def test_provider_module_imports_in_a_fresh_process() -> None:
    result = subprocess.run([sys.executable, "-c", "import web_testing_system.providers; import web_testing_system.orchestration.runner"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_fixed_openrouter_clients_without_network() -> None:
    settings = Settings(_env_file=None, openrouter_api_key="fake-key")
    for factory, model, provider in ((create_main_chat_client, settings.main_agent_model, settings.main_agent_provider), (create_tester_chat_client, settings.tester_agent_model, settings.tester_agent_provider)):
        client = factory(settings)
        assert isinstance(client, OpenAIChatCompletionClient)
        assert client.model == model
        assert str(client.client.base_url) == "https://openrouter.ai/api/v1/"
        assert client.client.max_retries == 0
        assert client.additional_properties["fixed_provider"] == provider
        assert "model_fallback_stats" not in client.additional_properties


def test_missing_key_and_free_routing_variants_are_rejected() -> None:
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        create_main_chat_client(Settings(_env_file=None))
    for model in ("openrouter/free", "openrouter/auto", "model:free", "model:floor", "~latest"):
        with pytest.raises(ValueError, match="fixed paid"):
            Settings(_env_file=None, main_agent_model=model)


def test_disabled_tools_preserve_semantics_without_unsupported_tool_choice() -> None:
    settings = Settings(_env_file=None, openrouter_api_key="fake-key")
    client = create_main_chat_client(settings)
    tool = FunctionTool(name="read_state", func=lambda: "state")
    prepared = client._prepare_options([Message(role="user", contents=["Review the fixed facts"])], {"tools": [tool], "tool_choice": "none"})
    assert "tools" not in prepared
    assert "tool_choice" not in prepared
    assert prepared["model"] == settings.main_agent_model
    enabled = client._prepare_options([Message(role="user", contents=["Plan"])], {"tools": [tool], "tool_choice": "auto"})
    assert enabled["tools"]
    assert enabled["tool_choice"] == "auto"


@pytest.mark.asyncio
async def test_provider_pin_usage_and_no_automatic_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[dict[str, object]] = []
    fail = False

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        if fail:
            return httpx.Response(503, json={"error": {"message": "endpoint unavailable", "code": 503}})
        return httpx.Response(200, json={"id": "response-1", "object": "chat.completion", "created": 1, "model": "deepseek/deepseek-v4-flash", "provider": "StreamLake", "choices": [{"index": 0, "message": {"role": "assistant", "content": "ready"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 17, "completion_tokens": 5, "completion_tokens_details": {"reasoning_tokens": 3}, "total_tokens": 22, "cost": 0.00023}})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    real_constructor = AsyncOpenAI
    monkeypatch.setattr("web_testing_system.providers.AsyncOpenAI", lambda **kwargs: real_constructor(**kwargs, http_client=http_client))
    store = StateStore(tmp_path / "state.db")
    store.initialize()
    budget = BudgetConfig().model_dump()
    store.create_run(run_id="provider-run", application="http://app.test", application_version="test", test_goal="usage", scope={}, status="RUNNING", global_budget=budget, remaining_budget=budget)
    store.create_budget(budget_id="main-budget", run_id="provider-run")
    settings = Settings(_env_file=None, openrouter_api_key="fake-key")
    usage = ProviderUsageMiddleware(store=store, run_id="provider-run", budget_id="main-budget", provider="openrouter", model=settings.main_agent_model, agent="main")
    client = create_main_chat_client(settings, usage=usage)
    agent = Agent(client=client, default_options={"max_tokens": 64})
    assert (await agent.run("ready?")).text == "ready"
    fail = True
    with pytest.raises(Exception, match="endpoint unavailable"):
        await agent.run("ready?")
    assert len(requests) == 2
    assert all(body["model"] == settings.main_agent_model for body in requests)
    assert all(body["max_tokens"] == 64 and "max_completion_tokens" not in body for body in requests)
    assert all(body["provider"] == {"only": ["streamlake/fp8"], "order": ["streamlake/fp8"], "allow_fallbacks": False, "require_parameters": True} for body in requests)
    events = store.list_events("provider-run", event_types=("LLM_CALL",))
    assert [event["result"]["success"] for event in events] == [True, False]
    assert events[0]["cost"] == 0.00023
    assert events[0]["result"]["served_provider"] == "StreamLake"
    assert events[0]["result"]["reasoning_tokens"] == 3
    assert events[0]["result"]["finish_reasons"] == ["stop"]
    assert events[1]["result"]["reasoning_tokens"] is None
    report = FinalReportBuilder(store).build("provider-run")
    assert report["cost_and_performance"]["llm_input_tokens"] == 17
    assert report["cost_and_performance"]["cost_status"] == "INCOMPLETE"
    await http_client.aclose()


@pytest.mark.asyncio
async def test_openrouter_visual_action_maps_coordinates_and_pins_provider() -> None:
    requests = []

    async def complete(**kwargs: object) -> object:
        requests.append(kwargs)
        call = SimpleNamespace(function=SimpleNamespace(name="visual_action", arguments=json.dumps({"action": "click", "x": 750, "y": 500})))
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=[call]))], usage=SimpleNamespace(prompt_tokens=13, completion_tokens=2, cost=0.001), model="z-ai/glm-5.3-flash")

    settings = Settings(_env_file=None, openrouter_api_key="fake-key", computer_use_model="z-ai/glm-5.3-flash")
    fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=complete)))
    client = OpenRouterComputerUseClient(settings, client=fake)
    png = bytes.fromhex("89504e470d0a1a0a") + bytes(8) + (800).to_bytes(4, "big") + (600).to_bytes(4, "big")
    result = await client.execute(ComputerUseRequest(screenshot=png, goal="Click Project B", url="http://demo.test", allowed_visual_actions=("click",), remaining_budget=1))
    assert (result.action, result.x, result.y) == ("click", 600, 300)
    assert (result.input_tokens, result.output_tokens, result.cost) == (13, 2, 0.001)
    assert requests[0]["extra_body"]["provider"]["only"] == ["relace"]
    assert requests[0]["tool_choice"] == "auto"


@pytest.mark.asyncio
async def test_paid_client_can_complete_more_than_forty_normal_tool_rounds(monkeypatch: pytest.MonkeyPatch) -> None:
    request_count = 0
    tool_count = 0

    def read_state() -> str:
        nonlocal tool_count
        tool_count += 1
        return str(tool_count)

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        message: dict[str, object] = {"role": "assistant", "content": "completed"}
        if request_count <= 45:
            message = {"role": "assistant", "content": None, "tool_calls": [{"id": f"call-{request_count}", "type": "function", "function": {"name": "read_state", "arguments": "{}"}}]}
        return httpx.Response(200, json={"id": f"response-{request_count}", "object": "chat.completion", "created": 1, "model": "z-ai/glm-5.3-flash", "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if request_count <= 45 else "stop"}]})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    real_constructor = AsyncOpenAI
    monkeypatch.setattr("web_testing_system.providers.AsyncOpenAI", lambda **kwargs: real_constructor(**kwargs, http_client=http_client))
    client = create_tester_chat_client(Settings(_env_file=None, openrouter_api_key="fake-key"))
    response = await Agent(client=client, tools=[read_state]).run("Complete the required sequence")
    assert response.text == "completed"
    assert tool_count == 45
    assert request_count == 46
    await http_client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [404, 503, "connection", 400])
async def test_tester_api_fallback_preserves_plan_request_and_records_each_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: int | str) -> None:
    requests: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            if failure == "connection":
                raise httpx.ConnectError("primary unavailable", request=request)
            return httpx.Response(int(failure), json={"error": {"message": "primary unavailable", "code": failure}})
        return httpx.Response(200, json={"id": "backup-response", "object": "chat.completion", "created": 1, "model": body["model"], "provider": "Alibaba", "choices": [{"index": 0, "message": {"role": "assistant", "content": "ready"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 17, "completion_tokens": 5, "total_tokens": 22, "cost": 0.00023}})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    real_constructor = AsyncOpenAI
    monkeypatch.setattr("web_testing_system.providers.AsyncOpenAI", lambda **kwargs: real_constructor(**kwargs, http_client=http_client))
    store = StateStore(tmp_path / "state.db")
    store.initialize()
    limits = BudgetConfig().model_dump()
    store.create_run(run_id="tester-fallback", application="http://app.test", application_version="test", test_goal="plan", scope={}, status="RUNNING", global_budget=limits, remaining_budget=limits)
    store.create_budget(budget_id="tester-budget", run_id="tester-fallback")
    settings = Settings(_env_file=None, openrouter_api_key="fake-key")
    usage = ProviderUsageMiddleware(store=store, run_id="tester-fallback", budget_id="tester-budget", provider="openrouter", model=settings.tester_agent_model, agent="tester")
    agent = Agent(client=create_tester_chat_client(settings, usage=usage), tools=[FunctionTool(name="execute_test_plan", func=lambda: "done")])
    options = {"tool_choice": "auto"}
    if failure == 400:
        with pytest.raises(Exception, match="primary unavailable"):
            await agent.run("Plan", options=options)
        assert len(requests) == 1
    else:
        assert (await agent.run("Plan", options=options)).text == "ready"
        assert (await agent.run("Exceptional replan", options=options)).text == "ready"
        assert len(requests) == 3
        assert all(body["model"] == "qwen/qwen3.8-flash" and body["provider"] == {"allow_fallbacks": True, "require_parameters": True} for body in requests[1:])
    assert requests[0]["model"] == settings.tester_agent_model
    assert requests[0]["provider"]["only"] == ["relace"]
    assert all(body["tool_choice"] == "auto" and [tool["function"]["name"] for tool in body["tools"]] == ["execute_test_plan"] for body in requests)
    assert all("reasoning" not in body and "reasoning_effort" not in body for body in requests)
    events = store.list_events("tester-fallback", event_types=("LLM_CALL",))
    assert len(events) == len(requests)
    assert events[0]["result"]["success"] is False
    assert events[0]["result"]["requested_provider"] == "relace"
    if failure != 400:
        assert all(event["result"]["model"] == "qwen/qwen3.8-flash" and event["result"]["requested_provider"] is None and event["result"]["served_provider"] == "Alibaba" for event in events[1:])
    await http_client.aclose()
