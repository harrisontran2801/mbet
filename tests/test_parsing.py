from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from mbet.config import AppConfig
from mbet.errors import InvalidResponseError
from mbet.history import parse_history
from mbet.markets import parse_balance, parse_events, parse_odds, parse_sports
from tests.conftest import scripted_client, seed_session


def _maps() -> dict:
    return AppConfig().response_maps


def test_balance_parsing():
    balance = parse_balance({"amount": "25.40", "currency": "USD"}, _maps()["balance"], "EUR")
    assert balance.amount == Decimal("25.40")
    assert balance.currency == "USD"


def test_balance_missing_field_is_an_error():
    with pytest.raises(InvalidResponseError):
        parse_balance({"currency": "USD"}, _maps()["balance"], "USD")


def test_event_and_odds_parsing():
    events = parse_events(
        {
            "events": [
                {
                    "id": "381923",
                    "sport": "football",
                    "competition": "League",
                    "startTime": "2026-09-26T13:00:00Z",
                    "home": "Team A",
                    "away": "Team B",
                },
                {
                    "id": "99",
                    "sport": "tennis",
                    "competition": "ATP",
                    "startTime": "2026-09-26T15:00:00Z",
                    "home": "Player A",
                    "away": "Player B",
                },
            ]
        },
        _maps()["events"],
        sport="football",
    )
    assert len(events) == 1
    assert events[0].id == "381923"
    assert events[0].match_label == "Team A vs Team B"
    assert events[0].start_time is not None

    markets = parse_odds(
        {
            "markets": [
                {
                    "id": "1x2",
                    "name": "1X2",
                    "selections": [
                        {"id": "home", "name": "Home", "odds": "1.85"},
                        {"id": "draw", "name": "Draw", "odds": "3.40"},
                        {"id": "away", "name": "Away", "odds": "4.20"},
                    ],
                }
            ]
        },
        _maps()["odds"],
        event_id="381923",
    )
    assert markets[0].name == "1X2"
    assert markets[0].selections[0].odds == Decimal("1.85")
    assert markets[0].selections[0].event_id == "381923"


def test_sports_and_history_parsing():
    sports = parse_sports({"sports": [{"id": "fb", "name": "Football"}]}, _maps()["sports"])
    assert sports[0].name == "Football"
    rows = parse_history(
        {
            "history": [
                {
                    "id": "h1",
                    "eventId": "381923",
                    "eventName": "Team A vs Team B",
                    "selection": "Home",
                    "stake": "1.00",
                    "odds": "1.85",
                    "status": "accepted",
                    "placedAt": "2026-09-26T12:00:00Z",
                }
            ]
        },
        _maps()["history"],
    )
    assert rows[0].stake == Decimal("1.00")
    assert rows[0].event_name == "Team A vs Team B"


def test_malformed_and_html_responses(tmp_path):
    calls = {"n": 0}

    def bad_json(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, text="not-json", headers={"content-type": "text/plain"})

    client = scripted_client(tmp_path, bad_json)
    seed_session(client)
    with pytest.raises(InvalidResponseError):
        client.balance()

    def html(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html><body>Hello</body></html>", headers={"content-type": "text/html"})

    client = scripted_client(tmp_path / "html", html)
    (tmp_path / "html").mkdir()
    seed_session(client)
    with pytest.raises(InvalidResponseError):
        client.balance()

    def missing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"foo": 1})

    client = scripted_client(tmp_path / "missing", missing)
    (tmp_path / "missing").mkdir()
    seed_session(client)
    with pytest.raises(InvalidResponseError):
        client.balance()


def test_client_reads_events_and_odds(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/events":
            return httpx.Response(
                200,
                json={
                    "events": [
                        {
                            "id": "381923",
                            "sport": "football",
                            "competition": "League",
                            "startTime": "2026-09-26T13:00:00Z",
                            "home": "Team A",
                            "away": "Team B",
                        }
                    ]
                },
            )
        if request.url.path == "/events/381923/odds":
            return httpx.Response(
                200,
                json={
                    "markets": [
                        {"id": "1x2", "name": "1X2", "selections": [{"id": "home", "name": "Home", "odds": "1.85"}]}
                    ]
                },
            )
        return httpx.Response(404, json={"path": request.url.path})

    client = scripted_client(tmp_path, handler)
    seed_session(client)
    events = client.events("football")
    assert events[0].home_team == "Team A"
    markets = client.odds("381923")
    assert markets[0].selections[0].odds == Decimal("1.85")
    found = client.search("Team A")
    assert found[0].id == "381923"
