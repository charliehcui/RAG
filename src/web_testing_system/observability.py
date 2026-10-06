"""LangSmith SDK spans with metadata only; tracing never controls execution."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Iterator,
    Mapping,
    Sequence,
)
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from typing import Any, Literal

from agent_framework import (
    ChatContext,
    ChatMiddleware,
    ChatResponse,
    FunctionInvocationContext,
    FunctionMiddleware,
    MiddlewareTermination,
)
from langsmith import Client, get_tracing_context, trace, tracing_context
from langsmith.run_trees import RunTree

from web_testing_system.config import Settings
from web_testing_system.security import redact_sensitive_data

logger = logging.getLogger(__name__)
_trace_secrets: ContextVar[tuple[str, ...]] = ContextVar("trace_secrets", default=())
_METADATA_KEYS = frozenset({"run_id", "scenario_id", "agent_role", "tester_id", "model", "provider", "main_model", "main_provider", "tester_model", "tester_provider", "task_id", "finding_id", "phase", "action", "success", "status", "error_type", "input_tokens", "output_tokens", "total_tokens", "total_cost", "cost_known", "ls_provider", "ls_model_name", "llm_request_count"})


def safe_metadata(values: Mapping[str, Any]) -> dict[str, Any]:
    result = {}
    for key, value in redact_sensitive_data(values).items():
        if key not in _METADATA_KEYS or not isinstance(value, (str, int, float, bool, type(None))):
            continue
        if isinstance(value, str):
            for secret in _trace_secrets.get():
                value = value.replace(secret, "[REDACTED]")
        result[key] = value
    return result


@contextmanager
def trace_span(name: str, run_type: Literal["chain", "llm", "tool"] = "chain", *, metadata: Mapping[str, Any] | None = None) -> Iterator[RunTree | None]:
    context = get_tracing_context()
    if context.get("enabled") is not True:
        yield None
        return
    merged = safe_metadata({**(context.get("metadata") or {}), **(metadata or {})})
    # SDK 上下文在上传或更新失败后仍会恢复父节点。
    with tracing_context(**{**context, "metadata": merged}):
        span = None
        try:
            manager = trace(name, run_type=run_type, inputs={}, metadata=merged)
            span = manager.__enter__()
        except Exception as error:
            logger.warning("LangSmith span unavailable: %s", type(error).__name__)
        try:
            yield span
        except BaseException as error:
            if span is not None and not isinstance(error, MiddlewareTermination):
                try:
                    span.end(error=type(error).__name__)
                except Exception:
                    pass
            raise
        finally:
            if span is not None:
                try:
                    span.end()
                    manager.__exit__(None, None, None)
                except Exception as error:
                    logger.warning("LangSmith span upload failed: %s", type(error).__name__)


def trace_result(span: RunTree | None, *, input_tokens: int = 0, output_tokens: int = 0, cost: float | None = None, **metadata: Any) -> None:
    if span is None:
        return
    try:
        sanitized = safe_metadata(metadata)
        span.add_metadata(sanitized)
        if sanitized.get("success") is False:
            span.error = str(sanitized.get("error_type") or "OPERATION_FAILED")
        if span.run_type == "llm":
            usage: Any = {"input_tokens": input_tokens, "output_tokens": output_tokens, "total_tokens": input_tokens + output_tokens}
            if cost is not None:
                usage["total_cost"] = cost
            span.add_metadata({"usage_metadata": usage})
    except Exception as error:
        logger.warning("LangSmith metadata unavailable: %s", type(error).__name__)


@asynccontextmanager
async def trace_run(settings: Settings, run_id: str, *, scenario_id: str | None = None, secrets: Sequence[str] = ()) -> AsyncIterator[None]:
    client = None
    key = settings.langsmith_api_key
    registered = list(secrets)
    for credential in (settings.openrouter_api_key, key):
        if credential is not None:
            registered.append(credential.get_secret_value())
    secret_token = _trace_secrets.set(tuple(value for value in registered if value))
    if settings.langsmith_tracing and key is not None and key.get_secret_value():
        try:
            client = Client(api_url=settings.langsmith_endpoint, api_key=key.get_secret_value(), timeout_ms=2_000, hide_inputs=True, hide_outputs=True, omit_traced_runtime_info=True)
        except Exception as error:
            logger.warning("LangSmith unavailable: %s", type(error).__name__)
    try:
        with tracing_context(enabled=client is not None, client=client, project_name=settings.langsmith_project, parent=False, metadata=safe_metadata({"run_id": run_id, "scenario_id": scenario_id or run_id, "agent_role": "orchestration", "main_model": settings.main_agent_model, "main_provider": settings.main_agent_provider, "tester_model": settings.tester_agent_model, "tester_provider": settings.tester_agent_provider})):
            with trace_span("Run"):
                yield
    finally:
        _trace_secrets.reset(secret_token)
        if client is not None:
            try:
                await asyncio.to_thread(client.flush, timeout=2)
            except Exception as error:
                logger.warning("LangSmith flush failed: %s", type(error).__name__)


class TraceChatMiddleware(ChatMiddleware):
    """Trace each actual model round without sending messages or responses."""

    async def process(self, context: ChatContext, call_next: Callable[[], Awaitable[None]]) -> None:
        with trace_span("LLM", "llm") as span:
            await call_next()
            try:
                response = context.result
                if isinstance(response, ChatResponse):
                    usage = response.usage_details or {}
                    properties = response.additional_properties or {}
                    trace_result(span, input_tokens=int(usage.get("input_token_count") or 0), output_tokens=int(usage.get("output_token_count") or 0), cost=properties.get("cost"), cost_known=properties.get("cost") is not None, success=True)
            except Exception as error:
                logger.warning("LangSmith usage unavailable: %s", type(error).__name__)


class TraceToolMiddleware(FunctionMiddleware):
    async def process(self, context: FunctionInvocationContext, call_next: Callable[[], Awaitable[None]]) -> None:
        with trace_span(context.function.name, "tool") as span:
            try:
                await call_next()
            except MiddlewareTermination:
                trace_result(span, success=True)
                raise
            trace_result(span, success=True)
