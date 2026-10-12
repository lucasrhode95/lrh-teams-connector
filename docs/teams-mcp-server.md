# Teams MCP server

`teams-connector mcp` runs a local MCP server over standard input/output. The client starts it when
needed; it is not an HTTP service. The MCP and web surfaces use the same service layer and archive.

## Install and sign in

The project requires Python 3.12 or later and [uv](https://docs.astral.sh/uv/). Its shared auth
dependency is a private GitHub repository pinned to an immutable commit, so the developer or user
installing the connector needs GitHub SSH access to `lrh-m365-auth`.

From the connector root:

```powershell
uv sync --locked
uv run teams-connector auth
uv run teams-connector status
```

The explicit `auth` command bootstraps the shared profile set for Outlook, Teams, and SharePoint.
It silently reuses accepted credentials and requests Microsoft's device-code approval only for
profiles that still need it. Microsoft controls the password and MFA flow. Normal MCP startup and
tool calls never prompt for sign-in.

All three connectors use one encrypted MSAL cache for the active Microsoft account. By default it
lives under the current user's `~/.lrh-m365-auth`; set `M365_AUTH_HOME` before the process starts to
change the shared location. To change accounts, run:

```powershell
uv run teams-connector auth --sign-out
uv run teams-connector auth
```

Sign-out applies across Outlook, Teams, and SharePoint. Already-running connector processes discard
their remembered tokens on the next acquisition. Encrypted storage is required by default. The
explicit `--unsecure` option selects a separate plaintext development cache and must not be used for
normal MCP connections.

`status` displays cached client credentials and any resource-specific access-token expiry without
network requests. Teams profiles share one OAuth client, so client-credential presence can be the
same even when only one resource token is cached. `status --check` attempts token acquisition for
every profile. A successful check proves that a token was acquired; it does not prove access to all
Teams endpoints, chats, channels, or files. For example:

```powershell
uv run teams-connector status --check --json
```

## Configure an MCP client

Use an absolute path to the repository. `uv run --directory` locates the locked environment even
when the MCP client starts from another folder.

### Codex CLI

```powershell
codex mcp add lrh-teams-connector -- uv run --directory C:\path\to\lrh-teams-connector teams-connector mcp
```

### Claude Code

```powershell
claude mcp add --transport stdio lrh-teams-connector -- uv run --directory C:\path\to\lrh-teams-connector teams-connector mcp
```

### VS Code

Add this entry to `.vscode/mcp.json`:

```json
{
  "servers": {
    "lrh-teams-connector": {
      "type": "stdio",
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "C:\\path\\to\\lrh-teams-connector",
        "teams-connector",
        "mcp"
      ]
    }
  }
}
```

Replace the sample path with the clone's absolute path. Escape Windows backslashes in JSON and
TOML. These instructions are for local clients that can run a process; the connector has no remote
MCP endpoint.

## Tools and workflow

### Find conversations and read messages

1. Call `list_chats` to browse current chats and channels. It returns up to 50 items by default,
   supports `limit` up to 200 and opaque continuation `cursor`, and can filter by `kind`. A cached
   catalog up to ten minutes old may answer unless `refresh=true`. `coverage.source` identifies
   whether the response used the local catalog or a refresh.
2. Use `search_chats` to match a conversation name or participant name. Participant names are
   available only after rosters have been indexed by `sync_archive`; read roster coverage.
3. Call `get_messages` with the selected `conversation_id`. Pages are newest first; records within
   each page are oldest first. `since` and `until` are inclusive. `message_limit` caps the latest
   matching messages, while `page_size` controls the returned page. Follow `cursor` to get older
   messages. If a message body has `text_truncated=true`, call `get_message_content` with its ID and
   the indicated continuation offset.

Example arguments for a bounded history read:

```json
{
  "conversation_id": "19:sample@thread.v2",
  "since": "2026-08-01T00:00:00Z",
  "until": "2026-08-31T23:59:59Z",
  "page_size": 25,
  "message_limit": 100
}
```

### Search message text

`search_messages` currently performs local matching. Choose exactly one scope: a list of
`conversation_ids` or `all_chats=true`, and provide both `since` and `until`. Each call fetches a
bounded number of remote pages into the archive before matching. If `coverage.preparation_complete`
is false, repeat with the returned `cursor`. The response distinguishes incomplete retrieval from
an empty match set and reports date coverage per conversation.

The modes are `exact`, `substring` (the default), and `all_terms`. Matching is case-insensitive,
accents remain significant, and SQL wildcard characters in a query are literal. Local archive
messages remain searchable after the remote service no longer returns them. This tool is not the
Teams Web global-search endpoint and does not return a page number or a jump-to-message handle.

Example search across one explicitly selected conversation and month:

```json
{
  "query": "budget review",
  "since": "2026-08-01T00:00:00Z",
  "until": "2026-08-31T23:59:59Z",
  "conversation_ids": ["19:sample@thread.v2"],
  "match_mode": "substring",
  "limit": 20,
  "max_pages": 10
}
```

### Sync, archive status, and exports

`sync_archive` explicitly fetches history and optional participant rosters in bounded resumable
batches. It does not run in the background. Repeated calls rotate fairly across chats and resume
each chat's saved continuation. `archive_status` reports total messages and the history, date-range,
and roster coverage for the selected conversations.

`export_chats` writes a `.txt` file by default or `.jsonl` when requested. Multiple chats become a
ZIP unless `combine=true`. You can apply inclusive date bounds and a latest-message limit per chat.
The result contains a local path; use the MCP client's local file-reading capability to inspect it.
Exporting fetches the selected remote history first and can take time for a large selection.

`download_attachment` saves a file linked from a message already in the archive. Pass `name` when
the message has multiple downloadable files. Downloads stream to an exclusive local file and have
a size bound; the tool returns the saved path. General OneDrive and SharePoint discovery belongs to
the SharePoint connector. Email attachments belong to Outlook.

### Send a message

`send_message` is the only Teams write tool. The user must explicitly authorize both the recipient
and exact message content. Pass the current conversation name from `list_chats` as
`expected_chat_name`; the service checks it before sending. HTML characters are escaped and the
message is attempted once. A network failure after the request may leave the result uncertain, so
inspect the conversation before deciding whether another send is needed. A search result or request
to analyze a conversation does not authorize a send.

## Local files, logs, and backup

The default Teams data directory is `%LOCALAPPDATA%\lrh-teams-connector` on Windows and
`~/.local/share/lrh-teams-connector` on other platforms. Set `TEAMS_CONNECTOR_HOME` to choose another
data directory. The archive is stored under `accounts/<fingerprint>/teams.sqlite3`; separate
accounts do not share a database. Owner metadata is checked whenever the store opens. No tokens or
authorization headers are stored in the archive.

The archive is not encrypted by the connector. It retains the messages the user has fetched until
the archive is removed. Generated exports and downloaded files are under `exports/` and `downloads/`
and are cleaned up after seven days when those directories are next used. Backup while the app is
running with SQLite's online backup API:

```powershell
uv run teams-connector backup C:\Backups\teams-archive.sqlite3
```

The command refuses to overwrite an existing destination unless `--overwrite` is supplied. A
backup keeps its account binding and must be opened with the same signed-in account.

The MCP process logs to a per-process daily-rotated file, keeping stdout for protocol traffic. Log
files are under the Teams data directory's `logs/` folder. UI logs also go to stdout. Operational
records include timing, status, and counts, not message bodies, search text, chat names, IDs, tokens,
or authorization headers. Set `TEAMS_CONNECTOR_LOG_LEVEL=DEBUG` to enable safe request and paging
diagnostics.

## Troubleshooting

- **`uv sync` cannot fetch the shared auth dependency:** verify GitHub SSH access to the private
  `lrh-m365-auth` repository and rerun `uv sync --locked`.
- **MCP reports authentication required:** run the explicit `teams-connector auth` command, finish
  Microsoft's device-code and MFA flow, then reconnect the MCP process.
- **A status profile is missing:** run the explicit `auth` command to bootstrap all shared profiles.
  Microsoft may ask for a separate approval for another client or audience.
- **A request returns access denied:** the token can be valid while access to a specific resource or
  operation is denied. Use the error and coverage result; do not treat cache presence as service
  authorization.
- **The archive reports a different account:** preserve it and sign in as its owner, or choose a
  separate `TEAMS_CONNECTOR_HOME` directory.
- **The UI cannot use its preferred port:** it selects another free loopback port and prints the
  URL when it starts.

## Checks

```powershell
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest
```

All tests use synthetic accounts and fake HTTP transports; they do not use private browser captures
or real message content.

## References

- [Project architecture](project-architecture.md)
- [Teams API research](teams-api-research.md)
- [Product roadmap](teams-connector-roadmap.md)
- [MCP specification](https://modelcontextprotocol.io/specification/2025-06-18/server/tools)
