"""Validated project and run configuration."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from web_testing_system.security import is_sensitive_name


class Settings(BaseSettings):
    """Local process settings loaded from environment variables and an optional .env file."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    gemini_api_key: SecretStr | None = None
    groq_api_key: SecretStr | None = None
    main_agent_provider: Literal["gemini"] = "gemini"
    main_agent_model: str | None = None
    main_agent_fallback_model: str | None = None
    tester_agent_provider: Literal["gemini", "groq"] = "gemini"
    tester_agent_model: str | None = None
    groq_base_url: str = "https://api.groq.com/openai/v1"
    laya_model: Literal["english", "multilingual", "typed-decisions"] = "english"
    computer_use_provider: Literal["gemini"] = "gemini"
    computer_use_model: str | None = None
    full_evaluation: bool = False
    artifacts_dir: Path = Path("artifacts/runs")
    state_db_path: Path = Path("artifacts/state/shared_state.db")
    temporary_sensitive_dir: Path = Path("artifacts/temporary_sensitive")


class ExpectedBehaviorSource(StrEnum):
    USER_PROVIDED = "USER_PROVIDED"
    DEMO_SPEC = "DEMO_SPEC"
    DETERMINISTIC_RULE = "DETERMINISTIC_RULE"


class TaskPriority(StrEnum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


class ExpectedBehavior(BaseModel):
    model_config = ConfigDict(extra="forbid")

    behavior_id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    applies_to: str = Field(min_length=1)
    source: ExpectedBehaviorSource


class AccountReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identity_reference: str = Field(min_length=1)
    role: str = Field(min_length=1)
    permissions: list[str] = Field(min_length=1)
    secret_reference: str | None = None


class ResetHook(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hook_type: str = Field(min_length=1)
    target: str = Field(min_length=1)


class BudgetConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_runtime_seconds: int = Field(default=900, gt=0)
    max_llm_calls: int = Field(default=20, ge=0)
    max_input_tokens: int = Field(default=100_000, ge=0)
    max_output_tokens: int = Field(default=20_000, ge=0)
    max_jev_calls: int = Field(default=50, ge=0)
    max_computer_use_calls: int = Field(default=0, ge=0)
    max_testers: int = Field(default=2, ge=1, le=4)
    max_browser_steps_per_task: int = Field(default=50, ge=1)
    max_replans_per_task: int = Field(default=2, ge=0)
    max_reproductions_per_finding: int = Field(default=3, ge=1)
    max_parallel_browser_contexts: int = Field(default=2, ge=1, le=4)


class RunConfig(BaseModel):
    """The validated user input needed before a test run may be created."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    target_url: AnyHttpUrl
    test_goal: str = Field(min_length=1)
    focus_features: list[str] = Field(min_length=1)
    allowed_scope: list[str] = Field(min_length=1)
    account_references: list[AccountReference] = Field(min_length=1)
    test_data: dict[str, Any]
    denied_operations: list[str] = Field(min_length=1)
    expected_behaviors: list[ExpectedBehavior] = Field(default_factory=list)
    application_version: str | None = None
    reset_hook: ResetHook | None = None
    budget: BudgetConfig = Field(default_factory=BudgetConfig)

    @field_validator("test_goal", "application_version")
    @classmethod
    def reject_blank_text(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("focus_features", "allowed_scope", "denied_operations")
    @classmethod
    def reject_blank_list_items(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("items must not be blank")
        return values

    @field_validator("test_data")
    @classmethod
    def reject_secret_test_data(cls, value: dict[str, Any]) -> dict[str, Any]:
        pending: list[Any] = [value]
        while pending:
            item = pending.pop()
            if isinstance(item, dict):
                if any(is_sensitive_name(str(key)) for key in item):
                    raise ValueError("test_data must contain references, not secret values")
                pending.extend(item.values())
            elif isinstance(item, list):
                pending.extend(item)
        return value


def resolve_application_version(user_version: str | None, discovered_version: str | None, demo_version: str | None) -> str:
    """Resolve version using the project-defined precedence, with UNKNOWN as the final fallback."""

    for version in (user_version, discovered_version, demo_version):
        if version is not None and version.strip():
            return version.strip()
    return "UNKNOWN"
