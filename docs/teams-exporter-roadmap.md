# Teams Exporter Roadmap

## Scope and evidence

This is the consolidated register of gaps between the current application and the intended product. Endpoint facts and captured payloads live in [Teams API research](teams-api-research.md); module responsibilities are in [Project architecture](project-architecture.md); usage is in [MCP server guide](teams-mcp-server.md).

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
| P2.2b Conversation search — MCP | **Partial** | `search_chats` should gain `refresh: bool = True` and default to refreshing catalog/roster state before searching. `refresh=False` remains an explicit offline/cache-only option. |
| P2.2c Scoped content/message search | **Done** | Date/chat-scoped remote preparation plus SQLite message search and participant indexing provide the deep, auditable search path. |
| P2.2d Broad Teams discovery via Substrate | **Pending** | Native Search v2 now has a concrete product use: heterogeneous broad discovery across messages/chats/files/teams/channels/etc. It should return pointers and honest limitations, not pretend every result is directly retrievable. |
| P2.3 Complete MCP content and continuation | **Done** | Bounded tools, continuations, large-message continuation, export snapshots, resource reads and tool-based export reads are implemented and tested. |
| P2.3a MCP history filter parity | **Pending** | Make `get_messages` and `export_chat` expose the same optional inclusive `since`/`until` plus latest-`N` `message_limit` semantics as the UI. `export_chat` already has these controls; `get_messages` still needs a total history limit distinct from response `page_size`. |
| P2.3b Media-inclusive export bundles | **Pending** | UI and MCP exports should accept `include_assets=false|true`. When enabled, materialize only assets referenced by the selected messages, rewrite those message references to local bundle filenames, and return self-contained ZIP packages. |
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

An explicit delegated `Chat.Read` request with the Microsoft Teams first-party client ID returns `AADSTS65002`: Microsoft has not preauthorized that Microsoft-owned client for the requested Graph permission. Treat private-chat Graph reads as unavailable under the current app-owned authentication model unless Microsoft changes that preauthorization in the future.

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

**Status: Pending. This is no longer considered redundant.**

The 2026-10-01 capture establishes a useful separate capability: Teams' top-bar/powerbar search can return heterogeneous pointers across categories such as messages, chats, files, teams/channels, people, events and apps.

That makes it valuable as a **broad-spectrum discovery channel** for difficult investigations. An agent could use it to learn that some potentially relevant artifact exists even when that artifact is outside the exporter's directly retrievable content.

Example desired behavior:

> "I found a file / channel / meeting / chat that looks relevant to this issue. I can retrieve some of these result types directly; for this one I only have a pointer, but it may be worth opening manually."

Implement this as a distinct MCP capability rather than changing scoped SQLite message search. A name such as `search_teams` or `broad_search` is preferable to overloading `search_messages`.

The result model should be heterogeneous but conservative, for example:

```text
kind
name/title
preview/snippet when supplied by Teams
conversation/thread/message/file identifiers when supplied
URL/deep-link when supplied
source/search category
whether this application knows how to retrieve/follow it directly
```

Requirements:

- acquire the needed Office/Substrate token through the app-owned auth flow;
- replay a controlled **known-hit** query to settle request body, pagination/refiners and result schema;
- return only fields the service actually supplied; do not invent accessibility;
- distinguish a pointer from a resource the connector can fetch;
- bound result count/response size and support continuation if the backend does;
- never replace scoped/local content search with this endpoint; it is an exploratory front door, not the durable archive/query engine.
## P2.3 — complete content through MCP

**Status: Done.**

The MCP layer provides bounded tool results with continuation, separate continuation for oversized individual message content, resumable preparation for history/search work, immutable export snapshots, standard MCP Resources, and `read_export` for clients that prefer tool-based reads.

Client-specific presentation of Resources can vary; that does not make the server implementation incomplete.

### P2.3a — MCP history filter parity

**Status: Pending. New change request.**

Both MCP history-reading paths should expose the same optional selection controls as the local export UI:

```text
since / start date    optional, inclusive
until / end date      optional, inclusive
message_limit         optional total latest-N bound
```

Keep the existing API naming (`since` / `until`) unless there is a strong reason to add aliases. All three filters are independent and optional.

`export_chat` already exposes `since`, `until`, and `message_limit`; preserve that behavior and add parity tests/documentation rather than redesigning it.

`get_messages` currently has `since` / `until` plus `page_size`. Add an optional **`message_limit` distinct from `page_size`**:

- `page_size` controls only how many messages can be returned in one MCP response;
- `message_limit` controls the total selected history window across all continuation pages;
- `message_limit=None` means no total count limit;
- a positive `message_limit=N` means select the **latest N messages that satisfy the date bounds**, counting backward from the newest qualifying message;
- with `until` set, count backward from the latest qualifying message at or before `until`;
- with both `since` and `until`, select only the inclusive range first, then apply latest-N semantics within that range;
- with only `message_limit`, return/traverse the latest N messages in the conversation;
- with no filters, preserve the current unbounded-history-with-continuation behavior.

Continuation must preserve the original selection window. For example, `message_limit=100` with `page_size=25` should allow at most four 25-message pages (subject to response-size constraints), never continue into the 101st older qualifying message. The cursor/state should therefore retain the chosen `since`, `until`, total limit, and how much of that bounded window has already been returned/fetched.

Continue returning each response page in the established chronological order. The **selection** is newest-backward; presentation order within a returned page can remain oldest-to-newest.

The count limit should apply after the normal conversation/date filtering and deduplication rules, so duplicates or out-of-range records do not consume the user's requested N messages. Deleted/tombstoned messages follow the normal retained-history policy and count as messages when included in the selected history.

Required tests:

- no filters;
- since only;
- until only;
- since + until;
- message limit only;
- since + message limit;
- until + message limit;
- since + until + message limit;
- `message_limit` larger than available history;
- `message_limit` spanning multiple MCP response pages;
- response-size truncation plus continuation without violating the total limit;
- dedupe/out-of-range rows not consuming the total limit;
- parity between `get_messages` selection and `export_chat` for the same conversation/filter tuple.


### P2.3b — media-inclusive self-contained export bundles

**Status: Pending. New change request.**

Both export surfaces — the local HTTP UI and MCP — should support an optional:

```text
include_assets = false | true
```

Default: `false`, preserving today's lightweight text-export behavior.

When `include_assets=true`, first apply the normal message-selection rules (`since`, `until`, `message_limit`, conversation selection). **Only assets referenced by those selected messages belong in the export.** Do not fetch unrelated chat media, avatars, old attachments outside the selected history window, or other account resources.

The export should then materialize each fetchable message asset using the authenticated resource-fetch layer and rewrite the corresponding reference in the TXT export from its protected/remote URL to the **local sibling filename** stored in the bundle.

Example:

```text
Remote/source representation:
https://tenant.sharepoint.com/.../architecture.png

Exported conversation reference:
architecture.png
```

The resulting TXT should not require Microsoft authentication to understand which local file belongs to the message.

#### Bundle layout

With assets disabled, preserve the current output rules.

With assets enabled, use this structure:

```text
1 conversation:
conversation.zip
├── conversation.txt
├── architecture.png
├── screenshot.jpg
└── report.pdf
```

For multiple conversations:

```text
teams-export.zip
├── conversation-a.zip
│   ├── conversation-a.txt
│   ├── image-a.png
│   └── report-a.pdf
├── conversation-b.zip
│   ├── conversation-b.txt
│   └── screenshot-b.jpg
└── conversation-c.zip
    └── conversation-c.txt
```

In other words:

```text
1 conversation + assets => ZIP containing TXT + sibling assets
N conversations + assets => outer ZIP containing one self-contained ZIP per conversation
```

Even if a selected conversation has no fetchable assets, when `include_assets=true` its per-conversation package should remain structurally consistent and contain at least the TXT.

The existing "concatenate conversations into one text file" option is incompatible with this media-bundle layout. When `include_assets=true`, disable or reject concatenate mode with a clear validation error rather than inventing ambiguous cross-conversation asset paths.

#### Asset naming and reference rewriting

- Prefer the human filename supplied by Teams/SharePoint/message metadata.
- Sanitize filenames for the local filesystem and ZIP format.
- Resolve collisions deterministically, for example `image.png`, `image-2.png`, `image-3.png`.
- The TXT must reference the **final collision-resolved local filename**, not the original remote URL.
- If the same upstream asset is referenced repeatedly in the selected history, fetch/store it once and reuse the same local filename.
- Never use an upstream URL as a ZIP path.
- Prevent `../`, absolute paths, reserved names, or other archive/path traversal.
- Do not include authentication tokens, signed URLs, cookies, or request headers in the TXT or ZIP metadata.

If an asset cannot be fetched, do not fail the entire conversation export by default. Keep the message and replace the unusable protected URL with an explicit local textual marker such as:

```text
[Asset unavailable: architecture.png]
```

The exact wording can be centralized in the formatter. The export should report failed/successful asset counts in its completion metadata.

#### Which resources count as assets

Initially treat resources explicitly attached/embedded by the **selected messages** as candidates, including where supported:

- ordinary Teams/SharePoint/OneDrive file attachments;
- inline images/media referenced by the message;
- AMS-backed message media.

Sender avatars/profile pictures are presentation metadata, not message assets, and should remain under N3 unless a later flag explicitly asks to package them.

#### Reuse the authenticated resource layer

Do not build a second downloading stack just for exports.

The N1/N3 authenticated resource layer should expose a common operation conceptually like:

```text
fetch_registered_resource(resource_descriptor) -> bytes/stream + content metadata
```

Normal UI/viewing can use that layer lazily through localhost/MCP resource routes.

A media-inclusive export uses the **same authenticated fetch adapters** but materializes the returned bytes directly into the export ZIP during export construction. No intermediate temp download directory is required beyond whatever bounded staging the ZIP writer itself needs.

#### Shared export architecture — one orchestrator for UI and MCP

Keep this DRY. The HTTP UI and MCP must **not** implement separate single/multi-chat or media/no-media export trees.

Both surfaces should adapt their inputs into the same application/service-level request, conceptually:

```text
ExportRequest(
    conversation_ids=[...],
    since=None,
    until=None,
    message_limit=None,
    include_assets=False,
    concatenate=False,
)
```

Then one shared orchestrator owns all branching:

```text
UI ------\
           -> export(request)
MCP -----/       |
                 +-- one conversation
                 |      -> export_single_chat(...)
                 |            +-- include_assets=false -> TXT artifact
                 |            +-- include_assets=true  -> ZIP(TXT + selected assets)
                 |
                 +-- multiple conversations
                        -> for each conversation:
                               export_single_chat(...)
                               -> TXT or per-chat ZIP
                        -> bundle returned per-chat artifacts in one outer ZIP
                        -> return outer ZIP
```

The important invariant is that **`export_single_chat(...)` is the only implementation of conversation extraction**. Multi-chat export is only orchestration around that primitive. Asset fetching, filename rewriting, message filtering, deletion markers, formatting, and selection semantics must not be duplicated in the UI path, MCP path, or batch path.

The shared result should be an artifact descriptor rather than UI/MCP-specific output, for example conceptually:

```text
ExportArtifact(
    path / stream handle,
    filename,
    content_type,
    byte_size,
    message_count,
    asset_counts,
)
```

The surface adapter decides only **how to deliver that artifact**:

- HTTP UI -> normal HTTP file download response;
- MCP -> MCP Resource/file artifact/reference suitable for text or binary content.

Do not make the shared exporter know about HTTP handlers, browser response objects, MCP tool schemas, or MCP protocol details.

##### Single-chat primitive

The single-chat builder receives one conversation plus the already-agreed optional filters:

```text
since
until
message_limit
include_assets
```

It returns:

```text
include_assets=false -> conversation.txt
include_assets=true  -> conversation.zip
                         conversation.txt
                         selected asset files...
```

The same primitive must be used when the request contains one conversation and when it is called repeatedly by the multi-chat orchestrator.

##### Multi-chat orchestration

For N > 1 conversations, call the single-chat primitive once per conversation and bundle its returned artifacts as-is:

```text
include_assets=false:
    teams-export.zip
        chat-a.txt
        chat-b.txt
        ...

include_assets=true:
    teams-export.zip
        chat-a.zip
            chat-a.txt
            assets...
        chat-b.zip
            chat-b.txt
            assets...
```

This preserves your desired rule:

```text
1 conversation + media -> ZIP -> files
N conversations + media -> outer ZIP -> one ZIP per conversation -> files
```

For N conversations with assets disabled, retain the simple current outer ZIP of TXT files rather than pointlessly wrapping each TXT in another ZIP.

##### UI adapter

Add an export checkbox such as:

```text
Include attachments/media
```

mapped to `include_assets=true` on the shared request.

The UI should submit one export request regardless of selected chat count. It must not loop over conversations in JavaScript or assemble archives in the browser. Progress should include asset discovery/fetch counts in addition to message progress, and the changed output type should be obvious before download.

##### MCP adapter

Expose the same export request semantics through MCP. Prefer a single export tool shape capable of one or many conversation IDs rather than maintaining a separate implementation of `export_chat` and `export_chats`. If backward-compatible tool naming is not desired for this actively developed project, the MCP API may be reshaped around the shared multi-ID request.

The MCP layer must not loop through public MCP calls to emulate batch export; it should call the same in-process orchestrator as the HTTP UI.

Do **not** require the MCP client to manually download each attachment or construct ZIPs.

For media-inclusive exports, expose the completed artifact as a binary/file MCP Resource or equivalent downloadable artifact reference. Avoid base64-encoding an entire potentially large ZIP inside ordinary tool JSON. Text-only exports may continue to support bounded text reads, but the canonical export artifact should still come from the shared exporter.

#### Selection parity

Asset collection must respect exactly the same selected message set as the textual export.

For example:

```text
since = 2026-09-01
until = 2026-09-30
message_limit = 100
include_assets = true
```

means:

1. select the latest 100 messages within the inclusive September range;
2. render only those messages;
3. discover/fetch only assets referenced by those 100 messages;
4. rewrite those references to the filenames bundled beside that conversation's TXT.

Do not let asset discovery expand the message/history scope.

#### Tests

Cover at least:

- one chat, assets disabled -> existing TXT behavior;
- one chat, assets enabled -> ZIP with TXT + assets;
- one chat, assets enabled but no assets -> ZIP with TXT only;
- multiple chats with assets -> outer ZIP containing one inner ZIP per chat;
- filters + `message_limit` selecting assets only from included messages;
- protected remote URL rewritten to local sibling filename;
- duplicate asset references downloaded once;
- duplicate filenames deconflicted and TXT references rewritten to the final names;
- unavailable asset leaves the message/export intact with an explicit marker;
- unsafe/path-traversal filenames sanitized;
- concatenate + assets rejected/disabled;
- no bearer token, signed URL, cookie, or authentication header leaks into export contents;
- MCP and HTTP UI use the same underlying bundle-builder/resource-fetch logic.


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
- HTTP must not need to rebuild a domain `ChatSummary` from its own JSON response records merely to call the exporter;
- send/history/export/search preparation should reuse the same resolver where practical.

Do not introduce a repository or abstraction layer merely for its own sake; a few well-named methods on `TeamsService` are enough.

### D1.2 — shared export request, job, progress and artifact contracts

P2.3b already defines the canonical export control flow and remains authoritative for packaging rules.

Make the surrounding export types reusable across HTTP and MCP as well, conceptually:

```text
ExportRequest
HistorySelection
ExportJob / ExportStatus
ExportProgress
ExportArtifact
```

The shared `ExportArtifact` should carry protocol-neutral information such as:

```text
filename
content_type
path / stream / artifact handle
byte_size
message_count
asset counts
```

Do **not** require HTTP and MCP to execute jobs identically.

It is acceptable for:

```text
HTTP -> background worker thread + polling/cancel endpoint
MCP  -> bounded/resumable tool calls
```

as long as both execution models ultimately drive the same shared export orchestration/single-chat builder and produce the same artifact contract.

The current HTTP `_build_download()` logic should not remain a second implementation of the business-level single-vs-multi export tree once P2.3b is implemented.

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

The resource descriptor should identify a validated known resource and the required fetch strategy without exposing reusable credentials.

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
| N1. Authenticated attachment/resource access | **Partial** | Build one authenticated resource-fetch layer. Normal viewing can use it lazily through localhost/MCP resource routes; P2.3b media-inclusive exports should reuse the same adapters to materialize selected assets into self-contained ZIP bundles. |
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
