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

The shared connector uses MSAL's silent acquisition for both Teams audiences. MSAL returns a still-valid cached access token or silently refreshes an expired one with the cached refresh token. Only explicit Microsoft credential rejection or access denial prompts for `python -m teams_exporter auth`; exhausted network retries report connectivity/service failure without blaming the credentials. Cache reads, refreshes and writes share an inter-process lock, and refreshed cache writes use an atomic replacement. No token, cookie or authorization header is returned or logged.

On Windows, the cache path is the absolute `%LOCALAPPDATA%\scrape-teams-chat\.teams_token_cache.json`, regardless of the process working directory. On first use, a repository-root `.teams_token_cache.json` is copied atomically and verified before that legacy copy is removed. If the destination already exists, it is never overwritten; a byte-identical legacy copy left by an interrupted migration can be removed safely, while differing files are both preserved. The cache is kept beside the SQLite archive and is Git-ignored if a legacy repository copy remains.

The archive is persistent across MCP and UI restarts. By default it lives under `%LOCALAPPDATA%\scrape-teams-chat` on Windows and `~/.local/share/scrape-teams-chat` on other platforms. The filename includes a short SHA-256 fingerprint derived from the Teams tenant and user IDs. The database stores that fingerprint for ownership checks; it does not store the account name, tokens, cookies, or authorization headers. Messages are retained indefinitely after they have been fetched; there is no expiry or automatic purge.

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

The MCP input and output schemas are typed. Message and export responses are bounded and provide continuation details. All tools except `send_message` are read-only.

- `list_chats`: page through the live chat catalog, ordered by activity. Its
  `coverage` object identifies the source and scope, CSA's reported partial
  state, raw-feed counts, excluded non-chat/unclassified rows, and unresolved
  fallback names. `catalog_complete` applies only to `catalog_scope`; it is not
  a guarantee that every hidden or tenant-specific Teams object is exposed.
- `search_chats`: find chats by participant-name substring in locally indexed rosters. Use `sync_search_index` first; coverage reports missing rosters and unresolved names.
- `get_messages`: fetch and archive one page for a conversation ID with optional inclusive timestamp bounds, then return the corresponding merged archive page. `max_response_chars` bounds the serialized result; individual `content_truncated` bodies include `content_next_offset` and can be continued with `get_message_content`. Coverage includes remote/archive composition and `retry_count_this_call`; follow `next_cursor` for older messages.
- `get_message_content`: read a bounded character segment of a message already in SQLite. Continue with the returned `next_offset` until it is null. Soft-deleted messages are represented as `[Message deleted]`; retained pre-deletion text is not returned as current content.
- `search_messages`: search one or more explicit conversation IDs, or `all_catalog=true`, across a required inclusive `since`/`until` range. The service fetches and upserts every remote page in that scope before returning hits, then matches against the merged local SQLite records. Modes are normalized full-body `exact`, literal `substring`, and `all_terms`; text uses Unicode NFKC and case folding, with accents remaining significant. SQL wildcard characters are escaped. `max_matches` is a client-controlled total cap (default 1,000, with no application upper bound); `results_truncated` reports when more matches existed than the chosen cap. The smaller `result_limit` only controls one response page. Follow `next_cursor` during bounded fetch preparation and then through results. Preparation responses contain no hits until all requested chats/ranges were scanned; `coverage.chat_coverage` lists each chat, bounds, pages, and any failure. Teams' native search route is not verified or used.
- `export_chat`: create or resume a bounded, restart-safe export job. Repeat the call with `export_id` and the exact `next_call_arguments` until `status=complete`. The completed result includes an actual MCP `resource_link`, export ID, MIME type, size, message count and remote/archive coverage. Completed snapshots are stored next to the SQLite file under a `.exports` directory; the indefinite message archive remains the source of retained history.
- `read_export`: read a bounded UTF-8-safe section of the same completed snapshot inline. Continue with `next_offset_bytes` until `complete=true`.
- `sync_search_index`: fetch bounded history and participant rosters into the persistent local archive. Calls continue saved history links and rotate fairly across chats. Completed histories refresh their newest page while preserving older messages. Each chat result reports `message_retries_this_call`.
- `get_search_index_status`: inspect per-chat history and roster progress and requested-range coverage.
- `send_message`: send plain text to an existing chat ID. The tool verifies the live chat ID and expected chat name before making one POST attempt. Its required `user_confirmation` argument records the user's authorization for the recipient and content.

The send tool can target only conversations present in the live API catalog. A row shown in the Teams interface without a stable ID in that catalog cannot be selected by this tool.

### Sending safety

`send_message` creates an external side effect. An MCP client or agent must call it only when the user explicitly authorized sending that content to that chat, either in the current request or after the agent asked and received a clear yes. If authorization is absent or ambiguous, ask the user and wait. The required `user_confirmation` value is an audit assertion; filling it in does not grant permission. For a scheduled task, the task instructions themselves must explicitly authorize the destination and sending scope. Authorization for one test or one message does not grant ongoing permission. Searching, summarizing, exporting, or finding a pending task never implies permission to send. The server does not log or store the confirmation text, and does not forward it to Microsoft. The MCP client may handle tool arguments according to its own logging policy.

The message request is attempted once and is not retried automatically. If a connection error occurs after Teams may have accepted the request, verify the chat before trying again to avoid duplicates. Message content is plain text and HTML-escaped before it is sent.

The service does not use a verified server-side message search route. `search_messages` first scans and archives the explicit remote chat/date scope, so a complete result means that scope was fetched successfully and searched together with older local-only messages. It does not mean every chat or every lifetime message was included unless that scope explicitly requested them. If preparation is incomplete, continue with its cursor; the coverage lists the exact chats and dates searched so far. Participant search still depends on the rosters successfully indexed.

The `export_chat` result's resource URI follows `teams-export://exports/{export_id}/parts/{offset_bytes}`. Read it with standard MCP `resources/read`; its JSON payload contains one bounded section of `content`, a `next_resource_uri` when more bytes remain, and the completion/size fields. The `read_export` tool exposes the same sections as structured tool results. Each section begins and ends on a UTF-8 boundary, so concatenating returned content reconstructs the exact export. Resource support varies across MCP clients; use the inline tool if needed.

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
3. Call `search_messages` with the exact chat scope and date interval when looking for text; it fetches and archives that requested range before searching.
4. Use `sync_search_index` for a broader manual prefetch or to populate participant rosters for `search_chats`. It resumes saved history links and rotates fairly across chats.
5. Check `get_search_index_status` for manual-sync/participant coverage. For on-demand text search, use that call's own per-chat range coverage.

The local archive can retain messages after Teams stops exposing them, provided they were fetched before then. On-demand exports and searches lazily keep the ranges they retrieve; no expiry or recurring job is configured. If a retention policy requires capturing messages before Teams removes them, an external schedule would be needed. The database is not application-encrypted, so storage and deletion must follow the organization's policy.

## References

- [Model Context Protocol Python SDK: tools](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/servers/tools.md)
- [Model Context Protocol Python SDK: structured output](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/servers/structured-output.md)
- [Codex MCP connections](https://developers.openai.com/api/docs/guides/agents-api/tools/mcp)
- [Claude Code MCP configuration](https://docs.anthropic.com/en/docs/claude-code/mcp)
