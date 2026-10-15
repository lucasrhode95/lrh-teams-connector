from __future__ import annotations

import json
from typing import Any

import httpx

from teams_connector.bootstrap import Services
from teams_connector.domain.errors import Upstream
from teams_connector.domain.models import Participant
from teams_connector.remote.ports import ChatMemberHit, ChatMemberResult
from teams_connector.remote.substrate_search import SubstrateChatSearch
from teams_connector.remote.transport import Transport
from tests.fakes.teams_fake import FOURTH, FakeChat, FakeTeams, cs_message

MEMBER_OID = "33333333-3333-3333-3333-333333333333"
ME = "11111111-1111-1111-1111-111111111111"


class SearchCredential:
    async def headers(self, renewal: dict[str, Any] | None) -> dict[str, str]:
        return {"Authorization": "Bearer synthetic-token"}

    def sign_in_command(self) -> str:
        return "teams-connector auth"


class MemberStub:
    def __init__(
        self,
        hits: list[ChatMemberHit] | None = None,
        error: Exception | None = None,
        people: list[Participant] | None = None,
        truncated: bool = False,
    ) -> None:
        self.hits = hits or []
        self.error = error
        self.found_people = people or []
        self.truncated = truncated
        self.calls: list[str] = []

    async def named_chats(self, text: str) -> ChatMemberResult:
        self.calls.append(text)
        if self.error is not None:
            raise self.error
        return ChatMemberResult(hits=self.hits, truncated=self.truncated)

    async def people(self, text: str) -> list[Participant]:
        if self.error is not None:
            raise self.error
        return self.found_people


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
        adapter = SubstrateChatSearch(
            transport, SearchCredential(), anchor_mailbox="Oid:synthetic@tenant", me_id=ME
        )
        result = await adapter.named_chats("Synthetic")
    finally:
        await transport.aclose()

    assert not result.truncated
    assert result.hits == [
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


def _adapter(handle: Any) -> tuple[Transport, SubstrateChatSearch]:
    transport = Transport(client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    return transport, SubstrateChatSearch(
        transport, SearchCredential(), anchor_mailbox="Oid:synthetic@tenant", me_id=ME
    )


async def test_named_chats_follow_teams_pages_and_report_a_cut() -> None:
    offsets: list[int] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        offset = json.loads(request.content)["EntityRequests"][0]["From"]
        offsets.append(offset)
        rows = [{"Source": {"ThreadId": f"19:chat-{offset + i}@thread.v2"}} for i in range(25)]
        return httpx.Response(
            200, json={"EntitySets": [{"ResultSets": [{"Results": rows, "MoreResultsAvailable": True}]}]}
        )

    transport, adapter = _adapter(handle)
    try:
        result = await adapter.named_chats("Synthetic")
    finally:
        await transport.aclose()
    assert offsets == [0, 25, 50, 75] and len(result.hits) == 100 and result.truncated


async def test_people_are_asked_as_teams_web_asks_and_never_include_the_user() -> None:
    requests: list[httpx.Request] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        people = [
            {"ExternalDirectoryObjectId": MEMBER_OID, "DisplayName": "Synthetic Member"},
            {"ExternalDirectoryObjectId": ME, "DisplayName": "Mia Example"},
        ]
        return httpx.Response(200, json={"Groups": [{"Type": "People", "Suggestions": people}]})

    transport, adapter = _adapter(handle)
    try:
        people = await adapter.people("synthetic.member@example.test")
    finally:
        await transport.aclose()
    assert people == [Participant(id=MEMBER_OID, name="Synthetic Member")]
    (request,) = requests
    assert request.url.host == "substrate.office.com" and request.url.params["scenario"] == "powerbar"
    body = json.loads(request.content)
    assert (
        body["dataSource"] == "personScoped"
        and body["Scenario"]["Dimensions"][0]["DimensionName"] == "QueryType"
    )
    assert body["EntityRequests"][0]["EntityType"] == "People"


async def test_a_one_on_one_is_found_through_the_person_teams_suggests(
    services: Services, fake: FakeTeams
) -> None:
    chat_id = f"19:{ME}_{FOURTH}@unq.gbl.spaces"
    fake.users[FOURTH] = "Robin Roe"
    fake.chats[chat_id] = FakeChat(
        id=chat_id,
        chat_type="oneOnOne",
        members=[(ME, None), (FOURTH, None)],
        messages=[cs_message("9001", "2026-09-06T08:00:00.000Z", author=FOURTH)],
    )
    await services.catalog.refresh()
    services.catalog._members = MemberStub(people=[Participant(id=FOURTH, name="Robin Roe")])

    result = await services.catalog.search("robin.roe@example.test", refresh=True)

    (match,) = result.items
    assert match.chat.id == chat_id and not match.name_matched
    assert [person.id for person in match.matched_participants] == [FOURTH]
    assert result.coverage.complete


async def test_pages_keep_the_first_answer_of_teams(services: Services, fake: FakeTeams) -> None:
    await services.catalog.refresh()
    chats = list(services.store.chats())
    hits = [ChatMemberHit(chat.id, [Participant(id=MEMBER_OID, name="Synthetic Member")]) for chat in chats]
    stub = MemberStub(hits)
    services.catalog._members = stub

    first = await services.catalog.search("Synthetic", refresh=True, limit=2)
    assert first.cursor and stub.calls == ["Synthetic"]
    services.catalog._members = MemberStub(error=Upstream("synthetic failure"))  # Teams stops answering

    seen = [item.chat.id for item in first.items]
    cursor = first.cursor
    while cursor:
        page = await services.catalog.search("ignored", cursor=cursor)
        assert page.coverage.complete
        seen += [item.chat.id for item in page.items]
        cursor = page.cursor
    assert sorted(seen) == sorted(chat.id for chat in chats)


async def test_a_cut_answer_from_teams_is_reported(services: Services) -> None:
    await services.catalog.refresh()
    services.catalog._members = MemberStub(truncated=True)

    result = await services.catalog.search("Synthetic", refresh=True)

    assert not result.coverage.complete
    assert any("more named chats" in note for note in result.coverage.notes)
