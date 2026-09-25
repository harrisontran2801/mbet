"""Parse sports, events, markets, and account balance from configured JSON maps."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from mbet.errors import InvalidResponseError
from mbet.models import AccountBalance, Event, Market, Selection, Sport


def dig(data: Any, path: str) -> Any:
    if path in ("", ".", "$"):
        return data
    current = data
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            if index >= len(current):
                raise KeyError(path)
            current = current[index]
        else:
            raise KeyError(path)
    return current


def _mapping(data: Any, what: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise InvalidResponseError(f"{what} response was not a JSON object.")
    return data


def _require(node: dict[str, Any], path: str, what: str) -> Any:
    try:
        return dig(node, path)
    except KeyError:
        keys = ", ".join(sorted(str(key) for key in node.keys())) or "(none)"
        raise InvalidResponseError(
            f"{what} response is missing '{path}'.",
            hint=f"Update response_maps to match the observed JSON. Top-level keys: {keys}.",
        ) from None


def as_decimal(value: Any, field: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise InvalidResponseError(f"Field '{field}' is not a number.") from None
    if not number.is_finite():
        raise InvalidResponseError(f"Field '{field}' is not a number.")
    return number


def parse_datetime(value: Any, field: str) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        stamp = float(value)
        if stamp > 10_000_000_000:
            stamp = stamp / 1000.0
        return datetime.fromtimestamp(stamp, tz=timezone.utc)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise InvalidResponseError(f"Field '{field}' is not a timestamp.") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _list_of(data: Any, path: str, what: str) -> list[Any]:
    if path in ("", ".", "$"):
        if isinstance(data, list):
            return data
        raise InvalidResponseError(f"{what} response did not contain a list.")
    try:
        value = dig(data, path)
    except KeyError:
        raise InvalidResponseError(
            f"{what} response is missing list '{path}'.",
            hint="Set response_maps to the list field you observed. An empty list is only returned when that field exists.",
        ) from None
    if not isinstance(value, list):
        raise InvalidResponseError(f"{what} field '{path}' was not a list.")
    return value


def parse_balance(data: Any, maps: dict[str, Any], default_currency: str) -> AccountBalance:
    node = _mapping(data, "Balance")
    amount = as_decimal(_require(node, str(maps.get("amount", "amount")), "Balance"), "amount")
    currency_path = str(maps.get("currency", "currency"))
    try:
        currency = str(dig(node, currency_path))
    except KeyError:
        currency = default_currency
    if not currency:
        currency = default_currency
    return AccountBalance(amount=amount, currency=currency)


def parse_sports(data: Any, maps: dict[str, Any]) -> list[Sport]:
    items = _list_of(data, str(maps.get("list", "sports")), "Sports")
    sports: list[Sport] = []
    id_path = str(maps.get("id", "id"))
    name_path = str(maps.get("name", "name"))
    for item in items:
        if not isinstance(item, dict):
            raise InvalidResponseError("Sports list contained a non-object entry.")
        sports.append(
            Sport(
                id=str(_require(item, id_path, "Sport")),
                name=str(_require(item, name_path, "Sport")),
            )
        )
    return sports


def _event_from(item: dict[str, Any], maps: dict[str, Any]) -> Event:
    def optional(path: str) -> str:
        if not path:
            return ""
        try:
            value = dig(item, path)
        except KeyError:
            return ""
        if value is None:
            return ""
        return str(value)

    start = None
    start_path = str(maps.get("start_time", "startTime"))
    if start_path:
        try:
            raw_start = dig(item, start_path)
        except KeyError:
            raw_start = None
        start = parse_datetime(raw_start, start_path)
    return Event(
        id=str(_require(item, str(maps.get("id", "id")), "Event")),
        sport=optional(str(maps.get("sport", "sport"))),
        competition=optional(str(maps.get("competition", "competition"))),
        start_time=start,
        home_team=optional(str(maps.get("home_team", "home"))),
        away_team=optional(str(maps.get("away_team", "away"))),
        name=optional(str(maps.get("name", "name"))),
    )


def parse_events(data: Any, maps: dict[str, Any], *, sport: str | None = None) -> list[Event]:
    items = _list_of(data, str(maps.get("list", "events")), "Events")
    events: list[Event] = []
    for item in items:
        if not isinstance(item, dict):
            raise InvalidResponseError("Events list contained a non-object entry.")
        events.append(_event_from(item, maps))
    if sport:
        needle = sport.casefold()
        events = [event for event in events if needle == event.sport.casefold() or needle in event.sport.casefold()]
    return events


def parse_event(data: Any, maps: dict[str, Any], *, fallback_id: str = "") -> Event:
    node = data
    root = str(maps.get("root", "") or "")
    if root:
        try:
            node = dig(data, root)
        except KeyError:
            raise InvalidResponseError(f"Event response is missing '{root}'.") from None
    if not isinstance(node, dict):
        raise InvalidResponseError("Event response was not a JSON object.")
    event = _event_from(node, maps)
    if not event.id and fallback_id:
        event.id = fallback_id
    return event


def parse_odds(data: Any, maps: dict[str, Any], *, event_id: str) -> list[Market]:
    items = _list_of(data, str(maps.get("list", "markets")), "Odds")
    selections_path = str(maps.get("selections", "selections"))
    markets: list[Market] = []
    for item in items:
        if not isinstance(item, dict):
            raise InvalidResponseError("Odds response contained a non-object market.")
        raw_selections = _list_of(item, selections_path, "Market selections")
        market_name = str(_require(item, str(maps.get("name", "name")), "Market"))
        market_id = str(_require(item, str(maps.get("id", "id")), "Market"))
        selections: list[Selection] = []
        for raw in raw_selections:
            if not isinstance(raw, dict):
                raise InvalidResponseError("Selection entry was not a JSON object.")
            selections.append(
                Selection(
                    id=str(_require(raw, str(maps.get("selection_id", "id")), "Selection")),
                    name=str(_require(raw, str(maps.get("selection_name", "name")), "Selection")),
                    odds=as_decimal(
                        _require(raw, str(maps.get("selection_odds", "odds")), "Selection"),
                        "odds",
                    ),
                    market=market_name,
                    event_id=event_id,
                )
            )
        markets.append(Market(id=market_id, name=market_name, event_id=event_id, selections=selections))
    return markets


def filter_events(events: list[Event], query: str) -> list[Event]:
    needle = query.casefold().strip()
    if not needle:
        return list(events)
    matched: list[Event] = []
    for event in events:
        haystack = " ".join(
            [event.id, event.sport, event.competition, event.home_team, event.away_team, event.name]
        ).casefold()
        if needle in haystack:
            matched.append(event)
    return matched


def find_selection(markets: list[Market], selection_id: str) -> Selection | None:
    needle = selection_id.casefold()
    for market in markets:
        for selection in market.selections:
            if selection.id.casefold() == needle or selection.name.casefold() == needle:
                return selection
    return None
