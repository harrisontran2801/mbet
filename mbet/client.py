"""High-level MarathonClient. CLI commands talk to this, not to raw HTTP."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from mbet.adapter import ConfigurableHttpAdapter, MarathonAdapter
from mbet.auth import AuthService
from mbet.bets import BetService, parse_stake
from mbet.config import AppConfig, load_config
from mbet.errors import ConfigurationError, EndpointError
from mbet.logging_config import get_logger, setup_logging
from mbet.markets import find_selection
from mbet.models import (
    AccountBalance,
    BetHistoryItem,
    BetResult,
    BetSlip,
    Event,
    Market,
    Selection,
    Sport,
    UserSession,
)
from mbet.storage import SessionStore
from mbet.transport import HttpTransport

log = get_logger("client")


class MarathonClient:
    def __init__(
        self,
        config: AppConfig,
        *,
        store: SessionStore | None = None,
        transport: HttpTransport | None = None,
        adapter: MarathonAdapter | None = None,
    ) -> None:
        self.config = config
        self.store = store or SessionStore()
        if adapter is not None and transport is not None:
            self.transport = transport
            self.adapter = adapter
        elif adapter is not None:
            self.transport = transport or HttpTransport(config, self.store)
            self.adapter = adapter
        else:
            self.transport = transport or HttpTransport(config, self.store)
            self.adapter = ConfigurableHttpAdapter(config, self.transport, self.store)
        self.auth = AuthService(self.adapter, self.store)
        self.bets = BetService(config, self.adapter, self.store)

    def close(self) -> None:
        self.transport.close()

    def status(self) -> dict[str, object]:
        session: UserSession | None
        try:
            session = self.auth.current()
        except ConfigurationError:
            session = None
        state = "NONE"
        username = ""
        if session is not None:
            username = session.username
            if not session.authenticated:
                state = "EXPIRED"
            elif session.expires_at is not None and session.expires_at <= datetime.now(timezone.utc):
                state = "EXPIRED"
            else:
                state = "ACTIVE"
        endpoints = {}
        for name in (
            "login",
            "logout",
            "balance",
            "sports",
            "events",
            "event",
            "odds",
            "history",
            "place_bet",
            "search",
        ):
            endpoints[name] = bool(str(getattr(self.config.endpoints, name) or "").strip())
        return {
            "username": username,
            "session": state,
            "betting_enabled": self.config.betting_enabled,
            "max_stake": self.config.max_stake,
            "currency": self.config.currency,
            "base_url": self.config.base_url,
            "endpoints": endpoints,
        }

    def balance(self) -> AccountBalance:
        self._ready("balance")
        balance = self.adapter.get_balance()
        balance.session_state = "ACTIVE"
        return balance

    def sports(self) -> list[Sport]:
        self._ready("sports")
        return self.adapter.get_sports()

    def events(self, sport: str | None = None) -> list[Event]:
        self._ready("events")
        return self.adapter.get_events(sport)

    def event(self, event_id: str) -> Event:
        if not (self.config.endpoints.event or self.config.endpoints.events):
            raise EndpointError(
                "Marathonbet transport adapter is not configured for this operation.",
                hint="Set endpoints.event or endpoints.events.",
            )
        self.auth.require()
        return self.adapter.get_event(event_id)

    def odds(self, event_id: str) -> list[Market]:
        self._ready("odds")
        return self.adapter.get_odds(event_id)

    def search(self, query: str) -> list[Event]:
        if not (self.config.endpoints.search or self.config.endpoints.events):
            raise EndpointError(
                "Marathonbet transport adapter is not configured for this operation.",
                hint="Set endpoints.search or endpoints.events.",
            )
        self.auth.require()
        return self.adapter.search(query)

    def history(self) -> list[BetHistoryItem]:
        self._ready("history")
        return self.adapter.get_history()

    def resolve_selection(self, event_id: str, selection: str, odds: Decimal | None) -> Selection:
        if odds is not None:
            return Selection(id=selection, name=selection, odds=odds, event_id=event_id)
        markets = self.odds(event_id)
        found = find_selection(markets, selection)
        if found is None:
            raise ConfigurationError(
                f"Selection '{selection}' was not in the odds response.",
                hint="Pass --odds if you already know the price, or check mbet odds.",
            )
        return found

    def build_slip(
        self,
        *,
        event_id: str,
        selection: str,
        stake: str,
        odds: str | None = None,
        event_label: str = "",
        selection_name: str = "",
    ) -> BetSlip:
        stake_value = parse_stake(stake)
        odds_value = Decimal(odds) if odds else None
        resolved = self.resolve_selection(event_id, selection, odds_value)
        if not event_label:
            if self.config.endpoints.event or self.config.endpoints.events:
                try:
                    event_label = self.event(event_id).match_label
                except Exception:
                    event_label = event_id
            else:
                event_label = event_id
        return BetSlip(
            event_id=event_id,
            selection_id=resolved.id,
            stake=stake_value,
            odds=resolved.odds,
            event_label=event_label,
            selection_name=selection_name or resolved.name,
            currency=self.config.currency,
        )

    def submit_bet(self, slip: BetSlip, *, acknowledge_unknown: bool = False) -> BetResult:
        self.auth.require()
        return self.bets.submit(slip, acknowledge_unknown=acknowledge_unknown)

    def _ready(self, endpoint: str) -> None:
        if isinstance(self.adapter, ConfigurableHttpAdapter):
            self.adapter.require_endpoint(endpoint)
        self.auth.require()


def build_client(config: AppConfig | None = None) -> MarathonClient:
    cfg = config or load_config()
    setup_logging(10 if cfg.debug else 20)
    return MarathonClient(cfg)
