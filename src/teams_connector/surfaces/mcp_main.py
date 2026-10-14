"""MCP server over stdio: thin tools over the shared service (architecture, "MCP tools").

One process per agent session. Nothing touches the network until the first tool call. Every tool is
read-only except send_message.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated, Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from teams_connector.bootstrap import AppContext, Services
from teams_connector.domain.models import (
    ArchiveStatus,
    ChatKind,
    ChatPage,
    ChatSearchResult,
    ExportArtifact,
    ExportFormat,
    ExportProgress,
    ExportRequest,
    MatchMode,
    MessageContent,
    MessagePage,
    RemoteMessageSearchResult,
    SavedFile,
    SearchResult,
    SendResult,
    SyncResult,
)

log = logging.getLogger(__name__)

INSTRUCTIONS = """\
Read access to the signed-in user's Microsoft Teams chats and team channels, plus one explicitly \
authorized message-send tool.

- Finding conversations: list_chats lists chats and channels by recent activity (kind: one_on_one, \
group, meeting, channel, or self for the user's own notes chat). search_chats finds conversations by \
name or by an indexed participant's name (rosters are indexed by sync_archive; read its coverage).
- Reading: get_messages returns a conversation's history newest pages first, oldest first within a \
page, with optional since/until and a total message_limit. Follow `cursor` for older messages. Long \
texts are shortened to fit the response (text_truncated); get_message_content continues one. \
is_self marks the user's own messages: use it rather than matching names.
- Searching message content: search_messages needs an explicit date range and a scope \
(conversation_ids, or all_chats=true). It first fetches that scope from Teams into the local archive \
(bounded per call: follow the cursor while preparation_complete is false), then matches literally \
(exact, substring or all_terms; case-insensitive, accents significant). Read coverage before treating \
results as complete.
- Online message discovery: search_remote_messages queries Teams message content through Microsoft \
Search v2 (at most 25 hits, query length up to 512 characters) and returns one bounded page of \
message pointers, previews, timestamps, and conversation identity candidates. It sets \
conversation_id only when a candidate exactly matches the local catalog. The \
catalog. The Search v2 message id is not yet proven interchangeable with get_messages message_id. \
For a matched conversation, use sent_at with get_messages since/until to inspect surrounding history; \
follow the returned search cursor only when more hits are useful. This tool does not discover \
transcripts or meeting recaps.
- Exporting: export_chats returns a local artifact path. One conversation without selected \
attachments returns a plain .txt/.jsonl file; with attachments it returns a .zip containing one \
conversation-named folder with the text export and retrieved files. Several conversations return one \
outer .zip, with an inner .zip for each conversation with attachments and a plain file for each \
without them. Attachments are included by default; set include_attachments=false to omit them. Date \
bounds and latest-N message_limit apply per conversation; export fetches the selected history, \
so a long selection takes a while. Read the returned file path with your file tools.
- Attachments: download_attachment saves a message's shared file (from get_messages) and returns its \
local path.
- The archive retains every message the connector has read, indefinitely. Messages with is_deleted=true \
were deleted in Teams and are shown from retention; edited ones show their newest text. sync_archive \
fetches history and rosters in bounded, resumable batches; archive_status reports coverage.
- Times are UTC; a date-time without an offset is taken as UTC.
- Sending: never call send_message unless the user explicitly authorized sending that content to that \
chat, in the current request or by confirming after you asked. If authorization is missing or \
ambiguous, ask and wait. user_confirmation records the authorization; it is not permission by itself. \
One authorization is not ongoing consent. Search, summaries and exports never imply permission to \
send. A send is attempted once: if its outcome is unknown, check the chat before trying again.
- If a tool says sign-in is required, ask the user to run the quoted `teams-connector auth` command in \
a terminal; never attempt to sign in yourself. "Access denied" errors are about that item, not the \
sign-in.
- Do not call these tools in parallel; prefer one larger call over many small ones.
"""

READ_ONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
LOCAL_FILE = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=False, openWorldHint=False
)
SEND = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True)

Since = Annotated[datetime | None, Field(description="Inclusive lower bound (ISO 8601; UTC if no offset).")]
Until = Annotated[datetime | None, Field(description="Inclusive upper bound (ISO 8601; UTC if no offset).")]
ConversationId = Annotated[str, Field(description="Conversation id from list_chats or search results.")]


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _logged[**P, R](function: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """Log tool name, duration and outcome. Never arguments or results."""

    @functools.wraps(function)
    async def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        started = time.monotonic()
        try:
            result = await function(*args, **kwargs)
        except Exception as exc:
            log.warning("tool %s failed (%s) in %dms", function.__name__, type(exc).__name__,
                        (time.monotonic() - started) * 1000)  # fmt: skip
            raise
        log.info("tool %s completed in %dms", function.__name__, (time.monotonic() - started) * 1000)
        return result

    return wrapped


def build_server(context: AppContext) -> FastMCP:
    mcp = FastMCP("lrh-teams-connector", instructions=INSTRUCTIONS, log_level="WARNING")

    async def services() -> Services:
        return await context.services()

    @mcp.tool(annotations=READ_ONLY)
    @_logged
    async def auth_status() -> dict[str, Any]:
        """Offline status: account, cached client credentials and resource-token expiry by profile."""
        status = context.tokens.status()
        return {
            "account": [a.username for a in status.accounts],
            "profiles": {
                p.profile: {
                    "client_credential_cached": p.client_credential_cached,
                    "access_token_expires_on": p.access_token_expires_on,
                    "purpose": p.purpose,
                }
                for p in status.profiles
            },
            "sign_in_command": context.tokens.sign_in_command(),
        }

    @mcp.tool(annotations=READ_ONLY)
    @_logged
    async def list_chats(
        refresh: Annotated[
            bool,
            Field(description="Refresh from Teams now (otherwise a cache up to 10 minutes old may answer)."),
        ] = False,  # fmt: skip
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
        cursor: str | None = None,
        include_empty: Annotated[bool, Field(description="Include chats Teams reports as empty.")] = False,
        kind: Annotated[ChatKind | None, Field(description="Only this kind of conversation.")] = None,
    ) -> ChatPage:
        """Chats and channels, most recent activity first. coverage.complete is limited to coverage.scope."""
        return await (await services()).catalog.list(
            refresh=refresh, limit=limit, cursor=cursor, include_empty=include_empty, kind=kind
        )

    @mcp.tool(annotations=READ_ONLY)
    @_logged
    async def search_chats(
        query: Annotated[str, Field(description="Case-insensitive part of a chat or participant name.")],
        refresh: Annotated[bool, Field(description="Refresh the chat list first (recommended).")] = True,
        limit: Annotated[int, Field(ge=1, le=100)] = 25,
        cursor: str | None = None,
    ) -> ChatSearchResult:
        """Conversations whose name, or an indexed participant's name, contains the query."""
        return await (await services()).catalog.search(query, refresh=refresh, limit=limit, cursor=cursor)

    @mcp.tool(annotations=READ_ONLY)
    @_logged
    async def search_remote_messages(
        query: Annotated[
            str,
            Field(max_length=512, description="Text to search online in Teams messages."),
        ],
        limit: Annotated[int, Field(ge=1, le=25, description="Hits returned in this online page.")] = 20,
        cursor: str | None = None,
    ) -> RemoteMessageSearchResult:
        """Search Teams messages online through Microsoft Search v2. Returns pointers and one page;
        it does not download or search the local archive. Follow the cursor only when more hits help."""
        return await (await services()).search.search_remote_messages(query, limit=limit, cursor=cursor)

    @mcp.tool(annotations=READ_ONLY)
    @_logged
    async def get_messages(
        conversation_id: ConversationId,
        since: Since = None,
        until: Until = None,
        message_limit: Annotated[
            int | None, Field(ge=1, description="Total latest messages to return across all pages.")
        ] = None,
        page_size: Annotated[int, Field(ge=1, le=50)] = 25,
        max_chars: Annotated[
            int, Field(ge=4_000, le=100_000, description="Text budget for the page.")
        ] = 30_000,
        cursor: Annotated[
            str | None,
            Field(description="Continuation; it restores the original options, which are then ignored."),
        ] = None,
    ) -> MessagePage:
        """One page of a conversation's history (Teams merged with the local archive), oldest first within
        the page; the cursor continues with older messages."""
        return await (await services()).history.get_messages(
            conversation_id,
            since=_utc(since),
            until=_utc(until),
            message_limit=message_limit,
            page_size=page_size,
            max_chars=max_chars,
            cursor=cursor,
        )

    @mcp.tool(annotations=READ_ONLY)
    @_logged
    async def get_message_content(
        conversation_id: ConversationId,
        message_id: str,
        offset: Annotated[int, Field(ge=0)] = 0,
        max_chars: Annotated[int, Field(ge=1, le=100_000)] = 50_000,
    ) -> MessageContent:
        """A character range of one archived message's text (continue a text_truncated message)."""
        return (await services()).history.content(
            conversation_id, message_id, offset=offset, max_chars=max_chars
        )

    @mcp.tool(annotations=READ_ONLY)
    @_logged
    async def search_messages(
        query: Annotated[str, Field(description="Text to find in message text.")],
        since: Since = None,
        until: Until = None,
        conversation_ids: Annotated[
            list[str] | None, Field(description="Scope: up to 100 conversations (or set all_chats).")
        ] = None,
        all_chats: Annotated[bool, Field(description="Scope: every conversation in the catalog.")] = False,
        match_mode: MatchMode = "substring",
        limit: Annotated[int, Field(ge=1, le=50, description="Matches returned per call.")] = 20,
        max_matches: Annotated[int, Field(ge=1, description="Total matches across calls.")] = 1000,
        max_pages: Annotated[int, Field(ge=1, le=100, description="Remote pages fetched per call.")] = 10,
        cursor: str | None = None,
    ) -> SearchResult:
        """Fetch a date range of the scoped conversations into the archive, then match it literally.
        since and until are required. Repeat with the cursor while preparation is incomplete."""
        return await (await services()).search.search_messages(
            query,
            since=_utc(since),
            until=_utc(until),
            conversation_ids=conversation_ids,
            all_chats=all_chats,
            mode=match_mode,
            limit=limit,
            max_matches=max_matches,
            max_pages=max_pages,
            cursor=cursor,
        )

    @mcp.tool(annotations=LOCAL_FILE)
    @_logged
    async def export_chats(
        conversation_ids: Annotated[list[str], Field(min_length=1, max_length=500)],
        ctx: Context,
        since: Since = None,
        until: Until = None,
        message_limit: Annotated[int | None, Field(ge=1, description="Latest N messages per chat.")] = None,
        include_attachments: Annotated[
            bool,
            Field(description="Include selected message files and images beside the conversation export."),
        ] = True,
        format: Annotated[
            ExportFormat, Field(description="txt (for people) or jsonl (one JSON record per message).")
        ] = "txt",
    ) -> ExportArtifact:
        """Export selected conversations and return the local artifact path.

        A single conversation without selected attachments returns a plain text/JSONL file; with
        attachments it returns a ZIP containing a folder with the text and retrieved files. Several
        conversations return one outer ZIP of each conversation artifact. Selected history is
        fetched from Teams first.
        """
        request = ExportRequest(
            conversation_ids=conversation_ids,
            since=_utc(since),
            until=_utc(until),
            message_limit=message_limit,
            format=format,
            include_attachments=include_attachments,
        )
        loop = asyncio.get_running_loop()
        pending: set[asyncio.Task[None]] = set()
        last: dict[str, tuple[str, int, int, int]] = {"step": ("", -1, -1, -1)}

        def progress(state: ExportProgress) -> None:
            step = (
                state.phase,
                state.chat_index,
                state.messages_retrieved // 500,
                state.attachments_downloaded + state.attachments_failed,
            )
            if step == last["step"]:
                return
            last["step"] = step
            text = (
                f"{state.phase}: chat {state.chat_index}/{state.chats_total}, "
                f"{state.messages_retrieved} messages, "
                f"{state.attachments_downloaded} attachments downloaded, "
                f"{state.attachments_failed} unavailable"
            )
            task = loop.create_task(ctx.report_progress(state.chat_index, state.chats_total, text))
            pending.add(task)
            task.add_done_callback(pending.discard)

        try:
            return await (await services()).exports.export(request, progress=progress)
        finally:
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    @mcp.tool(annotations=LOCAL_FILE)
    @_logged
    async def download_attachment(
        conversation_id: ConversationId,
        message_id: str,
        name: Annotated[
            str | None, Field(description="Attachment name, when the message has several.")
        ] = None,
    ) -> SavedFile:
        """Save one shared file attached to an archived message as a local file and return its path."""
        return await (await services()).files.download_attachment(conversation_id, message_id, name)

    @mcp.tool(annotations=READ_ONLY)
    @_logged
    async def sync_archive(
        conversation_id: Annotated[str | None, Field(description="Just this conversation.")] = None,
        max_chats: Annotated[int, Field(ge=1, le=200)] = 25,
        max_pages: Annotated[int, Field(ge=1, le=200)] = 20,
        include_rosters: bool = True,
        refresh_latest: Annotated[
            bool, Field(description="Also re-read the newest page of complete chats.")
        ] = True,
    ) -> SyncResult:
        """Fetch history and rosters into the archive in bounded batches; repeated calls resume each chat
        and rotate fairly across chats. Reads Teams; changes nothing there."""
        return await (await services()).archive.sync(
            conversation_id=conversation_id,
            max_chats=max_chats,
            max_pages=max_pages,
            include_rosters=include_rosters,
            refresh_latest=refresh_latest,
        )

    @mcp.tool(annotations=READ_ONLY)
    @_logged
    async def archive_status(
        conversation_id: str | None = None,
        since: Since = None,
        until: Until = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
        cursor: str | None = None,
    ) -> ArchiveStatus:
        """Per-conversation archive coverage: history completeness, archived span, rosters, and whether
        the requested range is fully archived."""
        return (await services()).archive.status(
            conversation_id=conversation_id, since=_utc(since), until=_utc(until), limit=limit, cursor=cursor
        )

    @mcp.tool(annotations=SEND)
    @_logged
    async def send_message(
        conversation_id: ConversationId,
        expected_chat_name: Annotated[
            str, Field(description="The chat's exact current name from list_chats; checked before sending.")
        ],
        text: Annotated[str, Field(description="Plain text, up to 10,000 characters (HTML is escaped).")],
        user_confirmation: Annotated[
            str,
            Field(
                description="A short quote or record of the user's explicit authorization for this recipient "
                "and content. Never invent or infer it; it does not authorize anything by itself."
            ),
        ],
    ) -> SendResult:
        """Send one plain-text message to a chat or channel. External side effect: only with the user's
        explicit authorization for this recipient and content. Attempted once, never retried."""
        return await (await services()).sender.send(
            conversation_id,
            expected_chat_name=expected_chat_name,
            text=text,
            user_confirmation=user_confirmation,
        )

    return mcp


def run(*, unsecure: bool) -> None:
    build_server(AppContext(unsecure=unsecure)).run("stdio")
