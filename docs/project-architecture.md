# Project architecture

The Python application has two entry points over one shared Teams service:

- An MCP client launches the stdio server when it needs Teams tools.
- A person runs the local export page and downloads conversation files directly, without an AI agent or MCP client.

Both entry points call the same service layer and connector. MCP search and history sync also use the persistent SQLite archive. The local export page is independent of AI, but it still connects to Microsoft Teams and requires a valid sign-in and network access.

## Product ownership

Teams owns message/chat/channel relationships and their attachments. Planned Teams meeting
resources include meeting chat, recordings, transcripts, recap, and meeting notes. Outlook owns the
related Exchange calendar event: schedule/recurrence, agenda, organizer, invitees, event attachments,
and the join link. Verified references connect the two resources; another virtual-meeting platform
does not make an Outlook event a Teams meeting. General OneDrive/SharePoint file discovery belongs
to the planned SharePoint connector, even when its file results overlap a Teams message link.

Ownership describes the target product boundary, not a claim that every meeting-resource tool is
implemented. Current tools and outstanding work are listed below and in the roadmap.

## Installed package and legacy exporter

The installed `teams-connector` CLI uses `src/teams_connector`: `bootstrap.py` wires the service,
remote adapters and store; `surfaces/` provides MCP and the loopback UI; `auth/tokens.py` manages
encrypted MSAL profiles. Its current cache is `token-cache.bin` under the configured data directory.
See [installed package setup](../README.md#installed-package-setup) for the actual CLI commands.

The component map and capabilities below describe the original `teams_exporter` application, which
is still present in this checkout. Its commands and cache are separate. Selecting the package CLI
does not migrate the legacy application's cache or archive.

## Legacy exporter component map

| Module or file | Responsibility |
|---|---|
| `teams_exporter/teams_connector.py` | Authenticates through the app-owned MSAL cache at the absolute per-user data path, silently renews access tokens, serializes cache access across processes, and makes Teams service requests. It loads the chat catalog, members and profile picture URIs; follows opaque message continuations; and implements plain-text sending. It contains the assumptions and low-level behavior for the private Teams routes. |
| `teams_exporter/teams_service.py` | Shared application layer used by MCP and the local page. It validates inputs, fetches and upserts remote message pages, queries the merged local archive, formats stable records and coverage, manages continuations, and coordinates search/export jobs. Before sending, it validates the live chat ID/name and requires an authorization record; the MCP caller remains responsible for obtaining explicit user consent. |
| `teams_exporter/teams_message_formatter.py` | Converts Teams message bodies into readable text, including HTML/rich text, delimited mentions, system/deleted-message markers, and attachment or image markers. |
| `teams_exporter/teams_exporter.py` | Retrieves and upserts all remote pages for a selected date range, queries the deduplicated SQLite history (including previously retained local-only messages), then atomically writes a formatted UTC text export. The local page combines separate chat files into a ZIP only when multiple chats are selected; a combined export is one text file. |
| `teams_exporter/teams_search_index.py` | Stores chats, complete message bodies, authors, edit/delete metadata, participant names, sync/export cursors and coverage in a persistent, account-bound SQLite archive. It provides literal SQL search, stable pagination and consistent backups. It has no automatic expiry. |
| `teams_exporter/mcp_server.py` | Exposes the shared service through standard MCP over `stdio`. It provides typed tools for listing, bounded message retrieval/content continuation, date-scoped search, resumable exports, participant lookup, archive status/sync, and explicitly authorized text sending. Completed conversation exports are discoverable as MCP Resources. |
| `teams_exporter/local_server.py` | Serves the simple conversation-selection and export page over loopback HTTP. It manages background export jobs and their progress, cancellation, and downloads while leaving Teams retrieval and formatting in shared application code. |
| `teams_exporter/__main__.py` | Command-line entry point for interactive sign-in, the local page, and archive backup. |
| `teams_exporter/__init__.py` | Python package marker. The application imports concrete modules directly rather than re-exporting a combined public API. |
| `teams_exporter/*.svg` | Local fallback icons for a person, group chat, and call when Teams does not provide a usable picture URI. |
| `tests/` | Functional tests using sanitized fixtures for connector behavior, pagination, formatting, archive coverage, MCP contracts, and local HTTP behavior. |

## MCP capabilities

The server exposes these tools:

- `list_chats` returns bounded pages from the live chat catalog.
- `search_chats` searches participant names already stored in indexed rosters.
- `search_messages` requires a chat scope and date range, fetches that scope, stores it, then runs exact/substring/all-terms SQL matching over the merged archive. Coverage lists chats, dates, fetched pages and failures. It does not use an unverified Teams keyword-search route.
- `get_messages` fetches remote message pages, upserts them, and returns a bounded page from the merged local archive. `get_message_content` continues oversized individual bodies.
- `export_chat` persists its remote-page continuation, then creates a UTC text snapshot. The tool returns an MCP Resource link; `read_export` and `resources/read` retrieve bounded sections.
- `sync_search_index` fetches bounded message and participant batches into the local archive and resumes saved history continuations on later calls.
- `get_search_index_status` reports per-chat indexing and requested-range coverage.
- `send_message` sends plain text to a chat in the live catalog. The caller must have explicit user authorization for that chat and content. It is the only write tool.

Teams Web's search UI and downloaded scripts show that Microsoft has search-related services, but the direct request contract and completeness have not been verified. The app therefore uses an explicit date-bounded scan and local SQL matching. A complete result means the specified chat/date scope was fetched successfully, not that all lifetime history or every hidden chat was searched. See the [MCP server guide](teams-mcp-server.md) for configuration and the [Teams API research](teams-api-research.md) for endpoint evidence and assumptions.

MCP timestamps and text export timestamps use UTC. The local page displays activity times in the browser's timezone. Listing, searching, archive sync, retrieval, and export do not mark messages as read.

## Local export page

The page is intended as a straightforward extraction tool for a person who wants to download Teams conversation history without asking an AI to search, summarize, classify, or produce the export. The person selects one or more chats, optionally chooses an inclusive date range and per-chat message count, and downloads the result. Clearing the date range and leaving the message count empty mean unlimited.

The local page requires the Python app to be running and uses its signed-in Teams connector. It is not an offline copy of Teams and cannot retrieve new messages while disconnected. Its purpose is to avoid requiring an AI product or a separate Teams-export application for a manual export. Large exports report page/message progress and can be cancelled between service requests; cancellation waits for an in-flight request to return or time out.

## Data flow

```text
MCP client -> mcp_server.py -> TeamsService -> TeamsConnector -> Microsoft Teams
                                  |                  |
                                  |                  +-> live catalog, profiles, members, messages
                                  +-> SQLite archive and coverage

Browser -> local_server.py -> TeamsService -> TeamsConnector -> Microsoft Teams
                    |
                    +-> TeamsExporter -> text file or ZIP download
```

Authentication is completed separately with `python -m teams_exporter auth`. Microsoft handles the password and verification. The application keeps the resulting opaque tokens in its local token cache and does not return or log them. MCP startup and tool calls use silent authentication and return an actionable authentication-needed error when sign-in is required.

On Windows, the absolute cache file is `%LOCALAPPDATA%\lrh-teams-connector\.teams_token_cache.json`, in the same per-user folder as SQLite. UI and MCP share it. MSAL silently refreshes expired access tokens; an inter-process lock protects refresh/write races and the cache is updated atomically. Credential rejection gives the separate sign-in command; transient failures report connectivity without claiming credentials were rejected.

## Related documentation

- [Teams API research](teams-api-research.md) records observed routes, payloads, and confidence limits.
- [Product roadmap](teams-connector-roadmap.md) records delivered work and remaining priorities.
- [MCP server guide](teams-mcp-server.md) covers install, sign-in, configuration, tools, and archive operations.

## Porting and rechecking an environment

Use the [API research record](teams-api-research.md) as evidence and request-shape
material, then investigate the target environment. The repository currently has no
standalone research scripts or private probe-config file. The editable application
settings and constructor parameters are the starting points:

- The src/teams_connector implementation keeps tenant authority, client IDs, profiles,
  denied pairs and fixed public API URLs in config.py. TokenProvider accepts profiles,
  authority and a cache path. Changing clients may also require changing the sign-in
  flow; the current command assumes the documented shared-client profile setup.
- The teams_exporter implementation accepts tenant and a TokenProvider on TeamsConnector.
  Its TokenProvider accepts tenant, client_id and cache_path. Service URLs and resource
  scopes are named TeamsConnector constants. Keep this implementation independent until
  its migration is requested.
- Chat Service and Middle Tier regions come from the authenticated AuthZ response.
  They must not be replaced with a copied user's regional URL. Tenant SharePoint links
  come from service responses, not a fixed company hostname.
- Both implementations select accounts from app-owned sign-in. There is no fixed expected
  email. Preserve account-ownership checks and use a separate private cache/archive for
  each investigation. The src implementation supports TEAMS_CONNECTOR_HOME; the legacy
  provider accepts an explicit cache path.

Investigation order:

1. Select the tenant, approved client/resource profiles and an isolated private cache.
   Complete the application's Microsoft sign-in without reusing browser credentials.
2. Establish which resource tokens and Graph scopes that configuration actually grants.
   Recorded denied pairs are the application's tested baseline, not a global ban. Reassess
   them deliberately for a different environment before updating production settings.
3. Make authorized read-only capability calls: identity; Graph teams/channels/files;
   private-chat catalog and history; AuthZ regional discovery; CSA and Chat Service
   equivalents where Graph does not serve the requirement. Compare each result to the
   request shape and evidence in the research record.
4. Record status, error category, response shape, counts, paging/coverage and date.
   Missing tokens, HTTP errors and empty results need separate interpretation. A token
   grant alone does not prove the operation works. Omit people, chat/project names,
   identifiers, message text and credentials from published findings.
5. Prefer documented Graph where proven usable, private services only for proven gaps,
   and unsupported or unresolved where neither route is established. Changing endpoint
   hosts also requires reviewing host allowlists, credential audiences and API compatibility.
6. Verify with synthetic fixtures and fake transports. Run live writes only when the user
   explicitly authorizes the destination and content; never include them in routine checks.

Examples use fictional names and projects. Public source attribution remains intact.
