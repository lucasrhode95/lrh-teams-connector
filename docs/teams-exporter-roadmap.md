# Teams Exporter Roadmap

## Purpose

This roadmap records the state of delivered work and the remaining product requirements in priority order. Endpoint behavior, observed payloads, and confidence levels belong in [Teams API research](teams-api-research.md). MCP setup belongs in [MCP server guide](teams-mcp-server.md).

## Delivered capabilities

| Capability | Status |
|---|---|
| Microsoft sign-in with an application-owned token cache | Implemented. The application never receives the user's password. |
| Teams connector, shared message formatting, and text export | Implemented. The connector uses app-owned credentials and service-specific request behavior. |
| Chat catalog retrieval and activity ordering | Partially implemented. CSA data is enriched from the paginated Chat Service catalog when CSA marks its response partial. |
| Message history retrieval | Partially implemented. Opaque continuation links, ID de-duplication, chronological output, and inclusive date/count filters are supported. The observed server-side lower bound is used for the start date; the end date is checked locally. |
| Local export page | Implemented for the basic export flow. It supports chat selection, inclusive date range, an optional positive message limit, clear-to-unlimited behavior, single-chat text downloads, multi-chat ZIP downloads, and combined plain-text downloads. It displays local times in the browser timezone and exports UTC timestamps. |
| UI themes and pictures | Implemented. The page selects the system light or dark theme at load time, uses available account/chat picture URIs, and falls back to local SVG icons. It does not display online presence. |
| Client-independent MCP server | Implemented over stdio with typed, bounded tools for listing chats, participant search, message search, retrieval, index sync/status, and message sending. |
| Persistent local search archive | Implemented with SQLite/FTS5, account-specific paths, resumable bounded sync, coverage reporting, and a backup command. Search results only cover messages and participant names already indexed. |
| Read-only Teams operations | Implemented. Listing, searching, indexing, retrieving, and exporting do not mark chats or messages as read. MCP message sending is a separate write operation that requires explicit user authorization. |

## Remaining work, in priority order

### P1.1 Complete and verify chat catalog loading

**Status: Partially implemented.** When CSA marks its startup catalog partial, the connector follows Chat Service conversation catalog pages, de-duplicates chat IDs, and preserves richer CSA summaries for chats present in both sources. A live sample returned 960 raw catalog entries over three pages; a later CSA response contained 943 entries, all represented in the raw list. Raw-only entries included system streams and other nonstandard types, so the fallback is conditional on CSA's partial-result signal. The current implementation resolves some untitled one-to-one chats through Middle Tier profile lookup.

Remaining work:

- Verify which hidden, system, meeting, and other chat types belong in the user-facing catalog, especially among entries present only in the raw catalog.
- Investigate whether the Teams updates feed and its sync token can maintain or complete the catalog incrementally. Delta synchronization is not implemented.
- Confirm title and activity ordering behavior for fallback-only chats and across additional chat types.
- Add sanitized fixture coverage for any newly verified catalog and delta behaviors.

### P1.2 Make long-history retrieval more reliable

**Status: Partially implemented.** Message retrieval follows opaque continuation links and removes overlapping messages by ID. Results are ordered oldest to newest. A tested page-size parameter and the inclusive start-time lower bound are used; local checks remain authoritative. An upper-time request parameter behaved inconsistently at ordinary-message boundaries, so the end date remains a local filter. Index sync stores continuation progress and resumes it on a later call.

Remaining work:

- Verify page ordering, page-size behavior, continuation overlap, and date boundaries across additional chat types and long histories.
- Define and test bounded retries for transient page failures. A normal retrieval/export does not currently retry failed requests automatically. Index sync reports a failed page and can resume from the last saved continuation on a later sync call.
- Keep incomplete retrieval or index coverage explicit; a missing continuation link means the service reported no older page, not that Microsoft exposed every message ever sent.
- Revisit server-side end-time filtering only after its inclusive boundary behavior is verified.

### P2.1 Operate the archive for long-term retention

**Status: Partially implemented.** The local SQLite archive is durable, account-bound, resumable, and portable through the backup command. MCP sync calls fetch bounded batches and rotate through chats. There is no recurring sync scheduler, automatic historical backfill, age-based deletion, or application-level database encryption.

Remaining work:

- Configure a recurring sync process that continues historical backfill and refreshes recent messages. MCP stdio starts on demand, so a client or external scheduler must invoke sync.
- Decide how archive retention, deletion, backup protection, and device security should follow company policy.
- Complete an initial backfill while Teams still exposes the required history, then monitor per-chat coverage and gaps over time. The archive cannot recover messages that were no longer available before they were fetched.
- Connect scheduled AI summaries of pending tasks and topic updates to the MCP tools. The MCP server provides retrieval and search; it does not schedule or generate those summaries itself.

### P2.2 Keep search coverage accurate and evaluate Teams search

**Status: Partially implemented.** Message search uses local FTS5, and participant search uses locally indexed names. Tool results report the chats, date ranges, and indexing state that were searched. Unindexed histories and unresolved participant names are not represented as a complete mailbox-wide search.

Teams Web's signed-in search UI produced chat and message results in a live check, and downloaded Web bundles contain search-related route strings. The exact direct request contract, response schema, pagination, token audience, and history coverage have not been verified. The application therefore does not use those routes for server-side message or participant search. See Teams API research for evidence and assumptions.

Remaining work:

- If direct Teams search is pursued, verify the request and response contract, paging, coverage, and authentication using read-only requests before implementing it.
- Continue to report local index coverage with search results; never imply that unindexed Teams history was searched.

## Nice-to-haves

These follow the catalog, retrieval, and MCP/archive work. Favorites/pinned chats are intentionally last.

### N1. Download attachments and images

**Status: Not implemented.** Exports preserve attachment and image markers and available links. A future implementation can download files into an export subfolder, use safe relative links, handle duplicate names, and report failures without losing the text export.

### N2. Show read/unread indicators

**Status: Partially available in backend data; not shown in the UI.** Display only a reliable server-provided value. Listing, viewing, searching, and exporting must never change read state.

### N3. Show profile pictures on messages

**Status: Not implemented.** Resolve sender pictures and display them beside messages in a future conversation view. Picture failures must not block retrieval.

### N4. Add a conversation viewer

**Status: Not implemented.** Let the user browse a selected conversation through the shared retrieval path, showing timestamps, senders, and formatted content without changing read state.

### N5. Show Favorites or pinned chats in a separate first section

**Status: Not implemented.** The meaning of CSA isSticky has not been verified against Teams Favorites. Confirm the remote field or route before using it, then show verified Favorites/pinned chats above the regular list.

## Engineering constraints

- Keep Microsoft sign-in and token caching application-owned; never collect or store the user's password.
- Use credentials only for their intended Microsoft service audience. Never expose tokens, cookies, or authorization headers.
- Keep connector, formatting, export, and shared service behavior reusable by both MCP and the local UI.
- Treat reverse-engineered Teams routes as private, undocumented contracts that can change. Do not claim behavior that has not been verified.
- Keep repository fixtures sanitized.
- Keep read tools read-only. MCP sending must remain a separate tool and requires explicit authorization for the target chat and message content.
