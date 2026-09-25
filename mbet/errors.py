"""Typed failures. Messages are safe to print; they never include secrets."""

from __future__ import annotations


NOT_CONFIGURED = "Marathonbet transport adapter is not configured for this operation."


class MbetError(Exception):
    """Base error with a user-facing message and optional next step."""

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


class AuthenticationError(MbetError):
    """Credentials were rejected or no session exists."""


class SessionExpiredError(MbetError):
    """The stored session is no longer accepted."""


class VerificationRequiredError(MbetError):
    """The service wants an interactive check. Never bypass this."""


class RateLimitError(MbetError):
    """The service asked us to slow down."""

    def __init__(self, message: str, *, retry_after: float | None = None, hint: str | None = None) -> None:
        super().__init__(message, hint=hint)
        self.retry_after = retry_after


class EndpointError(MbetError):
    """An operation has no configured endpoint, or the server refused it."""


class InvalidResponseError(MbetError):
    """The body was not the JSON shape described by response_maps."""


class BetRejectedError(MbetError):
    """The service clearly refused the bet. It was not accepted."""


class BetStatusUnknownError(MbetError):
    """The bet may or may not have been accepted. Do not auto-retry."""


class ConfigurationError(MbetError):
    """Local configuration is missing or invalid."""


class BettingDisabledError(MbetError):
    """Live bet submission is switched off."""


class StakeLimitError(MbetError):
    """Stake is above MBET_MAX_STAKE / max_stake."""


class DuplicateSubmissionError(MbetError):
    """An identical bet is in-flight, unknown, or was just accepted."""
