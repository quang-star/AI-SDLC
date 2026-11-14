"""Secret redaction utilities for logs, audit payloads, and error reports."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

_SENSITIVE_KEY_WORDS = (
    "api_key",
    "apikey",
    "secret",
    "password",
    "passwd",
    "token",
    "authorization",
    "private_key",
    "credential",
    "ciphertext",
)

_TOKEN_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(Bearer\s+)[A-Za-z0-9._\-+=]+", re.IGNORECASE),
    re.compile(r"\bsk-(?:proj-|live-|test-)?[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{12,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN [^-]+-----.*?-----END [^-]+-----", re.DOTALL),
)


def redact_text(text: str) -> str:
    """Scrub token/credential patterns from freeform text."""
    out = text
    for pat in _TOKEN_VALUE_PATTERNS:
        out = pat.sub(lambda m: f"{m.group(1)}[REDACTED]" if m.groups() else "[REDACTED]", out)
    return out


def redact_payload(value: Any) -> Any:
    """Recursively redact sensitive fields and token patterns from structured payloads."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        cleaned: dict[str, Any] = {}
        for k, v in value.items():
            key_lower = str(k).lower()
            if any(word in key_lower for word in _SENSITIVE_KEY_WORDS) and key_lower not in (
                "token_count",
                "input_tokens",
                "output_tokens",
                "cached_tokens",
                "credential_ref_id",
            ):
                cleaned[str(k)] = "[REDACTED]"
            else:
                cleaned[str(k)] = redact_payload(v)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [redact_payload(item) for item in value]
    return value
