from __future__ import annotations

from datetime import UTC, datetime

import pytest

from teams_connector.bootstrap import Services
from teams_connector.domain.errors import InvalidRequest
from tests.fakes.teams_fake import FakeTeams, cs_message

ONE = "19:one@unq.gbl.spaces"
GROUP = "19:group@thread.v2"
SINCE = datetime(2026, 1, 1, tzinfo=UTC)
UNTIL = datetime(2026, 12, 31, tzinfo=UTC)


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
