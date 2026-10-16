"""Exports with meeting transcripts: the transcripts folder, the include_transcripts flag, names, and an
unavailable transcript. Through the real exports service and the fake tenant. Synthetic meetings only."""

from __future__ import annotations

import zipfile
from pathlib import Path

from teams_connector.bootstrap import Services
from teams_connector.domain.models import ExportRequest
from tests.fakes.teams_fake import FakeTeams, add_meeting, synthetic_drive

CHAT = "19:one@unq.gbl.spaces"
DRIVE = synthetic_drive(
    "11111111-2222-3333-4444-555555555555",
    "66666666-7777-8888-9999-aaaaaaaaaaaa",
    "bbbbbbbb-cccc-dddd-eeee-ffffffffffff",
)
DOCUMENT = {
    "entries": [
        {
            "speakerDisplayName": "Synthetic Ann",
            "startOffset": "00:00:17.6950000",
            "text": "Good morning.",
            "spokenLanguageTag": "en-us",
        }
    ],
    "events": [],
}


def _request(**changes: object) -> ExportRequest:
    values: dict[str, object] = {"conversation_ids": [CHAT], "format": "txt"}
    values.update(changes)
    return ExportRequest.model_validate(values)


def _members(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        return sorted(archive.namelist())


def _text(path: Path, member: str) -> str:
    with zipfile.ZipFile(path) as archive:
        return archive.read(member).decode("utf-8")


async def test_a_transcript_is_exported_into_the_transcripts_folder(
    services: Services, fake: FakeTeams
) -> None:
    add_meeting(
        fake,
        chat_id=CHAT,
        title="Synthetic Standup",
        when="2026-09-03T10:00:00Z",
        drive=DRIVE,
        item="01EX1",
        transcript_id="t-ex-1",
        document=DOCUMENT,
    )
    await services.catalog.refresh()

    artifact = await services.exports.export(_request())

    stem = Path(artifact.filename).stem
    assert artifact.filename.endswith(".zip")
    members = _members(Path(artifact.path))
    assert f"{stem}/transcripts/Synthetic Standup.txt" in members
    conversation = next(m for m in members if m.endswith(".txt") and "/" not in m)
    assert "[Transcript: Synthetic Standup]" in _text(Path(artifact.path), conversation)
    assert "Synthetic Ann  0:17\nGood morning." in _text(
        Path(artifact.path), f"{stem}/transcripts/Synthetic Standup.txt"
    )


async def test_the_flag_leaves_transcripts_and_their_markers_out(services: Services, fake: FakeTeams) -> None:
    add_meeting(
        fake,
        chat_id=CHAT,
        title="Synthetic Standup",
        when="2026-09-03T10:00:00Z",
        drive=DRIVE,
        item="01EX2",
        transcript_id="t-ex-2",
        document=DOCUMENT,
    )
    await services.catalog.refresh()

    artifact = await services.exports.export(_request(include_transcripts=False, include_attachments=False))

    assert artifact.filename.endswith(".txt")
    text = Path(artifact.path).read_text(encoding="utf-8")
    assert "Transcript" not in text


async def test_transcripts_are_exported_without_attachments_too(services: Services, fake: FakeTeams) -> None:
    add_meeting(
        fake,
        chat_id=CHAT,
        title="Synthetic Standup",
        when="2026-09-03T10:00:00Z",
        drive=DRIVE,
        item="01EX3",
        transcript_id="t-ex-3",
        document=DOCUMENT,
    )
    await services.catalog.refresh()

    artifact = await services.exports.export(_request(include_attachments=False))

    assert any(
        member.endswith("/transcripts/Synthetic Standup.txt") for member in _members(Path(artifact.path))
    )


async def test_two_transcripts_with_one_title_get_distinct_names(services: Services, fake: FakeTeams) -> None:
    for number, when in enumerate(("2026-09-03T10:00:00Z", "2026-09-10T10:00:00Z"), start=1):
        add_meeting(
            fake,
            chat_id=CHAT,
            title="Synthetic Standup",
            when=when,
            drive=DRIVE,
            item=f"01EX4{number}",
            transcript_id=f"t-ex-4{number}",
            document=DOCUMENT,
        )
    await services.catalog.refresh()

    artifact = await services.exports.export(_request())

    names = [m.rsplit("/", 1)[-1] for m in _members(Path(artifact.path)) if "/transcripts/" in m]
    assert sorted(names) == ["Synthetic Standup-2.txt", "Synthetic Standup.txt"]
    stem = Path(artifact.filename).stem
    conversation = next(m for m in _members(Path(artifact.path)) if m.endswith(".txt") and "/" not in m)
    text = _text(Path(artifact.path), conversation)
    assert "[Transcript: Synthetic Standup]" in text and "[Transcript: Synthetic Standup-2]" in text
    assert f"{stem}/transcripts/Synthetic Standup-2.txt" in _members(Path(artifact.path))


async def test_an_unavailable_transcript_is_marked_and_not_packaged(
    services: Services, fake: FakeTeams
) -> None:
    add_meeting(
        fake,
        chat_id=CHAT,
        title="Synthetic Standup",
        when="2026-09-03T10:00:00Z",
        drive=DRIVE,
        item="01EX5",
        transcript_id="t-ex-5",
        document=DOCUMENT,
        denied=True,
    )
    await services.catalog.refresh()

    artifact = await services.exports.export(_request(include_attachments=False))

    assert artifact.filename.endswith(".txt")
    assert "[Attachment unavailable: Synthetic Standup]" in Path(artifact.path).read_text(encoding="utf-8")
