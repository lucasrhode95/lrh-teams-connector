from __future__ import annotations

import argparse
import logging
import functools
import json
import sys
import time
from datetime import datetime
from typing import Literal, TypedDict

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, ResourceLink, TextContent, ToolAnnotations

from teams_exporter.app_logging import configure_app_logging
from teams_exporter.teams_service import TeamsService
from teams_exporter.token_provider import TokenProvider


logger = logging.getLogger(__name__)


def _result_log_summary(result: object) -> str:
    """Summarize only response shape and counts; never log response content."""
    if isinstance(result, str):
        return f"response_chars={len(result)}"
    if not isinstance(result, dict):
        return f"result_type={type(result).__name__}"
    fields = []
    for key in ("items", "chats", "messages"):
        value = result.get(key)
        if isinstance(value, list):
            fields.append(f"{key}_count={len(value)}")
    for key in ("returned_count", "total_matches_in_index", "message_count", "byte_size"):
        value = result.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            fields.append(f"{key}={value}")
    status = result.get("status")
    if isinstance(status, str) and status in {
        "complete", "preparing", "accepted", "cancelled", "failed"
    }:
        fields.append(f"status={status}")
    return " ".join(fields) or f"result_fields={len(result)}"


def _logged_operation(function):
    """Add privacy-safe timing and outcome logs around one MCP operation."""
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        operation = function.__name__
        started = time.perf_counter()
        logger.info("MCP operation started name=%s", operation)
        try:
            result = function(*args, **kwargs)
        except Exception as exc:
            logger.warning(
                "MCP operation failed name=%s error_type=%s duration_ms=%.1f",
                operation,
                type(exc).__name__,
                (time.perf_counter() - started) * 1000,
            )
            raise
        logger.info(
            "MCP operation completed name=%s duration_ms=%.1f %s",
            operation,
            (time.perf_counter() - started) * 1000,
            _result_log_summary(result),
        )
        return result

    return wrapped


SERVER_INSTRUCTIONS = (
    "Read access to the signed-in user's Teams chat catalog and message history, plus one "
    "explicitly authorized message-send tool. Never call send_message unless the user "
    "explicitly authorized sending to that chat with that content, either in the current "
    "request or by confirming after you asked. If authorization is missing or ambiguous, "
    "ask the user and wait. A required user_confirmation argument records authorization; "
    "it is not permission by itself. Scheduled tasks may send only when their instructions "
    "explicitly authorize the destination and sending scope. Do not treat one test-message "
    "authorization as ongoing or blanket consent. Do not send automatically during search, "
    "summarization, or export. "
    "Message searches first fetch and archive the requested chat/date scope, then use "
    "local SQLite literal matching; no unverified Teams search endpoint is used. "
    "Follow the returned cursor while preparation is incomplete, and read the per-chat "
    "coverage before treating results as complete. Participant search uses indexed rosters. "
    "list_chats includes the signed-in user's self chat and marks it with is_self_chat. "
    "Its coverage describes catalog scope, source, exclusions, and unresolved fallback names. "
    "get_messages returns author_id and is_self when sender identity is available; "
    "use is_self rather than matching display names to identify the user's own messages. "
    "Long message bodies can be continued with get_message_content. Long conversation "
    "exports are resumable with export_chat and available as a bounded MCP Resource plus "
    "read_export tool. "
    "Search cursors expire after 30 minutes and are lost when this server process restarts."
)

READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    openWorldHint=False,
)

SEND_MESSAGE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    openWorldHint=True,
)


class ChatRecord(TypedDict):
    id: str
    name: str
    chat_type: str | None
    is_self_chat: bool
    last_message_at: str | None
    last_message_preview: str | None
    unread: bool | None
    is_empty_conversation: bool | None


class CatalogCoverage(TypedDict):
    catalog_complete: bool
    catalog_source: str
    catalog_scope: str
    csa_reported_partial: bool | None
    chat_service_unique_rows: int | None
    fallback_chats_added: int
    non_chat_service_rows_excluded: int
    unclassified_service_rows_excluded: int
    fallback_name_lookups_attempted: int
    fallback_chats_without_resolved_name: int
    total_chats: int
    returned_this_page: int
    empty_chats_excluded: int


class ListChatsResult(TypedDict):
    items: list[ChatRecord]
    next_page_token: str | None
    database_notice: str | None
    coverage: CatalogCoverage


class ParticipantRecord(TypedDict):
    id: str | None
    display_name: str | None


class ParticipantChatRecord(ChatRecord):
    matched_participants: list[ParticipantRecord]


class ParticipantSearchCoverage(TypedDict):
    method: str
    server_side_search_used: bool
    catalog_chats: int
    rosters_indexed: int
    rosters_synced: int
    rosters_not_indexed: int
    rosters_with_unresolved_names: int
    unresolved_participants: int
    complete_for_chat_catalog: bool
    index_generation: int
    sync_tool: str


class SearchChatsResult(TypedDict):
    items: list[ParticipantChatRecord]
    total_matches_in_index: int
    next_cursor: str | None
    database_notice: str | None
    coverage: ParticipantSearchCoverage


class ConversationRecord(TypedDict):
    id: str
    name: str
    is_self_chat: bool


class MessageRecord(TypedDict):
    id: str | None
    conversation_id: str
    author: str
    author_id: str | None
    is_self: bool | None
    timestamp: str | None
    content: str
    content_truncated: bool
    content_char_count: int
    content_next_offset: int | None
    message_type: str | None
    is_deleted: bool
    edited_at: str | None
    deleted_at: str | None


class MessagePageCoverage(TypedDict):
    complete: bool
    page_size: int
    requested_page_size: int
    raw_items_scanned_total: int
    missing_timestamp_items_excluded_total: int
    retry_count_this_call: int
    max_response_chars: int
    remote_pages_fetched_total: int
    remote_history_complete: bool
    archive_messages_in_page: int
    messages_with_truncated_content: int
    content_continuation_tool: str | None
    since: str | None
    until: str | None
    ordering: str
    continuation_direction: str


class GetMessagesResult(TypedDict):
    conversation: ConversationRecord
    items: list[MessageRecord]
    next_cursor: str | None
    database_notice: str | None
    coverage: MessagePageCoverage


class MessageContentResult(TypedDict):
    conversation_id: str
    message_id: str
    offset: int
    content: str
    content_char_count: int
    next_offset: int | None
    complete: bool
    is_deleted: bool
    edited_at: str | None
    deleted_at: str | None


class SearchChatCoverage(TypedDict):
    conversation_id: str
    since: str | None
    until: str | None
    remote_pages_fetched: int
    remote_items_scanned: int
    missing_timestamp_items: int
    remote_complete: bool
    last_refreshed_at: str | None


class MessageSearchCoverage(TypedDict):
    method: str
    server_side_search_used: bool
    complete_for_requested_scope: bool
    preparation_complete: bool
    index_generation: int | None
    requested_since: str | None
    requested_until: str | None
    requested_chat_ids: list[str]
    chat_coverage: list[SearchChatCoverage]
    pages_fetched_this_call: int
    max_pages_to_fetch: int
    failed_chat_id: str | None
    error: str | None
    continuation_tool: str | None


class SendMessageResult(TypedDict):
    conversation_id: str
    chat_name: str
    message_id: str | None
    client_message_id: str | None
    sent_at: str | None
    submitted_characters: int
    delivery_status: Literal["accepted"]


class SearchMessageRecord(TypedDict):
    id: str
    conversation_id: str
    chat_name: str
    author: str
    author_id: str | None
    is_self: bool | None
    timestamp: str | None
    excerpt: str
    message_type: str | None
    is_deleted: bool
    deleted_at: str | None


class SearchMessagesResult(TypedDict):
    items: list[SearchMessageRecord]
    max_matches: int
    returned_count: int
    returned_total: int
    results_truncated: bool
    truncation_reason: str | None
    next_cursor: str | None
    database_notice: str | None
    coverage: MessageSearchCoverage


class ExportCoverage(TypedDict):
    complete: bool
    remote_pages_fetched_total: int
    remote_items_scanned_total: int
    remote_messages_retrieved_total: int | None
    message_limit_reached: bool
    missing_timestamp_items_excluded_total: int
    retry_count_this_call: int
    archive_included: bool


class ExportChatResult(TypedDict):
    export_id: str
    status: Literal["preparing", "complete"]
    resource_uri: str | None
    mime_type: str | None
    byte_size: int | None
    message_count: int | None
    conversation_id: str
    since: str | None
    until: str | None
    coverage: ExportCoverage
    next_call_tool: str | None
    next_call_arguments: dict[str, object] | None


class ReadExportResult(TypedDict):
    export_id: str
    offset_bytes: int
    content: str
    byte_size: int
    next_offset_bytes: int | None
    complete: bool


class IndexCounts(TypedDict):
    catalog_chats: int
    indexed_messages: int
    participant_rows: int
    complete_message_histories: int
    complete_participant_rosters: int
    generation: int


class ParticipantSyncResult(TypedDict):
    attempted: bool
    complete: bool
    unresolved_participants: int
    error: str | None
    participant_count: int
    named_participant_count: int


class SyncedChatRecord(TypedDict):
    chat_id: str
    chat_name: str
    messages_added_this_call: int
    message_pages_fetched: int
    message_retries_this_call: int
    message_history_complete: bool
    messages_stored_total: int
    oldest_indexed_message_at: str | None
    newest_indexed_message_at: str | None
    participant_roster_complete: bool
    unresolved_participants: int
    participant_result: ParticipantSyncResult
    message_error: str | None


class SyncCoverage(TypedDict):
    chats_attempted: int
    messages_remaining_this_call: int
    pages_remaining_this_call: int
    index: IndexCounts
    continuation_persisted: bool
    messages_retained_after_fetch: bool


class SyncSearchIndexResult(TypedDict):
    items: list[SyncedChatRecord]
    database_notice: str | None
    coverage: SyncCoverage


class ChatIndexStatus(TypedDict):
    chat_id: str
    chat_name: str
    catalog_last_message_at: str | None
    catalog_refreshed_at: str
    message_history_complete: bool
    messages_stored: int
    messages_scanned: int
    message_pages: int
    oldest_message_at: str | None
    newest_message_at: str | None
    messages_indexed_at: str | None
    missing_timestamp_messages: int
    participant_sync_complete: bool
    participants_scanned: int
    unresolved_participants: int
    participant_sync_error: str | None
    participants_indexed_at: str | None
    sync_attempted_at: str | None
    requested_range_covered: bool


class StatusCoverage(TypedDict):
    catalog_chats: int
    covered_chats: int
    uncovered_chats: int
    complete_for_requested_scope: bool
    offset: int
    returned: int
    index_generation: int
    requested_since: str | None
    requested_until: str | None
    conversation_id: str | None
    index_totals: IndexCounts


class SearchIndexStatusResult(TypedDict):
    items: list[ChatIndexStatus]
    next_page_token: str | None
    database_notice: str | None
    coverage: StatusCoverage


def _parse_datetime(value: str | None, name: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be an ISO-8601 timestamp with a timezone.")
    try:
        result = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO-8601 timestamp with a timezone.") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone, for example 2026-09-28T12:00:00Z.")
    return result


def create_mcp_server(
    service: TeamsService | None = None,
    unsecure_tokens: bool = False,
) -> FastMCP:
    """Build the standard MCP tool server around the reusable Teams service."""
    teams = service or TeamsService(unsecure_tokens=unsecure_tokens)
    server = FastMCP(
        "teams-chat-reader",
        instructions=SERVER_INSTRUCTIONS,
    )

    @server.resource(
        "teams-export://exports/{export_id}/parts/{offset_bytes}",
        name="teams_export_part",
        description=(
            "A bounded UTF-8-safe section of a completed Teams text export. "
            "The JSON body includes content, next_offset_bytes, and complete."
        ),
        mime_type="application/json",
    )
    @_logged_operation
    def teams_export_part(export_id: str, offset_bytes: int) -> str:
        """Return a bounded part of an existing export resource."""
        part = teams.read_export(export_id, offset_bytes=offset_bytes)
        next_uri = (
            None
            if part["next_offset_bytes"] is None
            else f"teams-export://exports/{export_id}/parts/{part['next_offset_bytes']}"
        )
        return json.dumps(
            {**part, "next_resource_uri": next_uri},
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @server.tool(annotations=READ_ONLY)
    @_logged_operation
    def list_chats(
        page_size: int = 50,
        page_token: str | None = None,
        refresh: bool = False,
        no_empty_chats: bool = True,
    ) -> ListChatsResult:
        """List Teams chats by most recent activity, using bounded pages.

        The signed-in user's self chat is included and marked with is_self_chat.
        Coverage describes the source catalog and excluded or unclassified
        Chat Service rows. catalog_complete is limited to catalog_scope.

        Args:
            page_size: Number of chats to return (1–100).
            page_token: Token returned by the preceding page, if any.
            refresh: Reload the chat catalog instead of using the short-lived cache.
            no_empty_chats: Exclude conversations the catalog marks as empty before paging.
        """
        return teams.list_chats(
            page_size=page_size,
            page_token=page_token,
            refresh=refresh,
            no_empty_chats=no_empty_chats,
        )

    @server.tool(annotations=READ_ONLY)
    @_logged_operation
    def search_chats(
        participant_query: str,
        page_size: int = 25,
        cursor: str | None = None,
    ) -> SearchChatsResult:
        """Find chats in the locally indexed participant rosters.

        Teams server-side participant search is not used because its behavior is
        not verified. Sync rosters first with sync_search_index. Coverage reports
        how many catalog chats have a successfully indexed roster.

        Args:
            participant_query: Case-insensitive name substring to match.
            page_size: Maximum matching chats to return (1–50).
            cursor: Continue paging through local matches for the same query.
        """
        return teams.search_chats(
            participant_query,
            page_size=page_size,
            cursor=cursor,
        )

    @server.tool(annotations=READ_ONLY)
    @_logged_operation
    def get_messages(
        conversation_id: str,
        page_size: int = 25,
        since: str | None = None,
        until: str | None = None,
        cursor: str | None = None,
        max_response_chars: int = 30_000,
    ) -> GetMessagesResult:
        """Read one bounded history page from the live chat and local archive.

        Results include author_id and is_self when the service exposes a stable
        sender identity. Results are ordered oldest to newest within each page.
        Bodies are bounded by max_response_chars; use get_message_content to read
        a truncated body's next character range.
        The continuation cursor fetches older history. Date values must be
        ISO-8601 timestamps with a timezone; pass the same bounds when continuing.
        Coverage reports remote/archive composition and transient retries.

        Args:
            conversation_id: Stable Teams conversation ID from list_chats/search results. The self chat uses 48:notes.
            page_size: Requested message count (1–50); the response-size budget may lower the effective count.
            since: Inclusive lower timestamp, or null for the oldest available history.
            until: Inclusive upper timestamp, or null for no upper bound.
            cursor: Continuation returned by the previous page.
            max_response_chars: Maximum serialized response characters (4,096–100,000).
        """
        return teams.get_messages(
            conversation_id,
            page_size=page_size,
            since=_parse_datetime(since, "since"),
            until=_parse_datetime(until, "until"),
            cursor=cursor,
            max_response_chars=max_response_chars,
        )

    @server.tool(annotations=READ_ONLY)
    @_logged_operation
    def get_message_content(
        conversation_id: str,
        message_id: str,
        offset: int = 0,
        max_chars: int = 50_000,
    ) -> MessageContentResult:
        """Read a bounded character range from a message already in the archive.

        Use the content_next_offset returned by get_messages and repeat this tool
        until next_offset is null. Message IDs and conversation IDs are stable.

        Args:
            conversation_id: Stable conversation ID returned by list_chats.
            message_id: Stable message ID from get_messages.
            offset: Character offset to continue from, returned by the preceding call.
            max_chars: Maximum characters to return (1–50,000).
        """
        return teams.get_message_content(
            conversation_id,
            message_id,
            offset=offset,
            max_chars=max_chars,
        )

    @server.tool(annotations=READ_ONLY)
    @_logged_operation
    def search_messages(
        query: str,
        since: str,
        until: str,
        match_mode: Literal["exact", "substring", "all_terms"] = "substring",
        conversation_ids: list[str] | None = None,
        all_catalog: bool = False,
        result_limit: int = 20,
        max_matches: int = 1_000,
        max_pages_to_fetch: int = 10,
        cursor: str | None = None,
    ) -> SearchMessagesResult:
        """Fetch and archive an explicit date/chat scope, then search its messages.

        Teams content search is not used because no server-side route is verified.
        Search must be scoped to one or more conversation IDs, or all_catalog=true,
        and an inclusive date range. If preparation is incomplete, this call returns
        no matches; repeat with the returned cursor until preparation_complete is
        true. Coverage identifies every chat, date range, page count, and any error.
        Matching is local exact, substring, or all-terms matching after Unicode
        NFKC normalization and case folding; accents remain significant.

        Args:
            query: Text to find in formatted message bodies.
            since: Required inclusive lower timestamp, with timezone.
            until: Required inclusive upper timestamp, with timezone.
            match_mode: 'exact', 'substring', or 'all_terms'.
            conversation_ids: Explicit chat scope; set this or all_catalog=true.
            all_catalog: Search every chat in the current chat catalog.
            result_limit: Maximum matching messages returned in this call (1–50).
            max_matches: Maximum total results across calls; any positive integer.
            max_pages_to_fetch: Maximum remote pages to fetch in this call (1–100).
            cursor: Continue paging through matches for the same query and scope.
        """
        return teams.search_messages(
            query,
            match_mode=match_mode,
            conversation_ids=conversation_ids,
            all_catalog=all_catalog,
            since=_parse_datetime(since, "since"),
            until=_parse_datetime(until, "until"),
            result_limit=result_limit,
            max_matches=max_matches,
            max_pages_to_fetch=max_pages_to_fetch,
            cursor=cursor,
        )

    @server.tool(annotations=READ_ONLY)
    @_logged_operation
    def export_chat(
        conversation_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        message_limit: int | None = None,
        export_id: str | None = None,
        max_pages_to_fetch: int = 20,
    ) -> ExportChatResult:
        """Build a resumable text export and expose completed output as an MCP Resource.

        Start with conversation_id. If status is preparing, repeat with export_id
        and the returned next_call_arguments until status is complete. Then read
        resource_uri with MCP resources/read, or use read_export for bounded inline
        sections. Fresh remote pages are retained in the account's SQLite archive.

        Args:
            conversation_id: Stable chat ID for a new export; omit when resuming by export_id.
            since: Optional inclusive UTC or timezone-aware ISO-8601 lower bound.
            until: Optional inclusive UTC or timezone-aware ISO-8601 upper bound.
            message_limit: Optional maximum number of latest messages in the
                merged archive; also bounds remote retrieval to that count.
            export_id: ID returned by a previous incomplete export_chat call.
            max_pages_to_fetch: Maximum remote pages per call (1–100).
        """
        result = teams.export_chat(
            conversation_id,
            since=_parse_datetime(since, "since"),
            until=_parse_datetime(until, "until"),
            message_limit=message_limit,
            export_id=export_id,
            max_pages_to_fetch=max_pages_to_fetch,
        )
        if result["status"] == "complete" and result["resource_uri"]:
            content = [
                ResourceLink(
                    type="resource_link",
                    name=f"Teams conversation export {result['export_id']}",
                    title=f"{result['message_count']} messages",
                    uri=result["resource_uri"],
                    description="Read this bounded export resource in parts.",
                    mimeType=result["mime_type"],
                    size=result["byte_size"],
                )
            ]
        else:
            content = [
                TextContent(
                    type="text",
                    text=(
                        "Export preparation is incomplete. Repeat export_chat with "
                        f"{json.dumps(result['next_call_arguments'], separators=(',', ':'))}."
                    ),
                )
            ]
        return CallToolResult(content=content, structuredContent=result)

    @server.tool(annotations=READ_ONLY)
    @_logged_operation
    def read_export(
        export_id: str,
        offset_bytes: int = 0,
        max_bytes: int = 50_000,
    ) -> ReadExportResult:
        """Read one bounded UTF-8-safe section of a completed text export.

        Repeat with next_offset_bytes until complete is true. For resource-based
        reads, use the same offset as the resource URI's final path component.

        Args:
            export_id: Completed or preparing export ID returned by export_chat.
            offset_bytes: Byte offset returned by the preceding read.
            max_bytes: Maximum section size in bytes (4–100,000).
        """
        return teams.read_export(
            export_id,
            offset_bytes=offset_bytes,
            max_bytes=max_bytes,
        )

    @server.tool(annotations=READ_ONLY)
    @_logged_operation
    def sync_search_index(
        conversation_id: str | None = None,
        max_chats_to_sync: int = 25,
        max_messages_to_fetch: int = 1_000,
        max_pages_to_fetch: int = 20,
        include_participants: bool = True,
        refresh_latest: bool = True,
    ) -> SyncSearchIndexResult:
        """Fetch bounded Teams history and participant rosters into local SQLite.

        Repeated calls resume older history from saved service continuation links.
        Once a chat is fully indexed, calls refresh its newest message page while
        retaining older messages. Per-chat results report read retries in this
        call. This tool reads Teams but does not change state.

        Args:
            conversation_id: Sync just this known chat, or null for recent chats.
            max_chats_to_sync: Maximum catalog chats to consider (1–100).
            max_messages_to_fetch: Approximate raw message work limit (1–5000).
            max_pages_to_fetch: Maximum service pages to request (1–100).
            include_participants: Fetch any chat roster not indexed yet.
            refresh_latest: Refresh newest pages for histories already complete.
        """
        return teams.sync_search_index(
            conversation_id=conversation_id,
            max_chats_to_sync=max_chats_to_sync,
            max_messages_to_fetch=max_messages_to_fetch,
            max_pages_to_fetch=max_pages_to_fetch,
            include_participants=include_participants,
            refresh_latest=refresh_latest,
        )

    @server.tool(annotations=READ_ONLY)
    @_logged_operation
    def get_search_index_status(
        page_size: int = 50,
        page_token: str | None = None,
        conversation_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> SearchIndexStatusResult:
        """Inspect per-chat message and participant index coverage.

        The page includes completion flags, indexed timestamp bounds, freshness,
        missing timestamp counts, and whether the requested range is covered.

        Args:
            page_size: Number of chat coverage records (1–100).
            page_token: Continue to the next catalog page, if returned.
            conversation_id: Restrict status to one known chat.
            since: Inclusive range start as a timezone-aware ISO-8601 timestamp.
            until: Inclusive range end as a timezone-aware ISO-8601 timestamp.
        """
        return teams.get_search_index_status(
            page_size=page_size,
            page_token=page_token,
            conversation_id=conversation_id,
            since=_parse_datetime(since, "since"),
            until=_parse_datetime(until, "until"),
        )

    @server.tool(annotations=SEND_MESSAGE)
    @_logged_operation
    def send_message(
        conversation_id: str,
        expected_chat_name: str,
        text: str,
        user_confirmation: str,
    ) -> SendMessageResult:
        """Send one plain-text message to a known Teams chat.

        This creates an external side effect. Call this tool only after the user
        explicitly authorized this message to this recipient in their request,
        or after asking and receiving an explicit yes. If there is no clear
        authorization, ask the user and wait. A value in user_confirmation is
        an audit record, not a substitute for actual user permission. For a
        scheduled task, its instructions must explicitly authorize the recipient
        and sending scope. A one-time authorization does not grant future consent.
        Search, summaries, and exports never imply permission to send. The Teams
        request is attempted once; if its result is uncertain, verify the chat
        before trying again to avoid duplicate messages.

        Args:
            conversation_id: Stable ID from list_chats or a search result.
            expected_chat_name: Exact current chat name from list_chats; checked against the live catalog before sending.
            text: Plain-text message body, up to 10,000 characters. HTML is escaped.
            user_confirmation: Short quote or record of the user's explicit authorization for this recipient and content (1–1,000 characters). Never invent or infer it; this value alone does not authorize the action.
        """
        return teams.send_message(
            conversation_id,
            expected_chat_name,
            text,
            user_confirmation=user_confirmation,
        )

    return server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m teams_exporter.mcp_server")
    parser.add_argument(
        "--unsecure",
        action="store_true",
        help="use the plaintext development MSAL cache instead of encrypted storage",
    )
    args = parser.parse_args(argv)
    configure_app_logging("mcp", stdout=False)
    if args.unsecure:
        debug_path = TokenProvider(unsecure=True).cache_path
        print(TokenProvider.unsecure_warning(debug_path), file=sys.stderr)
    logger.info("Starting MCP server transport=stdio")
    try:
        create_mcp_server(unsecure_tokens=args.unsecure).run(transport="stdio")
    except Exception as exc:
        logger.error("MCP server stopped error_type=%s", type(exc).__name__)
        raise SystemExit(1) from None
    logger.info("MCP server stopped")


if __name__ == "__main__":
    main()
