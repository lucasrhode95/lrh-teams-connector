"""Recording messages: the transcript marker, the transcript attachment, and what a caller can see of both.
Synthetic records and card markup only."""

from __future__ import annotations

from teams_connector.domain.models import RecordingRef
from teams_connector.remote import chat_service_mapping as mapping

CONVERSATION = "19:synthetic-meeting@thread.v2"
VIDEO = (
    '<item type="onedriveForBusinessVideo" driveId="b!SYNTH-DRIVE" '
    'driveItemId="01SYNTHITEM" id="v1" uri="x"/>'
)
TRANSCRIPT = (
    '<item type="onedriveForBusinessTranscript" driveId="b!SYNTH-DRIVE" driveItemId="01SYNTHITEM" '
    'id="t-synth-1" uri="x"/>'
)


def _record(*items: str) -> dict[str, object]:
    card = (
        '<URIObject type="Video.2/CallRecording.1" uri="https://example.invalid/card">'
        "<Title>Synthetic Standup</Title>"
        '<a href="https://tenant.sharepoint.com/:v:/p/owner/SYNTHETIC">recording</a>'
        f"<RecordingContent>{''.join(items)}</RecordingContent></URIObject>"
    )
    return {
        "id": "1792000000001",
        "messagetype": "RichText/Media_CallRecording",
        "contenttype": "Text",
        "content": card,
        "originalarrivaltime": "2026-10-14T14:06:23Z",
        "imdisplayname": "Synthetic Organizer",
        "from": "8:orgid:00000000-0000-4000-8000-000000000001",
        "properties": {},
    }


def test_a_recording_with_a_transcript_gets_a_marker_and_a_transcript_attachment() -> None:
    message = mapping.to_message(_record(VIDEO, TRANSCRIPT), CONVERSATION)

    assert message.text.endswith("[Transcript: Synthetic Standup]")
    [attachment] = message.attachments
    assert attachment.source == "transcript"
    assert attachment.name == "Synthetic Standup"
    assert attachment.source_id == "t-synth-1"
    assert message.recording == RecordingRef(
        drive_id="b!SYNTH-DRIVE", item_id="01SYNTHITEM", title="Synthetic Standup", transcript_id="t-synth-1"
    )


def test_a_recording_without_a_transcript_has_no_transcript_marker_or_attachment() -> None:
    message = mapping.to_message(_record(VIDEO), CONVERSATION)

    assert "[Transcript" not in message.text
    assert [item.source for item in message.attachments] == []
    assert message.recording is not None and message.recording.item_id == "01SYNTHITEM"


def test_serialized_messages_do_not_expose_the_recording_reference() -> None:
    message = mapping.to_message(_record(VIDEO, TRANSCRIPT), CONVERSATION)

    dumped = message.model_dump(mode="json")
    assert "recording" not in dumped
    assert all("recording" not in item for item in dumped["attachments"])
    assert "b!SYNTH-DRIVE" not in message.model_dump_json()


def test_a_transcript_name_does_not_clash_with_a_file_of_the_same_name() -> None:
    raw = _record(VIDEO, TRANSCRIPT)
    raw["properties"] = {
        "files": '[{"fileName": "Synthetic Standup", "fileType": "txt", "fileInfo": {"shareUrl": '
        '"https://tenant.sharepoint.com/:t:/p/owner/SYNTH"}}]'
    }

    message = mapping.to_message(raw, CONVERSATION)

    assert sorted(item.name for item in message.attachments) == ["Synthetic Standup", "Synthetic Standup (2)"]
    assert "[Transcript: Synthetic Standup (2)]" in message.text
