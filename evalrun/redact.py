"""Strip API keys from anything the runner writes or prints.

Same rule as app.redact(): a configured key never appears in an answer,
error, trace or log line, even inside a provider's error text.
"""

from __future__ import annotations

from typing import Any

KEY_FIELDS = ("gemini_api_key", "openai_api_key", "anthropic_api_key")
MIN_SECRET = 8


def secrets_of(settings: Any) -> tuple[str, ...]:
    values = (getattr(settings, f, None) for f in KEY_FIELDS)
    return tuple(v for v in values if v and len(v) > MIN_SECRET)


def redact_text(text: str, secrets: tuple[str, ...]) -> str:
    for secret in secrets:
        text = text.replace(secret, "***")
    return text


def redact(value: Any, secrets: tuple[str, ...]) -> Any:
    """Redact every string inside nested dicts/lists."""
    if isinstance(value, str):
        return redact_text(value, secrets)
    if isinstance(value, dict):
        return {k: redact(v, secrets) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [redact(v, secrets) for v in value]
    return value
