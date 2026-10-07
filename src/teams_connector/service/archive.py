"""Explicit archive work: bounded, resumable history and roster syncs, and per-chat coverage status.

Sync is the only way the archive grows without a read: it continues each chat's full-history
traversal from its saved continuation, refreshes the newest page of chats already complete, and
indexes rosters for participant search. Work is bounded per call and rotates fairly across chats.
"""

from __future__ import annotations

from datetime import UTC, datetime

from teams_connector.domain.errors import AuthenticationRequired, ConnectorError, InvalidRequest
from teams_connector.domain.models import ArchiveStatus, Chat, ChatArchiveState, Participant, SyncResult
from teams_connector.remote.ports import MemberReader
from teams_connector.service import cursors
from teams_connector.service.catalog import Catalog
from teams_connector.service.history import History, check_range
from teams_connector.store.db import ChatState, Store, from_iso, iso


class Archive:
    def __init__(
        self,
        store: Store,
        catalog: Catalog,
        history: History,
        chat_members: MemberReader,
        channel_members: MemberReader,
        me: Participant,
    ) -> None:
        self._store = store
        self._catalog = catalog
        self._history = history
        self._chat_members = chat_members
        self._channel_members = channel_members
        self._me = me

    async def members(self, chat: Chat) -> list[Participant]:
        if chat.kind == "self":  # the self chat's roster endpoint is not usable; it is just the user
            return [self._me]
        reader = self._channel_members if chat.kind == "channel" else self._chat_members
        return await reader.members(chat)

    async def sync(
        self,
        *,
        conversation_id: str | None = None,
        max_chats: int = 25,
        max_pages: int = 20,
        include_rosters: bool = True,
        refresh_latest: bool = True,
    ) -> SyncResult:
        await self._catalog.ensure(refresh=True)
        if conversation_id:
            chats = [await self._catalog.get(conversation_id)]
        else:
            states = self._store.states()
            chats = sorted(
                self._store.chats(),
                key=lambda c: (states[c.id].sync_attempted_at or 0) if c.id in states else 0,
            )[:max_chats]
        pages_left = max_pages
        results: list[ChatArchiveState] = []
        for chat in chats:
            state = self._store.state(chat.id)
            error = None
            if include_rosters and (not state.roster_complete or state.unresolved_participants):
                try:
                    self._store.save_roster(chat.id, await self.members(chat))
                except AuthenticationRequired:
                    raise
                except ConnectorError as exc:
                    self._store.save_roster(chat.id, None, error=str(exc))
            added = pages = 0
            if pages_left > 0 and (not state.history_complete or refresh_latest):
                before = self._store.stored_counts().get(chat.id, 0)
                try:
                    if state.history_complete:  # complete: just pick up the newest page
                        fetch = await self._history.fetch_range(chat, since=None, until=None, max_pages=1)
                    else:
                        fetch = await self._history.fetch_range(
                            chat, since=None, until=None, link=state.history_link, max_pages=pages_left
                        )
                        self._store.save_history_progress(
                            chat.id, link=fetch.link, complete=fetch.complete,
                            missing_timestamps=fetch.missing_timestamps,
                        )  # fmt: skip
                    pages = fetch.pages
                except AuthenticationRequired:
                    raise
                except ConnectorError as exc:
                    error = str(exc)
                pages_left -= pages
                added = self._store.stored_counts().get(chat.id, 0) - before
            self._store.touch_sync_attempt(chat.id)
            row = self._row(chat, self._store.state(chat.id), self._store.stored_counts().get(chat.id, 0))
            row.messages_added, row.pages_fetched, row.error = added, pages, error or row.error
            results.append(row)
        return SyncResult(items=results, pages_left=pages_left, totals=self._store.totals())

    def status(
        self,
        *,
        conversation_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> ArchiveStatus:
        if cursor:
            s = cursors.decode(cursor, "archive_status")
            conversation_id, offset = s["conversation_id"], int(s["offset"])
            since, until = from_iso(s["since"]), from_iso(s["until"])
        else:
            check_range(since, until)
            offset = 0
        chats = self._store.chats()
        if conversation_id:
            chats = [c for c in chats if c.id == conversation_id]
            if not chats:
                raise InvalidRequest(f"{conversation_id} is not in the stored chat catalog.")
        states = self._store.states()
        counts = self._store.stored_counts()
        rows = []
        covered = 0
        for index, chat in enumerate(chats):
            state = states.get(chat.id) or ChatState(chat.id)
            in_range = _range_covered(chat, state, since, until)
            covered += in_range
            if offset <= index < offset + limit:
                row = self._row(chat, state, counts.get(chat.id, 0))
                row.range_covered = in_range
                rows.append(row)
        next_offset = offset + len(rows)
        return ArchiveStatus(
            items=rows,
            cursor=cursors.encode(
                "archive_status",
                conversation_id=conversation_id,
                offset=next_offset,
                since=iso(since),
                until=iso(until),
            )
            if next_offset < len(chats)
            else None,  # fmt: skip
            covered=covered,
            totals=self._store.totals(),
        )

    @staticmethod
    def _row(chat: Chat, state: ChatState, stored: int) -> ChatArchiveState:
        return ChatArchiveState(
            conversation_id=chat.id,
            name=chat.name,
            history_complete=state.history_complete,
            messages_stored=stored,
            oldest_message_at=from_iso(state.oldest_message_at),
            newest_message_at=from_iso(state.newest_message_at),
            synced_at=datetime.fromtimestamp(state.history_synced_at, UTC)
            if state.history_synced_at
            else None,
            roster_complete=state.roster_complete,
            unresolved_participants=state.unresolved_participants,
            error=state.roster_error,
        )


def _range_covered(chat: Chat, state: ChatState, since: datetime | None, until: datetime | None) -> bool:
    """Whether the archive holds the chat's whole selection: the history traversal is complete (or the
    archived span encloses the range) and it is fresh up to the catalog's last activity."""
    oldest, newest = from_iso(state.oldest_message_at), from_iso(state.newest_message_at)
    latest = chat.last_message_at
    fresh = (newest is None and latest is None) or bool(newest and latest and newest >= latest)
    if since or until:
        if state.missing_timestamps:
            return False
        upper = (until is not None and newest is not None and until <= newest) or fresh
        if state.history_complete:
            return upper
        return bool(since and oldest and since >= oldest and upper)
    return state.history_complete and fresh
