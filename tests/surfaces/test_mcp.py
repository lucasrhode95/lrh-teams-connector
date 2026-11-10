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
            "search_messages",
            "export_chats",
            "download_attachment",
            "get_presence",
            "search_transcripts",
            "send_message",
            "delete_messages",
            "edit_message",
            "find_person",
            "get_manager_chain",
            "list_direct_reports",
            "list_peers",
        }
        assert all(isinstance(tool.inputSchema, dict) for tool in tools.values())
        assert tools["list_chats"].inputSchema["properties"]
        assert all(tool.outputSchema is not None for tool in tools.values())
        assert tools["send_message"].annotations.readOnlyHint is False
        assert tools["send_message"].annotations.destructiveHint is True
        assert tools["delete_messages"].annotations.destructiveHint is True
        assert tools["edit_message"].annotations.destructiveHint is True
        assert tools["edit_message"].annotations.readOnlyHint is False
        assert "remove_attachments" in tools["edit_message"].inputSchema["properties"]
        assert "explicit confirmation" in (tools["delete_messages"].description or "")
        assert "files" in tools["send_message"].inputSchema["properties"]
        assert "include_transcripts" in tools["export_chats"].inputSchema["properties"]
        assert "include_summaries" in tools["export_chats"].inputSchema["properties"]
        assert "include_summaries" in tools["get_messages"].inputSchema["properties"]
        assert "include_summaries" in tools["search_transcripts"].inputSchema["properties"]
        assert tools["search_transcripts"].annotations.readOnlyHint is True
        assert tools["search_messages"].annotations.readOnlyHint is True

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


async def test_mcp_rejects_an_empty_search_and_a_send_to_a_misnamed_chat(
    context: AppContext, fake: FakeTeams
) -> None:
    async with create_connected_server_and_client_session(build_server(context)) as client:
        search = await client.call_tool("search_messages", {"terms": []})
        assert search.isError

        send = await client.call_tool(
            "send_message",
            {
                "conversation_id": "19:one@unq.gbl.spaces",
                "expected_chat_name": "Someone else",
                "text": "synthetic message",
            },
        )
        assert send.isError
        assert fake.sent == []


async def test_mcp_edits_with_attachments_and_refuses_another_persons_message(
    context: AppContext,
    fake: FakeTeams,
    tmp_path,
) -> None:
    file = tmp_path / "plan.txt"
    file.write_text("Synthetic plan", encoding="utf-8")
    async with create_connected_server_and_client_session(build_server(context)) as client:
        result = await client.call_tool(
            "edit_message",
            {
                "conversation_id": "19:one@unq.gbl.spaces",
                "expected_chat_name": "Pat Peer",
                "message_id": "1002",
                "html": "<b>Revised</b>",
                "files": [str(file)],
            },
        )
        assert not result.isError and result.structuredContent
        assert result.structuredContent["verified"] is True
        assert result.structuredContent["status"] == "updated"
        assert result.structuredContent["message_id"] == "1002"
        name = result.structuredContent["attachments"][0]["name"]
        removed = await client.call_tool(
            "edit_message",
            {
                "conversation_id": "19:one@unq.gbl.spaces",
                "expected_chat_name": "Pat Peer",
                "message_id": "1002",
                "remove_attachments": [name],
            },
        )
        assert not removed.isError and removed.structuredContent["verified"] is True
        refused = await client.call_tool(
            "edit_message",
            {
                "conversation_id": "19:one@unq.gbl.spaces",
                "expected_chat_name": "Pat Peer",
                "message_id": "1003",
                "text": "Changed",
            },
        )
        assert refused.isError and len(fake.edits) == 2
