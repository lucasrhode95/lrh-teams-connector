"""Teams Chat Service Aggregator (private): the chat catalog, as a snapshot or a delta.

The tested baseline lacked Graph chat-list access, so this adapter supplies the chat catalog. Channels in the
CSA rows are reported as ``channel_activity`` only: Graph is the channel catalog.
"""

from __future__ import annotations

import contextlib
import re
from typing import Any

from teams_connector.config import CSA_UPDATES_URL as UPDATES_URL
from teams_connector.domain.errors import InvalidRequest, Upstream
from teams_connector.domain.models import Chat
from teams_connector.remote import csa_mapping
from teams_connector.remote.chat_service import ChatService
from teams_connector.remote.csa_mapping import Me
from teams_connector.remote.ports import CatalogUpdate, People
from teams_connector.remote.teams_session import CsaCredential, TeamsSession
from teams_connector.remote.transport import TokenSource, Transport, operation

MAX_FALLBACK_NAME_LOOKUPS = 20


class CsaCatalog:
    def __init__(
        self,
        transport: Transport,
        session: TeamsSession,
        tokens: TokenSource,
        people: People,
        chat_service: ChatService,
        me: Me,
    ) -> None:
        self._transport = transport
        self._credential = CsaCredential(session, tokens)
        self._people = people
        self._chat_service = chat_service
        self._me = me

    async def _fetch(self, sync_token: str | None) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        if sync_token is not None:
            headers["x-ms-synctoken"] = sync_token
        data = await self._transport.json("GET", UPDATES_URL, credential=self._credential, headers=headers)
        return data if isinstance(data, dict) else {}

    async def updates(self, sync_token: str | None) -> CatalogUpdate:
        with operation("reading the chat catalog"):
            delta = sync_token is not None
            try:
                payload = await self._fetch(sync_token)
                if delta and _sync_token_expired(payload):
                    payload, delta = await self._fetch(None), False
            except (InvalidRequest, Upstream):
                if not delta:
                    raise
                payload, delta = await self._fetch(None), False  # rejected/expired continuation: snapshot
            return await self._normalize(payload, full=not delta)

    async def _normalize(self, payload: dict[str, Any], *, full: bool) -> CatalogUpdate:
        metadata = csa_mapping.mapping(payload.get("metadata"))
        token = metadata.get("syncToken") or metadata.get("forwardSyncToken")
        if not isinstance(token, str) or not token.strip():
            raise Upstream("The Teams chat catalog did not include a sync token.")
        raw_chats, raw_channels = payload.get("chats", []), payload.get("channels", [])
        if not isinstance(raw_chats, list) or not isinstance(raw_channels, list):
            raise Upstream("Unexpected Teams chat catalog response: chat or channel rows are invalid.")

        chats: list[Chat] = []
        deleted: list[str] = []
        channel_activity: list[Chat] = []
        try:
            for rows, channel in ((raw_chats, False), (raw_channels, True)):
                for row in rows:
                    if isinstance(row, dict) and csa_mapping.optional_bool(row.get("isDeleted")) is True:
                        deleted.append(csa_mapping.row_id(row, channel=channel))
                        continue
                    chat = csa_mapping.summarize_row(row, channel=channel, me=self._me)
                    if chat is not None:
                        (channel_activity if channel else chats).append(chat)
        except ValueError as exc:
            raise Upstream(str(exc)) from None

        partial = metadata.get("isPartialData") is True or metadata.get("hasMoreChats") is True
        complete = (
            True if metadata.get("isPartialData") is False and metadata.get("hasMoreChats") is False
            else False if partial else None
        )  # fmt: skip
        notes: list[str] = []
        if full and partial:
            added = await self._add_fallback_chats(chats)
            notes.append(f"The catalog service reported a partial snapshot; {added} chats were added "
                         "from the paginated Chat Service listing.")  # fmt: skip
            complete = True
        if full:
            existing = next((c for c in chats if c.id == csa_mapping.SELF_CHAT_ID), None)
            if existing is None:
                chats.append(csa_mapping.self_chat(self._me))
            else:
                chats[chats.index(existing)] = csa_mapping.self_chat(self._me, existing)

        plan = csa_mapping.plan_names(raw_chats, chats, payload.get("users"), self._me)
        if plan.missing_ids:
            with contextlib.suppress(Upstream):  # a name lookup must not make the catalog unavailable
                plan.apply(await self._people.names(plan.missing_ids))
        return CatalogUpdate(
            chats=chats,
            deleted_ids=deleted,
            sync_token=token.strip(),
            full=full,
            complete=complete,
            channel_activity=channel_activity,
            notes=notes,
        )

    async def _add_fallback_chats(self, chats: list[Chat]) -> int:
        """Complete a partial snapshot from the Chat Service listing (chat rows only)."""
        known = {chat.id for chat in chats}
        added: list[tuple[dict[str, Any], Chat]] = []
        for row in await self._chat_service.conversations():
            if row["id"] in known or csa_mapping.service_row_class(row) != "chat":
                continue
            chat = csa_mapping.summarize_service_row(row)
            chats.append(chat)
            added.append((row, chat))
            known.add(chat.id)
        untitled = [c for r, c in added if csa_mapping.is_untitled_one_on_one_service_row(r, c)]
        for chat in untitled[:MAX_FALLBACK_NAME_LOOKUPS]:
            try:
                members = await self._chat_service.members(chat)
            except (Upstream, InvalidRequest):
                continue
            peers = {p.id: p.name for p in members if p.id and p.id != self._me.user_id}
            if len(peers) == 1:
                peer, name = next(iter(peers.items()))
                if name:
                    chat.name = name
                if peer and not peer.startswith("28:"):
                    chat.photo_user_id = peer
        return len(added)


def _sync_token_expired(payload: dict[str, Any]) -> bool:
    error = csa_mapping.mapping(payload.get("error"))
    values = (
        error.get("code"), error.get("type"), error.get("message"),
        payload.get("errorCode"), payload.get("code"), payload.get("type"),
    )  # fmt: skip
    return any(
        isinstance(value, str) and "synctokenexpired" in re.sub(r"[^a-z]", "", value.casefold())
        for value in values
    )
