"""Create the configured real MAF chat clients without exposing API keys."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from time import perf_counter
from typing import Any

from agent_framework import BaseChatClient, ChatContext, ChatMiddleware
from agent_framework.gemini import GeminiChatClient
from agent_framework.openai import OpenAIChatCompletionClient
from google import genai
from google.genai import types as genai_types

from web_testing_system.config import Settings

TEMPORARY_PROVIDER_STATUS_CODES = {429, 500, 502, 503, 504}
logger = logging.getLogger(__name__)


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


def create_main_chat_client(settings: Settings) -> BaseChatClient:
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
    return GeminiChatClient(client=google_client, model=settings.main_agent_model, middleware=[fallback], additional_properties={"model_fallback_stats": stats})


def create_tester_chat_client(settings: Settings) -> BaseChatClient:
    if settings.tester_agent_provider == "gemini":
        return _create_gemini_client(settings, settings.tester_agent_model)
    if settings.groq_api_key is None or not settings.groq_api_key.get_secret_value():
        raise ValueError("GROQ_API_KEY is required")
    if settings.tester_agent_model is None or not settings.tester_agent_model.strip():
        raise ValueError("TESTER_AGENT_MODEL is required")
    return OpenAIChatCompletionClient(model=settings.tester_agent_model, api_key=settings.groq_api_key.get_secret_value(), base_url=settings.groq_base_url)


def _create_gemini_client(settings: Settings, model: str | None) -> BaseChatClient:
    if settings.gemini_api_key is None or not settings.gemini_api_key.get_secret_value():
        raise ValueError("GEMINI_API_KEY is required")
    if model is None or not model.strip():
        raise ValueError("a Gemini model name is required")
    return GeminiChatClient(api_key=settings.gemini_api_key.get_secret_value(), model=model)


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
