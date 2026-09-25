"""Stake limits, confirmation math, dry-run, and single-submit protection."""

from __future__ import annotations

import json
import os
import time
from decimal import Decimal, InvalidOperation
from typing import Any

from mbet.adapter import ConfigurableHttpAdapter, MarathonAdapter
from mbet.config import AppConfig
from mbet.errors import (
    BettingDisabledError,
    BetRejectedError,
    BetStatusUnknownError,
    ConfigurationError,
    DuplicateSubmissionError,
    EndpointError,
    MbetError,
    RateLimitError,
    SessionExpiredError,
    StakeLimitError,
    VerificationRequiredError,
)
from mbet.logging_config import get_logger
from mbet.models import BetConfirmation, BetResult, BetSlip
from mbet.storage import SessionStore

log = get_logger("bets")

_SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£"}


def format_money(amount: Decimal, currency: str) -> str:
    quantized = Decimal(amount).quantize(Decimal("0.01"))
    symbol = _SYMBOLS.get(currency.upper())
    if symbol:
        return f"{symbol}{quantized}"
    return f"{quantized} {currency}"


def format_odds(odds: Decimal) -> str:
    quantized = odds.quantize(Decimal("0.001"))
    text = format(quantized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def parse_stake(raw: str) -> Decimal:
    try:
        stake = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        raise ConfigurationError("Stake is not a number.") from None
    if not stake.is_finite() or stake <= 0:
        raise ConfigurationError("Stake must be greater than zero.")
    return stake.quantize(Decimal("0.01"))


def potential_return(stake: Decimal, odds: Decimal) -> Decimal:
    return (stake * odds).quantize(Decimal("0.01"))


def enforce_max_stake(stake: Decimal, config: AppConfig) -> None:
    limit = Decimal(config.max_stake).quantize(Decimal("0.01"))
    if stake > limit:
        raise StakeLimitError(
            f"Requested stake exceeds configured maximum of {format_money(limit, config.currency)}."
        )


def fingerprint(slip: BetSlip) -> str:
    import hashlib

    stake = slip.stake.quantize(Decimal("0.01"))
    odds = slip.odds.quantize(Decimal("0.001"))
    raw = f"{slip.event_id}|{slip.selection_id}|{stake}|{odds}|{slip.currency}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class SubmissionLedger:
    """Remember in-flight and unknown bets so a second submit is refused."""

    def __init__(self, store: SessionStore) -> None:
        self.store = store

    def _read(self) -> dict[str, Any]:
        path = self.store.ledger_path
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict[str, Any]) -> None:
        self.store._write_private(self.store.ledger_path, json.dumps(data).encode("utf-8"))

    def get(self, key: str) -> dict[str, Any] | None:
        row = self._read().get(key)
        return row if isinstance(row, dict) else None

    def record(self, key: str, status: str) -> None:
        data = self._read()
        data[key] = {"status": status, "ts": time.time()}
        self._write(data)
        log.info("bet_ledger status=%s", status)


class BetService:
    def __init__(self, config: AppConfig, adapter: MarathonAdapter, store: SessionStore) -> None:
        self.config = config
        self.adapter = adapter
        self.store = store
        self.ledger = SubmissionLedger(store)

    def confirm(self, slip: BetSlip, *, dry_run: bool) -> BetConfirmation:
        enforce_max_stake(slip.stake, self.config)
        payload: dict[str, object] = {}
        endpoint_configured = bool((self.config.endpoints.place_bet or "").strip())
        if isinstance(self.adapter, ConfigurableHttpAdapter):
            payload = self.adapter.describe_bet_payload(slip)
            endpoint_configured = bool(payload.get("endpoint_configured") and payload.get("body_configured"))
        return BetConfirmation(
            slip=slip,
            potential_return=potential_return(slip.stake, slip.odds),
            dry_run=dry_run,
            payload=payload,
            endpoint_configured=endpoint_configured,
        )

    def ensure_live_allowed(self) -> None:
        if not self.config.betting_enabled:
            raise BettingDisabledError(
                "Bet submission is disabled.",
                hint="Reading data is still allowed. Set betting_enabled: true and MBET_BETTING_ENABLED=true only when you intend to send a bet. Nothing was submitted.",
            )

    def submit(self, slip: BetSlip, *, acknowledge_unknown: bool = False) -> BetResult:
        """Place at most one HTTP bet. Unknown results are recorded and not retried."""
        enforce_max_stake(slip.stake, self.config)
        self.ensure_live_allowed()
        if isinstance(self.adapter, ConfigurableHttpAdapter):
            self.adapter.require_endpoint("place_bet")
            if not self.config.request_templates.place_bet.body:
                raise ConfigurationError(
                    "Bet request body is not configured.",
                    hint="Nothing was submitted.",
                )
        key = fingerprint(slip)
        self._guard(key, acknowledge_unknown)
        self.ledger.record(key, "in_flight")
        try:
            result = self.adapter.place_bet(slip)
        except BetStatusUnknownError:
            self.ledger.record(key, "unknown")
            raise
        except BetRejectedError:
            self.ledger.record(key, "rejected")
            raise
        except (RateLimitError, SessionExpiredError, EndpointError, VerificationRequiredError):
            self.ledger.record(key, "not_accepted")
            raise
        except MbetError:
            self.ledger.record(key, "unknown")
            raise
        except Exception:
            self.ledger.record(key, "unknown")
            raise BetStatusUnknownError(
                "Bet submission status is uncertain. Do NOT retry automatically.",
                hint="No automatic retry was performed.",
            ) from None
        if result.status != "accepted":
            self.ledger.record(key, "unknown")
            raise BetStatusUnknownError(
                "Bet submission status is uncertain. Do NOT retry automatically.",
                hint="No automatic retry was performed.",
            )
        self.ledger.record(key, "accepted")
        log.info("bet_accepted id=%s", result.bet_id or "")
        return result

    def _guard(self, key: str, acknowledge_unknown: bool) -> None:
        existing = self.ledger.get(key)
        if not existing:
            return
        status = str(existing.get("status", ""))
        if status in {"unknown", "in_flight"}:
            if acknowledge_unknown:
                log.info("unknown bet explicitly acknowledged; a new single submit is allowed")
                return
            raise DuplicateSubmissionError(
                "A previous bet with the same event, selection, odds, and stake was not confirmed.",
                hint="No automatic retry was performed. Check your account. If you verified it was not accepted, re-run with --acknowledge-unknown.",
            )
        if status == "accepted":
            age = time.time() - float(existing.get("ts", 0))
            window = self.config.duplicate_window_seconds
            if age < window:
                raise DuplicateSubmissionError(
                    "An identical bet was accepted recently. Refusing to submit a duplicate.",
                    hint=f"Wait {window} seconds if you really want another identical bet.",
                )


def betting_enabled_from_env(default: bool) -> bool:
    raw = os.environ.get("MBET_BETTING_ENABLED")
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
