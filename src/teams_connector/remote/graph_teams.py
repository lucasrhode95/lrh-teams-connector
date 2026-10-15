"""Microsoft Graph adapters: channels, channel history and members, people, photos, sending, files.

Live evidence (docs/api-research.md, "Microsoft Graph with the Teams client"): with the Teams
client, Graph serves channels (more completely than the private catalog), people, photos, shared
files and sending, but not chat reads (no Chat.Read for this client).
"""

from __future__ import annotations

import asyncio
import base64
import html
from datetime import datetime
from pathlib import Path
from typing import Any

from teams_connector.domain.errors import InvalidRequest, NotFound
from teams_connector.domain.models import Chat, Message, Participant
from teams_connector.remote import graph_mapping
from teams_connector.remote.graph import Graph, relative, segment, sub_failure
from teams_connector.remote.ports import HistoryPage, RemoteFile, SendReceipt, SharedFile
from teams_connector.remote.transport import operation

CHANNEL_PAGE = 50  # threads per page (Graph maximum for channel messages)
PHOTO_SIZE = "64x64"
MAX_PHOTO_BYTES = 2_000_000
_GUID_CHARS = set("0123456789abcdef-")


def _is_object_id(value: str) -> bool:
    return len(value) == 36 and set(value.lower()) <= _GUID_CHARS


class GraphChannels:
    """ChannelDirectory + HistoryReader + MemberReader for team channels."""

    def __init__(self, graph: Graph) -> None:
        self._graph = graph

    async def _teams(self) -> list[dict[str, Any]]:
        teams, _ = await self._graph.collect("/me/joinedTeams", {"$select": "id,displayName"}, max_items=1000)
        return teams

    async def channels(self) -> list[Chat]:
        with operation("listing team channels"):
            teams = await self._teams()

            async def team_channels(team: dict[str, Any]) -> list[Chat]:
                rows, _ = await self._graph.collect(
                    f"/teams/{segment(team['id'])}/allChannels",
                    {"$select": "id,displayName,membershipType"},
                    max_items=2000,
                )
                return [graph_mapping.channel(team, row) for row in rows]

            groups = await asyncio.gather(*(team_channels(team) for team in teams))
        return [chat for group in groups for chat in group]

    async def locate(self, channel_ids: list[str]) -> list[Chat]:
        """Omitted channels by id: every joined team is asked about every channel, in Graph batches. A
        channel is taken from the first team, in listing order, that answers for it."""
        if not channel_ids:
            return []
        with operation("finding channels by id"):
            teams = await self._teams()
            pairs = [(team, channel_id) for channel_id in channel_ids for team in teams]
            responses = await self._graph.batch(
                {
                    str(index): relative(
                        f"/teams/{segment(team['id'])}/channels/{segment(channel_id)}",
                        {"$select": "id,displayName,membershipType"},
                    )
                    for index, (team, channel_id) in enumerate(pairs)
                }
            )
            found: dict[str, Chat] = {}
            for index, (team, channel_id) in enumerate(pairs):
                if channel_id in found:
                    continue
                response = responses[str(index)]
                if response.ok and response.body.get("id"):
                    found[channel_id] = graph_mapping.channel(team, response.body)
                elif response.status not in (400, 404):  # not in this team: keep looking
                    raise sub_failure(response, detail="while finding channels by id")
        return list(found.values())

    async def channel(self, team_id: str, channel_id: str) -> Chat | None:
        """One channel read in its team. The team's name comes from the joined-teams listing, because a
        channel's listed name includes it. None when the team or the channel is no longer reachable."""
        team = next((t for t in await self._teams() if t.get("id") == team_id), None)
        if team is None:
            return None
        with operation("reading one channel"):
            try:
                row = await self._graph.get(
                    f"/teams/{segment(team_id)}/channels/{segment(channel_id)}",
                    {"$select": "id,displayName,membershipType"},
                )
            except (NotFound, InvalidRequest):
                return None
        return graph_mapping.channel(team, row) if row.get("id") else None

    async def message(self, chat: Chat, message_id: str) -> Message:
        """One channel post (a thread's root) by id, read in its team. Replies have their own route."""
        if not chat.team_id:
            raise InvalidRequest(f"Channel {chat.id} has no team; refresh the chat list.")
        with operation("reading one channel message"):
            data = await self._graph.get(
                f"/teams/{segment(chat.team_id)}/channels/{segment(chat.id)}/messages/{segment(message_id)}"
            )
        return graph_mapping.to_message(data, chat.id)

    async def reply(self, chat: Chat, root_id: str, message_id: str) -> Message:
        """One reply by id, read through its thread's root post."""
        if not chat.team_id:
            raise InvalidRequest(f"Channel {chat.id} has no team; refresh the chat list.")
        with operation("reading one channel reply"):
            data = await self._graph.get(
                f"/teams/{segment(chat.team_id)}/channels/{segment(chat.id)}/messages/{segment(root_id)}"
                f"/replies/{segment(message_id)}"
            )
        return graph_mapping.to_message(data, chat.id)

    async def page(
        self,
        chat: Chat,
        *,
        since: datetime | None,
        until: datetime | None,
        page_size: int,
        link: str | None,
    ) -> HistoryPage:
        """One page of threads (root posts with their replies), newest activity first.

        Graph orders threads by their latest activity (root or reply) and cannot filter by date, so
        dates are checked here: replies inside the range are kept even when their root is older, and
        the traversal stops after a page whose every thread is older than ``since``.
        """
        if not chat.team_id:
            raise InvalidRequest(f"Channel {chat.id} has no team; refresh the chat list.")
        with operation("reading channel history"):
            if link:
                data = await self._graph.get(link)
            else:
                data = await self._graph.get(
                    f"/teams/{segment(chat.team_id)}/channels/{segment(chat.id)}/messages",
                    {"$top": min(max(page_size, 1), CHANNEL_PAGE), "$expand": "replies"},
                )
            roots = [r for r in data.get("value", []) if isinstance(r, dict)]
            messages: list[Message] = []
            scanned = missing = 0
            every_thread_older = bool(roots)
            activities: list[datetime] = []
            for root in roots:
                replies = [r for r in root.get("replies") or [] if isinstance(r, dict)]
                more = root.get("replies@odata.nextLink")
                if isinstance(more, str):
                    rest, _ = await self._graph.collect(more, max_items=100_000)
                    known = {r.get("id") for r in replies}
                    replies += [r for r in rest if r.get("id") not in known]
                thread = [root, *replies]
                stamps = [t for t in (graph_mapping.created(m) for m in thread) if t]
                if stamps:
                    activities.append(max(stamps))
                if since is None or (stamps and max(stamps) >= since):
                    every_thread_older = False
                for raw in thread:
                    scanned += 1
                    message = graph_mapping.to_message(raw, chat.id)
                    if message.sent_at is None:
                        if since or until:
                            missing += 1
                            continue
                    elif (since and message.sent_at < since) or (until and message.sent_at > until):
                        continue
                    messages.append(message)
            next_link = data.get("@odata.nextLink")
            if since is not None and every_thread_older:
                next_link = None  # every later thread is older still
        messages.sort(key=lambda m: (m.sent_at is None, m.sent_at or datetime.min, m.id))
        return HistoryPage(
            messages=messages,
            next_link=next_link if isinstance(next_link, str) else None,
            scanned=scanned,
            missing_timestamps=missing,
            floor=min(activities) if activities else None,  # later threads have older activity
        )

    async def members(self, chat: Chat) -> list[Participant]:
        if not chat.team_id:
            raise InvalidRequest(f"Channel {chat.id} has no team; refresh the chat list.")
        with operation("listing channel members"):
            rows, _ = await self._graph.collect(
                f"/teams/{segment(chat.team_id)}/channels/{segment(chat.id)}/members", max_items=10_000
            )
        return [graph_mapping.participant(row) for row in rows]


class GraphPeople:
    """People: display names and photos from the directory."""

    def __init__(self, graph: Graph) -> None:
        self._graph = graph

    async def names(self, user_ids: list[str]) -> dict[str, str]:
        ids = list(dict.fromkeys(i.lower() for i in user_ids if _is_object_id(i)))
        if not ids:
            return {}
        with operation("looking up people's names"):
            responses = await self._graph.batch(
                {i: relative(f"/users/{i}", {"$select": "id,displayName"}) for i in ids}
            )
        names: dict[str, str] = {}
        for person, response in responses.items():
            name = response.body.get("displayName") if response.ok else None
            if isinstance(name, str) and name.strip():
                names[person] = name.strip()
        return names

    async def photo(self, user_id: str) -> tuple[bytes, str] | None:
        if not _is_object_id(user_id):
            return None
        with operation("fetching a profile photo"):
            try:
                content, content_type = await self._graph.get_bytes(
                    f"/users/{user_id.lower()}/photos/{PHOTO_SIZE}/$value", max_bytes=MAX_PHOTO_BYTES
                )
            except (NotFound, InvalidRequest):  # no photo (404) or not a directory user (400)
                return None
        kind = (content_type or "").split(";", 1)[0].strip().lower()
        if not kind.startswith("image/") or not content:
            return None
        return content, kind


def _paragraph(text: str) -> str:
    """Plain text as one paragraph: escaped, with each line break kept as ``<br>``."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "<p>" + "<br>".join(html.escape(line, quote=False) for line in lines) + "</p>"


class GraphWriter:
    """ChatWriter over Graph: chats via /chats/{id}/messages, channel posts via the team channel."""

    def __init__(self, graph: Graph) -> None:
        self._graph = graph

    async def send(self, chat: Chat, content: str, *, as_html: bool) -> SendReceipt:
        body = content if as_html else _paragraph(content)
        if chat.kind == "channel":
            if not chat.team_id:
                raise InvalidRequest(f"Channel {chat.id} has no team; refresh the chat list.")
            path = f"/teams/{segment(chat.team_id)}/channels/{segment(chat.id)}/messages"
        else:
            path = f"/chats/{segment(chat.id)}/messages"
        with operation("sending the message"):
            data, _ = await self._graph.post(
                path, {"body": {"contentType": "html", "content": body}}, idempotent=False
            )
        message_id = data.get("id")
        return SendReceipt(
            message_id=str(message_id) if message_id else None,
            sent_at=graph_mapping.created(data),
        )


class GraphFiles:
    """FileReader over Graph shares: any SharePoint/OneDrive sharing or web URL the user can open."""

    def __init__(self, graph: Graph) -> None:
        self._graph = graph

    async def describe(self, url: str) -> SharedFile:
        share = _share_id(url)
        with operation("describing a shared file"):
            item = await self._graph.get(
                f"/shares/{share}/driveItem", {"$select": "id,name,size,folder,eTag"}
            )
        if item.get("folder") is not None:
            raise InvalidRequest("The link points to a folder, not a file.")
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id:
            raise NotFound("Graph returned no file for the link.")
        name, etag, size = item.get("name"), item.get("eTag"), item.get("size")
        return SharedFile(
            item_id=item_id,
            name=name if isinstance(name, str) else None,
            version=etag if isinstance(etag, str) else "",
            size=size if isinstance(size, int) else None,
        )

    async def download(self, url: str, dest: Path, *, max_bytes: int) -> RemoteFile:
        share = _share_id(url)
        with operation("downloading a shared file"):
            content_type, size = await self._graph.download(
                f"/shares/{share}/driveItem/content", dest, max_bytes=max_bytes
            )
        return RemoteFile(name=None, content_type=content_type, size=size)


def _share_id(url: str) -> str:
    """Graph's share identifier for a sharing URL: ``u!`` and the URL-safe base64 of the URL, unpadded."""
    if not url.lower().startswith("https://"):
        raise InvalidRequest("Only https sharing URLs can be downloaded.")
    return "u!" + base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
