"""Graph transcript reads and video search, through the fake tenant. Synthetic data only."""

from __future__ import annotations

import httpx
import pytest

from teams_connector.domain.errors import ConnectorError, Upstream
from teams_connector.domain.models import RecordingRef
from teams_connector.remote.graph import Graph
from teams_connector.remote.graph_transcripts import GraphTranscripts, GraphVideoSearch
from teams_connector.remote.transport import Bearer, Transport
from tests.fakes.teams_fake import FakeTeams, FakeTokens, add_meeting, sample_tenant

DRIVE = "b!SYNTH-DRIVE"
DOCUMENT = {
    "entries": [
        {
            "speakerDisplayName": "Synthetic Ann",
            "startOffset": "00:00:17.6950000",
            "text": "Good morning.",
            "spokenLanguageTag": "en-us",
        }
    ],
    "events": [{"eventType": "TranscriptStarted", "startOffset": "00:00:00"}],
}


@pytest.fixture
def fake() -> FakeTeams:
    return sample_tenant()


def _graph(fake: FakeTeams) -> Graph:
    client = httpx.AsyncClient(transport=fake.transport())
    return Graph(Transport(client=client), Bearer(FakeTokens(), "teams.graph"))  # type: ignore[arg-type]


def _recording(item: str, transcript_id: str | None = "t-synth-1") -> RecordingRef:
    return RecordingRef(drive_id=DRIVE, item_id=item, title="Synthetic Standup", transcript_id=transcript_id)


async def test_the_default_transcript_is_read_as_entries_with_speakers(fake: FakeTeams) -> None:
    add_meeting(
        fake,
        chat_id="19:one@unq.gbl.spaces",
        title="Synthetic Standup",
        when="2026-09-03T10:00:00Z",
        drive=DRIVE,
        item="01SYNTH1",
        transcript_id="t-synth-1",
        document=DOCUMENT,
    )

    transcript = await GraphTranscripts(_graph(fake)).default(_recording("01SYNTH1"))

    assert transcript is not None
    assert [(e.speaker, e.start, e.text) for e in transcript.entries] == [
        ("Synthetic Ann", 17.695, "Good morning.")
    ]
    assert transcript.language == "en-us"


async def test_a_recording_without_a_transcript_has_none(fake: FakeTeams) -> None:
    assert await GraphTranscripts(_graph(fake)).default(_recording("01NONE", None)) is None


async def test_a_refused_transcript_read_raises_the_service_error(fake: FakeTeams) -> None:
    add_meeting(
        fake,
        chat_id="19:one@unq.gbl.spaces",
        title="Synthetic Standup",
        when="2026-09-03T10:00:00Z",
        drive=DRIVE,
        item="01DENIED",
        transcript_id="t-denied",
        denied=True,
    )

    with pytest.raises(ConnectorError):
        await GraphTranscripts(_graph(fake)).default(_recording("01DENIED"))


async def test_a_transcript_that_is_not_json_is_an_upstream_error(fake: FakeTeams) -> None:
    add_meeting(
        fake,
        chat_id="19:one@unq.gbl.spaces",
        title="Synthetic Standup",
        when="2026-09-03T10:00:00Z",
        drive=DRIVE,
        item="01HTML",
        transcript_id="t-html",
        document=b"<html>not a transcript</html>",
    )

    with pytest.raises(Upstream):
        await GraphTranscripts(_graph(fake)).default(_recording("01HTML", "t-html"))


async def test_video_search_returns_one_page_with_the_total(fake: FakeTeams) -> None:
    for number in range(3):
        fake.videos.append(
            {
                "driveId": DRIVE,
                "itemId": f"01V{number}",
                "name": f"Video {number}.mp4",
                "createdDateTime": "2026-09-03T10:00:00Z",
            }
        )

    hits, total = await GraphVideoSearch(_graph(fake)).videos('("deployment") filetype:mp4', offset=0, size=2)

    assert [hit.item_id for hit in hits] == ["01V0", "01V1"]
    assert hits[0].drive_id == DRIVE and hits[0].name == "Video 0.mp4"
    assert hits[0].created_at is not None
    assert total == 3


async def test_video_search_skips_hits_without_a_drive_item(fake: FakeTeams) -> None:
    fake.videos.append({"driveId": "", "itemId": "01BROKEN", "name": "x", "createdDateTime": None})

    hits, _ = await GraphVideoSearch(_graph(fake)).videos('("x") filetype:mp4', offset=0, size=5)

    assert hits == []
