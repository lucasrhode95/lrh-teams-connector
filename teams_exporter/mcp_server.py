from __future__ import annotations

import logging
import sys
from datetime import datetime
from typing import Literal, TypedDict

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from teams_exporter.teams_service import TeamsService


SERVER_INSTRUCTIONS = (
    "Read-only access to the signed-in user's Teams chat catalog and message history. "
    "Message and participant searches query a persistent local SQLite index. "
    "Use sync_search_index to fetch bounded history batches and get_search_index_status "
    "to inspect indexed coverage. Search results explicitly report incomplete coverage. "
    "Search cursors expire after 30 minutes and are lost when this server process restarts."
)

READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    openWorldHint=False,
)


class ChatRecord(TypedDict):
    id: str
    name: str
    chat_type: str | None
    last_message_at: str | None
    last_message_preview: str | None
    unread: bool | None


class CatalogCoverage(TypedDict):
    catalog_complete: bool
    total_chats: int
    returned_this_page: int


class ListChatsResult(TypedDict):
    items: list[ChatRecord]
    next_page_token: str | None
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
    coverage: ParticipantSearchCoverage


class ConversationRecord(TypedDict):
    id: str
    name: str


class MessageRecord(TypedDict):
    id: str | None
    conversation_id: str
    author: str
    timestamp: str | None
    content: str
    content_truncated: bool
    content_char_count: int
    message_type: str | None


class MessagePageCoverage(TypedDict):
    complete: bool
    page_size: int
    raw_items_scanned_total: int
    missing_timestamp_items_excluded_total: int
    since: str | None
    until: str | None
    ordering: str
    continuation_direction: str


class GetMessagesResult(TypedDict):
    conversation: ConversationRecord
    items: list[MessageRecord]
    next_cursor: str | None
    coverage: MessagePageCoverage


class SearchMessageRecord(TypedDict):
    id: str
    conversation_id: str
    chat_name: str
    author: str
    timestamp: str | None
    excerpt: str
    message_type: str | None


class MessageSearchCoverage(TypedDict):
    method: str
    server_side_search_used: bool
    complete_for_requested_scope: bool
    catalog_chats: int
    covered_chats: int
    uncovered_chats: int
    requested_since: str | None
    requested_until: str | None
    conversation_id: str | None
    indexed_messages: int
    index_generation: int
    chat_coverage_tool: str


class SearchMessagesResult(TypedDict):
    items: list[SearchMessageRecord]
    total_matches_in_index: int
    next_cursor: str | None
    coverage: MessageSearchCoverage


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


def create_mcp_server(service: TeamsService | None = None) -> FastMCP:
    """Build the standard MCP tool server around the reusable Teams service."""
    teams = service or TeamsService()
    server = FastMCP(
        "teams-chat-reader",
        instructions=SERVER_INSTRUCTIONS,
    )

    @server.tool(annotations=READ_ONLY)
    def list_chats(
        page_size: int = 50,
        page_token: str | None = None,
        refresh: bool = False,
    ) -> ListChatsResult:
        """List Teams chats by most recent activity, using bounded pages.

        Args:
            page_size: Number of chats to return (1–100).
            page_token: Token returned by the preceding page, if any.
            refresh: Reload the chat catalog instead of using the short-lived cache.
        """
        return teams.list_chats(
            page_size=page_size,
            page_token=page_token,
            refresh=refresh,
        )

    @server.tool(annotations=READ_ONLY)
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
    def get_messages(
        conversation_id: str,
        page_size: int = 25,
        since: str | None = None,
        until: str | None = None,
        cursor: str | None = None,
    ) -> GetMessagesResult:
        """Read one bounded history page, optionally within an inclusive date range.

        Results are ordered oldest to newest within each page. The continuation
        cursor fetches older history. Date values must be ISO-8601 timestamps
        with a timezone; pass the same bounds when continuing.

        Args:
            conversation_id: Stable Teams conversation ID from list_chats/search results.
            page_size: Number of messages requested (1–50). Each message body is capped at 1,200 characters and has a truncation flag.
            since: Inclusive lower timestamp, or null for the oldest available history.
            until: Inclusive upper timestamp, or null for no upper bound.
            cursor: Continuation returned by the previous page.
        """
        return teams.get_messages(
            conversation_id,
            page_size=page_size,
            since=_parse_datetime(since, "since"),
            until=_parse_datetime(until, "until"),
            cursor=cursor,
        )

    @server.tool(annotations=READ_ONLY)
    def search_messages(
        query: str,
        match_mode: Literal["phrase", "all_terms"] = "phrase",
        conversation_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        result_limit: int = 20,
        cursor: str | None = None,
    ) -> SearchMessagesResult:
        """Search message contents in the local SQLite FTS5 archive.

        No verified Teams server-side content-search route is used. Results are
        limited to locally indexed messages. The coverage object says whether the
        requested scope is complete; use get_search_index_status with the same
        date and conversation filters to inspect per-chat coverage.

        Args:
            query: Text to find in formatted message bodies.
            match_mode: 'phrase' for the exact token phrase or 'all_terms' for every query word.
            conversation_id: Restrict the scan to one known conversation, if desired.
            since: Inclusive lower timestamp, or null for all available history.
            until: Inclusive upper timestamp, or null for no upper bound.
            result_limit: Maximum matches to return in this call (1–50).
            cursor: Continue paging through matches for the same query and scope.
        """
        return teams.search_messages(
            query,
            match_mode=match_mode,
            conversation_id=conversation_id,
            since=_parse_datetime(since, "since"),
            until=_parse_datetime(until, "until"),
            result_limit=result_limit,
            cursor=cursor,
        )

    @server.tool(annotations=READ_ONLY)
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
        retaining older messages. This tool reads Teams but does not change state.

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

    return server


def main() -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    create_mcp_server().run(transport="stdio")


if __name__ == "__main__":
    main()
