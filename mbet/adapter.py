"""Adapter interface. The HTTP implementation only calls endpoints you configured."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import httpx

from mbet.config import AppConfig
from mbet.errors import (
    NOT_CONFIGURED,
    AuthenticationError,
    BetRejectedError,
    BetStatusUnknownError,
    ConfigurationError,
    EndpointError,
    InvalidResponseError,
    MbetError,
    VerificationRequiredError,
)
from mbet.history import parse_history
from mbet.logging_config import get_logger
from mbet.markets import (
    filter_events,
    find_selection,
    parse_balance,
    parse_event,
    parse_events,
    parse_odds,
    parse_sports,
)
from mbet.models import (
    AccountBalance,
    BetHistoryItem,
    BetResult,
    BetSlip,
    Event,
    Market,
    Sport,
    UserSession,
)
from mbet.redact import register_secret
from mbet.renderutil import render_body, render_path
from mbet.storage import SessionStore
from mbet.transport import HttpTransport, response_requires_verification

log = get_logger("adapter")

_UNCERTAIN = "Bet submission status is uncertain. Do NOT retry automatically."
_UNCERTAIN_HINT = "No automatic retry was performed. Check the account before submitting again."


class MarathonAdapter(ABC):
    """The CLI depends on this interface, not on a guessed Marathonbet URL map."""

    @abstractmethod
    def login(self, username: str, password: str) -> UserSession:
        raise NotImplementedError

    @abstractmethod
    def logout(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def get_balance(self) -> AccountBalance:
        raise NotImplementedError

    @abstractmethod
    def get_sports(self) -> list[Sport]:
        raise NotImplementedError

    @abstractmethod
    def get_events(self, sport: str | None = None) -> list[Event]:
        raise NotImplementedError

    @abstractmethod
    def get_event(self, event_id: str) -> Event:
        raise NotImplementedError

    @abstractmethod
    def get_odds(self, event_id: str) -> list[Market]:
        raise NotImplementedError

    @abstractmethod
    def get_history(self) -> list[BetHistoryItem]:
        raise NotImplementedError

    @abstractmethod
    def place_bet(self, slip: BetSlip) -> BetResult:
        raise NotImplementedError

    @abstractmethod
    def search(self, query: str) -> list[Event]:
        raise NotImplementedError


class ConfigurableHttpAdapter(MarathonAdapter):
    def __init__(self, config: AppConfig, transport: HttpTransport, store: SessionStore) -> None:
        self.config = config
        self.transport = transport
        self.store = store

    def require_endpoint(self, name: str) -> str:
        path = getattr(self.config.endpoints, name, "") or ""
        if not str(path).strip():
            raise EndpointError(
                NOT_CONFIGURED,
                hint=(
                    f"Set endpoints.{name} in ~/.config/mbet/config.yaml to the HTTPS path you observed "
                    "in your own authenticated session. mbet will not guess undocumented endpoints."
                ),
            )
        return str(path)

    def assert_login_configured(self) -> None:
        self.require_endpoint("login")
        if not self.config.request_templates.login.body:
            raise ConfigurationError(
                "Login request body is not configured.",
                hint=(
                    "Set request_templates.login.body to the fields your session actually posts. "
                    "Use {username} and {password}. mbet will not guess the field names, and it will not send a password until this is set."
                ),
            )

    def login(self, username: str, password: str) -> UserSession:
        self.assert_login_configured()
        register_secret(password)
        template = self.config.request_templates.login
        path = render_path(self.require_endpoint("login"), {"username": username})
        body = render_body(template.body, {"username": username, "password": password}, kind="login")
        try:
            response = self._send(template.method, path, template, body, idempotent=False, login=True)
            if response_requires_verification(response):
                raise VerificationRequiredError(
                    "Marathonbet requires an interactive verification step (CAPTCHA, MFA, device check, or similar).",
                    hint="Complete that step in your normal browser session. mbet will not bypass it.",
                )
            data = self._json(response)
            if response_requires_verification(response, data):
                raise VerificationRequiredError(
                    "Marathonbet requires an interactive verification step (CAPTCHA, MFA, device check, or similar).",
                    hint="Complete that step in your normal browser session. mbet will not bypass it.",
                )
            self._ensure_success(response, template.success_statuses, login=True)
        finally:
            body.clear()
        if isinstance(data, dict) and response_requires_verification(response, data):
            raise VerificationRequiredError(
                "Marathonbet requires an interactive verification step (CAPTCHA, MFA, device check, or similar).",
                hint="Complete that step in your normal browser session. mbet will not bypass it.",
            )
        cookies = self.transport.export_cookies()
        csrf = self._extract_csrf(data, cookies)
        for value in cookies.values():
            register_secret(value)
        register_secret(csrf)
        return UserSession(
            username=username,
            authenticated=True,
            created_at=datetime.now(timezone.utc),
            cookies=cookies,
            csrf_token=csrf,
        )

    def logout(self) -> None:
        path = (self.config.endpoints.logout or "").strip()
        if not path:
            log.info("remote logout skipped; endpoint not configured")
            return
        template = self.config.request_templates.logout
        response = self._send(template.method, path, template, template.body or None, idempotent=False, login=False)
        if response.status_code not in set(template.success_statuses) | {401, 204}:
            raise EndpointError(
                f"Remote logout failed (HTTP {response.status_code}). Local session will still be cleared.",
            )

    def get_balance(self) -> AccountBalance:
        data = self._read("balance")
        return parse_balance(data, self._map("balance"), self.config.currency)

    def get_sports(self) -> list[Sport]:
        return parse_sports(self._read("sports"), self._map("sports"))

    def get_events(self, sport: str | None = None) -> list[Event]:
        path = render_path(self.require_endpoint("events"), {"sport": sport or ""})
        data = self._read_path("events", path)
        return parse_events(data, self._map("events"), sport=sport)

    def get_event(self, event_id: str) -> Event:
        configured = (self.config.endpoints.event or "").strip()
        if configured:
            path = render_path(configured, {"event_id": event_id})
            data = self._read_path("event", path)
            return parse_event(data, self._map("event"), fallback_id=event_id)
        events = self.get_events()
        for event in events:
            if event.id == event_id:
                return event
        raise EndpointError(f"Event {event_id} was not found in the configured events feed.")

    def get_odds(self, event_id: str) -> list[Market]:
        path = render_path(self.require_endpoint("odds"), {"event_id": event_id})
        data = self._read_path("odds", path)
        return parse_odds(data, self._map("odds"), event_id=event_id)

    def get_history(self) -> list[BetHistoryItem]:
        return parse_history(self._read("history"), self._map("history"))

    def search(self, query: str) -> list[Event]:
        configured = (self.config.endpoints.search or "").strip()
        if configured:
            path = render_path(configured, {"query": query})
            data = self._read_path("search", path)
            return parse_events(data, self._map("events"))
        return filter_events(self.get_events(), query)

    def place_bet(self, slip: BetSlip) -> BetResult:
        """Send the bet exactly once. Any ambiguous outcome becomes BetStatusUnknownError."""
        template = self.config.request_templates.place_bet
        path = render_path(self.require_endpoint("place_bet"), {"event_id": slip.event_id})
        if not template.body:
            raise ConfigurationError(
                "Bet request body is not configured.",
                hint="Set request_templates.place_bet.body to the JSON or form fields you observed. Nothing was submitted.",
            )
        values = {
            "event_id": slip.event_id,
            "selection_id": slip.selection_id,
            "stake": f"{slip.stake.quantize(Decimal('0.01'))}",
            "odds": format(slip.odds, "f"),
            "currency": slip.currency,
            "event_name": slip.event_label,
            "selection_name": slip.selection_name,
        }
        body = render_body(template.body, values, kind="place_bet")
        try:
            response = self._send(template.method, path, template, body, idempotent=False, login=False)
        except MbetError:
            raise
        except httpx.HTTPError:
            raise BetStatusUnknownError(_UNCERTAIN, hint=_UNCERTAIN_HINT) from None
        if response.status_code >= 500:
            raise BetStatusUnknownError(_UNCERTAIN, hint=_UNCERTAIN_HINT)
        if response.status_code not in template.success_statuses:
            raise BetRejectedError(
                f"Bet rejected (HTTP {response.status_code}).",
                hint="The book did not accept this bet. It was not retried.",
            )
        try:
            data = self._json(response)
        except InvalidResponseError:
            raise BetStatusUnknownError(_UNCERTAIN, hint=_UNCERTAIN_HINT) from None
        return self._bet_result(data, slip)

    def describe_bet_payload(self, slip: BetSlip) -> dict[str, object]:
        template = self.config.request_templates.place_bet
        endpoint = (self.config.endpoints.place_bet or "").strip()
        values = {
            "event_id": slip.event_id,
            "selection_id": slip.selection_id,
            "stake": f"{slip.stake.quantize(Decimal('0.01'))}",
            "odds": format(slip.odds, "f"),
            "currency": slip.currency,
            "event_name": slip.event_label,
            "selection_name": slip.selection_name,
        }
        payload: dict[str, object] = {
            "method": template.method,
            "url": render_path(endpoint, {"event_id": slip.event_id}) if endpoint else "",
            "endpoint_configured": bool(endpoint),
            "body_configured": bool(template.body),
        }
        if endpoint and template.body:
            payload["body"] = render_body(template.body, values, kind="place_bet")
        if self.config.needs_csrf(login=False):
            payload["csrf_header"] = self.config.csrf.header
            payload["csrf_note"] = "value redacted; attached from the session at submit time"
        return payload

    def _bet_result(self, data: Any, slip: BetSlip) -> BetResult:
        maps = self._map("bet")
        if not isinstance(data, dict):
            raise BetStatusUnknownError(_UNCERTAIN, hint=_UNCERTAIN_HINT)
        status_path = str(maps.get("status", "status"))
        try:
            raw_status = str(dig_local(data, status_path)).strip().lower()
        except KeyError:
            raise BetStatusUnknownError(_UNCERTAIN, hint=_UNCERTAIN_HINT) from None
        message = ""
        message_path = str(maps.get("message", "message"))
        try:
            raw_message = dig_local(data, message_path)
            message = "" if raw_message is None else str(raw_message)
        except KeyError:
            message = ""
        bet_id = None
        bet_path = str(maps.get("bet_id", "betId"))
        try:
            raw_id = dig_local(data, bet_path)
            bet_id = None if raw_id in (None, "") else str(raw_id)
        except KeyError:
            bet_id = None
        accepted = {str(item).lower() for item in maps.get("accepted_values", [])}
        rejected = {str(item).lower() for item in maps.get("rejected_values", [])}
        unknown = {str(item).lower() for item in maps.get("unknown_values", [])}
        potential = (slip.stake * slip.odds).quantize(Decimal("0.01"))
        if raw_status in accepted:
            return BetResult(
                status="accepted",
                bet_id=bet_id,
                message=message,
                stake=slip.stake,
                odds=slip.odds,
                potential_return=potential,
            )
        if raw_status in rejected:
            raise BetRejectedError(message or "Bet rejected")
        if raw_status in unknown or raw_status:
            raise BetStatusUnknownError(_UNCERTAIN, hint=message or _UNCERTAIN_HINT)
        raise BetStatusUnknownError(_UNCERTAIN, hint=_UNCERTAIN_HINT)

    def _map(self, name: str) -> dict[str, Any]:
        raw = self.config.response_maps.get(name) or {}
        if not isinstance(raw, dict):
            raise ConfigurationError(f"response_maps.{name} must be a mapping.")
        return raw

    def _read(self, name: str) -> Any:
        return self._read_path(name, self.require_endpoint(name))

    def _read_path(self, name: str, path: str) -> Any:
        template = getattr(self.config.request_templates, name)
        response = self._send(template.method, path, template, None, idempotent=template.method in {"GET", "HEAD"}, login=False)
        self._ensure_success(response, template.success_statuses, login=False)
        return self._json(response)

    def _send(
        self,
        method: str,
        path: str,
        template: Any,
        body: dict[str, Any] | None,
        *,
        idempotent: bool,
        login: bool,
    ) -> httpx.Response:
        json_body = body if template.content_type == "json" and body else None
        form_body = body if template.content_type == "form" and body else None
        return self.transport.request(
            method,
            path,
            json_body=json_body,
            form_body=form_body,
            idempotent=idempotent,
            login=login,
        )

    def _json(self, response: httpx.Response) -> Any:
        content_type = response.headers.get("content-type", "")
        text = response.text or ""
        if "html" in content_type.lower() or text.lstrip().startswith("<"):
            raise InvalidResponseError(
                "Endpoint returned HTML, not a JSON payload.",
                hint="Point the endpoint at the authenticated request that returns JSON, and set response_maps. mbet does not scrape HTML or bypass protections.",
            )
        try:
            return response.json()
        except json.JSONDecodeError:
            raise InvalidResponseError("Response was not valid JSON.") from None

    def _ensure_success(self, response: httpx.Response, statuses: list[int], *, login: bool) -> None:
        if response.status_code in statuses:
            return
        if response.status_code in {400, 401, 403} and login:
            raise AuthenticationError("Authentication failed")
        if response.status_code == 403:
            raise EndpointError(
                "Request was forbidden (HTTP 403).",
                hint="mbet will not bypass bot protection, geo restrictions, or access controls.",
            )
        raise EndpointError(f"Unexpected HTTP {response.status_code} from the configured endpoint.")

    def _extract_csrf(self, data: Any, cookies: dict[str, str]) -> str | None:
        path = str(self._map("login").get("csrf") or self.config.csrf.response_json or "")
        token: str | None = None
        if path and isinstance(data, (dict, list)):
            try:
                raw = dig_local(data, path)
            except KeyError:
                raw = None
            if raw not in (None, ""):
                token = str(raw)
        cookie_name = self.config.csrf.cookie.strip()
        if cookie_name and cookies.get(cookie_name):
            token = cookies[cookie_name]
        return token


def dig_local(data: Any, path: str) -> Any:
    from mbet.markets import dig

    return dig(data, path)


def selection_for(markets: list[Market], selection_id: str) -> Any:
    return find_selection(markets, selection_id)
