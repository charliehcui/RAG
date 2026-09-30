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


def test_settings_support_configurable_providers_without_model_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_gemini_key = "fake-gemini-key-for-test"
    fake_groq_key = "fake-groq-key-for-test"
    monkeypatch.setenv("GEMINI_API_KEY", fake_gemini_key)
    monkeypatch.setenv("GROQ_API_KEY", fake_groq_key)
    monkeypatch.setenv("MAIN_AGENT_PROVIDER", "gemini")
    monkeypatch.setenv("MAIN_AGENT_MODEL", "configured-main-model")
    monkeypatch.setenv("MAIN_AGENT_FALLBACK_MODEL", "configured-main-fallback-model")
    monkeypatch.setenv("TESTER_AGENT_PROVIDER", "groq")
    monkeypatch.setenv("TESTER_AGENT_MODEL", "configured-tester-model")
    monkeypatch.setenv("COMPUTER_USE_MODEL", "configured-computer-use-model")

    settings = Settings(_env_file=None)

    assert settings.main_agent_provider == "gemini"
    assert settings.main_agent_model == "configured-main-model"
    assert settings.main_agent_fallback_model == "configured-main-fallback-model"
    assert settings.tester_agent_provider == "groq"
    assert settings.computer_use_provider == "gemini"
    assert settings.computer_use_model == "configured-computer-use-model"
    assert settings.tester_agent_model == "configured-tester-model"
    assert settings.full_evaluation is False
    assert fake_gemini_key not in repr(settings)
    assert fake_groq_key not in repr(settings)


def test_model_names_are_optional_and_not_hardcoded(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("GEMINI_API_KEY", "GROQ_API_KEY", "MAIN_AGENT_MODEL", "MAIN_AGENT_FALLBACK_MODEL", "TESTER_AGENT_MODEL", "COMPUTER_USE_MODEL"):
        monkeypatch.delenv(name, raising=False)

    settings = Settings(_env_file=None)

    assert settings.main_agent_model is None
    assert settings.main_agent_fallback_model is None
    assert settings.tester_agent_model is None
    assert settings.computer_use_model is None


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
