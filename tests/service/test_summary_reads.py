"""Teams' AI summary in get_messages and transcript search: the flag, one read per meeting, the share of
max_chars, the cursor, and a failed read. Real services and the fake tenant; synthetic meetings."""

from __future__ import annotations

from typing import Any

from teams_connector.bootstrap import Services
from teams_connector.domain.models import Message
from teams_connector.service.summaries import SUMMARY_LABEL, SUMMARY_UNAVAILABLE
from tests.fakes.teams_fake import FakeTeams, add_meeting, synthetic_drive

CHAT = "19:one@unq.gbl.spaces"
DRIVE = synthetic_drive(
    "11111111-2222-3333-4444-555555555555",
    "66666666-7777-8888-9999-aaaaaaaaaaaa",
    "bbbbbbbb-cccc-dddd-eeee-ffffffffffff",
)
STANDUP_MARKER = "[Transcript: Synthetic Standup]"
SUMMARY = "Synthetic summary: the deployment moves to Friday."
DOCUMENT: dict[str, Any] = {
    "entries": [
        {
            "speakerDisplayName": "Synthetic Ann",
            "startOffset": "00:00:17.6950000",
            "text": "The deployment moves.",
            "spokenLanguageTag": "en-us",
        },
        {
            "speakerDisplayName": "Synthetic Ben",
            "startOffset": "00:01:02.5000000",
            "text": "Another deployment note.",
            "spokenLanguageTag": "en-us",
        },
    ],
    "events": [],
}


def _meeting(
    fake: FakeTeams,
    *,
    item: str,
    title: str,
    when: str,
    summary: str | None,
    document: dict[str, Any] | None = None,
) -> None:
    add_meeting(
        fake,
        chat_id=CHAT,
        title=title,
        when=when,
        drive=DRIVE,
        item=item,
        transcript_id=f"t-{item}",
        document=document or DOCUMENT,
        summary=summary,
    )


def _marked(messages: list[Message]) -> str:
    """The text of the message that carries the standup's transcript marker."""
    [text] = [message.text for message in messages if STANDUP_MARKER in message.text]
    return text


async def test_get_messages_puts_the_summary_after_the_marker(services: Services, fake: FakeTeams) -> None:
    _meeting(fake, item="01GM1", title="Synthetic Standup", when="2026-12-30T10:00:00Z", summary=SUMMARY)
    await services.catalog.refresh()

    page = await services.history.get_messages(CHAT)

    text = _marked(page.items)
    assert text.index(STANDUP_MARKER) < text.index(SUMMARY_LABEL) < text.index(SUMMARY)
    assert len(fake.summary_reads) == 1


async def test_the_flag_leaves_the_summary_out_of_get_messages(services: Services, fake: FakeTeams) -> None:
    _meeting(fake, item="01GM2", title="Synthetic Standup", when="2026-12-30T10:00:00Z", summary=SUMMARY)
    await services.catalog.refresh()

    page = await services.history.get_messages(CHAT, include_summaries=False)

    text = _marked(page.items)
    assert STANDUP_MARKER in text and SUMMARY_LABEL not in text
    assert fake.summary_reads == []


async def test_a_failed_read_is_said_so_and_the_message_stays(services: Services, fake: FakeTeams) -> None:
    _meeting(fake, item="01GM3", title="Synthetic Standup", when="2026-12-30T10:00:00Z", summary=SUMMARY)
    await services.catalog.refresh()
    fake.summary_status = 403

    page = await services.history.get_messages(CHAT)

    text = _marked(page.items)
    assert SUMMARY_UNAVAILABLE in text and SUMMARY_LABEL not in text


async def test_the_cursor_keeps_the_flag(services: Services, fake: FakeTeams) -> None:
    _meeting(fake, item="01GM4", title="Synthetic Standup", when="2026-12-30T10:00:00Z", summary=SUMMARY)
    await services.catalog.refresh()

    page = await services.history.get_messages(CHAT, page_size=1, include_summaries=False)
    messages = list(page.items)
    while page.cursor:
        page = await services.history.get_messages(CHAT, cursor=page.cursor)
        messages += page.items

    text = _marked(messages)
    assert SUMMARY_LABEL not in text
    assert fake.summary_reads == []


async def test_the_summary_counts_toward_max_chars(services: Services, fake: FakeTeams) -> None:
    long_summary = "Synthetic summary sentence. " * 120  # about 3,300 characters
    _meeting(fake, item="01GM5", title="Synthetic Standup", when="2026-12-31T10:00:00Z", summary=long_summary)
    _meeting(fake, item="01GM6", title="Synthetic Review", when="2026-12-30T10:00:00Z", summary=long_summary)
    await services.catalog.refresh()

    page = await services.history.get_messages(CHAT, max_chars=4000)

    summarized = [message for message in page.items if SUMMARY_LABEL in message.text]
    assert len(summarized) == 1 and page.cursor is not None


async def test_a_search_lists_each_matched_meeting_once_with_its_summary(
    services: Services, fake: FakeTeams
) -> None:
    _meeting(fake, item="01GS1", title="Synthetic Standup", when="2026-12-30T10:00:00Z", summary=SUMMARY)
    _meeting(
        fake,
        item="01GS2",
        title="Synthetic Review",
        when="2026-12-31T10:00:00Z",
        summary="Synthetic review summary.",
    )
    await services.catalog.refresh()

    result = await services.transcript_search.search(["deployment"], conversation_ids=[CHAT])

    summaries = {entry.meeting: entry.summary for entry in result.summaries}
    assert summaries == {"Synthetic Standup": SUMMARY, "Synthetic Review": "Synthetic review summary."}
    assert len(fake.summary_reads) == 2


async def test_a_meeting_with_several_matches_is_read_once(services: Services, fake: FakeTeams) -> None:
    _meeting(fake, item="01GS3", title="Synthetic Standup", when="2026-12-30T10:00:00Z", summary=SUMMARY)
    await services.catalog.refresh()

    result = await services.transcript_search.search(["deployment"], conversation_ids=[CHAT])

    assert len(result.items) == 2
    assert len(result.summaries) == 1
    assert len(fake.summary_reads) == 1


async def test_the_flag_leaves_search_summaries_out(services: Services, fake: FakeTeams) -> None:
    _meeting(fake, item="01GS4", title="Synthetic Standup", when="2026-12-30T10:00:00Z", summary=SUMMARY)
    await services.catalog.refresh()

    result = await services.transcript_search.search(
        ["deployment"], conversation_ids=[CHAT], include_summaries=False
    )

    assert result.items
    assert result.summaries == []
    assert fake.summary_reads == []


async def test_a_failed_search_summary_read_leaves_the_matches_alone(
    services: Services, fake: FakeTeams
) -> None:
    _meeting(fake, item="01GS5", title="Synthetic Standup", when="2026-12-30T10:00:00Z", summary=SUMMARY)
    await services.catalog.refresh()
    fake.summary_status = 403

    result = await services.transcript_search.search(["deployment"], conversation_ids=[CHAT])

    assert result.items
    assert [entry.unavailable for entry in result.summaries] == [True]
    assert result.coverage.complete is True
    assert any("could not be read" in note for note in result.coverage.notes)


async def test_the_summaries_follow_the_returned_matches(services: Services, fake: FakeTeams) -> None:
    _meeting(fake, item="01GS6", title="Synthetic Standup", when="2026-12-30T10:00:00Z", summary=SUMMARY)
    _meeting(
        fake,
        item="01GS7",
        title="Synthetic Review",
        when="2026-12-31T10:00:00Z",
        summary="Synthetic review summary.",
    )
    await services.catalog.refresh()

    result = await services.transcript_search.search(["deployment"], conversation_ids=[CHAT], limit=1)

    assert len(result.items) == 1
    assert [entry.meeting for entry in result.summaries] == [result.items[0].meeting]
    assert len(fake.summary_reads) == 1
