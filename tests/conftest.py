from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import httpx

from mbet.adapter import ConfigurableHttpAdapter
from mbet.client import MarathonClient
from mbet.config import AppConfig, CsrfConfig, Endpoints, RequestTemplate, RequestTemplates, RetryConfig
from mbet.models import UserSession
from mbet.storage import SessionStore
from mbet.transport import HttpTransport


def make_config(**overrides: object) -> AppConfig:
    config = AppConfig(
        base_url="https://book.example",
        currency="USD",
        max_stake=Decimal("10.00"),
        betting_enabled=True,
        timeout=5,
        debug=False,
        timezone="UTC",
        duplicate_window_seconds=120,
        endpoints=Endpoints(
            login="/session/login",
            logout="/session/logout",
            balance="/account/balance",
            sports="/sports",
            events="/events",
            event="/events/{event_id}",
            odds="/events/{event_id}/odds",
            history="/bets/history",
            place_bet="/bets",
            search="",
        ),
        csrf=CsrfConfig(
            header="X-CSRF-Token",
            cookie="",
            response_json="csrfToken",
            required=True,
            required_for_login=False,
        ),
        request_templates=RequestTemplates(
            login=RequestTemplate(
                method="POST",
                content_type="json",
                body={"username": "{username}", "password": "{password}"},
            ),
            logout=RequestTemplate(method="POST", content_type="json", body={}),
            place_bet=RequestTemplate(
                method="POST",
                content_type="json",
                success_statuses=[200, 201],
                body={
                    "eventId": "{event_id}",
                    "selectionId": "{selection_id}",
                    "stake": "{stake}",
                    "odds": "{odds}",
                },
            ),
        ),
        retries=RetryConfig(max_attempts=3, backoff_seconds=0, max_backoff_seconds=0),
    )
    if not overrides:
        return config
    from mbet.config import deep_merge

    return AppConfig.model_validate(deep_merge(config.model_dump(mode="json"), overrides))


def scripted_client(tmp_path, handler, **overrides: object) -> MarathonClient:
    config = make_config(**overrides)
    store = SessionStore(directory=tmp_path, use_keyring=False)
    transport = HttpTransport(
        config,
        store,
        client=httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False),
    )
    adapter = ConfigurableHttpAdapter(config, transport, store)
    return MarathonClient(config, store=store, transport=transport, adapter=adapter)


def seed_session(client: MarathonClient, *, csrf: str | None = "csrf-NOT-IN-LOGS", cookie: str | None = "cookie-NOT-IN-LOGS") -> None:
    cookies = {}
    if cookie is not None:
        cookies["MBSESSION"] = cookie
    client.store.save(
        UserSession(
            username="alice",
            authenticated=True,
            created_at=datetime.now(timezone.utc),
            cookies=cookies,
            csrf_token=csrf,
        )
    )


def combined(result) -> str:
    return f"{result.stdout or ''}{result.stderr or ''}{getattr(result, 'output', '') or ''}"
