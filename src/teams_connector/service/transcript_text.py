"""Meeting transcripts as text: the file a download or an export writes, and how a search term matches an
utterance. Pure: no network.

A term is one phrase: each of its words must start a word of the utterance, in order, and words are compared
without case or accents (``budget`` finds ``Budgets``; ``execucao`` finds ``execução``). Like Search v2's
matching, ``udget`` finds nothing. A phrase is matched within one utterance, not across two.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import UTC, datetime

from teams_connector.domain.models import Transcript, TranscriptEntry

_WORD = re.compile(r"\w+")


def render(title: str, transcript: Transcript, *, read_at: datetime) -> str:
    """The transcript as text: a header (meeting, speakers, length, language, when it was read), then the
    utterances, grouped while the speaker stays the same, each group headed by its speaker and time."""
    speakers = {entry.speaker for entry in transcript.entries if entry.speaker}
    lines = [
        f"Transcript: {title}",
        f"Speakers: {len(speakers)}",
        f"Length: {offset_label(_length(transcript))}",
    ]
    if transcript.language:
        lines.append(f"Language: {transcript.language}")
    lines.append(f"Read from Teams at {_stamp(read_at)}")
    blocks = [_block(group) for group in _groups(transcript.entries)]
    return "\n".join(lines) + "\n\n" + "\n\n".join(blocks) + ("\n" if blocks else "")


def offset_label(seconds: float) -> str:
    """A time into the recording as the Teams transcript shows it: 0:17, or 1:02:03 from an hour on."""
    whole = int(seconds)
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def fold(text: str) -> str:
    """Text for comparison: case folded, accents removed."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(char for char in decomposed if not unicodedata.combining(char)).casefold()


def matches(term: str, text: str) -> bool:
    """Whether every word of ``term`` starts a word of ``text``, in order and next to each other."""
    phrase = _WORD.findall(fold(term))
    words = _WORD.findall(fold(text))
    if not phrase:
        return False
    return any(
        all(words[start + offset].startswith(part) for offset, part in enumerate(phrase))
        for start in range(len(words) - len(phrase) + 1)
    )


def _length(transcript: Transcript) -> float:
    stopped = [event.start for event in transcript.events if event.kind == "TranscriptStopped"]
    if stopped:
        return max(stopped)
    return max((entry.start for entry in transcript.entries), default=0.0)


def _groups(entries: list[TranscriptEntry]) -> list[list[TranscriptEntry]]:
    groups: list[list[TranscriptEntry]] = []
    for entry in entries:
        if groups and groups[-1][0].speaker == entry.speaker:
            groups[-1].append(entry)
        else:
            groups.append([entry])
    return groups


def _block(group: list[TranscriptEntry]) -> str:
    head = f"{group[0].speaker or 'Unknown speaker'}  {offset_label(group[0].start)}"
    return "\n".join([head, *(entry.text for entry in group)])


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
