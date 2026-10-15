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
import re
import time
from datetime import UTC, datetime

from teams_connector.domain.errors import AuthenticationRequired, ConnectorError, InvalidRequest, NotFound
from teams_connector.domain.models import (
    CatalogCoverage,
    Chat,
    ChatKind,
    ChatMatch,
    ChatPage,
    ChatSearchCoverage,
    ChatSearchResult,
    Participant,
)
from teams_connector.remote.ports import ChannelDirectory, ChatCatalog, ChatMemberSearch, HistoryReader
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
        members: ChatMemberSearch | None = None,
    ) -> None:
        self._store = store
        self._source = source
        self._channels = channels
        self._chat_history = chat_history
        self._members = members
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
            await self._refresh_all()

    async def _refresh_all(self) -> None:
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

    async def recipient(self, conversation_id: str) -> Chat:
        """The current chat for one send. Only the recipient is refreshed: a one-on-one, group or meeting
        chat by the catalog's delta, a channel by its team's listing and its own read. The self chat is
        read from the store. The freshness mark of the stored catalog does not change, so listings still
        wait for a full refresh."""
        conversation_id = _conversation_id(conversation_id)
        async with self._lock:
            stored = self._store.chat(conversation_id)
            if stored is None:
                await self._refresh_all()
            elif stored.kind == "channel":
                await self._refresh_channel(stored)
            elif stored.kind != "self":
                await self._refresh_chats()
        return await self.get(conversation_id)

    async def _refresh_chats(self) -> None:
        """The chat delta alone. Its sync token is not stored, so the next full refresh reads the same
        delta again; applying it twice is harmless, and channel activity in it is never lost."""
        update = await self._source.updates(self._store.meta("csa_sync_token"))
        if update.full:  # the token was rejected: only a full refresh can rebuild the snapshot
            await self._refresh_all()
            return
        self._store.save_catalog(update.chats, deleted_ids=update.deleted_ids)

    async def _refresh_channel(self, channel: Chat) -> None:
        """One channel's name and membership from its team, keeping its stored activity."""
        fresh = await self._channels.channel(channel.team_id, channel.id) if channel.team_id else None
        if fresh is None:  # gone or moved, or its team is unknown: the full refresh decides what is left
            await self._refresh_all()
            return
        self._store.save_catalog(
            [
                fresh.model_copy(
                    update={
                        "last_message_at": channel.last_message_at,
                        "last_message_preview": channel.last_message_preview,
                        "unread": channel.unread,
                    }
                )
            ]
        )

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

    # ---------------------------------------------------------------- reads

    async def get(self, conversation_id: str) -> Chat:
        """A catalog chat, refreshing the catalog once if it is not known yet."""
        conversation_id = _conversation_id(conversation_id)
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
        """Chats whose name contains the query, and, with refresh, the named group and meeting chats Teams
        matches by a plain name or email, with the members that matched."""
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
        by_member, complete, notes = await self._member_matches(query, asked=refresh)
        matches: list[ChatMatch] = []
        for chat in self._store.chats():
            if folded not in fold(chat.name) and chat.id not in by_member:
                continue
            matches.append(
                ChatMatch(
                    chat=chat,
                    name_matched=folded in fold(chat.name),
                    matched_participants=by_member.get(chat.id, []),
                )
            )
        page = matches[offset : offset + limit]
        next_offset = offset + len(page)
        return ChatSearchResult(
            query=query,
            items=page,
            cursor=cursors.encode("search_chats", query=query, offset=next_offset)
            if next_offset < len(matches)
            else None,
            coverage=ChatSearchCoverage(complete=complete, chats=len(self._store.chats()), notes=notes),
        )

    async def _member_matches(
        self, query: str, *, asked: bool
    ) -> tuple[dict[str, list[Participant]], bool, list[str]]:
        """Chats Teams matches by a plain name or email, with the members that matched, and whether Teams
        answered. Fails open: when Teams does not answer, the local catalog still answers."""
        if not asked:
            return {}, False, ["Teams' chat search was not asked: refresh=false searches the cache only."]
        if self._members is None:
            return {}, False, []
        if not _plain_text(query):
            return (
                {},
                False,
                ["Teams' chat search takes a plain name or email; this query was not sent to it."],
            )
        try:
            hits = await self._members.named_chats(query)
        except ConnectorError as exc:
            reason = type(exc).__name__
            return (
                {},
                False,
                [f"Teams' chat search did not answer ({reason}); chats matched by member may be missing."],
            )
        notes = [
            "Teams' chat search covers named group and meeting chats with recent activity; "
            "one-on-one chats are matched by name."
        ]
        unknown = sum(1 for hit in hits if self._store.chat(hit.chat_id) is None)
        if unknown:
            notes.append(
                f"{unknown} chats matched by Teams are not in the local catalog; refresh it to list them."
            )
        return {hit.chat_id: hit.matching_members for hit in hits}, True, notes


def _conversation_id(value: str) -> str:
    if not value or not value.strip():
        raise InvalidRequest("conversation_id must be a non-empty string.")
    return value.strip()


_PLAIN_TEXT = re.compile(r"[^\"()*:]{1,200}")
_QUERY_OPERATOR = re.compile(r"\b(?:and|or|not)\b", re.IGNORECASE)


def _plain_text(query: str) -> bool:
    """A plain name or email: no quotes, parentheses, wildcards, property syntax or query operators."""
    return bool(_PLAIN_TEXT.fullmatch(query)) and not _QUERY_OPERATOR.search(query)


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
