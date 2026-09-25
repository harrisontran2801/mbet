"""Login state. Passwords stay in memory only for the duration of the request."""

from __future__ import annotations

from datetime import datetime, timezone

from mbet.adapter import MarathonAdapter
from mbet.errors import AuthenticationError, SessionExpiredError
from mbet.logging_config import get_logger
from mbet.models import UserSession
from mbet.storage import SessionStore

log = get_logger("auth")


class AuthService:
    def __init__(self, adapter: MarathonAdapter, store: SessionStore) -> None:
        self.adapter = adapter
        self.store = store

    def assert_login_configured(self) -> None:
        assert_configured = getattr(self.adapter, "assert_login_configured", None)
        if callable(assert_configured):
            assert_configured()

    def login(self, username: str, password: str) -> UserSession:
        if not username.strip():
            raise AuthenticationError("Username is required.")
        if not password:
            raise AuthenticationError("Password is required.")
        session = self.adapter.login(username.strip(), password)
        self.store.save(session)
        log.info("login succeeded user=%s", session.username)
        return session

    def logout(self) -> bool:
        """Clear the local session. Returns True when a remote logout endpoint was called."""
        remote = bool(getattr(getattr(self.adapter, "config", None), "endpoints", None) and self.adapter.config.endpoints.logout.strip())  # type: ignore[attr-defined]
        try:
            self.adapter.logout()
        finally:
            self.store.clear()
        log.info("logout complete remote=%s", remote)
        return remote

    def current(self) -> UserSession | None:
        return self.store.load()

    def require(self) -> UserSession:
        session = self.current()
        if session is None or not session.authenticated:
            raise AuthenticationError("Not logged in.", hint="mbet login")
        if session.expires_at is not None:
            expires = session.expires_at
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if expires <= datetime.now(timezone.utc):
                raise SessionExpiredError("Marathonbet session expired.", hint="mbet login")
        return session
