"""Conversation history: remote pages merged into the archive, read back in bounded pages.

Chats are read from Chat Service and channels from Graph (bootstrap.py wires the two readers). Every
fetched message is retained in the archive, so a history page is the archive's view of the selection:
the server's messages plus retained ones the server no longer returns (deleted, or past retention).

Remote pages go from newer to older. A page reports a ``floor``: every message newer than it has been
delivered. The archive is only read down to the floor until the remote traversal completes, so a page
never skips a message that simply has not been fetched yet.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from teams_connector.domain.errors import InvalidRequest, Upstream
from teams_connector.domain.models import Chat, HistoryCoverage, Message, MessagePage
from teams_connector.remote.ports import HistoryReader
from teams_connector.service import cursors
from teams_connector.service.catalog import Catalog
from teams_connector.store.db import Store, from_iso, iso

REMOTE_PAGE_SIZE = 100
MAX_REMOTE_PAGES_PER_CALL = 10
MAX_STALLED_PAGES = 3  # remote pages in a row without a new message id: the service is looping


@dataclass
class RangeFetch:
    pages: int
    scanned: int
    selected: int
    missing_timestamps: int
    complete: bool  # the selection (or its latest-N limit) was fully fetched
    link: str | None  # where to continue when ``complete`` is False


def check_range(since: datetime | None, until: datetime | None) -> None:
    for name, value in (("since", since), ("until", until)):
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise InvalidRequest(f"{name} must include a timezone, for example 2026-09-28T12:00:00Z.")
    if since and until and since > until:
        raise InvalidRequest("since must be earlier than or equal to until.")


class History:
    def __init__(
        self,
        store: Store,
        catalog: Catalog,
        chats: HistoryReader,
        channels: HistoryReader,
        me_id: str,
    ) -> None:
        self._store = store
        self._catalog = catalog
        self._chats = chats
        self._channels = channels
        self._me_id = me_id

    def reader(self, chat: Chat) -> HistoryReader:
        return self._channels if chat.kind == "channel" else self._chats

    def annotate(self, message: Message) -> Message:
        """Per-result fields: whether the signed-in user wrote it."""
        if message.author_id:
            message.is_self = message.author_id == self._me_id
        return message

    # ---------------------------------------------------------------- traversal

    async def fetch_range(
        self,
        chat: Chat,
        *,
        since: datetime | None,
        until: datetime | None,
        limit: int | None = None,
        link: str | None = None,
        max_pages: int | None = None,
        on_page: Callable[[int, int], None] | None = None,
    ) -> RangeFetch:
        """Fetch a selection newest first into the archive, until it ends, ``limit`` messages of it were
        fetched, or ``max_pages`` pages were read (then ``link`` continues it)."""
        reader = self.reader(chat)
        pages = scanned = selected = missing = stalled = 0
        seen_links: set[str] = set()
        seen_ids: set[str] = set()
        while True:
            if max_pages is not None and pages >= max_pages:
                return RangeFetch(pages, scanned, selected, missing, False, link)
            page = await reader.page(chat, since=since, until=until, page_size=REMOTE_PAGE_SIZE, link=link)
            self._store.save_messages(chat.id, page.messages)
            pages += 1
            scanned += page.scanned
            selected += len(page.messages)
            missing += page.missing_timestamps
            new_ids = {m.id for m in page.messages} - seen_ids
            seen_ids |= new_ids
            stalled = 0 if new_ids or not page.messages else stalled + 1
            if on_page:
                on_page(pages, selected)
            if limit is not None and selected >= limit:
                return RangeFetch(pages, scanned, selected, missing, True, None)
            if not page.next_link:
                return RangeFetch(pages, scanned, selected, missing, True, None)
            if page.next_link in seen_links or stalled >= MAX_STALLED_PAGES:
                raise Upstream("Teams repeated a history continuation; the history read was stopped.")
            if link:
                seen_links.add(link)
            link = page.next_link

    # ---------------------------------------------------------------- history pages

    async def get_messages(
        self,
        conversation_id: str,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        message_limit: int | None = None,
        page_size: int = 25,
        max_chars: int = 30_000,
        cursor: str | None = None,
    ) -> MessagePage:
        if cursor:
            s = cursors.decode(cursor, "get_messages")
            conversation_id = s["conversation_id"]
            since, until = from_iso(s["since"]), from_iso(s["until"])
            message_limit, page_size, max_chars = s["limit"], s["page_size"], s["max_chars"]
        else:
            check_range(since, until)
            s = {
                "conversation_id": conversation_id,
                "since": iso(since),
                "until": iso(until),
                "limit": message_limit,
                "page_size": page_size,
                "max_chars": max_chars,
                "link": None,
                "started": False,
                "remote_complete": False,
                "floor": None,
                "before": None,
                "returned": 0,
                "remote_pages": 0,
            }
        chat = await self._catalog.get(conversation_id)
        reader = self.reader(chat)
        want = page_size if message_limit is None else min(page_size, message_limit - s["returned"])
        before = tuple(s["before"]) if s["before"] else None
        fetched: set[str] = set()

        pages_this_call = 0
        while not s["remote_complete"] and pages_this_call < MAX_REMOTE_PAGES_PER_CALL and want > 0:
            if s["started"] and s["floor"]:
                lower = max(d for d in (since, from_iso(s["floor"])) if d)
                ready = self._store.newest_first(chat.id, since=lower, until=until, before=before, limit=want)
                if len(ready) >= want:
                    break
            page = await reader.page(
                chat, since=since, until=until, page_size=REMOTE_PAGE_SIZE, link=s["link"]
            )
            self._store.save_messages(chat.id, page.messages)
            fetched |= {m.id for m in page.messages}
            pages_this_call += 1
            s["started"] = True
            s["remote_pages"] += 1
            if page.floor and (s["floor"] is None or iso(page.floor) < s["floor"]):  # type: ignore[operator]
                s["floor"] = iso(page.floor)
            if page.next_link and page.next_link == s["link"]:
                raise Upstream("Teams repeated a history continuation; the history read was stopped.")
            s["link"] = page.next_link
            s["remote_complete"] = page.next_link is None

        if want <= 0:
            items: list[Message] = []
        elif s["remote_complete"]:
            items = self._store.newest_first(chat.id, since=since, until=until, before=before, limit=want)
        elif s["floor"]:
            lower = max(d for d in (since, from_iso(s["floor"])) if d)
            items = self._store.newest_first(chat.id, since=lower, until=until, before=before, limit=want)
        else:
            items = []

        whole = _whole_messages(items, max_chars)
        trimmed = len(whole) < len(items)
        items = whole
        if items:
            last = items[-1]
            s["before"] = [iso(last.sent_at), last.id]
        s["returned"] += len(items)
        limit_reached = message_limit is not None and s["returned"] >= message_limit
        more_local = bool(
            items
            and s["remote_complete"]
            and self._store.newest_first(
                chat.id, since=since, until=until, before=tuple(s["before"]), limit=1
            )
        )
        has_more = not limit_reached and (trimmed or not s["remote_complete"] or more_local)

        ordered = [self.annotate(m) for m in reversed(items)]
        notes = []
        if has_more and not items:
            notes.append("The remote history is still being fetched for this selection; follow the cursor.")
        if trimmed:
            notes.append("The page ends before the next message, which would exceed max_chars.")
        return MessagePage(
            chat=chat,
            items=ordered,
            cursor=cursors.encode("get_messages", **s) if has_more else None,
            coverage=HistoryCoverage(
                complete=not has_more,
                remote_complete=bool(s["remote_complete"]),
                remote_pages=int(s["remote_pages"]),
                archive_only=sum(1 for m in items if m.id not in fetched),
                message_limit_reached=limit_reached,
                notes=notes,
            ),
        )


def _whole_messages(items: list[Message], max_chars: int) -> list[Message]:
    """The leading messages (newest first) whose texts fit ``max_chars`` together. The first message is
    always kept whole, so a page always moves on, even when that message alone exceeds the budget."""
    kept: list[Message] = []
    used = 0
    for message in items:
        if kept and used + len(message.text) > max_chars:
            break
        kept.append(message)
        used += len(message.text)
    return kept
