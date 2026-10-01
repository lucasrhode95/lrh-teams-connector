from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(importlib.util.find_spec("mcp"), "install requirements.txt to test MCP")
class MCPContractTests(unittest.TestCase):
    def test_stdio_discovery_typed_outputs_and_local_search_call(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        with tempfile.TemporaryDirectory() as temp_dir:
            environment = os.environ.copy()
            environment["TEAMS_SEARCH_INDEX_PATH"] = str(
                Path(temp_dir) / "contract-test.sqlite3"
            )
            environment["LOCALAPPDATA"] = str(Path(temp_dir) / "appdata")
            parameters = StdioServerParameters(
                command=sys.executable,
                args=["-m", "teams_exporter.mcp_server"],
                env=environment,
                cwd=str(PROJECT_ROOT),
            )

            async def exercise_client():
                async with stdio_client(parameters) as (read_stream, write_stream):
                    async with ClientSession(read_stream, write_stream) as session:
                        await session.initialize()
                        response = await session.list_tools()
                        tools_by_name = {tool.name: tool for tool in response.tools}
                        expected_names = {
                            "list_chats",
                            "search_chats",
                            "get_messages",
                            "get_message_content",
                            "search_messages",
                            "export_chat",
                            "read_export",
                            "sync_search_index",
                            "get_search_index_status",
                            "send_message",
                        }
                        self.assertEqual(set(tools_by_name), expected_names)
                        for tool in tools_by_name.values():
                            self.assertTrue(tool.inputSchema["properties"])
                            self.assertIsNotNone(tool.outputSchema)

                        catalog_schema = str(tools_by_name["list_chats"].outputSchema)
                        self.assertIn("catalog_scope", catalog_schema)
                        self.assertIn("unclassified_service_rows_excluded", catalog_schema)
                        self.assertIn("empty_chats_excluded", catalog_schema)
                        self.assertIn(
                            "is_empty_conversation",
                            str(tools_by_name["list_chats"].outputSchema),
                        )
                        self.assertIn(
                            "no_empty_chats",
                            tools_by_name["list_chats"].inputSchema["properties"],
                        )
                        self.assertIn(
                            "retry_count_this_call",
                            str(tools_by_name["get_messages"].outputSchema),
                        )
                        self.assertIn(
                            "content_next_offset",
                            str(tools_by_name["get_messages"].outputSchema),
                        )
                        self.assertIn(
                            "message_retries_this_call",
                            str(tools_by_name["sync_search_index"].outputSchema),
                        )

                        search_tool = tools_by_name["search_messages"]
                        self.assertEqual(
                            search_tool.inputSchema["properties"]["match_mode"]["enum"],
                            ["exact", "substring", "all_terms"],
                        )
                        self.assertIn("since", search_tool.inputSchema["required"])
                        self.assertIn("until", search_tool.inputSchema["required"])
                        self.assertIn("max_matches", search_tool.inputSchema["properties"])
                        search_schema = str(search_tool.outputSchema)
                        self.assertIn("is_deleted", search_schema)
                        self.assertIn("deleted_at", search_schema)
                        templates = await session.list_resource_templates()
                        self.assertEqual(len(templates.resourceTemplates), 1)
                        self.assertIn(
                            "teams-export://exports/{export_id}/parts/{offset_bytes}",
                            templates.resourceTemplates[0].uriTemplate,
                        )

                        send_tool = tools_by_name["send_message"]
                        self.assertEqual(
                            set(send_tool.inputSchema["required"]),
                            {
                                "conversation_id",
                                "expected_chat_name",
                                "text",
                                "user_confirmation",
                            },
                        )
                        self.assertFalse(send_tool.annotations.readOnlyHint)
                        self.assertTrue(send_tool.annotations.destructiveHint)
                        self.assertTrue(send_tool.annotations.openWorldHint)
                        self.assertIn("explicitly authorized", send_tool.description)

                        rejected_send = await session.call_tool(
                            "send_message",
                            {
                                "conversation_id": "19:fixture@thread.v2",
                                "expected_chat_name": "Fixture",
                                "text": "Should not be sent",
                                "user_confirmation": "",
                            },
                        )
                        self.assertTrue(rejected_send.isError)
                        rejection_text = " ".join(
                            item.text
                            for item in rejected_send.content
                            if hasattr(item, "text")
                        )
                        self.assertIn("explicit send authorization", rejection_text)

                        invalid_scope = await session.call_tool(
                            "search_messages", {"query": "sanitized contract probe"}
                        )
                        self.assertTrue(invalid_scope.isError)

            asyncio.run(exercise_client())

    def test_generic_mcp_client_can_call_archive_tools_and_read_export_resource(self):
        import asyncio
        import json
        from mcp.shared.memory import create_connected_server_and_client_session
        from pydantic import AnyUrl

        from teams_exporter.mcp_server import create_mcp_server

        class FixtureService:
            def get_messages(self, conversation_id, **kwargs):
                return {
                    "conversation": {"id": conversation_id, "name": "Fixture", "is_self_chat": False},
                    "items": [],
                    "next_cursor": None,
                    "database_notice": None,
                    "coverage": {
                        "complete": True,
                        "page_size": 1,
                        "requested_page_size": 1,
                        "raw_items_scanned_total": 0,
                        "missing_timestamp_items_excluded_total": 0,
                        "retry_count_this_call": 0,
                        "since": None,
                        "until": None,
                        "ordering": "oldest to newest within each returned page",
                        "continuation_direction": "older messages",
                        "max_response_chars": 30_000,
                        "remote_pages_fetched_total": 1,
                        "remote_history_complete": True,
                        "archive_messages_in_page": 0,
                        "messages_with_truncated_content": 0,
                        "content_continuation_tool": None,
                    },
                }

            def get_message_content(self, conversation_id, message_id, **kwargs):
                return {
                    "conversation_id": conversation_id,
                    "message_id": message_id,
                    "offset": 0,
                    "content": "fixture",
                    "content_char_count": 7,
                    "next_offset": None,
                    "complete": True,
                    "is_deleted": False,
                    "edited_at": None,
                    "deleted_at": None,
                }

            def search_messages(self, query, **kwargs):
                return {
                    "items": [],
                    "max_matches": 1_000,
                    "returned_count": 0,
                    "returned_total": 0,
                    "results_truncated": False,
                    "truncation_reason": None,
                    "next_cursor": None,
                    "database_notice": None,
                    "coverage": {
                        "method": "fixture",
                        "server_side_search_used": False,
                        "complete_for_requested_scope": True,
                        "preparation_complete": True,
                        "index_generation": 1,
                        "requested_since": "2026-09-28T00:00:00Z",
                        "requested_until": "2026-09-29T00:00:00Z",
                        "requested_chat_ids": ["19:fixture@thread.v2"],
                        "chat_coverage": [],
                        "pages_fetched_this_call": 1,
                        "max_pages_to_fetch": 10,
                        "failed_chat_id": None,
                        "error": None,
                        "continuation_tool": None,
                    },
                }

            def export_chat(self, conversation_id, **kwargs):
                return {
                    "export_id": "0123456789abcdef0123456789abcdef",
                    "status": "complete",
                    "resource_uri": "teams-export://exports/0123456789abcdef0123456789abcdef/parts/0",
                    "mime_type": "text/plain; charset=utf-8",
                    "byte_size": 6,
                    "message_count": 1,
                    "conversation_id": conversation_id or "19:fixture@thread.v2",
                    "since": None,
                    "until": None,
                    "coverage": {
                        "complete": True,
                        "remote_pages_fetched_total": 1,
                        "remote_items_scanned_total": 1,
                        "missing_timestamp_items_excluded_total": 0,
                        "retry_count_this_call": 0,
                        "archive_included": True,
                    },
                    "next_call_tool": None,
                    "next_call_arguments": None,
                }

            def read_export(self, export_id, **kwargs):
                return {
                    "export_id": export_id,
                    "offset_bytes": kwargs.get("offset_bytes", 0),
                    "content": "sample",
                    "byte_size": 6,
                    "next_offset_bytes": None,
                    "complete": True,
                }

        async def exercise():
            async with create_connected_server_and_client_session(
                create_mcp_server(FixtureService())
            ) as session:
                page = await session.call_tool(
                    "get_messages",
                    {"conversation_id": "19:fixture@thread.v2", "page_size": 1},
                )
                self.assertFalse(page.isError)
                self.assertEqual(page.structuredContent["items"], [])

                exported = await session.call_tool(
                    "export_chat", {"conversation_id": "19:fixture@thread.v2"}
                )
                self.assertFalse(exported.isError)
                uri = exported.structuredContent["resource_uri"]
                self.assertTrue(uri.startswith("teams-export://exports/"))

                resource = await session.read_resource(AnyUrl(uri))
                payload = json.loads(resource.contents[0].text)
                self.assertEqual(payload["content"], "sample")
                self.assertTrue(payload["complete"])

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
