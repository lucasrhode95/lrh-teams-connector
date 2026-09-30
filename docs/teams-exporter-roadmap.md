# Teams Exporter Roadmap

## Scope and evidence

This is the consolidated register of gaps between the current application and the intended product. It replaces the separate lists of open work previously scattered through this roadmap. Endpoint facts and captured payloads remain in [Teams API research](teams-api-research.md); module responsibilities are in [Project architecture](project-architecture.md); usage is in [MCP server guide](teams-mcp-server.md).

This revision is consolidation only. No new browser inspection, traffic capture, online research, authentication attempt, API trial, or live send was performed for it. Future investigation paths below are proposals, not evidence that an endpoint or capability works.

Status terms:

- **Implemented:** documented application behavior exists; this does not establish universal behavior across Microsoft tenants or all chat types.
- **Partial:** a useful part exists, with a specific remaining gap.
- **Missing:** the requested capability is absent.
- **Unverified:** available evidence is insufficient. Suspected failure cases listed below require validation and are not newly confirmed bugs.

## Intended product

- Retrieve relevant one-to-one, group, meeting, and self conversations with stable identities, honest coverage, reliable pagination, and inclusive date/count filters.
- Offer a simple local page for manual text/ZIP exports without an AI client. Retrieving new Teams data still requires connectivity and authentication.
- Expose the same engine through client-independent MCP for listing, participant/content search, and complete conversation or surrounding-context retrieval.
- Support explicitly authorized text and attachment sending through MCP, including a usable workflow from a phone.
- Preserve previously fetched history in an account-bound archive, with repeatable backfill, refresh, backup, and an operational path to 1+ year retention.
- Enable an external AI client to research topics and produce scheduled attention summaries grounded in identifiable source messages.
- Add optional viewing and presentation features after the core capabilities. Favorites/pinned presentation remains last.

## What is already delivered

| Component | Documented capability and limit |
|---|---|
| Authentication and connector | App-owned MSAL sign-in/cache, service-specific credentials, chat catalog, message pages, member/profile lookup, and plain-text sending. The application does not receive the password. Additional upload audiences are not established by the text-send implementation. |
| Retrieval and formatting | Opaque continuation links, message-ID deduplication, inclusive date/count checks, chronological results, HTML/mention formatting, and system/attachment markers. Start-time filtering has limited live verification; end-time filtering remains local. |
| Local page | Loopback server, chat selection/name filtering, date range with clear-to-unlimited, positive optional message count, text/ZIP/combined export, system theme at load, account/chat picture URI fallbacks, local display times and UTC exports. |
| MCP | Seven tools: list_chats, search_chats, search_messages, get_messages, sync_search_index, get_search_index_status, send_message. Typed bounded responses and explicit search/index coverage; stdio launches on demand. |
| Archive | Persistent account-bound SQLite/FTS5 storage, bounded resumable sync, chat rotation, ownership isolation and online backup. Search is local and limited to indexed history/rosters. |
| Sending | Plain text through MCP with destination verification and a required record of explicit user authorization. That record is not proof of permission; the caller must obtain it. Attachment sending is absent. |
| Read behavior | Listing, retrieving, searching, syncing and exporting do not intentionally change Teams read state. |

## Proposed execution order and dependencies

This order reflects the recent priority of self-chat access and sending files while using a phone. Work IDs preserve the earlier roadmap references; their numbering is not a strict execution sequence. Independent investigations can proceed without waiting for unrelated work.

| Order | Work | What it unlocks |
|---|---|---|
| 1 | P1.3 Self-chat discovery and sender identity | Reliable retrieval and targeting of the user's own chat; useful sending test destination. |
| 2 | P1.4 File handoff from phone/client to MCP host | Establishes whether a file selected remotely can actually reach the sender process. |
| 3 | P1.5 Attachment upload, access and delivery | Useful file sending through MCP; depends on a verified destination and accessible source bytes. |
| 4 | P1.1 Catalog completeness and ordering | A trustworthy scope for listing, archive sync and search. |
| 5 | P1.2 Long-history filtering and export reliability | Correct, recoverable exports and backfills. |
| 6 | P2.1 Archive continuity and retention | Reliable historical and recent search after repeated syncs. |
| 7 | P2.3 Complete MCP content and continuation | Entire-message/context access within bounded client responses. |
| 8 | P2.2 Search completeness and Teams search research | Search across the known scope, potentially with less initial backfill. Native search research can run alongside archive work. |
| 9 | P2.4 Scheduled AI workflows | Daily attention summaries and topic research using the preceding capabilities. |
| Later | Nice-to-haves | Attachments in exports, indicators, message pictures, viewing and Favorites. |

## P1.3 Self-chat discovery and sender identity

**Status: Missing first-class self-chat handling; identity enrichment proposed.**

- Known: the connector exposes the signed-in account's user/tenant IDs. Normalized message records currently expose an author name, without a normalized sender ID or self flag. Sending requires a destination present in the live API catalog.
- Unknown: how Teams represents the self-chat in catalog responses and direct history requests, whether it is omitted by the currently used listing path, and which identifiers or flags reliably distinguish it. The earlier marker message and send attempts do not establish a general discovery contract.
- Proposed path: compare existing catalog evidence with the self-chat opened in Teams Web; capture the requests caused by opening it and reading history. Inspect corresponding Web code and the referenced teams-api implementation. Compare the thread ID, roster, explicit self-chat flags if present, and authenticated account identity. Replay the verified read request using app-owned authentication.
- Identity handling: distinguish a conversation with oneself from a message authored by oneself inside another chat. Normalize stable sender identifiers; matching display names alone is insufficient. Keep the real name/ID in records even if presentation uses "You." Existing archive records without sender IDs may need enrichment or must retain unknown classification.
- Completion evidence: self-chat can be listed or explicitly resolved, read and targeted across restarts; a normal direct chat is not misclassified. Any send to it must verify the actual destination, including when it is absent from the normal catalog. Missing evidence yields a clear error.
- Possible user help: open the self-chat and identify an existing marker message/time if traffic cannot be mapped automatically. A new test message is a separate, explicitly authorized action.

## P1.4 File handoff from the phone or MCP client

**Status: Unverified for the intended mobile workflow.**

- Known: the MCP process runs on a host computer; a phone's file path does not identify a file on that host. The current UI listens on loopback. Stdio and MCP registration alone do not transfer client attachments to the server.
- Unknown: whether the user's current mobile/remote client makes an attached file available to the agent and then to the MCP host, and how this differs across other MCP clients.
- Proposed path: first transfer a harmless sample through the intended client without sending it to Teams. Determine whether it exposes accessible bytes, a host path, or a resource reference that the host can resolve. Compare source and received size/hash. Do not assume that a URI supported by one client is universally transferable.
- Implementation choice unlocked: support a host file source as the generic baseline, plus a client-supported staging mechanism where needed. Avoid large binary/base64 data in model context. If a separate upload interface is needed, decide its authenticated access and deployment independently; the loopback export page does not already solve remote upload.
- Completion evidence: a file selected on the phone reaches the application intact and with an understood lifetime, size limit and cleanup behavior. Unsupported clients receive a precise explanation of the missing handoff.
- Possible user help: attach a small sample from the phone and confirm the MCP client/remote workflow being used. This test requires no Teams write.

## P1.5 Send attachments

**Status: Missing. Research contains candidate upload shapes, not a verified end-to-end implementation.**

- Known: current sending is plain text. Existing research distinguishes ordinary SharePoint/OneDrive files from AMS inline images. It records candidate upload/link routes, file metadata fields and AMS references.
- Unknown: whether app-owned authentication can obtain the required SharePoint/AMS audience tokens without additional consent; tenant storage discovery; the exact attachment message payload; recipient access for self, direct, group and meeting chats; upload limits and completion behavior.
- Proposed path: inspect already collected Web code and Maxim-Mazurok/teams-api for the upload-to-message sequence. Observe a deliberately authorized sample attachment sent through Teams Web, if required, including storage upload, permissions, metadata and final message creation. Verify resource audiences and replay each stage with the application's authentication. A route string or successful upload alone is insufficient.
- Scope: begin with ordinary files and an optional caption through a separate MCP attachment tool. Treat inline images as a second flow. UI sending remains optional. Bind authorization to the actual source file(s), destination and caption.
- Reliability questions: large/chunked uploads, name conflicts, interrupted transfers, changed source files, expired upload sessions, uploaded-but-unsent files, and ambiguous final-send responses. Any future cleanup of remote files is a separate mutation and must not silently delete pre-existing content. Keep verified file/message IDs so retries can avoid duplicate sends.
- Completion evidence: an authorized sample is delivered as a real Teams attachment, can be opened by the intended recipient, has verified access scope, and returns file/message identifiers. Self-chat success alone does not prove access for other recipients. Validate failure handling without broadly sharing the file or silently overwriting an existing one.
- Possible user help: a new sign-in if the cached account cannot obtain the upload resource token; a harmless sample; consent for the exact live upload/send test. If tenant policy blocks the needed token or permissions, record that blocker rather than assuming another token will solve it.

## P1.1 Catalog completeness, names and ordering

**Status: Partial.**

- Known: partial CSA responses trigger paginated Chat Service fallback; IDs are deduplicated and richer CSA rows retained. Historical samples contained 960 raw entries and 943 CSA entries, with raw-only system/nonstandard types. Those sample counts are not a current completeness guarantee.
- Unknown: coverage of hidden/archived/meeting/self chats and additional groups; title resolution for fallback-only chats; stable ordering around ties/missing timestamps; partial flags; CSA delta and sync-token semantics.
- Proposed path: compare catalog pages with a small matrix of known visible/hidden/direct/group/meeting/self conversations. Observe startup and updates requests, study the downloaded call sites, and test read-only paging/token progression. Compare metadata before/after a naturally occurring catalog change; deliberately changing a chat is not required by default.
- Completion evidence: catalog scope and exclusions are explicit, unexplained omissions are reported, fallback ordering/title cases have sanitized fixtures, and delta maintenance has a verified restart/expired-token recovery rule before adoption.
- Possible user help: identify expected chats or explain which existing Teams sidebar sections should appear. Manual sidebar comparison must not cause automatic unhiding, pinning or membership changes.

## P1.2 Long-history filtering and export reliability

**Status: Partial.**

- Known: opaque continuation and ID deduplication exist; sampled startTime behavior is inclusive; endTime was inconsistent at ordinary-message boundaries and remains a local filter. Requested page size has limited live verification. Index sync persists progress, while normal retrieval/export has no automatic failed-request retry.
- Unknown: page-size behavior on subsequent pages, overlap/order across chat and event types, segment-boundary semantics, and whether a reliable server-side upper bound or early-stop rule exists.
- Proposed path: build a reference sample around exact timestamps, then vary one request parameter at a time. Compare first and continuation pages, equal start/end boundaries, unlimited/count-limited histories, system events and anomalous timestamps. Inspect Web pagination call sites and existing reverse-engineering sources. Only enable an optimization after matching the reference message IDs and bounds.
- Application work: define bounded retry/backoff for transient read failures, timeouts and throttling; preserve completed progress. Plan prompt cancellation and clear progress for long exports. Determine memory/disk and responsiveness behavior on large histories before claiming unlimited extraction is operationally reliable.
- Completion evidence: matching IDs/counts under supported filters; explicit errors/coverage on incomplete reads; restart/retry/cancel exercises preserve correct progress without duplicate output. Never infer complete lifetime history merely from an absent older-page link.
- Possible user help: identify a representative long conversation and known boundary messages. Read-only probing and sanitized fixtures should otherwise be unattended.

## P2.1 Archive continuity and 1+ year retention

**Status: Storage and bounded backfill implemented; continuous retention incomplete.**

- Known: SQLite persists messages and history cursors across restarts, separates accounts and supports online backup. Sync rotates through chats; documented completed-history refresh fetches the newest page. There is no configured scheduler or automatic age-based deletion.
- Questions to verify, not newly confirmed failures: can a busy chat produce more new messages between syncs than one refresh page covers? Are newest messages refreshed while historical backfill remains unfinished? How are expired saved continuations, older edits/deletions, roster changes and catalog removals handled? Does coverage distinguish stored rows from a continuously covered interval?
- Proposed path: use sanitized fixtures for multi-page bursts, long backfill plus new arrivals, interruptions, expired continuations, membership changes and edits/deletions. Then compare a bounded live interval with the stored IDs. Evaluate separate progress for historical backfill and catching up on new messages, with overlap/gap detection rather than newest timestamp alone.
- Operational work: choose a recurring sync entry point and scheduler, silent-auth failure reporting, backup cadence and a restore exercise using the same account. Decide retention and deletion behavior before implementing purge. App-level encryption is absent; whether it is required is an operational decision, not assumed unfinished product scope.
- Completion evidence: no unreported gaps in the exercised scenarios; measured coverage/freshness after restarts; successful backup/restore and account separation; recurring sync with actionable reauthentication needs. A year of retention requires capture while Teams still exposes the history and continued collection thereafter.
- Possible user help: choose sync cadence, retention/deletion policy and machine availability; complete sign-in when Microsoft requires interaction.

## P2.3 Complete content through bounded MCP responses

**Status: Bounded tools implemented; whole-message access needs clarification.**

- Known: get_messages documents a 1,200-character body cap with explicit truncation metadata. Older-message cursors do not by themselves provide the missing remainder of a long individual message. No full-body continuation is documented.
- Unknown: the required path for retrieving complete long bodies and quoted context; cursor lifetime/restart behavior for interactive retrieval; source references clients can reliably follow.
- Proposed path: check existing tool contracts against a long sanitized message and a conversation spanning many pages. Specify either body-offset retrieval, a bounded message-detail tool, or a complete export resource accessible to the client. Distinguish interactive result cursors from persisted archive-sync progress.
- Completion evidence: a client can reconstruct a full message and conversation without silent clipping or flooding context; expired/mismatched cursors fail clearly; IDs, author identity and UTC timestamps preserve source traceability.
- Possible user help: only if a particular MCP client cannot consume the chosen resource/file mechanism.

## P2.2 Search coverage and direct Teams search

**Status: Local search implemented; native search contract unverified.**

- Known: FTS5 supports phrase/all-term message search and indexed-roster participant lookup. Coverage is exposed. Teams Web search returned results in an earlier visual check; route strings alone did not establish request body, audience, paging or completeness.
- Proposed path: inspect the previously downloaded search call sites and referenced reverse-engineered client. Capture a controlled existing keyword/phrase and participant search in Teams Web, including relevant HTTP or WebSocket traffic if used. Replay read-only requests using app-owned credentials. Compare IDs, scope, dates, pagination and unavailable/hidden chat cases against known messages and local results.
- Local-search questions: missing/stale rosters, unresolved names, stale catalog scope, edits and recently arrived messages must affect coverage correctly. Fuzzy/semantic matching is a possible enhancement; the currently requested words/phrases do not require adding it.
- Completion evidence: the documented search scope matches what was actually searched, including when there are no hits. Native search is used only for verified scopes; it need not be complete lifetime search to be useful if its limits are explicit. Local indexing remains a valid implementation when native search cannot be verified.
- Possible user help: supply known phrases, participant names and expected conversations for comparison. New sign-in or consent may be needed for an additional resource audience; neither is assumed to be available.

## P2.4 Scheduled attention summaries and topic research

**Status: Retrieval primitives exist; recurring workflow is not configured.**

- Intended outcomes: daily pending tasks/requests requiring direct or indirect attention, recent topic updates, and ad-hoc research across chats with optional priority conversations.
- Proposed path: define a small evaluation set of existing requests, replies and resolutions. Exercise the client workflow: update coverage, search, fetch surrounding context, and cite chat/message IDs, authors and times. Test timezone cutoffs, stale indexes, unfinished coverage and contradictory follow-up messages.
- Open decisions: which MCP client schedules the run, when its host is available, how reauthentication is surfaced, delivery location, and how to distinguish pending work from completed or merely inferred tasks.
- Completion evidence: a scheduled run reports its actual time/chat coverage, produces traceable findings, distinguishes explicit requests from inferences, and exposes failures or missing scope. Scheduling alone does not prove useful task extraction.
- Possible user help: set the schedule and notification destination, clarify examples of pending/completed work, and authorize any external delivery separately. Summarization never implies permission to send Teams messages.

## Nice-to-haves and their investigation paths

These remain below core retrieval, MCP, archive and the newly requested attachment sending. Attachment downloading remains optional.

| Item | Current gap | Proposed path and completion evidence |
|---|---|---|
| N1. Download attachments/images in exports | Markers/links exist; binary downloads do not. | Verify file/AMS download authentication from existing attachment metadata, redirects, expired links and names; package safe relative paths and report individual failures without losing text. A restored export must open its included files. |
| N2. Remote read/unread indicators | CSA has an unread value; UI does not display it. | Compare existing remote values to the Teams UI, including missing/stale values. Show only verified states. Do not invoke mark-read endpoints to test this feature. |
| N3. Pictures on messages | Account/chat picture URI fallback exists; message sender pictures do not. | Build on verified sender identity, compare available URI behavior and authentication, and preserve generic fallbacks. Direct URIs are preferred over local image downloads. |
| N4. Conversation viewer and rendering fidelity | No viewer; export/formatting exist. | Reuse retrieval; use sanitized examples for mentions, replies, multiline text, rich/system messages and missing metadata. Preserve raw/unknown event meaning rather than inventing an actor/action. Verify functionality and shared formatting; appearance tests are not required. |
| N5. Optional presence indicator | No status displayed; a usable presence source is unverified. | Inspect existing account data and Web presence usage only if inexpensive and accessible with available authentication. Otherwise defer, as permitted. Profile failure must not block the UI. |
| N6. Favorites/pinned section | isSticky is not a verified Favorites marker. | Compare already configured Favorites with Web responses/call sites. Once mapped, display the section first without changing Teams state. This remains the lowest priority. |

## How future investigations should be conducted

- Start from existing API research, downloaded Teams HTML/JS and [Maxim-Mazurok/teams-api](https://github.com/Maxim-Mazurok/teams-api). Check source age and assumptions before reusing behavior.
- Use browser navigation and traffic observation to identify the requests behind a specific action. Inspect WebSocket traffic only if the observed behavior uses it. Then vary one parameter at a time in bounded direct trials.
- Keep browser-session success distinct from success with the application's app-owned authentication. The latter is required for an unattended connector capability.
- Record the hypothesis, source, observed result, scope, failure mode and completion evidence. Add confirmed route facts to API research and update status here; keep unsuccessful hypotheses labelled.
- Prefer read-only observations and sanitized functional fixtures. Persist no tokens, cookies, authorization headers or private chat contents in research artifacts or repository fixtures.
- Manual assistance may be needed for Microsoft sign-in, opening a representative chat, phone file handoff, expected-result validation, or a deliberately authorized upload/send.
- Permission to investigate is not blanket permission to send messages, share files, alter chat state or create public links. Prepare exact destination/content before requesting any missing write authorization; the current consolidation does not perform those actions.
- Keep shared behavior in the connector/service/formatter/archive layers so the MCP and local page remain reusable. Investigate Streamable HTTP only if an actual remote-client deployment requires it; stdio already meets the current server requirement.
- No further work is needed merely to recreate already delivered UI, MCP registration or backup features. A separate Outlook client remains outside this application's completion scope; see [Outlook feasibility plan](outlook-client-feasibility-plan.md).
