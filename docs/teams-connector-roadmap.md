# LRH Teams Connector Roadmap

## Scope and evidence

**Canonical connector terminology and implementation cutover (2026-10-11):** use
`lrh-teams-connector` for repository/MCP identity, `teams-connector` for the CLI, and
`teams_connector` for the Python package. Extractor/exporter are not application identities; export
remains a connector operation. The duplicate `teams_exporter` implementation and obsolete
dependency have been removed; the dated module/cache names below are migration history, not current
setup instructions.

The canonical README and architecture describe the target package. Older module names, cache paths,
and implementation details recorded below are dated migration evidence, not current setup
instructions. The agent performs necessary one-time local configuration edits and transparent
current-format file moves. Deliver only current names, paths and formats: no legacy aliases,
forwarding wrappers, compatibility dependencies, old-format readers, automatic import/migration,
dual-cache fallback or committed migration helpers. Preserve account ownership and current-format
data safeguards.

**Product ownership (2026-10-11):** Outlook owns Exchange calendar events, schedules, agenda,
organizer/invitees, event attachments, and join links. Teams owns related Teams meeting chats,
recordings, transcripts, recap, and meeting notes. Verified identifiers connect those resources;
general OneDrive/SharePoint file discovery belongs to the planned SharePoint connector. Calendar
and meeting-resource ownership does not depend on which product hosted a shared search endpoint.

**Standalone search update:** The pinned evidence is in repository `lrh-outlook-connector`, file
`docs/outlook-api-research.md` §3.14, at commit `3c71bb1f3b517a4b28d6aa752b1ee4e9fc8b494e`.
From an Outlook connector checkout, inspect it with
`git show 3c71bb1f3b517a4b28d6aa752b1ee4e9fc8b494e:docs/outlook-api-research.md`. It records
successful app-owned OWA-host Search v2 Message/Teams requests with pagination. One known chat's
result identities matched; a second target's identity bridge remains unresolved. The current
product search is still fetch/archive/local matching. Broad-discovery work can use the successful
shared Search profile while completing message identity and context checks; prior Graph denial
does not establish that every remote route is inaccessible.

**Small reliability fixes (implemented in the shared-auth reliability PR; synthetic checks,
2026-10-11):**

- `src/teams_connector/remote/graph.py` detects repeated Graph continuation links and stops
  collection instead of looping or collecting duplicate pages. Covered by
  `tests/unit/test_graph_paging_and_batch.py`.
- Graph JSON batches retry transient GET sub-responses with status 429, 502, 503 or 504 in bounded
  rounds, honoring `Retry-After` where supplied. These are read requests; message sends remain
  outside this retry path. Covered by `tests/unit/test_graph_paging_and_batch.py`.
- `src/teams_connector/service/localfiles.py` tolerates a file disappearing during concurrent
  cleanup while still surfacing other filesystem errors. Covered by
  `tests/unit/test_localfiles.py`.

These are synthetic regression checks; no live-service performance or reliability improvement has
been measured.

This is the consolidated register of gaps between the current application and the intended product. Endpoint facts and captured payloads live in [Teams API research](teams-api-research.md); module responsibilities are in [Project architecture](project-architecture.md); usage is in [MCP server guide](teams-mcp-server.md).

Implementation status in this register was reviewed on **2026-10-12**. Earlier capture dates below
remain the dates of that evidence, not the date of the current code review.

This snapshot was refreshed on **2026-10-01** against the uploaded repository plus a complete downloaded Teams Web source bundle and a network capture taken around three controlled actions:

1. top-bar search for `SEARCH CODEX $123 ABC`;
2. chat-list search for `SEACH CHATS CODEX #190 ABC`;
3. an explicitly authorized self-chat send containing `SEND MESSAGE TO MYSELF CODEX #789 AJHLK`.

The source/capture is private Teams implementation evidence, not a supported Microsoft API contract. Claims below distinguish application status from confidence in Microsoft behavior.

Status terms:

- **Done:** the product capability required by this project exists and has suitable tests/evidence. This does not claim universal behavior across every tenant/build.
- **Partial:** useful implementation or strong API research exists, but a specific product gap remains.
- **Pending:** the requested capability is not implemented yet.
- **Possibly abandoned:** there is a plausible implementation path, but it is currently unnecessary, redundant, or too low-value to justify the extra private-API complexity. Revive only if a concrete product need appears.

## Intended product

- Retrieve relevant one-to-one, group, meeting, self, and supported channel conversations with stable identities, useful names, correct types, and honest catalog coverage.
- Retrieve message history with reliable pagination and inclusive date/count filters.
- Offer a simple local page for manual text/ZIP exports without an AI client, with an optional self-contained asset bundle mode.
- Expose the same engine through client-independent MCP for fresh conversation discovery, scoped/deep content search, optional broad Teams discovery, complete conversation retrieval, and surrounding context.
- Retain fetched messages indefinitely in account-bound SQLite storage, populated lazily by retrieval/export/search rather than by a permanent background collector.
- Send explicitly authorized text messages, and optionally host-computer file attachments, through MCP.
- Let external AI/MCP clients research topics and optionally schedule attention workflows using the MCP primitives; scheduling itself is outside this repository.
- Keep presentation extras such as presence, Favorites, sender pictures, authenticated attachment/resource access, Markdown send rendering, and a conversation viewer optional.

## Current product status

| Work | Status | Current conclusion |
|---|---|---|
| G1 Prefer Microsoft Graph where parity is proven | **Pending — high priority** | Systematically migrate capabilities from private Teams/Web endpoints to supported Microsoft Graph APIs whenever live parity tests prove Graph is at least as complete and does not regress behavior. Start with channels and SharePoint/OneDrive/file-resource work; also evaluate profile photos and message sending. Keep private APIs only where Graph is unavailable/incomplete (notably private-chat catalog/history, richer presence, Favorites/read-state, and current broad Teams search). No backward-compatibility or state migration is required: reset reconstructable databases/caches/indexes and reauthenticate from scratch when useful. |
| P1.1 Catalog completeness, types, names and ordering | **Pending** | Core catalog behavior exists; this is a finish-line **quality review**, not a redesign/refactor project. Verify/fix type precedence, app names, unnamed-group display names, startup/bootstrap/delta consistency, and icon/label agreement, then reset the reconstructable catalog cache and bootstrap cleanly. |
| P1.2 Long-history filtering and export reliability | **Done** | Current bounded pagination, date/count behavior, retries, dedupe, cancellation, progress and spooled export assembly are considered settled. Do not add process-restart resume or speculative segment optimizations without a new concrete failure. |
| P1.3 Self-chat discovery, identity and targeting | **Done** | `48:notes` is handled explicitly, stable sender identity works, self activity is refreshed outside CSA, history works, and current Teams Web independently confirms direct self-chat sending. |
| Plain-text message sending | **Done** | The connector uses the same Chat Service `/messages` resource and a compatible payload shape as current Teams Web. |
| S1 Send response server message ID | **Partial** | Current Teams Web derives the server message ID from the HTTP `Location` header while the body can contain only `OriginalArrivalTime`. The connector currently discards response headers, so successful sends can return `message_id=None`. |
| P1.5 Attachment upload/access/delivery | **Pending** | Implementation waits for a controlled Teams Web attachment-send capture. SharePoint/OneDrive authentication is likely part of the path and should be added to the app-owned auth model if required. |
| P2.1 Lazy indefinite message storage and delete/edit semantics | **Partial** | Durable storage is implemented. Finish-line work is to verify live edit/delete behavior and enforce the chosen retention rule: edits overwrite; soft-deleted message text is retained whenever known and exports/search must flag deletion explicitly. |
| P2.2a Conversation search — local HTTP UI | **Done** | The browser search box remains an offline in-memory JavaScript filter over the already loaded catalog. |
| P2.2b Conversation search — MCP | **Partial** | `search_chats` defaults to refreshing the catalog; `refresh=false` is cache-only. Participant names still depend on previously indexed rosters, so freshness coverage is reported rather than triggering unbounded roster synchronization. |
| P2.2c Scoped content/message search | **Done** | Date/chat-scoped remote preparation plus SQLite message search and participant indexing provide the deep, auditable search path. |
| P2.2d Broad Teams discovery via Substrate | **Partial** | `search_remote_messages` performs bounded online Teams-message discovery and returns pointers, timestamps, and conservative conversation matches. Heterogeneous result categories, recap discovery, transcript discovery/search, and direct hit-id hydration remain separate open work. |
| P2.3 Complete MCP content and continuation | **Done** | Bounded tools, continuations, large-message continuation, export snapshots, resource reads and tool-based export reads are implemented and tested. |
| P2.3a MCP history filter parity | **Done** | `get_messages` and exports expose inclusive `since`/`until` and a total latest-`N` `message_limit`, separately from response `page_size`. |
| P2.3b Media-inclusive export bundles | **Done** | UI and MCP exports include selected message attachments by default, rewrite references to local names, and return one artifact per conversation; one artifact is returned directly and multiple artifacts are always wrapped in an outer ZIP. |
| P2.4 AI topic research through MCP | **Done** | The server exposes the retrieval/search/context primitives an external AI client needs. Scheduling/summarization/delivery is an external use case, not a requirement of this repository. |
| D1 Cross-surface DRY / shared domain boundaries | **Partial** | The repository already centralizes substantial behavior in `TeamsService`, but HTTP and MCP still duplicate or re-own some domain behavior. Do a targeted quality pass: shared chat resolution, export request/job/artifact contracts, history selection semantics, authenticated resource fetching, and domain errors. Keep HTTP/MCP transport concerns separate; this is not a broad architecture rewrite. |
## Finish-line priorities

### 1. G1 — prefer Microsoft Graph wherever live parity is proven

**Status: Pending. High-priority reliability migration.**

The repository currently depends heavily on private Teams Web/Chat Service/CSA/Middle Tier endpoints. Those integrations are necessary where Microsoft Graph does not expose the required capability, but a supported Graph implementation should be preferred when it can deliver the same product behavior without regression.

This is a **migration-for-reliability** effort, not an instruction to add Graph as a parallel backend everywhere. The desired steady state is:

```text
supported Graph capability with proven parity
    -> use Graph as the canonical implementation
    -> remove the superseded private implementation where practical

Graph unavailable / permission unavailable / materially incomplete
    -> keep the current private Teams implementation
```

Do not maintain dual implementations merely for compatibility. Avoid automatic fallbacks for non-idempotent writes unless delivery disposition is known; a failed/ambiguous send must never be retried through another backend in a way that can duplicate a message.

#### Live evidence already established

The 2026-10-01 Graph/private-API probe using the existing Teams first-party client identity established:

```text
Graph capability                                      Result
----------------------------------------------------  ------
current user                                          works
current-user profile photo                            works
joined Teams                                          works
team channels                                         works
channel metadata                                      works
channel members                                       works
channel messages                                      works
channel replies                                       works
OneDrive metadata/root listing                        works
Microsoft Search driveItem                            works
send message to known self chat 48:notes              works
private Chat Service sees the same Graph-sent ID      works

list private chats                                    blocked: no Chat.ReadBasic/Chat.Read/Chat.ReadWrite
private chat metadata/members/history                 blocked by missing Chat.* read scopes
read exact Graph-sent private-chat message            blocked by missing Chat.Read
edit/delete private-chat message                      blocked by missing Chat.ReadWrite
presence                                              blocked by missing Presence.Read
Microsoft Search chatMessage                          blocked in both v1 and beta
```

The tested environment's explicit delegated `Chat.Read` request with the Microsoft Teams first-party client ID returned `AADSTS65002`. Private-chat Graph reads remain unavailable in the current application's tested configuration. A fork or policy recheck must re-establish its grants and usable API calls before choosing this split; this historical denial is not a universal restriction.

#### Migration order

##### G1.1 — channels: migrate to Graph after parity/regression tests

Graph successfully bootstrapped:

```text
/me/joinedTeams
    -> /teams/{team}/channels
    -> channel metadata/members
    -> channel messages
    -> message replies
```

Prefer Graph for channel discovery/history once the normalized output is proven equivalent to or better than the current private path.

Requirements:

- map Graph team/channel/message/member records into the existing shared domain models rather than exposing Graph-shaped records to HTTP/MCP;
- verify timestamps, message IDs, authors, replies, system/event records, attachments, pagination, edits/deletes and channel types against representative current data;
- preserve archive/search/export behavior;
- once parity is proven, remove the redundant private channel-fetch implementation instead of keeping two permanent backends;
- if a specific Teams channel capability is not available through Graph, isolate that exception rather than keeping the entire old channel stack.

##### G1.2 — SharePoint/OneDrive resources and attachments: strongly prefer Graph

The existing token identity has working `Files.ReadWrite.All` / `Sites.ReadWrite.All` access and Graph OneDrive reads succeeded. P1.5, N1 and P2.3b should therefore treat Graph as the preferred SharePoint/OneDrive resource provider.

Desired resource architecture:

```text
AuthenticatedResourceFetcher
    -> Graph Drive/SharePoint provider          preferred when resource can be resolved
    -> AMS / Teams-specific provider            where Graph is not applicable
    -> direct SharePoint/private fallback       only when actually required
```

Use Graph where possible for:

- resolving DriveItems from Teams attachment metadata/sharing URLs;
- downloading attachment bytes;
- uploads required for host-file sends;
- metadata such as filename, size, MIME/file/folder identity;
- media-inclusive export materialization.

The controlled attachment-send capture is still required before declaring P1.5 complete because successful storage upload is not sufficient: the final Teams message/file-reference linkage, permissions and recipient accessibility must still be validated.

##### G1.3 — profile pictures: evaluate migration to Graph

Graph profile-photo retrieval works with the current token.

The existing Middle Tier implementation is already functional, so migrate only after checking:

- equivalent coverage for self and other chat participants;
- application/bot/group/avatar cases that may not map to Graph user photos;
- image size/quality and missing-photo semantics;
- no regression in the existing lazy local cache/proxy behavior.

If Graph covers the ordinary-user cases cleanly, make it the preferred user-photo provider and retain a Teams-specific provider only for identities Graph cannot represent.

##### G1.4 — chat message sending: evaluate Graph as the canonical write transport

A live Graph send to the known Teams self-chat ID `48:notes` returned `201`, and the existing private Chat Service immediately observed the same server message ID. This proves cross-backend ID interoperability for that case.

Before migrating the product send path, test disposable messages across representative supported destinations:

- self chat;
- ordinary one-to-one chat;
- group chat;
- meeting chat;
- rich HTML generated by N7;
- mentions/links and attachment-bearing messages once those are supported.

Only migrate if Graph preserves the behavior and metadata the product needs. If migrated:

- use one canonical send path, not Graph plus Chat Service fallback;
- preserve the no-retry rule for ambiguous message writes;
- return the authoritative Graph/server message ID directly;
- remove the superseded private plain-text send implementation unless a documented unsupported destination still requires it.

Do **not** attempt Graph edit/delete under the current token: `Chat.ReadWrite` is not available.

##### G1.5 — do not migrate private-chat reads/catalog merely to reduce private API count

Keep CSA/Chat Service for the capabilities Graph cannot currently perform under this authentication identity:

- private/group/meeting/self chat discovery and catalog state;
- private chat members/rosters where Graph permissions block access;
- private chat history and continuation;
- self-chat history;
- private-chat edit/delete observation;
- Teams-specific delta/catalog semantics.

Do not introduce a Graph-shaped abstraction around these operations when Graph cannot execute them.

##### G1.6 — keep Teams-native services where they are materially richer

Do not replace useful Teams-specific services with Graph merely because a vaguely related Graph API exists:

- Unified Presence remains preferable for the desired availability/activity/last-active/device information; current Graph presence permission is unavailable anyway.
- Favorites/conversation folders and CSA-style read/unread state remain Teams-private concerns.
- Broad discovery should remain Substrate Search v2 unless a future Graph search path proves equal or better across the heterogeneous Teams result types. The current token can search `driveItem`, but Graph `chatMessage` search is denied even though direct channel-message reads work.
- AMS and other Teams-specific media resources may still require their native service when no equivalent DriveItem exists.

**Search review (2026-10-10):** a supplied Teams web capture confirms the global Substrate Search v2
route is used for submitted searches, while targeted client source maps `FILEDOMAIN` to `File` and
`MEETINGDOMAIN` to `Event`. The client also has a separate Meet-app Event builder and distinct
meeting-recap/transcript retrieval paths. These are browser/source observations, not standalone
connector proof. Graph `driveItem` success and `chatMessage` denial remain specific to the tested
profile; reassess before choosing migration or unsupported status. Details are in
[Teams API research](teams-api-research.md#microsoft-365-search-capture-and-source-audit-2026-10-10)
and the [cross-product search findings](https://github.com/lucasrhode95/lrh-outlook-connector/blob/main/docs/m365-search-findings.md).

**Local known-hit check (2026-10-10):** the connector fetched one scoped self-chat item and then
returned it from local substring search; exact mode returned no match because the probe term was
embedded in the message. Preparation and requested-scope coverage completed, and no server-side
search was used. This validates local archive search only; it did not validate online discovery. The
bounded `search_remote_messages` implementation now covers one Search v2 Message/Teams page per call
through the shared `m365.search` profile. Its response mapping and continuation have synthetic tests,
while a live call through this connector and direct hit-id hydration remain unverified; see the
current P2.2d status above and [API research](teams-api-research.md#teams-online-message-search-adapter-2026-10-12).

**Required Microsoft 365 coverage:** a complete cross-product search workflow also needs calendar
events, meeting recap results, transcript discovery/retrieval, and transcript-text search where a
supported provider exposes it. Those remain separate from this Teams message-search tool. Captured
Search v2 `Event` results and browser recap/transcript routes are not proof of Teams-connector
access. Keep Event, recap, transcript-list, transcript-content, and transcript-keyword capabilities
distinct, and report unverified or blocked paths explicitly.

#### Authentication / token integration

Do not build a separate authentication subsystem for Graph.

Reuse the existing `TokenProvider` with:

```text
https://graph.microsoft.com/.default
```

and the same persisted account/session model used by the application.

Graph should be represented as a small capability/provider layer, not a second monolithic `TeamsConnector`. Keep provider-specific HTTP/error/pagination code behind shared domain/resource interfaces so HTTP and MCP remain unaware of whether data came from Graph or a Teams-private service.

#### Fresh-state / no-compatibility policy

This migration does **not** need backward compatibility.

During implementation it is acceptable to:

- delete/recreate the local SQLite message/search database when schema/source assumptions change;
- clear catalog/participant/resource caches and rebuild them;
- clear temporary export/job state;
- clear the application's token cache and require a clean sign-in if token/resource changes make that simpler;
- regenerate fixtures produced from the new canonical implementation.

Do not create database migrations, compatibility adapters, old/new source reconciliation, token-cache migration code or dual-write machinery just to preserve development-state data.

Preserve source-controlled files and deliberately retained user export artifacts unless a specific cleanup step explicitly targets them.

#### Acceptance rule

A Graph migration is complete only when tests/live probes demonstrate **no meaningful loss of current behavior**.

For each migrated capability:

1. capture representative output from the existing implementation;
2. capture the Graph equivalent;
3. compare IDs, ordering, metadata, pagination, formatting-relevant fields and error behavior;
4. add regression fixtures/tests for the agreed canonical behavior;
5. switch the shared domain/service path to Graph;
6. remove redundant private code where it no longer serves a documented capability;
7. reset reconstructable local state and bootstrap fresh.

Long-term reliability is the reason to migrate. If Graph is less complete, introduces an observable regression, or needs permissions this Microsoft client cannot obtain, keep the proven private implementation for that capability.


### 2. P1.1 — catalog quality review

**Status: Pending. Highest-priority remaining core quality pass. This is not an architectural refactor.**

The 2026-10-01 diagnostic and current Teams Web source remove most of the ambiguity. The goal is to review the current catalog implementation against that evidence, fix concrete mismatches, add regression tests, and stop.

Current Teams search code explicitly uses semantic subtypes such as:

```text
OneOnOneChat
PersonalNotes
NamedGroupChat
UnnamedGroupChat
MeetingChat
SharedChannel
StandardChannel
```

Treat that as useful current-client taxonomy, not proof that every catalog endpoint returns those exact strings.

Quality checks/fixes:

- Preserve explicit `chatType=meeting`, `threadType=meeting`, or equivalent meeting semantics before evaluating `isOneOnOne=false`; an explicit meeting must not normalize to `group`.
- Use one canonical normalized type for backend records, UI icon choice and UI subtitle. ID forms such as `19:meeting_...` are defensive diagnostics/fallbacks only.
- For untitled app/bot one-to-one conversations, use verified app metadata such as `lastMessage.properties.fromAppMetadata.name` only when roster/sender evidence shows the peer is the corresponding `28:...` app identity.
- For unnamed groups, synthesize a stable participant-based display name from resolved profile names in a simple deterministic order, preferably case-insensitive alphabetical display name.
- Group-name length rule: append full names separated by ` | `. Aim for about 80 characters; before adding another full name, stop if the addition would push the accumulated text above **87 characters**, then append ` +N` for omitted roster members. Never expose unresolved opaque IDs as names.
- Ensure startup/bootstrap/delta paths produce the same normalized result for the same raw row. Reuse existing normalization where sensible, but do not turn this quality review into a broad cleanup/refactor campaign.
- Verify the app-name fallback, group-name fallback, meeting/group/1:1/self types, and UI icon/label presentation with fixtures derived from the diagnostic payload shapes.
- After the fixes, **reset the reconstructable local catalog/cache and bootstrap fresh**. Do not build a migration/reconciliation/version framework for this development database. Preserve credentials and anything that is not safely reconstructable.

The current self-chat activity refresh, empty-chat filtering, bounded export behavior, profile-image caching and cache-first UI loading are not gaps to reopen unless tests expose a concrete regression.
### 3. S1 — return the server message ID after sends

**Status: Partial. Small quality fix.**

The controlled self-send capture and current Web source agree on the Chat Service write path:

```text
POST /v1/users/ME/conversations/{conversationId}/messages
```

The captured response body was shaped like:

```json
{"OriginalArrivalTime": 1790870496669}
```

Current Teams Web obtains the server-side message ID from the final path component of the response `Location` header and also reads a response context ID. The Python connector's generic `_http_json()` currently drops headers, so `send_message()` can accept delivery while returning no `message_id`.

Preferred fix: let the write path retain a **small allowlist of safe response metadata** such as `Location` (and `contextid` only if useful), or add a narrowly scoped response helper. Do not expose or persist arbitrary response/request headers. Keep the current no-retry rule for message writes because a network failure can leave delivery disposition ambiguous.

### 4. P2.1 — settle live edit/delete behavior and retention semantics

**Status: Partial. The storage architecture already exists; settle the remaining semantics empirically.**

Product decision:

- **Edits:** overwrite the stored body with the newest observed version. Keep `edited_at`; do **not** retain edit history/old versions.
- **Soft deletes:** retain message text whenever we have ever observed it.
  - If the backend's deleted-message record still contains the body, store that body and mark the row deleted.
  - If a later deleted-message record omits/clears the body but SQLite already has the prior content, **do not erase the cached body**. Preserve it and update only deletion metadata/state.
  - If the first time we ever see a message is already deleted and the backend provides no content, store the tombstone/metadata with no invented body.
- **Exports:** deleted rows must be unmistakably labeled as deleted while still showing retained body text when available. The existing deleted marker should remain prominent enough that retained deleted content can never be mistaken for a live message.
- **Search:** retained deleted content is useful for incident/history investigation. Keep it searchable, but every result must expose/label `is_deleted` (and `deleted_at` when known) so the state is explicit.

#### Authorized live experiment for Codex

The user explicitly authorizes a disposable test against the user's **own self-chat only (`48:notes`)** to settle this behavior once and for all.

Codex may create a temporary script (preferably under a temp/dev-only path and not committed as product code) using the normal connector/dev authentication. It may:

1. send several uniquely prefixed disposable test messages to `48:notes`;
2. fetch them and allow the normal archive path to store their initial content;
3. edit one message one or more times;
4. delete another message;
5. fetch raw history after each mutation as needed;
6. inspect how IDs, `edittime`, `deletetime`, message type and body/content change in the live backend;
7. run the normal service/index/export path and verify the SQLite result matches the policy above;
8. delete/clean up remaining disposable test messages when the experiment is complete where practical.

The authorization is limited to the user's self chat and disposable messages created for this test. Do not mutate other chats/messages. Never print tokens/cookies/auth headers. Record sanitized raw behavior and update `docs/teams-api-research.md` with the verified edit/delete contract afterward.

### 5. P1.2 — long-history filtering and export reliability

**Status: Done / settled.**

The current behavior is considered as good as needed for this project:

- opaque continuation traversal;
- message-ID dedupe;
- inclusive local date checks;
- verified `startTime` lower bound;
- remote work bounded by requested message count;
- chronological output;
- bounded retries for reads;
- cancellation and progress;
- spooled export assembly rather than retaining arbitrarily large histories in memory.

Do **not** add process-restart export resume, speculative segment-boundary optimization, or other resilience machinery unless a real user-visible failure later justifies it.
## P1.3 — self chat and plain-text sending

**Status: Done.**

Observed/reinforced behavior:

- the self/notes thread uses `48:notes` in this account/build;
- CSA does not reliably carry it, while Chat Service/history does;
- current Teams search taxonomy calls the self subtype `PersonalNotes`;
- standard member lookup can fail for this special thread, so self handling must not depend on its roster endpoint;
- direct history retrieval works;
- the 2026-10-01 capture shows a direct write to the self conversation's `/messages` resource and a following consumption-horizon property request;
- sender identity should continue to use stable IDs, never display-name matching.

The connector/service can keep `48:notes` isolated as a private convention so it can be replaced if Microsoft changes it.

## P1.5 — send host files as Teams attachments

**Status: Pending. High-priority dependency of G1/P2.3b; still wait for the next controlled attachment-send capture before final implementation.**

Graph authentication is now proven usable with `Files.ReadWrite.All` and `Sites.ReadWrite.All`, and basic OneDrive/DriveItem access works with the same Teams first-party identity. Prefer Graph for the SharePoint/OneDrive storage/resource portion when the capture proves that doing so preserves the final Teams attachment semantics.

The current source research establishes much of the likely pipeline, but the user will send a harmless attachment through Teams Web and provide the resulting network capture so we can remove remaining ambiguity before final product code.

Known/source-backed pieces:

- ordinary chat files are stored through SharePoint/OneDrive paths, commonly under `Microsoft Teams Chat Files`;
- quick/small uploads use drive-content upload routes;
- larger uploads can use chunked `StartUpload` / `ContinueUpload` / `FinishUpload` style operations;
- Chat Service exposes a thread file-reference operation such as `POST /v1/threads/{threadId}/files`;
- the outgoing message model serializes ordinary file metadata into `properties.files`; inline media can use AMS references separately.

The next capture should settle:

1. which SharePoint/OneDrive/Office token audience the current Teams Web flow actually uses;
2. how the drive/site/folder is discovered;
3. the exact storage upload call(s) for a normal small attachment;
4. whether/when Teams creates the Chat Service thread file reference;
5. sharing/permission operations performed before the message is sent;
6. the exact `properties.files` entry and final `/messages` payload;
7. response IDs needed for safe retries/deduplication.

If SharePoint/OneDrive access requires an additional app-owned MSAL resource/scope, add it deliberately to the authentication flow. That is acceptable for this product: its purpose is to let authorized agents retrieve and work with the signed-in user's Teams information. Do not copy browser cookies/tokens into the implementation.

Successful storage upload alone is not proof of successful Teams attachment delivery; verify that the intended recipient can open the delivered attachment.
## P2.1 — lazy indefinite storage

**Status: Partial until the authorized self-chat mutation experiment is complete.**

The storage model remains:

```text
requested remote scope
      -> fetch through normal Chat Service history
      -> normalize/format
      -> upsert account-bound SQLite
      -> query merged retained history
```

Already present:

- per-account durable SQLite selection;
- lazy population through retrieval/export/search;
- message dedupe/upsert;
- timestamps, authors, message types, edit/delete metadata;
- participant roster index;
- online backup;
- range coverage tracking and continuation.

Final semantics are defined in the P2.1 live-validation section above: edits overwrite; deleted content is retained whenever previously known; tombstones never erase known body text; exports and searches explicitly label deleted rows.

No permanent background sync job and no edit-version history are required.
## P2.2 — search

The product now has three deliberately different search layers. Keep them separate because they solve different problems.

### Conversation search — local HTTP UI

**Status: Done.**

The browser chat-search box should remain simple and offline: filter the already loaded in-memory `chats` array in JavaScript. Typing into this field must not create a remote Teams search request.

### Conversation search — MCP `search_chats`

**Status: Partial.**

The MCP conversation search should favor freshness because its purpose is discovery and the local participant/catalog index may otherwise be days or weeks stale.

Add:

```python
refresh: bool = True
```

through the MCP tool and service API, with **`True` as the default and recommended mode**.

When `refresh=True`:

- refresh the Teams chat catalog before searching;
- refresh/synchronize the participant/name information required for useful conversation matching, within bounded work;
- search current conversation display names/titles as well as resolved participant names;
- report freshness/coverage honestly if some rosters cannot be refreshed/resolved.

When `refresh=False`, perform an explicit cache/index-only search with no Teams network access.

This is still a **conversation finder**, not a message-content search.

### Scoped content/message search

**Status: Done.**

Keep the existing deep/auditable path for questions where the caller actually wants message content:

- explicit date/chat scope;
- fetch/upsert the requested remote scope;
- search merged retained SQLite rows;
- exact/substring/all-term matching with normalization and wildcard escaping;
- bounded response pages, continuation and explicit preparation/coverage;
- retained local history contributes to results;
- retained soft-deleted content remains searchable but is explicitly marked deleted.

This is the right tool once the caller knows which conversations/scope it wants to investigate.

### Broad Teams discovery via Substrate Search v2

**Status: Partial (message category implemented 2026-10-12).**

The standalone-proven endpoint is `POST https://outlook.office.com/searchservice/api/v2/query`,
authenticated with the shared `m365.search` profile. `search_remote_messages` sends
`EntityType=Message` and `ContentSources=[Teams]`, one bounded page at a time. It preserves the
Search v2 message reference, preview, sender, subject, timestamp, and returned conversation-ID
candidates. A local `conversation_id` is attached only when a candidate exactly matches a known
catalog ID. The tool does not populate or search the local archive.

Continuation reuses the same Cvid and LogicalId and advances From by the rows returned. The result
coverage stays incomplete if Search v2 omits its result set or paging flag. Agents can use a matched
conversation and timestamp with `get_messages` to inspect surrounding history, then decide whether
to request another search page. Search result IDs have not yet been verified as history message IDs;
no exact history page number is returned.

Live standalone behavior, known-hit identity results, and failure cases are recorded in
`lrh-outlook-connector/docs/outlook-api-research.md` §3.14. This connector branch has synthetic
request and mapping coverage; it has not yet run an account-backed search through the new MCP tool.

Still open: heterogeneous Search v2 results for chats, files, teams/channels and events; global
recap discovery; transcript discovery and transcript-text search; app-owned transcript retrieval;
and reliable mappings from other result categories to connector resources. Transcript requests that
returned empty or uninterpretable envelopes do not prove transcript search unsupported. Outlook
owns calendar events and their join links; Teams owns related meeting chats, recaps, recordings,
notes and transcripts; SharePoint owns general file discovery.
## P2.3 — complete content through MCP

**Status: Done.**

The MCP layer provides bounded tool results with continuation, separate continuation for oversized individual message content, resumable preparation for history/search work, immutable export snapshots, standard MCP Resources, and `read_export` for clients that prefer tool-based reads.

Client-specific presentation of Resources can vary; that does not make the server implementation incomplete.

### P2.3a — MCP history filter parity

**Status: Done.** get_messages and exports use inclusive since/until bounds and a total
latest-N message_limit, separate from response page_size. Continuations retain the original
selection. Synthetic tests cover date/count selection, paging, response truncation, and export
parity. No additional history-limit redesign is planned.

### P2.3b — media-inclusive self-contained export bundles

**Status: Done (2026-10-12).** Both UI and MCP pass the same ExportRequest to the shared export
orchestrator. include_attachments defaults to true. Only references in the selected messages are
considered; one-chat extraction returns a plain TXT/JSONL artifact when there are no selected
attachments, or a ZIP with one conversation-named folder containing the text export and retrieved
files when references exist. Failed downloads retain the text and replace the remote reference with
an unavailable marker.

For multiple conversations, the orchestrator calls the same per-chat builder and packages those
artifacts in one outer ZIP. Chats with attachments are inner ZIPs; chats without attachments remain
plain files. Every conversation remains a separate artifact; the export has no combined-text option.
Filenames are sanitized and deconflicted. Fetch limits are 200 references per conversation, 100 MiB
per file, and 250 MiB aggregate per conversation. Progress includes attachment totals, success and
failure counts. Browser and MCP surfaces do not implement separate packaging loops.

Synthetic regression coverage includes single-chat plain/ZIP output, reference rewriting, selected
range isolation, unavailable attachments, mixed multi-chat artifacts, multiple attachment-free chats
in an outer ZIP, and progress reporting. This is local artifact generation and does not change Teams.

## P2.4 — AI research workflows

**Status: Done.**

The server exposes the ingredients an external AI/MCP client needs to perform topic research, pending-work analysis and scheduled attention summaries: chat discovery, scoped/broad search, message retrieval, surrounding context, coverage, stable IDs and timestamps.

Scheduling, summarization cadence and delivery destination are **use cases of the MCP interface**, not product requirements for this repository. Do not add an internal scheduler/summarizer merely to reproduce those examples.
## D1 — cross-surface DRY / shared domain boundaries

**Status: Partial. Targeted quality cleanup, not a broad refactor.**

The repository already has the right broad layering:

```text
HTTP adapter ----\
                  -> TeamsService / shared domain behavior -> TeamsConnector / index / formatter
MCP adapter -----/
```

Preserve that direction. The goal of this item is to remove the remaining places where HTTP and MCP own parallel copies of the same **application/domain decision**, while keeping genuinely protocol-specific behavior in the adapters.

This work should be opportunistic and bounded. Do not delay higher-value finish-line work just to chase method-count reduction.

### D1.1 — shared chat resolution

Today several service operations independently scan the catalog for a conversation ID, while the HTTP layer also drains the paginated catalog and reconstructs `ChatSummary` objects for export preparation.

Introduce small shared service/domain helpers conceptually like:

```text
resolve_chat(conversation_id, refresh=false) -> ChatSummary
resolve_chats(conversation_ids, refresh=false) -> list[ChatSummary]
get_full_chat_catalog(...) -> list[ChatSummary or shared chat records]
```

Exact naming is flexible.

Goals:

- one definition of "known conversation";
- one place for refresh behavior and not-found errors;
- preserve requested input order when resolving multiple IDs;
- reject/handle duplicates consistently;
- HTTP must not need to rebuild a domain `ChatSummary` from its own JSON response records merely to call the export service;
- send/history/export/search preparation should reuse the same resolver where practical.

Do not introduce a repository or abstraction layer merely for its own sake; a few well-named methods on `TeamsService` are enough.

### D1.2 — shared export request, progress and artifact contracts

**Status: Done for UI/MCP export packaging (2026-10-12).** Both surfaces call the same
`Exports.export(ExportRequest)` implementation and receive the protocol-neutral `ExportArtifact`.
The UI may run that call as a cancellable background job with progress polling; MCP runs it inside
the tool call. The service owns selection, per-conversation extraction and packaging, so there is no
second HTTP-only single-versus-multiple-conversation package builder. Continue to keep job polling,
cancellation, HTTP download headers and MCP artifact delivery in their respective adapters.

The exact packaging contract is recorded under [P2.3b](#p23b--media-inclusive-self-contained-export-bundles).

### D1.3 — one `HistorySelection` semantic contract

Keep protocol parsing separate:

```text
HTTP UI:
calendar dates + browser UTC offset

MCP:
timezone-aware ISO-8601 timestamps
```

Those are legitimately different adapter concerns.

After parsing, both surfaces should construct the same domain-level selection, conceptually:

```text
HistorySelection(
    since=...,
    until=...,
    message_limit=...,
)
```

That shared object/validation path owns the semantics:

```text
since/until are inclusive
date filtering happens first
message_limit selects the latest N qualifying messages
latest-N counting proceeds backward from the newest qualifying message
```

Use it for:

- HTTP export;
- MCP export;
- MCP `get_messages` bounded-history mode;
- any future batch export;
- asset selection in P2.3b.

`page_size`, `max_response_chars`, and MCP continuation mechanics remain transport/presentation concerns rather than fields in `HistorySelection`.

### D1.4 — generalized authenticated remote-resource layer

The current HTTP profile-picture path already demonstrates the desired pattern: validate/register a protected upstream resource, expose an opaque local identity, and fetch it with the application's own credentials when needed.

Generalize this into one internal resource model/fetch layer rather than implementing separate end-to-end pipelines for:

```text
profile pictures
message images
AMS resources
SharePoint attachments
OneDrive attachments
```

Conceptually:

```text
RemoteResource
ResourceDescriptor
AuthenticatedResourceFetcher
provider-specific fetch adapters
```

The resource descriptor should identify a validated known resource and the required fetch strategy without exposing reusable credentials. P2.3b now materializes selected export attachments through the shared `Files` service; a general registered-resource gateway for lazy UI/MCP viewing is still not implemented.

Consumers then become:

```text
HTTP lazy resource gateway -> fetch/stream registered resource
MCP resource/tool          -> fetch/read registered resource
media-inclusive export     -> fetch same resource and write bytes into ZIP
```

Use provider/source-specific validation and auth strategies under the shared interface. Do not build an arbitrary URL proxy.

The resource layer should also centralize where appropriate:

- allowed hosts/path families;
- filename/content-type normalization;
- size limits;
- safe redirects;
- retry policy for idempotent reads;
- resource-fetch errors;
- optional bounded cache metadata.

### D1.5 — shared domain validation and errors

Keep JSON/MCP schema parsing in each adapter, but move shared business validation to the domain/service layer.

Examples that should have one authoritative implementation:

- conversation IDs exist and belong to the current catalog;
- multiple conversation selections are deduplicated/resolved consistently;
- `since <= until`;
- valid positive `message_limit`;
- supported export/media combinations;
- resource exists/is allowed;
- export job/artifact state transitions.

Prefer meaningful domain errors, for example conceptually:

```text
InvalidRequestError
ConversationNotFoundError
AuthenticationRequiredError
ExportBusyError
ResourceUnavailableError
UpstreamTeamsError
```

Exact classes/names are flexible; do not manufacture a huge exception hierarchy.

Adapters map those shared failures into their own protocol:

```text
HTTP -> status code + JSON/browser behavior
MCP  -> tool error / structured result
```

### D1.6 — explicitly keep these adapter-specific

Do **not** DRY these merely because the code looks similar:

- HTTP calendar-date + UTC-offset parsing vs MCP ISO-8601 parsing;
- HTTP loopback/origin/CSRF-style safeguards vs MCP stdio/tool transport;
- HTTP redirects/login-page behavior vs MCP authentication errors;
- HTTP file streaming/download headers vs MCP ResourceLink/resource reads;
- HTML/static asset serving;
- MCP tool schemas, annotations and response-content construction;
- HTTP connection preflight that decides between redirecting a page and returning JSON.

The session/token implementation itself should remain shared in `TeamsService`/`TeamsConnector`, as it already largely is.

### D1.7 — tests / acceptance criteria

Add focused parity tests rather than structural tests that assert a specific class count.

Useful cases:

- resolving the same conversation through HTTP-backed and MCP-backed flows reaches the same shared chat record;
- invalid/missing conversation IDs produce equivalent domain failures before protocol mapping;
- identical `HistorySelection` values choose the same messages for UI export, MCP export and bounded `get_messages`;
- one-chat and multi-chat exports use the same single-chat primitive;
- the same protected resource descriptor can be consumed by HTTP lazy access and export materialization without two fetch implementations;
- HTTP and MCP adapters remain thin enough that changing a shared selection/export rule requires changing one domain implementation plus adapter schema/docs/tests, not two business-logic implementations.

If completing another roadmap item naturally exposes one of these duplications, fix it there rather than scheduling a separate sweeping cleanup pass.


## Nice-to-haves

| Item | Status | Direction |
|---|---|---|
| N1. Authenticated attachment/resource access | **Partial** | P2.3b exports materialize selected attachments through the shared `Files` service. A general registered-resource gateway for lazy localhost/MCP viewing, shared across resource sources, remains future work. |
| N2. Remote read/unread indicators | **Partial** | Likely a small quality-of-life feature: validate CSA read-state freshness/semantics and expose a read-only indicator without marking anything read. |
| N3. Message sender pictures/resources | **Partial** | Reuse the same authenticated-resource plumbing as N1 where practical; only the upstream source/auth strategy differs (for example Middle Tier/AMS vs SharePoint). |
| N4. Conversation viewer/rendering fidelity | **Partial / ongoing** | Current exports look very good in real use so far. Continue improving only from actual problematic exports; a richer viewer can reuse the same formatter later. |
| N5. Presence/device indicator | **Partial** | Keep it. Availability/activity, last-active time and device type add useful freshness/context. Verify app-owned auth, cache briefly, fail open, and avoid exposing richer status-note/calendar text by default. |
| N6. Favorites section | **Partial** | Keep as optional quality-of-life behavior behind a bypassable flag/config path. Read actual favorite membership; do not infer it from `isSticky`. |
| N7. Markdown-to-Teams rich-text input | **Pending / wanted** | High-value send enhancement: accept ordinary Markdown from agents and render a safe subset into Teams-compatible HTML so Codex can write naturally. |

### N1 / N3 — lazy authenticated resource gateway

The user's proposed "middle man" model is reasonable and preferable to eager temporary downloads. It is effectively a constrained authenticated reverse proxy for resources the signed-in user is already allowed to access.

Avoid exposing an endpoint like:

```text
http://127.0.0.1:8765/resource?url=https://arbitrary.example/...
```

because that would leak remote URLs into logs/history and risks becoming an SSRF/open-proxy primitive.

Prefer:

```text
http://127.0.0.1:8765/api/resources/{opaque_resource_id}
```

where the server resolves the opaque ID to a previously discovered resource descriptor containing the validated upstream location/type and the required authentication strategy.

Core rules:

- bind to localhost as today;
- never accept an arbitrary caller-supplied upstream URL;
- allow only known Teams/Middle-Tier/AMS/tenant SharePoint/OneDrive hosts and expected path families;
- never forward the caller's headers/cookies upstream and never expose Microsoft auth tokens downstream;
- lazily fetch on first access and stream the response directly when practical; no temp file is required just to proxy bytes;
- preserve a safe subset of response metadata such as MIME type, length and suggested filename;
- optionally cache bytes locally after first access only if repeated access/offline durability makes it worthwhile; caching is an optimization, not the primary design;
- use one internal resource-fetch abstraction with per-source adapters/auth strategies. Separate public endpoints are only necessary if their semantics differ materially;
- consider exposing the same registered resource as an MCP Resource so an agent on the same host does not have to reason about raw localhost URLs.

If SharePoint/OneDrive requires another OAuth resource/scope, add it to the normal authentication model. The goal is authorized access to the user's own work data; the security objective is to constrain where those credentials can be used, not to pretend the tool can function without privileges.

### N2 — read/unread

This looks like a small implementation once the current CSA field semantics are verified against the UI. Keep it read-only; no mark-read mutation is needed to display state.

### N5 — presence/device

Keep the privacy-minimal useful subset by default:

```text
availability
activity
lastActiveTime
deviceType
```

Presence should be short-lived cached data and never block chat listing/export when unavailable.

### N6 — Favorites

Implement favorite extraction as a contained method/path that can be bypassed with configuration, for example an `include_favorites` feature flag. Until the behavior is validated across more accounts, preserve the current flat catalog as the safe fallback/default behavior.

### N7 — Markdown to Teams rich text

This is a desired feature, not an abandoned idea.

The goal is for an agent to call `send_message` with normal Markdown and have Teams receive readable rich text. Support a deliberately bounded, safe subset first:

- paragraphs and line breaks;
- **bold** and *italics*;
- inline code;
- fenced code blocks;
- bulleted and numbered lists;
- links;
- simple tables if current Teams rendering accepts them reliably.

Do not pass raw Markdown/HTML through unsanitized. Convert Markdown to an allowlisted Teams-compatible HTML subset, escape unsupported/raw HTML, and degrade unsupported constructs to readable text rather than failing the send.

Expose an explicit format/parser option if useful, but the MCP path may reasonably default to Markdown parsing so AI clients can write naturally. Plain-text mode must remain available for exact literal sends.

Validate the renderer with disposable self-chat messages before relying on tables/code blocks across all chat types.
## Current Teams identifier/type observations relevant to implementation

These belong primarily in API research, but the roadmap consequences are:

```text
19:meeting_...@thread.v2     observed meeting-thread ID form
19:...@thread.v2             common thread/group/meeting-family form
19:<id>_<id>@unq.gbl.spaces observed one-to-one/unique form
8:orgid:<GUID>               organizational user MRI
28:<id>                      app/bot MRI family
48:notes                     observed self/personal-notes conversation
```

Do not build the canonical classifier from prefixes alone. Prefer explicit `chatType`, `threadType`, `productThreadType`, `isOneOnOne`, and other authoritative metadata; use ID patterns only as guarded fallbacks or diagnostics.

## How future investigations should be conducted

- Start from [Teams API research](teams-api-research.md), the captured Teams Web source/network archive and current repository code before searching for another implementation.
- Distinguish **observed network behavior**, **current Web source behavior**, **inference**, and **our implementation choice**.
- Keep browser-session success distinct from success with the application's app-owned MSAL authentication. Production capabilities must work without copying browser tokens/cookies.
- Use controlled marker strings when tracing searches or other read behavior so a captured request can be tied to one user action.
- Prefer read-only observations. Any write probe requires explicit authorization for the exact destination/content/file.
- Never persist tokens, cookies, authorization headers, sync cursors, private status notes or unrelated message bodies in research artifacts/fixtures.
- When a private API alternative no longer improves the product over an implemented local mechanism, mark it **Possibly abandoned**; conversely, keep server-native APIs when they enable a genuinely different capability such as broad heterogeneous discovery.
- Prefer a supported Microsoft Graph implementation over a private Teams endpoint when live parity is proven; do not keep both merely for compatibility. Keep private APIs where Graph is unavailable, permission-blocked, or materially less complete.
- Keep shared behavior in connector/service/formatter/archive layers so MCP and the local UI use one implementation.
- No further work is needed merely to recreate already delivered MCP registration, backup, profile-picture caching, empty-chat filtering or bounded export behavior.
- A separate Outlook client remains outside this application's completion scope; see [Outlook feasibility plan](outlook-client-feasibility-plan.md).

## Microsoft 365 search follow-up (updated 2026-10-12)

- Complete message histories for the two known transcript-bearing meeting chats each expose one
  embedded transcript URI reference with route shape `/drives/{drive}/items/{item}/{segment}/{segment}/media/transcripts/{transcript}/content`.
  This is reference-only evidence; no request, authorization result, or transcript response was
  observed. Next: compare it with a sanitized DevTools/Resource Saver capture from the loaded
  transcript viewer and map the two extra path segments.
- The bounded code-path and performance review is recorded in Teams API research,
  [Connector code-path audit and measurements](teams-api-research.md#connector-code-path-audit-and-measurements-2026-10-10).
  Small tests found one modest concurrent-read timing difference and mixed FTS results, with large
  common-query regressions and added database size. They do not justify a concurrency or indexing
  change. Any future optimization should use representative workload and throttling measurements,
  preserve coverage accounting, and remain separate from Graph-migration decisions.

### People/search/profile helper audit (2026-10-11)

- A targeted read of the supplied Teams Web bundle establishes source-level request/response
  construction for multiple People Picker and profile helpers, including the distinct
  `peopleProfileService` working-set route. The route inventory, body/parameter shapes, response
  transforms and unresolved boundaries are recorded in [Teams API research](teams-api-research.md#peoplesearchprofile-helpers).
- The supplied HAR captures Substrate `scenario=powerbar` requests with a People entity and one
  successful response containing a single People suggestion. That is a different surface from
  Middle Tier `/beta/users/searchV2` and the full Search v2 result endpoint. Field names are
  documented without retaining result values.
- The new Teams global-search HAR contains successful `fetch`, `fetchShortProfile`, and
  `fetchfeaturesettings` calls, plus profile-image hydration. The first two return a `value` array
  with observed profile fields; the feature-settings request/response shape is only partially
  mapped. This raises those helpers from source-only to browser-captured evidence; it does not
  establish free-form People Picker `search`/`searchV2`, `searchUsers`, external-user search, or
  People Profile working-set behavior. Keep those uncaptured helpers as research leads and do not
  add them to production code from source alone.
- The Teams Web source also identifies separate chat-list GraphQL connections and a Teams/Channels
  folder sorting helper, but does not connect that helper to recent-chat rendering. Exact sidebar
  recent ordering remains open; see the [dated source audit](teams-api-research.md#teams-web-chat-list-and-people-helper-source-audit-2026-10-11).

### Exact-message search-result navigation (2026-10-11 source audit)

The parseable mixed Search v2 response returned Message `Source` fields including
`InternetMessageId`, `ClientThreadId`, `ClientConversationId`, and `DateTimeSent`. The Web
normalizer uses the internet message id for its UI result id and composes the navigation reference
from reply-chain identity, client thread id, and timestamp. This identifies and dates the hit
without a page ordinal; `ThreadSubType` and `ParentMessageId` used by the source normalizer were not
top-level captured `Source` fields, so their extraction remains unclear. Matching the id to
Graph/Chat Service history and capturing the follow-up history request remain open.

The supplied Teams client bundle maps a Search result's primary navigation reference to a chat or
channel command. The chat command receives `replyChainId`, `messageId`, `dateTimeSent`, a generated
`searchSessionId`, and highlight text; the channel command receives channel/team identity,
`replyChainId`, `deepLinkId`/message identity, and highlight options. This explains how the client
can address an exact hit without a page ordinal. The HAR does not include a message-history request
or the click-triggered wire sequence. Mapping `dateTimeSent` to the existing Chat Service
`startTime` parameter remains an inference. See [API research](teams-api-research.md#search-result-activation-and-exact-message-navigation-source-2026-10-11-audit)
and the [cross-product endpoint/workflow ledger](https://github.com/lucasrhode95/lrh-outlook-connector/blob/main/docs/m365-search-findings.md).
