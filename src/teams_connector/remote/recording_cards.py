"""Meeting recording cards: which recording a Teams message card names, and whether it has a transcript.

A recording card (``RichText/Media_CallRecording``, a ``Video.2/CallRecording.1`` URIObject) lists the
recording's OneDrive or SharePoint item as an ``onedriveForBusinessVideo`` entry with ``driveId`` and
``driveItemId``. When Teams made a transcript, the card has an ``onedriveForBusinessTranscript`` entry (its
``id`` is the transcript's id, and it names the same drive and item) and an ``amsTranscript`` entry. So the
card alone identifies the recording and its transcript, with no other request (docs/api-research.md,
"Transcripts", live 2026-10-16). Pure: no network.
"""

from __future__ import annotations

import html
import re

from teams_connector.domain.models import RecordingRef

_CARD_TYPE = "video.2/callrecording"
_ITEM = re.compile(r"<item\b([^>]*?)/?>", re.IGNORECASE)
_ATTRIBUTE = re.compile(r'([\w:-]+)\s*=\s*"([^"]*)"')
_TITLE = re.compile(r"<Title\b[^>]*>(.*?)</Title\s*>", re.IGNORECASE | re.DOTALL)
_TRANSCRIPT_TYPES = ("onedriveforbusinesstranscript", "amstranscript")


def recording(content: str) -> RecordingRef | None:
    """The recording a card names, or None when the content is not a recording card or its video is not on
    OneDrive or SharePoint yet (Teams is still exporting it)."""
    if _CARD_TYPE not in content.casefold():
        return None
    video = _item_of_type(content, "onedriveforbusinessvideo")
    if video is None or not video.get("driveid") or not video.get("driveitemid"):
        return None
    transcript = _item_of_type(content, "onedriveforbusinesstranscript")
    return RecordingRef(
        drive_id=video["driveid"],
        item_id=video["driveitemid"],
        title=title(content),
        transcript_id=(transcript or {}).get("id") or None,
    )


def has_transcript(content: str) -> bool:
    """Whether the card says Teams made a transcript of the recording (no request is made to check it)."""
    return any(_item_of_type(content, kind) is not None for kind in _TRANSCRIPT_TYPES)


def title(content: str) -> str | None:
    """The recording's title as the card states it, or None."""
    match = _TITLE.search(content)
    if not match:
        return None
    text = " ".join(html.unescape(re.sub(r"<[^>]*>", "", match.group(1))).split())
    return text or None


def _item_of_type(content: str, kind: str) -> dict[str, str] | None:
    """The attributes (names in lower case, values unescaped) of the first card item of ``kind``."""
    for match in _ITEM.finditer(content):
        attributes = {
            name.casefold(): html.unescape(value) for name, value in _ATTRIBUTE.findall(match.group(1))
        }
        if attributes.get("type", "").casefold() == kind:
            return attributes
    return None
