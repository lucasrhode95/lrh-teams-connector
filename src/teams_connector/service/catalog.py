"""The chat catalog: chats from the Teams catalog service, channels from Graph, cached in the store.

- Chats (one-on-one, group, meeting, self) come from the private catalog as a delta against a stored
  sync token; a rejected token falls back to a full snapshot.
- Channels come from Graph (every channel of every joined team). The private catalog's channel rows
  only contribute activity (last message, unread) and the few channels Graph's listing omits, which are
  then found by id in the joined teams.
- The self chat's activity is refreshed from its history: the catalog's deltas omit it.
- Listing answers from the store when it is fresh (stale-while-revalidate is not needed: a delta costs
  one request).
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime

from teams_connector.domain.errors import AuthenticationRequired, ConnectorError, InvalidRequest, NotFound
from teams_connector.domain.models import (
    CatalogCoverage,
    Chat,
    ChatKind,
    ChatMatch,
    ChatPage,
    ChatSearchResult,
    RosterCoverage,
)
from teams_connector.remote.ports import ChannelDirectory, ChatCatalog, HistoryReader
from teams_connector.service import cursors
from teams_connector.store.db import Store, fold

log = logging.getLogger(__name__)

CATALOG_TTL_SECONDS = 10 * 60
SCOPE = (
    "Chats from the Teams chat catalog (one-on-one, group, meeting and the self chat) and every channel of "
    "the user's joined teams. Teams' internal streams (notifications, call logs) are not conversations."
)


class Catalog:
    def __init__(
        self,
        store: Store,
        source: ChatCatalog,
        channels: ChannelDirectory,
        chat_history: HistoryReader,
    ) -> None:
        self._store = store
        self._source = source
        self._channels = channels
        self._chat_history = chat_history
        self._lock = asyncio.Lock()
        self._notes: list[str] = []

    # ---------------------------------------------------------------- refresh

    def age(self) -> float | None:
        stamp = self._store.meta("catalog_at")
        return time.time() - float(stamp) if stamp else None

    async def ensure(self, *, refresh: bool) -> str:
        """Make the stored catalog usable. Returns the source: 'remote' (refreshed now) or 'local'."""
        age = self.age()
        if refresh or age is None or age > CATALOG_TTL_SECONDS:
            await self.refresh()
            return "remote"
        return "local"

    async def refresh(self) -> None:
        async with self._lock:
            bootstrapped = self._store.meta("catalog_bootstrapped") == "1"
            token = self._store.meta("csa_sync_token") if bootstrapped else None
            update = await self._source.updates(token)
            notes = list(update.notes)

            known_channels = {c.id: c for c in self._store.chats(kind="channel")}
            vanished: list[str] = []
            try:
                listed = {c.id: c for c in await self._channels.channels()}
                # Channels the listing omits: rows the private catalog knows, and channels we stored before.
                missing = [c.id for c in update.channel_activity if c.id not in listed]
                missing += [i for i in known_channels if i not in listed and i not in missing]
                for chat in await self._channels.locate(missing):
                    listed[chat.id] = chat
                vanished = [i for i in known_channels if i not in listed]
            except AuthenticationRequired:
                raise
            except ConnectorError as exc:  # channels stay as stored; chats still refresh
                listed = {}
                notes.append(f"Channels were not refreshed: {exc}")
            activity = {c.id: c for c in update.channel_activity}
            channels = [_with_activity(chat, activity.get(chat.id)) for chat in listed.values()]

            meta = {"csa_sync_token": update.sync_token, "catalog_at": str(time.time())}
            if update.full:
                meta["catalog_bootstrapped"] = "1"
                meta["catalog_complete"] = "1" if update.complete is not False else "0"
            self._store.save_catalog(
                [*update.chats, *channels], deleted_ids=[*update.deleted_ids, *vanished], meta=meta
            )
            self._notes = notes
            await self._refresh_self_chat()

    async def _refresh_self_chat(self) -> None:
        """The catalog's deltas omit the self chat's activity; read its newest message instead."""
        chat = next((c for c in self._store.chats(kind="self")), None)
        if chat is None:
            return
        try:
            page = await self._chat_history.page(chat, since=None, until=None, page_size=1, link=None)
        except ConnectorError as exc:
            log.warning("Self-chat activity refresh failed (%s)", type(exc).__name__)
            return
        if not page.messages:
            self._store.save_catalog([chat.model_copy(update={"is_empty": True})])
            return
        latest = page.messages[-1]
        preview = " ".join(latest.text.split())[:240] or None
        self._store.save_catalog(
            [
                chat.model_copy(
                    update={
                        "last_message_at": latest.sent_at,
                        "last_message_preview": preview,
                        "is_empty": False,
                    }
                )
            ]  # fmt: skip
        )
        self._store.save_messages(chat.id, page.messages)

    # ---------------------------------------------------------------- reads

    async def get(self, conversation_id: str) -> Chat:
        """A catalog chat, refreshing the catalog once if it is not known yet."""
        if not conversation_id or not conversation_id.strip():
            raise InvalidRequest("conversation_id must be a non-empty string.")
        conversation_id = conversation_id.strip()
        if self.age() is None:
            await self.refresh()
        chat = self._store.chat(conversation_id)
        if chat is None and (self.age() or 0) > 5:
            await self.refresh()
            chat = self._store.chat(conversation_id)
        if chat is None:
            raise NotFound(f"{conversation_id} is not in the chat catalog. Use list_chats or search_chats.")
        return chat

    async def list(
        self,
        *,
        refresh: bool = False,
        limit: int = 50,
        cursor: str | None = None,
        include_empty: bool = False,
        kind: ChatKind | None = None,
    ) -> ChatPage:
        if cursor:
            state = cursors.decode(cursor, "list_chats")
            offset, include_empty, kind = (
                int(state["offset"]),
                bool(state["include_empty"]),
                state["chat_kind"],
            )
            source = "local"
        else:
            offset = 0
            source = await self.ensure(refresh=refresh)
        everything = self._store.chats()
        chats = [c for c in everything if include_empty or c.is_empty is not True]
        empty_excluded = len(everything) - len(chats)
        if kind:
            chats = [c for c in chats if c.kind == kind]
        page = chats[offset : offset + limit]
        next_offset = offset + len(page)
        next_cursor = (
            cursors.encode("list_chats", offset=next_offset, include_empty=include_empty, chat_kind=kind)
            if next_offset < len(chats)
            else None
        )
        return ChatPage(
            items=page, cursor=next_cursor, coverage=self.coverage(source, len(chats), empty_excluded)
        )

    def coverage(self, source: str, total: int, empty_excluded: int = 0) -> CatalogCoverage:
        stamp = self._store.meta("catalog_at")
        return CatalogCoverage(
            source="remote" if source == "remote" else "local",
            complete=self._store.meta("catalog_complete") != "0",
            scope=SCOPE,
            total=total,
            empty_excluded=empty_excluded,
            refreshed_at=datetime.fromtimestamp(float(stamp), UTC) if stamp else None,
            notes=list(self._notes),
        )

    async def search(
        self, query: str, *, refresh: bool = True, limit: int = 25, cursor: str | None = None
    ) -> ChatSearchResult:
        """Chats whose name, or one of whose indexed participants' names, contains the query."""
        if cursor:
            state = cursors.decode(cursor, "search_chats")
            query, offset = str(state["query"]), int(state["offset"])
        else:
            if not query or not query.strip():
                raise InvalidRequest("query must be a non-empty string.")
            query, offset = query.strip(), 0
            if refresh:
                await self.ensure(refresh=True)
            elif self.age() is None:
                raise InvalidRequest(
                    "No cached chat catalog is available; repeat with refresh=true to search Teams."
                )
        folded = fold(query)
        participants = self._store.participant_matches(query)
        matches = [
            ChatMatch(
                chat=chat,
                name_matched=folded in fold(chat.name),
                matched_participants=participants.get(chat.id, []),
            )  # fmt: skip
            for chat in self._store.chats()
            if folded in fold(chat.name) or chat.id in participants
        ]
        page = matches[offset : offset + limit]
        next_offset = offset + len(page)
        return ChatSearchResult(
            query=query,
            items=page,
            cursor=cursors.encode("search_chats", query=query, offset=next_offset)
            if next_offset < len(matches)
            else None,
            coverage=self.roster_coverage(),
        )

    def roster_coverage(self) -> RosterCoverage:
        chats = self._store.chats()
        states = self._store.states()
        indexed = [states[c.id] for c in chats if c.id in states and states[c.id].roster_complete]
        unresolved = sum(s.unresolved_participants for s in indexed)
        missing = len(chats) - len(indexed)
        notes = []
        if missing:
            notes.append(
                f"{missing} chats have no indexed roster yet, so participant matches may be missing "
                "for them; "
                "sync_archive indexes rosters. Chat names are always searched."
            )
        return RosterCoverage(
            complete=missing == 0 and unresolved == 0 and bool(chats),
            chats=len(chats),
            rosters_indexed=len(indexed),
            rosters_missing=missing,
            unresolved_participants=unresolved,
            notes=notes,
        )


def _with_activity(channel: Chat, activity: Chat | None) -> Chat:
    if activity is None:
        return channel
    return channel.model_copy(
        update={
            "last_message_at": activity.last_message_at,
            "last_message_preview": activity.last_message_preview,
            "unread": activity.unread,
        }
    )
