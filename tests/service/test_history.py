from __future__ import annotations

from datetime import UTC, datetime

import pytest

from teams_connector.bootstrap import Services
from teams_connector.domain.errors import InvalidRequest, Upstream
from teams_connector.remote.ports import HistoryPage
from teams_connector.service.history import History
from tests.fakes.teams_fake import FakeTeams, cs_message, graph_message

ONE = "19:one@unq.gbl.spaces"
CHANNEL = "19:general@thread.tacv2"


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def long_chat(fake: FakeTeams, count: int = 120) -> None:
    fake.chats[ONE].messages = [
        cs_message(f"m{i:03d}", f"2026-06-{1 + i // 24:02d}T{i % 24:02d}:00:00.000Z", f"Message {i}")
        for i in range(count)
    ]


async def test_pages_go_back_in_time_and_are_chronological_within(
    services: Services, fake: FakeTeams
) -> None:
    long_chat(fake)
    seen: list[str] = []
    page = await services.history.get_messages(ONE, page_size=50)
    while True:
        ids = [m.id for m in page.items]
        assert ids == sorted(ids)  # oldest first within a page
        seen = ids + seen
        if not page.cursor:
            break
        page = await services.history.get_messages(ONE, cursor=page.cursor)
    assert seen == [f"m{i:03d}" for i in range(120)]  # nothing skipped, nothing repeated
    assert page.coverage.complete and page.coverage.remote_complete


async def test_until_filter_never_skips_unfetched_messages(services: Services, fake: FakeTeams) -> None:
    long_chat(fake, 300)
    until = at("2026-06-05T00:00:00")  # the newest 3 remote pages hold nothing in range
    collected: list[str] = []
    page = await services.history.get_messages(ONE, until=until, page_size=20)
    while True:
        collected = [m.id for m in page.items] + collected
        if not page.cursor:
            break
        page = await services.history.get_messages(ONE, cursor=page.cursor)
    assert collected == [f"m{i:03d}" for i in range(97)]  # m096 is exactly at the inclusive bound


async def test_since_until_and_message_limit_select_the_latest_n(services: Services, fake: FakeTeams) -> None:
    long_chat(fake)
    page = await services.history.get_messages(
        ONE, since=at("2026-06-02T00:00:00"), until=at("2026-06-03T23:59:59"), message_limit=30, page_size=20
    )
    assert [m.id for m in page.items] == [f"m{i:03d}" for i in range(52, 72)]
    rest = await services.history.get_messages(ONE, cursor=page.cursor)
    assert [m.id for m in rest.items] == [f"m{i:03d}" for i in range(42, 52)]
    assert rest.cursor is None and rest.coverage.message_limit_reached


async def test_history_is_read_from_teams_on_every_call(services: Services, fake: FakeTeams) -> None:
    await services.history.get_messages(ONE)
    removed = fake.chats[ONE].messages.pop(0)  # gone from Teams: nothing keeps a copy
    page = await services.history.get_messages(ONE)
    assert removed["id"] not in [m.id for m in page.items]


async def test_is_self_and_attachments(services: Services) -> None:
    page = await services.history.get_messages(ONE)
    by_id = {m.id: m for m in page.items}
    assert by_id["1002"].is_self is True and by_id["1001"].is_self is False
    attachment = by_id["1003"].attachments[0]
    assert attachment.name == "notes.txt" and "[File: notes.txt]" in by_id["1003"].text


async def test_a_page_holds_whole_messages_and_the_cursor_continues_with_older_ones(
    services: Services, fake: FakeTeams
) -> None:
    fake.chats[ONE].messages = [
        cs_message("old", "2026-09-01T00:00:00.000Z", "a" * 3000),
        cs_message("mid", "2026-09-02T00:00:00.000Z", "b" * 3000),
        cs_message("new", "2026-09-03T00:00:00.000Z", "c" * 3000),
    ]
    first = await services.history.get_messages(ONE, max_chars=4000)
    assert [m.id for m in first.items] == ["new"] and first.cursor and not first.coverage.complete
    assert first.items[0].text == "c" * 3000
    second = await services.history.get_messages(ONE, cursor=first.cursor)
    assert [m.id for m in second.items] == ["mid"] and second.cursor
    third = await services.history.get_messages(ONE, cursor=second.cursor)
    assert [m.id for m in third.items] == ["old"] and third.cursor is None and third.coverage.complete


async def test_a_message_larger_than_the_budget_is_returned_whole(
    services: Services, fake: FakeTeams
) -> None:
    fake.chats[ONE].messages = [cs_message("big", "2026-09-01T00:00:00.000Z", "x" * 9000)]
    page = await services.history.get_messages(ONE, max_chars=4000)
    assert [len(m.text) for m in page.items] == [9000] and page.cursor is None


async def test_channel_history_comes_from_graph_threads(services: Services, fake: FakeTeams) -> None:
    page = await services.history.get_messages(CHANNEL)
    assert [(m.id, m.reply_to_id) for m in page.items] == [("5003", None), ("5001", None), ("5002", "5001")]
    root = page.items[1]
    assert root.subject == "Launch" and root.text == "Launch plan" and root.author == "Pat Peer"
    assert not any(c.startswith("GET emea.ng.msg") and "general" in c for c in fake.calls)


async def test_channel_replies_inside_the_range_are_kept_and_old_threads_stop_paging(
    services: Services, fake: FakeTeams
) -> None:
    channel = fake.channels[CHANNEL]
    channel.threads += [
        graph_message(f"old{i}", f"2025-01-{1 + i % 28:02d}T{i // 28:02d}:00:00Z") for i in range(150)
    ]
    page = await services.history.get_messages(CHANNEL, since=at("2026-09-02T00:00:00"))
    assert [m.id for m in page.items] == ["5002"]  # the reply, although its root is older
    assert page.coverage.complete
    # page 2 holds only 2025 threads, so pages 3 and 4 are not read
    assert sum("channels/19:general@thread.tacv2/messages" in c for c in fake.calls) == 2


async def test_deleted_channel_post_shows_no_text(services: Services, fake: FakeTeams) -> None:
    fake.channels[CHANNEL].threads[1] = graph_message(
        "5003", "2026-08-01T12:00:00Z", deleted="2026-09-10T00:00:00Z"
    )
    page = await services.history.get_messages(CHANNEL)
    post = next(m for m in page.items if m.id == "5003")
    assert post.is_deleted and "Old post" not in post.text


async def test_invalid_requests(services: Services) -> None:
    with pytest.raises(InvalidRequest):
        await services.history.get_messages(
            ONE, since=at("2026-02-01T00:00:00"), until=at("2026-01-01T00:00:00")
        )
    with pytest.raises(InvalidRequest):
        await services.history.get_messages(ONE, since=datetime(2026, 1, 1))  # no timezone


async def test_the_cursor_skips_new_arrivals_that_push_its_marker_past_the_first_page(
    services: Services, fake: FakeTeams
) -> None:
    long_chat(fake)
    first = await services.history.get_messages(ONE, page_size=20)
    assert [m.id for m in first.items] == [f"m{i:03d}" for i in range(100, 120)] and first.cursor
    fake.chats[ONE].messages += [
        cs_message(f"n{i:03d}", f"2026-07-{1 + i // 24:02d}T{i % 24:02d}:00:00.000Z", f"New {i}")
        for i in range(150)
    ]  # newer than everything returned: the marked message is now on the second remote page

    seen: list[str] = []
    page = await services.history.get_messages(ONE, cursor=first.cursor)
    while True:
        seen = [m.id for m in page.items] + seen
        if not page.cursor:
            break
        page = await services.history.get_messages(ONE, cursor=page.cursor)
    assert seen == [f"m{i:03d}" for i in range(100)]  # nothing repeated, nothing new mixed in


class _RepeatingReader:
    """A history reader whose continuation always points back at the same page."""

    async def page(self, chat: object, **_: object) -> HistoryPage:
        return HistoryPage(messages=[], next_link="https://emea.ng.msg.teams.microsoft.com/same", scanned=0)


async def test_an_export_stops_when_teams_repeats_a_continuation(services: Services) -> None:
    chat = await services.catalog.get(ONE)
    history = History(services.catalog, _RepeatingReader(), _RepeatingReader(), "me")  # type: ignore[arg-type]
    with pytest.raises(Upstream):
        async for _ in history.selection(chat, since=None, until=None):
            pass
