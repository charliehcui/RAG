"""Create the configured real MAF chat clients without exposing API keys."""

from agent_framework import BaseChatClient
from agent_framework.gemini import GeminiChatClient
from agent_framework.openai import OpenAIChatCompletionClient

from web_testing_system.config import Settings


def create_main_chat_client(settings: Settings) -> BaseChatClient:
    if settings.main_agent_provider != "gemini":
        raise ValueError("Main Agent provider must be Gemini")
    return _create_gemini_client(settings, settings.main_agent_model)


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
