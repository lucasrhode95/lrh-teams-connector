"""The ports the service depends on (architecture, "Code structure"; requirements §4).

Adapters (wired in bootstrap.py):

- ChatCatalog: remote/csa.py (Teams Chat Service Aggregator; Graph cannot list chats for this client).
- ChannelDirectory: remote/graph_teams.py (Graph joined teams and channels).
- HistoryReader: remote/chat_service.py for chats (ChatHistoryReader), remote/graph_teams.py for channels
  (ChannelHistoryReader).
- MemberReader: remote/chat_service.py for chats, remote/graph_teams.py for channels.
- People, ChatWriter, FileSharer, FileReader, MeetingNotesReader: remote/graph_teams.py. ImageReader:
  remote/image_readers.py.
- MessageDeleter: remote/chat_service.py.
- PresenceReader: remote/presence.py.

Swapping an adapter (another tenant, a policy change) must not change anything above this file.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol

from teams_connector.domain.models import Chat, Message, MessageHit, Participant, Presence


@dataclass
class CatalogUpdate:
    """One catalog read: a full snapshot (``full``) or the changes since ``sync_token``."""

    chats: list[Chat]  # new or changed chats, normalized (channels excluded)
    deleted_ids: list[str]
    sync_token: str  # continuation for the next delta; persisted with the rows
    full: bool
    complete: bool | None  # the service reported the snapshot complete (None: it did not say)
    channel_activity: list[Chat] = field(default_factory=list)  # the private catalog's view of channels
    notes: list[str] = field(default_factory=list)


@dataclass
class HistoryPage:
    """One remote history page. Pages go from newer to older; ``messages`` are oldest first (a channel
    page: its threads by latest activity, each in time order)."""

    messages: list[Message]  # only those inside the requested since/until
    next_link: str | None  # opaque continuation to older history; None when the selection is exhausted
    scanned: int  # raw records read
    missing_timestamps: int = 0  # excluded because a time filter could not be checked


@dataclass(frozen=True)
class SendReceipt:
    message_id: str | None
    sent_at: datetime | None


@dataclass(frozen=True)
class UploadedFile:
    """A file uploaded to the sender's OneDrive for a message. ``attachment_id`` is the GUID of its eTag,
    which Graph's ``reference`` attachment and the body's ``<attachment>`` tag must share."""

    item_id: str
    drive_id: str
    name: str
    web_url: str
    attachment_id: str


@dataclass(frozen=True)
class HostedImage:
    """An image sent inline with a message (PNG or JPEG). The body names the n-th image of the message
    (1-based) as ``<img src="../hostedContents/n/$value">``."""

    data: bytes
    content_type: str


def hosted_image_src(number: int) -> str:
    """How a message body names its ``number``-th inline image (1-based)."""
    return f"../hostedContents/{number}/$value"


@dataclass(frozen=True)
class MeetingNotes:
    """A Loop meeting-notes page as plain text, read at one moment (the page is live)."""

    title: str | None
    text: str


@dataclass(frozen=True)
class RemoteFile:
    name: str | None
    content_type: str | None
    size: int


@dataclass
class MessageSearchPage:
    """Normalized result of one Search v2 page."""

    items: list[MessageHit]
    more_results_available: bool | None
    result_set_found: bool
    total: int | None  # Search v2's count for the query, when it sent one


class ChatCatalog(Protocol):
    async def updates(self, sync_token: str | None) -> CatalogUpdate:
        """A delta since ``sync_token``, or a full snapshot when it is None or no longer accepted."""
        ...


class ChannelDirectory(Protocol):
    async def channels(self) -> list[Chat]:
        """Every channel of every joined team."""
        ...

    async def locate(self, channel_ids: list[str]) -> list[Chat]:
        """Channels the listing omits (e.g. some shared channels), found by id in the joined teams."""
        ...

    async def channel(self, team_id: str, channel_id: str) -> Chat | None:
        """One channel with its current name, read in its team; None when it is no longer reachable."""
        ...


@dataclass(frozen=True)
class ChatMemberHit:
    """A named chat Teams matched for a plain name or email, with the members that matched."""

    chat_id: str
    matching_members: list[Participant]


@dataclass(frozen=True)
class ChatMemberResult:
    """Teams' named-chat matches for one query. ``truncated``: Teams matched more than were read."""

    hits: list[ChatMemberHit]
    truncated: bool


class ChatMemberSearch(Protocol):
    async def named_chats(self, text: str) -> ChatMemberResult:
        """Named group and meeting chats matched by a plain name or email, with their matching members.
        One-on-one chats are not included: ``people`` finds them."""
        ...

    async def people(self, text: str) -> list[Participant]:
        """The people Teams suggests for a plain name or email, by Entra object id; never the signed-in
        user. A one-on-one chat's id names both of its people."""
        ...


class HistoryReader(Protocol):
    async def page(
        self,
        chat: Chat,
        *,
        since: datetime | None,
        until: datetime | None,
        page_size: int,
        link: str | None,
    ) -> HistoryPage:
        """One page of history, newest first across pages. ``link`` continues a previous page."""
        ...

    async def message(self, chat: Chat, message_id: str) -> Message:
        """One message of the chat, read from Teams by its id."""
        ...


class ChatHistoryReader(HistoryReader, Protocol):
    async def messages_at(self, chat: Chat, sent_at: datetime) -> list[Message]:
        """The messages that arrived at exactly ``sent_at``: one lookup by time, in chats and channels."""
        ...


class ChannelHistoryReader(HistoryReader, Protocol):
    async def reply(self, chat: Chat, root_id: str, message_id: str) -> Message:
        """One reply in a channel thread, read through its root post: the root-message route does not
        answer for replies."""
        ...


class MemberReader(Protocol):
    async def members(self, chat: Chat) -> list[Participant]: ...


class People(Protocol):
    async def names(self, user_ids: list[str]) -> dict[str, str]:
        """Display names by Entra object id; unknown ids are left out."""
        ...

    async def photo(self, user_id: str) -> tuple[bytes, str] | None:
        """A small profile photo and its content type; None when the person has none."""
        ...


class PresenceReader(Protocol):
    async def presence(self, user_id: str) -> Presence | None:
        """One person's presence by Entra object id, or None when Teams reports none. A read only."""
        ...


class ChatWriter(Protocol):
    async def send(
        self,
        chat: Chat,
        body: str,
        *,
        attachments: Sequence[UploadedFile] = (),
        images: Sequence[HostedImage] = (),
    ) -> SendReceipt:
        """Send once: the HTML ``body``, which names every one of ``images`` (``hosted_image_src``), with
        ``attachments`` after it (``body`` may then be empty). Never retried: an ambiguous outcome raises
        WriteOutcomeUnknown."""
        ...


class MeetingNotesReader(Protocol):
    async def meeting_notes(self, url: str) -> MeetingNotes:
        """The content of the Loop page a meeting-notes link names, read now, in one request."""
        ...


class MessageDeleter(Protocol):
    async def delete(self, chat: Chat, message_id: str) -> None:
        """Soft-delete one of the user's own messages in a chat, once. Never retried: an ambiguous outcome
        raises WriteOutcomeUnknown."""
        ...


class FileSharer(Protocol):
    async def upload(self, path: Path) -> UploadedFile:
        """Upload one local file to the sender's chat-files folder, renaming on a name clash. Sent once."""
        ...

    async def share(self, file: UploadedFile, recipients: Sequence[str]) -> None:
        """Grant people (Entra object ids) access to an uploaded file without notifying them. Sent once."""
        ...


@dataclass(frozen=True)
class SharedFile:
    """A SharePoint or OneDrive file as Graph describes it. ``item_id`` and ``version`` key its cache."""

    item_id: str
    name: str | None
    version: str
    size: int | None


class FileReader(Protocol):
    async def describe(self, url: str) -> SharedFile:
        """The drive item behind a sharing URL, without its content."""
        ...

    async def download(self, url: str, dest: Path, *, max_bytes: int) -> RemoteFile:
        """Download a shared file's content (a SharePoint/OneDrive sharing URL) into ``dest``."""
        ...


class ImageReader(Protocol):
    async def download(self, url: str, dest: Path, *, max_bytes: int) -> RemoteFile:
        """Download a pasted image's full-size content (an AMS view or a Graph hosted copy) into ``dest``."""
        ...


class MessageSearchReader(Protocol):
    async def search_messages(
        self,
        query: str,
        *,
        offset: int,
        page_size: int,
        cvid: str,
        logical_id: str,
    ) -> MessageSearchPage:
        """Search Teams message content via the standalone Microsoft Search v2 route."""
        ...
