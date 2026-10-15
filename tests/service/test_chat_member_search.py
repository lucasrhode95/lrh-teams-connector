from __future__ import annotations

import json
from typing import Any

import httpx

from teams_connector.bootstrap import Services
from teams_connector.domain.errors import Upstream
from teams_connector.domain.models import Participant
from teams_connector.remote.ports import ChatMemberHit
from teams_connector.remote.substrate_search import SubstrateChatSearch
from teams_connector.remote.transport import Transport

MEMBER_OID = "33333333-3333-3333-3333-333333333333"


class SearchCredential:
    async def headers(self, renewal: dict[str, Any] | None) -> dict[str, str]:
        return {"Authorization": "Bearer synthetic-token"}

    def sign_in_command(self) -> str:
        return "teams-connector auth"


class MemberStub:
    def __init__(self, hits: list[ChatMemberHit] | None = None, error: Exception | None = None) -> None:
        self.hits = hits or []
        self.error = error
        self.calls: list[str] = []

    async def named_chats(self, text: str) -> list[ChatMemberHit]:
        self.calls.append(text)
        if self.error is not None:
            raise self.error
        return self.hits


async def test_named_chats_map_thread_ids_and_matching_members() -> None:
    requests: list[httpx.Request] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        row = {
            "Source": {
                "ThreadId": "19:synthetic@thread.v2",
                "MatchingMembers": [
                    {
                        "MRI": f"8:orgid:{MEMBER_OID}",
                        "DisplayName": "Synthetic Member",
                        "GivenName": "Synthetic",
                        "Surname": "Member",
                    }
                ],
            }
        }
        return httpx.Response(200, json={"EntitySets": [{"ResultSets": [{"Results": [row]}]}]})

    transport = Transport(client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    try:
        adapter = SubstrateChatSearch(transport, SearchCredential(), anchor_mailbox="Oid:synthetic@tenant")
        hits = await adapter.named_chats("Synthetic")
    finally:
        await transport.aclose()

    assert hits == [
        ChatMemberHit(
            chat_id="19:synthetic@thread.v2",
            matching_members=[Participant(id=MEMBER_OID, name="Synthetic Member")],
        )
    ]
    entity = json.loads(requests[0].content)["EntityRequests"][0]
    assert entity["EntityType"] == "Chat" and entity["Query"]["QueryString"] == "Synthetic"


async def test_a_stored_chat_matched_by_member_lists_that_member(services: Services) -> None:
    await services.catalog.refresh()
    group = next(iter(services.store.chats(kind="group")))
    stub = MemberStub([ChatMemberHit(group.id, [Participant(id=MEMBER_OID, name="Synthetic Member")])])
    services.catalog._members = stub  # the test's own seam: Teams' chat search

    result = await services.catalog.search("Synthetic", refresh=True)

    match = next(item for item in result.items if item.chat.id == group.id)
    assert [person.id for person in match.matched_participants] == [MEMBER_OID] and not match.name_matched
    assert stub.calls == ["Synthetic"]
    assert any("named group and meeting chats" in note for note in result.coverage.notes)


async def test_a_query_with_query_syntax_is_not_sent(services: Services) -> None:
    await services.catalog.refresh()
    stub = MemberStub()
    services.catalog._members = stub

    result = await services.catalog.search("Synthetic AND Member", refresh=True)

    assert stub.calls == []
    assert any("plain name or email" in note for note in result.coverage.notes)


async def test_a_teams_failure_leaves_the_local_answer_in_place(services: Services) -> None:
    await services.catalog.refresh()
    services.catalog._members = MemberStub(error=Upstream("synthetic failure"))

    result = await services.catalog.search("Platform", refresh=True)

    assert any(item.name_matched for item in result.items)
    assert any("did not answer" in note for note in result.coverage.notes)


async def test_cache_only_search_does_not_ask_teams(services: Services) -> None:
    await services.catalog.refresh()
    stub = MemberStub([ChatMemberHit("19:none@thread.v2", [])])
    services.catalog._members = stub

    await services.catalog.search("Synthetic", refresh=False)

    assert stub.calls == []
