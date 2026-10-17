"""Domain errors. Each surface maps these to its own protocol (CLI exit codes, MCP tool errors, HTTP)."""

from __future__ import annotations

from m365_auth import (
    AccountMismatch,
    AuthenticationRequired,
    SecureStorageUnavailable,
)
from m365_auth import (
    AuthError as ConnectorError,
)


class ConfigurationError(ConnectorError):
    """The application configuration is invalid (for example a denied client/scope pair)."""


class InvalidRequest(ConnectorError):
    """The caller asked for something malformed or unsupported."""


class DownloadLimitExceeded(InvalidRequest):
    """A download exceeded the connector's local size limit."""

    def __init__(self, limit_bytes: int) -> None:
        self.limit_bytes = limit_bytes
        mebibytes, remainder = divmod(limit_bytes, 1024 * 1024)
        self.limit = f"{mebibytes} MiB" if remainder == 0 else f"{limit_bytes:,} bytes"
        super().__init__(f"The file exceeds the connector's {self.limit} download limit.")


class NotFound(ConnectorError):
    """The requested item does not exist (or no longer exists) on the server or locally."""


class Throttled(ConnectorError):
    """Microsoft is throttling requests; retry later."""


class Upstream(ConnectorError):
    """A Microsoft service could not be reached or returned an unexpected failure."""


class PermissionDenied(Upstream):
    """Microsoft denied this operation for the current account or item (HTTP 403); the sign-in itself is
    fine. A kind of ``Upstream``, so code that tolerates an upstream failure tolerates this too."""


class WriteOutcomeUnknown(ConnectorError):
    """A write was sent but its outcome is unknown (no response). Check before retrying; never resend."""


class Cancelled(ConnectorError):
    """The caller cancelled a long-running operation."""


__all__ = [
    "AccountMismatch",
    "AuthenticationRequired",
    "Cancelled",
    "ConfigurationError",
    "ConnectorError",
    "DownloadLimitExceeded",
    "InvalidRequest",
    "NotFound",
    "PermissionDenied",
    "SecureStorageUnavailable",
    "Throttled",
    "Upstream",
    "WriteOutcomeUnknown",
]
