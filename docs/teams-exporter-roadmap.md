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
- Support explicitly authorized text and attachment sending through MCP using files already on the host computer, including requests made remotely from a phone.
- Retain fetched messages indefinitely in account-bound SQLite storage, lazily as retrieval, export or search requests require them. Merge fresh Teams records with previously retained history; no scheduled collection or expiry is required.
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

This order puts shared retrieval/storage foundations before optional attachment sending. Work IDs preserve earlier references; P1.4 was removed because phone-to-host file transfer is outside the requested workflow. Independent investigations can proceed without waiting for unrelated work.

| Order | Work | What it unlocks |
|---|---|---|
| 1 | P1.3 Self-chat discovery and sender identity | Reliable retrieval and targeting of the user's own chat; useful sending test destination. |
| 2 | P1.1 Catalog completeness and ordering | A trustworthy scope for listing, retrieval and search. |
| 3 | P1.2 Long-history filtering and export reliability | Correct, recoverable retrieval of requested intervals. |
| 4 | P2.1 Lazy indefinite message storage | Shared fetch, upsert, query and return flow for UI and MCP. |
| 5 | P2.3 Complete MCP content and continuation | Entire-message/context access through bounded repeatable requests. |
| 6 | P2.2 Search completeness and Teams search research | Fetch a requested interval, then search merged SQLite records when native search is unavailable. |
| 7 | P2.4 Scheduled AI workflows | Optional client-driven summaries using on-demand retrieval. |
| Optional | P1.5 Attachment upload, access and delivery | Send a host-computer file with a message; a nice-to-have alongside existing text sending. |
| Later | Nice-to-haves | Attachments in exports, indicators, message pictures, viewing and Favorites. |

## P1.3 Self-chat discovery and sender identity

**Status: Implemented for the self-chat observed in the signed-in Teams account.**

- Evidence: Teams Web showed the current user's self-chat as a one-member conversation with ID 48:notes. The complete CSA startup catalog omitted it, while the paginated Chat Service catalog included it. Direct read-only message history retrieval by 48:notes succeeded.
- Implementation: the connector includes the special self-chat in the normalized catalog, identifies it with is_self_chat, and places it first so it appears in the initial MCP/UI page. Existing send authorization and destination checks can now resolve it by its listed ID and display name. No message was sent as part of implementation.
- Sender identity: live Chat Service messages expose from as a composite string that contains the sender's 8:orgid object ID. The connector extracts that stable identity; MCP get_messages and local indexed search return author_id and is_self when known. Message sync now stores sender IDs. Existing archive rows have no sender ID until that message is fetched again; their is_self value remains unknown.
- Identity handling compares stable identifiers with the authenticated account's object ID. Display-name matching is never used to mark a message as the user's own. A direct conversation with another person is not classified as the self-chat.
- Limitation: 48:notes is a private Teams convention, not a documented Microsoft API contract, and the live evidence covers one tenant/build. Keep the special ID isolated so it can be replaced. The standard thread-members route returned an error for this ID in the observed account; self-chat listing and history do not depend on it.
- Remaining validation: a live send to self still requires a separate explicit user authorization. No additional user action is needed for read/list behavior.

## P1.5 Send attachments

**Status: Missing. Research contains candidate upload shapes, not a verified end-to-end implementation.**

- Requested flow: while remotely controlling the agent from a phone, ask it to send a message to oneself or another specified Teams recipient and attach an existing file on the host computer. The server reads that local file and uploads it to Teams. Phone file upload/download and a separate file-transfer interface are not requirements. This is a nice-to-have extension to text sending.
- Known: current sending is plain text. Existing research distinguishes ordinary SharePoint/OneDrive files from AMS inline images. It records candidate upload/link routes, file metadata fields and AMS references.
- Unknown: whether app-owned authentication can obtain the required SharePoint/AMS audience tokens without additional consent; tenant storage discovery; the exact attachment message payload; recipient access for self, direct, group and meeting chats; upload limits and completion behavior.
- Proposed path: inspect already collected Web code and Maxim-Mazurok/teams-api for the upload-to-message sequence. Observe a deliberately authorized sample attachment sent through Teams Web, if required, including storage upload, permissions, metadata and final message creation. Verify resource audiences and replay each stage with the application's authentication. A route string or successful upload alone is insufficient.
- Scope: begin with ordinary host files and an optional text message, sharing the existing send destination/authorization behavior. Decide whether to extend send_message or expose a companion tool during implementation. Treat inline images as a second flow. UI sending remains optional. Bind authorization to the actual local file(s), destination and text; report inaccessible paths clearly.
- Reliability questions: large/chunked uploads, name conflicts, interrupted transfers, changed source files, expired upload sessions, uploaded-but-unsent files, and ambiguous final-send responses. Any future cleanup of remote files is a separate mutation and must not silently delete pre-existing content. Keep verified file/message IDs so retries can avoid duplicate sends.
- Completion evidence: an authorized sample is delivered as a real Teams attachment, can be opened by the intended recipient, has verified access scope, and returns file/message identifiers. Self-chat success alone does not prove access for other recipients. Validate failure handling without broadly sharing the file or silently overwriting an existing one.
- Possible user help: a new sign-in if the cached account cannot obtain the upload resource token; a harmless sample; consent for the exact live upload/send test. If tenant policy blocks the needed token or permissions, record that blocker rather than assuming another token will solve it.

## P1.1 Catalog completeness, names and ordering

**Status: Implemented for the observed chat/meeting scope; tenant-wide visibility remains unverified.**

- Catalog rules: preserve CSA rows and their richer UI fields; use the fully paginated Chat Service feed when CSA reports partial data or omits either completeness flag. Merge by conversation ID and admit only recognized chat, one-to-one, meeting, and the known notes-to-self row. Exclude known team/channel entities and service streams. Unknown raw-only types are excluded and make coverage incomplete rather than silently disappearing.
- Names and ordering: use names/topics exposed by the source rows. For fallback-only untitled one-to-one chats, attempt a bounded roster lookup and report how many remain unnamed. Put the known self-chat first, then sort by descending last activity; preserve source order for timestamp ties and put missing timestamps last.
- Coverage: `list_chats` reports the source, defined scope, CSA partial flag, raw unique row count, fallback rows added, known non-chat and unclassified rows excluded, and unresolved fallback names. `catalog_complete` means CSA explicitly reported complete, or fallback pagination finished without an unclassified raw-only row; it is limited to `catalog_scope`, not a claim about every Teams-visible or tenant-retained object. Malformed catalog rows now fail explicitly instead of being silently skipped.
- Live evidence (2026-09-30): CSA returned 943 rows with both completeness flags false; the paginated Chat Service feed returned 960 unique rows and contained every CSA ID. Its 17 raw-only rows were the known self-chat plus 16 team/channel and service-stream rows; no additional ordinary chat or meeting type appeared in this account snapshot. This supports the scoped type filter for that session, not a universal completeness guarantee.
- Remaining uncertainty: other tenants/builds, hidden or archived items, additional catalog views, and whether private service flags consistently mean complete. CSA delta/sync-token behavior is unverified; it is not used for listing, which performs a full CSA read and only pages the fallback when needed.
- Further evidence would come from comparing a small, user-identified set of visible/hidden/direct/group/meeting chats and checking naturally occurring metadata changes. No deliberate hide, pin, membership or message-state mutation is required. The user can identify any expected chat missing from the result.

## P1.2 Long-history filtering and export reliability

**Status: Partial.**

- Implemented: read-only GET/HEAD requests retry transient HTTP 408/429/5xx and transport failures up to four total attempts per request, with bounded exponential delay and bounded Retry-After handling. Writes are never retried. A retry budget resets for each page request, so one failing page does not restart successful earlier page reads. MCP message-page and index-sync results report retry counts.
- Implemented: full exports consume paged history into a disk-backed spool and then write chronologically to a staging file; the final destination is replaced only after successful completion. This bounds message-body memory during export and avoids publishing a half-written destination.
- Implemented: the local page starts an export job, polls progress (chat, phase, pages, messages and retries), and can request cancellation. Cancellation is checked between pages, while waiting between retries, during formatting, and while building combined/ZIP output. A completed page is handed to the disk spool before cancellation is observed at the next page boundary.
- Known: opaque continuation and ID deduplication exist; sampled startTime behavior is inclusive; endTime was inconsistent at ordinary-message boundaries and remains a local filter. Requested page size has only limited live verification. Index sync persists page progress in SQLite; an ordinary text/ZIP export does not persist a resumable checkpoint across process restart or permanent failure after its retry budget is exhausted.
- Cancellation limit: the current `urlopen` request cannot be interrupted mid-read. Cancellation is observed after that request returns/fails or its 30-second timeout expires; retry backoff and work between requests stop promptly.
- Live sample (2026-09-30): the user-selected Fiona direct conversation matched one catalog entry by exact display name. Two ten-message pages had no overlap; the continuation page was older and retained `pageSize=10` from the opaque link even when the local continuation call supplied 3. An exact `startTime` message was included; `until` was inclusive in the client filter and was not sent to Teams. The two-member roster did not expose an email address, so email identity was not independently verified. This is one conversation/build sample, not a universal ordering or page-size guarantee.
- Unknown: page-size behavior on subsequent pages, overlap/order across chat and event types, segment-boundary semantics, and whether a reliable server-side upper bound or early-stop rule exists.
- Investigation: compare first and continuation pages with different requested page sizes, overlaps, and message IDs; test inclusive start/end boundaries around known ordinary and system messages; and determine whether continuation ordering supports any safe early-stop optimization. Inspect Web pagination call sites and existing reverse-engineering sources. Only enable server-side filtering or early-stop after matching the reference messages and bounds.
- Remaining application work: decide whether normal export needs durable resumable checkpoints or whether the current bounded-retry/error/progress behavior is sufficient. Measure disk use and responsiveness with a representative very long history before claiming unlimited extraction is operationally reliable.
- Completion evidence: matching IDs/counts under supported filters; explicit errors and coverage on incomplete reads; retries do not repeat completed pages; cancel stops between requests and reports the completed-page count; and large-history tests confirm chronological output without retaining all message bodies in memory. Never infer complete lifetime history merely from an absent older-page link.
- Selected test conversation: Fiona Grant (display name Grant, Fiona), identified by the user as fiona.grant@example.com. A unique exact-name catalog match and two-page read have now been observed, but the member response did not expose the email, so matching the account by email remains unverified. Existing pages provide a reference for later comparisons; no boundary example from the user was needed for this sample.

## P2.1 Lazy indefinite storage: fetch, upsert, query, return

**Status: Persistent SQLite exists; the unified retrieval/merge contract below is the target, not yet verified as implemented.**

- Retention decision: keep messages indefinitely, with no expiry, automatic purge, scheduled collection or mandatory full-history backfill. Store messages when a requested retrieval, export or search fetches them. Storage deletion/expiry controls can be added later if needed; the user can deliberately remove the database to reset the archive.
- Shared engine flow, used by both UI and MCP:
  1. Fetch Teams messages for the requested conversation(s) and time interval, following remote pagination. Persist fetched records as pages arrive, including useful records encountered while traversing history.
  2. Upsert into account-bound SQLite using stable conversation/message IDs. Fresh remote records are authoritative for fields they actually supply; apply verified edit/version and explicit deletion semantics. Do not let an older page overwrite a newer known revision or let an incomplete deletion payload erase identity/time metadata.
  3. Query SQLite for the requested interval. Its unique keys provide the deduplicated union of fresh remote records and retained local-only records, without requiring two full in-memory histories.
  4. Return that merged set in stable chronological order with IDs and UTC timestamps. Apply requested output limits and continuation to the merged set, so retained local records participate correctly in ordering and counts.
- Edits and deletions:
  - Investigate and document Teams edit timestamps/version fields, deletion flags/tombstones, and whether deleted records retain their original IDs and timestamps.
  - Update stored content when an incoming record represents an edit. Record an explicit remote soft-deletion flag locally and expose that state to consumers; do not render a deleted record as ordinary current text. Decide separately how retained pre-deletion text is represented.
  - Preserve local records absent from remote responses, including records removed by remote retention. Absence alone cannot distinguish hard deletion, retention expiry, incomplete paging or lack of access, so it must never trigger local deletion or an invented deletion flag.
  - Keep remote refresh coverage separate from retained-history coverage. Local-only history is usable, but its latest remote state is unknown if it can no longer be refreshed.
- Investigation path: examine existing research and response fields first, then controlled read-only comparisons of known edited/deleted messages and sanitized fixtures. Any deliberate edit/delete test requires separate authorization. Establish revision ordering and idempotent upsert behavior before implementing the shared flow.
- Functional verification: overlapping date exports, repeated fetches, edited messages, soft-delete records, local-only records, interrupted pages, concurrent refreshes and account isolation. A repeated request must not duplicate rows; failures must preserve successfully stored pages and report incomplete refresh rather than claim full coverage.
- Simplification: replace separate export/index population paths with this shared pipeline where possible. Reassess existing sync tools as optional manual prefetch/status operations; remove redundant machinery instead of maintaining parallel retrieval implementations. Continuous catch-up, sync schedules, automatic deletion and full-lifetime completeness are not completion requirements.
- Existing persistence, account separation and backup remain useful. No schema-version or legacy-format compatibility work is required. Only history actually fetched while available can be retained; never promise recovery of messages already unavailable before the first request.
- Possible user help: identify an existing edited/deleted example if needed or complete a required sign-in. Retention duration and collection cadence no longer need a decision.

## P2.3 Complete content through bounded MCP responses

**Status: Bounded tools implemented; separate inline and resource-export interfaces agreed, implementation pending.**

- Known: get_messages documents a 1,200-character body cap with explicit truncation metadata. Older-message cursors do not by themselves provide the missing remainder of a long individual message. No full-body continuation is documented.
- Agreed interfaces (MCP tools/resource operations over stdio; no HTTP server required):
  - get_messages: bounded inline message pages for conversational use, with explicit continuation and a way to retrieve the remainder of oversized individual messages.
  - export_chat: a separate tool that prepares a complete export snapshot for the requested conversation and filters, then returns an MCP resource_link plus export ID, MIME type, byte size, message count, UTC bounds and coverage. Use an account-bound URI such as teams-export://exports/<id>; do not embed the complete export in the tool response.
  - resources/read: standard MCP access to the completed export resource. A resource link is not a guarantee that every client can download a file or keep its contents outside model context.
  - read_export: bounded sections of the same immutable snapshot, with continuation and exact next-call arguments, for large exports and clients needing tool-based access. Sections must allow reconstruction without missing text or splitting Unicode incorrectly.
- Shared behavior: both retrieval paths use P2.1's fetch/upsert/query engine and shared formatting. The UI can download the same generated export artifact. Resource delivery does not eliminate Teams pagination, resumable preparation, client context limits or the need for bounded reading.
- Remaining decisions: snapshot storage/lifetime, restart-safe export IDs/cursors, long-message section boundaries and supported client resource behavior. Distinguish disposable generated exports from indefinitely retained SQLite messages; artifact cleanup must not delete archived history.
- Target continuation contract: every bounded result states whether more work/results remain and supplies an opaque continuation plus the exact next tool name and arguments. Tool descriptions instruct the agent to repeat those calls until complete, or until the user's requested limit is met. Bind continuation to the original account, filters and ordering; distinguish retrieval-in-progress from a complete search with zero matches. Avoid returning the same page or repeating the entire remote fetch on every continuation.
- Long-running work must advance through bounded resumable calls rather than depend on one indefinitely open MCP exchange. The server provides continuation; the client/agent drives additional calls. It cannot force a client to continue, so unfinished coverage must remain explicit. Automated pagination does not remove the separate need to retrieve the remainder of an oversized individual message.
- Proposed path: check both interfaces against a long sanitized message and a conversation spanning many pages. Verify resource discovery/read and bounded snapshot reconstruction with a generic MCP client. Distinguish interactive result cursors, snapshot section cursors and persisted remote-fetch progress. Standard resource-list pagination must not be mistaken for pagination within a resource's content.
- Completion evidence: a client can reconstruct a full message and conversation without silent clipping or flooding context; expired/mismatched cursors fail clearly; IDs, author identity and UTC timestamps preserve source traceability.
- Possible user help: only if a particular MCP client cannot consume the chosen resource/file mechanism.

## P2.2 Search coverage and direct Teams search

**Status: Local search implemented; native search contract unverified.**

- Known: FTS5 supports phrase/all-term message search and indexed-roster participant lookup. Coverage is exposed. Teams Web search returned results in an earlier visual check; route strings alone did not establish request body, audience, paging or completeness.
- Required fallback when native search cannot be verified:
  - Accept a required from/to date range and explicit chat scope (selected chats or the resolved catalog). Fetch all available remote messages in that interval through P2.1, upsert them, then search the merged SQLite records, including retained local-only history.
  - Large scopes use the P2.3 continuation contract for preparation and result pages. Report which chats/intervals were refreshed and which failed or remain pending; do not call a search complete until the requested fetch scope has been exhausted successfully. This is on-demand population, not a prerequisite background index job.
  - Start with simple SQL matching: exact normalized text equality and literal substring matching using parameterized LIKE. Escape user-supplied percent/underscore and the escape character so they are literal text, not accidental SQL wildcards.
  - Define normalization, case and accent behavior explicitly and test non-ASCII names/content; SQLite's default LIKE behavior must not be assumed to provide full Unicode case folding. No fuzzy, semantic or similarity search is required.
  - Apply the same bounds and deduplicated stored records used for retrieval. Define how explicit deletion flags affect searchable content and return deletion state clearly. Participant lookup remains a roster/name operation, with missing roster coverage reported separately.
- Search-result safety cap:
  - Add a positive integer max_matches input controlled by the client, with a default of 1,000 when omitted and no application-imposed upper bound. Clients may request arbitrarily large totals. This is a total search cap across all selected chats, separate from the smaller per-response page/byte limits; continuation must preserve the chosen cap without resetting or bypassing it. Large totals must remain paginated rather than requiring allocation of the entire result set in memory.
  - Apply the cap after deduplication, with deterministic ordering and stable ID tie-breakers. For local SQL search, inspect up to max_matches + 1 matching rows to detect overflow without counting or returning every match.
  - MCP responses include max_matches, returned_count (for the current page), returned_total (across this search's pages), results_truncated and truncation_reason. When overflow is confirmed, set results_truncated=true and truncation_reason=max_matches; explain that the caller should narrow the query, date range or chat scope. Do not claim an exact total match count unless measured.
  - Ordinary result pagination is not truncation. Keep has_more/next-call arguments for remaining permitted pages, but stop continuation at the total cap. If preparation is incomplete, report that coverage separately and leave truncation unknown until it can be determined; do not imply the full scope was searched.
  - This limits returned matches, not the date-bounded fetch/upsert needed to prepare the local search. Apply equivalent output limits to verified native search if it is introduced.
  - Functional checks: fewer than the cap, exactly the cap, one over the cap, overlapping pages/deduplication, lower and very large caller limits, and continuation across multiple chats without increasing individual response sizes.
- Proposed path: inspect the previously downloaded search call sites and referenced reverse-engineered client. Capture a controlled existing keyword/phrase and participant search in Teams Web, including relevant HTTP or WebSocket traffic if used. Replay read-only requests using app-owned credentials. Compare IDs, scope, dates, pagination and unavailable/hidden chat cases against known messages and local results.
- Local-search questions: missing/stale rosters, unresolved names, stale catalog scope, edits and recently arrived messages must affect coverage correctly. Evaluate whether the existing FTS layer still serves a necessary purpose after simple SQL matching is implemented; remove redundant code rather than require two search engines.
- Completion evidence: the documented search scope matches what was actually searched, including when there are no hits. Native search is used only for verified scopes; it need not be complete lifetime search to be useful if its limits are explicit. Local indexing remains a valid implementation when native search cannot be verified.
- Possible user help: supply known phrases, participant names and expected conversations for comparison. New sign-in or consent may be needed for an additional resource audience; neither is assumed to be available.

## P2.4 Scheduled attention summaries and topic research

**Status: Retrieval primitives exist; optional client-driven recurring workflow is not configured.**

- Intended outcomes: daily pending tasks/requests requiring direct or indirect attention, recent topic updates, and ad-hoc research across chats with optional priority conversations.
- Proposed path: define a small evaluation set of existing requests, replies and resolutions. Exercise the client workflow: request a time range, fetch/upsert/search on demand, fetch surrounding context, and cite chat/message IDs, authors and times. Test timezone cutoffs, unfinished coverage and contradictory follow-up messages. Each scheduled client request uses the same lazy storage flow; a separate archive scheduler is unnecessary.
- Open decisions: which MCP client schedules the run, when its host is available, how reauthentication is surfaced, delivery location, and how to distinguish pending work from completed or merely inferred tasks.
- Completion evidence: a scheduled run reports its actual time/chat coverage, produces traceable findings, distinguishes explicit requests from inferences, and exposes failures or missing scope. Scheduling alone does not prove useful task extraction.
- Possible user help: set the schedule and notification destination, clarify examples of pending/completed work, and authorize any external delivery separately. Summarization never implies permission to send Teams messages.

## Nice-to-haves and their investigation paths

These and P1.5 attachment sending remain optional, below core retrieval, MCP and lazy storage. Attachment downloading remains optional; Favorites remains last.

| Item | Current gap | Proposed path and completion evidence |
|---|---|---|
| N1. Download attachments/images in exports | Markers/links exist; binary downloads do not. | Verify file/AMS download authentication from existing attachment metadata, redirects, expired links and names; package safe relative paths and report individual failures without losing text. A restored export must open its included files. |
| N2. Remote read/unread indicators | CSA has an unread value; UI does not display it. | Compare existing remote values to the Teams UI, including missing/stale values. Show only verified states. Do not invoke mark-read endpoints to test this feature. |
| N3. Pictures on messages | Account/chat picture URI fallback exists; message sender pictures do not. | Build on verified sender identity, compare available URI behavior and authentication, and preserve generic fallbacks. Direct URIs are preferred over local image downloads. |
| N4. Conversation viewer and rendering fidelity | No viewer; export/formatting exist. | Reuse retrieval; use sanitized examples for mentions, replies, multiline text, rich/system messages and missing metadata. Preserve raw/unknown event meaning rather than inventing an actor/action. Verify functionality and shared formatting; appearance tests are not required. |
| N5. Optional presence indicator | No status displayed; a usable presence source is unverified. | Inspect existing account data and Web presence usage only if inexpensive and accessible with available authentication. Otherwise defer, as permitted. Profile failure must not block the UI. |
| N7. Optional Markdown input parsing for outgoing messages | The existing output formatter converts Teams messages to readable text, but send_message treats input as plain text, so Markdown fences are sent literally. | Add an input-parser flag controlled by the requester; it defaults to enabled and can be disabled per request. Version 1 recognizes only code fences delimited by three backticks, with an optional language tag after the opening fence. Version 2 adds ordered and unordered lists. Leave other Markdown untouched. Convert supported syntax into Teams-supported rich text safely, preserve code/list content and surrounding text, and verify how the generated payload renders in Teams. |
| N6. Favorites/pinned section | isSticky is not a verified Favorites marker. | Compare already configured Favorites with Web responses/call sites. Once mapped, display the section first without changing Teams state. This remains the lowest priority. |

## How future investigations should be conducted

- Start from existing API research, downloaded Teams HTML/JS and [Maxim-Mazurok/teams-api](https://github.com/Maxim-Mazurok/teams-api). Check source age and assumptions before reusing behavior.
- Use browser navigation and traffic observation to identify the requests behind a specific action. Inspect WebSocket traffic only if the observed behavior uses it. Then vary one parameter at a time in bounded direct trials.
- Keep browser-session success distinct from success with the application's app-owned authentication. The latter is required for an unattended connector capability.
- Record the hypothesis, source, observed result, scope, failure mode and completion evidence. Add confirmed route facts to API research and update status here; keep unsuccessful hypotheses labelled.
- Prefer read-only observations and sanitized functional fixtures. Persist no tokens, cookies, authorization headers or private chat contents in research artifacts or repository fixtures.
- Manual assistance may be needed for Microsoft sign-in, opening a representative chat, identifying a host file, expected-result validation, or a deliberately authorized upload/send.
- Permission to investigate is not blanket permission to send messages, share files, alter chat state or create public links. Prepare exact destination/content before requesting any missing write authorization; the current consolidation does not perform those actions.
- Keep shared behavior in the connector/service/formatter/archive layers so the MCP and local page remain reusable. Investigate Streamable HTTP only if an actual remote-client deployment requires it; stdio already meets the current server requirement.
- No further work is needed merely to recreate already delivered UI, MCP registration or backup features. A separate Outlook client remains outside this application's completion scope; see [Outlook feasibility plan](outlook-client-feasibility-plan.md).
