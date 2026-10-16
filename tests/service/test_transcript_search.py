"""Transcript search through the real services and the fake tenant: in conversations, across meetings, and the
checks that keep a non-meeting video out. Synthetic meetings and terms only."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from teams_connector.bootstrap import Services
from teams_connector.domain.errors import InvalidRequest, Upstream
from tests.fakes.teams_fake import FakeTeams, add_meeting, cs_message

CHAT = "19:one@unq.gbl.spaces"
DRIVE = "b!SYNTH-DRIVE"


def _document(*lines: tuple[str, str, str]) -> dict[str, object]:
    """A transcript document: (speaker, offset, text) per utterance."""
    return {
        "entries": [
            {"speakerDisplayName": speaker, "startOffset": offset, "text": text, "spokenLanguageTag": "en-us"}
            for speaker, offset, text in lines
        ],
        "events": [],
    }


STANDUP = _document(
    ("Synthetic Ann", "00:00:17.6950000", "Ship the 1.0 deployment today."),
    ("Synthetic Ben", "00:01:02.5000000", "Execução do plano is next."),
    ("Synthetic Ben", "00:02:00.0000000", "The deployment slipped."),
)
OTHER = _document(
    ("Synthetic Cy", "00:00:05.0000000", "deployment 1.0 is a later date."),
    ("Synthetic Cy", "00:00:09.0000000", "Nothing to see here."),
)


async def _meetings(services: Services, fake: FakeTeams) -> None:
    add_meeting(
        fake,
        chat_id=CHAT,
        title="Synthetic Standup",
        when="2026-09-10T10:00:00Z",
        drive=DRIVE,
        item="01TS1",
        transcript_id="t-ts-1",
        document=STANDUP,
    )
    add_meeting(
        fake,
        chat_id=CHAT,
        title="Synthetic Review",
        when="2026-09-20T10:00:00Z",
        drive=DRIVE,
        item="01TS2",
        transcript_id="t-ts-2",
        document=OTHER,
    )
    await services.catalog.refresh()


async def test_in_conversations_finds_utterances_with_speaker_time_and_meeting(
    services: Services, fake: FakeTeams
) -> None:
    await _meetings(services, fake)

    result = await services.transcript_search.search(["deployment"], conversation_ids=[CHAT])

    found = [(h.meeting, h.speaker, h.offset, h.text) for h in result.items]
    assert ("Synthetic Standup", "Synthetic Ann", "0:17", "Ship the 1.0 deployment today.") in found
    assert ("Synthetic Standup", "Synthetic Ben", "2:00", "The deployment slipped.") in found
    assert ("Synthetic Review", "Synthetic Cy", "0:05", "deployment 1.0 is a later date.") in found
    assert all(h.conversation_id == CHAT and h.message_id.startswith("rec-") for h in result.items)
    assert result.coverage.mode == "conversations"
    assert result.coverage.complete is True and result.coverage.recordings == 2


async def test_a_phrase_matches_its_words_in_order(services: Services, fake: FakeTeams) -> None:
    await _meetings(services, fake)

    in_order = await services.transcript_search.search(["1.0 deployment"], conversation_ids=[CHAT])
    reversed_order = await services.transcript_search.search(["deployment 1.0"], conversation_ids=[CHAT])

    assert [h.text for h in in_order.items] == ["Ship the 1.0 deployment today."]
    assert [h.text for h in reversed_order.items] == ["deployment 1.0 is a later date."]


async def test_terms_match_word_prefixes_and_ignore_case_and_accents(
    services: Services, fake: FakeTeams
) -> None:
    await _meetings(services, fake)

    prefix = await services.transcript_search.search(["deploy"], conversation_ids=[CHAT])
    accent = await services.transcript_search.search(["EXECUCAO"], conversation_ids=[CHAT])
    partial = await services.transcript_search.search(["udget"], conversation_ids=[CHAT])

    assert len(prefix.items) == 3
    assert [h.text for h in accent.items] == ["Execução do plano is next."]
    assert partial.items == []


async def test_an_unreadable_transcript_makes_the_result_incomplete(
    services: Services, fake: FakeTeams
) -> None:
    add_meeting(
        fake,
        chat_id=CHAT,
        title="Synthetic Denied",
        when="2026-09-10T10:00:00Z",
        drive=DRIVE,
        item="01TSD",
        transcript_id="t-ts-d",
        document=STANDUP,
        denied=True,
    )
    await services.catalog.refresh()

    result = await services.transcript_search.search(["deployment"], conversation_ids=[CHAT])

    assert result.items == []
    assert result.coverage.complete is False
    assert result.coverage.transcripts_unavailable == 1
    assert any("no readable transcript" in note for note in result.coverage.notes)


async def test_the_limit_stops_the_search_and_says_so(services: Services, fake: FakeTeams) -> None:
    await _meetings(services, fake)

    result = await services.transcript_search.search(["deployment"], conversation_ids=[CHAT], limit=1)

    assert len(result.items) == 1
    assert result.coverage.truncated is True and result.coverage.complete is False


async def test_dates_bound_the_meetings_searched(services: Services, fake: FakeTeams) -> None:
    await _meetings(services, fake)

    result = await services.transcript_search.search(
        ["deployment"],
        conversation_ids=[CHAT],
        since=datetime(2026, 9, 15, tzinfo=UTC),
    )

    assert {h.meeting for h in result.items} == {"Synthetic Review"}


async def test_a_channel_is_skipped_with_a_note(services: Services, fake: FakeTeams) -> None:
    await services.catalog.refresh()

    result = await services.transcript_search.search(
        ["deployment"], conversation_ids=["19:general@thread.tacv2"]
    )

    assert result.items == []
    assert any("Channel recordings are not searched yet" in note for note in result.coverage.notes)


async def test_across_meetings_keeps_only_videos_a_meeting_chat_holds(
    services: Services, fake: FakeTeams
) -> None:
    await _meetings(services, fake)
    fake.videos.clear()
    fake.videos.append(
        {
            "driveId": DRIVE,
            "itemId": "01NOTAMEETING",
            "name": "Training video.mp4",
            "createdDateTime": "2026-09-10T10:00:00Z",
        }
    )
    fake.videos.insert(
        0,
        {
            "driveId": DRIVE,
            "itemId": "01TS1",
            "name": "Synthetic Standup.mp4",
            "createdDateTime": "2026-09-10T10:00:00Z",
        },
    )

    result = await services.transcript_search.search(["deployment"])

    assert {h.meeting for h in result.items} == {"Synthetic Standup"}
    assert result.coverage.mode == "meetings"
    assert result.coverage.complete is False
    assert result.coverage.candidates == 2
    assert result.coverage.not_meetings == 1


async def test_a_video_whose_drive_item_differs_from_the_card_is_not_a_meeting(
    services: Services, fake: FakeTeams
) -> None:
    await _meetings(services, fake)
    fake.videos.clear()
    fake.videos.append(
        {
            "driveId": DRIVE,
            "itemId": "01SOMETHINGELSE",
            "name": "Synthetic Standup copy.mp4",
            "createdDateTime": "2026-09-10T10:00:00Z",
        }
    )

    result = await services.transcript_search.search(["deployment"])

    assert result.items == []
    assert result.coverage.not_meetings == 1


@pytest.mark.parametrize(
    "terms",
    [[], ["  "], ['say "hi"'], ["x" * 201], [f"t{i}" for i in range(26)]],
)
async def test_invalid_terms_are_refused_before_any_read(services: Services, terms: list[str]) -> None:
    with pytest.raises(InvalidRequest):
        await services.transcript_search.search(terms)


async def test_the_limit_and_conversation_count_are_bounded(services: Services) -> None:
    with pytest.raises(InvalidRequest):
        await services.transcript_search.search(["x"], limit=0)
    with pytest.raises(InvalidRequest):
        await services.transcript_search.search(["x"], conversation_ids=[])


async def test_one_chat_that_refuses_to_be_read_does_not_end_the_search(
    services: Services, fake: FakeTeams, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_meeting(
        fake,
        chat_id=CHAT,
        title="Synthetic Standup",
        when="2026-09-10T10:00:00Z",
        drive=DRIVE,
        item="01TS1",
        transcript_id="t-ts-1",
        document=STANDUP,
    )
    # a quieter chat active around the video's creation, which Teams refuses to read
    fake.chats["19:meeting_abc@thread.v2"].messages.append(
        cs_message("quiet-1", "2026-09-10T09:30:00Z", "Quiet chat")
    )
    fake.videos.clear()
    fake.videos.append(
        {
            "driveId": DRIVE,
            "itemId": "01TS1",
            "name": "Synthetic Standup.mp4",
            "createdDateTime": "2026-09-10T10:00:00Z",
        }
    )
    await services.catalog.refresh()
    original = services.history.selection

    def selection(chat, **kwargs):  # type: ignore[no-untyped-def]
        if chat.id == "19:meeting_abc@thread.v2":

            async def refuse():  # type: ignore[no-untyped-def]
                raise Upstream("Teams refused the chat.")
                yield  # pragma: no cover

            return refuse()
        return original(chat, **kwargs)

    monkeypatch.setattr(services.history, "selection", selection)

    result = await services.transcript_search.search(["deployment"])

    assert {h.meeting for h in result.items} == {"Synthetic Standup"}
    assert result.coverage.not_meetings == 0
    assert any("could not be checked" in note for note in result.coverage.notes)
