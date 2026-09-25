"""Encrypted session storage. Passwords are never written."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from mbet.config import config_dir
from mbet.errors import ConfigurationError
from mbet.logging_config import get_logger
from mbet.models import UserSession

log = get_logger("storage")
_SERVICE = "mbet"
_KEY_ACCOUNT = "session-key"


def _keyring_enabled() -> bool:
    flag = os.environ.get("MBET_DISABLE_KEYRING", "")
    return flag.strip().lower() not in {"1", "true", "yes", "on"}


class SessionStore:
    """Persist cookies and the CSRF token encrypted at rest. Not the password."""

    def __init__(self, directory: Path | None = None, *, use_keyring: bool | None = None) -> None:
        self.directory = directory or config_dir()
        self.use_keyring = _keyring_enabled() if use_keyring is None else use_keyring

    @property
    def session_path(self) -> Path:
        return self.directory / "session.bin"

    @property
    def key_path(self) -> Path:
        return self.directory / "session.key"

    @property
    def ledger_path(self) -> Path:
        return self.directory / "submissions.json"

    def _ensure_dir(self) -> None:
        self.directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        try:
            os.chmod(self.directory, 0o700)
        except OSError:
            pass

    def _load_keyring_key(self) -> bytes | None:
        if not self.use_keyring:
            return None
        try:
            import keyring
            from keyring.errors import KeyringError
        except ImportError:
            return None
        try:
            existing = keyring.get_password(_SERVICE, _KEY_ACCOUNT)
            if existing:
                return existing.encode("utf-8")
            key = Fernet.generate_key().decode("utf-8")
            keyring.set_password(_SERVICE, _KEY_ACCOUNT, key)
            return key.encode("utf-8")
        except KeyringError:
            return None
        except Exception:
            # Headless environments often have no secret service. Fall back to a 0600 file.
            return None

    def _key(self) -> bytes:
        self._ensure_dir()
        keyring_key = self._load_keyring_key()
        if keyring_key:
            return keyring_key
        if self.key_path.exists():
            key = self.key_path.read_bytes().strip()
            if key:
                return key
        key = Fernet.generate_key()
        self._write_private(self.key_path, key)
        log.info("session key stored in a mode-0600 file because the OS keychain is unavailable")
        return key

    def _write_private(self, path: Path, data: bytes) -> None:
        self._ensure_dir()
        temporary = path.with_suffix(path.suffix + ".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        os.replace(temporary, path)
        os.chmod(path, 0o600)

    def save(self, session: UserSession) -> None:
        payload = session.model_dump(mode="json")
        # Defense in depth: a password must never be part of the session document.
        payload.pop("password", None)
        token = Fernet(self._key()).encrypt(json.dumps(payload).encode("utf-8"))
        self._write_private(self.session_path, token)
        log.info("session saved for user=%s", session.username)

    def load(self) -> UserSession | None:
        if not self.session_path.exists():
            return None
        raw = self.session_path.read_bytes()
        try:
            decoded = Fernet(self._key()).decrypt(raw)
            data = json.loads(decoded.decode("utf-8"))
        except (InvalidToken, json.JSONDecodeError, OSError):
            raise ConfigurationError(
                "Stored session could not be decrypted.",
                hint="Run mbet logout, then mbet login.",
            ) from None
        if not isinstance(data, dict):
            raise ConfigurationError("Stored session is invalid.", hint="Run mbet logout, then mbet login.")
        data.pop("password", None)
        return UserSession.model_validate(data)

    def clear(self) -> None:
        if self.session_path.exists():
            self.session_path.unlink()
        log.info("local session cleared")

    def mark_expired(self) -> None:
        session = None
        try:
            session = self.load()
        except ConfigurationError:
            self.clear()
            return
        if session is None:
            return
        session.authenticated = False
        session.expires_at = datetime.now(timezone.utc)
        session.cookies = {}
        session.csrf_token = None
        self.save(session)
