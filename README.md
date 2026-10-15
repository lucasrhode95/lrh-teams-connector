# Teams for MCP clients

A local MCP server and browser UI for one user's Microsoft Teams chats and channels.

- Find conversations by name or participant, then read chat and channel history.
- Search message text in chosen chats and dates, or discover messages online through Microsoft Search.
- Export conversations with their attachments, and download files linked from messages.
- Send a message, only after you explicitly authorize it.

Local search covers the history the connector has prepared, and online search returns bounded
pages; neither claims to cover every message or meeting transcript. See the
[requirements](docs/requirements.md) for scope and the [roadmap](docs/roadmap.md) for open work.

## Requirements

- Install [uv](https://docs.astral.sh/uv/getting-started/installation/).
- Use a Microsoft 365 account with Teams access.

A separate Python installation is optional: uv selects an installed Python 3.12 or newer, or installs a managed version when needed.

## Setup

```bash
uv sync --locked
uv run teams-connector auth
uv run teams-connector status
```

Run the `auth` command above and it logs you in. All `lrh-*` Microsoft 365 connectors share one login engine (`lrh-m365-auth`), so one login covers them all. Microsoft may ask for more than one approval, because the connectors use different Microsoft clients.

`status` shows the signed-in account and its cached profiles without touching the network. To switch accounts, run `auth --sign-out`, then `auth` again. Which profile serves which tool is in [architecture](docs/architecture.md#authentication-and-account-boundaries).

> Credentials are entered only on Microsoft's sign-in page. Tokens stay in the shared encrypted cache
> (`~/.lrh-m365-auth`, or `M365_AUTH_HOME`). If encryption is unavailable, sign-in fails closed.

The Teams store and local output files are stored under `%LOCALAPPDATA%\lrh-teams-connector` on
Windows, or `~/.local/share/lrh-teams-connector` on other systems. Set `TEAMS_CONNECTOR_HOME` to
override the Teams data directory. Set `TEAMS_CONNECTOR_FAVORITES` to `1` to mark the chats in the Teams Favorites folder (off by default).

### Configure a local MCP client

Replace `/path/to/lrh-teams-connector` with the absolute path to your clone.

**Claude Code** — add the server from a terminal:

```bash
claude mcp add --transport stdio lrh-teams-connector -- uv run --directory /path/to/lrh-teams-connector teams-connector mcp
```

See the [Claude Code MCP guide](https://docs.anthropic.com/en/docs/claude-code/mcp).

**Codex CLI** — register the same stdio command:

```bash
codex mcp add lrh-teams-connector -- uv run --directory /path/to/lrh-teams-connector teams-connector mcp
```

See the [Codex MCP guide](https://developers.openai.com/codex/mcp/).

**GitHub Copilot Chat in VS Code** — add this server to `.vscode/mcp.json` in your workspace:

```json
{
  "servers": {
    "lrh-teams-connector": {
      "type": "stdio",
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "/path/to/lrh-teams-connector",
        "teams-connector",
        "mcp"
      ]
    }
  }
}
```

Replace the sample path with your clone’s absolute path. In Windows JSON paths, escape backslashes (for example, `C:\\Users\\you\\lrh-teams-connector`). This example requires Copilot Chat’s Agent mode with local MCP support; see [VS Code’s MCP guide](https://code.visualstudio.com/docs/copilot/chat/mcp-servers).

> **Client compatibility:** These examples require a locally running agentic client that can launch a stdio MCP server. Hosted ChatGPT Web, Claude web chat, Copilot web chat, and similar services cannot start this local process. This project does not provide a remote MCP endpoint; hosted clients require one to be deployed separately.

## MCP tools

The MCP server exposes the tools below. Each one's inputs, results and caveats are in [architecture](docs/architecture.md#mcp-tools).

| MCP tool | Service module | Behavior |
|---|---|---|
| `auth_status` | shared auth provider | Read cached account/client credentials and resource-token expiry |
| `list_chats` | [`service/catalog.py`](src/teams_connector/service/catalog.py) | List chats and channels by recent activity, with kind filters and paging; with `TEAMS_CONNECTOR_FAVORITES` set, a favorite flag |
| `search_chats` | [`service/catalog.py`](src/teams_connector/service/catalog.py) | Match conversation names, and named group and meeting chats Teams matches by member (with a refresh) |
| `get_messages` | [`service/history.py`](src/teams_connector/service/history.py) | Read a conversation's history, newest pages first, with inclusive date bounds |
| `search_messages` | [`service/search.py`](src/teams_connector/service/search.py) | Search Teams messages online through Microsoft Search v2: any of up to 25 terms, optionally within chosen chats and dates; one page of hits with message ids |
| `export_chats` | [`service/export/`](src/teams_connector/service/export/) | Export conversations to `.txt`, `.jsonl` or `.zip`, with their attachments |
| `download_attachment` | [`service/files.py`](src/teams_connector/service/files.py) | Save the files a message names (shared files and pasted images), by message id or by its arrival time |
| `send_message` | [`service/send.py`](src/teams_connector/service/send.py) | Send one message (plain text, or HTML sent as written) after explicit authorization; the only write tool |

Message timestamps are returned in UTC.

## Browser export UI

Start the local UI when you want to choose conversations and download an export without an MCP
client:

```bash
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

The local store holds the chat catalog and account metadata, and nothing else. Message content is not
kept: Teams is the record, and every history read goes to Teams. The store is separated by Microsoft
account and checks its stored owner before use; it is not application-encrypted, so follow the storage
rules for the device. Generated exports and downloaded message attachments are cleaned up after seven
days the next time their output directory is used. Downloads are cached per source under the data directory's
`downloads` folder, so a file already there is returned without being fetched again.

A store left by an earlier version that kept messages is moved, without deletion, into a
`legacy-archive-<timestamp>` folder next to it the first time this version opens the account. The
catalog is then rebuilt from Teams. Delete that folder when you no longer need it.

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
- **The store belongs to another account:** do not delete or replace it. Sign into the expected
  account or choose a separate `TEAMS_CONNECTOR_HOME` location.

## Microsoft 365 connectors

This repository is part of the `lrh-*` **Microsoft 365** connectors: local tools that give MCP agents
access to a common corporate environment, its mail and calendar, chats and meetings, and files. Each
repository owns one product, and all of them share one sign-in.

<table>
  <tr><th width="270" nowrap>Repository</th><th>Responsible for</th></tr>
  <tr><td nowrap><picture><img src="https://raw.githubusercontent.com/lucasrhode95/lrh-m365-auth/main/docs/logos/entra-id.svg" height="29" align="absmiddle" alt="Entra ID"></picture>&nbsp;<a href="https://github.com/lucasrhode95/lrh-m365-auth">lrh-m365-auth</a></td><td>Shared <b>Microsoft</b> sign-in through <b>Entra ID</b>: one encrypted token cache and the profiles every connector uses</td></tr>
  <tr><td nowrap><picture><img src="https://raw.githubusercontent.com/lucasrhode95/lrh-m365-auth/main/docs/logos/outlook.svg" height="24" align="absmiddle" alt="Outlook"></picture>&nbsp;<a href="https://github.com/lucasrhode95/lrh-outlook-connector">lrh-outlook-connector</a></td><td><b>Exchange</b> mail, folders, drafts, rules, native signatures, and calendar events (schedule, agenda, attendees, <b>Teams</b> join link)</td></tr>
  <tr><td nowrap><picture><img src="https://raw.githubusercontent.com/lucasrhode95/lrh-m365-auth/main/docs/logos/teams.svg" height="24" align="absmiddle" alt="Teams"></picture>&nbsp;<a href="https://github.com/lucasrhode95/lrh-teams-connector">lrh-teams-connector</a></td><td><b>Teams</b> chats and channels, message attachments, and meeting chats, recordings, transcripts, recaps and notes</td></tr>
  <tr><td nowrap><picture><img src="https://raw.githubusercontent.com/lucasrhode95/lrh-m365-auth/main/docs/logos/sharepoint.svg" height="24" align="absmiddle" alt="SharePoint"></picture>&nbsp;<a href="https://github.com/lucasrhode95/lrh-sharepoint-connector">lrh-sharepoint-connector</a></td><td>General <b>OneDrive</b> and <b>SharePoint</b> file search and selected file retrieval (read-only)</td></tr>
</table>

A calendar event stays with **Outlook** even when it links to a **Teams** meeting; **Teams** owns that
meeting's chat and artifacts. A file linked from an email or a **Teams** message stays part of that
message, while the stored file itself is retrieved through **SharePoint**. Only verified identifiers
connect records across products.

## Development

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest
```

Tests use synthetic accounts and fake HTTP transports. They do not use browser captures or real
message content.

Project references: [documentation index](docs/README.md) · [requirements](docs/requirements.md) · [architecture](docs/architecture.md) · [API research](docs/api-research.md) · [roadmap](docs/roadmap.md).

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
