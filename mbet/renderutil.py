"""Substitute configured request templates. Placeholders are explicit, never guessed."""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any
from urllib.parse import quote

from mbet.errors import ConfigurationError

_FULL = re.compile(r"^\{(\w+)(\|number)?\}$")
_PARTIAL = re.compile(r"\{(\w+)\}")

_LOGIN_KEYS = {"username", "password"}
_BET_KEYS = {"event_id", "selection_id", "stake", "odds", "currency", "event_name", "selection_name"}


def render_path(template: str, values: dict[str, Any]) -> str:
    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values or values[key] is None:
            return ""
        return quote(str(values[key]), safe="")

    return _PARTIAL.sub(replace, template)


def _number(value: Any) -> int | float:
    number = Decimal(str(value))
    if number == number.to_integral():
        return int(number)
    return float(number)


def render_body(template: dict[str, str], values: dict[str, Any], *, kind: str) -> dict[str, Any]:
    allowed = _LOGIN_KEYS if kind == "login" else _BET_KEYS if kind == "place_bet" else set(values)
    rendered: dict[str, Any] = {}
    for key, raw in template.items():
        if not isinstance(raw, str):
            raise ConfigurationError(f"Request template field '{key}' must be a string placeholder.")
        full = _FULL.fullmatch(raw.strip())
        if full:
            name = full.group(1)
            if name == "password" and kind != "login":
                raise ConfigurationError("The password placeholder is only allowed on the login template.")
            if kind in {"login", "place_bet"} and name not in allowed:
                raise ConfigurationError(f"Placeholder {{{name}}} is not allowed on the {kind} template.")
            if name not in values:
                raise ConfigurationError(f"Template placeholder {{{name}}} has no value.")
            rendered[key] = _number(values[name]) if full.group(2) else values[name]
            continue

        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if name == "password" and kind != "login":
                raise ConfigurationError("The password placeholder is only allowed on the login template.")
            if name not in values:
                raise ConfigurationError(f"Template placeholder {{{name}}} has no value.")
            return str(values[name])

        try:
            rendered[key] = _PARTIAL.sub(replace, raw)
        except ConfigurationError:
            raise
    return rendered
