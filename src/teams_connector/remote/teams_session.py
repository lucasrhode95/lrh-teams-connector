"""Teams private-service session: the AuthZ exchange and the credentials built on it.

``POST authsvc/v1.0/authz`` with the ``teams`` (Skype spaces) token returns a Skype token and the
account's regional service URLs. Chat Service requests authenticate with the Skype token; CSA requests
with the ``csa`` bearer token plus the Skype token. Graph never needs this session.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from teams_connector.config import AUTHZ_URL
from teams_connector.domain.errors import Upstream
from teams_connector.remote.transport import Bearer, TokenSource, Transport, host_allowed, operation

_DEFAULT_LIFETIME = 50 * 60
_RENEW_MARGIN = 5 * 60


@dataclass(frozen=True)
class SessionInfo:
    skype_token: str
    chat_service_url: str  # regional Chat Service base, e.g. https://emea.ng.msg.teams.microsoft.com
    expires_at: float


class TeamsSession:
    def __init__(self, transport: Transport, tokens: TokenSource) -> None:
        self._transport = transport
        self._tokens = tokens
        self._spaces = Bearer(tokens, "teams")
        self._info: SessionInfo | None = None
        self._lock = asyncio.Lock()

    async def info(self, *, renew: dict[str, Any] | None = None) -> SessionInfo:
        """The current session, exchanging tokens when there is none, it expires, or ``renew`` asks."""
        async with self._lock:
            if renew is None and self._info and self._info.expires_at - time.time() > _RENEW_MARGIN:
                return self._info
            self._info = await self._authorize(renew)
            return self._info

    async def chat_service_url(self) -> str:
        return (await self.info()).chat_service_url

    def sign_in_command(self) -> str:
        return self._tokens.sign_in_command("teams")

    async def _authorize(self, renew: dict[str, Any] | None) -> SessionInfo:
        with operation("starting the Teams session"):
            credential = _Renewed(self._spaces, renew)
            data = await self._transport.json(
                "POST",
                AUTHZ_URL,
                credential=credential,
                headers={"Accept": "application/json; ver=1.0"},
                content=b"",
                retry=True,  # a token exchange: safe to repeat
            )
        if not isinstance(data, dict):
            raise Upstream("Teams AuthZ returned no session.")
        tokens = _mapping(data.get("tokens"))
        nested = _mapping(data.get("skypeToken"))
        skype = tokens.get("skypeToken") or tokens.get("skypetoken") or nested.get("skypetoken")
        chat_service = _mapping(data.get("regionGtms")).get("chatService")
        if not isinstance(skype, str) or not skype:
            raise Upstream("Teams AuthZ succeeded without returning a Skype token.")
        if not isinstance(chat_service, str) or not chat_service.startswith("https://"):
            raise Upstream("Teams AuthZ did not return a Chat Service URL.")
        if not host_allowed(urlsplit(chat_service).hostname):
            raise Upstream("Teams AuthZ returned a Chat Service URL outside the Teams hosts.")
        lifetime = tokens.get("expiresIn") or nested.get("expiresIn")
        try:
            seconds = float(lifetime) if lifetime else _DEFAULT_LIFETIME
        except (TypeError, ValueError):
            seconds = _DEFAULT_LIFETIME
        return SessionInfo(skype, chat_service.rstrip("/"), time.time() + seconds)


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


class _Renewed:
    """A bearer credential whose first use applies a pending renewal (force refresh / claims)."""

    def __init__(self, bearer: Bearer, renewal: dict[str, Any] | None) -> None:
        self._bearer = bearer
        self._pending = renewal

    async def headers(self, renewal: dict[str, Any] | None) -> dict[str, str]:
        pending, self._pending = self._pending, None
        return await self._bearer.headers(renewal or pending)

    def sign_in_command(self) -> str:
        return self._bearer.sign_in_command()


class SkypeCredential:
    """Chat Service authentication: the session's Skype token. A rejected token renews the session."""

    def __init__(self, session: TeamsSession) -> None:
        self._session = session

    async def headers(self, renewal: dict[str, Any] | None) -> dict[str, str]:
        info = await self._session.info(renew=renewal)
        return {"Authentication": f"skypetoken={info.skype_token}"}

    def sign_in_command(self) -> str:
        return self._session.sign_in_command()


class CsaCredential:
    """CSA authentication: the ``csa`` bearer token plus the session's Skype token."""

    def __init__(self, session: TeamsSession, tokens: TokenSource) -> None:
        self._session = session
        self._bearer = Bearer(tokens, "csa")

    async def headers(self, renewal: dict[str, Any] | None) -> dict[str, str]:
        info = await self._session.info(renew=renewal)
        return {**(await self._bearer.headers(renewal)), "x-skypetoken": info.skype_token}

    def sign_in_command(self) -> str:
        return self._bearer.sign_in_command()
