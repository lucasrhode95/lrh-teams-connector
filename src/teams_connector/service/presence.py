"""Presence for a one-on-one peer or signed-in user: read on request, never blocking the chat list.

A successful read is kept for a minute in this process only; nothing is stored. A failed read is not
cached and does not fail the call: it returns no presence and the reason, so the caller can go on.
"""

from __future__ import annotations

import time

from teams_connector.domain.errors import AuthenticationRequired, ConnectorError, InvalidRequest
from teams_connector.domain.models import Presence, PresenceResult
from teams_connector.remote.ports import PresenceReader
from teams_connector.service.catalog import Catalog

CACHE_SECONDS = 60
ORG_PREFIX = "8:orgid:"


def _person_id(value: str | None) -> str | None:
    """The Entra object id of the other person; None for bots and for chats without a person."""
    if not value:
        return None
    if value.startswith(ORG_PREFIX):
        return value[len(ORG_PREFIX) :] or None
    return None if value.startswith("28:") else value


class Presences:
    def __init__(self, catalog: Catalog, source: PresenceReader) -> None:
        self._catalog = catalog
        self._source = source
        self._cache: dict[str, tuple[float, Presence | None]] = {}

    async def for_chat(self, conversation_id: str) -> PresenceResult:
        """Entry point: the presence of the other person in a one-on-one chat."""
        chat = await self._catalog.get(conversation_id)
        if chat.kind != "one_on_one":
            raise InvalidRequest("Presence is available for one-on-one chats only.")
        user_id = _person_id(chat.photo_user_id)
        if user_id is None:
            return PresenceResult(reason="This chat has no person to read presence for.")
        return await self._for_person(user_id)

    async def for_self(self, user_id: str) -> PresenceResult:
        """Read the signed-in user's presence; callers must resolve the id from the active account."""
        return await self._for_person(user_id)

    async def _for_person(self, user_id: str) -> PresenceResult:
        """Read a server-resolved Entra id through the shared minute cache."""
        cached = self._cache.get(user_id)
        if cached is not None and cached[0] > time.monotonic():
            return _result(cached[1])
        try:
            presence = await self._source.presence(user_id)
        except AuthenticationRequired:
            raise
        except ConnectorError as exc:  # fail open: the caller goes on without presence
            return PresenceResult(reason=f"Presence was not read: {exc}")
        self._cache[user_id] = (time.monotonic() + CACHE_SECONDS, presence)
        return _result(presence)


def _result(presence: Presence | None) -> PresenceResult:
    if presence is None:
        return PresenceResult(reason="Teams reports no presence for this person.")
    return PresenceResult(presence=presence)
