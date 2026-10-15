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
from teams_connector.domain.errors import InvalidRequest
from teams_connector.domain.models import (
    ChatKind,
    ChatPage,
    ChatSearchResult,
    DownloadedFiles,
    ExportArtifact,
    ExportFormat,
    ExportProgress,
    ExportRequest,
    MessagePage,
    MessageSearchResult,
    SendResult,
)

log = logging.getLogger(__name__)

INSTRUCTIONS = """\
Read access to the signed-in user's Microsoft Teams chats and team channels, plus one explicitly \
authorized message-send tool.

- Finding conversations: list_chats lists chats and channels by recent activity (kind: one_on_one, \
group, meeting, channel, or self for the user's own notes chat). search_chats finds conversations by \
name, and with refresh by the members Teams matches for a plain name or email; read its coverage.
- Reading: get_messages returns a conversation's history newest pages first, oldest first within a \
page, with optional since/until and a total message_limit. Follow `cursor` for older messages. A \
page holds whole messages up to max_chars (at least one), so no message text is cut. \
is_self marks the user's own messages: use it rather than matching names.
- Searching message content: search_messages takes terms (1 to 25 phrases; a message matches when any \
term matches), optionally scoped by conversation_ids and since/until. Each term is one phrase, matched on \
whole words and word prefixes; it is never split on its spaces or read as query syntax. Hits are pointers: \
message_id is the id get_messages reads, and conversation_id is set when it matches a known chat. total is \
approximate. Read coverage before treating results as complete.
- Exporting: export_chats returns a local artifact path. One conversation without selected \
attachments returns a plain .txt/.jsonl file; with attachments it returns a .zip containing one \
conversation-named folder with the text export and retrieved files. Several conversations return one \
outer .zip, with an inner .zip for each conversation with attachments and a plain file for each \
without them. Attachments are included by default; set include_attachments=false to omit them. Date \
bounds and latest-N message_limit apply per conversation; export fetches the selected history, \
so a long selection takes a while. Read the returned file path with your file tools.
- Attachments: download_attachment saves the files a message names (its shared files and pasted images, \
shown as [File: name] and [Image: name] markers) and returns their local paths. Pass the message_id from \
get_messages, or the message's sent_at time instead.
- Messages are read from Teams on each call; nothing is kept locally. Messages with is_deleted=true were \
deleted in Teams, and Teams keeps no text for them; edited ones show their newest text.
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
        """Conversations whose name contains the query. With refresh (the default), also the named
        group and meeting chats that Teams matches by a member's name or email, with the members
        that matched."""
        return await (await services()).catalog.search(query, refresh=refresh, limit=limit, cursor=cursor)

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
            int,
            Field(
                ge=4_000,
                le=100_000,
                description="Text budget: whole messages up to this many characters, at least one.",
            ),
        ] = 30_000,
        cursor: Annotated[
            str | None,
            Field(description="Continuation; it restores the original options, which are then ignored."),
        ] = None,
    ) -> MessagePage:
        """One page of a conversation's history ), oldest first within
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
    async def search_messages(
        terms: Annotated[
            list[str] | None,
            Field(
                description="1 to 25 phrases; a message matches when ANY term matches (OR). Each term is "
                "one phrase: never split on its spaces, never read as query syntax, and no double quotes. "
                "Search v2 matches whole words and word prefixes: 'budget' also finds 'budgets', 'udget' "
                "finds nothing, and 'budget review' needs those words in that order. Omit only when "
                "continuing with a cursor."
            ),
        ] = None,
        conversation_ids: Annotated[
            list[str] | None,
            Field(description="Scope: up to 100 conversation ids from list_chats or search_chats."),
        ] = None,
        since: Since = None,
        until: Until = None,
        limit: Annotated[int, Field(ge=1, le=25, description="Hits per page, 1 to 25.")] = 25,
        cursor: str | None = None,
    ) -> MessageSearchResult:
        """Search Teams messages online through Microsoft Search v2, across all chats or within the chosen
        conversations and dates. Hits are pointers: message_id is the id get_messages reads, and
        conversation_id is set when it matches a known chat. total is approximate. Follow cursor only when
        more hits help; with a cursor, terms, scope and dates come from it."""
        return await (await services()).search.search_messages(
            terms,
            conversation_ids=conversation_ids,
            since=_utc(since),
            until=_utc(until),
            limit=limit,
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
        message_id: Annotated[
            str | None, Field(description="The message's id, from get_messages or search_messages.")
        ] = None,
        sent_at: Annotated[
            datetime | None,
            Field(description="The message's time, exactly as the TXT export prints it, instead of its id."),
        ] = None,
        names: Annotated[
            list[str] | None,
            Field(
                description="The files to save, by the names their markers show; all of them when omitted."
            ),
        ] = None,
    ) -> DownloadedFiles:
        """Save the files a message names (shared files and pasted images) as local files and return their
        paths. Pass exactly one of message_id or sent_at."""
        if (message_id is None) == (sent_at is None):
            raise InvalidRequest("Pass exactly one of message_id or sent_at.")
        when = _utc(sent_at) if sent_at is not None else None
        return await (await services()).files.download_attachments(
            conversation_id, message_id=message_id, sent_at=when, names=names
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
