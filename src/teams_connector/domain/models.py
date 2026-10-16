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


class Presence(Compact):
    """One person's Teams presence: availability and activity, when they were last active, and their device.
    No status note and no calendar text."""

    availability: str | None = None
    activity: str | None = None
    last_active_at: datetime | None = None
    device_type: str | None = None


class PresenceResult(Compact):
    """The presence of the other person in a one-on-one chat. ``reason`` says why there is none."""

    presence: Presence | None = None
    reason: str | None = None


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


class RecordingRef(Compact):
    """A meeting recording a message card names: its OneDrive or SharePoint item, its title, and its
    transcript when the card has one. Internal: serializers leave it out; search and downloads read it."""

    drive_id: str
    item_id: str
    title: str | None = None
    transcript_id: str | None = None


class Attachment(Compact):
    """A file or image a message carries. ``name`` is the marker the text shows. ``source`` says how its bytes
    are fetched; ``None`` means the marker is all there is to show."""

    name: str
    kind: Literal["file", "image", "link"] = "file"
    source: Literal["sharepoint", "ams", "graph", "data", "loop", "transcript"] | None = None
    # sharing URL (sharepoint), full-size AMS or Graph hosted-content URL (ams, graph), Loop page link (loop)
    url: str | None = None
    source_id: str | None = None  # AMS object id (ams, graph) or SHA-256 of the decoded bytes (data)
    content_type: str | None = None
    size: int | None = None
    payload: bytes | None = Field(default=None, exclude=True)  # decoded data: image bytes, never serialized
    recording: RecordingRef | None = Field(
        default=None, exclude=True
    )  # transcript: the meeting it belongs to


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
    recording: RecordingRef | None = Field(default=None, exclude=True)  # a recording card: its meeting
    # the text with the meeting notes withheld: the message's own text and markers; set only for notes
    text_without_notes: str | None = Field(default=None, exclude=True)


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


class SentFile(Compact):
    """A file sent with a message: its name in the sender's OneDrive (renamed on a clash) and its link."""

    name: str
    url: str


class SendResult(Compact):
    conversation_id: str
    chat_name: str
    message_id: str | None = None
    sent_at: datetime | None = None
    characters: int
    files: list[SentFile] = Field(default_factory=list)
    status: Literal["accepted"] = "accepted"


class DeletedMessage(Compact):
    """A message deleted in Teams. ``files`` names the files it shared: they stay in the sender's OneDrive,
    still shared with the chat."""

    message_id: str
    sent_at: datetime | None = None
    files: list[str] = Field(default_factory=list)


class NotDeletedMessage(Compact):
    """A message left as it was (skipped) or whose delete has an unknown outcome (uncertain), and why."""

    message_id: str
    reason: str


class DeleteResult(Compact):
    conversation_id: str
    chat_name: str
    deleted: list[DeletedMessage] = Field(default_factory=list)
    skipped: list[NotDeletedMessage] = Field(default_factory=list)
    uncertain: list[NotDeletedMessage] = Field(default_factory=list)


# ---------------------------------------------------------------------------------------------- files


class SavedFile(Compact):
    path: str
    name: str
    content_type: str | None = None
    size: int


class UnavailableFile(Compact):
    name: str
    reason: str


class DownloadedFiles(Compact):
    """The files one download call saved, in the order of the message's attachments, and those it could
    not fetch. A call that saves none fails instead."""

    files: list[SavedFile]
    unavailable: list[UnavailableFile] = Field(default_factory=list)


class ExportRequest(BaseModel):
    """Chats to export, with optional inclusive dates, latest-N limit, and selected attachments."""

    conversation_ids: list[str] = Field(min_length=1, max_length=500)
    since: datetime | None = None
    until: datetime | None = None
    message_limit: int | None = Field(default=None, ge=1, le=1_000_000)  # latest N per chat
    format: ExportFormat = "txt"
    include_attachments: bool = True
    include_transcripts: bool = True
    include_summaries: bool = True  # Teams' AI summary of each transcribed meeting, in its transcript marker


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


class PreviewOutput(Compact):
    """One conversation's plain-text export preview and whether the initial excerpt is incomplete."""

    text: str
    messages: int
    capped: bool


# -------------------------------------------------------------------------------------------- transcripts


class TranscriptEntry(Compact):
    """One utterance of a meeting transcript: who said it, when (seconds into the recording), and what."""

    speaker: str | None = None
    start: float
    text: str


class TranscriptEvent(Compact):
    """A start or stop of the transcript itself, at a time in the recording."""

    kind: str
    start: float


class Transcript(Compact):
    language: str | None = None
    entries: list[TranscriptEntry] = Field(default_factory=list)
    events: list[TranscriptEvent] = Field(default_factory=list)


class TranscriptHit(Compact):
    """One utterance that matched a search. ``message_id`` is the recording's message, which get_messages
    reads."""

    meeting: str | None = None  # the recording's title
    conversation_id: str
    message_id: str
    speaker: str | None = None
    offset: str  # the time into the recording, as the Teams transcript shows it: 0:17 or 1:02:03
    text: str
    term: str  # the search term the utterance matched


class TranscriptSearchCoverage(Compact):
    """What a transcript search covered. ``complete`` says every recording in its scope was read."""

    mode: Literal["conversations", "meetings"]
    complete: bool
    recordings: int  # meeting recordings whose transcripts were read
    transcripts_unavailable: int = 0  # recordings whose transcript Teams did not provide
    candidates: int = 0  # meetings mode: video hits from the search
    not_meetings: int = 0  # meetings mode: every chat that could hold the card was read, and none did
    unverified: int = 0  # meetings mode: some chat that could hold the card could not be read
    truncated: bool = False  # more hits than ``limit``
    notes: list[str] = Field(default_factory=list)


class MeetingSummaryEntry(Compact):
    """Teams' AI summary of a meeting that a transcript search matched, once per meeting.

    It is AI-generated and can be wrong; the matches are the record.
    ``message_id`` is the recording's message, as the matches name it.
    """

    meeting: str | None = None  # the recording's title
    conversation_id: str
    message_id: str
    summary: str | None = None  # None when Teams has none for the meeting
    unavailable: bool = False  # the summary could not be read


class TranscriptSearchResult(Compact):
    items: list[TranscriptHit]
    coverage: TranscriptSearchCoverage
    # with include_summaries: one entry per matched meeting
    summaries: list[MeetingSummaryEntry] = Field(default_factory=list)
