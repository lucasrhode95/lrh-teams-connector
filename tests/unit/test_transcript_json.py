"""Transcript JSON: entries, offsets and events, from a synthetic document."""

from __future__ import annotations

import pytest

from teams_connector.remote import transcript_json

DOCUMENT = {
    "version": "synthetic",
    "entries": [
        {
            "speakerDisplayName": "Synthetic Ann",
            "startOffset": "00:00:17.6950000",
            "endOffset": "00:00:20.0000000",
            "text": "Good   morning.",
            "spokenLanguageTag": "en-us",
        },
        {
            "speakerDisplayName": "Synthetic Ben",
            "startOffset": "00:01:02.5000000",
            "text": "Second point.",
            "spokenLanguageTag": "en-us",
        },
        {"speakerDisplayName": "Synthetic Ben", "startOffset": "00:01:03.0000000", "text": "   "},
        {"speakerDisplayName": None, "startOffset": "not a time", "text": "Dropped."},
    ],
    "events": [
        {"eventType": "TranscriptStarted", "startOffset": "00:00:00"},
        {"eventType": "TranscriptStopped", "startOffset": "01:05:00.1000000"},
    ],
}


def test_entries_keep_speaker_offset_and_text_and_drop_empty_or_untimed_ones() -> None:
    result = transcript_json.transcript(DOCUMENT)

    assert [(e.speaker, e.start, e.text) for e in result.entries] == [
        ("Synthetic Ann", 17.695, "Good morning."),
        ("Synthetic Ben", 62.5, "Second point."),
    ]
    assert result.language == "en-us"


def test_events_keep_their_kind_and_time() -> None:
    result = transcript_json.transcript(DOCUMENT)

    assert [(e.kind, e.start) for e in result.events] == [
        ("TranscriptStarted", 0.0),
        ("TranscriptStopped", 3900.1),
    ]


@pytest.mark.parametrize("payload", [None, [], {}, {"entries": "nope"}])
def test_a_document_without_usable_entries_is_an_empty_transcript(payload: object) -> None:
    result = transcript_json.transcript(payload)

    assert result.entries == [] and result.events == [] and result.language is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [("00:00:17.6950000", 17.695), ("1:02:03", 3723.0), ("00:00:00", 0.0), ("17", None), (None, None)],
)
def test_offsets_are_seconds_from_the_recording_start(value: object, expected: float | None) -> None:
    assert transcript_json.seconds(value) == expected
