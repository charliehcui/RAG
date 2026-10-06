from __future__ import annotations

import pytest
from pydantic import ValidationError

from web_testing_system.config import (
    AccountReference,
    ExpectedBehavior,
    ExpectedBehaviorSource,
    RunConfig,
    Settings,
    TaskPriority,
    resolve_application_version,
)


def valid_run_input() -> dict[str, object]:
    return {
        "target_url": "https://example.test",
        "test_goal": "Check the account workflow",
        "focus_features": ["accounts"],
        "allowed_scope": ["/accounts"],
        "account_references": [
            AccountReference(identity_reference="identity-member", role="member", permissions=["read", "create"], secret_reference="env:TEST_MEMBER_PASSWORD")
        ],
        "test_data": {"project_prefix": "test-run"},
        "denied_operations": ["delete production data"],
    }


def test_settings_pin_paid_models_and_providers_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_key = "fake-openrouter-key"
    monkeypatch.setenv("OPENROUTER_API_KEY", fake_key)
    monkeypatch.setenv("MAIN_AGENT_MODEL", "deepseek/deepseek-v4-flash")
    monkeypatch.setenv("MAIN_AGENT_PROVIDER", "streamlake/fp8")
    monkeypatch.setenv("TESTER_AGENT_MODEL", "z-ai/glm-5.3-flash")
    monkeypatch.setenv("TESTER_AGENT_PROVIDER", "relace")
    settings = Settings(_env_file=None)
    assert settings.main_agent_provider == "streamlake/fp8"
    assert settings.tester_agent_provider == "relace"
    assert settings.main_agent_model == "deepseek/deepseek-v4-flash"
    assert settings.tester_agent_model == "z-ai/glm-5.3-flash"
    assert settings.full_evaluation is False
    assert fake_key not in repr(settings)
    assert not hasattr(settings, "main_agent_fallback_model")
    assert not hasattr(settings, "gemini_api_key")
    assert not hasattr(settings, "groq_api_key")


def test_verified_paid_defaults_and_manual_backups(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("OPENROUTER_API_KEY", "MAIN_AGENT_MODEL", "MAIN_AGENT_PROVIDER", "TESTER_AGENT_MODEL", "TESTER_AGENT_PROVIDER", "COMPUTER_USE_MODEL"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(_env_file=None)
    assert settings.main_agent_model == "deepseek/deepseek-v4-flash"
    assert settings.main_agent_provider == "streamlake/fp8"
    assert settings.tester_agent_model == "z-ai/glm-5.3-flash"
    assert settings.tester_agent_provider == "relace"
    assert settings.main_agent_backup_model == "deepseek/deepseek-v3.2"
    assert settings.tester_agent_backup_model == "openai/gpt-oss-20b"
    assert settings.computer_use_model is None
    assert Settings(_env_file=None, computer_use_model="").computer_use_model is None


def test_run_config_validates_required_input_and_expected_behavior() -> None:
    run_input = valid_run_input()
    run_input["expected_behaviors"] = [
        ExpectedBehavior(behavior_id="EB-1", description="A member can create an account", applies_to="accounts/member/create", source=ExpectedBehaviorSource.USER_PROVIDED)
    ]

    run_config = RunConfig.model_validate(run_input)

    assert run_config.account_references[0].role == "member"
    assert run_config.expected_behaviors[0].source == ExpectedBehaviorSource.USER_PROVIDED

    with pytest.raises(ValidationError) as error:
        RunConfig.model_validate({"target_url": "https://example.test"})

    missing_fields = {item["loc"][0] for item in error.value.errors() if item["type"] == "missing"}
    assert missing_fields == {"test_goal", "focus_features", "allowed_scope", "account_references", "test_data", "denied_operations"}


def test_version_precedence_and_closed_enums() -> None:
    assert resolve_application_version("user-v", "page-v", "demo-v") == "user-v"
    assert resolve_application_version(None, "page-v", "demo-v") == "page-v"
    assert resolve_application_version(None, None, "demo-v") == "demo-v"
    assert resolve_application_version(None, None, None) == "UNKNOWN"
    assert {item.value for item in ExpectedBehaviorSource} == {"USER_PROVIDED", "DEMO_SPEC", "DETERMINISTIC_RULE"}
    assert {item.value for item in TaskPriority} == {"P0", "P1", "P2", "P3"}


def test_run_config_rejects_secret_values_in_test_data() -> None:
    run_input = valid_run_input()
    run_input["test_data"] = {"member": {"password": "fake-password"}}

    with pytest.raises(ValidationError) as error:
        RunConfig.model_validate(run_input)

    assert "must contain references" in str(error.value)
    assert "fake-password" not in str(error.value)
