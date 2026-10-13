# LRH Teams Connector

A local MCP server and browser UI for one user's Microsoft Teams chats and channels. Both surfaces
use the same service layer, shared Microsoft sign-in, and account-bound local archive.

The connector reads chats, channel posts, message history, and message attachments. It provides
local scoped search over prepared history and a separate bounded online Teams Search v2 tool for
message discovery. Those tools have different coverage and continuation behavior; neither claims
to search every message or meeting transcript. See the [roadmap](docs/teams-connector-roadmap.md)
for scope and remaining live-validation work.

## Microsoft 365 connectors

This repository is part of the `lrh-*` Microsoft 365 connectors: local tools that give MCP agents
access to a common corporate environment, its mail and calendar, chats and meetings, and files. Each
repository owns one product, and all of them share one sign-in.

| | Repository | Responsible for |
|---|---|---|
| <img src="docs/logos/entra-id.svg" height="20" alt="Entra ID"> | [lrh-m365-auth](https://github.com/lucasrhode95/lrh-m365-auth) | Shared Microsoft sign-in: one encrypted token cache and the profiles every connector uses |
| <img src="docs/logos/outlook.svg" height="20" alt="Outlook"> | [lrh-outlook-connector](https://github.com/lucasrhode95/lrh-outlook-connector) | Exchange mail, folders, drafts, rules, native signatures, and calendar events (schedule, agenda, attendees, Teams join link) |
| <img src="docs/logos/teams.svg" height="20" alt="Teams"> | [lrh-teams-connector](https://github.com/lucasrhode95/lrh-teams-connector) | Teams chats and channels, message attachments, and meeting chats, recordings, transcripts, recaps and notes |
| <img src="docs/logos/sharepoint.svg" height="20" alt="SharePoint"> | [lrh-sharepoint-connector](https://github.com/lucasrhode95/lrh-sharepoint-connector) | General OneDrive and SharePoint file search and selected file retrieval (read-only) |

A calendar event stays with Outlook even when it links to a Teams meeting; Teams owns that meeting's
chat and artifacts. A file linked from an email or a Teams message stays part of that message, while
the stored file itself is retrieved through SharePoint. Only verified identifiers connect records
across products.

## Requirements and setup

- Install [uv](https://docs.astral.sh/uv/getting-started/installation/).
- Use Python 3.12 or newer; uv can install a managed Python if needed.
- Use a Microsoft 365 account with Teams access. Microsoft may request more than one device-code
  approval because the shared auth catalog uses separate first-party client/resource profiles.

From the repository root:

```powershell
uv sync --locked
uv run teams-connector auth
uv run teams-connector status
```

Any of the Outlook, Teams, or SharePoint connectors can start the same shared sign-in bootstrap.
It silently reuses accepted profile credentials and asks Microsoft only for profiles that still
need approval. Tokens for one active account are shared across connectors. To switch accounts, run
`uv run teams-connector auth --sign-out`, then `uv run teams-connector auth` and complete the new
sign-in. This signs the shared auth cache out for all three connectors. Running processes discard
their remembered tokens on the next token request.

`status` reports cached client credentials and any resource-specific access-token expiry without
making network calls. Teams profiles share one OAuth client, so client-credential presence can be
the same across them even when only one resource token is cached. `status --check` attempts token
acquisition for each profile; that confirms token availability, not access to every Teams endpoint
or individual conversation. Permission denials should be read separately from token acquisition.

The shared encrypted auth cache is stored outside this repository under the current user's
`~/.lrh-m365-auth` directory by default. Set `M365_AUTH_HOME` to choose another shared location.
The Teams archive and local output files are stored under `%LOCALAPPDATA%\lrh-teams-connector` on
Windows, or `~/.local/share/lrh-teams-connector` on other systems. Set `TEAMS_CONNECTOR_HOME` to
override the Teams data directory.

## MCP server

The MCP server uses stdio. Register this command with a local MCP client, replacing the example path
with the absolute path to your clone:

```powershell
uv run --directory C:\path\to\lrh-teams-connector teams-connector mcp
```

For example, add a server to Codex CLI:

```powershell
codex mcp add lrh-teams-connector -- uv run --directory C:\path\to\lrh-teams-connector teams-connector mcp
```

For Claude Code:

```powershell
claude mcp add --transport stdio lrh-teams-connector -- uv run --directory C:\path\to\lrh-teams-connector teams-connector mcp
```

For VS Code's local MCP support, add the equivalent server to `.vscode/mcp.json`:

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

Replace every sample path with the clone's absolute path. JSON and TOML strings on Windows require
doubled backslashes. These commands require a local client that can start an MCP process; this
project does not expose a remote MCP service.

### MCP tools

- `auth_status` reports the active account, cached OAuth client credentials and resource-token
  expiry per profile.
- `list_chats` returns recent chats and channels, with paging, kind filters, empty-chat control, and
  freshness coverage. A catalog no more than ten minutes old may answer unless `refresh=true`.
- `search_chats` matches conversation names and participant names indexed by `sync_archive`.
  Check roster coverage before treating participant results as complete.
- `search_remote_messages` searches message content online through Microsoft Search v2. It returns
  one bounded page with the service's message pointer, preview, timestamp, and conversation-ID
  candidates. A `conversation_id` is included only when a candidate exactly matches the local chat
  catalog. The Search v2 message pointer is not yet proven interchangeable with `get_messages`'s
  `message_id`; use the matched conversation and timestamp to fetch surrounding history. Follow its
  cursor only when additional hits are useful. It does not search meeting transcripts or recaps.
- `get_messages` returns one bounded page of a conversation, newest pages first and oldest first
  within each page. Follow `cursor` for older history. Date bounds are inclusive; `message_limit`
  counts the latest matching messages. Long bodies can be continued with `get_message_content`.
- `search_messages` requires an inclusive date range and either selected `conversation_ids` or
  `all_chats=true`. It fetches history into the local archive in bounded calls, then matches
  literally using exact, substring, or all-terms mode. Follow its cursor while preparation is
  incomplete and inspect per-chat coverage. This is local matching, not the Teams Web global search
  endpoint.
- `sync_archive` incrementally fetches history and participant rosters in bounded, resumable batches.
  `archive_status` reports which histories, date ranges, and rosters are covered.
- `export_chats` writes a `.txt` or `.jsonl` file for one conversation or a `.zip` for several; set
  `include_attachments=false` to omit linked message files. One conversation with selected
  attachments returns a ZIP containing its text export and downloaded files; without attachments it
  returns the plain text/JSONL file. Multiple conversations return an outer ZIP of per-conversation
  artifacts, so chats with files have their own ZIP and chats without files remain plain files. The
  client receives the single artifact directly or the outer ZIP. Exports can filter by date and
  per-chat message count.
- `download_attachment` saves a file linked from a message already in the local archive. Email
  attachments belong to `lrh-outlook-connector`; general OneDrive and SharePoint search belongs to
  `lrh-sharepoint-connector`.
- `send_message` is the only Teams write tool. Use it only after the user explicitly authorizes the
  destination and exact content. The connector checks the current conversation name and sends once;
  if the response is uncertain, inspect Teams before considering another send.

Message timestamps are returned in UTC. Search coverage describes the exact conversations and date
range prepared; a successful query does not imply that every Teams message has already been
downloaded.

## Browser export UI and archive backup

Start the local UI when you want to choose conversations and download an export without an MCP
client:

```powershell
uv run teams-connector ui
```

It opens a browser on a loopback URL, normally `http://127.0.0.1:8765`, and stops after 30 idle
minutes. Use `uv run teams-connector ui --help` for `--port`, `--no-browser`, and `--idle-minutes`.
If the preferred port is busy, the connector selects another free loopback port. The page uses a
per-run session token and rejects non-local hosts. It supports chat selection, an inclusive date
range in the browser's time zone, a per-chat message limit, and text or attachment-inclusive
downloads. The browser opens only after the server reports ready and prints the selected URL.
One conversation with no selected attachments downloads as a plain file; with attachments, its text
and available files are placed together in a conversation-named folder inside one ZIP. Multiple
conversations download as one outer ZIP, with an inner ZIP for each conversation with attachments
and a plain text file for each without them.

The SQLite archive persists messages the connector has read, participant names, catalog state,
and sync progress. It is separated by Microsoft account and checks its stored owner before use. The
archive is local and is not application-encrypted; follow the storage rules for the device. Messages
are retained until the archive is removed. Generated exports and downloaded message attachments are
cleaned up after seven days the next time their output directory is used.

Create a consistent online backup while connectors are running:

```powershell
uv run teams-connector backup C:\Backups\teams-archive.sqlite3
```

An existing destination is preserved unless `--overwrite` is supplied. The backup remains bound to
the same account identity; another account cannot silently open it as its own archive.

## Troubleshooting

- **MCP says sign-in is required:** run `uv run teams-connector auth`, complete Microsoft's device
  challenge and MFA if requested, then reconnect the MCP server.
- **A profile is missing:** the explicit auth command checks the shared catalog and asks only for
  profiles not yet accepted. `status --check` tests token acquisition for those profiles.
- **A specific chat or file says access denied:** the current sign-in may be valid while that item
  or operation is unavailable to the account. A cached token does not prove service permission.
- **The MCP dependency cannot be fetched:** `lrh-m365-auth` is installed from its public GitHub
  repository over HTTPS; check that github.com is reachable, then run `uv sync --locked` again.
- **The local UI does not start:** check its printed URL and any automatically selected port. Stop
  the UI with Ctrl+C; it also exits after the configured idle period.
- **The archive belongs to another account:** do not delete or replace it. Sign into the expected
  account or choose a separate `TEAMS_CONNECTOR_HOME` location.

## Development checks

```powershell
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest
```

Tests use synthetic accounts and fake HTTP transports. They do not use browser captures or real
message content.

## Documentation

- [Architecture](docs/project-architecture.md)
- [Teams API research](docs/teams-api-research.md)
- [Product roadmap](docs/teams-connector-roadmap.md)
- [MCP guide](docs/teams-mcp-server.md)

## Disclaimer

> [!WARNING]
> These tools sign in as Microsoft's own apps and use undocumented Outlook and Teams services.
> Giving an AI agent access to your work mail, chats or files may break your organization's policies
> on AI use and data handling, so check with your IT or security team first. The tools are provided
> as is, without warranty, and the authors are not responsible for any misuse or its consequences.
> Microsoft, Outlook, Teams, SharePoint and Entra are trademarks of Microsoft; this project is not
> affiliated with or endorsed by Microsoft.

## License

Copyright 2026 Lucas Rhode. This is source-available software, not open source: the code is public
so anyone can read and audit it. The [PolyForm Internal Use License 1.0.0](LICENSE.md) lets you and
your company use and adapt the software for your own internal operations, including at work. The
licensor also permits personal, noncommercial use. Serving other companies with it is not internal
use: you may not sell it, ship it in a product, or offer it as a service, and you may not distribute
the software or a modified copy of it.
