from __future__ import annotations

import json
import logging

import httpx
import pytest

from mbet.errors import AuthenticationError, VerificationRequiredError
from mbet.logging_config import setup_logging
from tests.conftest import scripted_client

PASSWORD = "p@ss-NOT-IN-LOGS-99"
COOKIE = "cookie-NOT-IN-LOGS"
CSRF = "csrf-NOT-IN-LOGS"


def _login_ok(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content.decode())
    assert body["username"] == "alice"
    assert body["password"] == PASSWORD
    return httpx.Response(
        200,
        json={"ok": True, "csrfToken": CSRF},
        headers={"set-cookie": f"MBSESSION={COOKIE}; Path=/; HttpOnly"},
    )


def test_login_success_sets_authenticated_state(tmp_path):
    client = scripted_client(tmp_path, lambda request: _login_ok(request) if request.url.path == "/session/login" else httpx.Response(404))
    session = client.auth.login("alice", PASSWORD)
    assert session.authenticated is True
    assert session.username == "alice"
    stored = client.store.load()
    assert stored is not None
    assert stored.authenticated is True
    assert stored.csrf_token == CSRF
    assert stored.cookies["MBSESSION"] == COOKIE
    assert not hasattr(stored, "password") or "password" not in stored.model_dump()


def test_login_failure_does_not_save_session(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"ok": False})

    client = scripted_client(tmp_path, handler)
    with pytest.raises(AuthenticationError, match="Authentication failed"):
        client.auth.login("alice", PASSWORD)
    assert client.store.load() is None


def test_login_stops_on_captcha_without_a_second_attempt(tmp_path):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(403, json={"captchaRequired": True})

    client = scripted_client(tmp_path, handler)
    with pytest.raises(VerificationRequiredError):
        client.auth.login("alice", PASSWORD)
    assert calls["n"] == 1
    assert client.store.load() is None


def test_password_cookie_and_csrf_are_not_logged_or_stored_in_plaintext(tmp_path, caplog):
    setup_logging(logging.DEBUG)
    logging.getLogger("mbet").addHandler(caplog.handler)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/session/login":
            return _login_ok(request)
        return httpx.Response(404)

    client = scripted_client(tmp_path, handler)
    client.auth.login("alice", PASSWORD)
    text = caplog.text
    assert PASSWORD not in text
    assert COOKIE not in text
    assert CSRF not in text
    raw = (tmp_path / "session.bin").read_bytes()
    assert PASSWORD.encode() not in raw
    assert COOKIE.encode() not in raw
    assert CSRF.encode() not in raw
