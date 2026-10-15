"""Conversation history, read from Teams on every call. Nothing is stored locally.

Chats are read from Chat Service and channels from Graph (bootstrap.py wires the two readers). Remote pages
come newest first, and messages within a page are oldest first. A response holds up to ``page_size`` whole
messages within ``max_chars`` (at least one), newest first, and is returned oldest first. The cursor carries
the remote link of the page being read and the oldest message the response returned, so a walk continues
without skipping or repeating messages.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime

from teams_connector.domain.errors import InvalidRequest, NotFound, Upstream
from teams_connector.domain.models import Chat, HistoryCoverage, Message, MessagePage
from teams_connector.remote.ports import ChannelHistoryReader, ChatHistoryReader, HistoryReader
from teams_connector.service import cursors
from teams_connector.service.catalog import Catalog
from teams_connector.store.db import from_iso, iso

REMOTE_PAGE_SIZE = 100
MAX_REMOTE_PAGES_PER_CALL = 10  # bounds the time one call spends reading Teams


class History:
    def __init__(
        self,
        catalog: Catalog,
        chats: ChatHistoryReader,
        channels: ChannelHistoryReader,
        me_id: str,
    ) -> None:
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

    async def selection(
        self,
        chat: Chat,
        *,
        since: datetime | None,
        until: datetime | None,
        message_limit: int | None = None,
    ) -> AsyncIterator[list[Message]]:
        """A chat's selection page by page for exports: newest page first, each page oldest first. Stops after
        ``message_limit`` messages, the latest ones.

        Assumes (not re-checked here): ``since`` and ``until`` are aware datetimes, checked by the caller.
        """
        reader = self.reader(chat)
        link: str | None = None
        seen: set[str] = set()
        taken = 0
        while True:
            page = await reader.page(chat, since=since, until=until, page_size=REMOTE_PAGE_SIZE, link=link)
            messages = page.messages
            if message_limit is not None:
                remaining = message_limit - taken
                messages = messages[max(0, len(messages) - remaining) :]
            taken += len(messages)
            if messages:
                yield [self.annotate(message) for message in messages]
            if message_limit is not None and taken >= message_limit:
                return
            if not page.next_link:
                return
            link = _next_link(page.next_link, seen)

    async def message(self, conversation_id: str, message_id: str) -> Message:
        """Entry point: one message read from Teams by its id, for downloads."""
        if not message_id or not message_id.strip():
            raise InvalidRequest("message_id must be a non-empty string.")
        message_id = message_id.strip()
        chat = await self._catalog.get(conversation_id)
        located = await self._chats.message(chat, message_id)
        if chat.kind == "channel":
            return self.annotate(await self._channel_message(chat, located))
        return self.annotate(located)

    async def resolve(
        self, conversation_id: str, *, message_id: str | None, sent_at: datetime | None
    ) -> Message:
        """Entry point: the one message a download names, by its id or by its arrival time. A channel message
        is found through Chat Service, which names a reply's thread, and read through Graph, as get_messages
        reads it, so its names do not depend on the route. A time that several messages share is refused
        with their ids."""
        if message_id is not None and sent_at is None:
            return await self.message(conversation_id, message_id)
        if message_id is None and sent_at is not None:
            chat = await self._catalog.get(conversation_id)
            found = await self._chats.messages_at(chat, sent_at)
            if not found:
                raise NotFound("No message arrived at that time.")
            if len(found) > 1:
                ids = ", ".join(repr(message.id) for message in found)
                raise InvalidRequest(
                    f"Several messages arrived at that time; pass message_id (one of {ids})."
                )
            if chat.kind == "channel":
                return self.annotate(await self._channel_message(chat, found[0]))
            return self.annotate(found[0])
        raise InvalidRequest("Pass exactly one of message_id or sent_at.")

    async def _channel_message(self, chat: Chat, located: Message) -> Message:
        """The Graph copy of a channel message Chat Service located: a post by its id, a reply through
        its thread's root."""
        if located.reply_to_id:
            return await self._channels.reply(chat, located.reply_to_id, located.id)
        return await self._channels.message(chat, located.id)

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
        """Entry point: one response of a conversation's history, newest first within the walk, oldest first
        within the response. With a cursor, the selection and paging come from the cursor."""
        if cursor:
            state = cursors.decode(cursor, "get_messages")
            conversation_id = state["conversation_id"]
            since, until = from_iso(state["since"]), from_iso(state["until"])
            message_limit, page_size, max_chars = state["limit"], state["page_size"], state["max_chars"]
            link, before, returned = state["link"], _marker(state["before"]), int(state["returned"])
        else:
            check_range(since, until)
            link, before, returned = None, None, 0
        chat = await self._catalog.get(conversation_id)
        reader = self.reader(chat)
        # The cursor's marker, until a page shows it or older messages: new messages can push it past the
        # first page between calls.
        pending = before
        seen: set[str] = set()

        items: list[Message] = []  # newest first
        used = 0
        pages = 0
        missing = 0
        remote_complete = False
        stopped = False
        while not stopped:
            full = len(items) >= page_size
            limit_reached = message_limit is not None and returned + len(items) >= message_limit
            if full or limit_reached or pages >= MAX_REMOTE_PAGES_PER_CALL:
                stopped = True
                break
            page = await reader.page(chat, since=since, until=until, page_size=REMOTE_PAGE_SIZE, link=link)
            pages += 1
            missing += page.missing_timestamps
            candidates = list(reversed(page.messages))
            if pending is not None:
                candidates, passed = _after_marker(candidates, pending)
                if passed:
                    pending = None
            for message in candidates:
                over_count = len(items) >= page_size
                over_limit = message_limit is not None and returned + len(items) >= message_limit
                over_budget = bool(items) and used + len(message.text) > max_chars
                if over_count or over_limit or over_budget:
                    stopped = True
                    break
                items.append(message)
                used += len(message.text)
                before = _marker_of(message)
            else:
                if not page.next_link:
                    remote_complete = True
                    break
                link = _next_link(page.next_link, seen)
                if pending is None:
                    before = None  # the next page continues this read: nothing in it was returned
                continue
            break

        limit_reached = message_limit is not None and returned + len(items) >= message_limit
        has_more = not remote_complete and not limit_reached
        notes: list[str] = []
        if has_more and not items:
            notes.append("The remote history is still being read for this selection; follow the cursor.")
        if has_more and stopped and not limit_reached:
            notes.append("The page ends before the next message, which would exceed max_chars or page_size.")
        if missing:
            notes.append(f"{missing} message(s) without a usable time were left out by since and until.")
        next_cursor = None
        if has_more:
            next_cursor = cursors.encode(
                "get_messages",
                conversation_id=conversation_id,
                since=iso(since),
                until=iso(until),
                limit=message_limit,
                page_size=page_size,
                max_chars=max_chars,
                link=link,
                before=before,
                returned=returned + len(items),
            )
        return MessagePage(
            chat=chat,
            items=[self.annotate(message) for message in reversed(items)],
            cursor=next_cursor,
            coverage=HistoryCoverage(
                complete=next_cursor is None,
                remote_complete=remote_complete,
                remote_pages=pages,
                message_limit_reached=limit_reached,
                notes=notes,
            ),
        )


def check_range(since: datetime | None, until: datetime | None) -> None:
    for name, value in (("since", since), ("until", until)):
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise InvalidRequest(f"{name} must include a timezone, for example 2026-09-28T12:00:00Z.")
    if since and until and since > until:
        raise InvalidRequest("since must be earlier than or equal to until.")


def _marker_of(message: Message) -> list[str | None]:
    return [iso(message.sent_at), message.id]


def _marker(value: object) -> list[str | None] | None:
    if value is None:
        return None
    if not isinstance(value, list) or len(value) != 2 or not isinstance(value[1], str):
        raise InvalidRequest("The history cursor has an invalid position.")
    return value


def _after_marker(candidates: list[Message], marker: list[str | None]) -> tuple[list[Message], bool]:
    """The messages (newest first) that come after the marked message, and whether this page reached it.
    The page reaches it when it holds the marked message or older ones; a page of newer messages only
    (new arrivals pushed the marker further) gives nothing and leaves the marker pending."""
    stamp, marked_id = marker
    ids = [message.id for message in candidates]
    if marked_id in ids:
        return candidates[ids.index(marked_id) + 1 :], True
    marked_at = from_iso(stamp)
    if marked_at is None:
        return [], True
    older = [m for m in candidates if m.sent_at is not None and m.sent_at < marked_at]
    return older, bool(older)


def _next_link(link: str, seen: set[str]) -> str:
    """The continuation to follow, refused when Teams repeats one: following it would never end."""
    if link in seen:
        raise Upstream("Teams repeated a history continuation; the read stopped there.")
    seen.add(link)
    return link
