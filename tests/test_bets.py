from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from mbet.errors import (
    BetRejectedError,
    BetStatusUnknownError,
    BettingDisabledError,
    DuplicateSubmissionError,
    StakeLimitError,
)
from mbet.models import BetSlip
from tests.conftest import scripted_client, seed_session


def _slip(stake: str = "1.00", odds: str = "1.85") -> BetSlip:
    return BetSlip(
        event_id="381923",
        selection_id="home",
        stake=Decimal(stake),
        odds=Decimal(odds),
        event_label="Team A vs Team B",
        selection_name="Team A",
        currency="USD",
    )


def test_dry_run_does_not_submit(tmp_path):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500)

    client = scripted_client(tmp_path, handler)
    confirmation = client.bets.confirm(_slip(), dry_run=True)
    assert confirmation.dry_run is True
    assert confirmation.potential_return == Decimal("1.85")
    assert calls["n"] == 0
    body = confirmation.payload["body"]
    assert body["eventId"] == "381923"
    assert body["selectionId"] == "home"
    assert body["stake"] == "1.00"


def test_max_stake_blocks_before_http(tmp_path):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"status": "accepted"})

    client = scripted_client(tmp_path, handler)
    with pytest.raises(StakeLimitError, match=r"\$10\.00"):
        client.bets.submit(_slip("100.00"))
    assert calls["n"] == 0


def test_betting_disabled_blocks_submission(tmp_path):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"status": "accepted"})

    client = scripted_client(tmp_path, handler, betting_enabled=False)
    with pytest.raises(BettingDisabledError):
        client.bets.submit(_slip())
    assert calls["n"] == 0
    confirmation = client.bets.confirm(_slip(), dry_run=True)
    assert confirmation.dry_run is True


def test_server_error_is_not_retried_and_blocks_a_duplicate(tmp_path):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, json={"status": "error"})

    client = scripted_client(tmp_path, handler)
    seed_session(client)
    with pytest.raises(BetStatusUnknownError, match="uncertain"):
        client.submit_bet(_slip())
    assert calls["n"] == 1
    with pytest.raises(DuplicateSubmissionError):
        client.submit_bet(_slip())
    assert calls["n"] == 1


def test_timeout_is_not_retried(tmp_path):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ReadTimeout("timed out")

    client = scripted_client(tmp_path, handler)
    seed_session(client)
    with pytest.raises(BetStatusUnknownError):
        client.submit_bet(_slip())
    assert calls["n"] == 1


def test_rejected_bet_is_a_clear_rejection(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "rejected", "message": "price changed"})

    client = scripted_client(tmp_path, handler)
    seed_session(client)
    with pytest.raises(BetRejectedError, match="price changed"):
        client.submit_bet(_slip())


def test_ambiguous_success_body_is_unknown(tmp_path):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"ok": True})

    client = scripted_client(tmp_path, handler)
    seed_session(client)
    with pytest.raises(BetStatusUnknownError):
        client.submit_bet(_slip())
    assert calls["n"] == 1


def test_acknowledge_unknown_allows_exactly_one_more_submit(tmp_path):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadTimeout("timed out")
        return httpx.Response(200, json={"status": "accepted", "betId": "b-2"})

    client = scripted_client(tmp_path, handler)
    seed_session(client)
    with pytest.raises(BetStatusUnknownError):
        client.submit_bet(_slip())
    result = client.submit_bet(_slip(), acknowledge_unknown=True)
    assert result.bet_id == "b-2"
    assert calls["n"] == 2
