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
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from web_testing_system.security import is_sensitive_name


class Settings(BaseSettings):
    """Local process settings loaded from environment variables and an optional .env file."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    openrouter_api_key: SecretStr | None = None
    langsmith_api_key: SecretStr | None = None
    langsmith_tracing: bool = True
    langsmith_project: str = "multi-agent-web-testing"
    langsmith_endpoint: str = "https://api.smith.langchain.com"
    main_agent_model: str = "deepseek/deepseek-v4-flash"
    main_agent_provider: str = "streamlake/fp8"
    main_agent_backup_model: str = "deepseek/deepseek-v3.2"
    main_agent_backup_provider: str = "deepinfra/fp4"
    tester_agent_model: str = "z-ai/glm-5.3-flash"
    tester_agent_provider: str = "relace"
    tester_agent_backup_model: str = "qwen/qwen3.8-flash"
    tester_agent_backup_provider: str | None = None
    jev_model: str = "typesafe/jev-1.13"
    computer_use_provider: str = "relace"
    computer_use_model: str | None = None
    full_evaluation: bool = False
    artifacts_dir: Path = Path("artifacts/runs")
    state_db_path: Path = Path("artifacts/state/shared_state.db")
    temporary_sensitive_dir: Path = Path("artifacts/temporary_sensitive")

    @field_validator("computer_use_model", mode="before")
    @classmethod
    def empty_visual_model_disables_gateway(cls, value: str | None) -> str | None:
        return value or None

    @field_validator("tester_agent_backup_provider", mode="before")
    @classmethod
    def empty_backup_provider_uses_automatic_routing(cls, value: str | None) -> str | None:
        return value.strip() or None if value is not None else None

    @field_validator("main_agent_model", "tester_agent_model", "main_agent_backup_model", "tester_agent_backup_model", "computer_use_model")
    @classmethod
    def reject_free_or_routed_models(cls, value: str | None) -> str | None:
        if value is None:
            return None
        model = value.strip()
        if not model or ":" in model or model.startswith(("openrouter/", "~")):
            raise ValueError("use a fixed paid model ID without free or routing variants")
        return model

    @field_validator("main_agent_provider", "tester_agent_provider", "main_agent_backup_provider", "computer_use_provider")
    @classmethod
    def require_fixed_provider(cls, value: str) -> str:
        if not value.strip() or value.casefold() in {"auto", "free", "gemini", "groq"}:
            raise ValueError("use one fixed paid OpenRouter provider endpoint")
        return value.strip()


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


class RequiredCheck(BaseModel):
    """One explicit, independently scored check; behavior descriptions are reference only."""

    model_config = ConfigDict(extra="forbid")

    check_id: str = Field(min_length=1)
    goal_id: str = Field(min_length=1)
    behavior_id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    identity_reference: str = Field(min_length=1)
    depends_on: list[str] = Field(default_factory=list)
    action_type: Literal["assertion", "repeat_submit"] = "assertion"


class ResetHookConfig(BaseModel):
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
    max_testers: int = Field(default=3, ge=1, le=4)
    max_browser_steps_per_task: int = Field(default=50, ge=1)
    max_replans_per_task: int = Field(default=2, ge=0)
    max_reproductions_per_finding: int = Field(default=3, ge=1)
    max_replay_steps_per_finding: int = Field(default=200, ge=1)
    max_parallel_browser_contexts: int = Field(default=3, ge=1, le=4)


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
    required_checks: list[RequiredCheck] = Field(default_factory=list)
    evaluation_version: str | None = None
    application_version: str | None = None
    reset_hook: ResetHookConfig | None = None
    budget: BudgetConfig = Field(default_factory=BudgetConfig)

    @model_validator(mode="after")
    def validate_required_checks(self) -> RunConfig:
        checks = {check.check_id: check for check in self.required_checks}
        if len(checks) != len(self.required_checks):
            raise ValueError("required check IDs must be unique")
        behaviors = {behavior.behavior_id for behavior in self.expected_behaviors}
        identities = {account.identity_reference for account in self.account_references}
        visited: set[str] = set()
        active: set[str] = set()

        def visit(check_id: str) -> None:
            if check_id in active:
                raise ValueError("required check dependencies must be acyclic")
            if check_id in visited:
                return
            active.add(check_id)
            for dependency in checks[check_id].depends_on:
                if dependency not in checks:
                    raise ValueError("required check dependency is unavailable")
                visit(dependency)
            active.remove(check_id)
            visited.add(check_id)

        for check in self.required_checks:
            if check.behavior_id not in behaviors or check.identity_reference not in identities:
                raise ValueError("required check needs a configured behavior and identity")
            visit(check.check_id)
        return self

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
