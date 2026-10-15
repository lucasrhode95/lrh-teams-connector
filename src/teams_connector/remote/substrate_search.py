"""Microsoft Search v2 adapter for the verified Teams-message search operation.

The route is hosted by Outlook Web and uses the shared ``m365.search`` profile. Search discovery is
separate from Graph/Chat Service history reads; this adapter returns only fields observed in the
Search response and does not claim that a hit id is a history id.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from teams_connector import config
from teams_connector.domain.models import MessageHit
from teams_connector.remote.ports import MessageSearchPage, MessageSearchReader
from teams_connector.remote.transport import Credential, Transport, operation


class SubstrateSearch(MessageSearchReader):
    def __init__(self, transport: Transport, credential: Credential, *, anchor_mailbox: str) -> None:
        self._transport = transport
        self._credential = credential
        self._anchor_mailbox = anchor_mailbox

    async def search_messages(
        self,
        query: str,
        *,
        offset: int,
        page_size: int,
        cvid: str,
        logical_id: str,
    ) -> MessageSearchPage:
        """Serialize service-validated inputs to one app-owned Search v2 request."""
        body = {
            "Cvid": cvid,
            "LogicalId": logical_id,
            "Scenario": {"Name": "owa.react"},
            "TimeZone": "UTC",
            "TextDecorations": "Off",
            "EntityRequests": [
                {
                    "EntityType": "Message",
                    "ContentSources": ["Teams"],
                    "Query": {"QueryString": query, "DisplayQueryString": query},
                    "From": offset,
                    "Size": page_size,
                    "Sort": [],
                    "PropertySet": "Optimized",
                }
            ],
        }
        with operation("searching Teams messages"):
            data = await self._transport.json(
                "POST",
                config.SEARCH_URL,
                credential=self._credential,
                headers={"X-AnchorMailbox": self._anchor_mailbox},
                json_body=body,
                retry=True,
            )
        rows, more, found, total = _message_rows(data)
        return MessageSearchPage(
            items=[_map_hit(row) for row in rows],
            more_results_available=more,
            result_set_found=found,
            total=total,
        )


def _message_rows(data: Any) -> tuple[list[dict[str, Any]], bool | None, bool, int | None]:
    if not isinstance(data, dict):
        return [], None, False, None
    entity_sets = data.get("EntitySets")
    if not isinstance(entity_sets, list):
        return [], None, False, None
    rows: list[dict[str, Any]] = []
    flags: list[bool] = []
    totals: list[int] = []
    found = False
    for entity_set in entity_sets:
        if not isinstance(entity_set, dict):
            continue
        result_sets = entity_set.get("ResultSets")
        if not isinstance(result_sets, list):
            continue
        for result_set in result_sets:
            if not isinstance(result_set, dict):
                continue
            results = result_set.get("Results")
            if isinstance(results, list):
                found = True
                rows.extend(item for item in results if isinstance(item, dict))
            elif _reports_empty(result_set):
                found = True
            more = result_set.get("MoreResultsAvailable", entity_set.get("MoreResultsAvailable"))
            if isinstance(more, bool):
                flags.append(more)
            total = result_set.get("Total")
            if type(total) is int:
                totals.append(total)
    more_results = any(flags) if flags else None
    return rows, more_results, found, sum(totals) if totals else None


def _reports_empty(result_set: dict[str, Any]) -> bool:
    """An answer with no hits: ``Total`` is the integer 0 and there is no next page. Search v2 sends
    no ``Results`` key then, which is a confirmed empty page, not a missing one."""
    total = result_set.get("Total")
    return type(total) is int and total == 0 and result_set.get("MoreResultsAvailable") is False


def _map_hit(row: dict[str, Any]) -> MessageHit:
    source = row.get("Source")
    source = source if isinstance(source, dict) else {}
    return MessageHit(
        message_id=_string(source.get("InternetMessageId")),
        conversation_id_candidates=_conversation_ids(source),
        sent_at=_datetime(source.get("DateTimeSent")),
        sender=_sender(source.get("Sender")),
        subject=_string(source.get("Subject")),
        preview=_string(row.get("HitHighlightedSummary")) or _string(source.get("Preview")),
    )


def _conversation_ids(source: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("ClientConversationId", "ClientThreadId", "ConversationRestId", "ConversationThreadId"):
        value = _string(source.get(key))
        if value and value not in values:
            values.append(value)
    conversation = source.get("ConversationId")
    if isinstance(conversation, dict):
        value = _string(conversation.get("Id"))
        if value and value not in values:
            values.append(value)
    return values


def _sender(value: Any) -> str | None:
    if isinstance(value, dict):
        return _string(value.get("DisplayName")) or _string(value.get("Name"))
    return _string(value)


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
