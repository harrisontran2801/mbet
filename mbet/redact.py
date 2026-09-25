"""Redaction helpers. Registered secrets are scrubbed from log text."""

from __future__ import annotations

import contextvars
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_secrets: contextvars.ContextVar[frozenset[str] | None] = contextvars.ContextVar(
    "mbet_secrets", default=None
)

_SENSITIVE_KEY = re.compile(
    r"(password|passwd|pwd|secret|token|csrf|cookie|authorization|session)",
    re.IGNORECASE,
)
_SENSITIVE_HEADER = re.compile(
    r"(authorization|cookie|set-cookie|csrf|xsrf|token|api-key|secret)",
    re.IGNORECASE,
)


def register_secret(value: str | None) -> None:
    """Remember a secret so later log lines cannot echo it. Short values are ignored."""
    if not value:
        return
    text = str(value)
    if len(text) < 6:
        return
    current = set(_secrets.get() or ())
    current.add(text)
    _secrets.set(frozenset(current))


def redact_text(text: str) -> str:
    redacted = text
    for secret in sorted(_secrets.get() or (), key=len, reverse=True):
        if secret and secret in redacted:
            redacted = redacted.replace(secret, "***")
    return redacted


def is_sensitive_key(key: str) -> bool:
    return bool(_SENSITIVE_KEY.search(key))


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if is_sensitive_key(str(key)):
                out[key] = "***"
            else:
                out[key] = redact(item)
        return out
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    cleaned: dict[str, str] = {}
    for key, value in headers.items():
        if _SENSITIVE_HEADER.search(key):
            cleaned[key] = "***"
        else:
            cleaned[key] = redact_text(value)
    return cleaned


def redact_url(url: str) -> str:
    parts = urlsplit(url)
    if not parts.query:
        return redact_text(url)
    pairs = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if is_sensitive_key(key):
            pairs.append((key, "***"))
        else:
            pairs.append((key, redact_text(value)))
    return redact_text(urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(pairs), parts.fragment)))
