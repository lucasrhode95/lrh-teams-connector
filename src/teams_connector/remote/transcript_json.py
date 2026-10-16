"""Teams' transcript JSON (Graph's ``media/transcripts/{id}/content?format=json``) as a domain transcript.

Each entry is one utterance with its speaker's display name and its start and end offsets, written as
``HH:MM:SS.fffffff`` from the start of the recording. The ``events`` list holds the transcript's own start and
stop. Entries without text are skipped; an unknown or malformed field is ignored rather than failing the whole
transcript (docs/api-research.md, "Transcripts", live 2026-10-16). Pure: no network.
"""

from __future__ import annotations

import re
from typing import Any

from teams_connector.domain.models import Transcript, TranscriptEntry, TranscriptEvent

_OFFSET = re.compile(r"^(\d+):(\d{1,2}):(\d{1,2}(?:\.\d+)?)$")


def transcript(payload: Any) -> Transcript:
    """The transcript of a JSON document; an empty transcript when the document has no entries."""
    if not isinstance(payload, dict):
        return Transcript()
    entries = [entry for item in _list(payload.get("entries")) if (entry := _entry(item)) is not None]
    events = [event for item in _list(payload.get("events")) if (event := _event(item)) is not None]
    language = next(
        (
            item.get("spokenLanguageTag")
            for item in _list(payload.get("entries"))
            if isinstance(item, dict) and isinstance(item.get("spokenLanguageTag"), str)
        ),
        None,
    )
    return Transcript(language=language, entries=entries, events=events)


def seconds(value: Any) -> float | None:
    """A transcript offset in seconds, or None when it is not in the ``HH:MM:SS.fffffff`` form."""
    if not isinstance(value, str):
        return None
    match = _OFFSET.match(value.strip())
    if not match:
        return None
    hours, minutes, rest = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(rest)


def _entry(item: Any) -> TranscriptEntry | None:
    if not isinstance(item, dict):
        return None
    text = item.get("text")
    start = seconds(item.get("startOffset"))
    if not isinstance(text, str) or not text.strip() or start is None:
        return None
    speaker = item.get("speakerDisplayName")
    return TranscriptEntry(
        speaker=speaker.strip() if isinstance(speaker, str) and speaker.strip() else None,
        start=start,
        text=" ".join(text.split()),
    )


def _event(item: Any) -> TranscriptEvent | None:
    if not isinstance(item, dict):
        return None
    start = seconds(item.get("startOffset"))
    kind = item.get("eventType")
    if start is None or not isinstance(kind, str) or not kind:
        return None
    return TranscriptEvent(kind=kind, start=start)


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []
