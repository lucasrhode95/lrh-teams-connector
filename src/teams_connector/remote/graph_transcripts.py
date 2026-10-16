"""Meeting transcripts and video search over Microsoft Graph, with the Teams client's Graph token.

A recording's drive item lists its transcripts at ``media/transcripts``; the default one's JSON
(``content?format=json``) carries the speakers and offsets. That route is not in Graph's documentation: it was
verified live on one account and tenant (2026-10-16), so a refusal surfaces as the service's error, which the
caller reports as an unavailable transcript. Video search is Graph's documented ``search/query`` over
``driveItem``. Parsing is pure and lives in ``transcript_json`` and ``recording_cards``.
"""

from __future__ import annotations

import json
from typing import Any

from teams_connector.domain.errors import Upstream
from teams_connector.domain.models import RecordingRef, Transcript
from teams_connector.remote import graph_mapping, transcript_json
from teams_connector.remote.graph import Graph, segment
from teams_connector.remote.ports import VideoHit
from teams_connector.remote.transport import operation

MAX_TRANSCRIPT_BYTES = 10 * 1024 * 1024  # a 66-cue transcript was about 20 KB (2026-10-16)


class GraphTranscripts:
    """TranscriptReader: a recording's default transcript, as entries and events."""

    def __init__(self, graph: Graph) -> None:
        self._graph = graph

    async def default(self, recording: RecordingRef) -> Transcript | None:
        base = f"/drives/{segment(recording.drive_id)}/items/{segment(recording.item_id)}/media/transcripts"
        with operation("listing a recording's transcripts"):
            listing = await self._graph.get(base)
        chosen = _choose(listing, recording.transcript_id)
        if chosen is None:
            return None
        with operation("reading a transcript"):
            body = await self._graph.download_bytes(
                f"{base}/{segment(chosen)}/content?format=json", max_bytes=MAX_TRANSCRIPT_BYTES
            )
        try:
            payload = json.loads(body.decode("utf-8-sig"))
        except (ValueError, UnicodeDecodeError):
            raise Upstream("Teams returned a transcript that is not JSON.") from None
        return transcript_json.transcript(payload)


class GraphVideoSearch:
    """VideoSearch: one page of Graph's ``search/query`` over drive items."""

    def __init__(self, graph: Graph) -> None:
        self._graph = graph

    async def videos(self, query: str, *, offset: int, size: int) -> tuple[list[VideoHit], int | None]:
        body = {
            "requests": [
                {"entityTypes": ["driveItem"], "query": {"queryString": query}, "from": offset, "size": size}
            ]
        }
        with operation("searching videos"):
            data, _ = await self._graph.post("/search/query", body, idempotent=True)
        container = _container(data)
        hits = [hit for raw in _list(container.get("hits")) if (hit := _video(raw)) is not None]
        total = container.get("total")
        return hits, total if isinstance(total, int) and not isinstance(total, bool) else None


def _choose(listing: dict[str, Any], card_transcript_id: str | None) -> str | None:
    """The transcript to read: the one the recording card names, else the one Graph flags as default."""
    rows = [
        row for row in _list(listing.get("value")) if isinstance(row, dict) and isinstance(row.get("id"), str)
    ]
    if card_transcript_id and any(row["id"] == card_transcript_id for row in rows):
        return card_transcript_id
    default = next((row["id"] for row in rows if row.get("isDefault") is True), None)
    return default


def _container(data: dict[str, Any]) -> dict[str, Any]:
    values = _list(data.get("value"))
    containers = _list(_mapping(values[0] if values else None).get("hitsContainers"))
    return _mapping(containers[0] if containers else None)


def _video(raw: Any) -> VideoHit | None:
    resource = _mapping(_mapping(raw).get("resource"))
    parent = _mapping(resource.get("parentReference"))
    drive, item = parent.get("driveId"), resource.get("id")
    if not isinstance(drive, str) or not drive or not isinstance(item, str) or not item:
        return None
    name = resource.get("name")
    return VideoHit(
        drive_id=drive,
        item_id=item,
        name=name if isinstance(name, str) else None,
        created_at=graph_mapping.created(resource),
    )


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []
