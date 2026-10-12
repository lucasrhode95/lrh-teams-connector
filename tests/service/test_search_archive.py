from __future__ import annotations

from datetime import UTC, datetime

import pytest

from teams_connector.bootstrap import Services
from teams_connector.domain.errors import InvalidRequest
from teams_connector.domain.models import RemoteMessageHit
from teams_connector.remote.ports import RemoteMessageSearchPage
from teams_connector.service import cursors
from teams_connector.service.search import REMOTE_SEARCH_MAX_OFFSET, REMOTE_SEARCH_MAX_QUERY_CHARS
from tests.fakes.teams_fake import FakeTeams, cs_message

ONE = "19:one@unq.gbl.spaces"
GROUP = "19:group@thread.v2"
SINCE = datetime(2026, 1, 1, tzinfo=UTC)
UNTIL = datetime(2026, 12, 31, tzinfo=UTC)


class RemoteSearchStub:
    def __init__(self, pages: list[RemoteMessageSearchPage]) -> None:
        self.pages = iter(pages)
        self.calls: list[dict[str, object]] = []

    async def search_messages(
        self,
        query: str,
        *,
        offset: int,
        page_size: int,
        cvid: str,
        logical_id: str,
    ) -> RemoteMessageSearchPage:
        self.calls.append(
            {
                "query": query,
                "offset": offset,
                "page_size": page_size,
                "cvid": cvid,
                "logical_id": logical_id,
            }
        )
        return next(self.pages)


async def test_online_search_returns_pointer_and_exact_known_conversation(services: Services) -> None:
    await services.catalog.refresh()
    before = services.store.count(ONE, since=None, until=None)
    remote = RemoteSearchStub(
        [
            RemoteMessageSearchPage(
                items=[
                    RemoteMessageHit(
                        search_message_id="synthetic-search-message",
                        message_id_kind="InternetMessageId",
                        conversation_id_candidates=[ONE],
                        sent_at=datetime(2026, 10, 10, 12, 34, tzinfo=UTC),
                        preview="synthetic matching excerpt",
                    )
                ],
                more_results_available=True,
                result_set_found=True,
            ),
            RemoteMessageSearchPage(
                items=[
                    RemoteMessageHit(
                        search_message_id="synthetic-unmapped-message",
                        conversation_id_candidates=["19:unknown@thread.v2"],
                    )
                ],
                more_results_available=False,
                result_set_found=True,
            ),
        ]
    )
    services.search._remote_messages = remote

    first = await services.search.search_remote_messages("synthetic query", limit=1)
    assert first.cursor and not first.coverage.complete
    matched = first.items[0]
    assert matched.search_message_id == "synthetic-search-message"
    assert matched.conversation_id == ONE
    assert matched.conversation_name == "Pat Peer"
    assert matched.sent_at == datetime(2026, 10, 10, 12, 34, tzinfo=UTC)

    second = await services.search.search_remote_messages("synthetic query", limit=25, cursor=first.cursor)
    assert second.coverage.complete and second.cursor is None
    assert second.items[0].conversation_id is None
    assert second.items[0].conversation_id_candidates == ["19:unknown@thread.v2"]
    assert remote.calls[1]["offset"] == 1
    assert remote.calls[0]["cvid"] == remote.calls[1]["cvid"]
    assert remote.calls[0]["logical_id"] == remote.calls[1]["logical_id"]
    assert services.store.count(ONE, since=None, until=None) == before


async def test_online_search_rejects_a_cursor_for_another_query(services: Services) -> None:
    remote = RemoteSearchStub(
        [
            RemoteMessageSearchPage(
                items=[RemoteMessageHit(search_message_id="synthetic-hit")],
                more_results_available=True,
                result_set_found=True,
            )
        ]
    )
    services.search._remote_messages = remote
    first = await services.search.search_remote_messages("synthetic query")
    assert first.cursor

    with pytest.raises(InvalidRequest, match="does not match this search cursor"):
        await services.search.search_remote_messages("different query", cursor=first.cursor)
    assert len(remote.calls) == 1


async def test_online_search_bounds_query_length_and_page_size(services: Services) -> None:
    with pytest.raises(InvalidRequest, match="no longer than"):
        await services.search.search_remote_messages("x" * (REMOTE_SEARCH_MAX_QUERY_CHARS + 1))
    with pytest.raises(InvalidRequest, match="limit must be between"):
        await services.search.search_remote_messages("synthetic", limit=True)  # type: ignore[arg-type]


async def test_online_search_does_not_emit_cursor_past_offset_limit(services: Services) -> None:
    remote = RemoteSearchStub(
        [
            RemoteMessageSearchPage(
                items=[RemoteMessageHit(search_message_id="synthetic-last-page")],
                more_results_available=True,
                result_set_found=True,
            )
        ]
    )
    services.search._remote_messages = remote
    cursor = cursors.encode(
        "search_remote_messages",
        query="synthetic query",
        offset=REMOTE_SEARCH_MAX_OFFSET,
        page_size=25,
        cvid="00000000-0000-0000-0000-000000000001",
        logical_id="00000000-0000-0000-0000-000000000002",
    )

    result = await services.search.search_remote_messages("synthetic query", cursor=cursor)

    assert result.cursor is None
    assert not result.coverage.complete
    assert any("maximum message-search offset" in note for note in result.coverage.notes)


async def test_search_prepares_the_scope_then_matches(services: Services) -> None:
    result = await services.search.search_messages(
        "BUDGET review", since=SINCE, until=UNTIL, conversation_ids=[ONE]
    )
    assert result.coverage.preparation_complete and result.coverage.complete
    assert [h.message.id for h in result.items] == ["1002"]
    assert result.items[0].chat_name == "Pat Peer" and result.items[0].message.is_self


async def test_preparation_is_bounded_and_continues_with_the_cursor(services: Services) -> None:
    await services.catalog.refresh()
    first = await services.search.search_messages(
        "news", since=SINCE, until=UNTIL, all_chats=True, max_pages=1
    )
    assert not first.coverage.preparation_complete and first.items == [] and first.cursor
    result = first
    while result.cursor and not result.coverage.preparation_complete:
        result = await services.search.search_messages("", cursor=result.cursor, max_pages=1)
    assert [h.message.id for h in result.items] == ["2001"]


async def test_match_modes_and_literal_escaping(services: Services, fake: FakeTeams) -> None:
    fake.chats[ONE].messages = [
        cs_message("a", "2026-05-01T00:00:00.000Z", "100% done_now"),
        cs_message("b", "2026-05-02T00:00:00.000Z", "100 percent done"),
        cs_message("c", "2026-05-03T00:00:00.000Z", "Relatório final"),
    ]

    async def ids(query: str, mode: str = "substring") -> list[str]:
        result = await services.search.search_messages(
            query,
            since=SINCE,
            until=UNTIL,
            conversation_ids=[ONE],
            mode=mode,  # type: ignore[arg-type]
        )
        return [h.message.id for h in result.items]

    assert await ids("100%") == ["a"]  # % is literal, not a wildcard
    assert await ids("done_") == ["a"]
    assert await ids("done 100", "all_terms") == ["b", "a"]
    assert await ids("relatorio") == []  # accents stay significant
    assert await ids("RELATÓRIO FINAL", "exact") == ["c"]


async def test_deleted_messages_stay_searchable_and_flagged(services: Services, fake: FakeTeams) -> None:
    await services.search.search_messages("hello", since=SINCE, until=UNTIL, conversation_ids=[ONE])
    fake.chats[ONE].messages[0] = cs_message(
        "1001",
        "2026-09-01T10:00:00.000Z",
        deleted="2026-09-10T00:00:00.000Z",
        kind="MessageDelete",
    )
    result = await services.search.search_messages("hello", since=SINCE, until=UNTIL, conversation_ids=[ONE])
    hit = result.items[0].message
    assert hit.is_deleted and hit.deleted_at and hit.text == "First hello"


async def test_search_requires_a_range_and_one_scope(services: Services) -> None:
    with pytest.raises(InvalidRequest, match="since and until"):
        await services.search.search_messages("x", conversation_ids=[ONE])
    with pytest.raises(InvalidRequest, match="exactly one"):
        await services.search.search_messages(
            "x", since=SINCE, until=UNTIL, conversation_ids=[ONE], all_chats=True
        )


async def test_search_cursor_rejects_a_different_query(services: Services) -> None:
    first = await services.search.search_messages(
        "e", since=SINCE, until=UNTIL, conversation_ids=[ONE], limit=1
    )
    assert first.cursor
    with pytest.raises(InvalidRequest, match="query does not match"):
        await services.search.search_messages(
            "another query", since=SINCE, until=UNTIL, conversation_ids=[ONE], cursor=first.cursor
        )


async def test_unreachable_chat_is_reported_not_fatal(services: Services) -> None:
    result = await services.search.search_messages(
        "hello", since=SINCE, until=UNTIL, conversation_ids=["19:gone@thread.v2", ONE]
    )
    assert [h.message.id for h in result.items] == ["1001"]
    assert not result.coverage.complete
    assert result.coverage.prepared_this_call[0].error


async def test_sync_resumes_rotates_and_indexes_rosters(services: Services, fake: FakeTeams) -> None:
    fake.chats[ONE].messages = [
        cs_message(f"m{i:03d}", f"2026-06-{1 + i // 24:02d}T{i % 24:02d}:00:00.000Z") for i in range(250)
    ]
    first = await services.archive.sync(conversation_id=ONE, max_pages=1)
    assert first.items[0].messages_added == 100 and not first.items[0].history_complete
    assert first.items[0].roster_complete
    while not services.store.state(ONE).history_complete:
        await services.archive.sync(conversation_id=ONE, max_pages=1)
    assert services.store.count(ONE, since=None, until=None) == 250
    everything = await services.archive.sync(max_chats=10, max_pages=20)
    names = {row.conversation_id: row for row in everything.items}
    assert names["48:notes"].roster_complete  # the self chat's roster is just the user
    assert everything.totals.complete_histories == len(services.store.chats())


async def test_status_reports_range_coverage(services: Services) -> None:
    await services.archive.sync(conversation_id=ONE)
    status = services.archive.status(conversation_id=ONE, since=SINCE, until=UNTIL)
    row = status.items[0]
    assert row.history_complete and row.range_covered and row.messages_stored == 3
    assert status.covered == 1
