"""Recording cards: the recording and transcript a card names, from synthetic card markup only."""

from __future__ import annotations

from teams_connector.domain.models import RecordingRef
from teams_connector.remote import recording_cards


def _card(*items: str, title: str = "Synthetic Standup") -> str:
    return (
        '<URIObject type="Video.2/CallRecording.1" uri="https://example.invalid/card">'
        f"<Title>{title}</Title>"
        '<a href="https://tenant.sharepoint.com/:v:/p/owner/SYNTHETIC">recording</a>'
        f"<RecordingContent>{''.join(items)}</RecordingContent></URIObject>"
    )


VIDEO = (
    '<item type="onedriveForBusinessVideo" driveId="b!SYNTH-DRIVE" '
    'driveItemId="01SYNTHITEM" id="v1" uri="x"/>'
)
TRANSCRIPT = (
    '<item type="onedriveForBusinessTranscript" driveId="b!SYNTH-DRIVE" driveItemId="01SYNTHITEM" '
    'id="t-synth-1" uri="x"/>'
)
AMS_TRANSCRIPT = '<item type="amsTranscript" id="a1" uri="y"/>'


def test_a_card_names_its_recording_and_transcript() -> None:
    ref = recording_cards.recording(_card(VIDEO, TRANSCRIPT, AMS_TRANSCRIPT))

    assert ref == RecordingRef(
        drive_id="b!SYNTH-DRIVE", item_id="01SYNTHITEM", title="Synthetic Standup", transcript_id="t-synth-1"
    )
    assert recording_cards.has_transcript(_card(VIDEO, TRANSCRIPT, AMS_TRANSCRIPT))


def test_a_card_without_a_transcript_names_only_the_recording() -> None:
    content = _card(VIDEO)

    ref = recording_cards.recording(content)

    assert ref is not None and ref.transcript_id is None
    assert not recording_cards.has_transcript(content)


def test_a_card_whose_video_is_not_exported_yet_names_nothing() -> None:
    assert recording_cards.recording(_card(TRANSCRIPT)) is None


def test_other_content_is_not_a_recording() -> None:
    assert recording_cards.recording("<p>Hello</p>") is None
    assert recording_cards.has_transcript("<p>Hello</p>") is False


def test_title_is_read_as_text_and_missing_when_empty() -> None:
    assert recording_cards.title(_card(VIDEO, title="Planning &amp; review")) == "Planning & review"
    assert recording_cards.title(_card(VIDEO, title="   ")) is None
