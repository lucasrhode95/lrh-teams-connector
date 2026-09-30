# Project architecture

The Python application has two entry points over one shared Teams service:

- An MCP client launches the stdio server when it needs Teams tools.
- A person runs the local export page and downloads conversation files directly, without an AI agent or MCP client.

Both entry points call the same service layer and connector. MCP search and history sync also use the persistent SQLite archive. The local export page is independent of AI, but it still connects to Microsoft Teams and requires a valid sign-in and network access.

## Component map

| Module or file | Responsibility |
|---|---|
| `teams_exporter/teams_connector.py` | Authenticates through the app-owned Microsoft token cache and makes Teams service requests. It loads the chat catalog, conversation members, account/profile picture URIs, and message pages; follows opaque continuation links; and implements the message-send request. It contains the assumptions and low-level behavior for the private Teams routes. |
| `teams_exporter/teams_service.py` | Shared application layer used by MCP and the local page. It validates inputs, pages chat and message results, formats stable records and coverage, manages continuation cursors, and coordinates archive sync/search. Before sending, it validates the live chat ID/name and requires an authorization record; the MCP caller remains responsible for obtaining explicit user consent. |
| `teams_exporter/teams_message_formatter.py` | Converts Teams message bodies into readable text, including HTML/rich text, delimited mentions, system/deleted-message markers, and attachment or image markers. |
| `teams_exporter/teams_exporter.py` | Writes formatted conversation messages to plain-text export files. The local page combines these files into a ZIP when multiple chats are selected, or a single text file when combine is selected. |
| `teams_exporter/teams_search_index.py` | Stores chats, indexed messages, participant names, sync cursors, and coverage in a persistent SQLite database with FTS5 search. It isolates archives by signed-in account and provides a consistent backup operation. |
| `teams_exporter/mcp_server.py` | Exposes the shared service through the standard MCP protocol over `stdio`. It provides typed input/output schemas and bounded result pages; an MCP client starts the process on demand. |
| `teams_exporter/local_server.py` | Serves the simple conversation-selection and export page over loopback HTTP. It handles the browser API requests and downloads while leaving Teams retrieval and formatting in shared application code. |
| `teams_exporter/__main__.py` | Command-line entry point for interactive sign-in, the local page, and archive backup. |
| `teams_exporter/__init__.py` | Python package marker. The application imports concrete modules directly rather than re-exporting a combined public API. |
| `teams_exporter/*.svg` | Local fallback icons for a person, group chat, and call when Teams does not provide a usable picture URI. |
| `scrape_teams_chat.js` | Standalone browser-console scraper from the earlier workflow. It is separate from the Python application's connector, MCP server, and local page. |
| `tests/` | Functional tests using sanitized fixtures for connector behavior, pagination, formatting, archive coverage, MCP contracts, and local HTTP behavior. |

## MCP capabilities

The server exposes these tools:

- `list_chats` returns bounded pages from the live chat catalog.
- `search_chats` searches participant names already stored in indexed rosters.
- `search_messages` searches message text already stored in the local FTS5 index.
- `get_messages` retrieves live message pages by conversation ID, with optional inclusive time bounds and a continuation cursor.
- `sync_search_index` fetches bounded message and participant batches into the local archive and resumes saved history continuations on later calls.
- `get_search_index_status` reports per-chat indexing and requested-range coverage.
- `send_message` sends plain text to a chat in the live catalog. The caller must have explicit user authorization for that chat and content. It is the only write tool.

Teams Web's search UI and downloaded scripts show that Microsoft has search-related services, but the direct request contract and completeness have not been verified. The app therefore searches indexed local history and reports when coverage is incomplete; an empty result is not proof that Teams has no match. See the [MCP server guide](teams-mcp-server.md) for configuration and the [Teams API research](teams-api-research.md) for endpoint evidence and assumptions.

MCP timestamps and text export timestamps use UTC. The local page displays activity times in the browser's timezone. Listing, searching, archive sync, retrieval, and export do not mark messages as read.

## Local export page

The page is intended as a straightforward extraction tool for a person who wants to download Teams conversation history without asking an AI to search, summarize, classify, or produce the export. The person selects one or more chats, optionally chooses an inclusive date range and per-chat message count, and downloads the result. Clearing the date range and leaving the message count empty mean unlimited.

The local page requires the Python app to be running and uses its signed-in Teams connector. It is not an offline copy of Teams and cannot retrieve new messages while disconnected. Its purpose is to avoid requiring an AI product or a separate Teams-export application for a manual export.

## Data flow

```text
MCP client -> mcp_server.py -> TeamsService -> TeamsConnector -> Microsoft Teams
                                  |                  |
                                  |                  +-> live catalog, profiles, members, messages
                                  +-> SQLite/FTS5 archive and coverage

Browser -> local_server.py -> TeamsService -> TeamsConnector -> Microsoft Teams
                    |
                    +-> TeamsExporter -> text file or ZIP download
```

Authentication is completed separately with `python -m teams_exporter auth`. Microsoft handles the password and verification. The application keeps the resulting opaque tokens in its local token cache and does not return or log them. MCP startup and tool calls use silent authentication and return an actionable authentication-needed error when sign-in is required.

## Related documentation

- [Teams API research](teams-api-research.md) records observed routes, payloads, and confidence limits.
- [Product roadmap](teams-exporter-roadmap.md) records delivered work and remaining priorities.
- [MCP server guide](teams-mcp-server.md) covers install, sign-in, configuration, tools, and archive operations.
