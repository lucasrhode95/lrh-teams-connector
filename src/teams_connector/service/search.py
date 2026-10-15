"""Teams message search: one Search v2 query for a list of terms, optionally scoped by conversation and date.

Each term is sent as one quoted phrase, and a message matches when any term matches. Terms are never
split on their spaces and never read as query syntax. Search v2 matches whole words and word prefixes,
and a quoted phrase matches its words in order. Search reads no history and writes nothing: its hits are
pointers, and get_messages reads a conversation's history.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from uuid import UUID, uuid4

from teams_connector.domain.errors import InvalidRequest
from teams_connector.domain.models import Chat, MessageHit, MessageSearchCoverage, MessageSearchResult
from teams_connector.remote.ports import MessageSearchReader
from teams_connector.service import cursors
from teams_connector.service.history import check_range
from teams_connector.store.db import Store, from_iso, iso

CURSOR_KIND = "search_messages"
MAX_TERMS = 25
MAX_TERM_CHARS = 200
MAX_SCOPE_CONVERSATIONS = 100
MAX_QUERY_CHARS = 1_000  # the whole query sent to Search v2, scope and dates included
MAX_PAGE_SIZE = 25
MAX_OFFSET = 100_000
_CONVERSATION_ID = re.compile(r"[A-Za-z0-9:@._-]{1,200}")


class Search:
    def __init__(self, store: Store, remote_messages: MessageSearchReader) -> None:
        self._store = store
        self._remote_messages = remote_messages

    async def search_messages(
        self,
        terms: list[str] | None = None,
        *,
        conversation_ids: list[str] | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 25,
        cursor: str | None = None,
    ) -> MessageSearchResult:
        """Entry point: validates the terms, scope, dates, limit and cursor, then returns one Search v2 page.

        A message matches when any term matches. With a cursor, the terms, scope and dates come from the
        cursor and the other arguments are ignored.
        """
        if cursor:
            terms, conversation_ids, since, until, offset, page_size, cvid, logical_id = _resume(cursor)
        else:
            check_range(since, until)
            terms, conversation_ids = _terms(terms), _scope(conversation_ids)
            if type(limit) is not int or not 1 <= limit <= MAX_PAGE_SIZE:
                raise InvalidRequest(f"limit must be between 1 and {MAX_PAGE_SIZE}.")
            offset, page_size = 0, limit
            cvid, logical_id = str(uuid4()), str(uuid4())
        query = build_query(terms, conversation_ids, since, until)

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
            if next_offset <= MAX_OFFSET:
                next_cursor = cursors.encode(
                    CURSOR_KIND,
                    terms=terms,
                    conversation_ids=conversation_ids,
                    since=iso(since),
                    until=iso(until),
                    page_size=page_size,
                    offset=next_offset,
                    cvid=cvid,
                    logical_id=logical_id,
                )
                notes.append("Follow the cursor only if more matches are useful.")
            else:
                notes.append(f"The maximum search offset ({MAX_OFFSET}) was reached; no cursor is available.")
        if page.total is not None:
            notes.append("total is Search v2's count for the query and can move between pages.")
        if any(hit.message_id is None for hit in items):
            notes.append("Some hits carry no message_id; get_messages cannot read them.")
        return MessageSearchResult(
            query=query,
            items=items,
            cursor=next_cursor,
            coverage=MessageSearchCoverage(
                complete=complete,
                offset=offset,
                page_size=page_size,
                more_results_available=page.more_results_available,
                total=page.total,
                notes=notes,
            ),
        )


def build_query(
    terms: list[str],
    conversation_ids: list[str],
    since: datetime | None,
    until: datetime | None,
) -> str:
    """The Search v2 query for validated inputs. The scope and dates are clauses, soft-deleted messages are
    left out, and the quoted terms form one OR group. A term is a phrase, never query syntax."""
    clauses: list[str] = []
    if conversation_ids:
        clauses.append("(" + " OR ".join(f"clientthreadid:{item}" for item in conversation_ids) + ")")
    if since is not None:
        clauses.append(f"sent >= {_search_time(since)}")
    if until is not None:
        clauses.append(f"sent <= {_search_time(until)}")
    clauses.append("NOT (isClientSoftDeleted:TRUE)")
    clauses.append("(" + " OR ".join(f'"{term}"' for term in terms) + ")")
    query = " AND ".join(clauses)
    if len(query) > MAX_QUERY_CHARS:
        raise InvalidRequest(
            f"The terms and scope make a query longer than {MAX_QUERY_CHARS} characters; use fewer terms "
            "or conversations."
        )
    return query


def _terms(value: list[str] | None) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_TERMS:
        raise InvalidRequest(f"terms must be a list of 1 to {MAX_TERMS} phrases.")
    cleaned: list[str] = []
    for term in value:
        if not isinstance(term, str) or not term.strip():
            raise InvalidRequest("Each term must be non-empty text.")
        text = term.strip()
        if len(text) > MAX_TERM_CHARS:
            raise InvalidRequest(f"Each term must be at most {MAX_TERM_CHARS} characters.")
        if '"' in text:
            raise InvalidRequest("A term cannot contain a double quote; search for the words without it.")
        if any(ord(character) < 32 for character in text):
            raise InvalidRequest("A term cannot contain control characters.")
        cleaned.append(text)
    return list(dict.fromkeys(cleaned))


def _scope(value: list[str] | None) -> list[str]:
    if not value:
        return []
    if not isinstance(value, list) or len(value) > MAX_SCOPE_CONVERSATIONS:
        raise InvalidRequest(f"conversation_ids must hold at most {MAX_SCOPE_CONVERSATIONS} conversations.")
    for item in value:
        if not isinstance(item, str) or not _CONVERSATION_ID.fullmatch(item):
            raise InvalidRequest("conversation_ids must be conversation ids from list_chats or search_chats.")
    return list(dict.fromkeys(value))


def _search_time(value: datetime) -> str:
    """Search v2 compares sent times to the millisecond, in UTC; the bound is written that way."""
    moment = value.astimezone(UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S") + f".{moment.microsecond // 1000:03d}Z"


def _resume(
    cursor: str,
) -> tuple[list[str], list[str], datetime | None, datetime | None, int, int, str, str]:
    """The search inputs and paging a cursor carries. Cursors are not signed, so the terms, scope and dates
    are checked again as a new request's are, and the query is rebuilt from them, never read from the
    cursor."""
    state = cursors.decode(cursor, CURSOR_KIND)
    terms = _terms(state.get("terms"))
    conversation_ids = _scope(state.get("conversation_ids"))
    since, until = _cursor_time(state.get("since")), _cursor_time(state.get("until"))
    check_range(since, until)
    offset = state.get("offset")
    page_size = state.get("page_size")
    if type(offset) is not int or not 0 <= offset <= MAX_OFFSET:
        raise InvalidRequest("The search cursor has an invalid offset.")
    if type(page_size) is not int or not 1 <= page_size <= MAX_PAGE_SIZE:
        raise InvalidRequest("The search cursor has an invalid page size.")
    return (
        terms,
        conversation_ids,
        since,
        until,
        offset,
        page_size,
        _cursor_uuid(state.get("cvid"), "Cvid"),
        _cursor_uuid(state.get("logical_id"), "LogicalId"),
    )


def _cursor_time(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidRequest("The search cursor has an invalid date.")
    try:
        return from_iso(value)
    except ValueError:
        raise InvalidRequest("The search cursor has an invalid date.") from None


def _cursor_uuid(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise InvalidRequest(f"The search cursor has an invalid {label}.")
    try:
        return str(UUID(value))
    except ValueError:
        raise InvalidRequest(f"The search cursor has an invalid {label}.") from None


def _attach_known_conversation(hit: MessageHit, chats: dict[str, Chat]) -> MessageHit:
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
