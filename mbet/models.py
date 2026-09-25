"""Domain models. Secrets are excluded from the default log view."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field


class UserSession(BaseModel):
    username: str
    authenticated: bool = True
    created_at: datetime
    expires_at: datetime | None = None
    cookies: dict[str, str] = Field(default_factory=dict)
    csrf_token: str | None = None

    def public_view(self) -> dict[str, object]:
        return self.model_dump(exclude={"cookies", "csrf_token"})


class AccountBalance(BaseModel):
    amount: Decimal
    currency: str
    session_state: str = "ACTIVE"


class Sport(BaseModel):
    id: str
    name: str


class Event(BaseModel):
    id: str
    sport: str = ""
    competition: str = ""
    start_time: datetime | None = None
    home_team: str = ""
    away_team: str = ""
    name: str = ""

    @property
    def match_label(self) -> str:
        if self.home_team or self.away_team:
            return f"{self.home_team} vs {self.away_team}".strip()
        return self.name or self.id


class Selection(BaseModel):
    id: str
    name: str
    odds: Decimal
    market: str = ""
    event_id: str = ""


class Market(BaseModel):
    id: str
    name: str
    event_id: str = ""
    selections: list[Selection] = Field(default_factory=list)


class BetSlip(BaseModel):
    event_id: str
    selection_id: str
    stake: Decimal
    odds: Decimal
    event_label: str = ""
    selection_name: str = ""
    currency: str = "USD"


class BetConfirmation(BaseModel):
    slip: BetSlip
    potential_return: Decimal
    dry_run: bool = False
    payload: dict[str, object] = Field(default_factory=dict)
    endpoint_configured: bool = False


class BetResult(BaseModel):
    status: Literal["accepted", "rejected", "unknown"]
    bet_id: str | None = None
    message: str = ""
    stake: Decimal | None = None
    odds: Decimal | None = None
    potential_return: Decimal | None = None


class BetHistoryItem(BaseModel):
    id: str
    event_id: str = ""
    event_name: str = ""
    selection: str = ""
    stake: Decimal
    odds: Decimal | None = None
    status: str = ""
    placed_at: datetime | None = None
    potential_return: Decimal | None = None
