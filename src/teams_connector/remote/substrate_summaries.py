"""Teams' AI summary of a meeting, from the search recommendations route that Teams Web asks for it (the
private route in docs/api-research.md, "Meeting summary").

Teams Web names the meeting by its entity id, which is built from the recording's drive id: ``SPO_``, the
base64url of the drive's site, web and library GUIDs as text, and the drive item id. The summary is the
``MeetingSummary`` of the answer's first result. The request uses the shared ``m365.search`` sign-in, as the
other substrate calls do.
"""

from __future__ import annotations

import base64
import json
import re
import uuid
from typing import Any

from teams_connector import config
from teams_connector.domain.errors import Upstream
from teams_connector.domain.models import RecordingRef
from teams_connector.remote.ports import MeetingSummaryReader
from teams_connector.remote.transport import Credential, Transport, operation

# A drive id is "b!" and the base64url of three 16-byte GUIDs: 48 bytes, 64 characters without padding.
DRIVE_ID = re.compile(r"b!([A-Za-z0-9_-]{64})")
ITEM_ID = re.compile(r"[A-Za-z0-9]{1,256}")
ENTITY_TYPE = "MeetingCatchUp"
SCENARIO = "MeetingCatchup"


class SubstrateMeetingSummaries(MeetingSummaryReader):
    def __init__(self, transport: Transport, credential: Credential, *, anchor_mailbox: str) -> None:
        self._transport = transport
        self._credential = credential
        self._anchor_mailbox = anchor_mailbox

    async def summary(self, recording: RecordingRef) -> str | None:
        """The summary of the recording's meeting, or None when Teams has none. ``entity_id`` checks the
        recording's ids, which come from a message card, before anything is sent."""
        body = {
            "entityRequests": [
                {
                    "context": {"entityId": entity_id(recording.drive_id, recording.item_id)},
                    "queryParameters": [{"entityType": ENTITY_TYPE}],
                }
            ],
            "scenario": {"name": SCENARIO},
        }
        with operation("reading a meeting summary"):
            data = await self._transport.json(
                "POST",
                config.MEETING_SUMMARY_URL,
                credential=self._credential,
                headers={"X-AnchorMailbox": self._anchor_mailbox},
                json_body=body,
                retry=True,
            )
        return summary_text(data)


def entity_id(drive_id: str, item_id: str) -> str:
    """The meeting's entity id, built as Teams Web builds it. The drive id's payload holds the site, web and
    library GUIDs as three 16-byte blocks in GUID byte order (little-endian first fields). The entity id
    carries the same three GUIDs as text, joined by commas and base64url-encoded.

    This is the entry point for the ids. A shape Teams does not send raises the service's error, which exports
    report as the summary being unavailable.
    """
    match = DRIVE_ID.fullmatch(drive_id)
    if match is None or ITEM_ID.fullmatch(item_id) is None:
        raise Upstream("The recording's identifiers have a format this connector does not read.")
    payload = base64.urlsafe_b64decode(match.group(1))
    guids = [str(uuid.UUID(bytes_le=payload[start : start + 16])) for start in (0, 16, 32)]
    middle = base64.urlsafe_b64encode(",".join(guids).encode("ascii")).decode("ascii").rstrip("=")
    return f"SPO_{middle}_{item_id}"


def summary_text(data: Any) -> str | None:
    """The first non-empty ``MeetingSummary`` among the answer's results, or None. The same key under
    ``InsightProcessingStates`` is a processing state, not the text, and is not read."""
    for entity_set in _items(data, "EntitySets"):
        for result_set in _items(entity_set, "ResultSets"):
            for row in _items(result_set, "Results"):
                source = row.get("Source") if isinstance(row, dict) else None
                text = source.get("MeetingSummary") if isinstance(source, dict) else None
                if isinstance(text, str) and text.strip():
                    return readable_summary(text.strip())
    return None


def readable_summary(text: str) -> str | None:
    """Teams' summary as readable text.

    Teams sends a JSON document of key topics, each with a headline, a summary and detailed points. The text
    keeps those, one bullet per point, and drops citations and model fields. Text that is not that
    document is returned as it is. A document without topics has no summary.
    """
    try:
        document = json.loads(text)
    except ValueError:
        return text
    topics = document.get("KeyTopics") if isinstance(document, dict) else None
    if not isinstance(topics, list):
        return text
    blocks = [block for block in (_topic_text(topic) for topic in topics) if block]
    return "\n\n".join(blocks) or None


def _topic_text(topic: Any) -> str:
    if not isinstance(topic, dict):
        return ""
    lines = [_plain(topic.get("Headline")), _plain(topic.get("Summary"))]
    details = topic.get("DetailedSummaries")
    for detail in details if isinstance(details, list) else []:
        if not isinstance(detail, dict):
            continue
        label, body = _plain(detail.get("topic")), _plain(detail.get("text"))
        if label and body:
            lines.append(f"- {label}: {body}")
        elif label or body:
            lines.append(f"- {label or body}")
    return "\n".join(line for line in lines if line)


def _plain(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _items(container: Any, key: str) -> list[Any]:
    """The list under ``key`` of a JSON object, or an empty list when either is missing."""
    value = container.get(key) if isinstance(container, dict) else None
    return value if isinstance(value, list) else []
