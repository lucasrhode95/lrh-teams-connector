from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from teams_connector.bootstrap import AppContext
from teams_connector.surfaces.mcp_main import build_server
from tests.fakes.teams_fake import FakeTokens

SUBJECT = "aaaaaaaa-1111-1111-1111-111111111111"
MANAGER = "bbbbbbbb-2222-2222-2222-222222222222"
PEER = "cccccccc-3333-3333-3333-333333333333"
ROOT = "dddddddd-4444-4444-4444-444444444444"


def profile(identifier: str, name: str) -> dict[str, Any]:
    return {
        "@odata.type": "#microsoft.graph.user",
        "id": identifier,
        "displayName": name,
        "mail": f"{name.split()[0].lower()}@example.test",
        "userPrincipalName": f"{name.split()[0].lower()}@example.test",
    }


@dataclass
class OrgTenant:
    calls: list[httpx.Request] = field(default_factory=list)
    denied: bool = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.host == "graph.microsoft.com"
        self.calls.append(request)
        if self.denied:
            return httpx.Response(403, json={"error": {"code": "Authorization_RequestDenied"}})
        subject = profile(SUBJECT, "Alex Example")
        manager = profile(MANAGER, "Sam Example")
        peer = profile(PEER, "Alex Other")
        root = profile(ROOT, "Quinn Root")
        path = request.url.path.removeprefix("/v1.0")
        if path == "/users":
            return httpx.Response(200, json={"value": [subject, peer]})
        if path == f"/users/{SUBJECT}":
            if "$expand" in request.url.params:
                subject["manager"] = {**manager, "manager": root}
            return httpx.Response(200, json=subject)
        if path == f"/users/{SUBJECT}/manager":
            return httpx.Response(200, json=manager)
        if path == f"/users/{ROOT}":
            return httpx.Response(200, json=root)
        if path == f"/users/{ROOT}/manager":
            return httpx.Response(404, json={"error": {"code": "Request_ResourceNotFound"}})
        if path == f"/users/{MANAGER}/directReports":
            if "$skiptoken" in request.url.params:
                return httpx.Response(200, json={"value": [peer]})
            return httpx.Response(
                200,
                json={
                    "value": [subject, {"@odata.type": "#microsoft.graph.orgContact", "id": "contact"}],
                    "@odata.nextLink": f"https://graph.microsoft.com/v1.0{path}?$skiptoken=next",
                },
            )
        if path == f"/users/{SUBJECT}/directReports":
            return httpx.Response(200, json={"value": []})
        raise AssertionError("Unexpected synthetic directory request")


@pytest.fixture
async def org_context():
    tenant = OrgTenant()
    client = httpx.AsyncClient(transport=httpx.MockTransport(tenant.handle))
    app = AppContext(tokens=FakeTokens(), http_client=client)  # type: ignore[arg-type]
    yield app, tenant
    await app.aclose()
    await client.aclose()


async def test_four_org_tools_use_real_wiring_and_typed_read_only_results(org_context) -> None:
    context, tenant = org_context
    async with create_connected_server_and_client_session(build_server(context)) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        for name in ("find_person", "get_manager_chain", "list_direct_reports", "list_peers"):
            assert tools[name].annotations.readOnlyHint is True
            assert tools[name].annotations.destructiveHint is False
            assert tools[name].annotations.idempotentHint is True
            assert tools[name].outputSchema

        found = await client.call_tool("find_person", {"query": "Alex"})
        assert not found.isError and found.structuredContent
        assert [person["id"] for person in found.structuredContent["items"]] == [SUBJECT, PEER]
        assert found.structuredContent["items"][0]["email"] == "alex@example.test"
        chain = await client.call_tool("get_manager_chain", {"person_id": SUBJECT})
        assert not chain.isError and chain.structuredContent
        assert [person["id"] for person in chain.structuredContent["managers"]] == [MANAGER, ROOT]
        reports = await client.call_tool("list_direct_reports", {"person_id": SUBJECT})
        assert not reports.isError and reports.structuredContent
        assert reports.structuredContent["items"] == [] and reports.structuredContent["complete"]
        peers = await client.call_tool("list_peers", {"person_id": SUBJECT})
        assert not peers.isError and peers.structuredContent
        assert [person["id"] for person in peers.structuredContent["items"]] == [PEER]
        assert peers.structuredContent["derived"] is True and peers.structuredContent["complete"] is True
        assert peers.structuredContent["manager"]["id"] == MANAGER
        root = await client.call_tool("list_peers", {"person_id": ROOT})
        assert not root.isError and root.structuredContent
        assert root.structuredContent["items"] == [] and root.structuredContent["reason"]
        assert len(tenant.calls) == 10


async def test_org_input_errors_do_not_read_directory_and_denials_stay_errors(org_context) -> None:
    context, tenant = org_context
    async with create_connected_server_and_client_session(build_server(context)) as client:
        for name, args in (
            ("find_person", {"query": "  "}),
            ("find_person", {"query": "Alex", "limit": 101}),
            ("get_manager_chain", {"person_id": "Alex"}),
            ("list_direct_reports", {"person_id": "../me"}),
            ("list_peers", {"person_id": "alex@example.test"}),
        ):
            assert (await client.call_tool(name, args)).isError
        assert tenant.calls == []
        tenant.denied = True
        denied = await client.call_tool("get_manager_chain", {"person_id": SUBJECT})
        assert denied.isError
        assert len(tenant.calls) == 1
