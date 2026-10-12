from __future__ import annotations

import httpx
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from teams_connector.bootstrap import AppContext
from teams_connector.surfaces.mcp_main import build_server
from tests.fakes.teams_fake import FakeTeams, FakeTokens


@pytest.fixture
async def context(fake: FakeTeams):
    http_client = httpx.AsyncClient(transport=fake.transport())
    app = AppContext(tokens=FakeTokens(), http_client=http_client)  # type: ignore[arg-type]
    yield app
    await app.aclose()


async def test_mcp_exposes_current_contract_and_reads_synthetic_chats(context: AppContext) -> None:
    async with create_connected_server_and_client_session(build_server(context)) as client:
        response = await client.list_tools()
        tools = {tool.name: tool for tool in response.tools}

        assert set(tools) == {
            "auth_status",
            "list_chats",
            "search_chats",
            "get_messages",
            "get_message_content",
            "search_messages",
            "export_chats",
            "download_attachment",
            "sync_archive",
            "archive_status",
            "send_message",
        }
        assert all(isinstance(tool.inputSchema, dict) for tool in tools.values())
        assert tools["list_chats"].inputSchema["properties"]
        assert all(tool.outputSchema is not None for tool in tools.values())
        assert tools["send_message"].annotations.readOnlyHint is False
        assert tools["send_message"].annotations.destructiveHint is True

        auth_status = await client.call_tool("auth_status", {})
        assert not auth_status.isError and auth_status.structuredContent is not None
        profiles = auth_status.structuredContent["profiles"]
        assert profiles["teams.csa"]["client_credential_cached"] is True
        assert profiles["teams.csa"]["access_token_expires_on"] is None
        assert "signed_in" not in profiles["teams.csa"]

        result = await client.call_tool("list_chats", {"refresh": True, "limit": 10})
        assert not result.isError
        assert result.structuredContent is not None
        assert result.structuredContent["coverage"]["source"] == "remote"
        assert "19:one@unq.gbl.spaces" in {row["id"] for row in result.structuredContent["items"]}


async def test_mcp_rejects_unscoped_search_and_unconfirmed_send(context: AppContext, fake: FakeTeams) -> None:
    async with create_connected_server_and_client_session(build_server(context)) as client:
        search = await client.call_tool("search_messages", {"query": "synthetic marker"})
        assert search.isError

        send = await client.call_tool(
            "send_message",
            {
                "conversation_id": "19:one@unq.gbl.spaces",
                "expected_chat_name": "Pat Peer",
                "text": "synthetic message",
                "user_confirmation": "",
            },
        )
        assert send.isError
        assert fake.sent == []
