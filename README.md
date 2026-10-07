# Teams Exporter

This project provides two ways to retrieve Teams conversations: a local page for direct, user-operated exports and a standard MCP server for clients and AI agents. Both use the shared Teams connector and application-owned Microsoft sign-in.

The connector follows Teams Desktop client conventions and uses endpoint behavior reverse-engineered from signed-in Teams Web traffic. The assumption that Web and Desktop use the same relevant service routes must be verified feature by feature. These are private, undocumented Microsoft routes, not a stable public API. This work is also inspired by [Maxim-Mazurok/teams-api](https://github.com/Maxim-Mazurok/teams-api).

## Sign in

Run `python -m teams_exporter auth` from the repository root to complete Microsoft's sign-in flow. Microsoft handles the password and any verification; this application never sees or stores the password. The shared connector stores opaque tokens in an OS-encrypted cache under `%LOCALAPPDATA%\scrape-teams-chat` on Windows (DPAPI), or the per-user data directory on other platforms, independent of the working directory. UI and MCP use the same MSAL cache; access tokens renew silently when Microsoft accepts the stored refresh token. If encrypted storage is unavailable, the connector fails closed and never writes a plaintext cache.

For explicitly requested local development probes, `python -m teams_exporter auth --unsecure` stores a separate plaintext MSAL cache in the repository's ignored `.local` folder. Any process that can read it can reuse the signed-in session. Direct scripts can select the same cache with `TeamsConnector(unsecure=True)`. The normal UI, MCP server, and sign-in command stay encrypted by default; development mode must be selected explicitly and its cache should be deleted when no longer needed.

## Use the MCP server

The standard MCP server uses `stdio`, so an MCP client starts it when needed. It does not need a continuously running HTTP service. Its tools list chats, find chats by indexed participant names, search a required chat/date scope, retrieve messages with continuations, export complete conversations as MCP Resources or bounded sections, sync and inspect the local archive, and send a message. Sending is the only write operation and requires explicit user authorization for the destination and content.

Message search uses SQL literal matching over the requested range: it first fetches and stores the selected chats' remote history, then searches the deduplicated SQLite archive, including messages retained locally. It requires an explicit inclusive date range and chat scope, and reports per-chat coverage. No native Teams keyword-search route is claimed. See the [MCP server guide](docs/teams-mcp-server.md) for installation, client configuration, tool details, and archive behavior.

## Use the local export page

Run `python -m teams_exporter serve`, then open `http://127.0.0.1:8765`. The page lets you find and select conversations, apply an inclusive date range and optional per-chat message limit, and download the results. One chat produces a text file; multiple chats produce a ZIP unless you combine them into one text file. The page follows the system light/dark theme at load time and uses account/chat pictures when available, with local icon fallbacks. The server rejects duplicate binds; stop an existing server process before restarting it on the same port.

This is the direct, user-operated export path: it does not require AI, an MCP client, or another application. It still needs network access to Teams and a valid application sign-in; “offline” here means independent of an AI tool, not disconnected from Teams. The server listens only on the local machine. The page is read-only and does not mark chats or messages as read. UI times use the browser's timezone; exported message timestamps use UTC.

The local page uses Python's threaded standard-library HTTP server. It does not provide HTTP/2 or HTTP/3, and this loopback UI does not replace the server stack to add either protocol.

## Application logs

The HTTP server writes formatted logs to stdout and to a daily-rotated file. The MCP server writes to the file only, keeping stdout available for MCP protocol messages. Logs are stored under `%LOCALAPPDATA%\scrape-teams-chat\logs` on Windows, or the standard per-user application data directory on other platforms. Each process uses its own log file so concurrent MCP and HTTP processes do not race while rotating files; seven daily backups are retained and older files are pruned at startup. Python warnings are redirected to the same logging system. The default level is `INFO`; set `TEAMS_EXPORTER_LOG_LEVEL=DEBUG` before starting a process for request and pagination detail. Operational logs record timings, statuses, and counts, not message bodies, search terms, chat names, identifiers, tokens, or authorization headers.

## Portability and research

Research probes call live endpoints with explicit authorization. Browser captures and
downloaded bundles are temporary exploratory inputs kept outside the repository; the
published research contains endpoint documentation and findings, not capture files or
capture-processing tooling.


The application has no fixed company account or tenant hostname. Public Microsoft client,
resource and endpoint settings are editable, and regional service URLs come from sign-in.
The [porting checklist](docs/project-architecture.md#porting-and-rechecking-an-environment)
explains the two implementations' settings and how to reassess grants, Graph capabilities
and private-service gaps. The research record contains anonymized historical observations,
not universal permission guarantees.

## Project documentation

- [Project architecture and module guide](docs/project-architecture.md)
- [Teams API research](docs/teams-api-research.md)
- [Product roadmap](docs/teams-exporter-roadmap.md)
- [MCP server guide](docs/teams-mcp-server.md)
