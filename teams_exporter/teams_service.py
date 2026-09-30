from __future__ import annotations

import hashlib
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from secrets import token_urlsafe
from typing import Any

from teams_exporter.teams_connector import (
    AuthenticationRequiredError,
    ChatSummary,
    TeamsConnector,
)
from teams_exporter.teams_message_formatter import TeamsMessageFormatter
from teams_exporter.teams_search_index import TeamsSearchIndex


MAX_LIST_PAGE_SIZE = 100
MAX_HISTORY_PAGE_SIZE = 50
MAX_MESSAGE_CONTENT_CHARS = 1_200
MAX_SEARCH_RESULTS = 50
MAX_SEARCH_CHATS = 100
MAX_SYNC_MESSAGES = 5_000
MAX_SYNC_PAGES = 100
MAX_SYNC_CHATS = 100
CURSOR_TTL_SECONDS = 30 * 60
MAX_ACTIVE_CURSORS = 256


class InvalidCursorError(ValueError):
    """Raised when a continuation token is unknown, expired, or mismatched."""


@dataclass(slots=True)
class _Cursor:
    kind: str
    scope: tuple[str, ...]
    state: dict[str, Any]
    expires_at: float


class TeamsService:
    """Reusable read-only Teams operations shared by MCP and future interfaces."""

    def __init__(
        self,
        connector: TeamsConnector | None = None,
        formatter: TeamsMessageFormatter | None = None,
        index: TeamsSearchIndex | None = None,
    ) -> None:
        self.connector = connector or TeamsConnector()
        self.formatter = formatter or TeamsMessageFormatter()
        configured_index = os.environ.get("TEAMS_SEARCH_INDEX_PATH")
        self._use_base_index_if_missing = bool(configured_index)
        if configured_index:
            index_path = Path(configured_index)
        elif os.name == "nt" and os.environ.get("LOCALAPPDATA"):
            index_path = Path(os.environ["LOCALAPPDATA"]) / "scrape-teams-chat" / "search-index.sqlite3"
        else:
            index_path = Path.home() / ".local" / "share" / "scrape-teams-chat" / "search-index.sqlite3"
        self._index_path = index_path
        self._index = index
        self._index_is_injected = index is not None
        self._selected_fingerprint: str | None = None
        self.database_notice: str | None = None
        self.account_info: dict[str, str] | None = None
        self._cursors: dict[str, _Cursor] = {}
        self._chats_cache: list[ChatSummary] | None = None
        self._chats_cache_until = 0.0

    @property
    def index(self) -> TeamsSearchIndex:
        """Return this account's durable archive, selecting it from cached auth."""
        if self._index is None:
            get_cached_identity = getattr(self.connector, "get_cached_account_metadata", None)
            metadata = get_cached_identity() if callable(get_cached_identity) else None
            if not metadata:
                raise AuthenticationRequiredError(
                    "Teams sign-in is required before this account's local archive can be opened. "
                    "Run `python -m teams_exporter auth` and retry."
                )
            self._select_index_for_account(metadata)
        return self._index

    def _ensure_session(self) -> None:
        # Only the separate `python -m teams_exporter auth` command is interactive.
        self.connector.login(interactive=False)
        if not self._index_is_injected:
            self._select_index_for_account(self.connector.get_user_metadata())

    def _select_index_for_account(self, metadata: dict[str, Any]) -> None:
        if self._index_is_injected:
            return
        email = metadata.get("username")
        display_name = metadata.get("name") or email or "Teams account"
        self.account_info = {
            "name": str(display_name),
            "email": str(email) if email else "Email unavailable",
        }
        get_picture_uri = getattr(self.connector, "get_profile_picture_uri", None)
        if callable(get_picture_uri):
            picture_uri = get_picture_uri(display_name=str(display_name))
            if isinstance(picture_uri, str) and picture_uri.startswith("https://"):
                self.account_info["picture_url"] = picture_uri
        tenant_id = metadata.get("tenant_id")
        user_id = metadata.get("user_id")
        home_account_id = metadata.get("home_account_id")
        if tenant_id and user_id:
            identity_key = f"{tenant_id}:{user_id}"
        elif home_account_id:
            identity_key = f"{tenant_id or ''}:{home_account_id}"
        else:
            raise AuthenticationRequiredError(
                "The signed-in Teams account does not expose a stable account identifier. "
                "The local archive was left unopened."
            )
        fingerprint = hashlib.sha256(identity_key.encode("utf-8")).hexdigest()
        if fingerprint == self._selected_fingerprint:
            return
        if self._selected_fingerprint is not None:
            self._chats_cache = None
            self._chats_cache_until = 0.0
            self._cursors.clear()
        path, self.database_notice = TeamsSearchIndex.resolve_account_path(
            self._index_path,
            fingerprint,
            use_base_if_missing=self._use_base_index_if_missing,
        )
        self._index = TeamsSearchIndex(path, owner_fingerprint=fingerprint)
        self._selected_fingerprint = fingerprint

    def _get_chats(self, *, refresh: bool = False) -> list[ChatSummary]:
        if refresh or self._chats_cache is None or time.monotonic() >= self._chats_cache_until:
            self._chats_cache = self.connector.list_chats()
            self.index.upsert_chats(self._chats_cache)
            self._chats_cache_until = time.monotonic() + 60
        return self._chats_cache

    def list_chats(
        self,
        *,
        page_size: int = 50,
        page_token: str | None = None,
        refresh: bool = False,
        include_avatar_uris: bool = False,
    ) -> dict[str, Any]:
        """List the activity-ordered live chat catalog in bounded pages."""
        self._validate_int("page_size", page_size, 1, MAX_LIST_PAGE_SIZE)
        if not isinstance(include_avatar_uris, bool):
            raise ValueError("include_avatar_uris must be a boolean.")
        self._ensure_session()
        chats = self._get_chats(refresh=refresh)
        offset = self._decode_offset(page_token)
        page = chats[offset : offset + page_size]
        next_offset = offset + len(page)
        items = [self._chat_record(chat) for chat in page]
        if include_avatar_uris:
            for item, chat in zip(items, page):
                item["avatar_url"] = chat.avatar_url
        return {
            "items": items,
            "next_page_token": f"offset:{next_offset}" if next_offset < len(chats) else None,
            "database_notice": self.database_notice,
            "coverage": {
                "catalog_complete": True,
                "total_chats": len(chats),
                "returned_this_page": len(page),
            },
        }

    def search_chats(
        self,
        participant_query: str,
        *,
        page_size: int = 25,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Search indexed participant names and report roster-index coverage."""
        query = self._validate_query(participant_query, "participant_query")
        self._validate_int("page_size", page_size, 1, MAX_SEARCH_RESULTS)
        scope = (query.casefold(),)
        generation = self.index.counts()["generation"]
        if cursor:
            state = self._read_cursor(cursor, "search_chats", scope)
            if state["generation"] != generation:
                raise InvalidCursorError("The participant index changed; restart the search.")
            offset = state["offset"]
        else:
            offset = 0

        items, total = self.index.search_chats_by_participant(
            query, limit=page_size, offset=offset
        )
        coverage = self.index.participant_coverage()
        complete = coverage["catalog_chats"] == coverage["complete_rosters"]
        next_offset = offset + len(items)
        next_cursor = None
        if next_offset < total:
            next_cursor = self._store_cursor(
                "search_chats",
                scope,
                {"offset": next_offset, "generation": generation},
                old=cursor,
            )
        elif cursor:
            self._cursors.pop(cursor, None)
        return {
            "items": items,
            "total_matches_in_index": total,
            "next_cursor": next_cursor,
            "database_notice": self.database_notice,
            "coverage": {
                "method": "local SQLite participant index",
                "server_side_search_used": False,
                "catalog_chats": coverage["catalog_chats"],
                "rosters_indexed": coverage["complete_rosters"],
                "rosters_synced": coverage["synced_rosters"],
                "rosters_not_indexed": max(
                    0, coverage["catalog_chats"] - coverage["synced_rosters"]
                ),
                "rosters_with_unresolved_names": coverage["rosters_with_unresolved_names"],
                "unresolved_participants": coverage["unresolved_participants"],
                "complete_for_chat_catalog": complete and coverage["catalog_chats"] > 0,
                "index_generation": generation,
                "sync_tool": "sync_search_index",
            },
        }

    def get_messages(
        self,
        conversation_id: str,
        *,
        page_size: int = 25,
        since: datetime | None = None,
        until: datetime | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Return one bounded chronological page and an in-process continuation."""
        self._validate_query(conversation_id, "conversation_id")
        self._validate_int("page_size", page_size, 1, MAX_HISTORY_PAGE_SIZE)
        since_utc = self._normalize_bound("since", since)
        until_utc = self._normalize_bound("until", until)
        if since_utc and until_utc and since_utc > until_utc:
            raise ValueError("since must be earlier than or equal to until.")
        scope = (conversation_id, self._bound_key(since_utc), self._bound_key(until_utc))
        self._ensure_session()

        if cursor:
            state = self._read_cursor(cursor, "get_messages", scope)
        else:
            state = {
                "continuation_url": None,
                "seen_message_ids": set(),
                "raw_items_scanned": 0,
                "missing_timestamp_items": 0,
                "seen_continuation_urls": set(),
            }

        request_url = state["continuation_url"]
        if request_url:
            if request_url in state["seen_continuation_urls"]:
                raise RuntimeError("Teams repeated a message continuation link.")
            state["seen_continuation_urls"].add(request_url)
        messages, next_url, raw_scanned, missing_timestamps = self.connector.get_messages_page(
            conversation_id,
            page_size=page_size,
            since=since_utc,
            until=until_utc,
            continuation_url=request_url,
        )
        if next_url and next_url in state["seen_continuation_urls"]:
            raise RuntimeError("Teams repeated a message continuation link.")
        state["continuation_url"] = next_url
        state["raw_items_scanned"] += raw_scanned
        state["missing_timestamp_items"] += missing_timestamps
        seen: set[str] = state["seen_message_ids"]
        unique: list[dict[str, Any]] = []
        for message in messages:
            message_id = message.get("id")
            if message_id is not None:
                message_id = str(message_id)
                if message_id in seen:
                    continue
                seen.add(message_id)
            unique.append(message)

        complete = next_url is None
        next_cursor = None
        if not complete:
            next_cursor = self._store_cursor("get_messages", scope, state, old=cursor)
        elif cursor:
            self._cursors.pop(cursor, None)

        chats = self._get_chats()
        chat = next((item for item in chats if item.id == conversation_id), None)
        return {
            "conversation": {
                "id": conversation_id,
                "name": chat.display_name if chat else conversation_id,
            },
            "items": [self._message_record(conversation_id, message) for message in unique],
            "next_cursor": next_cursor,
            "database_notice": self.database_notice,
            "coverage": {
                "complete": complete,
                "page_size": page_size,
                "raw_items_scanned_total": state["raw_items_scanned"],
                "missing_timestamp_items_excluded_total": state["missing_timestamp_items"],
                "since": self._bound_key(since_utc) or None,
                "until": self._bound_key(until_utc) or None,
                "ordering": "oldest to newest within each returned page",
                "continuation_direction": "older messages",
            },
        }

    def search_messages(
        self,
        query: str,
        *,
        match_mode: str = "phrase",
        conversation_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        result_limit: int = 20,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Search the local FTS5 archive with explicit index coverage."""
        query = self._validate_query(query, "query")
        if match_mode not in {"phrase", "all_terms"}:
            raise ValueError("match_mode must be 'phrase' or 'all_terms'.")
        self._validate_int("result_limit", result_limit, 1, MAX_SEARCH_RESULTS)
        since_utc = self._normalize_bound("since", since)
        until_utc = self._normalize_bound("until", until)
        if since_utc and until_utc and since_utc > until_utc:
            raise ValueError("since must be earlier than or equal to until.")
        if conversation_id is not None:
            self._validate_query(conversation_id, "conversation_id")
        since_key = self._bound_key(since_utc) or None
        until_key = self._bound_key(until_utc) or None
        scope = (
            query.casefold(),
            match_mode,
            conversation_id or "*",
            since_key or "",
            until_key or "",
        )
        generation = self.index.counts()["generation"]
        if cursor:
            state = self._read_cursor(cursor, "search_messages", scope)
            if state["generation"] != generation:
                raise InvalidCursorError("The search index changed; restart the search.")
            offset = state["offset"]
        else:
            offset = 0

        rows, total = self.index.search_messages(
            query,
            match_mode=match_mode,
            conversation_id=conversation_id,
            since=since_key,
            until=until_key,
            limit=result_limit,
            offset=offset,
        )
        items = [self._indexed_message_record(row, query) for row in rows]
        next_offset = offset + len(items)
        next_cursor = None
        if next_offset < total:
            next_cursor = self._store_cursor(
                "search_messages",
                scope,
                {"offset": next_offset, "generation": generation},
                old=cursor,
            )
        elif cursor:
            self._cursors.pop(cursor, None)

        coverage = self.index.coverage_summary(
            since=since_key,
            until=until_key,
            conversation_id=conversation_id,
            limit=0,
        )
        counts = self.index.counts()
        return {
            "items": items,
            "total_matches_in_index": total,
            "next_cursor": next_cursor,
            "database_notice": self.database_notice,
            "coverage": {
                "method": "local SQLite FTS5 index",
                "server_side_search_used": False,
                "complete_for_requested_scope": (
                    coverage["complete_for_requested_scope"]
                    and coverage["catalog_chats"] > 0
                ),
                "catalog_chats": coverage["catalog_chats"],
                "covered_chats": coverage["covered_chats"],
                "uncovered_chats": coverage["uncovered_chats"],
                "requested_since": since_key,
                "requested_until": until_key,
                "conversation_id": conversation_id,
                "indexed_messages": counts["indexed_messages"],
                "index_generation": generation,
                "chat_coverage_tool": "get_search_index_status",
            },
        }

    def sync_search_index(
        self,
        *,
        conversation_id: str | None = None,
        max_chats_to_sync: int = 25,
        max_messages_to_fetch: int = 1_000,
        max_pages_to_fetch: int = 20,
        include_participants: bool = True,
        refresh_latest: bool = True,
    ) -> dict[str, Any]:
        """Fetch bounded batches into the durable message and participant index.

        Incomplete histories continue from their persisted Teams continuation URL.
        Complete histories refresh their newest page and retain older indexed data.
        """
        self._validate_int("max_chats_to_sync", max_chats_to_sync, 1, MAX_SYNC_CHATS)
        self._validate_int("max_messages_to_fetch", max_messages_to_fetch, 1, MAX_SYNC_MESSAGES)
        self._validate_int("max_pages_to_fetch", max_pages_to_fetch, 1, MAX_SYNC_PAGES)
        if conversation_id is not None:
            self._validate_query(conversation_id, "conversation_id")
        self._ensure_session()
        chats = self._get_chats(refresh=True)
        if conversation_id:
            chats = [chat for chat in chats if chat.id == conversation_id]
            if not chats:
                raise ValueError("conversation_id was not found in the current chat catalog.")
        else:
            attempt_times = self.index.sync_attempt_times()
            chats = sorted(
                chats,
                key=lambda chat: (attempt_times.get(chat.id) or "", chat.id),
            )
        chats = chats[:max_chats_to_sync]

        results: list[dict[str, Any]] = []
        message_budget = max_messages_to_fetch
        page_budget = max_pages_to_fetch
        for chat in chats:
            state = self.index.get_state(chat.id)
            participant_result: dict[str, Any] = {
                "attempted": False,
                "complete": bool(state["participant_sync_complete"]),
                "participant_count": int(state["participants_scanned"]),
                "named_participant_count": max(
                    0,
                    int(state["participants_scanned"])
                    - int(state["unresolved_participants"]),
                ),
                "unresolved_participants": int(state["unresolved_participants"]),
                "error": state["participant_sync_error"],
            }
            if include_participants and (
                not state["participant_sync_complete"] or state["unresolved_participants"] > 0
            ):
                participant_result["attempted"] = True
                try:
                    members = self.connector.get_conversation_members(chat.id)
                    participants = [self._participant_record(member) for member in members]
                    unresolved = sum(not person["display_name"] for person in participants)
                    self.index.save_participants(
                        chat.id,
                        participants,
                        scanned_count=len(members),
                        unresolved_count=unresolved,
                    )
                    participant_result.update(
                        {
                            "complete": True,
                            "participant_count": len(participants),
                            "named_participant_count": sum(
                                bool(person["display_name"]) for person in participants
                            ),
                            "unresolved_participants": unresolved,
                            "error": None,
                        }
                    )
                except AuthenticationRequiredError:
                    raise
                except (RuntimeError, ValueError) as exc:
                    safe_error = self._safe_error(exc)
                    self.index.save_participants(chat.id, [], error=safe_error)
                    participant_result.update({"complete": False, "error": safe_error})

            state = self.index.get_state(chat.id)
            history_complete = bool(state["message_history_complete"])
            message_pages = 0
            message_items = 0
            message_error = None
            if page_budget > 0 and message_budget > 0 and (not history_complete or refresh_latest):
                is_latest_refresh = history_complete
                continuation_url = None if is_latest_refresh or state["messages_scanned"] == 0 else state[
                    "messages_next_url"
                ]
                seen_links: set[str] = set()
                while page_budget > 0 and message_budget > 0:
                    if continuation_url and continuation_url in seen_links:
                        message_error = "Repeated continuation link."
                        break
                    if continuation_url:
                        seen_links.add(continuation_url)
                    request_size = min(MAX_HISTORY_PAGE_SIZE, message_budget)
                    try:
                        page, next_url, raw_scanned, missing_timestamps = (
                            self.connector.get_messages_page(
                                chat.id,
                                page_size=request_size,
                                continuation_url=continuation_url,
                            )
                        )
                    except AuthenticationRequiredError:
                        raise
                    except (RuntimeError, ValueError) as exc:
                        message_error = self._safe_error(exc)
                        break
                    saved_next_url = None if is_latest_refresh else next_url
                    new_state = self.index.save_message_page(
                        chat.id,
                        page,
                        next_url=saved_next_url,
                        raw_scanned=raw_scanned,
                        missing_timestamp_count=missing_timestamps,
                        formatter=self.formatter,
                        history_complete=(
                            True if is_latest_refresh else next_url is None
                        ),
                    )
                    message_pages += 1
                    message_items += int(new_state["messages_added_this_page"])
                    message_budget -= min(raw_scanned, message_budget)
                    page_budget -= 1
                    history_complete = bool(new_state["message_history_complete"])
                    if is_latest_refresh or next_url is None:
                        break
                    continuation_url = next_url

            final_state = self.index.get_state(chat.id)
            self.index.touch_sync_attempt(chat.id)
            results.append(
                {
                    "chat_id": chat.id,
                    "chat_name": chat.display_name,
                    "messages_added_this_call": message_items,
                    "message_pages_fetched": message_pages,
                    "message_history_complete": bool(final_state["message_history_complete"]),
                    "messages_stored_total": final_state["messages_stored"],
                    "oldest_indexed_message_at": final_state["oldest_message_at"],
                    "newest_indexed_message_at": final_state["newest_message_at"],
                    "participant_roster_complete": bool(final_state["participant_sync_complete"]),
                    "unresolved_participants": int(final_state["unresolved_participants"]),
                    "participant_result": participant_result,
                    "message_error": message_error,
                }
            )

        return {
            "items": results,
            "database_notice": self.database_notice,
            "coverage": {
                "chats_attempted": len(results),
                "messages_remaining_this_call": message_budget,
                "pages_remaining_this_call": page_budget,
                "index": self.index.counts(),
                "continuation_persisted": True,
                "messages_retained_after_fetch": True,
            },
        }

    def get_search_index_status(
        self,
        *,
        page_size: int = 50,
        page_token: str | None = None,
        conversation_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> dict[str, Any]:
        """Page through per-chat sync and requested-range coverage status."""
        self._validate_int("page_size", page_size, 1, MAX_LIST_PAGE_SIZE)
        if conversation_id is not None:
            self._validate_query(conversation_id, "conversation_id")
        since_key = self._bound_key(self._normalize_bound("since", since)) or None
        until_key = self._bound_key(self._normalize_bound("until", until)) or None
        if since_key and until_key and since_key > until_key:
            raise ValueError("since must be earlier than or equal to until.")
        offset = self._decode_offset(page_token)
        rows, total, generation = self.index.index_status(
            limit=page_size,
            offset=offset,
            conversation_id=conversation_id,
        )
        coverage = self.index.coverage_summary(
            since=since_key,
            until=until_key,
            conversation_id=conversation_id,
            limit=page_size,
            offset=offset,
        )
        coverage_by_id = {record["chat_id"]: record for record in coverage["items"]}
        items = []
        for row in rows:
            row.pop("messages_next_url", None)
            chat_id = row["conversation_id"]
            items.append(
                {
                    "chat_id": chat_id,
                    "chat_name": row["display_name"],
                    "catalog_last_message_at": row["last_message_at"],
                    "catalog_refreshed_at": row["catalogued_at"],
                    "message_history_complete": bool(row["message_history_complete"] or 0),
                    "messages_stored": int(row["messages_stored"] or 0),
                    "messages_scanned": int(row["messages_scanned"] or 0),
                    "message_pages": int(row["message_pages"] or 0),
                    "oldest_message_at": row["oldest_message_at"],
                    "newest_message_at": row["newest_message_at"],
                    "messages_indexed_at": row["messages_indexed_at"],
                    "missing_timestamp_messages": int(row["missing_timestamp_messages"] or 0),
                    "participant_sync_complete": bool(row["participant_sync_complete"] or 0),
                    "participants_scanned": int(row["participants_scanned"] or 0),
                    "unresolved_participants": int(row["unresolved_participants"] or 0),
                    "participant_sync_error": row["participant_sync_error"],
                    "participants_indexed_at": row["participants_indexed_at"],
                    "sync_attempted_at": row["sync_attempted_at"],
                    "requested_range_covered": coverage_by_id.get(chat_id, {}).get(
                        "requested_range_covered", False
                    ),
                }
            )
        next_offset = offset + len(rows)
        return {
            "items": items,
            "next_page_token": f"offset:{next_offset}" if next_offset < total else None,
            "database_notice": self.database_notice,
            "coverage": {
                **{key: value for key, value in coverage.items() if key != "items"},
                "complete_for_requested_scope": (
                    coverage["complete_for_requested_scope"] and coverage["catalog_chats"] > 0
                ),
                "index_generation": generation,
                "requested_since": since_key,
                "requested_until": until_key,
                "conversation_id": conversation_id,
                "index_totals": self.index.counts(),
            },
        }

    def _message_record(
        self,
        conversation_id: str,
        message: dict[str, Any],
    ) -> dict[str, Any]:
        body = self.formatter.format_message(message)
        timestamp = self.connector._message_datetime(message)
        return {
            "id": str(message["id"]) if message.get("id") is not None else None,
            "conversation_id": conversation_id,
            "author": self._message_author(message, body),
            "timestamp": self._timestamp(timestamp),
            "content": body[:MAX_MESSAGE_CONTENT_CHARS],
            "content_truncated": len(body) > MAX_MESSAGE_CONTENT_CHARS,
            "content_char_count": len(body),
            "message_type": self._first_string(message, "messagetype", "messageType"),
        }

    def _indexed_message_record(self, row: dict[str, Any], query: str) -> dict[str, Any]:
        return {
            "id": row["message_id"],
            "conversation_id": row["conversation_id"],
            "chat_name": row["display_name"],
            "author": row["author"],
            "timestamp": row["sent_at"],
            "excerpt": self._excerpt(row["body"], query),
            "message_type": row["message_type"],
        }

    @staticmethod
    def _participant_record(member: dict[str, Any]) -> dict[str, str | None]:
        return {
            "id": TeamsService._first_string(
                member, "id", "mri", "objectId", "userId", "userMri"
            ),
            "display_name": TeamsService._first_string(
                member, "userDisplayName", "displayName", "name", "friendlyName"
            ),
        }

    @staticmethod
    def _chat_record(chat: ChatSummary) -> dict[str, Any]:
        return {
            "id": chat.id,
            "name": chat.display_name,
            "chat_type": chat.chat_type,
            "last_message_at": TeamsService._timestamp(chat.last_message_at),
            "last_message_preview": chat.last_message_preview,
            "unread": chat.unread,
        }

    @staticmethod
    def _message_author(message: dict[str, Any], body: str) -> str:
        for key in (
            "imdisplayname",
            "imDisplayName",
            "fromDisplayNameInToken",
            "displayName",
            "author",
        ):
            value = message.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return "System" if body.lstrip().startswith(("<", "{")) else "Unknown sender"

    @staticmethod
    def _first_string(data: dict[str, Any], *keys: str) -> str | None:
        for key in keys:
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    @staticmethod
    def _excerpt(text: str, query: str | None, max_chars: int = 600) -> str:
        compact = re.sub(r"\s+", " ", text).strip()
        if len(compact) <= max_chars:
            return compact
        position = compact.casefold().find((query or "").casefold())
        if position < 0:
            position = 0
        start = max(0, position - max_chars // 4)
        end = min(len(compact), start + max_chars)
        return f"{'…' if start else ''}{compact[start:end]}{'…' if end < len(compact) else ''}"

    @staticmethod
    def _timestamp(value: datetime | None) -> str | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _normalize_bound(name: str, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise TypeError(f"{name} must be a datetime or None.")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{name} must include a timezone.")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _bound_key(value: datetime | None) -> str:
        return TeamsService._timestamp(value) or ""

    @staticmethod
    def _validate_int(name: str, value: int, minimum: int, maximum: int) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise ValueError(f"{name} must be between {minimum} and {maximum}.")

    @staticmethod
    def _validate_query(value: str, name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-empty string.")
        return value.strip()

    @staticmethod
    def _decode_offset(page_token: str | None) -> int:
        if page_token is None:
            return 0
        match = re.fullmatch(r"offset:(\d+)", page_token)
        if not match:
            raise InvalidCursorError("Invalid page token.")
        return int(match.group(1))

    def _read_cursor(self, token: str, kind: str, scope: tuple[str, ...]) -> dict[str, Any]:
        self._prune_cursors()
        entry = self._cursors.get(token)
        if entry is None or entry.expires_at <= time.monotonic():
            raise InvalidCursorError("Continuation cursor is missing or expired; restart the query.")
        if entry.kind != kind or entry.scope != scope:
            raise InvalidCursorError("Continuation cursor does not match this request.")
        return entry.state

    def _store_cursor(
        self,
        kind: str,
        scope: tuple[str, ...],
        state: dict[str, Any],
        *,
        old: str | None,
    ) -> str:
        if old:
            self._cursors.pop(old, None)
        self._prune_cursors()
        while len(self._cursors) >= MAX_ACTIVE_CURSORS:
            self._cursors.pop(next(iter(self._cursors)))
        token = token_urlsafe(24)
        self._cursors[token] = _Cursor(
            kind=kind,
            scope=scope,
            state=state,
            expires_at=time.monotonic() + CURSOR_TTL_SECONDS,
        )
        return token

    def _prune_cursors(self) -> None:
        now = time.monotonic()
        expired = [token for token, entry in self._cursors.items() if entry.expires_at <= now]
        for token in expired:
            self._cursors.pop(token, None)

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        # Do not pass through request URLs or any backend diagnostic that might
        # include opaque continuation values or authentication material.
        return f"{exc.__class__.__name__}: request failed"
