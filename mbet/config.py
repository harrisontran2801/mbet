"""Load ~/.config/mbet/config.yaml, then let environment variables override it."""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator

from mbet.errors import ConfigurationError

ENDPOINT_NAMES = (
    "login",
    "logout",
    "balance",
    "sports",
    "events",
    "event",
    "odds",
    "history",
    "place_bet",
    "search",
)


class Endpoints(BaseModel):
    login: str = ""
    logout: str = ""
    balance: str = ""
    sports: str = ""
    events: str = ""
    event: str = ""
    odds: str = ""
    history: str = ""
    place_bet: str = ""
    search: str = ""


class CsrfConfig(BaseModel):
    header: str = ""
    cookie: str = ""
    response_json: str = ""
    required: bool = True
    required_for_login: bool = False


class RequestTemplate(BaseModel):
    method: str = "GET"
    content_type: str = "json"
    body: dict[str, str] = Field(default_factory=dict)
    success_statuses: list[int] = Field(default_factory=lambda: [200, 201, 204])

    @field_validator("method")
    @classmethod
    def _method(cls, value: str) -> str:
        method = value.upper().strip()
        if method not in {"GET", "POST", "PUT", "DELETE", "HEAD"}:
            raise ValueError(f"unsupported method {value}")
        return method

    @field_validator("content_type")
    @classmethod
    def _content_type(cls, value: str) -> str:
        kind = value.lower().strip()
        if kind not in {"json", "form"}:
            raise ValueError("content_type must be json or form")
        return kind


class RequestTemplates(BaseModel):
    login: RequestTemplate = Field(default_factory=lambda: RequestTemplate(method="POST"))
    logout: RequestTemplate = Field(default_factory=lambda: RequestTemplate(method="POST"))
    balance: RequestTemplate = Field(default_factory=RequestTemplate)
    sports: RequestTemplate = Field(default_factory=RequestTemplate)
    events: RequestTemplate = Field(default_factory=RequestTemplate)
    event: RequestTemplate = Field(default_factory=RequestTemplate)
    odds: RequestTemplate = Field(default_factory=RequestTemplate)
    history: RequestTemplate = Field(default_factory=RequestTemplate)
    place_bet: RequestTemplate = Field(
        default_factory=lambda: RequestTemplate(method="POST", success_statuses=[200, 201])
    )
    search: RequestTemplate = Field(default_factory=RequestTemplate)


class RetryConfig(BaseModel):
    max_attempts: int = 3
    backoff_seconds: float = 0.4
    max_backoff_seconds: float = 2.0

    @field_validator("max_attempts")
    @classmethod
    def _attempts(cls, value: int) -> int:
        if value < 1:
            raise ValueError("max_attempts must be >= 1")
        return value


def _default_maps() -> dict[str, Any]:
    return {
        "balance": {"amount": "amount", "currency": "currency"},
        "sports": {"list": "sports", "id": "id", "name": "name"},
        "events": {
            "list": "events",
            "id": "id",
            "sport": "sport",
            "competition": "competition",
            "start_time": "startTime",
            "home_team": "home",
            "away_team": "away",
            "name": "name",
        },
        "event": {
            "root": "",
            "id": "id",
            "sport": "sport",
            "competition": "competition",
            "start_time": "startTime",
            "home_team": "home",
            "away_team": "away",
            "name": "name",
        },
        "odds": {
            "list": "markets",
            "id": "id",
            "name": "name",
            "selections": "selections",
            "selection_id": "id",
            "selection_name": "name",
            "selection_odds": "odds",
        },
        "history": {
            "list": "history",
            "id": "id",
            "event_id": "eventId",
            "event_name": "eventName",
            "selection": "selection",
            "stake": "stake",
            "odds": "odds",
            "status": "status",
            "placed_at": "placedAt",
            "potential_return": "potentialReturn",
        },
        "login": {"csrf": "csrfToken"},
        "bet": {
            "status": "status",
            "bet_id": "betId",
            "message": "message",
            "accepted_values": ["accepted", "ok", "success", "placed"],
            "rejected_values": ["rejected", "failed", "error", "declined"],
            "unknown_values": ["unknown", "pending", "processing"],
        },
    }


class AppConfig(BaseModel):
    base_url: str = "https://www.marathonbet.com"
    currency: str = "USD"
    max_stake: Decimal = Decimal("10.00")
    betting_enabled: bool = False
    timeout: float = 20.0
    debug: bool = False
    timezone: str = "UTC"
    duplicate_window_seconds: int = 120
    allow_insecure: bool = False
    endpoints: Endpoints = Field(default_factory=Endpoints)
    csrf: CsrfConfig = Field(default_factory=CsrfConfig)
    request_templates: RequestTemplates = Field(default_factory=RequestTemplates)
    response_maps: dict[str, Any] = Field(default_factory=_default_maps)
    retries: RetryConfig = Field(default_factory=RetryConfig)
    headers: dict[str, str] = Field(default_factory=dict)

    @field_validator("max_stake", mode="before")
    @classmethod
    def _money(cls, value: object) -> object:
        if isinstance(value, float):
            return format(value, "f")
        return value

    @field_validator("base_url")
    @classmethod
    def _base_url(cls, value: str) -> str:
        cleaned = value.strip().rstrip("/")
        if not cleaned:
            raise ValueError("base_url is required")
        return cleaned

    def needs_csrf(self, *, login: bool) -> bool:
        if not self.csrf.header.strip():
            return False
        if login:
            return self.csrf.required_for_login
        return self.csrf.required


def config_dir() -> Path:
    override = os.environ.get("MBET_HOME")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg).expanduser() / "mbet"
    return Path.home() / ".config" / "mbet"


def user_config_path() -> Path:
    override = os.environ.get("MBET_CONFIG")
    if override:
        return Path(override).expanduser()
    return config_dir() / "config.yaml"


def bundled_config_path() -> Path | None:
    candidate = Path(__file__).resolve().parent.parent / "config" / "marathonbet.yaml"
    if candidate.is_file():
        return candidate
    return None


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def parse_bool(value: str) -> bool:
    cleaned = value.strip().lower()
    if cleaned in {"1", "true", "yes", "on"}:
        return True
    if cleaned in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"Invalid boolean value: {value}")


def _apply_env(data: dict[str, Any]) -> dict[str, Any]:
    updated = dict(data)
    if os.getenv("MBET_BASE_URL"):
        updated["base_url"] = os.environ["MBET_BASE_URL"]
    if os.getenv("MBET_CURRENCY"):
        updated["currency"] = os.environ["MBET_CURRENCY"]
    if os.getenv("MBET_MAX_STAKE"):
        updated["max_stake"] = os.environ["MBET_MAX_STAKE"]
    if "MBET_BETTING_ENABLED" in os.environ and os.environ["MBET_BETTING_ENABLED"] != "":
        updated["betting_enabled"] = parse_bool(os.environ["MBET_BETTING_ENABLED"])
    if os.getenv("MBET_TIMEOUT"):
        updated["timeout"] = os.environ["MBET_TIMEOUT"]
    if "MBET_DEBUG" in os.environ and os.environ["MBET_DEBUG"] != "":
        updated["debug"] = parse_bool(os.environ["MBET_DEBUG"])
    if os.getenv("MBET_TIMEZONE"):
        updated["timezone"] = os.environ["MBET_TIMEZONE"]
    endpoints = dict(updated.get("endpoints") or {})
    for name in ENDPOINT_NAMES:
        env_name = f"MBET_ENDPOINT_{name.upper()}"
        if os.getenv(env_name):
            endpoints[name] = os.environ[env_name]
    updated["endpoints"] = endpoints
    return updated


def load_config() -> AppConfig:
    data = AppConfig().model_dump(mode="json")
    paths: list[Path] = []
    bundled = bundled_config_path()
    if bundled is not None:
        paths.append(bundled)
    user_path = user_config_path()
    if user_path.is_file() and user_path != bundled:
        paths.append(user_path)
    for path in paths:
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except OSError as exc:
            raise ConfigurationError(f"Could not read {path}.") from exc
        except yaml.YAMLError as exc:
            raise ConfigurationError(f"Invalid YAML in {path.name}.") from exc
        if not isinstance(loaded, dict):
            raise ConfigurationError(f"{path} must contain a YAML mapping.")
        data = deep_merge(data, loaded)
    data = _apply_env(data)
    try:
        return AppConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigurationError("Invalid configuration.", hint=str(exc)) from None
