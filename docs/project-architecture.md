# Project architecture

The repository contains one current application: the `teams-connector` CLI and the
`teams_connector` Python package. MCP and the loopback web UI call the same domain services.

```text
MCP stdio ─┐
           ├─> services ─> remote ports/adapters ─> Microsoft Graph and Teams services
Local UI ──┘        │
                    └─> account-bound SQLite archive and local files

Outlook / Teams / SharePoint auth commands ─> shared lrh-m365-auth cache
```

## Product ownership

Teams owns chat and channel messages, their attachments, and Teams meeting chats, recordings,
transcripts, recaps, and meeting notes. Outlook owns the Exchange calendar event, schedule,
organizer, invitees, event attachments, and join link. A Teams meeting may have related resources in
both products; verified identifiers link those records. General OneDrive and SharePoint discovery
and retrieval belong to `lrh-sharepoint-connector`, even when a message links to the same file.

These are product boundaries, not a claim that every planned meeting or file tool is shipped.
Current capabilities and open work are listed in the [roadmap](teams-connector-roadmap.md).

## Code structure

| Layer | Responsibility |
|---|---|
| `src/teams_connector/__main__.py` | Canonical CLI (`teams-connector`) for shared sign-in, profile status, MCP, UI, and account-bound archive backup. Loads MCP and web dependencies only for those commands. |
| `surfaces/mcp_main.py` | Typed MCP tools over the service layer. Keeps stdio free of logs; `send_message` is the only Teams write tool. |
| `surfaces/web/` | Starlette app, static local export page, and loopback UI launcher. Uses a per-run session token, Host validation, and an idle timeout. |
| `service/` | Domain behavior for the catalog, message history, local matching, archive sync, exports, attachment downloads, profile photos, and sending. MCP and UI share these operations. |
| `remote/ports.py` | Interfaces used by services; service code does not depend on concrete HTTP adapters. |
| `remote/transport.py` | The only general HTTP client. Applies the allowlist, authentication, one-time 401 renewal, bounded idempotent retries, response limits, error mapping, and credential-free download redirects. |
| `remote/chat_service.py`, `remote/csa.py` | Private Teams service adapters for chat catalog/history/rosters and CSA updates. Their endpoints are undocumented and described as private in the research record. |
| `remote/graph.py`, `remote/graph_teams.py` | Microsoft Graph paging, batching, channel history, people/photos, file download, and the explicitly authorized message-send request. |
| `remote/*_mapping.py` and `remote/teams_html.py` | Convert wire responses into domain records and readable message text. Wire-format field names remain inside this layer. |
| `store/db.py` | Account-bound SQLite archive for the catalog, message history, participant rosters, and resumable sync progress. Contains no tokens or authorization headers. |
| `service/export/` | Text/JSONL formatting and safe output packaging. Exports stream from the archive, use exclusive temporary paths, and publish only after completion. |
| `service/localfiles.py` | Per-user output directories, retention cleanup, and collision-safe file creation. |
| `src/teams_connector/config.py` | Product CLI/data names and fixed public service URLs. Tenant authority, client IDs, token profiles, scopes, and shared-cache configuration live in `lrh-m365-auth`. |

The service layer receives adapters through the ports and is responsible for decisions and coverage.
Only the `remote/` layer knows Microsoft request bodies, headers, endpoints, paging links, and IDs.
The two surfaces validate external inputs before calling services; internal service routines rely
on their typed contracts.

## Authentication and account boundaries

The private `lrh-m365-auth` package supplies five named profiles shared by the three connectors:

| Profile | Used for |
|---|---|
| `teams.spaces` | Teams AuthZ/session exchange and Teams account identity. |
| `teams.csa` | Teams conversation catalog updates. |
| `teams.graph` | Graph teams, channels, people, and message operations. |
| `outlook.graph` | Outlook and validated OneDrive/SharePoint Graph capabilities. |
| `outlook.api` | Outlook Web operations owned by the Outlook connector. |

The auth package owns MSAL state and the encrypted cross-process cache; it does not issue Teams or
Graph requests or exchange a Teams Skype token. Each connector keeps its product wire logic. An
explicit `auth` command from any connector bootstraps the common profile catalog, silently reuses
accepted profiles, and requests approval only when needed. Microsoft can ask for more than one
approval because the public clients and resource audiences differ.

One active Microsoft account is bound to the shared cache. Signing out and choosing another account
invalidates the shared profile state; long-running processes refresh their account binding on the
next token request. The Teams composition root checks the current identity at each service boundary
and builds an account-specific Store before serving account data. Sign-in status means only that
credentials are cached. A successful token acquisition does not prove that a particular service or
conversation will authorize the operation.

MCP, ordinary UI use, and status reads do not launch interactive login. Encrypted storage is
mandatory by default. The explicitly selected `--unsecure` mode uses a separate plaintext development
cache and must never be used as an implicit fallback.

## Requests, caching, and storage

The MCP server is a stdio process scoped to one client session. It performs no network calls until a
tool needs them. The UI runs only while explicitly started, binds to loopback, and closes on exit,
Ctrl+C, or its idle timeout. There is no recurring sync task, watcher, remote listener, or background
refresh process.

The chat catalog may be reused for up to ten minutes. Explicit refresh waits for the remote read.
History, participant rosters, and sync continuation state are persisted in SQLite. Searches fetch
the requested conversation/date scope in bounded calls and match the stored messages; `coverage`
explains which chats/ranges were actually fetched. This is not a remote global-keyword search.

The database lives below `<Teams data directory>/accounts/<account fingerprint>/teams.sqlite3`.
Owner metadata is verified on open; an account mismatch raises an error instead of reusing another
account's archive. SQLite WAL, busy timeouts, per-operation connections, and short transactions allow
the MCP and UI processes to use the same account store. The local archive is not encrypted by this
application. Message content is retained until the user removes the archive.

Exports and downloaded attachments are written below the Teams data directory, are given exclusive
filenames, and are removed after seven days when their output directory is next used. A failed or
cancelled export does not publish a partial result. The `backup` command uses SQLite's online backup
API and refuses to replace an existing destination unless `--overwrite` is explicit.

## HTTP and safety rules

The shared transport accepts HTTPS only and checks Graph and Teams hosts before sending credentials.
Regional Chat Service URLs come from the Teams AuthZ response and are checked against the Teams
domain allowlist. Read-only requests retry a bounded set of transient failures; the retry delay
honors `Retry-After` within its limit. Writes are not retried. A request whose result is ambiguous
raises `WriteOutcomeUnknown` so an agent can inspect the target before taking another action.

Graph file-download endpoints can return a preauthenticated SharePoint redirect. The transport
allows one HTTPS redirect to a SharePoint download host and does not forward the Graph bearer token
to that host. Downloads have caller-supplied byte limits and are streamed to temporary files before
publication. URLs, query strings, bodies, authorization headers, message content, and search terms
are omitted from operational logs; credential-like values are redacted as another safeguard.

The local UI rejects non-local Host headers and requires a random per-process token on API calls.
It uses a restrictive Content Security Policy and renders message content as text. The browser page
does not receive a Microsoft token.

## Search behavior and boundaries

`search_messages` is a local archive workflow. The caller chooses the conversation scope, inclusive
date range, and matching mode. The connector retrieves that range in bounded page requests, stores
it, matches messages locally, and returns explicit preparation and per-conversation coverage. The
agent decides whether to request more pages or broader context. No claim is made that this route is
equivalent to Teams Web's cross-conversation search or its jump-to-message navigation.

Reverse-engineered endpoint shapes, source audits, and dated confidence notes are maintained in
[Teams API research](teams-api-research.md). Treat successful browser behavior as evidence, not a
guarantee that the connector's app-owned token has the same access.

## Development and checks

```powershell
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest
```

Tests use synthetic accounts and fake transports. Do not use browser captures or private messages as
fixtures. Keep `AGENTS.md` and `CLAUDE.md` identical. See the [MCP guide](teams-mcp-server.md) for
client setup, tool behavior, archive operations, and troubleshooting.
