"""Paid OpenRouter clients, Tester API fallback and actual request accounting."""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from time import perf_counter
from typing import Any
from uuid import uuid4

from agent_framework import (
    BaseChatClient,
    ChatContext,
    ChatMiddleware,
    ChatResponse,
    Message,
)
from agent_framework.openai import OpenAIChatCompletionClient
from openai import APIConnectionError, APIStatusError, AsyncOpenAI
from openai.types.chat import ChatCompletion

from web_testing_system.config import Settings
from web_testing_system.runtime.budget import BudgetExceededError, BudgetGuard
from web_testing_system.state import StateStore

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


def provider_preferences(provider: str) -> dict[str, Any]:
    """Pin exactly one endpoint; never switch provider during an experiment."""
    return {"only": [provider], "order": [provider], "allow_fallbacks": False, "require_parameters": True}


class OpenRouterChatClient(OpenAIChatCompletionClient):
    """Keep OpenRouter's billed cost and served provider when parsing MAF responses."""

    def _prepare_options(self, messages: Sequence[Message], options: Mapping[str, Any]) -> dict[str, Any]:
        prepared = super()._prepare_options(messages, options)
        # 固定端点不接受 tool_choice=none；移除工具声明保持同样的禁用工具语义。
        if prepared.get("tool_choice") == "none":
            prepared.pop("tool_choice")
            prepared.pop("tools", None)
        if "max_completion_tokens" in prepared:
            prepared["max_tokens"] = prepared.pop("max_completion_tokens")
        return prepared

    def _parse_response_from_openai(self, response: ChatCompletion, options: Mapping[str, Any]) -> ChatResponse:
        parsed = super()._parse_response_from_openai(response, options)
        properties = dict(parsed.additional_properties or {})
        properties["cost"] = getattr(response.usage, "cost", None)
        properties["served_provider"] = getattr(response, "provider", None)
        details = getattr(response.usage, "completion_tokens_details", None)
        properties["reasoning_tokens"] = getattr(details, "reasoning_tokens", None)
        properties["finish_reasons"] = [choice.finish_reason for choice in response.choices]
        properties["tool_argument_characters"] = sum(len(call.function.arguments) for choice in response.choices for call in choice.message.tool_calls or [] if call.type == "function")
        parsed.additional_properties = properties
        return parsed


class FixedProviderMiddleware(ChatMiddleware):
    def __init__(self, provider: str) -> None:
        self.provider = provider

    async def process(self, context: ChatContext, call_next: Callable[[], Awaitable[None]]) -> None:
        options = dict(context.options or {})
        options["extra_body"] = {**options.get("extra_body", {}), "provider": provider_preferences(self.provider)}
        context.options = options
        await call_next()


class TesterFallbackMiddleware(ChatMiddleware):
    """Use the Tester backup only after a primary API/connection failure."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.using_backup = False

    async def process(self, context: ChatContext, call_next: Callable[[], Awaitable[None]]) -> None:
        options = dict(context.options or {})
        options["model"] = self.settings.tester_agent_backup_model if self.using_backup else self.settings.tester_agent_model
        provider = self.settings.tester_agent_backup_provider if self.using_backup else self.settings.tester_agent_provider
        preferences = provider_preferences(provider) if provider else {"allow_fallbacks": True, "require_parameters": True}
        options["extra_body"] = {**options.get("extra_body", {}), "provider": preferences}
        context.options = options
        try:
            await call_next()
        except Exception as error:
            cause = getattr(error, "inner_exception", None) or error.__cause__ or error
            api_failure = isinstance(cause, APIConnectionError) or isinstance(cause, APIStatusError) and (cause.status_code in {401, 403, 404, 408, 429} or cause.status_code >= 500)
            if self.using_backup or not api_failure:
                raise
            self.using_backup = True
            options["model"] = self.settings.tester_agent_backup_model
            provider = self.settings.tester_agent_backup_provider
            options["extra_body"] = {**options.get("extra_body", {}), "provider": provider_preferences(provider) if provider else {"allow_fallbacks": True, "require_parameters": True}}
            context.options = options
            context.result = None
            await call_next()


class ProviderUsageMiddleware(ChatMiddleware):
    """Record one actual OpenRouter request, including failed attempts."""

    def __init__(self, *, store: StateStore, run_id: str, budget_id: str, provider: str, model: str, agent: str, fixed_provider: str | None = None, task_id: str | None = None, tester_id: str | None = None, budget: BudgetGuard | None = None) -> None:
        self.store = store
        self.run_id = run_id
        self.budget_id = budget_id
        self.provider = provider
        self.model = model
        self.fixed_provider = fixed_provider
        self.agent = agent
        self.task_id = task_id
        self.tester_id = tester_id
        self.budget = budget
        self.phase = "main_planning" if agent == "main" else "tester"

    async def process(self, context: ChatContext, call_next: Callable[[], Awaitable[None]]) -> None:
        options = dict(context.options or {})
        if self.fixed_provider is not None:
            options["extra_body"] = {**options.get("extra_body", {}), "provider": provider_preferences(self.fixed_provider)}
            context.options = options
        requested_providers = options.get("extra_body", {}).get("provider", {}).get("only", [])
        requested_provider = requested_providers[0] if len(requested_providers) == 1 else None
        run = self.store.get_run(self.run_id)
        if run is None:
            raise KeyError(f"unknown run: {self.run_id}")
        remaining = run["remaining_budget"]
        if self.budget is not None:
            self.budget.ensure_can_start("llm")
        if remaining.get("max_runtime_seconds", 1) <= 0:
            raise BudgetExceededError("MAX_RUNTIME_REACHED")
        if remaining["max_llm_calls"] <= 0:
            raise BudgetExceededError("MAX_LLM_CALLS_REACHED")
        if remaining["max_input_tokens"] <= 0 or remaining["max_output_tokens"] <= 0:
            raise BudgetExceededError("MAX_LLM_TOKENS_REACHED")
        self.store.update_budget(budget_id=self.budget_id, llm_calls=1)
        started_at = perf_counter()
        error_name: str | None = None
        try:
            await call_next()
        except Exception as error:
            error_name = type(error).__name__
            raise
        finally:
            latency_seconds = perf_counter() - started_at
            response = context.result if error_name is None and isinstance(context.result, ChatResponse) else None
            usage = response.usage_details if response is not None else None
            input_tokens = int((usage or {}).get("input_token_count") or 0)
            output_tokens = int((usage or {}).get("output_token_count") or 0)
            properties = response.additional_properties or {} if response is not None else {}
            cost_known = properties.get("cost") is not None
            cost = max(float(properties.get("cost") or 0), 0)
            served_provider = properties.get("served_provider")
            model = str(response.model or (context.options or {}).get("model") or self.model) if response is not None else str((context.options or {}).get("model") or self.model)
            if self.budget is not None:
                self.budget.record_llm_call(input_tokens=input_tokens, output_tokens=output_tokens, runtime_seconds=latency_seconds, cost=cost)
            self.store.update_budget(budget_id=self.budget_id, input_tokens=input_tokens, output_tokens=output_tokens, runtime_seconds=latency_seconds, estimated_cost=cost)
            self.store.append_event(event_id=f"event-{uuid4().hex}", run_id=self.run_id, task_id=self.task_id, tester_id=self.tester_id, event_type="LLM_CALL", tool=self.provider, action="provider_request", result={"agent": self.agent, "phase": self.phase, "provider": self.provider, "model": model, "success": error_name is None, "error_type": error_name, "input_tokens": input_tokens, "output_tokens": output_tokens, "reasoning_tokens": properties.get("reasoning_tokens"), "tool_argument_characters": properties.get("tool_argument_characters"), "finish_reasons": properties.get("finish_reasons"), "usage_source": "provider" if usage is not None else "unavailable", "requested_provider": requested_provider, "served_provider": served_provider, "cost_known": cost_known}, latency_ms=latency_seconds * 1_000, cost=cost)


def _create_chat_client(settings: Settings, *, model: str, provider: str, usage: ProviderUsageMiddleware | None, fallback: TesterFallbackMiddleware | None = None) -> BaseChatClient:
    if settings.openrouter_api_key is None or not settings.openrouter_api_key.get_secret_value():
        raise ValueError("OPENROUTER_API_KEY is required")
    if "/" not in model or ":" in model or model.startswith(("openrouter/", "~")):
        raise ValueError("a fixed paid OpenRouter model ID is required")
    client = AsyncOpenAI(api_key=settings.openrouter_api_key.get_secret_value(), base_url=OPENROUTER_BASE_URL, max_retries=0, timeout=60)
    middleware: list[ChatMiddleware] = [fallback or FixedProviderMiddleware(provider)]
    if usage is not None:
        usage.fixed_provider = None if fallback else provider
        middleware.append(usage)
    chat_client = OpenRouterChatClient(model=model, async_client=client, middleware=middleware)
    chat_client.function_invocation_configuration["max_iterations"] = 500
    chat_client.additional_properties.update({"provider": "openrouter", "fixed_provider": provider})
    return chat_client


def create_main_chat_client(settings: Settings, *, usage: ProviderUsageMiddleware | None = None) -> BaseChatClient:
    return _create_chat_client(settings, model=settings.main_agent_model, provider=settings.main_agent_provider, usage=usage)


def create_tester_chat_client(settings: Settings, *, usage: ProviderUsageMiddleware | None = None) -> BaseChatClient:
    client = _create_chat_client(settings, model=settings.tester_agent_model, provider=settings.tester_agent_provider, usage=usage, fallback=TesterFallbackMiddleware(settings))
    client.additional_properties.update({"backup_model": settings.tester_agent_backup_model, "backup_provider": settings.tester_agent_backup_provider})
    return client
