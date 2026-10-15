"""The ports the service depends on (architecture, "Code structure"; requirements §4).

Adapters (wired in bootstrap.py):

- ChatCatalog: remote/csa.py (Teams Chat Service Aggregator; Graph cannot list chats for this client).
- ChannelDirectory: remote/graph_teams.py (Graph joined teams and channels).
- HistoryReader: remote/chat_service.py for chats (ChatHistoryReader), remote/graph_teams.py for channels
  (ChannelHistoryReader).
- MemberReader: remote/chat_service.py for chats, remote/graph_teams.py for channels.
- People, ChatWriter, FileReader: remote/graph_teams.py. ImageReader: remote/image_readers.py.
- PresenceReader: remote/presence.py. FavoriteFolders: remote/csa.py (the sidebar folders).

Swapping an adapter (another tenant, a policy change) must not change anything above this file.
"""

from __future__ import annotations

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
    """One remote history page. Pages go from newer to older; ``messages`` are oldest first."""

    messages: list[Message]  # only those inside the requested since/until
    next_link: str | None  # opaque continuation to older history; None when the selection is exhausted
    scanned: int  # raw records read
    missing_timestamps: int = 0  # excluded because a time filter could not be checked
    # Every message newer than this has been delivered by this page or an earlier one (or filtered out).
    floor: datetime | None = None


@dataclass(frozen=True)
class SendReceipt:
    message_id: str | None
    sent_at: datetime | None


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


class FavoriteFolders(Protocol):
    async def favorite_ids(self) -> set[str]:
        """The ids of the chats in the Favorites sidebar folder. A read only."""
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


class ChatMemberSearch(Protocol):
    async def named_chats(self, text: str) -> list[ChatMemberHit]:
        """Named group and meeting chats matched by a plain name or email, with their matching members.
        One-on-one chats are not included: they are found by name in the catalog."""
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
    async def send(self, chat: Chat, content: str, *, as_html: bool) -> SendReceipt:
        """Send once: ``content`` as plain text, or as HTML sent as written when ``as_html``. Never retried:
        an ambiguous outcome raises WriteOutcomeUnknown."""
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
