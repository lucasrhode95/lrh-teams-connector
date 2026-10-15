"""Domain models: the single schema for the service, the MCP tools and the web API.

These never mirror a Microsoft wire format. The remote/*_mapping.py modules convert Teams and
Graph JSON into them, and nothing else knows those field names.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, SerializerFunctionWrapHandler, model_serializer

# one_on_one: two people (or a person and an app); self: the user's own notes chat.
ChatKind = Literal["one_on_one", "group", "meeting", "channel", "self"]
MatchMode = Literal["exact", "substring", "all_terms"]
ExportFormat = Literal["txt", "jsonl"]


class Compact(BaseModel):
    """Serializes optional fields only when set (not null, not an empty list): MCP results stay small,
    and a missing field means its default. Required fields are always present."""

    @model_serializer(mode="wrap")
    def _drop_empty(self, handler: SerializerFunctionWrapHandler) -> Any:
        data = handler(self)
        if not isinstance(data, dict):
            return data
        fields = type(self).model_fields
        return {
            k: v
            for k, v in data.items()
            if (k in fields and fields[k].is_required()) or (v is not None and v != [] and v != {})
        }


# ---------------------------------------------------------------------------------------------- chats


class Chat(Compact):
    """One conversation: a chat (one-on-one, group, meeting, self) or a team channel."""

    id: str  # stable Teams conversation (thread) id, the same in every backend
    name: str
    kind: ChatKind | None = None
    team_id: str | None = None  # channels: the team that hosts the channel
    last_message_at: datetime | None = None
    last_message_preview: str | None = None
    unread: bool | None = None
    is_empty: bool | None = None  # the catalog reports no messages
    photo_user_id: str | None = None  # the person whose photo represents the chat (self, one-on-one peer)


class Participant(Compact):
    id: str | None = None  # Microsoft Entra object id for people, app id for bots
    name: str | None = None


class CatalogCoverage(Compact):
    """What a chat listing covers. ``complete`` is limited to ``scope``."""

    source: Literal["remote", "local"]
    complete: bool
    scope: str
    total: int  # chats matching the request, before paging
    empty_excluded: int = 0
    refreshed_at: datetime | None = None
    notes: list[str] = Field(default_factory=list)


class ChatPage(Compact):
    items: list[Chat]
    cursor: str | None = None
    coverage: CatalogCoverage


class ChatMatch(Compact):
    chat: Chat
    name_matched: bool = False
    matched_participants: list[Participant] = Field(default_factory=list)


class RosterCoverage(Compact):
    """How far participant search can be trusted: rosters are indexed by sync_archive."""

    complete: bool
    chats: int
    rosters_indexed: int
    rosters_missing: int
    unresolved_participants: int = 0
    notes: list[str] = Field(default_factory=list)


class ChatSearchResult(Compact):
    query: str
    items: list[ChatMatch]
    cursor: str | None = None
    coverage: RosterCoverage


# ---------------------------------------------------------------------------------------------- messages


class Attachment(Compact):
    name: str
    kind: Literal["file", "image", "link"] = "file"
    url: str | None = None  # sharing or web URL; files can be downloaded with download_attachment
    content_type: str | None = None
    size: int | None = None


class Message(Compact):
    id: str
    conversation_id: str
    author: str | None = None
    author_id: str | None = None  # Entra object id for people, "28:..." for apps
    is_self: bool | None = None  # derived per result: the signed-in user wrote it
    sent_at: datetime | None = None
    text: str = ""  # readable text: rich text, mentions, cards and attachment markers rendered
    system_event: str | None = None  # set for system/event records (member added, call, ...)
    subject: str | None = None  # channel posts
    reply_to_id: str | None = None  # channel replies: the thread's root message id
    edited_at: datetime | None = None
    is_deleted: bool = False
    deleted_at: datetime | None = None
    attachments: list[Attachment] = Field(default_factory=list)


class HistoryCoverage(Compact):
    """What a history page covers. Agents must read this before treating results as complete."""

    complete: bool  # nothing more matches the selection (otherwise follow the cursor)
    remote_complete: bool  # the remote traversal reached the end of the selection
    remote_pages: int  # remote pages fetched so far for this selection
    archive_only: int = 0  # messages in this page known only from local retention
    message_limit_reached: bool = False
    notes: list[str] = Field(default_factory=list)


class MessagePage(Compact):
    chat: Chat
    items: list[Message]  # oldest first within the page; the cursor continues with older messages
    cursor: str | None = None
    coverage: HistoryCoverage


class SearchHit(Compact):
    chat_name: str
    message: Message  # text is an excerpt around the match


class ChatPreparation(Compact):
    conversation_id: str
    pages_fetched: int
    complete: bool
    error: str | None = None


class SearchCoverage(Compact):
    complete: bool  # every chat of the scope was fetched for the range and all matches were returned
    preparation_complete: bool
    chats_in_scope: int
    chats_prepared: int
    pages_fetched_this_call: int
    since: datetime
    until: datetime
    prepared_this_call: list[ChatPreparation] = Field(default_factory=list)
    results_truncated: bool = False
    notes: list[str] = Field(default_factory=list)


class SearchResult(Compact):
    query: str
    items: list[SearchHit]
    cursor: str | None = None
    coverage: SearchCoverage


class RemoteMessageHit(Compact):
    """A Teams Search v2 pointer, with a local conversation only when ids match exactly."""

    search_message_id: str | None = None
    message_id_kind: str | None = None
    conversation_id: str | None = None  # set only for an exact local catalog-id match
    conversation_name: str | None = None
    conversation_id_candidates: list[str] = Field(default_factory=list)
    sent_at: datetime | None = None
    sender: str | None = None
    subject: str | None = None
    preview: str | None = None


class RemoteSearchCoverage(Compact):
    complete: bool
    offset: int
    page_size: int
    more_results_available: bool | None = None
    notes: list[str] = Field(default_factory=list)


class RemoteMessageSearchResult(Compact):
    query: str
    items: list[RemoteMessageHit]
    cursor: str | None = None
    coverage: RemoteSearchCoverage


# ---------------------------------------------------------------------------------------------- archive


class ChatArchiveState(Compact):
    conversation_id: str
    name: str
    history_complete: bool  # the archive holds the chat's whole history (as of synced_at)
    messages_stored: int
    oldest_message_at: datetime | None = None
    newest_message_at: datetime | None = None
    synced_at: datetime | None = None
    roster_complete: bool = False
    unresolved_participants: int = 0
    range_covered: bool | None = None  # status only: the requested range is fully in the archive
    messages_added: int | None = None  # sync only
    pages_fetched: int | None = None  # sync only
    error: str | None = None


class ArchiveTotals(Compact):
    chats: int
    messages: int
    participants: int
    complete_histories: int
    complete_rosters: int


class SyncResult(Compact):
    items: list[ChatArchiveState]
    pages_left: int
    totals: ArchiveTotals


class ArchiveStatus(Compact):
    items: list[ChatArchiveState]
    cursor: str | None = None
    covered: int | None = None  # chats whose requested range is fully archived
    totals: ArchiveTotals


# ---------------------------------------------------------------------------------------------- writes


class SendResult(Compact):
    conversation_id: str
    chat_name: str
    message_id: str | None = None
    sent_at: datetime | None = None
    characters: int
    status: Literal["accepted"] = "accepted"


# ---------------------------------------------------------------------------------------------- files


class SavedFile(Compact):
    path: str
    name: str
    content_type: str | None = None
    size: int


class ExportRequest(BaseModel):
    """Chats to export, with optional inclusive dates, latest-N limit, and selected attachments."""

    conversation_ids: list[str] = Field(min_length=1, max_length=500)
    since: datetime | None = None
    until: datetime | None = None
    message_limit: int | None = Field(default=None, ge=1, le=1_000_000)  # latest N per chat
    format: ExportFormat = "txt"
    include_attachments: bool = True


class ExportedChat(Compact):
    conversation_id: str
    name: str
    messages: int
    remote_complete: bool
    attachments_downloaded: int = 0
    attachments_failed: int = 0


class ExportArtifact(Compact):
    path: str
    filename: str
    content_type: str
    size: int
    message_count: int
    chats: list[ExportedChat]


class ExportProgress(Compact):
    phase: Literal["retrieving", "attachments", "writing", "done"]
    chat_index: int = 0
    chats_total: int = 0
    pages_fetched: int = 0
    messages_retrieved: int = 0
    messages_written: int = 0
    attachments_total: int = 0
    attachments_downloaded: int = 0
    attachments_failed: int = 0
