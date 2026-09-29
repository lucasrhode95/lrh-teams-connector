# Teams Exporter Roadmap

## Purpose

This file tracks product requirements that remain incomplete or partially implemented, in implementation order. API routes, observed payloads, authentication details, and confidence notes belong in [Teams API research](teams-api-research.md).

The project uses app-owned Microsoft authentication and read-only Teams requests for its current export flow. The current implementation is a local Python backend with no user-facing web UI.

## Current implementation snapshot

| Capability | Status |
|---|---|
| Explicit MSAL login with app-owned token cache and device-code fallback | Implemented |
| Separate resource tokens and service-specific request headers for Chat Service, Middle Tier, and CSA | Implemented |
| CSA startup chat catalog with activity-time ordering | Implemented; partial catalogs fail clearly rather than returning an incomplete list |
| Display-name lookup for untitled one-to-one chats | Partially implemented; uses Middle Tier when CSA lacks names |
| Message retrieval by conversation ID, opaque continuation links, deduplication, count/date filters, chronological result | Implemented; date-bounded requests currently continue through older history unless the count is reached |
| Message formatter for rich text, semantic @mentions, deleted/system markers, attachment and image markers | Implemented |
| Plain-text export for one or multiple chats | Implemented |
| Fixture-based tests for connector, filtering, formatting, and export | Implemented |
| Local web UI for sign-in, chat selection, filters, and downloads | Not implemented |
| CSA delta synchronization for partial/incremental catalogs | Not implemented |

## Phase 1 — complete the local exporter

Implement these requirements in the order listed.

### P1.1 Complete and verify chat catalog loading

**Status:** Partial. CSA startup works when it returns a complete response. The connector detects `isPartialData` or `hasMoreChats` and raises an error; it does not yet request the additional catalog data.

Requirements:

- Handle partial CSA startup results and additional chat groups without silently omitting chats.
- Verify whether `teams/users/me/updates` plus `metadata.syncToken` can complete or incrementally maintain the catalog.
- Keep the visible chat order aligned with the live Teams UI. Current evidence supports descending `lastMessage.originalArrivalTime` for one session; retain it as an observed behavior, not a universal guarantee.
- Resolve one-to-one names where possible and show a safe fallback when the service provides no profile.
- Add fixture coverage for partial responses, delta updates, missing titles, and stable ordering.

Acceptance:

- The UI receives either a complete list or a clear actionable error; it never presents a partial list as complete.
- Chat ordering and title behavior are covered by tests using sanitized fixtures.

### P1.2 Make message filters efficient and reliable on long histories

**Status:** Partial. Conversation-ID retrieval, opaque `backwardLink` pagination, message deduplication, inclusive `since`, count limits, timezone validation, and oldest-to-newest output are implemented. A `since`-only request currently scans to the end of available history because timestamp/page-boundary stopping semantics are not fully verified. Continuation page-size behavior also needs verification.

Requirements:

- Verify page ordering, continuation behavior, requested `pageSize`, and segment-boundary fields against multiple live pages.
- Stop requesting older pages once the service data proves that they cannot contain messages at or after `since`.
- Preserve conservative behavior for anomalous timestamps and system messages; do not drop messages based on an unverified monotonic-order assumption.
- For finite `limit`, request only the remaining number where the service honors the requested page size.
- Keep the public result ordered oldest to newest and deduplicated by message ID.

Acceptance:

- A multi-page fixture proves the inclusive date boundary, count-plus-date behavior, duplicate handling, and early stop.
- A live check confirms the chosen early-stop rule on ordinary messages and relevant system-message cases before enabling it by default.

### P1.3 Build the local web UI and export flow

**Status:** Not implemented.

User flow:

1. Open a local page and see the available chats.
2. Select one or more chats.
3. Choose a message count, a date boundary, or both; provide an explicit unlimited option where useful.
4. Download the selected exports.
5. If authentication is missing, show a sign-in action and the device-code instructions. After sign-in completes, let the user reload or refresh the chat list.

Requirements:

- Keep authentication, pagination, filtering, and export logic in the backend services.
- Use the existing `TeamsConnector`, `TeamsMessageFormatter`, and `TeamsExporter` APIs.
- Show progress and clear errors for login, catalog loading, and exports.
- Avoid marking chats read as a side effect of listing, selecting, or exporting.

Acceptance:

- A user can complete the flow locally without using a terminal after starting the app.
- A user can export a selected chat with count-only, date-only, and combined filters.
- The UI displays an explicit authentication-needed state and can refresh after device-code sign-in.

## Nice-to-haves — after the local exporter works

These are lower priority than the Phase 1 requirements. Keep this order; Favorites/pinned chats are the lowest-priority item.

### N1. Download attachments and images

**Status:** Not implemented; exports currently preserve attachment/image markers and available links.

Download files and images into an export subfolder and use relative links in the text export. Preserve names where safe, handle duplicate names, and report failed downloads without losing the text export.

### N2. Display read/unread indicators

**Status:** Partially available in backend data; `ChatSummary` can carry an unread value from CSA. No UI exists.

Display only server-provided read/unread state. This feature must be read-only: listing, viewing, searching, or exporting must never mark a chat or message as read or change any status. If the service does not expose a reliable remote value, show no indicator.

### N3. Show profile pictures on messages

Resolve sender profile images and display them alongside messages in the UI. Keep failures non-blocking and avoid making image availability a requirement for message retrieval or export.

### N4. Send a message to a selected chat

Allow the user to compose and send a message to an explicitly selected chat. Require a clear user action for each send, show the destination and message before submission, and report service errors. Do not add automatic sending or sending during search/export.

### N5. Add a conversation viewer

**Status:** Not implemented.

Let the user open a selected chat and browse messages using the same retrieval path as export. Show useful timestamps, senders, and message content. Keep the viewer from changing read state.

### N6. Show Favorites/pinned chats in a separate first section

**Status:** Not implemented; the meaning of CSA `isSticky` has not been verified against Teams Favorites.

Display verified Favorites/pinned chats in their own section above the regular chat list. First confirm which remote field or route represents this UI state. Do not infer Favorites from `isSticky` until live comparison establishes that mapping. This is the lowest-priority nice-to-have.

## Phase 2 — expose chat search and extraction to Codex

Expose a local interface that Codex can call. An MCP server is the preferred option; it may use a small local HTTP API internally. The interface must use the existing app-owned sign-in and connector rather than requiring a separate Microsoft app registration.

### Primary AI workflows

These are the main user outcomes for the Phase 2 interface:

1. **Recurring attention summaries:** Let an AI run on a daily schedule to summarize pending tasks, requests, and recent topic updates that may need the user's direct or indirect attention. Each summary should link its findings to the source chat and message so the user can verify the context.
2. **Ad-hoc topic research:** Let the user ask a question about a subject across their Teams messages, optionally calling out a conversation to prioritize (for example, searching all chats about Analytics deployment while giving special attention to Analytics Chat). Return relevant findings with source chats, authors, timestamps, and enough surrounding context to check the interpretation.

Both workflows must state the search scope and any coverage limits. They are read-only: they must not send messages or change chat state. Summaries and research answers must be grounded in retrieved messages and distinguish explicit requests from inferred action items.

Required read capabilities:

- `list_chats`: list available chats with IDs, names, type, and available last-message metadata.
- `search_messages`: find messages by words or phrases and return chat ID/name, sender, timestamp, message ID where available, and a useful match excerpt.
- `search_chats`: find chats by participant names, including one-to-one and group conversations where participant data is available.
- `get_messages`: retrieve an entire conversation when no time bounds are provided, or a timestamp-bounded section when `since` and/or `until` are provided.
- Support the workflow of taking a search result and retrieving its full conversation or surrounding section by timestamp.

Search may use a Teams search service if its current behavior and coverage are verified, or a local index if needed. The chosen backend must make coverage and limits clear; search results must not imply that unindexed history was searched.

A persistent local message archive is also a future requirement for preserving messages already retrieved if Teams later stops returning them due to retention. It should deduplicate by conversation/message identity, preserve timestamps and available message IDs/edit/deletion metadata, merge later retrievals, and support repeat queries without calling Teams for already stored ranges. The exact storage technology is undecided. Define encryption, retention, deletion controls, and applicable company-policy constraints before enabling persistence.

Read tools must not mark chats or messages as read or modify any Teams state. Message sending is a separate optional write capability under N4 and is not part of the Phase 2 read interface by default.

Acceptance:

- Codex can list chats, search message contents, find chats from participant names, and retrieve whole or timestamp-bounded message history through documented tools.
- Search results can be followed by a retrieval call using their conversation ID and timestamps.
- A scheduled daily run can produce an attention summary with source references; an ad-hoc topic request can search across chats and prioritize a named conversation, with findings traceable to messages.
- Calls preserve authentication boundaries and expose no tokens, cookies, or authorization headers.

## Engineering constraints

- Use explicit, app-owned MSAL authentication and token caching.
- Keep credentials scoped to their service audience; do not reuse Teams browser tokens or browser IndexedDB.
- Keep the connector responsible for authentication and backend retrieval; keep formatting and file writing in their existing separate modules.
- Store only sanitized response fixtures in the repository.
- The API and ordering evidence is documented in `teams-api-research.md`; this plan records product requirements, status, priority, and acceptance criteria.
