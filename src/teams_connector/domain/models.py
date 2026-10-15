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


class ChatSearchCoverage(Compact):
    """Whether Teams' chat search answered for this query. Names are always searched from the catalog."""

    complete: bool  # Teams answered for a plain name or email; member matches are then as complete as Teams
    chats: int  # chats in the local catalog
    notes: list[str] = Field(default_factory=list)


class ChatSearchResult(Compact):
    query: str
    items: list[ChatMatch]
    cursor: str | None = None
    coverage: ChatSearchCoverage


# ---------------------------------------------------------------------------------------------- messages


class Attachment(Compact):
    """A file or image a message carries. ``name`` is the marker the text shows. ``source`` says how its bytes
    are fetched; ``None`` means the marker is all there is to show."""

    name: str
    kind: Literal["file", "image", "link"] = "file"
    source: Literal["sharepoint", "ams", "graph", "data"] | None = None
    url: str | None = None  # sharing URL (sharepoint), full-size AMS or Graph hosted-content URL (ams, graph)
    source_id: str | None = None  # AMS object id (ams, graph) or SHA-256 of the decoded bytes (data)
    content_type: str | None = None
    size: int | None = None
    payload: bytes | None = Field(default=None, exclude=True)  # decoded data: image bytes, never serialized


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
    message_limit_reached: bool = False
    notes: list[str] = Field(default_factory=list)


class MessagePage(Compact):
    chat: Chat
    items: list[Message]  # oldest first within the page; the cursor continues with older messages
    cursor: str | None = None
    coverage: HistoryCoverage


class MessageHit(Compact):
    """One Teams message found by Search v2. ``message_id`` is the id get_messages returns for it."""

    message_id: str | None = None  # the hit's InternetMessageId; None when Search v2 sent none
    conversation_id: str | None = None  # set only for an exact local catalog-id match
    conversation_name: str | None = None
    conversation_id_candidates: list[str] = Field(default_factory=list, exclude=True)
    sent_at: datetime | None = None
    sender: str | None = None
    subject: str | None = None
    preview: str | None = None


class MessageSearchCoverage(Compact):
    complete: bool
    offset: int
    page_size: int
    more_results_available: bool | None = None
    total: int | None = None  # Search v2's count for the query; approximate, it can move between pages
    notes: list[str] = Field(default_factory=list)


class MessageSearchResult(Compact):
    query: str  # the query sent to Search v2: quoted terms, with the scope and dates as clauses
    items: list[MessageHit]
    cursor: str | None = None
    coverage: MessageSearchCoverage


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


class DownloadedFiles(Compact):
    """The files one download call saved, in the order of the message's attachments."""

    files: list[SavedFile]


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
