"""Scoped message search: fetch the requested chats and dates into the archive, then match locally.

Microsoft Graph's message search is denied for this client and no private Teams search route has a
verified contract, so search delegates the fetching to Teams (date-bounded history reads) and does the
matching in the archive: exact, substring or all-terms literal matching after NFKC normalization and
case folding (accents stay significant). Retained messages the server no longer returns are searched
too, and deleted ones are flagged.

Preparation is bounded per call; the cursor continues it and then pages through the matches.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime

from teams_connector.domain.errors import AuthenticationRequired, ConnectorError, InvalidRequest
from teams_connector.domain.models import (
    ChatPreparation,
    MatchMode,
    Message,
    SearchCoverage,
    SearchHit,
    SearchResult,
)
from teams_connector.service import cursors
from teams_connector.service.catalog import Catalog
from teams_connector.service.history import History, check_range
from teams_connector.store.db import Store, from_iso, iso

MAX_SCOPE_CHATS = 100


class Search:
    def __init__(self, store: Store, catalog: Catalog, history: History) -> None:
        self._store = store
        self._catalog = catalog
        self._history = history

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


def _excerpt(message: Message, query: str, max_chars: int = 600) -> Message:
    compact = re.sub(r"\s+", " ", message.text).strip()
    if len(compact) > max_chars:
        position = max(0, compact.casefold().find(query.casefold()))
        start = max(0, position - max_chars // 4)
        end = min(len(compact), start + max_chars)
        compact = f"{'…' if start else ''}{compact[start:end]}{'…' if end < len(compact) else ''}"
    return message.model_copy(update={"text": compact})
