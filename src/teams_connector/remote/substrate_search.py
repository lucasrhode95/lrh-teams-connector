"""Microsoft Search v2 adapter for the verified Teams-message search operation.

The route is hosted by Outlook Web and uses the shared ``m365.search`` profile. Search discovery is
separate from Graph/Chat Service history reads; this adapter returns only fields observed in the
Search response and does not claim that a hit id is a history id.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from teams_connector import config
from teams_connector.domain.models import MessageHit, Participant
from teams_connector.remote.chat_service_mapping import user_id
from teams_connector.remote.ports import (
    ChatMemberHit,
    ChatMemberResult,
    ChatMemberSearch,
    MessageSearchPage,
    MessageSearchReader,
)
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


CHAT_PAGE = 25  # Teams Web's page size for group chats
MAX_CHAT_HITS = 100
PEOPLE_FIELDS = ["Id", "MRI", "DisplayName", "EmailAddresses", "PeopleType", "PeopleSubtype",
                 "UserPrincipalName", "ExternalDirectoryObjectId"]  # fmt: skip
# The filter Teams Web sends with its search-box people suggestions (captured 2026-10-14).
PEOPLE_FILTER = {
    "And": [
        {"Or": [{"Term": {"PeopleType": "Person"}}, {"Term": {"PeopleType": "Other"}}]},
        {"Or": [{"Term": {"PeopleSubtype": subtype}}
                for subtype in ("OrganizationUser", "MTOUser", "PersonalContact", "Guest")]},
        {"Or": [{"Term": {"Flags": "NonHidden"}}]},
    ]
}  # fmt: skip
PEOPLE_SIZE = 5  # as Teams Web asks


class SubstrateChatSearch(ChatMemberSearch):
    """Named chats Teams matches by name or member (the Search v2 endpoint), and the people Teams suggests
    for a name (Teams Web's search-box suggestions), with the same ``m365.search`` profile.

    The text is sent unquoted: a quoted query returned no chats. The service only passes plain names and
    emails, so the text cannot change the query's meaning.
    """

    def __init__(
        self, transport: Transport, credential: Credential, *, anchor_mailbox: str, me_id: str
    ) -> None:
        self._transport = transport
        self._credential = credential
        self._anchor_mailbox = anchor_mailbox
        self._me_id = me_id.casefold()

    async def named_chats(self, text: str) -> ChatMemberResult:
        """Up to ``MAX_CHAT_HITS`` chats, page by page while Teams reports more."""
        hits: list[ChatMemberHit] = []
        offset = 0
        while True:
            rows, more = await self._chat_page(text, offset)
            hits.extend(_chat_hit(source) for source in rows if _string(source.get("ThreadId")))
            offset += len(rows)
            if not more or not rows:
                return ChatMemberResult(hits=hits, truncated=False)
            if offset >= MAX_CHAT_HITS:
                return ChatMemberResult(hits=hits, truncated=True)

    async def people(self, text: str) -> list[Participant]:
        body = {
            "EntityRequests": [
                {
                    "Query": {"QueryString": text, "DisplayQueryString": text, "NormalizedQueryString": text},
                    "EntityType": "People",
                    "Size": PEOPLE_SIZE,
                    "Fields": PEOPLE_FIELDS,
                    "Filter": PEOPLE_FILTER,
                    "Provenances": ["Mailbox", "Directory"],
                    "From": 0,
                }
            ],
            "Scenario": {
                "Name": "powerbar",
                "Dimensions": [{"DimensionName": "QueryType", "DimensionValue": "PeopleCentricSearch"}],
            },
            "Cvid": str(uuid4()),
            "AppName": "Microsoft Teams",
            "LogicalId": str(uuid4()),
            "dataSource": "personScoped",
        }
        with operation("searching Teams people"):
            data = await self._transport.json(
                "POST",
                config.PEOPLE_SUGGESTIONS_URL,
                credential=self._credential,
                headers={"X-AnchorMailbox": self._anchor_mailbox},
                json_body=body,
                retry=True,
            )
        people = {(person.id or "").casefold(): person for person in _suggested_people(data)}
        return [person for key, person in people.items() if key != self._me_id]

    async def _chat_page(self, text: str, offset: int) -> tuple[list[dict[str, Any]], bool]:
        body = {
            "Cvid": str(uuid4()),
            "LogicalId": str(uuid4()),
            "Scenario": {"Name": "owa.react"},
            "TimeZone": "UTC",
            "TextDecorations": "Off",
            "EntityRequests": [
                {
                    "EntityType": "Chat",
                    "ContentSources": ["Teams"],
                    "Query": {"QueryString": text, "DisplayQueryString": text},
                    "From": offset,
                    "Size": CHAT_PAGE,
                    "Sort": [],
                    "PropertySet": "Optimized",
                }
            ],
        }
        with operation("searching Teams chats by member"):
            data = await self._transport.json(
                "POST",
                config.SEARCH_URL,
                credential=self._credential,
                headers={"X-AnchorMailbox": self._anchor_mailbox},
                json_body=body,
                retry=True,
            )
        _, more, _, _ = _message_rows(data)
        return _result_sources(data), more is True


def _suggested_people(data: Any) -> list[Participant]:
    groups = data.get("Groups") if isinstance(data, dict) else None
    people: list[Participant] = []
    for group in groups if isinstance(groups, list) else []:
        if not isinstance(group, dict) or group.get("Type") != "People":
            continue
        for suggestion in group.get("Suggestions") or []:
            if not isinstance(suggestion, dict):
                continue
            object_id = _string(suggestion.get("ExternalDirectoryObjectId"))
            if object_id:
                people.append(Participant(id=object_id, name=_string(suggestion.get("DisplayName"))))
    return people


def _result_sources(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        return []
    sources: list[dict[str, Any]] = []
    for entity_set in data.get("EntitySets") or []:
        if not isinstance(entity_set, dict):
            continue
        for result_set in entity_set.get("ResultSets") or []:
            if not isinstance(result_set, dict):
                continue
            for row in result_set.get("Results") or []:
                if isinstance(row, dict) and isinstance(row.get("Source"), dict):
                    sources.append(row["Source"])
    return sources


def _chat_hit(source: dict[str, Any]) -> ChatMemberHit:
    members: list[Participant] = []
    for member in source.get("MatchingMembers") or []:
        if not isinstance(member, dict):
            continue
        object_id = user_id(member.get("MRI"))
        if object_id is None:
            continue
        name = _string(member.get("DisplayName")) or " ".join(
            part for part in (_string(member.get("GivenName")), _string(member.get("Surname"))) if part
        )
        members.append(Participant(id=object_id, name=name or None))
    return ChatMemberHit(chat_id=str(source["ThreadId"]), matching_members=members)


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
