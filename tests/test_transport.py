from __future__ import annotations

import httpx
import pytest

from mbet.errors import RateLimitError
from tests.conftest import scripted_client, seed_session


def test_idempotent_get_retries_rate_limit_then_succeeds(tmp_path):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"error": "slow"})
        return httpx.Response(200, json={"amount": "25.40", "currency": "USD"})

    client = scripted_client(tmp_path, handler)
    seed_session(client)
    balance = client.balance()
    assert str(balance.amount) == "25.40"
    assert calls["n"] == 2


def test_idempotent_get_retries_gateway_error(tmp_path):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="unavailable")
        return httpx.Response(200, json={"sports": [{"id": "fb", "name": "Football"}]})

    client = scripted_client(tmp_path, handler)
    seed_session(client)
    sports = client.sports()
    assert sports[0].id == "fb"
    assert calls["n"] == 2


def test_bet_post_is_not_retried_on_rate_limit(tmp_path):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, headers={"Retry-After": "0"}, json={"error": "slow"})

    client = scripted_client(tmp_path, handler)
    seed_session(client)
    from decimal import Decimal

    from mbet.models import BetSlip

    slip = BetSlip(event_id="1", selection_id="home", stake=Decimal("1.00"), odds=Decimal("1.50"), currency="USD")
    with pytest.raises(RateLimitError):
        client.submit_bet(slip)
    assert calls["n"] == 1
