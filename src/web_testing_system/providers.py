"""Create the configured real MAF chat clients without exposing API keys."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from time import perf_counter
from typing import Any
from uuid import uuid4

from agent_framework import BaseChatClient, ChatContext, ChatMiddleware, ChatResponse
from agent_framework.gemini import GeminiChatClient
from agent_framework.openai import OpenAIChatCompletionClient
from google import genai
from google.genai import types as genai_types
from openai import AsyncOpenAI

from web_testing_system.config import Settings
from web_testing_system.runtime.budget import BudgetExceededError, BudgetGuard
from web_testing_system.state import StateStore

TEMPORARY_PROVIDER_STATUS_CODES = {429, 500, 502, 503, 504}
logger = logging.getLogger(__name__)


class ProviderUsageMiddleware(ChatMiddleware):
    """Record one actual Gemini or Groq request, including failed attempts."""

    def __init__(self, *, store: StateStore, run_id: str, budget_id: str, provider: str, model: str, agent: str, task_id: str | None = None, tester_id: str | None = None, budget: BudgetGuard | None = None) -> None:
        self.store = store
        self.run_id = run_id
        self.budget_id = budget_id
        self.provider = provider
        self.model = model
        self.agent = agent
        self.task_id = task_id
        self.tester_id = tester_id
        self.budget = budget

    async def process(self, context: ChatContext, call_next: Callable[[], Awaitable[None]]) -> None:
        run = self.store.get_run(self.run_id)
        if run is None:
            raise KeyError(f"unknown run: {self.run_id}")
        remaining = run["remaining_budget"]
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
            cost = max(float((response.additional_properties or {}).get("cost", 0) or 0), 0) if response is not None else 0
            model = str(response.model or (context.options or {}).get("model") or self.model) if response is not None else str((context.options or {}).get("model") or self.model)
            if self.budget is not None:
                self.budget.record_llm_call(input_tokens=input_tokens, output_tokens=output_tokens, runtime_seconds=latency_seconds, cost=cost)
            self.store.update_budget(budget_id=self.budget_id, input_tokens=input_tokens, output_tokens=output_tokens, runtime_seconds=latency_seconds, estimated_cost=cost)
            self.store.append_event(event_id=f"event-{uuid4().hex}", run_id=self.run_id, task_id=self.task_id, tester_id=self.tester_id, event_type="LLM_CALL", tool=self.provider, action="provider_request", result={"agent": self.agent, "provider": self.provider, "model": model, "success": error_name is None, "error_type": error_name, "input_tokens": input_tokens, "output_tokens": output_tokens, "usage_source": "provider" if usage is not None else "unavailable"}, latency_ms=latency_seconds * 1_000, cost=cost)


class MainAgentFallbackMiddleware(ChatMiddleware):
    """Retry the primary Gemini model once, then use the configured fallback."""

    def __init__(self, primary_model: str, fallback_model: str, stats: dict[str, Any]) -> None:
        self.primary_model = primary_model
        self.fallback_model = fallback_model
        self.stats = stats

    async def process(self, context: ChatContext, call_next: Callable[[], Awaitable[None]]) -> None:
        active_model = str(self.stats["active_model"])
        self._set_model(context, active_model)
        if active_model == self.fallback_model:
            self._record_attempt(active_model)
            await call_next()
            self.stats["final_model"] = active_model
            return

        started_at = perf_counter()
        primary_error: Exception | None = None
        for attempt in range(2):
            self._record_attempt(self.primary_model)
            try:
                await call_next()
                self.stats["final_model"] = self.primary_model
                return
            except Exception as error:
                status_code = _provider_status_code(error)
                if status_code not in TEMPORARY_PROVIDER_STATUS_CODES:
                    raise
                primary_error = error
                if attempt == 1:
                    break
                self.stats["retry_count"] += 1
                context.result = None
                await asyncio.sleep(1)

        assert primary_error is not None
        self.stats["fallback_count"] += 1
        self.stats["active_model"] = self.fallback_model
        self._set_model(context, self.fallback_model)
        context.result = None
        self._record_attempt(self.fallback_model)
        outcome = "PASS"
        try:
            await call_next()
        except Exception:
            outcome = "FAIL"
            raise
        finally:
            self.stats["final_model"] = self.fallback_model
            event = {
                "original_model": self.primary_model,
                "error": _provider_error_summary(primary_error),
                "retry_count": 1,
                "fallback_model": self.fallback_model,
                "final_model": self.fallback_model,
                "latency_ms": round((perf_counter() - started_at) * 1_000, 1),
                "outcome": outcome,
            }
            self.stats["events"].append(event)
            logger.warning("Main Agent model fallback: %s", event)

    def _record_attempt(self, model: str) -> None:
        self.stats["attempts"] += 1
        self.stats["attempts_by_model"][model] += 1

    @staticmethod
    def _set_model(context: ChatContext, model: str) -> None:
        options = dict(context.options or {})
        options["model"] = model
        context.options = options


def create_main_chat_client(settings: Settings, *, usage: ProviderUsageMiddleware | None = None) -> BaseChatClient:
    if settings.main_agent_provider != "gemini":
        raise ValueError("Main Agent provider must be Gemini")
    if settings.gemini_api_key is None or not settings.gemini_api_key.get_secret_value():
        raise ValueError("GEMINI_API_KEY is required")
    if settings.main_agent_model is None or not settings.main_agent_model.strip():
        raise ValueError("MAIN_AGENT_MODEL is required")
    if settings.main_agent_fallback_model is None or not settings.main_agent_fallback_model.strip():
        raise ValueError("MAIN_AGENT_FALLBACK_MODEL is required")
    stats: dict[str, Any] = {
        "primary_model": settings.main_agent_model,
        "fallback_model": settings.main_agent_fallback_model,
        "active_model": settings.main_agent_model,
        "final_model": settings.main_agent_model,
        "attempts": 0,
        "retry_count": 0,
        "fallback_count": 0,
        "attempts_by_model": {settings.main_agent_model: 0, settings.main_agent_fallback_model: 0},
        "events": [],
    }
    fallback = MainAgentFallbackMiddleware(settings.main_agent_model, settings.main_agent_fallback_model, stats)
    google_client = genai.Client(api_key=settings.gemini_api_key.get_secret_value(), http_options=genai_types.HttpOptions(retry_options=genai_types.HttpRetryOptions(attempts=1)))
    return GeminiChatClient(client=google_client, model=settings.main_agent_model, middleware=[fallback, usage] if usage is not None else [fallback], additional_properties={"model_fallback_stats": stats})


def create_tester_chat_client(settings: Settings, *, usage: ProviderUsageMiddleware | None = None) -> BaseChatClient:
    if settings.tester_agent_provider == "gemini":
        return _create_gemini_client(settings, settings.tester_agent_model, usage=usage)
    if settings.groq_api_key is None or not settings.groq_api_key.get_secret_value():
        raise ValueError("GROQ_API_KEY is required")
    if settings.tester_agent_model is None or not settings.tester_agent_model.strip():
        raise ValueError("TESTER_AGENT_MODEL is required")
    groq_client = AsyncOpenAI(api_key=settings.groq_api_key.get_secret_value(), base_url=settings.groq_base_url, max_retries=0)
    return OpenAIChatCompletionClient(model=settings.tester_agent_model, async_client=groq_client, middleware=[usage] if usage is not None else None)


def _create_gemini_client(settings: Settings, model: str | None, *, usage: ProviderUsageMiddleware | None = None) -> BaseChatClient:
    if settings.gemini_api_key is None or not settings.gemini_api_key.get_secret_value():
        raise ValueError("GEMINI_API_KEY is required")
    if model is None or not model.strip():
        raise ValueError("a Gemini model name is required")
    google_client = genai.Client(api_key=settings.gemini_api_key.get_secret_value(), http_options=genai_types.HttpOptions(retry_options=genai_types.HttpRetryOptions(attempts=1)))
    return GeminiChatClient(client=google_client, model=model, middleware=[usage] if usage is not None else None)


def _provider_status_code(error: Exception) -> int | None:
    current: BaseException | None = error
    while current is not None:
        for name in ("status_code", "code"):
            value: Any = getattr(current, name, None)
            if isinstance(value, int):
                return value
        response = getattr(current, "response", None)
        response_status = getattr(response, "status_code", None)
        if isinstance(response_status, int):
            return response_status
        current = current.__cause__ or current.__context__
    return None


def _provider_error_summary(error: Exception) -> str:
    status_code = _provider_status_code(error)
    current: BaseException = error
    while current.__cause__ is not None:
        current = current.__cause__
    message = str(current).replace("\n", " ")[:300]
    return f"{status_code or 'UNKNOWN'} {type(current).__name__}: {message}"
