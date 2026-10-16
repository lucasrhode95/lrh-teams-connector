"""The text export's sender label. Synthetic messages only.

A person's name shows as it is. Teams' own events and meeting cards read Teams. Other messages with no name
read Unknown sender.
"""

from __future__ import annotations

from datetime import UTC, datetime

from teams_connector.domain.models import Message, RecordingRef
from teams_connector.service.export import formatter

WHEN = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)


def _head(message: Message) -> str:
    return formatter.txt_message(message).split("\n", 1)[0]


def test_a_person_is_named() -> None:
    message = Message(id="1", conversation_id="c", author="Synthetic Ann", sent_at=WHEN, text="Hello.")

    assert _head(message) == f"[{formatter.timestamp(WHEN)}] Synthetic Ann"


def test_teams_own_events_read_teams() -> None:
    message = Message(
        id="2",
        conversation_id="c",
        sent_at=WHEN,
        text="[Meeting ended · 1m 00s]",
        system_event="Event/Call",
    )

    assert _head(message) == f"[{formatter.timestamp(WHEN)}] Teams"


def test_a_meeting_card_reads_teams() -> None:
    message = Message(
        id="3",
        conversation_id="c",
        sent_at=WHEN,
        text="[Recording: Synthetic Standup]",
        recording=RecordingRef(drive_id="b!synthetic", item_id="01SYNTH"),
    )

    assert _head(message) == f"[{formatter.timestamp(WHEN)}] Teams"


def test_any_other_message_without_a_name_reads_unknown_sender() -> None:
    message = Message(id="4", conversation_id="c", sent_at=WHEN, text="Hello.")

    assert _head(message) == f"[{formatter.timestamp(WHEN)}] Unknown sender"
