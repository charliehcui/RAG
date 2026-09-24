"""Small helpers that keep secret material out of normal output."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

REDACTED = "[REDACTED]"
SENSITIVE_NAME_PARTS = ("api_key", "authorization", "cookie", "password", "secret", "storage_state", "token")


def is_sensitive_name(name: str) -> bool:
    normalized_name = name.lower().replace("-", "_")
    return any(part in normalized_name for part in SENSITIVE_NAME_PARTS)


def redact_sensitive_data(value: Any) -> Any:
    """Return a copy of structured data with known credential fields removed."""

    if isinstance(value, Mapping):
        return {str(key): REDACTED if is_sensitive_name(str(key)) else redact_sensitive_data(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact_sensitive_data(item) for item in value]
    return value


class SecretRedactionFilter(logging.Filter):
    """Replace explicitly registered secret values before a log record is emitted."""

    def __init__(self, secrets: Sequence[str]) -> None:
        super().__init__()
        self.secrets = tuple(secret for secret in secrets if secret)

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        for secret in self.secrets:
            message = message.replace(secret, REDACTED)
        record.msg = message
        record.args = ()
        return True

