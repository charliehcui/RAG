from __future__ import annotations

import logging

from web_testing_system.security import (
    REDACTED,
    SecretRedactionFilter,
    redact_sensitive_data,
)


def test_structured_secret_fields_are_redacted() -> None:
    payload = {
        "api_key": "fake-api-key",
        "nested": {"password": "fake-password", "safe": "visible"},
        "cookies": [{"token": "fake-token"}],
    }

    redacted = redact_sensitive_data(payload)

    assert redacted == {
        "api_key": REDACTED,
        "nested": {"password": REDACTED, "safe": "visible"},
        "cookies": REDACTED,
    }


def test_registered_secrets_do_not_reach_log_output(caplog) -> None:  # type: ignore[no-untyped-def]
    fake_secret = "fake-secret-that-must-not-be-logged"
    logger = logging.getLogger("secret-redaction-test")
    logger.addFilter(SecretRedactionFilter([fake_secret]))

    with caplog.at_level(logging.INFO, logger=logger.name):
        logger.info("provider credential=%s", fake_secret)

    assert fake_secret not in caplog.text
    assert REDACTED in caplog.text
