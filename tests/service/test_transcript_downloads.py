"""Downloading a meeting's transcript as a text file, through the real services and the fake tenant.
Synthetic meetings only."""

from __future__ import annotations

import pytest

from teams_connector.bootstrap import Services
from teams_connector.domain.errors import ConnectorError, NotFound
from tests.fakes.teams_fake import FakeTeams, add_meeting

CHAT = "19:one@unq.gbl.spaces"
DRIVE = "b!SYNTH-DRIVE"
DOCUMENT = {
    "entries": [
        {
            "speakerDisplayName": "Synthetic Ann",
            "startOffset": "00:00:17.6950000",
            "text": "Good morning.",
            "spokenLanguageTag": "en-us",
        },
        {"speakerDisplayName": "Synthetic Ann", "startOffset": "00:00:25.0000000", "text": "Agenda first."},
        {"speakerDisplayName": "Synthetic Ben", "startOffset": "00:01:02.5000000", "text": "Second point."},
    ],
    "events": [
        {"eventType": "TranscriptStarted", "startOffset": "00:00:00"},
        {"eventType": "TranscriptStopped", "startOffset": "00:05:42.5350000"},
    ],
}


async def test_a_transcript_downloads_as_text_with_speakers_and_times(
    services: Services, fake: FakeTeams
) -> None:
    add_meeting(
        fake,
        chat_id=CHAT,
        title="Synthetic Standup",
        when="2026-09-03T10:00:00Z",
        drive=DRIVE,
        item="01DL1",
        transcript_id="t-dl-1",
        document=DOCUMENT,
    )
    await services.catalog.refresh()

    result = await services.files.download_attachments(CHAT, message_id="rec-01DL1", sent_at=None, names=None)

    [saved] = result.files
    assert saved.name == "Synthetic Standup.txt"
    with open(saved.path, encoding="utf-8") as handle:
        text = handle.read()
    assert text.startswith("Transcript: Synthetic Standup\nSpeakers: 2\nLength: 5:42\nLanguage: en-us\n")
    assert "Read from Teams at " in text
    assert "Synthetic Ann  0:17\nGood morning.\nAgenda first.\n\nSynthetic Ben  1:02\nSecond point.\n" in text
    assert result.unavailable == []


async def test_a_refused_transcript_is_reported_and_nothing_is_saved(
    services: Services, fake: FakeTeams
) -> None:
    add_meeting(
        fake,
        chat_id=CHAT,
        title="Synthetic Standup",
        when="2026-09-03T10:00:00Z",
        drive=DRIVE,
        item="01DL2",
        transcript_id="t-dl-2",
        document=DOCUMENT,
        denied=True,
    )
    await services.catalog.refresh()

    with pytest.raises(ConnectorError):
        await services.files.download_attachments(CHAT, message_id="rec-01DL2", sent_at=None, names=None)


async def test_a_recording_without_a_transcript_has_nothing_to_download(
    services: Services, fake: FakeTeams
) -> None:
    add_meeting(
        fake,
        chat_id=CHAT,
        title="Synthetic Standup",
        when="2026-09-03T10:00:00Z",
        drive=DRIVE,
        item="01DL3",
        transcript_id=None,
    )
    await services.catalog.refresh()

    with pytest.raises(NotFound):
        await services.files.download_attachments(CHAT, message_id="rec-01DL3", sent_at=None, names=None)
