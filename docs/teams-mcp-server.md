# Teams MCP server

This repository provides a client-independent MCP server over standard input/output (`stdio`). MCP clients launch the process when they connect, so no always-on HTTP listener is needed. The MCP server and the existing local export page share `TeamsService` and `TeamsConnector`; formatting and retrieval behavior therefore stay in application code instead of being duplicated in either interface. Read tools do not change Teams state; the `send_message` tool is the only write operation. See the [project architecture guide](project-architecture.md) for the module map and local export workflow.

## Install and sign in

From the repository root, install the Python dependencies:

```powershell
python -m pip install -r requirements.txt
```

Complete Microsoft sign-in separately:

```powershell
python -m teams_exporter auth
```

Microsoft handles the password and verification pages. The application uses the resulting app-owned token cache. MCP startup and tool calls only attempt silent authentication; when the cache needs attention, the client receives an authentication-needed error. Run the sign-in command above, then reconnect to the MCP server.

The archive is persistent across MCP and UI restarts. By default it lives under `%LOCALAPPDATA%\scrape-teams-chat` on Windows and `~/.local/share/scrape-teams-chat` on other platforms. The filename includes a short SHA-256 fingerprint derived from the Teams tenant and user IDs. The database stores that fingerprint for ownership checks; it does not store the account name, tokens, cookies, or authorization headers.

When `TEAMS_SEARCH_INDEX_PATH` points to a new file, the first signed-in account uses that path. If the file is already marked for another account, the existing file is preserved and the other account receives a separate account-specific file. Older indexes without ownership metadata are also preserved and reported as unverified; the application does not assume whose messages they contain. The local UI and MCP tool results include a warning when this happens.

Create a consistent backup while the app is running with:

```powershell
python -m teams_exporter backup C:\Backups\teams-archive.sqlite3
```

The backup uses SQLite's online backup API, so it includes committed data even while the archive is open. Copy that `.sqlite3` file to the other machine, then point `TEAMS_SEARCH_INDEX_PATH` at it before starting the app. The same Teams account fingerprint will allow it to open the archive; a different account will get a separate file while the copied backup stays intact. Add `--overwrite` only when you intend to replace an existing backup destination. Database files and common SQLite sidecars are Git-ignored.

## Laptop follow-through

The live read-only smoke test succeeded with the existing cached Microsoft session. If that cache expires, run the separate sign-in command above on the laptop and complete Microsoft's device-code flow. Then add the stdio configuration for the MCP client you want to use, pointing it at this repository's `.venv` Python executable.

The SQLite archive can support long retention after a full backfill, but this repository does not yet install a recurring sync schedule or provide a standalone scheduled-sync command. Reaching the 1+ year goal will need an initial backfill and a recurring run configured on the laptop; the desired cadence and Windows Task Scheduler setup can be decided then.

## Tools

The MCP input and output schemas are typed, results have bounded page sizes, and search responses state whether the indexed chats and requested dates are fully covered. All tools except `send_message` are read-only.

- `list_chats`: page through the live chat catalog, ordered by activity. Its
  `coverage` object identifies the source and scope, CSA's reported partial
  state, raw-feed counts, excluded non-chat/unclassified rows, and unresolved
  fallback names. `catalog_complete` applies only to `catalog_scope`; it is not
  a guarantee that every hidden or tenant-specific Teams object is exposed.
- `search_chats`: find chats by participant-name substring in locally indexed rosters. Use `sync_search_index` first; coverage reports missing rosters and unresolved names.
- `get_messages`: retrieve a bounded page for a conversation ID with optional inclusive timestamp bounds. Each message body is capped at 1,200 characters; `content_truncated` and `content_char_count` make clipping explicit. Coverage includes `retry_count_this_call`; follow `next_cursor` for older messages.
- `search_messages`: search locally indexed content with exact token-phrase or all-terms matching, optionally scoped to one chat and/or date range. Follow `next_cursor` for more matches.
- `sync_search_index`: fetch bounded history and participant rosters into the persistent local archive. Calls continue saved history links and rotate fairly across chats. Completed histories refresh their newest page while preserving older messages. Each chat result reports `message_retries_this_call`.
- `get_search_index_status`: inspect per-chat history and roster progress and requested-range coverage.
- `send_message`: send plain text to an existing chat ID. The tool verifies the live chat ID and expected chat name before making one POST attempt. Its required `user_confirmation` argument records the user's authorization for the recipient and content.

The send tool can target only conversations present in the live API catalog. A row shown in the Teams interface without a stable ID in that catalog cannot be selected by this tool.

### Sending safety

`send_message` creates an external side effect. An MCP client or agent must call it only when the user explicitly authorized sending that content to that chat, either in the current request or after the agent asked and received a clear yes. If authorization is absent or ambiguous, ask the user and wait. The required `user_confirmation` value is an audit assertion; filling it in does not grant permission. For a scheduled task, the task instructions themselves must explicitly authorize the destination and sending scope. Authorization for one test or one message does not grant ongoing permission. Searching, summarizing, exporting, or finding a pending task never implies permission to send. The server does not log or store the confirmation text, and does not forward it to Microsoft. The MCP client may handle tool arguments according to its own logging policy.

The message request is attempted once and is not retried automatically. If a connection error occurs after Teams may have accepted the request, verify the chat before trying again to avoid duplicates. Message content is plain text and HTML-escaped before it is sent.

The service does not use a verified server-side message search route. Search results cover only messages already in the local FTS5 index. An empty result with incomplete coverage is not evidence that Teams has no matching message. Participant search likewise depends on the rosters successfully indexed.

Message timestamps returned through MCP are normalized to UTC. The local page formats chat activity times in the browser's local timezone; text export timestamps are UTC.

Read-only GET/HEAD requests retry transient HTTP 408, 429, selected 5xx responses, and transport failures up to four total attempts per request. Retry-After and exponential delays are capped. A new request/page receives a fresh retry budget; write requests are never retried. The MCP retry fields count additional attempts for the current page request or per-chat sync call.

The local page runs exports as background jobs and reports the current chat, phase, pages, messages, and retries. Cancellation is checked between pages, during retry delays, while formatting, and while combining files. It cannot abort a network read already in progress; the request completes or reaches its 30-second timeout before cancellation is observed. Export message bodies are spooled to disk and the final file is published only after successful completion. A normal export does not yet have a durable resume checkpoint after a process restart or exhausted retry budget; index-sync pages have their own persisted progress.

## Configure an MCP client

Replace `<repo>` with the absolute repository path and use the Python executable where the dependencies were installed. On Windows, paths in TOML strings need doubled backslashes.

### Codex

Add this to `~/.codex/config.toml`:

```toml
[mcp_servers.teams_chat]
command = "C:\\path\\to\\scrape-teams-chat\\.venv\\Scripts\\python.exe"
args = ["-m", "teams_exporter.mcp_server"]
cwd = "C:\\path\\to\\scrape-teams-chat"
```

### Claude Code

Create or update the project-root `.mcp.json`:

```json
{
  "mcpServers": {
    "teams_chat": {
      "type": "stdio",
      "command": "C:\\path\\to\\scrape-teams-chat\\.venv\\Scripts\\python.exe",
      "args": ["-m", "teams_exporter.mcp_server"]
    }
  }
}
```

Open the project from its repository root so Python can import `teams_exporter`. These examples use the same stdio command and do not depend on an agent skill.

## Suggested use

1. Run the one-time sign-in command if the cached Microsoft session is missing or expired.
2. Call `list_chats` to find a stable conversation ID.
3. Call `sync_search_index` repeatedly in bounded batches to build local history. Its persisted continuation and fair chat rotation let a later client process resume.
4. Check `get_search_index_status` before interpreting `search_messages` or `search_chats` results as complete.

The local archive can retain messages after Teams stops exposing them, provided they were indexed first. A 1+ year retention goal therefore needs a successful historical backfill and a recurring sync schedule. This repository does not yet create that schedule; see the [roadmap](teams-exporter-roadmap.md). The database has no automatic expiration and is not application-encrypted, so storage and deletion must follow the organization's policy.

## References

- [Model Context Protocol Python SDK: tools](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/servers/tools.md)
- [Model Context Protocol Python SDK: structured output](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/servers/structured-output.md)
- [Codex MCP connections](https://developers.openai.com/api/docs/guides/agents-api/tools/mcp)
- [Claude Code MCP configuration](https://docs.anthropic.com/en/docs/claude-code/mcp)
