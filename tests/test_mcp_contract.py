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
                            "search_messages",
                            "sync_search_index",
                            "get_search_index_status",
                        }
                        self.assertEqual(set(tools_by_name), expected_names)
                        for tool in tools_by_name.values():
                            self.assertTrue(tool.inputSchema["properties"])
                            self.assertIsNotNone(tool.outputSchema)

                        search_tool = tools_by_name["search_messages"]
                        self.assertEqual(
                            search_tool.inputSchema["properties"]["match_mode"]["enum"],
                            ["phrase", "all_terms"],
                        )
                        self.assertNotIn(
                            "max_messages_to_scan", search_tool.inputSchema["properties"]
                        )

                        result = await session.call_tool(
                            "search_messages", {"query": "sanitized contract probe"}
                        )
                        if result.isError:
                            # Local archive access is account-bound; without a
                            # cached account the server must not open an
                            # unverified database.
                            error_text = " ".join(
                                item.text for item in result.content if hasattr(item, "text")
                            )
                            self.assertIn("Teams sign-in is required", error_text)
                        else:
                            self.assertEqual(result.structuredContent["items"], [])
                            self.assertIn("database_notice", result.structuredContent)
                            self.assertFalse(
                                result.structuredContent["coverage"][
                                    "complete_for_requested_scope"
                                ]
                            )

            asyncio.run(exercise_client())


if __name__ == "__main__":
    unittest.main()
