from __future__ import annotations

import json
from typing import Any

import httpx

from teams_connector.config import SEARCH_URL
from teams_connector.remote.substrate_search import SubstrateSearch
from teams_connector.remote.transport import Transport


class SearchCredential:
    async def headers(self, renewal: dict[str, Any] | None) -> dict[str, str]:
        return {"Authorization": "Bearer synthetic-token"}

    def sign_in_command(self) -> str:
        return "teams-connector auth"


async def test_substrate_message_search_builds_known_request_and_maps_hit() -> None:
    requests: list[httpx.Request] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "EntitySets": [
                    {
                        "ResultSets": [
                            {
                                "Results": [
                                    {
                                        "HitHighlightedSummary": "synthetic <c0>needle</c0>",
                                        "Source": {
                                            "InternetMessageId": "synthetic-message-id",
                                            "ClientConversationId": "synthetic-chat-id",
                                            "ClientThreadId": "synthetic-chat-id",
                                            "DateTimeSent": "2026-10-10T12:34:56Z",
                                            "Sender": {"DisplayName": "Synthetic Sender"},
                                            "Subject": "Synthetic subject",
                                        },
                                    }
                                ],
                                "MoreResultsAvailable": True,
                            }
                        ]
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    transport = Transport(client=client)
    try:
        adapter = SubstrateSearch(transport, SearchCredential(), anchor_mailbox="Oid:synthetic@tenant")
        result = await adapter.search_messages(
            "synthetic needle",
            offset=5,
            page_size=5,
            cvid="synthetic-cvid",
            logical_id="synthetic-logical-id",
        )
    finally:
        await transport.aclose()

    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST" and str(request.url) == SEARCH_URL
    assert request.headers["x-anchormailbox"] == "Oid:synthetic@tenant"
    body = json.loads(request.content)
    entity = body["EntityRequests"][0]
    assert entity["EntityType"] == "Message"
    assert entity["ContentSources"] == ["Teams"]
    assert entity["From"] == 5 and entity["Size"] == 5
    assert body["Cvid"] == "synthetic-cvid" and body["LogicalId"] == "synthetic-logical-id"
    assert result.more_results_available is True and result.result_set_found
    hit = result.items[0]
    assert hit.search_message_id == "synthetic-message-id"
    assert hit.message_id_kind == "InternetMessageId"
    assert hit.conversation_id_candidates == ["synthetic-chat-id"]
    assert hit.sent_at and hit.sent_at.isoformat() == "2026-10-10T12:34:56+00:00"
    assert hit.sender == "Synthetic Sender" and hit.subject == "Synthetic subject"
    assert hit.preview == "synthetic <c0>needle</c0>"


async def test_missing_search_result_set_is_not_mistaken_for_zero_hits() -> None:
    async def handle(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ApiVersion": "synthetic", "Instrumentation": {}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    transport = Transport(client=client)
    try:
        adapter = SubstrateSearch(transport, SearchCredential(), anchor_mailbox="Oid:synthetic@tenant")
        result = await adapter.search_messages(
            "synthetic needle",
            offset=0,
            page_size=10,
            cvid="synthetic-cvid",
            logical_id="synthetic-logical-id",
        )
    finally:
        await transport.aclose()

    assert not result.result_set_found
    assert result.items == []
    assert result.more_results_available is None


async def test_empty_search_answer_with_zero_total_is_a_confirmed_empty_page() -> None:
    async def handle(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "EntitySets": [
                    {"ResultSets": [{"Total": 0, "MoreResultsAvailable": False}]},
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    transport = Transport(client=client)
    try:
        adapter = SubstrateSearch(transport, SearchCredential(), anchor_mailbox="Oid:synthetic@tenant")
        result = await adapter.search_messages(
            "synthetic absent term",
            offset=0,
            page_size=10,
            cvid="synthetic-cvid",
            logical_id="synthetic-logical-id",
        )
    finally:
        await transport.aclose()

    assert result.result_set_found and result.items == []
    assert result.more_results_available is False
