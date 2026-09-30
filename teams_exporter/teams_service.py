from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import time
import uuid
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
MAX_SEND_MESSAGE_CHARS = 10_000
MAX_USER_CONFIRMATION_CHARS = 1_000
MAX_SEARCH_RESULTS = 50
MAX_SEARCH_CHATS = 100
MAX_SYNC_MESSAGES = 5_000
MAX_SYNC_PAGES = 100
MAX_SYNC_CHATS = 100
MAX_SEARCH_PAGES_PER_CALL = 100
DEFAULT_MAX_SEARCH_MATCHES = 1_000
MAX_EXPORT_PAGES_PER_CALL = 100
EXPORT_RESOURCE_PAGE_BYTES = 50_000
MAX_EXPORT_RESOURCE_BYTES = 100_000
MAX_MESSAGE_CONTENT_PAGE_CHARS = 50_000
DEFAULT_MESSAGE_RESPONSE_CHARS = 30_000
MAX_MESSAGE_RESPONSE_CHARS = 100_000
AUTH_CHECK_TTL_SECONDS = 30
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
    """Reusable Teams operations shared by MCP and future interfaces."""

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
        self._auth_check_lock = threading.RLock()
        self._auth_checked_until = 0.0

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
        with self._auth_check_lock:
            now = time.monotonic()
            active_session = getattr(self.connector, "has_active_session", None)
            if callable(active_session):
                active_session = active_session()
            if now < self._auth_checked_until and active_session is not False:
                return
            try:
                self.connector.login(interactive=False)
                if not self._index_is_injected:
                    self._select_index_for_account(self.connector.get_user_metadata())
            except AuthenticationRequiredError:
                self._auth_checked_until = 0.0
                invalidate = getattr(self.connector, "invalidate_session", None)
                if callable(invalidate):
                    invalidate()
                raise
            self._auth_checked_until = time.monotonic() + AUTH_CHECK_TTL_SECONDS

    def invalidate_authentication(self) -> None:
        """Force the next app request to revalidate Teams credentials."""
        with self._auth_check_lock:
            self._auth_checked_until = 0.0

    @property
    def has_recent_connection(self) -> bool:
        """Whether a recent check confirmed that the connector still has a session."""
        if time.monotonic() >= self._auth_checked_until:
            return False
        active_session = getattr(self.connector, "has_active_session", None)
        if callable(active_session):
            active_session = active_session()
        return active_session is not False

    def check_authentication(self) -> dict[str, Any]:
        """Confirm the saved Teams session without loading the chat catalog."""
        self._ensure_session()
        return {"authenticated": True}

    def get_profile_picture(self, url: str) -> tuple[bytes, str]:
        """Fetch one picture through the authenticated connector without caching it."""
        self._ensure_session()
        fetch_picture = getattr(self.connector, "get_profile_picture", None)
        if not callable(fetch_picture):
            raise RuntimeError("The configured Teams connector cannot fetch profile pictures.")
        return fetch_picture(url)

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
            sync_updates = getattr(self.connector, "get_chat_updates", None)
            if callable(sync_updates):
                update = sync_updates(self.index.get_chat_sync_token())
                chats = update.get("chats")
                sync_token = update.get("sync_token")
                if not isinstance(chats, list):
                    raise RuntimeError("Teams returned an invalid chat update list.")
                deleted_ids = update.get("deleted_ids", [])
                if not isinstance(deleted_ids, list):
                    raise RuntimeError("Teams returned an invalid deleted chat list.")
                self.index.upsert_chat_updates(
                    chats,
                    sync_token,
                    deleted_ids=deleted_ids,
                )
                self._chats_cache = [
                    self._chat_from_record(record)
                    for record in self.index.chat_catalog()
                ]
            else:
                self._chats_cache = self.connector.list_chats()
                self.index.upsert_chats(self._chats_cache)
            self._chats_cache_until = time.monotonic() + 60
        return self._chats_cache

    def list_cached_chats(
        self,
        *,
        page_size: int = MAX_LIST_PAGE_SIZE,
        page_token: str | None = None,
        include_avatar_uris: bool = False,
    ) -> dict[str, Any]:
        """Read the durable SQLite catalog without calling Teams."""
        self._validate_int("page_size", page_size, 1, MAX_LIST_PAGE_SIZE)
        if not isinstance(include_avatar_uris, bool):
            raise ValueError("include_avatar_uris must be a boolean.")
        self._ensure_session()
        chats = [self._chat_from_record(record) for record in self.index.chat_catalog()]
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
                **self._catalog_coverage_record(),
                "total_chats": len(chats),
                "returned_this_page": len(page),
                "source": "local SQLite cache",
            },
        }

    def _catalog_coverage_record(self) -> dict[str, Any]:
        coverage = getattr(self.connector, "catalog_coverage", None)
        if getattr(coverage, "catalog_source", None) == "not loaded":
            coverage = None
        defaults = {
            "catalog_complete": True,
            "catalog_source": "connector catalog",
            "catalog_scope": "Chats returned by the configured Teams connector.",
            "csa_reported_partial": None,
            "chat_service_unique_rows": None,
            "fallback_chats_added": 0,
            "non_chat_service_rows_excluded": 0,
            "unclassified_service_rows_excluded": 0,
            "fallback_name_lookups_attempted": 0,
            "fallback_chats_without_resolved_name": 0,
        }
        return {
            key: getattr(coverage, key, default) if coverage is not None else default
            for key, default in defaults.items()
        }

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
                **self._catalog_coverage_record(),
                "total_chats": len(chats),
                "returned_this_page": len(page),
            },
        }

    def send_message(
        self,
        conversation_id: str,
        expected_chat_name: str,
        text: str,
        *,
        user_confirmation: str,
    ) -> dict[str, Any]:
        """Send one message after the caller has recorded explicit user consent.

        ``user_confirmation`` is required as a safeguard against accidental
        calls, but its presence alone is not authorization. MCP instructions
        require the caller to obtain consent for this recipient and content.
        The value is neither logged nor stored.
        """
        if not isinstance(conversation_id, str) or not conversation_id.strip():
            raise ValueError("conversation_id must be a non-empty string.")
        if not isinstance(expected_chat_name, str) or not expected_chat_name.strip():
            raise ValueError("expected_chat_name must be a non-empty string.")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text must be a non-empty string.")
        if len(text) > MAX_SEND_MESSAGE_CHARS:
            raise ValueError(
                f"text must contain at most {MAX_SEND_MESSAGE_CHARS:,} characters."
            )
        if "\x00" in text:
            raise ValueError("text cannot contain a null character.")
        if (
            not isinstance(user_confirmation, str)
            or not user_confirmation.strip()
            or len(user_confirmation) > MAX_USER_CONFIRMATION_CHARS
        ):
            raise ValueError(
                "user_confirmation must contain the user's explicit send authorization."
            )

        self._ensure_session()
        chat_id = conversation_id.strip()
        chat = next(
            (
                candidate
                for candidate in self._get_chats(refresh=True)
                if candidate.id == chat_id
            ),
            None,
        )
        if chat is None:
            raise ValueError("conversation_id was not found in the current Teams chat catalog.")
        actual_name = " ".join(chat.display_name.split()).casefold()
        expected_name = " ".join(expected_chat_name.split()).casefold()
        if actual_name != expected_name:
            raise ValueError(
                "expected_chat_name does not match the current Teams chat catalog entry."
            )

        send = getattr(self.connector, "send_message", None)
        if not callable(send):
            raise RuntimeError("This Teams connector does not support message sending.")
        result = send(chat_id, text)
        if not isinstance(result, dict):
            raise RuntimeError("Teams did not return a valid message-send result.")
        return {
            "conversation_id": chat.id,
            "chat_name": chat.display_name,
            "message_id": result.get("message_id"),
            "client_message_id": result.get("client_message_id"),
            "sent_at": result.get("sent_at"),
            "submitted_characters": len(text),
            "delivery_status": "accepted",
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
        for item in items:
            item["is_self_chat"] = (
                item.get("id") == TeamsConnector.SELF_CHAT_ID
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
        max_response_chars: int = DEFAULT_MESSAGE_RESPONSE_CHARS,
    ) -> dict[str, Any]:
        """Fetch one remote page, persist it, and page the merged local archive."""
        self._validate_query(conversation_id, "conversation_id")
        self._validate_int("page_size", page_size, 1, MAX_HISTORY_PAGE_SIZE)
        self._validate_int(
            "max_response_chars",
            max_response_chars,
            4_096,
            MAX_MESSAGE_RESPONSE_CHARS,
        )
        effective_page_size = min(
            page_size,
            max(1, max_response_chars // 1_500),
        )
        since_utc = self._normalize_bound("since", since)
        until_utc = self._normalize_bound("until", until)
        if since_utc and until_utc and since_utc > until_utc:
            raise ValueError("since must be earlier than or equal to until.")
        scope = (conversation_id, self._bound_key(since_utc), self._bound_key(until_utc))
        self._ensure_session()
        chats = self._get_chats()
        chat = next((item for item in chats if item.id == conversation_id), None)
        if chat is None:
            raise ValueError("conversation_id was not found in the current Teams chat catalog.")

        if cursor:
            state = self._read_cursor(cursor, "get_messages", scope)
        else:
            state = {
                "continuation_url": None,
                "seen_continuation_urls": set(),
                "before": None,
                "raw_items_scanned": 0,
                "missing_timestamp_items": 0,
                "remote_pages_fetched": 0,
                "remote_complete": False,
            }

        request_url = state["continuation_url"]
        if request_url:
            if request_url in state["seen_continuation_urls"]:
                raise RuntimeError("Teams repeated a message continuation link.")
        messages, next_url, raw_scanned, missing_timestamps = self.connector.get_messages_page(
            conversation_id,
            page_size=effective_page_size,
            since=since_utc,
            until=until_utc,
            continuation_url=request_url,
        )
        if request_url:
            state["seen_continuation_urls"].add(request_url)
        self.index.upsert_messages(
            conversation_id,
            messages,
            formatter=self.formatter,
        )
        retry_count = int(getattr(self.connector, "last_request_retry_count", 0))
        if next_url and next_url in state["seen_continuation_urls"]:
            raise RuntimeError("Teams repeated a message continuation link.")
        state["continuation_url"] = next_url
        state["remote_complete"] = next_url is None
        state["remote_pages_fetched"] += 1
        state["raw_items_scanned"] += raw_scanned
        state["missing_timestamp_items"] += missing_timestamps
        stored_page = self.index.query_message_page(
            conversation_id,
            since=self._bound_key(since_utc) or None,
            until=self._bound_key(until_utc) or None,
            before=state["before"],
            limit=effective_page_size,
        )
        has_local_more = False
        if stored_page:
            last = stored_page[-1]
            has_local_more = bool(
                self.index.query_message_page(
                    conversation_id,
                    since=self._bound_key(since_utc) or None,
                    until=self._bound_key(until_utc) or None,
                    before=(last["sent_at"], last["message_id"]),
                    limit=1,
                )
            )
            state["before"] = (last["sent_at"], last["message_id"])
        has_more = next_url is not None or has_local_more
        complete = not has_more
        next_cursor = None
        if has_more:
            next_cursor = self._store_cursor("get_messages", scope, state, old=cursor)
        elif cursor:
            self._cursors.pop(cursor, None)

        account_user_id = self._current_account_user_id()
        fetched_ids = {
            str(message["id"])
            for message in messages
            if message.get("id") is not None
        }
        records = [
            self._stored_message_record(row, account_user_id=account_user_id)
            for row in reversed(stored_page)
        ]
        base_records = []
        for record in records:
            body = record["content"]
            base_records.append(
                {
                    **record,
                    "content": "",
                    "content_truncated": bool(body),
                    "content_next_offset": 0 if body else None,
                }
            )
        result = {
            "conversation": {
                "id": conversation_id,
                "name": chat.display_name if chat else conversation_id,
                "is_self_chat": (
                    chat.is_self_chat
                    if chat is not None
                    else conversation_id == TeamsConnector.SELF_CHAT_ID
                ),
            },
            "items": records,
            "next_cursor": next_cursor,
            "database_notice": self.database_notice,
            "coverage": {
                "complete": complete,
                "page_size": effective_page_size,
                "requested_page_size": page_size,
                "raw_items_scanned_total": state["raw_items_scanned"],
                "missing_timestamp_items_excluded_total": state["missing_timestamp_items"],
                "retry_count_this_call": retry_count,
                "max_response_chars": max_response_chars,
                "remote_pages_fetched_total": state["remote_pages_fetched"],
                "remote_history_complete": state["remote_complete"],
                "archive_messages_in_page": sum(
                    1 for row in stored_page if row["message_id"] not in fetched_ids
                ),
                "since": self._bound_key(since_utc) or None,
                "until": self._bound_key(until_utc) or None,
                "ordering": "oldest to newest within each returned page",
                "continuation_direction": "older messages",
            },
        }
        base_result = {**result, "items": base_records}
        base_size = len(json.dumps(base_result, ensure_ascii=False, separators=(",", ":")))
        content_budget = max(
            0,
            max_response_chars - base_size - 256 - 32 * len(records),
        )
        remaining_records = len(records)
        bodies_truncated = 0
        for record in result["items"]:
            body = str(record["content"] or "")
            allowance = content_budget // remaining_records if remaining_records else 0
            snippet = self._json_prefix_with_budget(body, allowance)
            record["content"] = snippet
            truncated = len(snippet) < len(body)
            record["content_truncated"] = truncated
            record["content_next_offset"] = len(snippet) if truncated else None
            if truncated:
                bodies_truncated += 1
            spent = max(0, len(json.dumps(snippet, ensure_ascii=False)) - 2)
            content_budget = max(0, content_budget - spent)
            remaining_records -= 1
        result["coverage"]["messages_with_truncated_content"] = bodies_truncated
        result["coverage"]["content_continuation_tool"] = (
            "get_message_content" if bodies_truncated else None
        )
        return result

    @staticmethod
    def _json_prefix_with_budget(value: str, budget: int) -> str:
        """Return a UTF-8-safe character prefix fitting serialized JSON budget."""
        if not value or budget <= 0:
            return ""
        low, high = 0, len(value)
        while low < high:
            middle = (low + high + 1) // 2
            encoded_length = len(json.dumps(value[:middle], ensure_ascii=False)) - 2
            if encoded_length <= budget:
                low = middle
            else:
                high = middle - 1
        return value[:low]

    def fetch_chat_to_archive(
        self,
        conversation_id: str,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        cancel_event=None,
        progress_callback=None,
    ) -> dict[str, Any]:
        """Fetch a requested remote range, upsert every page, and report coverage."""
        self._validate_query(conversation_id, "conversation_id")
        since_utc = self._normalize_bound("since", since)
        until_utc = self._normalize_bound("until", until)
        if since_utc and until_utc and since_utc > until_utc:
            raise ValueError("since must be earlier than or equal to until.")
        self._ensure_session()
        chat = next((item for item in self._get_chats() if item.id == conversation_id), None)
        if chat is None:
            raise ValueError("conversation_id was not found in the current Teams chat catalog.")

        def report_fetch(pages: int, messages: int, retries: int) -> None:
            if progress_callback is not None:
                progress_callback(pages, messages, retries)

        for page in self.connector.iter_message_pages(
            conversation_id,
            limit=None,
            since=since_utc,
            until=until_utc,
            cancel_event=cancel_event,
            progress_callback=report_fetch,
        ):
            if cancel_event is not None and cancel_event.is_set():
                raise RuntimeError("Teams export was cancelled before the page was archived.")
            self.index.upsert_messages(
                conversation_id,
                page,
                formatter=self.formatter,
            )

        coverage = dict(getattr(self.connector, "last_message_read_coverage", {}))
        since_key = self._bound_key(since_utc) or None
        until_key = self._bound_key(until_utc) or None
        archive_count = self.index.count_messages_in_range(
            conversation_id, since=since_key, until=until_key
        )
        return {
            "conversation_id": conversation_id,
            "requested_since": since_key,
            "requested_until": until_key,
            "archive_messages_in_range": archive_count,
            "remote_coverage": coverage,
            "database_notice": self.database_notice,
        }

    def archived_export_message_count(
        self,
        conversation_id: str,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
    ) -> tuple[int, tuple[str, str] | None]:
        if limit is not None:
            self._validate_unbounded_positive_int("limit", limit)
        since_key = self._bound_key(self._normalize_bound("since", since)) or None
        until_key = self._bound_key(self._normalize_bound("until", until)) or None
        total = self.index.count_messages_in_range(
            conversation_id, since=since_key, until=until_key
        )
        count = min(total, limit) if limit is not None else total
        cutoff = (
            self.index.latest_message_cutoff(
                conversation_id,
                since=since_key,
                until=until_key,
                limit=count,
            )
            if limit is not None and count
            else None
        )
        return count, cutoff

    def iter_archived_export_pages(
        self,
        conversation_id: str,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        from_key: tuple[str, str] | None = None,
        page_size: int = 500,
    ):
        """Yield bounded chronological message pages from the merged local archive."""
        self._validate_int("page_size", page_size, 1, 5_000)
        since_key = self._bound_key(self._normalize_bound("since", since)) or None
        until_key = self._bound_key(self._normalize_bound("until", until)) or None
        after = None
        while True:
            rows = self.index.query_messages_chronological(
                conversation_id,
                since=since_key,
                until=until_key,
                after=after,
                from_key=from_key,
                limit=page_size,
            )
            if not rows:
                return
            yield [
                {
                    "id": row["message_id"],
                    "OriginalArrivalTime": row["sent_at"],
                    "imdisplayname": row["author"] or "Unknown sender",
                    "__formatted_body": (
                        "[Message deleted]" if row["is_deleted"] else row["body"]
                    ),
                }
                for row in rows
            ]
            last = rows[-1]
            after = (last["sent_at"], last["message_id"])

    def get_message_content(
        self,
        conversation_id: str,
        message_id: str,
        *,
        offset: int = 0,
        max_chars: int = MAX_MESSAGE_CONTENT_PAGE_CHARS,
    ) -> dict[str, Any]:
        """Read one bounded character slice from a message already in the archive."""
        self._validate_query(conversation_id, "conversation_id")
        self._validate_query(message_id, "message_id")
        self._validate_int("offset", offset, 0, 2**63 - 1)
        self._validate_int("max_chars", max_chars, 1, MAX_MESSAGE_CONTENT_PAGE_CHARS)
        self._ensure_session()
        row = self.index.get_message(conversation_id, message_id)
        if row is None:
            raise ValueError("Message is not present in the local archive. Fetch its conversation first.")
        content = "[Message deleted]" if row["is_deleted"] else str(row["body"] or "")
        end = min(len(content), offset + max_chars)
        next_offset = end if end < len(content) else None
        return {
            "conversation_id": conversation_id,
            "message_id": message_id,
            "offset": offset,
            "content": content[offset:end],
            "content_char_count": len(content),
            "next_offset": next_offset,
            "complete": next_offset is None,
            "is_deleted": bool(row["is_deleted"]),
            "edited_at": row["edited_at"],
            "deleted_at": row["deleted_at"],
        }

    def export_chat(
        self,
        conversation_id: str | None = None,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        message_limit: int | None = None,
        export_id: str | None = None,
        max_pages_to_fetch: int = 20,
    ) -> dict[str, Any]:
        """Prepare a resumable full-text snapshot from remote and retained messages."""
        self._validate_int(
            "max_pages_to_fetch", max_pages_to_fetch, 1, MAX_EXPORT_PAGES_PER_CALL
        )
        if message_limit is not None:
            self._validate_unbounded_positive_int("message_limit", message_limit)
        since_utc = self._normalize_bound("since", since)
        until_utc = self._normalize_bound("until", until)
        if since_utc and until_utc and since_utc > until_utc:
            raise ValueError("since must be earlier than or equal to until.")
        since_key = self._bound_key(since_utc)
        until_key = self._bound_key(until_utc)

        if export_id is None:
            if not isinstance(conversation_id, str) or not conversation_id.strip():
                raise ValueError("conversation_id is required to start an export.")
            chat_id = conversation_id.strip()
            chat = next(
                (item for item in self._get_chats(refresh=True) if item.id == chat_id),
                None,
            )
            if chat is None:
                raise ValueError("conversation_id was not found in the current Teams chat catalog.")
            self.index.upsert_chats([chat])
            export_id = uuid.uuid4().hex
            job = self.index.create_export_job(
                export_id,
                chat_id,
                since_key,
                until_key,
                message_limit,
            )
        else:
            if not isinstance(export_id, str) or not re.fullmatch(r"[a-f0-9]{32}", export_id):
                raise ValueError("export_id is invalid.")
            job = self.index.get_export_job(export_id)
            if job is None:
                raise ValueError("export_id was not found or has expired.")
            if conversation_id and conversation_id != job["conversation_id"]:
                raise ValueError("export_id does not match conversation_id.")
            if since is not None and since_key != job["since"]:
                raise ValueError("export_id does not match since.")
            if until is not None and until_key != job["until"]:
                raise ValueError("export_id does not match until.")
            if message_limit is not None and message_limit != job["message_limit"]:
                raise ValueError("export_id does not match message_limit.")

        pages_this_call = 0
        retries_this_call = 0
        if not job["complete"]:
            since_value = self._parse_stored_bound(job["since"])
            until_value = self._parse_stored_bound(job["until"])
            while pages_this_call < max_pages_to_fetch:
                request_url = job["next_url"]
                messages, next_url, raw_scanned, missing_timestamps = (
                    self.connector.get_messages_page(
                        str(job["conversation_id"]),
                        page_size=TeamsConnector.MESSAGE_PAGE_SIZE,
                        since=since_value,
                        until=until_value,
                        continuation_url=request_url,
                    )
                )
                retries_this_call += int(
                    getattr(self.connector, "last_request_retry_count", 0)
                )
                job = self.index.save_export_job_page(
                    export_id,
                    messages,
                    requested_url=request_url,
                    next_url=next_url,
                    raw_scanned=raw_scanned,
                    missing_timestamp_count=missing_timestamps,
                    formatter=self.formatter,
                )
                pages_this_call += 1
                if job["complete"]:
                    break

        if job["complete"]:
            job = self._write_export_snapshot(job)
            resource_uri = f"teams-export://exports/{export_id}/parts/0"
            return {
                "export_id": export_id,
                "status": "complete",
                "resource_uri": resource_uri,
                "mime_type": "text/plain; charset=utf-8",
                "byte_size": int(job["byte_size"] or 0),
                "message_count": int(job["message_count"] or 0),
                "conversation_id": job["conversation_id"],
                "since": job["since"] or None,
                "until": job["until"] or None,
                "coverage": {
                    "complete": True,
                    "remote_pages_fetched_total": int(job["pages"]),
                    "remote_items_scanned_total": int(job["scanned"]),
                    "missing_timestamp_items_excluded_total": int(
                        job["missing_timestamps"]
                    ),
                    "retry_count_this_call": retries_this_call,
                    "archive_included": True,
                },
                "next_call_tool": None,
                "next_call_arguments": None,
            }

        return {
            "export_id": export_id,
            "status": "preparing",
            "resource_uri": None,
            "mime_type": None,
            "byte_size": None,
            "message_count": None,
            "conversation_id": job["conversation_id"],
            "since": job["since"] or None,
            "until": job["until"] or None,
            "coverage": {
                "complete": False,
                "remote_pages_fetched_total": int(job["pages"]),
                "remote_items_scanned_total": int(job["scanned"]),
                "missing_timestamp_items_excluded_total": int(
                    job["missing_timestamps"]
                ),
                "retry_count_this_call": retries_this_call,
                "archive_included": True,
            },
            "next_call_tool": "export_chat",
            "next_call_arguments": {
                "export_id": export_id,
                "max_pages_to_fetch": max_pages_to_fetch,
            },
        }

    def read_export(
        self,
        export_id: str,
        *,
        offset_bytes: int = 0,
        max_bytes: int = EXPORT_RESOURCE_PAGE_BYTES,
    ) -> dict[str, Any]:
        """Read a bounded, UTF-8-safe segment from a completed export snapshot."""
        if not isinstance(export_id, str) or not re.fullmatch(r"[a-f0-9]{32}", export_id):
            raise ValueError("export_id is invalid.")
        self._validate_int("offset_bytes", offset_bytes, 0, 2**63 - 1)
        self._validate_int("max_bytes", max_bytes, 4, MAX_EXPORT_RESOURCE_BYTES)
        job = self.index.get_export_job(export_id)
        if job is None or not job["complete"] or not job["snapshot_path"]:
            raise ValueError("The export snapshot is not ready or no longer exists.")
        expected_directory = self._export_directory().resolve()
        path = Path(job["snapshot_path"]).resolve()
        if path.parent != expected_directory or not path.is_file():
            raise ValueError("The export snapshot is not available.")
        total_bytes = int(job["byte_size"] or path.stat().st_size)
        if offset_bytes > total_bytes:
            raise ValueError("offset_bytes is beyond the end of the export.")
        with path.open("rb") as stream:
            if offset_bytes < total_bytes:
                stream.seek(offset_bytes)
                first_byte = stream.read(1)
                if first_byte and first_byte[0] & 0xC0 == 0x80:
                    raise ValueError(
                        "offset_bytes must be a UTF-8 boundary returned by a previous read."
                    )
            stream.seek(offset_bytes)
            raw = stream.read(max_bytes)
        content = ""
        decoded_bytes = len(raw)
        for end in range(len(raw), max(-1, len(raw) - 4), -1):
            try:
                content = raw[:end].decode("utf-8")
                decoded_bytes = end
                break
            except UnicodeDecodeError:
                continue
        if raw and not content and offset_bytes < total_bytes:
            raise ValueError("The export contains invalid UTF-8 at this offset.")
        next_offset = offset_bytes + decoded_bytes
        complete = next_offset >= total_bytes
        return {
            "export_id": export_id,
            "offset_bytes": offset_bytes,
            "content": content,
            "byte_size": total_bytes,
            "next_offset_bytes": None if complete else next_offset,
            "complete": complete,
        }

    def _write_export_snapshot(self, job: dict[str, Any]) -> dict[str, Any]:
        export_id = str(job["export_id"])
        destination_directory = self._export_directory()
        destination_directory.mkdir(parents=True, exist_ok=True)
        destination = destination_directory / f"{export_id}.txt"
        if job.get("snapshot_path") and destination.is_file():
            return job

        conversation_id = str(job["conversation_id"])
        since = str(job["since"] or "") or None
        until = str(job["until"] or "") or None
        total = self.index.count_messages_in_range(
            conversation_id, since=since, until=until
        )
        message_limit = job.get("message_limit")
        output_count = min(total, int(message_limit)) if message_limit is not None else total
        cutoff = (
            self.index.latest_message_cutoff(
                conversation_id,
                since=since,
                until=until,
                limit=output_count,
            )
            if message_limit is not None and output_count
            else None
        )
        chat = next(
            (item for item in self._get_chats() if item.id == conversation_id),
            None,
        )
        chat_name = chat.display_name if chat else conversation_id
        since_value = self._parse_stored_bound(job["since"])
        until_value = self._parse_stored_bound(job["until"])

        handle, temporary_name = tempfile.mkstemp(
            prefix=f".{export_id}.", suffix=".tmp", dir=destination_directory
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as output:
                header = [
                    f"Conversation: {chat_name}",
                    f"Conversation ID: {conversation_id}",
                    "Time zone: UTC",
                    f"Messages: {output_count}",
                ]
                if since_value is not None:
                    header.append(f"Since: {self._timestamp(since_value)}")
                if until_value is not None:
                    header.append(f"Until: {self._timestamp(until_value)}")
                if message_limit is not None:
                    header.append(f"Limit: {message_limit}")
                output.write("\n".join(header) + "\n\n")
                after = None
                while True:
                    rows = self.index.query_messages_chronological(
                        conversation_id,
                        since=since,
                        until=until,
                        after=after,
                        from_key=cutoff,
                        limit=500,
                    )
                    if not rows:
                        break
                    for row in rows:
                        output.write(f"[{row['sent_at']}] {row['author']}\n")
                        body = "[Message deleted]" if row["is_deleted"] else row["body"]
                        output.write(body if body else "[No text content]")
                        output.write("\n\n")
                    last = rows[-1]
                    after = (last["sent_at"], last["message_id"])
            os.replace(temporary_path, destination)
        finally:
            temporary_path.unlink(missing_ok=True)
        return self.index.complete_export_job(
            export_id,
            snapshot_path=str(destination.resolve()),
            byte_size=destination.stat().st_size,
            message_count=output_count,
        )

    def _export_directory(self) -> Path:
        return self.index.path.parent / f"{self.index.path.stem}.exports"

    @staticmethod
    def _parse_stored_bound(value: str | None) -> datetime | None:
        if not value:
            return None
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))

    def search_messages(
        self,
        query: str,
        *,
        match_mode: str = "substring",
        conversation_ids: list[str] | None = None,
        all_catalog: bool = False,
        since: datetime | None = None,
        until: datetime | None = None,
        result_limit: int = 20,
        max_matches: int = DEFAULT_MAX_SEARCH_MATCHES,
        max_pages_to_fetch: int = 10,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Fetch and upsert an explicit date/chat scope before local SQL search."""
        query = self._validate_query(query, "query")
        if match_mode not in {"exact", "substring", "all_terms"}:
            raise ValueError("match_mode must be 'exact', 'substring', or 'all_terms'.")
        self._validate_int("result_limit", result_limit, 1, MAX_SEARCH_RESULTS)
        self._validate_unbounded_positive_int("max_matches", max_matches)
        self._validate_int(
            "max_pages_to_fetch", max_pages_to_fetch, 1, MAX_SEARCH_PAGES_PER_CALL
        )
        since_utc = self._normalize_bound("since", since)
        until_utc = self._normalize_bound("until", until)
        if since_utc is None or until_utc is None:
            raise ValueError("since and until are required for a complete message search.")
        if since_utc and until_utc and since_utc > until_utc:
            raise ValueError("since must be earlier than or equal to until.")
        if not isinstance(all_catalog, bool):
            raise ValueError("all_catalog must be a boolean.")
        if all_catalog == bool(conversation_ids):
            raise ValueError("Set exactly one of conversation_ids or all_catalog=true.")
        if not all_catalog:
            if not isinstance(conversation_ids, list) or not conversation_ids:
                raise ValueError("conversation_ids must contain at least one conversation ID.")
            conversation_ids = list(dict.fromkeys(
                self._validate_query(conversation_id, "conversation_ids item")
                for conversation_id in conversation_ids
            ))
        self._ensure_session()
        if all_catalog:
            chats = self._get_chats(refresh=cursor is None)
            chat_ids = [chat.id for chat in chats]
        else:
            chat_ids = conversation_ids
        if not chat_ids:
            raise ValueError("The requested chat scope is empty.")

        since_key = self._bound_key(since_utc) or None
        until_key = self._bound_key(until_utc) or None
        scope = (
            self.index._normalize_search_text(query),
            match_mode,
            json.dumps(chat_ids, separators=(",", ":")),
            since_key,
            until_key,
            str(max_matches),
            str(result_limit),
        )
        if cursor:
            state = self._read_cursor(cursor, "search_messages", scope)
        else:
            state = {
                "phase": "prepare",
                "chat_index": 0,
                "continuation_url": None,
                "seen_continuation_urls": set(),
                "chat_coverage": {},
                "after": None,
                "returned_total": 0,
                "generation": None,
            }

        pages_this_call = 0
        failed_chat: str | None = None
        error_message: str | None = None
        while state["phase"] == "prepare" and state["chat_index"] < len(chat_ids):
            chat_id = chat_ids[state["chat_index"]]
            request_url = state["continuation_url"]
            if request_url and request_url in state["seen_continuation_urls"]:
                raise RuntimeError("Teams repeated a message continuation link.")
            try:
                messages, next_url, raw_scanned, missing_timestamps = (
                    self.connector.get_messages_page(
                        chat_id,
                        page_size=MAX_HISTORY_PAGE_SIZE,
                        since=since_utc,
                        until=until_utc,
                        continuation_url=request_url,
                    )
                )
                if request_url:
                    state["seen_continuation_urls"].add(request_url)
                if next_url and next_url in state["seen_continuation_urls"]:
                    raise RuntimeError("Teams repeated a message continuation link.")
                self.index.upsert_messages(
                    chat_id,
                    messages,
                    formatter=self.formatter,
                )
            except AuthenticationRequiredError:
                raise
            except Exception as exc:
                failed_chat = chat_id
                error_message = self._safe_error(exc)
                break

            pages_this_call += 1
            previous = state["chat_coverage"].get(chat_id, {})
            state["continuation_url"] = next_url
            state["chat_coverage"][chat_id] = {
                "conversation_id": chat_id,
                "since": since_key,
                "until": until_key,
                "remote_pages_fetched": previous.get("remote_pages_fetched", 0) + 1,
                "remote_items_scanned": previous.get("remote_items_scanned", 0) + raw_scanned,
                "missing_timestamp_items": previous.get("missing_timestamp_items", 0)
                + missing_timestamps,
                "remote_complete": next_url is None,
                "last_refreshed_at": self.index._now(),
            }
            if next_url is None:
                state["chat_index"] += 1
                state["continuation_url"] = None
                state["seen_continuation_urls"] = set()
            if pages_this_call >= max_pages_to_fetch:
                break

        if state["phase"] == "prepare" and state["chat_index"] >= len(chat_ids):
            state["phase"] = "results"
            state["generation"] = self.index.counts()["generation"]

        requested_coverage = []
        for chat_id in chat_ids:
            item = state["chat_coverage"].get(chat_id)
            if item is None:
                item = {
                    "conversation_id": chat_id,
                    "since": since_key,
                    "until": until_key,
                    "remote_pages_fetched": 0,
                    "remote_items_scanned": 0,
                    "missing_timestamp_items": 0,
                    "remote_complete": False,
                    "last_refreshed_at": None,
                }
            requested_coverage.append(dict(item))
        preparation_complete = state["phase"] == "results"
        if not preparation_complete or failed_chat:
            next_cursor = self._store_cursor(
                "search_messages", scope, state, old=cursor
            )
            return {
                "items": [],
                "max_matches": max_matches,
                "returned_count": 0,
                "returned_total": state["returned_total"],
                "results_truncated": False,
                "truncation_reason": None,
                "next_cursor": next_cursor,
                "database_notice": self.database_notice,
                "coverage": {
                    "method": "local SQLite literal matching",
                    "server_side_search_used": False,
                    "complete_for_requested_scope": False,
                    "preparation_complete": False,
                    "index_generation": None,
                    "requested_since": since_key,
                    "requested_until": until_key,
                    "requested_chat_ids": chat_ids,
                    "chat_coverage": requested_coverage,
                    "pages_fetched_this_call": pages_this_call,
                    "max_pages_to_fetch": max_pages_to_fetch,
                    "failed_chat_id": failed_chat,
                    "error": error_message,
                    "continuation_tool": "search_messages",
                },
            }

        generation = self.index.counts()["generation"]
        if cursor and state["generation"] != generation:
            raise InvalidCursorError("The local archive changed; restart the search.")
        remaining = max_matches - state["returned_total"]
        query_limit = min(result_limit, remaining)
        rows, has_more = self.index.search_messages(
            query,
            match_mode=match_mode,
            conversation_ids=chat_ids,
            since=since_key,
            until=until_key,
            after=state["after"],
            limit=query_limit,
        )
        truncated = remaining <= len(rows) and has_more
        account_user_id = self._current_account_user_id()
        items = [
            self._indexed_message_record(row, query, account_user_id=account_user_id)
            for row in rows
        ]
        state["returned_total"] += len(items)
        if rows:
            last = rows[-1]
            state["after"] = (
                last["sent_at"],
                last["conversation_id"],
                last["message_id"],
            )
        if truncated:
            next_cursor = None
        elif has_more:
            next_cursor = self._store_cursor(
                "search_messages", scope, state, old=cursor
            )
        else:
            next_cursor = None
            if cursor:
                self._cursors.pop(cursor, None)

        return {
            "items": items,
            "max_matches": max_matches,
            "returned_count": len(items),
            "returned_total": state["returned_total"],
            "results_truncated": truncated,
            "truncation_reason": "max_matches" if truncated else None,
            "next_cursor": next_cursor,
            "database_notice": self.database_notice,
            "coverage": {
                "method": "local SQLite literal matching",
                "server_side_search_used": False,
                "complete_for_requested_scope": True,
                "preparation_complete": True,
                "index_generation": generation,
                "requested_since": since_key,
                "requested_until": until_key,
                "requested_chat_ids": chat_ids,
                "chat_coverage": requested_coverage,
                "pages_fetched_this_call": pages_this_call,
                "max_pages_to_fetch": max_pages_to_fetch,
                "failed_chat_id": None,
                "error": None,
                "continuation_tool": "search_messages" if next_cursor else None,
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
            message_retries = 0
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
                        message_retries += int(
                            getattr(self.connector, "last_request_retry_count", 0)
                        )
                        raise
                    except (RuntimeError, ValueError) as exc:
                        message_retries += int(
                            getattr(self.connector, "last_request_retry_count", 0)
                        )
                        message_error = self._safe_error(exc)
                        break
                    message_retries += int(
                        getattr(self.connector, "last_request_retry_count", 0)
                    )
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
                    "message_retries_this_call": message_retries,
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

    def _stored_message_record(
        self,
        row: dict[str, Any],
        *,
        account_user_id: str | None = None,
    ) -> dict[str, Any]:
        author_id = row.get("author_id")
        author_identifier = TeamsConnector._profile_identifier(author_id)
        account_identifier = TeamsConnector._profile_identifier(account_user_id)
        is_deleted = bool(row.get("is_deleted"))
        stored_body = str(row.get("body") or "")
        body = "[Message deleted]" if is_deleted else stored_body
        return {
            "id": row["message_id"],
            "conversation_id": row["conversation_id"],
            "author": row["author"],
            "author_id": author_id,
            "is_self": (
                author_identifier == account_identifier
                if author_identifier and account_identifier
                else None
            ),
            "timestamp": row["sent_at"],
            "content": body,
            "content_truncated": False,
            "content_char_count": len(body),
            "content_next_offset": None,
            "message_type": row["message_type"],
            "is_deleted": is_deleted,
            "edited_at": row.get("edited_at"),
            "deleted_at": row.get("deleted_at"),
        }

    def _indexed_message_record(
        self,
        row: dict[str, Any],
        query: str,
        *,
        account_user_id: str | None = None,
    ) -> dict[str, Any]:
        author_id = row.get("author_id")
        author_identifier = TeamsConnector._profile_identifier(author_id)
        account_identifier = TeamsConnector._profile_identifier(account_user_id)
        return {
            "id": row["message_id"],
            "conversation_id": row["conversation_id"],
            "chat_name": row["display_name"],
            "author": row["author"],
            "author_id": author_id,
            "is_self": (
                author_identifier == account_identifier
                if author_identifier and account_identifier
                else None
            ),
            "timestamp": row["sent_at"],
            "excerpt": self._excerpt(row["body"], query),
            "message_type": row["message_type"],
        }

    def _current_account_user_id(self) -> str | None:
        for method_name in ("get_user_metadata", "get_cached_account_metadata"):
            get_metadata = getattr(self.connector, method_name, None)
            if not callable(get_metadata):
                continue
            try:
                metadata = get_metadata()
            except RuntimeError:
                continue
            if not isinstance(metadata, dict):
                continue
            user_id = metadata.get("user_id")
            if isinstance(user_id, str) and user_id.strip():
                return user_id.strip()
        return None

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
            "is_self_chat": chat.is_self_chat,
            "last_message_at": TeamsService._timestamp(chat.last_message_at),
            "last_message_preview": chat.last_message_preview,
            "unread": chat.unread,
        }

    @staticmethod
    def _chat_from_record(record: dict[str, Any]) -> ChatSummary:
        timestamp = record.get("last_message_at")
        parsed = None
        if isinstance(timestamp, str) and timestamp:
            try:
                parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            except ValueError:
                parsed = None
        unread = record.get("unread")
        if isinstance(unread, int) and not isinstance(unread, bool):
            unread = bool(unread)
        return ChatSummary(
            id=str(record["conversation_id"]),
            display_name=str(record["display_name"]),
            last_message_at=parsed,
            last_message_preview=record.get("last_message_preview"),
            unread=unread if isinstance(unread, bool) else None,
            chat_type=record.get("chat_type"),
            avatar_url=record.get("avatar_url"),
            is_self_chat=str(record["conversation_id"]) == TeamsConnector.SELF_CHAT_ID,
        )

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
    def _validate_unbounded_positive_int(name: str, value: int) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} must be a positive integer.")

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
        detail = TeamsConnector._safe_exception_detail(exc)
        return f"{exc.__class__.__name__}: {detail}"
