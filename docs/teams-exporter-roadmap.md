# Teams Exporter Roadmap

## Purpose

This file tracks product requirements that remain incomplete or partially implemented, in implementation order. API routes, observed payloads, authentication details, and confidence notes belong in [Teams API research](teams-api-research.md).

The project uses app-owned Microsoft authentication and read-only Teams requests for its current export flow. It provides both client-independent MCP access and a small local export page.

## Current implementation snapshot

| Capability | Status |
|---|---|
| Explicit MSAL login with app-owned token cache and device-code fallback | Implemented |
| Separate resource tokens and service-specific request headers for Chat Service, Middle Tier, and CSA | Implemented |
| CSA startup chat catalog with activity-time ordering | Implemented; partial CSA results fall back to paginated Chat Service conversations and preserve richer CSA entries |
| Display-name lookup for untitled one-to-one chats | Partially implemented; uses Middle Tier when CSA lacks names |
| Message retrieval by conversation ID, opaque continuation links, deduplication, inclusive date-range/count filters, chronological result | Implemented; `since` uses the observed server-side `startTime` lower bound, while `until` remains a local filter |
| Message formatter for rich text, semantic @mentions, deleted/system markers, attachment and image markers | Implemented |
| Plain-text export for one or multiple chats | Implemented |
| Fixture-based tests for connector, filtering, formatting, and export | Implemented |
| Client-independent MCP access over stdio | Implemented; see Phase 2 status |
| Persistent local SQLite/FTS5 message index | Implemented; bounded and resumable, but no scheduled refresh is configured |
| Local loopback web UI for chat selection, inclusive date-range/count filters, and text/ZIP downloads | Implemented; basic export flow only |
| CSA delta synchronization for partial/incremental catalogs | Not implemented |

## Phase 1 — complete the local exporter

Implement these requirements in the order listed.

### P1.1 Complete and verify chat catalog loading

**Status:** Partially implemented. When CSA marks its startup catalog partial, the connector now retrieves the Chat Service `view=mychats` catalog through its opaque `backwardLink` pages, de-duplicates IDs, and keeps CSA summaries when both sources contain the same chat. A live probe returned 960 raw catalog entries over three pages; a later CSA response contained 943, all represented in the raw list. The 17 raw-only entries included system streams and other nonstandard types, so the fallback is only used when CSA explicitly reports partial data. The catalog request supports pagination, but no server-side search, type, or date filter has been confirmed.

Requirements:

- Verify how partial CSA responses and additional chat groups map to the raw `view=mychats` feed, especially hidden/system stream types.
- Verify whether `teams/users/me/updates` plus `metadata.syncToken` can complete or incrementally maintain the catalog.
- Validate activity ordering and title behavior for fallback-only rows; current live UI alignment evidence applies to CSA ordering and is not a universal guarantee.
- Resolve one-to-one names where possible and show a safe fallback when the service provides no profile.
- Add sanitized fixture coverage for partial responses, delta updates, missing titles, and stable ordering.

Acceptance:

- A partial CSA response is completed by paginated raw catalog results, or produces a clear actionable error; it is never presented as complete without fallback.
- Chat ordering and title behavior are covered by tests using sanitized fixtures.

### P1.2 Make message filters efficient and reliable on long histories

**Status:** Partially implemented. Live tests confirmed that `pageSize` is honored on sampled first pages and that `startTime=<Unix epoch milliseconds>` acts as an inclusive lower bound on the message service. The connector sends `since` through `startTime` and still applies local inclusive checks. `until` remains local because tested `endTime` behavior was inconsistent at ordinary-message timestamp edges. Opaque continuations can overlap, so ID de-duplication remains necessary. Broader chat-type and boundary verification is still needed.

Requirements:

- Verify page ordering, continuation behavior, requested `pageSize`, and segment-boundary fields against more chat types and long histories.
- Determine whether a reliable `endTime` boundary exists before moving `until` filtering to the service.
- Preserve conservative behavior for anomalous timestamps and system messages; do not drop messages based on an unverified monotonic-order assumption.
- Keep verifying that finite `limit` page sizes are honored across continuation pages.
- Keep the public result ordered oldest to newest and deduplicated by message ID.

Acceptance:

- A multi-page fixture proves the inclusive lower boundary, count-plus-date behavior, duplicate handling, and local upper-bound behavior.
- Live checks confirm the `startTime` lower-bound semantics across ordinary and system messages, and a separate safe upper-bound rule is established before using `endTime`.

### P1.3 Build the local web UI and export flow

**Status:** Implemented as a small, single-page local interface served by Python's standard-library HTTP server. It lists the activity-ordered catalog, filters the list by chat name, supports one inclusive local-calendar date range and an optional per-chat message limit, and shows the signed-in user's name and email. It follows the operating system's light/dark preference when the page loads. It uses available account and chat avatar URIs directly, with local generic, group, and call SVG fallbacks; image failure or delay does not block the chat list. One selected chat downloads as text. Multiple chats download as a ZIP with one text file per chat, or as one plain-text file when combine is selected. A blank message limit means all matching messages. The date filter uses the browser's timezone; exported timestamps remain UTC. The server binds to loopback only. When authentication is missing, it shows the separate interactive sign-in command and can reload the catalog after sign-in. The UI does not mark chats or messages read.

User flow:

1. Start the local server and open the page to see the available chats.
2. Select one or more chats.
3. Optionally choose an inclusive start and end date and a maximum message count per chat; a blank count means unlimited. One calendar click selects a single day; a second click sets the end date.
4. Optionally combine multiple selected chats into one plain-text download.
5. Download the selected exports.
6. If authentication is missing, show the separate sign-in command. After sign-in completes, let the user refresh the chat list.

Requirements:

- Keep authentication, pagination, filtering, and export logic in shared backend code.
- Use the existing `TeamsConnector`, `TeamsMessageFormatter`, and `TeamsExporter` APIs.
- Show loading state and clear errors for sign-in, catalog loading, and exports.
- Avoid marking chats read as a side effect of listing, selecting, or exporting.
- Bind the HTTP server to loopback and keep access tokens out of browser code and responses.

Acceptance:

- A user can select one chat and download a text export, or select multiple chats and download a ZIP.
- Multiple chats download as a ZIP containing per-chat text files by default, or as one plain-text file when combine is selected.
- A user can combine an inclusive date-range filter with an optional per-chat message limit; leaving the limit blank retrieves all matching messages.
- The date range is interpreted in the browser's timezone. Exported timestamps use UTC.
- The UI displays an explicit authentication-needed state and reloads the catalog after the separate device-code sign-in completes.
- Listing and exporting remain read-only, and the HTTP listener is reachable only on the local machine.

## Phase 2 — MCP client access and local message archive

Expose a standard, client-independent Python MCP server over `stdio`. MCP clients start it on demand; it does not need a continuously running HTTP listener. Keep chat listing, search, formatting, indexing, and retrieval in shared application services so a future local UI can reuse them. Reuse the app-owned sign-in and connector without a separate Microsoft app registration.

**Status:** Implemented for read/search MCP access, with `send_message` as an explicitly authorized write tool. The seven tools are `list_chats`, `search_chats`, `search_messages`, `get_messages`, `sync_search_index`, `get_search_index_status`, and `send_message`. Their MCP input and output schemas are typed. Message and participant searches use a persistent SQLite database with FTS5 and per-chat coverage state. `sync_search_index` fetches bounded batches, stores opaque history continuations in SQLite, and rotates through the chat catalog so repeated calls can reach older chats as well as recent ones. Completed histories refresh their newest page while retaining older indexed messages. The MCP server instructs agents to ask for user authorization before every send; the required confirmation field records the authorization but is not itself permission. The local UI does not expose sending.

### Search backend findings

- Teams Web's signed-in search UI returned message and chat results for a keyword across multiple conversations during a read-only live check.
- The downloaded Teams Web bundles define `search/api/v2/query` and `searchservice/api/v2/query`, plus `/search/api/v1/suggestions`. Those route strings establish that the Web client has search services, but the exact request body, response schema, paging behavior, token audience, and history coverage have not been verified for direct calls.
- The implementation therefore does not claim or use server-side content search. `search_messages` searches only messages already in the local FTS5 index and reports whether the requested scope is fully indexed.
- No verified server-side chat-by-participant search contract is used. `search_chats` searches locally indexed participant rosters. The Chat Service member route returned nine member records for a live group chat; its names were blank. The existing Middle Tier profile lookup resolved all nine identifiers in that sample. The index reports unresolved names and treats those rosters as incomplete for name search.
- These are private, undocumented Microsoft service contracts and can change. More route evidence and live test detail belongs in [Teams API research](teams-api-research.md).

### MCP tools and archive behavior

- `list_chats`: list the live catalog in bounded pages.
- `search_chats`: search indexed participant names; return local-index coverage and a continuation cursor.
- `search_messages`: search local FTS5 content by exact token phrase or all query terms, with optional conversation and timestamp filters.
- `get_messages`: retrieve bounded pages from the live service with optional inclusive time bounds. Message bodies have a per-item character cap and explicit truncation metadata.
- `sync_search_index`: copy bounded message pages and participant rosters into local SQLite; repeat calls continue a history backfill and rotate fairly across chats.
- `get_search_index_status`: page through per-chat message and participant coverage, freshness, and timestamp limits.

The index is persistent and stored under the signed-in user's local application data directory by default; `TEAMS_SEARCH_INDEX_PATH` can choose another base path. Each database records a SHA-256 fingerprint of the Teams tenant and user IDs so the application can select the matching archive without storing tokens or the account name. Existing indexes without ownership metadata are preserved and treated as unverified; indexes bound to another account are preserved while a separate account-specific file is created. The local UI and MCP tool responses report this switch. A `backup` command uses SQLite's online backup API to produce a portable file that can be opened by the same Teams account on another machine. The database contains message text and participant names and is not encrypted by the application. Protect the device and its local account with the organization's approved disk-encryption and access-control settings. SQLite database files and sidecars are ignored by Git.

This archive can preserve messages already fetched if Teams later stops returning them. It cannot recover history that Teams no longer exposes before the first successful backfill. The `message_history_complete` flag means the service returned no older continuation link; it is not proof that Microsoft exposed every message ever sent.

For a 1+ year retention goal, the persistent archive provides the storage and deduplication layer. Repeated sync calls rotate through all chats and continue each chat's saved history cursor, so bounded runs can gradually backfill the catalog. A complete initial backfill must happen while Teams still exposes older history, followed by recurring syncs to capture new messages. MCP stdio runs only when a client starts it; this repository does not yet configure a scheduler or standalone scheduled-sync command. The SQLite file has no automatic age-based deletion and is not application-encrypted. Retention duration, deletion, backup, device protection, and company policy still need an explicit operational policy.

MCP and archive requirements:

- Keep listing, searching, syncing, and retrieving read-only; these operations must not mark chats/messages read or change Teams state. `send_message` is the sole write tool and requires explicit user authorization for the destination and content.
- Reuse the app-owned MSAL cache. MCP startup and calls use silent authentication only and return an actionable authentication-needed error; they never wait for device-code login or print protocol diagnostics to stdout.
- Return stable chat/message IDs, authors, timestamps, useful excerpts, and explicit index or retrieval coverage.
- Bound tool inputs/results and support continuation for long chat lists, histories, and search results.
- Never present local partial-index results as a complete mailbox-wide search. Coverage is false for unindexed chats, unfinished histories, stale latest ranges, or messages with timestamps that cannot be checked for a requested date range.
- Keep sanitized fixtures only. Tests cover pagination, continuation, overlapping-page deduplication, date bounds, FTS search, participant-name coverage, missing authentication, and MCP schemas.
- Document installation and generic stdio configuration, with Codex and at least one other client example. No agent skill is required for the server to work.

### Primary AI workflows

These are the main user outcomes for the Phase 2 interface:

1. **Recurring attention summaries:** Let an AI run on a daily schedule to summarize pending tasks, requests, and recent topic updates that may need the user's direct or indirect attention. Each summary should link its findings to the source chat and message so the user can verify the context.
2. **Ad-hoc topic research:** Let the user ask a question about a subject across their Teams messages, optionally calling out a conversation to prioritize (for example, searching all chats about Analytics deployment while giving special attention to Analytics Chat). Return relevant findings with source chats, authors, timestamps, and enough surrounding context to check the interpretation.

Both workflows must state the search scope and any coverage limits. Automated summaries and research answers must not send messages or change chat state. A separate direct request can use `send_message` after explicit user authorization. Summaries and research answers must be grounded in retrieved messages and distinguish explicit requests from inferred action items.

Required read capabilities:

- `list_chats`: list available chats with IDs, names, type, and available last-message metadata.
- `search_messages`: find messages by words or phrases and return chat ID/name, sender, timestamp, message ID where available, and a useful match excerpt.
- `search_chats`: find chats by participant names, including one-to-one and group conversations where participant data is available.
- `get_messages`: retrieve an entire conversation when no time bounds are provided, or a timestamp-bounded section when `since` and/or `until` are provided.
- Support the workflow of taking a search result and retrieving its full conversation or surrounding section by timestamp.

Message and participant search use the local index until a Teams search service's behavior and coverage are verified. Search results must not imply that unindexed history was searched.

A persistent local archive is now implemented for messages and available participant names. It deduplicates by conversation/message identity, preserves timestamps and message type, merges refreshes, and supports repeat searches without calling Teams. Remaining requirements for production retention are recurring scheduling, an explicit retention/deletion policy, and alignment with company policy. App-level database encryption is not implemented.

Read tools must not mark chats or messages as read or modify any Teams state. Message sending is a separate optional write capability under N4 and is not part of the Phase 2 read interface by default.

Acceptance:

- Any standard MCP client can list chats, search indexed message contents, find chats from indexed participant names, retrieve live whole or timestamp-bounded message history, and inspect/sync the local archive.
- Search results can be followed by a retrieval call using their conversation ID and timestamps.
- A scheduled daily run can produce an attention summary with source references; an ad-hoc topic request can search across indexed chats and prioritize a named conversation, with findings traceable to messages. The scheduler itself remains to be configured.
- Calls preserve authentication boundaries and expose no tokens, cookies, or authorization headers.

## Nice-to-haves — after the local exporter and MCP client access work

These are lower priority than the Phase 1 and Phase 2 requirements. The conversation viewer is the lowest-priority item.

### N1. Download attachments and images

**Status:** Not implemented; exports currently preserve attachment/image markers and available links.

Download files and images into an export subfolder and use relative links in the text export. Preserve names where safe, handle duplicate names, and report failed downloads without losing the text export.

### N2. Display read/unread indicators

**Status:** Partially available in backend data; `ChatSummary` can carry an unread value from CSA. No UI exists.

Display only server-provided read/unread state. This feature must be read-only: listing, viewing, searching, or exporting must never mark a chat or message as read or change any status. If the service does not expose a reliable remote value, show no indicator.

### N3. Show profile pictures on messages

Resolve sender profile images and display them alongside messages in the UI. Keep failures non-blocking and avoid making image availability a requirement for message retrieval or export.

### N4. Send a message to a selected chat

**Status:** Partially implemented through the MCP `send_message` tool. It verifies the chat ID and expected name against the live catalog, accepts plain text only, and requires the calling agent to record explicit user authorization. The MCP server instructs agents to ask and wait when authorization is missing or ambiguous. Scheduled tasks may send only when their own instructions clearly authorize the destination and sending scope. Only conversations present in the live API catalog are targetable; a Teams UI row without a stable catalog ID cannot be selected. The local UI does not expose sending yet.

The eventual UI should require a clear user action for each send, show the destination and message before submission, and report service errors. Search, summaries, and exports must never send automatically.

### N5. Show Favorites/pinned chats in a separate first section

**Status:** Not implemented; the meaning of CSA `isSticky` has not been verified against Teams Favorites.

Display verified Favorites/pinned chats in their own section above the regular chat list. First confirm which remote field or route represents this UI state. Do not infer Favorites from `isSticky` until live comparison establishes that mapping.

### N6. Add a conversation viewer

**Status:** Not implemented.

Let the user open a selected chat and browse messages using the same retrieval path as export. Show useful timestamps, senders, and message content. Keep the viewer from changing read state. This is the lowest-priority nice-to-have.

## Engineering constraints

- Use explicit, app-owned MSAL authentication and token caching.
- Keep credentials scoped to their service audience; do not reuse Teams browser tokens or browser IndexedDB.
- Keep the connector responsible for authentication and backend retrieval; keep formatting and file writing in their existing separate modules.
- Store only sanitized response fixtures in the repository.
- The API and ordering evidence is documented in `teams-api-research.md`; this plan records product requirements, status, priority, and acceptance criteria.
