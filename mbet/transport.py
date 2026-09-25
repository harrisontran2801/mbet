"""HTTPS transport. Idempotent reads may retry. Bet placement never does."""

from __future__ import annotations

import json
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from mbet.config import AppConfig
from mbet.errors import EndpointError, RateLimitError, SessionExpiredError, VerificationRequiredError
from mbet.logging_config import get_logger
from mbet.redact import redact, redact_headers, redact_url
from mbet.storage import SessionStore

log = get_logger("transport")

_USER_AGENT = "mbet/0.1.0 (personal terminal client; not a browser)"
_RETRYABLE = {502, 503, 504}
_UNSAFE = {"POST", "PUT", "DELETE"}


def _verification_payload(data: Any) -> bool:
    flags = {
        "captcha",
        "captcharequired",
        "recaptcha",
        "hcaptcha",
        "mfa",
        "mfarequired",
        "twofactor",
        "twofactorrequired",
        "otprequired",
        "deviceverification",
        "verificationrequired",
        "challengerequired",
    }
    if isinstance(data, dict):
        for key, value in data.items():
            normalized = str(key).lower().replace("_", "").replace("-", "")
            if normalized in flags and value not in (False, None, "", 0, "false", "False"):
                return True
            if _verification_payload(value):
                return True
        return False
    if isinstance(data, list):
        return any(_verification_payload(item) for item in data[:20])
    return False


def response_requires_verification(response: httpx.Response, data: Any | None = None) -> bool:
    """Detect an interactive challenge. This never attempts to solve it."""
    if data is not None and _verification_payload(data):
        return True
    content_type = response.headers.get("content-type", "")
    text = response.text[:8000] if response.content else ""
    stripped = text.lstrip().lower()
    looks_html = "html" in content_type.lower() or stripped.startswith("<!doctype html") or stripped.startswith("<html")
    if not looks_html:
        return False
    markers = (
        "captcha",
        "g-recaptcha",
        "hcaptcha",
        "cf-challenge",
        "two-factor",
        "two factor",
        "verification code",
        "verify your device",
        "device verification",
        "unusual traffic",
    )
    return any(marker in stripped for marker in markers)


class HttpTransport:
    def __init__(
        self,
        config: AppConfig,
        store: SessionStore,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self._owns_client = client is None
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(config.timeout),
            follow_redirects=False,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            headers=self._default_headers(),
        )

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def _default_headers(self) -> dict[str, str]:
        headers = {
            "User-Agent": _USER_AGENT,
            "Accept": "application/json",
        }
        headers.update(self.config.headers)
        return headers

    def export_cookies(self) -> dict[str, str]:
        return {cookie.name: cookie.value for cookie in self.client.cookies.jar}

    def _apply_session_cookies(self) -> None:
        self.client.cookies.clear()
        try:
            session = self.store.load()
        except Exception:
            return
        if session is None:
            return
        for name, value in session.cookies.items():
            self.client.cookies.set(name, value, domain=self._cookie_domain(), path="/")

    def _url(self, path: str) -> str:
        if path.startswith("https://") or path.startswith("http://"):
            url = path
        else:
            url = f"{self.config.base_url}/{path.lstrip('/')}"
        parsed = urlparse(url)
        if parsed.scheme not in {"https", "http"} or not parsed.netloc:
            raise EndpointError(f"Refusing to call a malformed URL for this operation.")
        host = (parsed.hostname or "").lower()
        loopback = host in {"localhost", "127.0.0.1", "::1"}
        if parsed.scheme != "https" and not (loopback or self.config.allow_insecure):
            raise EndpointError(
                "Refusing a non-HTTPS endpoint.",
                hint="Set base_url and endpoints to https URLs. Loopback HTTP is allowed for local tests only.",
            )
        return url

    def _cookie_domain(self) -> str:
        host = urlparse(self.config.base_url).hostname or ""
        return host

    def _log_request(self, method: str, url: str, headers: dict[str, str], body: Any) -> None:
        log.debug(
            "http_request %s",
            json.dumps(
                redact(
                    {
                        "method": method,
                        "url": redact_url(url),
                        "headers": redact_headers(headers),
                        "body": body,
                    }
                )
            ),
        )

    def _log_response(self, response: httpx.Response) -> None:
        body: Any
        try:
            body = response.json()
        except Exception:
            body = response.text[:300]
        log.debug(
            "http_response %s",
            json.dumps(
                redact(
                    {
                        "status": response.status_code,
                        "url": redact_url(str(response.request.url)),
                        "headers": redact_headers(dict(response.headers)),
                        "body": body,
                    }
                )
            ),
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        form_body: dict[str, Any] | None = None,
        idempotent: bool = False,
        login: bool = False,
        extra_headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        method = method.upper()
        url = self._url(path)
        attempts = self.config.retries.max_attempts if idempotent else 1
        last_response: httpx.Response | None = None
        for attempt in range(1, attempts + 1):
            self._apply_session_cookies()
            headers = dict(extra_headers or {})
            if method in _UNSAFE:
                self._attach_csrf(headers, login=login)
            self._log_request(method, url, {**dict(self.client.headers), **headers}, json_body if json_body is not None else form_body)
            try:
                response = self.client.request(
                    method,
                    url,
                    headers=headers or None,
                    json=json_body,
                    data=form_body,
                )
            except httpx.HTTPError:
                if idempotent and attempt < attempts:
                    self._sleep(attempt)
                    continue
                raise
            self._log_response(response)
            last_response = response
            data: Any | None = None
            content_type = response.headers.get("content-type", "")
            if "json" in content_type.lower() and response.content:
                try:
                    data = response.json()
                except Exception:
                    data = None
            if response_requires_verification(response, data):
                raise VerificationRequiredError(
                    "Marathonbet requires an interactive verification step (CAPTCHA, MFA, device check, or similar).",
                    hint="Complete that step in your normal browser session. mbet will not bypass CAPTCHA, MFA, bot protection, or device verification.",
                )
            if response.status_code == 429:
                retry_after = _retry_after(response)
                if idempotent and attempt < attempts and _wait_allowed(retry_after, self.config.retries.max_backoff_seconds):
                    self._sleep(attempt, retry_after)
                    continue
                raise RateLimitError(
                    "Marathonbet rate limited this request.",
                    retry_after=retry_after,
                    hint="Wait and try the read again yourself. Bet placement is never retried automatically.",
                )
            if response.status_code in _RETRYABLE and idempotent and attempt < attempts:
                self._sleep(attempt)
                continue
            if response.status_code in {401, 419, 440} and not login:
                self.store.mark_expired()
                raise SessionExpiredError(
                    "Marathonbet session expired.",
                    hint="mbet login",
                )
            if response.status_code in {301, 302, 303, 307, 308}:
                raise EndpointError(
                    f"Endpoint returned a redirect (HTTP {response.status_code}).",
                    hint="mbet does not follow redirects. If this is a login wall or an interactive check, complete it in the browser and update the configured endpoint.",
                )
            return response
        if last_response is not None:
            return last_response
        raise EndpointError("Request failed before a response was received.")

    def _attach_csrf(self, headers: dict[str, str], *, login: bool) -> None:
        if not self.config.needs_csrf(login=login):
            return
        token = self._csrf_token()
        if not token:
            from mbet.errors import AuthenticationError

            raise AuthenticationError(
                "CSRF token is required but missing.",
                hint="Configure csrf.header and csrf.response_json (or csrf.cookie) from the token your session already receives, then run mbet login again. mbet will not invent a token.",
            )
        headers[self.config.csrf.header] = token

    def _csrf_token(self) -> str | None:
        token = None
        try:
            session = self.store.load()
        except Exception:
            session = None
        if session and session.csrf_token:
            token = session.csrf_token
        cookie_name = self.config.csrf.cookie.strip()
        if cookie_name:
            cookie_value = self.client.cookies.get(cookie_name)
            if cookie_value:
                token = cookie_value
        return token or None

    def _sleep(self, attempt: int, retry_after: float | None = None) -> None:
        delay = self.config.retries.backoff_seconds * attempt
        if retry_after is not None:
            delay = max(delay, retry_after)
        delay = min(delay, self.config.retries.max_backoff_seconds)
        if delay > 0:
            time.sleep(delay)


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _wait_allowed(retry_after: float | None, cap: float) -> bool:
    if retry_after is None:
        return True
    return retry_after <= cap
