from __future__ import annotations

from datetime import UTC, datetime

import pytest

from teams_connector.bootstrap import Services
from teams_connector.domain.errors import InvalidRequest
from teams_connector.domain.models import MessageHit
from teams_connector.remote.ports import MessageSearchPage
from teams_connector.service import cursors
from teams_connector.service.search import Search, build_query

ONE = "19:one@unq.gbl.spaces"
SINCE = datetime(2026, 9, 1, 9, 30, 15, 250000, tzinfo=UTC)
UNTIL = datetime(2026, 9, 30, 12, tzinfo=UTC)


class SearchStub:
    def __init__(self, pages: list[MessageSearchPage]) -> None:
        self.pages = list(pages)
        self.queries: list[dict[str, object]] = []

    async def search_messages(
        self, query: str, *, offset: int, page_size: int, cvid: str, logical_id: str
    ) -> MessageSearchPage:
        self.queries.append(
            {"query": query, "offset": offset, "page_size": page_size, "cvid": cvid, "logical_id": logical_id}
        )
        return self.pages.pop(0)


def _page(
    items: list[MessageHit] | None = None,
    *,
    more: bool | None = False,
    found: bool = True,
    total: int | None = None,
) -> MessageSearchPage:
    return MessageSearchPage(
        items=items or [], more_results_available=more, result_set_found=found, total=total
    )


def _search(services: Services, *pages: MessageSearchPage) -> tuple[Search, SearchStub]:
    stub = SearchStub(list(pages))
    return Search(services.store, stub), stub


def test_terms_are_quoted_and_ored_with_the_scope_and_dates() -> None:
    assert build_query(["budget review", "forecast"], [ONE], SINCE, UNTIL) == (
        f"(clientthreadid:{ONE}) AND sent >= 2026-09-01T09:30:15.250Z AND sent <= 2026-09-30T12:00:00.000Z "
        'AND NOT (isClientSoftDeleted:TRUE) AND ("budget review" OR "forecast")'
    )


def test_without_scope_or_dates_the_query_is_the_terms_and_the_soft_delete_filter() -> None:
    assert build_query(["budget"], [], None, None) == 'NOT (isClientSoftDeleted:TRUE) AND ("budget")'


async def test_one_query_is_sent_and_hits_map_to_message_ids(services: Services) -> None:
    await services.catalog.refresh()
    hit = MessageHit(message_id="synthetic-message-1", conversation_id_candidates=[ONE], sent_at=SINCE)
    search, stub = _search(services, _page([hit], more=False, total=1))

    result = await search.search_messages(["budget"], conversation_ids=[ONE], since=SINCE, until=UNTIL)

    expected = build_query(["budget"], [ONE], SINCE, UNTIL)
    assert len(stub.queries) == 1 and stub.queries[0]["query"] == expected
    assert result.items[0].message_id == "synthetic-message-1"
    assert result.items[0].conversation_id == ONE and result.items[0].conversation_name
    assert result.coverage.complete and result.coverage.total == 1
    assert not any("message_id" in note for note in result.coverage.notes)


async def test_a_cursor_continues_the_same_query_and_ignores_the_other_arguments(services: Services) -> None:
    search, stub = _search(
        services,
        _page([MessageHit(message_id="synthetic-1")], more=True, total=2),
        _page([MessageHit(message_id="synthetic-2")], more=False, total=2),
    )

    first = await search.search_messages(["budget"], limit=1)
    assert first.cursor is not None and not first.coverage.complete

    second = await search.search_messages(["something else"], cursor=first.cursor)
    assert [hit.message_id for hit in second.items] == ["synthetic-2"] and second.coverage.complete
    assert stub.queries[1]["query"] == stub.queries[0]["query"]
    assert stub.queries[1]["offset"] == 1 and stub.queries[1]["cvid"] == stub.queries[0]["cvid"]


async def test_an_empty_answer_is_a_confirmed_zero(services: Services) -> None:
    search, _ = _search(services, _page([], more=False, found=True, total=0))
    result = await search.search_messages(["nothing here"])
    assert result.items == [] and result.coverage.complete and result.coverage.total == 0


async def test_a_missing_result_set_is_not_a_zero(services: Services) -> None:
    search, _ = _search(services, _page([], more=None, found=False))
    result = await search.search_messages(["something"])
    assert not result.coverage.complete
    assert any("zero hits are not confirmed" in note for note in result.coverage.notes)


async def test_a_hit_without_a_message_id_is_said_so(services: Services) -> None:
    search, _ = _search(services, _page([MessageHit(message_id=None)], more=False))
    result = await search.search_messages(["something"])
    assert any("carry no message_id" in note for note in result.coverage.notes)


@pytest.mark.parametrize(
    "terms",
    [
        None,
        [],
        [""],
        ["   "],
        ['say "hi"'],
        ["tab\tseparated"],
        ["x" * 201],
        [f"term {number}" for number in range(26)],
    ],
)
async def test_invalid_terms_are_refused_before_any_request(
    services: Services, terms: list[str] | None
) -> None:
    search, stub = _search(services)
    with pytest.raises(InvalidRequest):
        await search.search_messages(terms)
    assert stub.queries == []


async def test_invalid_scope_limit_and_dates_are_refused(services: Services) -> None:
    search, stub = _search(services)
    with pytest.raises(InvalidRequest):
        await search.search_messages(["x"], conversation_ids=["19:not a conversation id"])
    with pytest.raises(InvalidRequest):
        await search.search_messages(["x"], conversation_ids=[ONE] * 101)
    with pytest.raises(InvalidRequest):
        await search.search_messages(["x"], limit=26)
    with pytest.raises(InvalidRequest):
        await search.search_messages(["x"], since=datetime(2026, 1, 1))  # no timezone
    assert stub.queries == []


async def test_a_query_longer_than_the_limit_is_refused(services: Services) -> None:
    search, stub = _search(services)
    terms = [f"{number}" + "x" * 150 for number in range(7)]
    with pytest.raises(InvalidRequest):
        await search.search_messages(terms)
    assert stub.queries == []


async def test_a_tampered_cursor_is_refused(services: Services) -> None:
    search, stub = _search(services)
    cursor = cursors.encode(
        "search_messages",
        query="NOT (isClientSoftDeleted:TRUE)",
        offset=-1,
        page_size=5,
        cvid="00000000-0000-0000-0000-000000000001",
        logical_id="00000000-0000-0000-0000-000000000002",
    )
    with pytest.raises(InvalidRequest):
        await search.search_messages(cursor=cursor)
    assert stub.queries == []


async def test_a_cursor_cannot_carry_its_own_query(services: Services) -> None:
    search, stub = _search(services)
    for state in (
        {"query": "anything at all"},  # a raw query is never read from a cursor
        {"terms": ['x" OR "y'], "conversation_ids": []},  # terms are checked again, as on a new request
        {"terms": ["budget"], "conversation_ids": ["19:a) OR (b"]},
    ):
        cursor = cursors.encode(
            "search_messages",
            **state,
            offset=5,
            page_size=5,
            cvid="00000000-0000-0000-0000-000000000001",
            logical_id="00000000-0000-0000-0000-000000000002",
        )
        with pytest.raises(InvalidRequest):
            await search.search_messages(cursor=cursor)
    assert stub.queries == []
