"""Teams Chat Service (private): chat history, chat rosters, and the paginated conversation listing.

Graph cannot read chats for the Teams client (no Chat.Read), so one-on-one, group, meeting and
self-chat history and rosters come from here. Continuation links are opaque and must stay on the
account's regional Chat Service host and the same conversation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

from teams_connector.domain.errors import InvalidRequest, Upstream
from teams_connector.domain.models import Chat, Participant
from teams_connector.remote import chat_service_mapping as mapping
from teams_connector.remote.ports import HistoryPage, People
from teams_connector.remote.teams_session import SkypeCredential, TeamsSession
from teams_connector.remote.transport import Transport, operation

MAX_PAGE_SIZE = 200
LISTING_PAGE_SIZE = 500


class ChatService:
    """HistoryReader + MemberReader for chats, plus the conversation listing CSA falls back to."""

    def __init__(self, transport: Transport, session: TeamsSession, people: People) -> None:
        self._transport = transport
        self._session = session
        self._credential = SkypeCredential(session)
        self._people = people

    async def _get(self, url: str) -> dict[str, Any]:
        data = await self._transport.json(
            "GET", url, credential=self._credential, headers={"Accept": "application/json"}
        )
        return data if isinstance(data, dict) else {}

    async def _base(self) -> str:
        return await self._session.chat_service_url()

    async def _messages_url(self, conversation_id: str) -> str:
        return f"{await self._base()}/v1/users/ME/conversations/{quote(conversation_id, safe='')}/messages"

    async def _check_link(self, link: str, conversation_id: str) -> None:
        expected = urlsplit(await self._messages_url(conversation_id))
        actual = urlsplit(link)
        if (
            actual.scheme != "https"
            or (actual.hostname or "").lower() != (expected.hostname or "").lower()
            or unquote(actual.path).rstrip("/") != unquote(expected.path).rstrip("/")
        ):
            raise InvalidRequest(
                "The continuation does not belong to this conversation; restart the request."
            )

    async def page(
        self,
        chat: Chat,
        *,
        since: datetime | None,
        until: datetime | None,
        page_size: int,
        link: str | None,
    ) -> HistoryPage:
        """One page of history, newest first across pages. ``since`` is also sent to the service as its
        observed ``startTime`` lower bound; local date checks stay authoritative (``endTime`` is not
        reliable, so ``until`` is only checked locally)."""
        if link is None:
            start = str(_epoch_ms(since)) if since else "0"
            url = _with_query(
                await self._messages_url(chat.id),
                startTime=start,
                pageSize=str(min(max(page_size, 1), MAX_PAGE_SIZE)),
                view="msnp24Equivalent",
            )
        else:
            await self._check_link(link, chat.id)
            url = link
        with operation("reading chat history"):
            data = await self._get(url)
        rows = data.get("messages")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise Upstream("Unexpected Teams messages response.")
        selected = []
        missing = 0
        for row in sorted(rows, key=mapping.order_key):
            message = mapping.to_message(row, chat.id)
            if message.sent_at is None:
                if since or until:
                    missing += 1
                    continue
            elif (since and message.sent_at < since) or (until and message.sent_at > until):
                continue
            selected.append(message)
        next_link = mapping.mapping(data.get("_metadata")).get("backwardLink")
        if next_link is not None and not isinstance(next_link, str):
            raise Upstream("Unexpected Teams message continuation link.")
        if next_link:
            await self._check_link(next_link, chat.id)
        stamps = [t for t in (mapping.message_datetime(row) for row in rows) if t]
        return HistoryPage(
            selected,
            next_link or None,
            scanned=len(rows),
            missing_timestamps=missing,
            floor=min(stamps) if stamps else None,
        )

    async def members(self, chat: Chat) -> list[Participant]:
        """The roster of a chat, with names from the roster or the directory."""
        url = f"{await self._base()}/v1/threads/{quote(chat.id, safe='')}/members"
        with operation("listing chat members"):
            data = await self._get(url)
        rows = data.get("members")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise Upstream("Unexpected Teams conversation members response.")
        people: list[Participant] = []
        for row in rows:
            name = next(
                (
                    value.strip()
                    for key in ("userDisplayName", "displayName", "name", "friendlyName")
                    if isinstance((value := row.get(key)), str) and value.strip()
                ),
                None,
            )
            people.append(Participant(id=mapping.member_id(row), name=name))
        unnamed = [p.id for p in people if p.id and not p.name and not p.id.startswith("28:")]
        if unnamed:
            try:
                names = await self._people.names(unnamed)
            except Upstream:
                names = {}  # a name lookup must not make the roster unavailable
            people = [
                p if p.name or not p.id else p.model_copy(update={"name": names.get(p.id)}) for p in people
            ]
        return people

    async def conversations(self) -> list[dict[str, Any]]:
        """Every conversation row the service lists for the user (``view=mychats``), deduplicated."""
        url: str | None = _with_query(
            f"{await self._base()}/v1/users/ME/conversations", view="mychats", pageSize=str(LISTING_PAGE_SIZE)
        )
        base = urlsplit(await self._base())
        rows: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        seen_links: set[str] = set()
        with operation("listing conversations"):
            while url:
                if url in seen_links:
                    raise Upstream("Teams repeated a conversation catalog continuation link.")
                seen_links.add(url)
                if (urlsplit(url).hostname or "").lower() != (base.hostname or "").lower():
                    raise Upstream("Teams returned a continuation link outside the Chat Service.")
                data = await self._get(url)
                page = data.get("conversations")
                if not isinstance(page, list) or any(not isinstance(row, dict) for row in page):
                    raise Upstream("Unexpected Teams conversation catalog response.")
                for row in page:
                    conversation_id = row.get("id")
                    if not isinstance(conversation_id, str) or not conversation_id:
                        raise Upstream("Unexpected Teams conversation catalog row without an id.")
                    if conversation_id not in seen_ids:
                        seen_ids.add(conversation_id)
                        rows.append(row)
                link = mapping.mapping(data.get("_metadata")).get("backwardLink")
                if link is not None and not isinstance(link, str):
                    raise Upstream("Unexpected Teams conversation catalog continuation link.")
                url = link or None
        return rows


def _epoch_ms(value: datetime) -> int:
    return int(value.astimezone(UTC).timestamp() * 1000)


def _with_query(url: str, **updates: str) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update(updates)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
