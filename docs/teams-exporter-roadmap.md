# Teams Exporter Roadmap

## Scope and evidence

This is the consolidated register of gaps between the current application and the intended product. It replaces the separate lists of open work previously scattered through this roadmap. Endpoint facts and captured payloads remain in [Teams API research](teams-api-research.md); module responsibilities are in [Project architecture](project-architecture.md); usage is in [MCP server guide](teams-mcp-server.md).

This status snapshot was reviewed against the current implementation and sanitized functional tests on 2026-09-30. The review did not make a live Teams request, capture browser traffic or search online, and it sent no message or changed Teams state. Live observations below are dated and scoped; remaining investigation paths are proposals unless identified as observed evidence.

Status terms:

- **Implemented:** documented application behavior exists; this does not establish universal behavior across Microsoft tenants or all chat types.
- **Partial:** a useful part exists, with a specific remaining gap.
- **Pending:** the requested capability is not implemented yet.
- **Unverified:** available evidence is insufficient. Suspected failure cases listed below require validation and are not newly confirmed bugs.

## Intended product

- Retrieve relevant one-to-one, group, meeting, and self conversations with stable identities and honest catalog coverage. Retrieve message history with reliable pagination and inclusive date/count filters.
- Offer a simple local page for manual text/ZIP exports without an AI client. Retrieving new Teams data still requires connectivity and authentication.
- Expose the same engine through client-independent MCP for listing, participant/content search, and complete conversation or surrounding-context retrieval.
- Support explicitly authorized text and attachment sending through MCP using files already on the host computer, including requests made remotely from a phone.
- Retain fetched messages indefinitely in account-bound SQLite storage, lazily as retrieval, export or search requests require them. Merge fresh Teams records with previously retained history; no scheduled collection or expiry is required.
- Enable an external AI client to research topics and produce scheduled attention summaries grounded in identifiable source messages.
- Add optional viewing and presentation features after the core capabilities. Favorites/pinned presentation remains last.

## What is already delivered

| Component | Documented capability and limit |
|---|---|
| Authentication and connector | App-owned MSAL cache in the absolute per-user application-data folder, silent access-token renewal, cross-process cache locking, service-specific credentials, chat catalog, message pages, member/profile lookup, and plain-text sending. The application does not receive the password. Additional upload audiences are not established by text sending. |
| Retrieval and formatting | Opaque continuation links, message-ID deduplication, inclusive date/count checks, chronological results, HTML/mention formatting, and system/attachment markers. Remote bounds and coverage are reported separately from retained local history. |
| Local page | Loopback server, chat selection/name filtering, inclusive date range with clear-to-unlimited, positive optional message count, text/ZIP/combined export, system theme at load, account/chat picture URI fallbacks, local display times and UTC exports. Exports fetch, archive and query through the shared service. |
| MCP | Ten typed tools: list_chats, search_chats, search_messages, get_messages, get_message_content, export_chat, read_export, sync_search_index, get_search_index_status, and send_message. Bounded responses, explicit coverage, continuations and completed exports as MCP Resources; stdio launches on demand. |
| Archive | Persistent account-bound SQLite storage, on-demand fetch/upsert/query, no expiry, bounded resumable fetch/search, ownership isolation and online backup. Message search uses normalized SQL matching; participant search uses indexed rosters. Native Teams search is not used. |
| Sending | Plain text through MCP with destination verification and a required record of explicit user authorization. That record is not proof of permission; the caller must obtain it. Attachment sending is absent. |
| Read behavior | Listing, retrieving, searching, syncing and exporting do not intentionally change Teams read state. |

## Proposed execution order and dependencies

This order puts shared retrieval/storage foundations before optional attachment sending. Work IDs preserve earlier references; P1.4 was removed because phone-to-host file transfer is outside the requested workflow. Independent investigations can proceed without waiting for unrelated work.

| Order | Work | What it unlocks | Status |
|---|---|---|---|
| 1 | P1.3 Self-chat discovery and sender identity | Reliable retrieval and targeting of the user's own chat; useful sending test destination. | **Implemented** for the observed signed-in account; broader tenant behavior remains unverified. |
| 2 | P1.1 Catalog completeness and ordering | A trustworthy scope for listing, retrieval and search. | **Partial** — catalog normalization and coverage are implemented; self-chat activity ordering and cache-first UI refresh remain. |
| 3 | P1.2 Long-history filtering and export reliability | Correct, recoverable retrieval of requested intervals. | **Partial** — bounded retries, filtering, spooling and cancellation exist; exports do not resume after restart, and very long live histories need more validation. |
| 4 | P2.1 Lazy indefinite message storage | Retain fetched messages and reconcile fresh Teams records with the local archive. | **Partial** — shared fetch/upsert/query is implemented; live edit/deletion semantics remain unverified. |
| 5 | P2.3 Complete MCP content and continuation | Retrieve complete content through bounded calls or MCP Resources. | **Implemented** for the current generic MCP client contract; client support for Resources can vary. |
| 6 | P2.2 Search coverage and Teams search research | Search messages and participants with honest, explicit coverage. | **Partial** — scoped local SQL and participant search are implemented; the native Teams search contract remains unverified and unused. |
| 7 | P2.4 Scheduled AI workflows | Find pending tasks and topic updates on a recurring or ad-hoc basis. | **Partial** — retrieval primitives exist; this application does not configure a summarizer, schedule or delivery destination. |
| Optional | P1.5 Attachment upload, access and delivery | Send a host-computer file with a message; a nice-to-have alongside existing text sending. | **Pending** — candidate routes are documented, but end-to-end upload and delivery are not implemented or verified. |
| Later | Nice-to-haves | Attachments in exports, indicators, message pictures, viewing and Favorites. | **Partial / pending** — see the item-level status table below. |

## P1.3 Self-chat discovery and sender identity

**Status: Implemented for the self-chat observed in the signed-in Teams account.**

- Evidence: Teams Web showed the current user's self-chat as a one-member conversation with ID 48:notes. The complete CSA startup catalog omitted it, while the paginated Chat Service catalog included it. Direct read-only message history retrieval by 48:notes succeeded.
- Implementation: the connector includes the special self-chat in the normalized catalog, identifies it with is_self_chat, and places it first so it appears in the initial MCP/UI page. Existing send authorization and destination checks can now resolve it by its listed ID and display name. No message was sent as part of implementation.
- Sender identity: live Chat Service messages expose from as a composite string that contains the sender's 8:orgid object ID. The connector extracts that stable identity; MCP get_messages and local indexed search return author_id and is_self when known. Message sync now stores sender IDs. Existing archive rows have no sender ID until that message is fetched again; their is_self value remains unknown.
- Identity handling compares stable identifiers with the authenticated account's object ID. Display-name matching is never used to mark a message as the user's own. A direct conversation with another person is not classified as the self-chat.
- Limitation: 48:notes is a private Teams convention, not a documented Microsoft API contract, and the live evidence covers one tenant/build. Keep the special ID isolated so it can be replaced. The standard thread-members route returned an error for this ID in the observed account; self-chat listing and history do not depend on it.
- Remaining validation: a live send to self still requires a separate explicit user authorization. No additional user action is needed for read/list behavior.

## P1.5 Send attachments

**Status: Pending. Research contains candidate upload shapes, not a verified end-to-end implementation.**

- Requested flow: while remotely controlling the agent from a phone, ask it to send a message to oneself or another specified Teams recipient and attach an existing file on the host computer. The server reads that local file and uploads it to Teams. Phone file upload/download and a separate file-transfer interface are not requirements. This is a nice-to-have extension to text sending.
- Known: current sending is plain text. Existing research distinguishes ordinary SharePoint/OneDrive files from AMS inline images. It records candidate upload/link routes, file metadata fields and AMS references.
- Unknown: whether app-owned authentication can obtain the required SharePoint/AMS audience tokens without additional consent; tenant storage discovery; the exact attachment message payload; recipient access for self, direct, group and meeting chats; upload limits and completion behavior.
- Proposed path: inspect already collected Web code and Maxim-Mazurok/teams-api for the upload-to-message sequence. Observe a deliberately authorized sample attachment sent through Teams Web, if required, including storage upload, permissions, metadata and final message creation. Verify resource audiences and replay each stage with the application's authentication. A route string or successful upload alone is insufficient.
- Scope: begin with ordinary host files and an optional text message, sharing the existing send destination/authorization behavior. Decide whether to extend send_message or expose a companion tool during implementation. Treat inline images as a second flow. UI sending remains optional. Bind authorization to the actual local file(s), destination and text; report inaccessible paths clearly.
- Reliability questions: large/chunked uploads, name conflicts, interrupted transfers, changed source files, expired upload sessions, uploaded-but-unsent files, and ambiguous final-send responses. Any future cleanup of remote files is a separate mutation and must not silently delete pre-existing content. Keep verified file/message IDs so retries can avoid duplicate sends.
- Completion evidence: an authorized sample is delivered as a real Teams attachment, can be opened by the intended recipient, has verified access scope, and returns file/message identifiers. Self-chat success alone does not prove access for other recipients. Validate failure handling without broadly sharing the file or silently overwriting an existing one.
- Possible user help: a new sign-in if the cached account cannot obtain the upload resource token; a harmless sample; consent for the exact live upload/send test. If tenant policy blocks the needed token or permissions, record that blocker rather than assuming another token will solve it.

## P1.1 Catalog completeness, names and ordering

**Status: Partial — catalog scope is implemented for observed data; ordering and cache-first refresh fixes remain.**

- Catalog rules: preserve CSA rows and their richer UI fields; use the fully paginated Chat Service feed when CSA reports partial data or omits either completeness flag. Merge by conversation ID and admit only recognized chat, one-to-one, meeting, and the known notes-to-self row. Exclude known team/channel entities and service streams. Unknown raw-only types are excluded and make coverage incomplete rather than silently disappearing.
- Names and ordering: use names/topics exposed by the source rows. For fallback-only untitled one-to-one chats, attempt a bounded roster lookup and report how many remain unnamed. **Ordering fix:** the current connector prioritizes the known self-chat; remove that special priority and sort every chat, including self-chat, by descending last activity. Preserve source order for timestamp ties and put missing timestamps last. Add a regression case where a less-recent self-chat appears below chats with newer activity.
- Coverage: `list_chats` reports the source, defined scope, CSA partial flag, raw unique row count, fallback rows added, known non-chat and unclassified rows excluded, and unresolved fallback names. `catalog_complete` means CSA explicitly reported complete, or fallback pagination finished without an unclassified raw-only row; it is limited to `catalog_scope`, not a claim about every Teams-visible or tenant-retained object. Malformed catalog rows now fail explicitly instead of being silently skipped.
- Live evidence (2026-09-30): CSA returned 943 rows with both completeness flags false; the paginated Chat Service feed returned 960 unique rows and contained every CSA ID. Its 17 raw-only rows were the known self-chat plus 16 team/channel and service-stream rows; no additional ordinary chat or meeting type appeared in this account snapshot. This supports the scoped type filter for that session, not a universal completeness guarantee.
- Incremental-discovery evidence (2026-09-30): a Chat Service request with `pageSize=25` returned 24 rows and an opaque `backwardLink`; following that exact link returned 24 more with no overlap. A later CSA startup request returned 944 chats with `isPartialData=false` and `hasMoreChats=false` in about 4.7 seconds. The CSA updates route accepted its `metadata.syncToken` and returned a complete empty delta with the same token. No nonempty delta or token rotation was observed.
- Full-traversal timing (2026-09-30): the raw catalog returned 961 unique rows in three `pageSize=500` requests, taking 6.67 seconds for the pages and 8.36 seconds including 1.69 seconds for authentication. At `pageSize=25`, it took 42 pages and 55.44 seconds for the pages, or 57.73 seconds including 2.28 seconds for authentication; no retries occurred. In this sample, small pages improve availability of an early page but make a full traversal much slower. The raw catalog includes non-chat rows, so these timings compare request strategies rather than normalized UI catalog construction.
- Startup/cache behavior (chosen approach; no page-by-page UI loading): persist an account-bound snapshot of the full conversation list. On page load, React should immediately request the fresh full list and render the cached list first when one exists, with a visible refresh indicator. If there is no cached list, keep the existing loading state until the response arrives. Replace/update the list when the fresh full response arrives; preserve the selected conversation by stable chat ID if it remains in the new list. Use CSS transitions for added and reordered rows to reduce visual jumps, respecting reduced-motion preferences. Keep a cached list visible and indicate refresh failure if the background request fails. The observed raw-feed pagination is retained as API evidence, but is not the UI startup plan: full traversal took 6.67 seconds with `pageSize=500` and 55.44 seconds with `pageSize=25`, while a complete CSA startup request took about 4.7 seconds. These are account/session-specific measurements and the raw feed includes non-chat rows. The CSA delta route returned an empty result only; the UI refresh should request the current full list rather than depend on unverified delta semantics.
- Remaining uncertainty: other tenants/builds, hidden or archived items, additional catalog views, and whether private service flags consistently mean complete. The empty CSA delta route is verified but is not used for listing; nonempty delta semantics and token recovery remain unverified. Normal listing still performs a full CSA read and only pages the raw fallback when needed.
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

**Status: Partial — the shared lazy archive flow is implemented; live edit/deletion semantics remain unverified.**

- Retention decision: keep messages indefinitely, with no expiry, automatic purge, scheduled collection or mandatory full-history backfill. Store messages when a requested retrieval, export or search fetches them. Storage deletion/expiry controls can be added later if needed; the user can deliberately remove the database to reset the archive.
- Implemented shared engine flow, used by UI and MCP:
  1. Fetch Teams messages for the requested conversation(s) and time interval, following remote pagination. Persist fetched records as pages arrive, including useful records encountered while traversing history.
  2. Upsert into account-bound SQLite using stable conversation/message IDs. Fresh remote records are authoritative for fields they actually supply; use explicit edit/version and deletion metadata where present, with the unresolved semantics below. Do not let an older page overwrite a newer known revision or let an incomplete deletion payload erase identity/time metadata.
  3. Query SQLite for the requested interval. Its unique keys provide the deduplicated union of fresh remote records and retained local-only records, without requiring two full in-memory histories.
  4. Return that merged set in stable chronological order with IDs and UTC timestamps. Apply requested output limits and continuation to the merged set, so retained local records participate correctly in ordering and counts.
- Edit/delete handling: captured/researched message shapes contain `properties.edittime`, `properties.deletetime`, and `MessageDelete` event types. The archive uses those explicit fields to update edited content, retain tombstones and available prior text, mark deleted records in retrieval/export, and exclude tombstones from message search. Older or unversioned rows cannot overwrite a known edit; absence from a remote response never deletes a local row.
- Remaining verification: these fields and event shapes are reverse-engineered evidence, not a complete Microsoft contract. A controlled comparison against a known edited and soft-deleted message is still needed to establish how IDs, revision times and tombstones behave in the user's tenant. When no explicit update arrives, retained history's remote state remains unknown.
- Functional verification: sanitized tests cover overlapping/repeated fetches, edit and tombstone upserts, local-only messages, interrupted pages, stable ordering, account isolation and coverage. A live edit/delete comparison was not performed; it would require an existing suitable sample and remains read-only.
- Simplification: replace separate export/index population paths with this shared pipeline where possible. Reassess existing sync tools as optional manual prefetch/status operations; remove redundant machinery instead of maintaining parallel retrieval implementations. Continuous catch-up, sync schedules, automatic deletion and full-lifetime completeness are not completion requirements.
- Existing persistence, account separation and backup remain useful. No schema-version or legacy-format compatibility work is required. Only history actually fetched while available can be retained; never promise recovery of messages already unavailable before the first request.
- Possible user help: identify existing edited/deleted examples for a read-only comparison. Retention duration and collection cadence no longer need a decision.

## P2.3 Complete content through bounded MCP responses

**Status: Implemented for the current generic MCP client contract.**

- Implemented interfaces (MCP tools/resource operations over stdio; no HTTP server required):
  - get_messages: bounded inline message pages for conversational use, with explicit continuation and per-message content continuation through get_message_content.
  - export_chat: prepares a resumable, complete snapshot for the requested conversation and filters, then returns an MCP resource link plus export ID, MIME type, byte size, message count and coverage. The URI is `teams-export://exports/{export_id}/parts/{offset_bytes}`; the complete export is not embedded in the tool result.
  - resources/read: standard MCP access to the completed export resource. A resource link is not a guarantee that every client can download a file or keep its contents outside model context.
  - read_export: bounded sections of the same immutable snapshot, with continuation and exact next-call arguments, for large exports and clients needing tool-based access. Sections must allow reconstruction without missing text or splitting Unicode incorrectly.
- Shared behavior: both retrieval paths use P2.1's fetch/upsert/query engine and shared formatting. The UI writes its own downloadable response file; it does not reuse the MCP snapshot file. Resource delivery does not eliminate Teams pagination, resumable preparation, client context limits or the need for bounded reading.
- Implemented continuation: bounded results state whether more work remains and provide a cursor/offset. Tool descriptions identify the follow-up call. Search preparation, message pages and resource sections remain separate cursors; repeated search calls continue fetching only pending remote pages. Export snapshots are stored separately from the indefinite SQLite message archive.
- Remaining limitation: pagination is client-driven; MCP cannot force a client to make follow-up calls. Resource links and tool results are standard MCP, but clients may differ in how they present or retain resource contents. Export preparation and completed snapshots persist in the account archive; ordinary get-message and search cursors are in-memory and expire, so those queries must be restarted after a process restart. Archived messages remain independent of export snapshots.
- Long-running work must advance through bounded resumable calls rather than depend on one indefinitely open MCP exchange. The server provides continuation; the client/agent drives additional calls. It cannot force a client to continue, so unfinished coverage must remain explicit. Automated pagination does not remove the separate need to retrieve the remainder of an oversized individual message.
- Verification: sanitized tests and a generic in-process MCP client exercise tool discovery, bounded message retrieval, full-content continuation, export snapshot preparation, resource discovery and resource reads. The MCP output size is bounded; reconstructed export sections preserve UTF-8. More client-specific checks are needed only if a selected MCP client cannot consume the standard resource or continuation contract.
- Possible user help: identify a target MCP client only if its resource support needs separate validation.

## P2.2 Search coverage and direct Teams search

**Status: Scoped local search implemented; native Teams search contract remains unverified and is not used.**

- Current search behavior: message search uses parameterized SQLite matching (`exact`, `substring`, or `all_terms`) over normalized stored text. Participant-name search uses the local roster index and reports unresolved/incomplete roster coverage. The Web bundle contains search route constants, but the message-search request shape and coverage are unknown. A read-only request to the Substrate chat-suggestion flow returned HTTP 400 and did not establish complete conversation/message search. No native search request is used; see [Teams API research](teams-api-research.md#other-teams-search-routes).
- Search scope and coverage:
  - Accept a required from/to date range and explicit chat scope (selected chats or the resolved catalog). Fetch all available remote messages in that interval through P2.1, upsert them, then search the merged SQLite records, including retained local-only history.
  - Large scopes use the P2.3 continuation contract for preparation and result pages. Report which chats/intervals were refreshed and which failed or remain pending; do not call a search complete until the requested fetch scope has been exhausted successfully. This is on-demand population, not a prerequisite background index job.
- Matching normalizes text with Unicode NFKC and case-folding, escapes SQL LIKE wildcard characters, and supports literal exact, substring and all-term modes. Fuzzy/semantic similarity is not implemented. Soft-deleted messages are excluded from content search.
- Search-result safety cap (implemented):
  - `max_matches` is a positive integer controlled by the client, defaults to 1,000, and has no application-imposed upper bound. Clients may request arbitrarily large totals. This is a total search cap across all selected chats, separate from the smaller per-response page/byte limits; continuation preserves the chosen cap. Large totals remain paginated rather than requiring allocation of the entire result set in memory.
  - The cap applies after deduplication, with deterministic ordering and stable ID tie-breakers. Local SQL search inspects at most `max_matches + 1` matching rows to detect overflow without counting or returning every match.
  - MCP responses include max_matches, returned_count (for the current page), returned_total (across this search's pages), results_truncated and truncation_reason. When overflow is confirmed, set results_truncated=true and truncation_reason=max_matches; explain that the caller should narrow the query, date range or chat scope. Do not claim an exact total match count unless measured.
  - Ordinary result pagination is not truncation. Keep has_more/next-call arguments for remaining permitted pages, but stop continuation at the total cap. If preparation is incomplete, report that coverage separately and leave truncation unknown until it can be determined; do not imply the full scope was searched.
  - This limits returned matches, not the date-bounded fetch/upsert needed to prepare the local search. Apply equivalent output limits to verified native search if it is introduced.
- Functional tests cover fewer than the cap, exactly the cap, one over the cap, overlapping pages/deduplication, lower and very large caller limits, and continuation across multiple chats without increasing individual response sizes.
- Remaining limitations: search only covers the requested date/chat range fetched successfully for this request plus previously retained local rows in that range. An incomplete preparation returns coverage and a continuation instead of hits that could look complete. Participant search can be incomplete when names/rosters are unresolved or stale. Native search remains a separate optional investigation; no additional token or consent is assumed to be available or needed for current local search.
- Verification: sanitized tests cover SQL wildcard escaping, normalization, date/chat scoping, multi-call preparation, partial/failing coverage, deduplication, continuation and truncation at `max_matches`. A generic MCP client test verifies the typed contracts. Direct Teams search is not part of these completion claims.
- Proposed native-search investigation, if still valuable: trace a controlled Teams Web text and participant search, including HTTP or WebSocket traffic only if observed, then compare request body, auth audience, paging and result coverage against known cases. Use read-only requests and app-owned credentials; route strings alone are not evidence of support.
- Possible user help: identify known phrases, participant names and expected conversations only if a future native-search comparison is pursued.

## P2.4 Scheduled attention summaries and topic research

**Status: Retrieval primitives exist; optional client-driven recurring workflow is not configured.**

- Intended outcomes: daily pending tasks/requests requiring direct or indirect attention, recent topic updates, and ad-hoc research across chats with optional priority conversations.
- Proposed path: define a small evaluation set of existing requests, replies and resolutions. Exercise the client workflow: request a time range, fetch/upsert/search on demand, fetch surrounding context, and cite chat/message IDs, authors and times. Test timezone cutoffs, unfinished coverage and contradictory follow-up messages. Each scheduled client request uses the same lazy storage flow; a separate archive scheduler is unnecessary.
- Open decisions: which MCP client schedules the run, when its host is available, how reauthentication is surfaced, delivery location, and how to distinguish pending work from completed or merely inferred tasks.
- Completion evidence: a scheduled run reports its actual time/chat coverage, produces traceable findings, distinguishes explicit requests from inferences, and exposes failures or missing scope. Scheduling alone does not prove useful task extraction.
- Possible user help: set the schedule and notification destination, clarify examples of pending/completed work, and authorize any external delivery separately. Summarization never implies permission to send Teams messages.

## Nice-to-haves and their investigation paths

These and P1.5 attachment sending remain optional, below core retrieval, MCP and lazy storage. Attachment downloading remains optional; Favorites remains last.

| Item | Status | Current gap | Proposed path and completion evidence |
|---|---|---|---|
| N1. Download attachments/images in exports | **Partial** | Markers/links exist; binary downloads do not. | Verify file/AMS download authentication from existing attachment metadata, redirects, expired links and names; package safe relative paths and report individual failures without losing text. A restored export must open its included files. |
| N2. Remote read/unread indicators | **Partial** | The catalog exposes an unread value, but the UI does not display it. Remote freshness and exact semantics are not validated. | Compare existing remote values to the Teams UI, including missing/stale values. Show only verified states. Do not invoke mark-read endpoints to test this feature. |
| N3. Pictures on messages | **Partial** | Account/chat picture URI fallback exists; message sender pictures do not. | Build on verified sender identity, compare available URI behavior and authentication, and preserve generic fallbacks. Direct URIs are preferred over local image downloads. |
| N4. Conversation viewer and rendering fidelity | **Partial** | There is no viewer; export and shared message formatting exist. | Reuse retrieval; use sanitized examples for mentions, replies, multiline text, rich/system messages and missing metadata. Preserve raw/unknown event meaning rather than inventing an actor/action. Verify functionality and shared formatting; appearance tests are not required. |
| N5. Optional presence indicator | **Pending / unverified** | No status is displayed, and a usable presence source is unverified. | Inspect existing account data and Web presence usage only if inexpensive and accessible with available authentication. Otherwise defer, as permitted. Profile failure must not block the UI. |
| N7. Optional Markdown input parsing for outgoing messages | **Pending** | `send_message` treats input as plain text, so Markdown fences are sent literally. | Add an input-parser flag controlled by the requester; it defaults to enabled and can be disabled per request. Version 1 recognizes only code fences delimited by three backticks, with an optional language tag after the opening fence. Version 2 adds ordered and unordered lists. Leave other Markdown untouched. Convert supported syntax into Teams-supported rich text safely, preserve code/list content and surrounding text, and verify how the generated payload renders in Teams. |
| N6. Favorites/pinned section | **Pending / unverified** | Favorites presentation is absent; `isSticky` is not a verified Favorites marker. | Compare already configured Favorites with Web responses/call sites. Once mapped, display the section first without changing Teams state. This remains the lowest priority. |

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
