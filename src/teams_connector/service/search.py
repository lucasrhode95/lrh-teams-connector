"""Online message discovery and scoped local archive search.

``search_remote_messages`` returns a bounded page of Search v2 message pointers. It neither downloads
history nor writes to the local archive. ``search_messages`` is the deep, scoped workflow: fetch the
requested chats and dates, then match locally using exact, substring or all-terms literal matching
after NFKC normalization and case folding (accents stay significant). Retained messages the server
no longer returns are searched too, and deleted ones are flagged.

Local-search preparation is bounded per call; its cursor continues preparation and then pages through
the matches. Remote-search continuation is one independent Search v2 page at a time.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from uuid import UUID, uuid4

from teams_connector.domain.errors import AuthenticationRequired, ConnectorError, InvalidRequest
from teams_connector.domain.models import (
    Chat,
    ChatPreparation,
    MatchMode,
    Message,
    RemoteMessageHit,
    RemoteMessageSearchResult,
    RemoteSearchCoverage,
    SearchCoverage,
    SearchHit,
    SearchResult,
)
from teams_connector.remote.ports import MessageSearchReader
from teams_connector.service import cursors
from teams_connector.service.catalog import Catalog
from teams_connector.service.history import History, check_range
from teams_connector.store.db import Store, from_iso, iso

MAX_SCOPE_CHATS = 100
REMOTE_SEARCH_MAX_PAGE_SIZE = 25
REMOTE_SEARCH_MAX_OFFSET = 100_000
REMOTE_SEARCH_MAX_QUERY_CHARS = 512


class Search:
    def __init__(
        self,
        store: Store,
        catalog: Catalog,
        history: History,
        remote_messages: MessageSearchReader,
    ) -> None:
        self._store = store
        self._catalog = catalog
        self._history = history
        self._remote_messages = remote_messages

    async def search_remote_messages(
        self,
        query: str,
        *,
        limit: int = 20,
        cursor: str | None = None,
    ) -> RemoteMessageSearchResult:
        """Validate public inputs and return one online Search v2 page without hydrating history."""
        if not isinstance(query, str) or not query.strip():
            raise InvalidRequest("query must be a non-empty string.")
        query = query.strip()
        if len(query) > REMOTE_SEARCH_MAX_QUERY_CHARS:
            raise InvalidRequest(f"query must be no longer than {REMOTE_SEARCH_MAX_QUERY_CHARS} characters.")
        if type(limit) is not int or not 1 <= limit <= REMOTE_SEARCH_MAX_PAGE_SIZE:
            raise InvalidRequest(f"limit must be between 1 and {REMOTE_SEARCH_MAX_PAGE_SIZE}.")

        if cursor:
            state = cursors.decode(cursor, "search_remote_messages")
            if state.get("query") != query:
                raise InvalidRequest("The query does not match this search cursor; start the search again.")
            offset = state.get("offset")
            cvid = _cursor_uuid(state.get("cvid"), "Cvid")
            logical_id = _cursor_uuid(state.get("logical_id"), "LogicalId")
            page_size = state.get("page_size")
            if type(offset) is not int or not 0 <= offset <= REMOTE_SEARCH_MAX_OFFSET:
                raise InvalidRequest("The search cursor has an invalid offset.")
            if type(page_size) is not int or not 1 <= page_size <= REMOTE_SEARCH_MAX_PAGE_SIZE:
                raise InvalidRequest("The search cursor has an invalid page size.")
        else:
            offset = 0
            page_size = limit
            cvid, logical_id = str(uuid4()), str(uuid4())

        page = await self._remote_messages.search_messages(
            query,
            offset=offset,
            page_size=page_size,
            cvid=cvid,
            logical_id=logical_id,
        )
        chats = {chat.id: chat for chat in self._store.chats()}
        items = [_attach_known_conversation(hit, chats) for hit in page.items]
        notes: list[str] = []
        next_offset = offset + len(page.items)
        next_cursor: str | None = None
        complete = page.result_set_found and page.more_results_available is False
        if not page.result_set_found:
            notes.append("Search returned no interpretable Message result set; zero hits are not confirmed.")
        elif page.more_results_available is None:
            notes.append("Search did not report whether more results are available.")
        elif page.more_results_available and not page.items:
            notes.append("Search reported more results but returned no rows to advance the offset.")
        elif page.more_results_available:
            if next_offset <= REMOTE_SEARCH_MAX_OFFSET:
                next_cursor = cursors.encode(
                    "search_remote_messages",
                    query=query,
                    page_size=page_size,
                    offset=next_offset,
                    cvid=cvid,
                    logical_id=logical_id,
                )
            else:
                notes.append(
                    f"The maximum message-search offset ({REMOTE_SEARCH_MAX_OFFSET}) was reached; "
                    "no cursor is available."
                )
        if page.more_results_available and next_cursor is not None:
            notes.append("Follow the cursor only if more online matches are useful.")
        notes.append(
            "A search message id is a Search v2 pointer; direct reuse as get_messages message_id "
            "has not been verified. Use a matched conversation_id and sent_at to request nearby history."
        )
        return RemoteMessageSearchResult(
            query=query,
            items=items,
            cursor=next_cursor,
            coverage=RemoteSearchCoverage(
                complete=complete,
                offset=offset,
                page_size=page_size,
                more_results_available=page.more_results_available,
                notes=notes,
            ),
        )

    async def search_messages(
        self,
        query: str,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        conversation_ids: list[str] | None = None,
        all_chats: bool = False,
        mode: MatchMode = "substring",
        limit: int = 20,
        max_matches: int = 1000,
        max_pages: int = 10,
        cursor: str | None = None,
    ) -> SearchResult:
        if cursor:
            s = cursors.decode(cursor, "search_messages")
            if query and query.strip() != s.get("query"):
                raise InvalidRequest("The query does not match this search cursor; start the search again.")
            query, mode, limit, max_matches = s["query"], s["mode"], s["limit"], s["max_matches"]
            since, until = from_iso(s["since"]), from_iso(s["until"])
            chat_ids = await self._scope(s["ids"], s["all"], s["scope_hash"])
        else:
            if not query or not query.strip():
                raise InvalidRequest("query must be a non-empty string.")
            if since is None or until is None:
                raise InvalidRequest("since and until are required: search covers an explicit date range.")
            check_range(since, until)
            if all_chats == bool(conversation_ids):
                raise InvalidRequest("Set exactly one of conversation_ids or all_chats=true.")
            if mode == "all_terms" and not re.findall(r"\w+", query, flags=re.UNICODE):
                raise InvalidRequest("query must contain at least one searchable word.")
            if conversation_ids and len(set(conversation_ids)) > MAX_SCOPE_CHATS:
                raise InvalidRequest(
                    f"Search at most {MAX_SCOPE_CHATS} conversations, or use all_chats=true."
                )
            ids = list(dict.fromkeys(i.strip() for i in conversation_ids or [] if i and i.strip()))
            await self._catalog.ensure(refresh=all_chats)
            chat_ids = await self._scope(ids, all_chats, None)
            s = {
                "query": query.strip(),
                "mode": mode,
                "limit": limit,
                "max_matches": max_matches,
                "since": iso(since),
                "until": iso(until),
                "ids": ids,
                "all": all_chats,
                "scope_hash": _scope_hash(chat_ids),
                "index": 0,
                "link": None,
                "after": None,
                "returned": 0,
            }
            query = s["query"]
        if since is None or until is None:
            raise InvalidRequest("This cursor has no date range; start the search again.")

        prepared: list[ChatPreparation] = []
        pages_left = max_pages
        while s["index"] < len(chat_ids) and pages_left > 0:
            chat_id = chat_ids[s["index"]]
            try:
                chat = await self._catalog.get(chat_id)
                fetch = await self._history.fetch_range(
                    chat, since=since, until=until, link=s["link"], max_pages=pages_left
                )
            except AuthenticationRequired:
                raise
            except ConnectorError as exc:
                prepared.append(ChatPreparation(conversation_id=chat_id, pages_fetched=0, complete=False,
                                                error=str(exc)))  # fmt: skip
                s["index"] += 1  # reported, then skipped: one inaccessible chat must not block the scope
                s["link"] = None
                continue
            pages_left -= fetch.pages
            prepared.append(ChatPreparation(conversation_id=chat_id, pages_fetched=fetch.pages,
                                            complete=fetch.complete))  # fmt: skip
            if fetch.complete:
                s["index"] += 1
                s["link"] = None
            else:
                s["link"] = fetch.link
        failures = [p for p in prepared if p.error]
        s.setdefault("failed", 0)
        s["failed"] += len(failures)

        coverage = SearchCoverage(
            complete=False,
            preparation_complete=s["index"] >= len(chat_ids),
            chats_in_scope=len(chat_ids),
            chats_prepared=min(s["index"], len(chat_ids)),
            pages_fetched_this_call=max_pages - pages_left,
            since=since,
            until=until,
            prepared_this_call=prepared,
        )
        if not coverage.preparation_complete:
            coverage.notes.append(
                "The scope is still being fetched; no matches are returned until it is. Follow the cursor."
            )
            return SearchResult(query=query, items=[], cursor=cursors.encode("search_messages", **s),
                                coverage=coverage)  # fmt: skip

        remaining = max_matches - s["returned"]
        hits, more = self._store.search(
            query,
            mode=mode,
            conversation_ids=chat_ids,
            since=since,
            until=until,
            after=tuple(s["after"]) if s["after"] else None,
            limit=max(0, min(limit, remaining)),
        )
        s["returned"] += len(hits)
        if hits:
            last = hits[-1].message
            s["after"] = [iso(last.sent_at), last.conversation_id, last.id]
        truncated = more and s["returned"] >= max_matches
        coverage.results_truncated = truncated
        coverage.complete = not truncated and s["failed"] == 0
        if s["failed"]:
            coverage.notes.append(
                f"{s['failed']} conversations could not be fetched; see prepared_this_call."
            )
        items = [
            SearchHit(chat_name=hit.chat_name, message=_excerpt(self._history.annotate(hit.message), query))
            for hit in hits
        ]
        return SearchResult(
            query=query,
            items=items,
            cursor=cursors.encode("search_messages", **s) if more and not truncated else None,
            coverage=coverage,
        )

    async def _scope(self, ids: list[str], all_chats: bool, expected_hash: str | None) -> list[str]:
        chat_ids = sorted(c.id for c in self._store.chats()) if all_chats else ids
        if expected_hash is not None and _scope_hash(chat_ids) != expected_hash:
            raise InvalidRequest(
                "The chat catalog changed since this search started; start the search again."
            )
        if not chat_ids:
            raise InvalidRequest("The requested chat scope is empty.")
        return chat_ids


def _scope_hash(ids: list[str]) -> str:
    return hashlib.sha256(json.dumps(ids).encode()).hexdigest()[:16]


def _cursor_uuid(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise InvalidRequest(f"The search cursor has an invalid {label}.")
    try:
        return str(UUID(value))
    except ValueError:
        raise InvalidRequest(f"The search cursor has an invalid {label}.") from None


def _attach_known_conversation(hit: RemoteMessageHit, chats: dict[str, Chat]) -> RemoteMessageHit:
    matches = list(
        dict.fromkeys(candidate for candidate in hit.conversation_id_candidates if candidate in chats)
    )
    if len(matches) != 1:
        return hit
    chat = chats[matches[0]]
    return hit.model_copy(
        update={
            "conversation_id": matches[0],
            "conversation_name": chat.name,
        }
    )


def _excerpt(message: Message, query: str, max_chars: int = 600) -> Message:
    compact = re.sub(r"\s+", " ", message.text).strip()
    if len(compact) > max_chars:
        position = max(0, compact.casefold().find(query.casefold()))
        start = max(0, position - max_chars // 4)
        end = min(len(compact), start + max_chars)
        compact = f"{'…' if start else ''}{compact[start:end]}{'…' if end < len(compact) else ''}"
    return message.model_copy(update={"text": compact})
