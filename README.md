![Local MCP clients and the Teams connector, with a screenshot of the browser UI using synthetic Teams data.](https://raw.githubusercontent.com/lucasrhode95/lrh-teams-connector/main/docs/connector-overview.svg)

A local MCP server and browser UI for one user's Microsoft Teams chats, channels and meetings.

- Find conversations by name or participant, then read chat and channel history.
- Search message text and meeting transcripts, with each meeting's AI summary.
- Export conversations with their attachments, transcripts and summaries.
- Send a message or delete your own, only after you explicitly authorize it.

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

`status` shows the signed-in account and its cached profiles without touching the network. To switch accounts, run `auth --sign-out`, then `auth` again. Which profile serves which tool is in [architecture](docs/architecture.md#3-authentication-and-account-boundaries).

> Credentials are entered only on Microsoft's sign-in page. Tokens stay in the shared encrypted cache
> (`~/.lrh-m365-auth`, or `M365_AUTH_HOME`). If encryption is unavailable, sign-in fails closed.

The Teams store and local output files are stored under `%USERPROFILE%/.lrh-teams-connector` on
Windows, or `~/.local/share/lrh-teams-connector` on other systems. Set `TEAMS_CONNECTOR_HOME` to
override the Teams data directory. If your tenant's region differs from the default, set `TEAMS_CONNECTOR_NOTES_MEDIA_HOSTS` to its Loop media host (comma-separated), so Loop meeting notes can be read.

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

> **Client compatibility:** These examples require a locally running agentic client that can launch a stdio MCP server. Hosted chat apps cannot start this local process, and this project provides no remote MCP endpoint. ChatGPT can still reach it through OpenAI's [Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels), where your plan and workspace allow custom MCP apps; files the server saves stay on the computer running the tunnel.

The UI opens in your browser and stops after 30 minutes without activity:

```bash
uv run teams-connector ui
```

![125 percent screenshot of the local Teams connector UI inside a macOS-style browser window. People, chats and messages are synthetic.](https://raw.githubusercontent.com/lucasrhode95/lrh-teams-connector/main/docs/ui-preview.svg)

> The web UI is for manual exports. Its preview is the export's own plain text, built with the same
> settings as the download; how the page behaves is in [architecture](docs/architecture.md#9-local-export-ui).

## MCP tools

The MCP server exposes the tools below. Each one's inputs, results and caveats are in [architecture](docs/architecture.md#8-mcp-tools).

| MCP tool | Service module | Behavior |
|---|---|---|
| `auth_status` | shared auth provider | Read cached account/client credentials and resource-token expiry |
| `list_chats` | [`service/catalog.py`](src/teams_connector/service/catalog.py) | List chats and channels by recent activity, with kind filters and paging |
| `search_chats` | [`service/catalog.py`](src/teams_connector/service/catalog.py) | Match conversation names, and with a refresh the named group and meeting chats Teams matches by member and the one-on-ones with the people Teams suggests |
| `get_messages` | [`service/history.py`](src/teams_connector/service/history.py) | Read a conversation's history, newest pages first (a channel: threads by latest activity), with inclusive date bounds, and each transcribed meeting's AI summary after its marker (`include_summaries`, on by default) |
| `search_messages` | [`service/search.py`](src/teams_connector/service/search.py) | Search Teams messages online through Microsoft Search v2: any of up to 25 terms, optionally within chosen chats and dates; one page of hits with message ids |
| `search_transcripts` | [`service/transcripts.py`](src/teams_connector/service/transcripts.py) | Find words said in meetings, in the transcripts of recordings: complete within chosen conversations, or ranked across meetings (a video counts only when a meeting chat holds its recording), and each matched meeting's AI summary once (`include_summaries`, on by default) |
| `export_chats` | [`service/export/`](src/teams_connector/service/export/) | Export conversations to `.txt`, `.jsonl` or `.zip`, with their attachments, meeting transcripts and their AI summaries |
| `download_attachment` | [`service/files.py`](src/teams_connector/service/files.py) | Save the files a message names (shared files and pasted images), by message id or by its arrival time; lists those it could not fetch |
| `get_presence` | [`service/presence.py`](src/teams_connector/service/presence.py) | Availability, activity, device and last-active time of the other person in a one-on-one chat; nothing richer |
| `send_message` | [`service/send.py`](src/teams_connector/service/send.py) | Send one message (plain text, or HTML sent as written), optionally with inline PNG/JPEG images and local files uploaded to your OneDrive and shared with the chat, after explicit authorization; a write tool |
| `delete_messages` | [`service/delete.py`](src/teams_connector/service/delete.py) | Delete a set of your own messages in one chat after one explicit confirmation of the set; a delete with an uncertain outcome is never resent, and the result lists what was deleted, skipped and uncertain; a write tool |

Message timestamps are returned in UTC.

Which service backs each tool and route, and which private or public service each capability uses, is in [the architecture](docs/architecture.md#2-services-ports-and-adapters) and [requirements §4](docs/requirements.md#4-backend-choice).

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
node --test tests/surfaces/export_ui.test.cjs
```

`lrh-m365-auth` follows its `main` branch, and `uv.lock` records the exact commit. Run
`uv lock --upgrade-package lrh-m365-auth` to move to its latest commit.

Logs: each `mcp` and `ui` process writes a daily-rotated file under `<data directory>/logs/`, kept for seven days; the UI also logs to stdout. Set `TEAMS_CONNECTOR_LOG_LEVEL=DEBUG` for request timings ([architecture](docs/architecture.md#7-logging)).

Tests use synthetic accounts and fake HTTP transports. They do not use browser captures or real
message content.

Project references: [documentation index](docs/README.md) · [requirements](docs/requirements.md) · [architecture](docs/architecture.md) · [API research](docs/api-research.md) · [roadmap](docs/roadmap.md).

## Disclaimer

> [!WARNING]
> Unofficial: these tools sign in as Microsoft's own apps and use undocumented services. Check your
> organization's AI and data policies before giving an agent your work data. Provided as is, without
> warranty. Microsoft, Outlook, Teams, SharePoint and Entra are Microsoft trademarks; this project is
> not affiliated with Microsoft.

## License

Copyright 2026 Lucas Rhode. This is source-available software, not open source: the code is public
so anyone can read and audit it. The [PolyForm Internal Use License 1.0.0](LICENSE.md) lets you and
your company use and adapt the software for your own internal operations, including at work. The
licensor also permits personal, noncommercial use. Serving other companies with it is not internal
use: you may not sell it, ship it in a product, or offer it as a service, and you may not distribute
the software or a modified copy of it.
