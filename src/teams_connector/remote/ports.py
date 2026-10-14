"""The ports the service depends on (architecture, "Code structure"; requirements §4).

Adapters (wired in bootstrap.py):

- ChatCatalog: remote/csa.py (Teams Chat Service Aggregator; Graph cannot list chats for this client).
- ChannelDirectory: remote/graph_teams.py (Graph joined teams and channels).
- HistoryReader: remote/chat_service.py for chats, remote/graph_teams.py for channels.
- MemberReader: remote/chat_service.py for chats, remote/graph_teams.py for channels.
- People, ChatWriter, FileReader: remote/graph_teams.py.

Swapping an adapter (another tenant, a policy change) must not change anything above this file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol

from teams_connector.domain.models import Chat, Message, Participant, RemoteMessageHit


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
class RemoteMessageSearchPage:
    """Normalized result of one Search v2 page."""

    items: list[RemoteMessageHit]
    more_results_available: bool | None
    result_set_found: bool


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


class MemberReader(Protocol):
    async def members(self, chat: Chat) -> list[Participant]: ...


class People(Protocol):
    async def names(self, user_ids: list[str]) -> dict[str, str]:
        """Display names by Entra object id; unknown ids are left out."""
        ...

    async def photo(self, user_id: str) -> tuple[bytes, str] | None:
        """A small profile photo and its content type; None when the person has none."""
        ...


class ChatWriter(Protocol):
    async def send(self, chat: Chat, text: str) -> SendReceipt:
        """Send plain text once. Never retried: an ambiguous outcome raises WriteOutcomeUnknown."""
        ...


class FileReader(Protocol):
    async def download(self, url: str, dest: Path, *, max_bytes: int | None = None) -> RemoteFile:
        """Download a shared file (SharePoint/OneDrive sharing or web URL) into ``dest``."""
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
    ) -> RemoteMessageSearchPage:
        """Search Teams message content via the standalone Microsoft Search v2 route."""
        ...
