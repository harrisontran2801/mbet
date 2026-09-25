from __future__ import annotations

from datetime import datetime, timedelta, timezone

from decimal import Decimal

import httpx
import pytest

from mbet.errors import NOT_CONFIGURED, EndpointError, SessionExpiredError
from tests.conftest import scripted_client, seed_session


def test_cookie_is_persisted_and_sent_on_the_next_session(tmp_path):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/session/login":
            return httpx.Response(
                200,
                json={"csrfToken": "csrf-NOT-IN-LOGS"},
                headers={"set-cookie": "MBSESSION=cookie-NOT-IN-LOGS; Path=/; HttpOnly"},
            )
        if request.url.path == "/account/balance":
            seen["cookie"] = request.headers.get("cookie", "")
            seen["csrf"] = request.headers.get("x-csrf-token", "")
            return httpx.Response(200, json={"amount": "25.40", "currency": "USD"})
        return httpx.Response(404, json={"missing": request.url.path})

    first = scripted_client(tmp_path, handler)
    first.auth.login("alice", "p@ss-NOT-IN-LOGS-99")
    first.close()

    second = scripted_client(tmp_path, handler)
    balance = second.balance()
    assert balance.amount == Decimal("25.40")
    assert "MBSESSION=cookie-NOT-IN-LOGS" in seen["cookie"]


def test_csrf_header_is_attached_and_missing_token_fails_before_submit(tmp_path):
    calls = {"bet": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/bets":
            calls["bet"] += 1
            assert request.headers.get("x-csrf-token") == "csrf-NOT-IN-LOGS"
            return httpx.Response(200, json={"status": "accepted", "betId": "b-1"})
        return httpx.Response(404)

    client = scripted_client(tmp_path, handler)
    seed_session(client)
    from decimal import Decimal

    from mbet.models import BetSlip

    slip = BetSlip(
        event_id="381923",
        selection_id="home",
        stake=Decimal("1.00"),
        odds=Decimal("1.85"),
        event_label="Team A vs Team B",
        selection_name="Team A",
        currency="USD",
    )
    result = client.submit_bet(slip)
    assert result.status == "accepted"
    assert calls["bet"] == 1

    bare = scripted_client(tmp_path / "bare", handler)
    (tmp_path / "bare").mkdir(exist_ok=True)
    seed_session(bare, csrf=None, cookie=None)
    with pytest.raises(Exception, match="CSRF"):
        bare.submit_bet(slip)
    assert calls["bet"] == 1


def test_local_session_expiry_does_not_call_the_network(tmp_path):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"amount": "1", "currency": "USD"})

    client = scripted_client(tmp_path, handler)
    seed_session(client)
    session = client.store.load()
    assert session is not None
    session.expires_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    client.store.save(session)
    with pytest.raises(SessionExpiredError):
        client.balance()
    assert calls["n"] == 0


def test_remote_401_expires_the_session(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "expired"})

    client = scripted_client(tmp_path, handler)
    seed_session(client)
    with pytest.raises(SessionExpiredError):
        client.balance()
    stored = client.store.load()
    assert stored is not None
    assert stored.authenticated is False


def test_missing_endpoint_fails_clearly(tmp_path):
    client = scripted_client(tmp_path, lambda request: httpx.Response(500), endpoints={"balance": ""})
    with pytest.raises(EndpointError) as exc:
        client.balance()
    assert exc.value.message == NOT_CONFIGURED
