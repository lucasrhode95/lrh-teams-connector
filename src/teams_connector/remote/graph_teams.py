"""Microsoft Graph adapters: channels, channel history and members, people, photos, sending (with files),
shared files.

Live evidence (docs/api-research.md, "Microsoft Graph with the Teams client"): with the Teams
client, Graph serves channels (more completely than the private catalog), people, photos, shared
files and sending, but not chat reads (no Chat.Read for this client).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from teams_connector.domain.errors import (
    ConnectorError,
    InvalidRequest,
    NotFound,
    Upstream,
    WriteOutcomeUnknown,
)
from teams_connector.domain.models import Chat, Message, Participant
from teams_connector.remote import graph_mapping, loop_notes
from teams_connector.remote.graph import Graph, relative, segment, sub_failure
from teams_connector.remote.ports import (
    HistoryPage,
    HostedImage,
    MeetingNotes,
    RemoteFile,
    SendReceipt,
    SharedFile,
    UploadedFile,
)
from teams_connector.remote.transport import operation

CHANNEL_PAGE = 50  # threads per page (Graph maximum for channel messages)
PHOTO_SIZE = "64x64"
MAX_PHOTO_BYTES = 2_000_000
MAX_NOTES_BYTES = 5 * 1024 * 1024  # a converted notes page was about 12 KB (2026-10-15)
_GUID_CHARS = set("0123456789abcdef-")
SINGLE_UPLOAD_BYTES = 250 * 1024 * 1024  # this size and larger need an upload session (live, 2026-10-15)
UPLOAD_CHUNK_BYTES = 10 * 1024 * 1024  # Graph requires multiples of 320 KiB
UPLOAD_RESUMES = 3  # status reads and resends of missing ranges after failed chunks, per file


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
        """One page of threads (root posts with their replies). Graph orders threads by their latest
        activity (root or reply); the page keeps that order, oldest activity first like every page, and
        each thread in time order. Pages are therefore not in time order across threads: a thread with
        a recent reply comes with its old root.

        Graph cannot filter by date, so dates are checked here: replies inside the range are kept even
        when their root is older, and the traversal stops after a page whose every thread is older
        than ``since``.
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
            threads: list[list[Message]] = []  # latest activity first, as Graph returns them
            scanned = missing = 0
            every_thread_older = bool(roots)
            for root in roots:
                replies = [r for r in root.get("replies") or [] if isinstance(r, dict)]
                more = root.get("replies@odata.nextLink")
                if isinstance(more, str):
                    rest, _ = await self._graph.collect(more, max_items=100_000)
                    known = {r.get("id") for r in replies}
                    replies += [r for r in rest if r.get("id") not in known]
                thread = [root, *replies]
                stamps = [t for t in (graph_mapping.created(m) for m in thread) if t]
                if since is None or (stamps and max(stamps) >= since):
                    every_thread_older = False
                kept: list[Message] = []
                for raw in thread:
                    scanned += 1
                    message = graph_mapping.to_message(raw, chat.id)
                    if message.sent_at is None:
                        if since or until:
                            missing += 1
                            continue
                    elif (since and message.sent_at < since) or (until and message.sent_at > until):
                        continue
                    kept.append(message)
                kept.sort(key=lambda m: (m.sent_at is None, m.sent_at or datetime.min, m.id))
                threads.append(kept)
            next_link = data.get("@odata.nextLink")
            if since is not None and every_thread_older:
                next_link = None  # every later thread is older still
        return HistoryPage(
            messages=[message for thread in reversed(threads) for message in thread],
            next_link=next_link if isinstance(next_link, str) else None,
            scanned=scanned,
            missing_timestamps=missing,
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


class GraphWriter:
    """ChatWriter over Graph: chats via /chats/{id}/messages, channel posts via the team channel. Files go
    as ``reference`` attachments, each matched by an ``<attachment>`` tag in the body; inline images as
    ``hostedContents``, numbered from 1 in order (live, 2026-10-15)."""

    def __init__(self, graph: Graph) -> None:
        self._graph = graph

    async def send(
        self,
        chat: Chat,
        body: str,
        *,
        attachments: Sequence[UploadedFile] = (),
        images: Sequence[HostedImage] = (),
    ) -> SendReceipt:
        body += "".join(f'<attachment id="{file.attachment_id}"></attachment>' for file in attachments)
        if chat.kind == "channel":
            if not chat.team_id:
                raise InvalidRequest(f"Channel {chat.id} has no team; refresh the chat list.")
            path = f"/teams/{segment(chat.team_id)}/channels/{segment(chat.id)}/messages"
        else:
            path = f"/chats/{segment(chat.id)}/messages"
        payload: dict[str, Any] = {"body": {"contentType": "html", "content": body}}
        if attachments:
            payload["attachments"] = [
                {"id": f.attachment_id, "contentType": "reference", "contentUrl": f.web_url, "name": f.name}
                for f in attachments
            ]
        if images:
            payload["hostedContents"] = [
                {
                    "@microsoft.graph.temporaryId": str(number),
                    "contentBytes": base64.b64encode(image.data).decode("ascii"),
                    "contentType": image.content_type,
                }
                for number, image in enumerate(images, start=1)
            ]
        with operation("sending the message"):
            data, _ = await self._graph.post(path, payload, idempotent=False)
        message_id = data.get("id")
        return SendReceipt(
            message_id=str(message_id) if message_id else None,
            sent_at=graph_mapping.created(data),
        )


class GraphFileSharer:
    """FileSharer over Graph, as Teams Web sends a file (docs/api-research.md, "Sending files"): upload to
    the sender's OneDrive chat-files folder, then ``driveItem/invite`` without an invitation email.

    ``access`` is config.SHARED_FILE_ACCESS: "chat_default" asks for the tenant's default link
    (``Prefer: getDefaultLink``), "direct" grants each person access without a link.
    """

    def __init__(self, graph: Graph, *, folder: str, access: Literal["chat_default", "direct"]) -> None:
        self._graph = graph
        self._folder = folder
        self._access = access

    async def upload(self, path: Path) -> UploadedFile:
        """One file into the chat-files folder: a single PUT below 250 MiB (Graph's limit for one request),
        an upload session from there on. A name clash is renamed."""
        target = f"/me/drive/root:/{segment(self._folder)}/{segment(path.name)}:"
        with operation("uploading a file"):
            if path.stat().st_size < SINGLE_UPLOAD_BYTES:
                item = await self._graph.put_content(
                    f"{target}/content?@microsoft.graph.conflictBehavior=rename", path.read_bytes()
                )
            else:
                item = await self._upload_in_chunks(path, target)
        return _uploaded(item)

    async def share(self, file: UploadedFile, recipients: Sequence[str]) -> None:
        """One invite for every recipient. A partial success (HTTP 207, with an ``error`` entry for some
        recipients) is a failure: the message must not be posted when anyone cannot open the file."""
        body = {
            "recipients": [{"objectId": oid} for oid in recipients],
            "roles": ["write"],
            "requireSignIn": True,
            "sendInvitation": False,
        }
        prefer = "getDefaultLink" if self._access == "chat_default" else None
        with operation("sharing a file"):
            data, _ = await self._graph.post(
                f"/drives/{segment(file.drive_id)}/items/{segment(file.item_id)}/invite",
                body,
                idempotent=False,
                prefer=prefer,
            )
        entries = data.get("value")
        entries = entries if isinstance(entries, list) else []
        failed = [e for e in entries if isinstance(e, dict) and e.get("error")]
        if failed:
            error = failed[0]["error"]
            code = error.get("code") if isinstance(error, dict) else None
            raise Upstream(
                f"Graph could not share the file with {len(failed)} of {len(recipients)} chat members"
                + (f" ({code})." if isinstance(code, str) and code else ".")
            )

    async def _upload_in_chunks(self, path: Path, target: str) -> dict[str, Any]:
        """An upload session: chunks read from disk one at a time and PUT, without the Graph token, to the
        session's pre-authenticated ``uploadUrl``. The last chunk's answer is the drive item.

        Each chunk is sent once. After a failed chunk the session's status names the ranges still missing
        and only those are sent again, at most ``UPLOAD_RESUMES`` times per file (an exception to sending
        each write once, approved by the user on 2026-10-15). Then the session is cancelled and the upload
        fails.
        """
        session, _ = await self._graph.post(
            f"{target}/createUploadSession",
            {"item": {"@microsoft.graph.conflictBehavior": "rename"}},
            idempotent=False,
        )
        upload_url = session.get("uploadUrl")
        if not isinstance(upload_url, str) or not upload_url:
            raise Upstream("Graph opened an upload session but returned no upload URL.")
        size = path.stat().st_size
        offset, limit, resumes = 0, size - 1, 0  # the next chunk starts at offset and ends by limit
        with path.open("rb") as handle:
            while True:
                end = min(offset + UPLOAD_CHUNK_BYTES - 1, limit)
                handle.seek(offset)
                chunk = handle.read(end - offset + 1)
                try:
                    status, body = await self._graph.upload_session(
                        "PUT",
                        upload_url,
                        headers={"Content-Range": f"bytes {offset}-{end}/{size}"},
                        content=chunk,
                    )
                    if status in (200, 201):
                        return body
                    lowest = offset + 1  # an accepted chunk moves the upload forward
                except ConnectorError as exc:
                    if resumes == UPLOAD_RESUMES:
                        await self._cancel(upload_url)
                        raise
                    resumes += 1
                    body = await self._missing_ranges(upload_url, exc, last_chunk=end == size - 1)
                    lowest = 0
                missing = _next_expected_range(body, size)
                if missing is None or not lowest <= missing[0] < size:
                    await self._cancel(upload_url)
                    raise Upstream("Graph's upload session did not name the next range to send.")
                offset, limit = missing

    async def _missing_ranges(
        self, upload_url: str, failure: ConnectorError, *, last_chunk: bool
    ) -> dict[str, Any]:
        """The session's status after a failed chunk. When it cannot be read the session is cancelled; if
        the failed chunk was the last one, the file may have been completed, so the outcome is unknown."""
        try:
            _, body = await self._graph.upload_session("GET", upload_url)
            return body
        except ConnectorError:
            await self._cancel(upload_url)
            if last_chunk:
                raise WriteOutcomeUnknown(
                    f"The last part of the upload failed ({failure}) and the upload's state could not be "
                    "read; the file may or may not have been uploaded."
                ) from failure
            raise failure from None

    async def _cancel(self, upload_url: str) -> None:
        """Cancel an upload session (``DELETE uploadUrl``), once; a failure leaves it to expire."""
        with contextlib.suppress(ConnectorError):  # an unused session expires on its own
            await self._graph.upload_session("DELETE", upload_url)


def _next_expected_range(body: dict[str, Any], size: int) -> tuple[int, int] | None:
    """The first range an upload session still misses, as (first byte, last byte): ``nextExpectedRanges[0]``,
    "start-end" or "start-" (to the end of the file). Graph may list several ranges; each is sent in turn."""
    ranges = body.get("nextExpectedRanges")
    first = ranges[0] if isinstance(ranges, list) and ranges else None
    start, _, end = first.partition("-") if isinstance(first, str) else ("", "", "")
    if not start.isdigit() or not (end.isdigit() or end == ""):
        return None
    last = min(int(end), size - 1) if end else size - 1
    return (int(start), last) if int(start) <= last else None


def _uploaded(item: dict[str, Any]) -> UploadedFile:
    """The uploaded drive item. Its eTag (``"{GUID},n"``) names the attachment."""
    parent = item.get("parentReference")
    drive_id = parent.get("driveId") if isinstance(parent, dict) else None
    fields = (item.get("id"), drive_id, item.get("name"), item.get("webUrl"), item.get("eTag"))
    if not all(isinstance(value, str) and value for value in fields):
        raise Upstream("Graph accepted the upload but did not describe the uploaded file.")
    item_id, drive, name, web_url, etag = (str(value) for value in fields)
    guid = etag.strip('"').split(",")[0].strip("{}").lower()
    if not _is_object_id(guid):
        raise Upstream("Graph accepted the upload but its version tag has no file id.")
    return UploadedFile(item_id=item_id, drive_id=drive, name=name, web_url=web_url, attachment_id=guid)


class GraphFiles:
    """FileReader and MeetingNotesReader over Graph shares: any SharePoint/OneDrive sharing or web URL the
    user can open."""

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

    async def meeting_notes(self, url: str) -> MeetingNotes:
        """A Loop page through Graph's HTML conversion (``content?format=html``, one request, live
        2026-10-15), cleaned to its content. A page Graph will not convert (no access, deleted, a conversion
        error, or an export restriction Graph enforces) raises the service's error."""
        share = _share_id(url)
        with operation("reading meeting notes"):
            page = await self._graph.download_bytes(
                f"/shares/{share}/driveItem/content?format=html", max_bytes=MAX_NOTES_BYTES
            )
        return loop_notes.meeting_notes(page.decode("utf-8", errors="replace"))


def _share_id(url: str) -> str:
    """Graph's share identifier for a sharing URL: ``u!`` and the URL-safe base64 of the URL, unpadded."""
    if not url.lower().startswith("https://"):
        raise InvalidRequest("Only https sharing URLs can be downloaded.")
    return "u!" + base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
