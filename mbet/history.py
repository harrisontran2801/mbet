"""Parse settled and open bet history from the configured JSON map."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from mbet.errors import InvalidResponseError
from mbet.markets import _list_of, _require, as_decimal, dig, parse_datetime
from mbet.models import BetHistoryItem


def parse_history(data: Any, maps: dict[str, Any]) -> list[BetHistoryItem]:
    items = _list_of(data, str(maps.get("list", "history")), "History")
    rows: list[BetHistoryItem] = []
    for item in items:
        if not isinstance(item, dict):
            raise InvalidResponseError("History list contained a non-object entry.")
        odds_path = str(maps.get("odds", "odds"))
        odds: Decimal | None
        try:
            raw_odds = dig(item, odds_path) if odds_path else None
        except KeyError:
            raw_odds = None
        odds = None if raw_odds in (None, "") else as_decimal(raw_odds, "odds")
        return_path = str(maps.get("potential_return", "potentialReturn"))
        try:
            raw_return = dig(item, return_path) if return_path else None
        except KeyError:
            raw_return = None
        potential = None if raw_return in (None, "") else as_decimal(raw_return, "potential_return")
        placed_path = str(maps.get("placed_at", "placedAt"))
        try:
            raw_placed = dig(item, placed_path) if placed_path else None
        except KeyError:
            raw_placed = None

        def optional(path_key: str, default_path: str) -> str:
            path = str(maps.get(path_key, default_path))
            if not path:
                return ""
            try:
                value = dig(item, path)
            except KeyError:
                return ""
            return "" if value is None else str(value)

        rows.append(
            BetHistoryItem(
                id=str(_require(item, str(maps.get("id", "id")), "History item")),
                event_id=optional("event_id", "eventId"),
                event_name=optional("event_name", "eventName"),
                selection=optional("selection", "selection"),
                stake=as_decimal(_require(item, str(maps.get("stake", "stake")), "History item"), "stake"),
                odds=odds,
                status=optional("status", "status"),
                placed_at=parse_datetime(raw_placed, placed_path),
                potential_return=potential,
            )
        )
    return rows
