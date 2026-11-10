from collections.abc import Callable
from typing import Any

import httpx
import pytest

from teams_connector.domain.errors import InvalidRequest, NotFound, PermissionDenied, Upstream
from teams_connector.remote.graph import Graph
from teams_connector.remote.graph_org import PROFILE, GraphOrgDirectory
from teams_connector.remote.transport import Transport

ROOT = "https://graph.microsoft.com/v1.0"
PERSON = "11111111-1111-1111-1111-111111111111"
MANAGER = "22222222-2222-2222-2222-222222222222"
TOP = "33333333-3333-3333-3333-333333333333"


class NoCredential:
    async def headers(self, renewal: dict[str, Any] | None) -> dict[str, str]:
        return {"Authorization": "Bearer synthetic"}

    def sign_in_command(self) -> str:
        return "teams-connector auth"


@pytest.fixture
async def directory():
    clients: list[httpx.AsyncClient] = []

    def build(handler: Callable[[httpx.Request], httpx.Response]) -> GraphOrgDirectory:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        clients.append(client)
        return GraphOrgDirectory(Graph(Transport(client=client), NoCredential()))

    yield build
    for client in clients:
        await client.aclose()


def user(identifier: str, **fields: Any) -> dict[str, Any]:
    return {"@odata.type": "#microsoft.graph.user", "id": identifier, **fields}


async def test_expanded_chain_in_one_read_with_consistency_and_prefer(directory) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET"
        assert request.url.path == f"/v1.0/users/{PERSON}"
        assert request.url.params["$expand"] == f"manager($levels=max;$select={PROFILE})"
        assert request.headers["ConsistencyLevel"] == "eventual"
        assert request.headers["Prefer"] == "include-unknown-enum-members"
        return httpx.Response(
            200,
            json=user(PERSON, manager=user(MANAGER, manager=user(TOP, displayName=None))),
        )

    subject, managers = await directory(handle).manager_chain(PERSON)
    assert subject.id == PERSON
    assert [person.id for person in managers] == [MANAGER, TOP]
    assert managers[-1].name is None
    assert len(requests) == 1


async def test_root_has_empty_chain_and_manager_404_is_none(directory) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/manager"):
            return httpx.Response(404, json={"error": {"code": "Request_ResourceNotFound"}})
        return httpx.Response(200, json=user(PERSON))

    source = directory(handle)
    assert (await source.manager_chain(PERSON))[1] == []
    assert await source.manager(PERSON) is None


async def test_unknown_subject_and_denials_are_not_empty_hierarchy(directory) -> None:
    source = directory(lambda _: httpx.Response(404, json={"error": {"code": "Request_ResourceNotFound"}}))
    with pytest.raises(NotFound):
        await source.manager_chain(PERSON)
    assert await source.find(PERSON, limit=25) == ([], True)
    source = directory(lambda _: httpx.Response(403, json={"error": {"code": "Authorization_RequestDenied"}}))
    with pytest.raises(PermissionDenied):
        await source.manager(PERSON)
    with pytest.raises(PermissionDenied):
        await source.direct_reports(PERSON)
    with pytest.raises(PermissionDenied):
        await source.find(PERSON, limit=25)


async def test_reports_follow_opaque_pages_filter_contacts_and_deduplicate(directory) -> None:
    calls: list[str] = []
    link = f"{ROOT}/users/{PERSON}/directReports?$skiptoken=opaque%2Btoken"

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if len(calls) == 1:
            return httpx.Response(
                200,
                json={
                    "value": [user(MANAGER), {"@odata.type": "#microsoft.graph.orgContact", "id": "contact"}],
                    "@odata.nextLink": link,
                },
            )
        assert str(request.url) == link
        return httpx.Response(200, json={"value": [user(MANAGER), user(TOP, displayName="  Alex Example  ")]})

    people, complete = await directory(handle).direct_reports(PERSON)
    assert complete
    assert [person.id for person in people] == [MANAGER, TOP]
    assert people[-1].name == "Alex Example"
    assert len(calls) == 2


@pytest.mark.parametrize("link", [f"{ROOT}/reports?token=again", "https://other.example/reports"])
async def test_reports_reject_repeating_or_foreign_links(directory, link: str) -> None:
    calls = 0

    def handle(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"value": [], "@odata.nextLink": link})

    with pytest.raises((Upstream, InvalidRequest)):
        await directory(handle).direct_reports(PERSON)
    assert calls <= 2


async def test_report_bounds_are_explicitly_incomplete(directory, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("teams_connector.remote.graph_org.MAX_REPORT_PAGES", 2)
    calls = 0

    def handle(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"value": [], "@odata.nextLink": f"{ROOT}/reports?skip={calls}"})

    assert await directory(handle).direct_reports(PERSON) == ([], False)
    assert calls == 2
    monkeypatch.setattr("teams_connector.remote.graph_org.MAX_REPORTS", 1)
    source = directory(lambda _: httpx.Response(200, json={"value": [user(MANAGER), user(TOP)]}))
    people, complete = await source.direct_reports(PERSON)
    assert [person.id for person in people] == [MANAGER]
    assert not complete


@pytest.mark.parametrize(
    ("query", "predicate"),
    [
        ("O'Example", "startswith(displayName,'O''Example')"),
        ("alex@example.test", "mail eq 'alex@example.test' or userPrincipalName eq 'alex@example.test'"),
        ("x') or true or ('x", "startswith(displayName,'x'') or true or (''x')"),
    ],
)
async def test_search_escapes_literals_and_never_selects_an_ambiguous_match(
    directory, query, predicate
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.params["$filter"] == predicate
        assert request.url.params["$select"] == PROFILE
        return httpx.Response(200, json={"value": [user(PERSON), user(MANAGER)]})

    people, complete = await directory(handle).find(query, limit=1)
    assert [person.id for person in people] == [PERSON]
    assert not complete


async def test_search_by_id_and_empty_name_result(directory) -> None:
    source = directory(lambda _: httpx.Response(200, json=user(PERSON)))
    people, complete = await source.find(PERSON.upper(), limit=25)
    assert people[0].id == PERSON and complete
    source = directory(lambda _: httpx.Response(200, json={"value": []}))
    assert await source.find("Nobody", limit=25) == ([], True)


@pytest.mark.parametrize(
    "manager", [user(PERSON), {"displayName": "Missing id"}, {"id": MANAGER, "@odata.type": "other"}, "bad"]
)
async def test_bad_or_cyclic_chain_fails_instead_of_claiming_completion(directory, manager) -> None:
    source = directory(lambda _: httpx.Response(200, json=user(PERSON, manager=manager)))
    with pytest.raises(Upstream):
        await source.manager_chain(PERSON)
