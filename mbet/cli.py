"""Terminal interface. Passwords are prompted, never accepted as flags."""

from __future__ import annotations

import functools
import logging
from decimal import Decimal
from typing import Any, Callable

import typer
import yaml
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from mbet import __version__
from mbet.bets import enforce_max_stake, format_money, format_odds, parse_stake
from mbet.client import MarathonClient, build_client
from mbet.config import load_config
from mbet.errors import (
    AuthenticationError,
    BetRejectedError,
    BetStatusUnknownError,
    EndpointError,
    MbetError,
    SessionExpiredError,
    StakeLimitError,
)
from mbet.logging_config import setup_logging
from mbet.models import BetSlip, Event, Market
from mbet.redact import redact_headers

from typer.exceptions import Abort, Exit

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Terminal client for your Marathonbet session. No browser, no guessed API.",
)
console = Console()
err = Console(stderr=True)


def _guard(fn: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except (Exit, Abort):
            raise
        except MbetError as exc:
            _render_error(exc)
            raise typer.Exit(code=1)
        except Exception as exc:
            err.print(f"[red]✗[/] Unexpected error ({type(exc).__name__}). Details were redacted.")
            logging.getLogger("mbet.cli").error("unexpected %s", type(exc).__name__)
            raise Exit(code=1)

    return wrapper


def _render_error(exc: MbetError) -> None:
    if isinstance(exc, SessionExpiredError):
        err.print("[red]✗[/] Marathonbet session expired.")
        err.print("Run:\n")
        err.print("[bold]mbet login[/]")
        return
    if isinstance(exc, AuthenticationError) and exc.message == "Authentication failed":
        err.print("[red]✗[/] Authentication failed")
        return
    if isinstance(exc, BetStatusUnknownError):
        err.print("[yellow]⚠ Bet status could not be confirmed.[/]")
        err.print("No automatic retry was performed.")
        return
    if isinstance(exc, StakeLimitError):
        err.print(f"[red]✗[/] {exc.message}")
        return
    err.print(f"[red]✗[/] {exc.message}")
    if exc.hint:
        err.print(exc.hint)


def _client() -> MarathonClient:
    return build_client()


def _format_when(event: Event, timezone_name: str) -> str:
    if event.start_time is None:
        return "—"
    try:
        from zoneinfo import ZoneInfo

        zone = ZoneInfo(timezone_name)
    except Exception:
        from datetime import timezone

        zone = timezone.utc
    return event.start_time.astimezone(zone).strftime("%H:%M")


def _events_table(events: list[Event], timezone_name: str) -> Table:
    table = Table(box=box.SIMPLE, header_style="bold", pad_edge=False)
    table.add_column("EVENT")
    table.add_column("TIME")
    table.add_column("MATCH")
    for event in events:
        table.add_row(event.id, _format_when(event, timezone_name), event.match_label)
    return table


def _print_odds(event_id: str, markets: list[Market]) -> None:
    console.print(f"[bold]EVENT[/] {event_id}")
    if not markets:
        console.print("No markets in the response.")
        return
    for market in markets:
        console.print(f"\n[bold]{market.name}[/]")
        for selection in market.selections:
            console.print(f"{selection.name}  {format_odds(selection.odds)}")


@app.command()
def version() -> None:
    """Print the mbet version."""
    console.print(f"mbet {__version__}")


@app.command()
@_guard
def login() -> None:
    """Sign in. The password is hidden and is not stored."""
    client = _client()
    try:
        client.auth.assert_login_configured()
        console.print("Marathonbet login")
        username = typer.prompt("Username/email")
        password = typer.prompt("Password", hide_input=True)
        console.print("Authenticating...")
        try:
            client.auth.login(username, password)
        finally:
            password = ""
            del password
        console.print("[green]✓ Login successful[/]")
    finally:
        client.close()


@app.command()
@_guard
def logout() -> None:
    """Clear the local session and call logout if it is configured."""
    client = _client()
    try:
        remote = client.auth.logout()
        if remote:
            console.print("[green]✓ Logged out[/]")
        else:
            console.print("[green]✓ Local session cleared[/]")
            console.print("Remote logout endpoint is not configured, so the server session was not revoked.")
    finally:
        client.close()


@app.command()
@_guard
def status() -> None:
    """Show session, safety switches, and which endpoints are configured."""
    client = _client()
    try:
        info = client.status()
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style="bold")
        grid.add_column()
        grid.add_row("Session", str(info["session"]))
        if info["username"]:
            grid.add_row("Account", str(info["username"]))
        grid.add_row("Betting", "ENABLED" if info["betting_enabled"] else "DISABLED")
        grid.add_row("Max stake", format_money(Decimal(str(info["max_stake"])), str(info["currency"])))
        grid.add_row("Base URL", str(info["base_url"]))
        endpoints = info["endpoints"]
        assert isinstance(endpoints, dict)
        for name, configured in endpoints.items():
            grid.add_row(name, "configured" if configured else "not configured")
        console.print(Panel(grid, title="mbet status", box=box.SQUARE, border_style="green"))
        if not info["betting_enabled"]:
            console.print("Bet submission is off. Balance, events, and odds stay available once configured.")
    finally:
        client.close()


@app.command()
@_guard
def balance() -> None:
    """Show the account balance."""
    client = _client()
    try:
        account = client.balance()
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style="bold")
        grid.add_column()
        grid.add_row("Balance", format_money(account.amount, account.currency))
        grid.add_row("Currency", account.currency)
        grid.add_row("Session", account.session_state)
        console.print(Panel(grid, title="Marathonbet Account", box=box.SQUARE, border_style="green"))
    finally:
        client.close()


@app.command("sports")
@_guard
def sports_cmd() -> None:
    """List sports from the configured endpoint."""
    client = _client()
    try:
        rows = client.sports()
        table = Table(box=box.SIMPLE, header_style="bold")
        table.add_column("ID")
        table.add_column("SPORT")
        for sport in rows:
            table.add_row(sport.id, sport.name)
        console.print(table if rows else "No sports returned.")
    finally:
        client.close()


@app.command("events")
@_guard
def events_cmd(sport: str | None = typer.Option(None, "--sport", help="Filter by sport name or id.")) -> None:
    """List events."""
    client = _client()
    try:
        rows = client.events(sport)
        console.print(_events_table(rows, client.config.timezone) if rows else "No events returned.")
    finally:
        client.close()


@app.command("event")
@_guard
def event_cmd(event_id: str = typer.Argument(..., metavar="EVENT_ID")) -> None:
    """Show one event."""
    client = _client()
    try:
        event = client.event(event_id)
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style="bold")
        grid.add_column()
        grid.add_row("Event", event.id)
        grid.add_row("Match", event.match_label)
        grid.add_row("Sport", event.sport or "—")
        grid.add_row("Competition", event.competition or "—")
        grid.add_row("Time", _format_when(event, client.config.timezone))
        console.print(Panel(grid, title="Event", box=box.SQUARE))
    finally:
        client.close()


@app.command("odds")
@_guard
def odds_cmd(event_id: str = typer.Argument(..., metavar="EVENT_ID")) -> None:
    """Show markets and prices for an event."""
    client = _client()
    try:
        _print_odds(event_id, client.odds(event_id))
    finally:
        client.close()


@app.command("search")
@_guard
def search_cmd(query: str = typer.Argument(..., help="Team, competition, or event id.")) -> None:
    """Search events already returned by the configured feed."""
    client = _client()
    try:
        rows = client.search(query)
        console.print(_events_table(rows, client.config.timezone) if rows else "No matching events.")
    finally:
        client.close()


@app.command("history")
@_guard
def history_cmd() -> None:
    """Show bet history from the configured endpoint."""
    client = _client()
    try:
        rows = client.history()
        table = Table(box=box.SIMPLE, header_style="bold")
        table.add_column("ID")
        table.add_column("EVENT")
        table.add_column("SELECTION")
        table.add_column("STAKE")
        table.add_column("ODDS")
        table.add_column("STATUS")
        for item in rows:
            table.add_row(
                item.id,
                item.event_name or item.event_id,
                item.selection,
                format_money(item.stake, client.config.currency),
                format_odds(item.odds) if item.odds is not None else "—",
                item.status,
            )
        console.print(table if rows else "No history returned.")
    finally:
        client.close()


@app.command("bet")
@_guard
def bet_cmd(
    event: str | None = typer.Option(None, "--event"),
    selection: str | None = typer.Option(None, "--selection"),
    stake: str | None = typer.Option(None, "--stake"),
    odds: str | None = typer.Option(None, "--odds"),
    event_name: str | None = typer.Option(None, "--event-name"),
    selection_name: str | None = typer.Option(None, "--selection-name"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the bet and do not send it."),
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt. Off unless you pass it."),
    acknowledge_unknown: bool = typer.Option(
        False,
        "--acknowledge-unknown",
        help="Allow one new submit after you verified an earlier identical bet was not accepted.",
    ),
) -> None:
    """Prepare a bet. Nothing is sent until you confirm, and never on --dry-run."""
    client = _client()
    try:
        event_id = event or typer.prompt("Event ID")
        selection_id = selection or typer.prompt("Selection ID")
        stake_raw = stake or typer.prompt("Stake")
        stake_value = parse_stake(stake_raw)
        enforce_max_stake(stake_value, client.config)
        if not dry_run:
            client.bets.ensure_live_allowed()
        odds_value = Decimal(odds) if odds else None
        label = event_name or ""
        picked_name = selection_name or ""
        if odds_value is None:
            slip = client.build_slip(
                event_id=event_id,
                selection=selection_id,
                stake=format(stake_value, "f"),
                event_label=label,
                selection_name=picked_name,
            )
        else:
            if not label:
                label = event_id
            slip = BetSlip(
                event_id=event_id,
                selection_id=selection_id,
                stake=stake_value,
                odds=odds_value,
                event_label=label,
                selection_name=picked_name or selection_id,
                currency=client.config.currency,
            )
        confirmation = client.bets.confirm(slip, dry_run=dry_run)
        grid = Table.grid(padding=(0, 1))
        grid.add_column()
        grid.add_row("Event:")
        grid.add_row(slip.event_label)
        grid.add_row("")
        grid.add_row("Selection:")
        grid.add_row(slip.selection_name)
        grid.add_row("")
        grid.add_row("Odds:")
        grid.add_row(format_odds(slip.odds))
        grid.add_row("")
        grid.add_row("Stake:")
        grid.add_row(format_money(slip.stake, slip.currency))
        grid.add_row("")
        grid.add_row("Potential return:")
        grid.add_row(format_money(confirmation.potential_return, slip.currency))
        console.print(Panel(grid, title="Bet", box=box.SQUARE))
        if dry_run:
            _print_dry_run(confirmation.payload, client.config.betting_enabled)
            return
        if not yes:
            confirmed = typer.confirm("Confirm bet?", default=False)
            if not confirmed:
                console.print("Cancelled. No bet was submitted.")
                return
        result = client.submit_bet(slip, acknowledge_unknown=acknowledge_unknown)
        console.print(f"[green]✓ Bet accepted[/] {result.bet_id or ''}".rstrip())
        if result.message:
            console.print(result.message)
    finally:
        client.close()


def _print_dry_run(payload: dict[str, object], betting_enabled: bool) -> None:
    console.print("[bold]Dry run.[/] No bet was submitted.")
    if not betting_enabled:
        console.print("Live submission is disabled. This preview would still be blocked.")
    if not payload:
        console.print("No request template is available on this adapter.")
        return
    if not payload.get("endpoint_configured") or not payload.get("body_configured"):
        console.print("The place-bet endpoint or body template is not configured, so there is nothing to send.")
    safe = dict(payload)
    if "csrf_header" in safe:
        safe["csrf_header"] = f"{safe['csrf_header']}: ***"
    console.print_json(data=_jsonable(safe))


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


@app.command("config")
@_guard
def config_cmd() -> None:
    """Print the effective configuration with sensitive header values redacted."""
    cfg = load_config()
    setup_logging(logging.DEBUG if cfg.debug else logging.INFO)
    dumped = cfg.model_dump(mode="json")
    headers = dumped.get("headers") or {}
    if isinstance(headers, dict):
        dumped["headers"] = redact_headers({str(key): str(val) for key, val in headers.items()})
    text = Text(yaml.safe_dump(dumped, sort_keys=False))
    console.print(Panel(text, title="Effective config", box=box.SQUARE))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
