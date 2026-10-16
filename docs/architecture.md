# Teams Connector — Architecture

**Inputs:** [Requirements](requirements.md) · [API research](api-research.md) · **Open work:** [roadmap](roadmap.md)

The repository contains one current application: the `teams-connector` CLI and the
`teams_connector` Python package. MCP and the loopback web UI call the same domain services.

![Teams connector architecture](diagrams/architecture.svg)

The classes and who calls whom, in more detail: [code map](diagrams/code-map.svg). The diagrams give the
overview; this document records the rules and contracts they cannot show.

## Code structure

| Layer | Responsibility |
|---|---|
| `src/teams_connector/__main__.py` | Canonical CLI (`teams-connector`) for shared sign-in, profile status, MCP, and UI. Loads MCP and web dependencies only for those commands. |
| `surfaces/mcp_main.py` | Typed MCP tools over the service layer. Keeps stdio free of logs; `send_message` and `delete_messages` are the only Teams write tools. |
| `surfaces/web/` | Starlette app, static local export page, and loopback UI launcher. Uses a per-run session token, Host validation, and an idle timeout. |
| `service/` | Domain behavior for the catalog, message history, exports, attachment downloads, profile photos, and sending. MCP and UI share these operations. |
| `remote/ports.py` | Interfaces used by services; service code does not depend on concrete HTTP adapters. |
| `remote/transport.py` | The only general HTTP client. Applies the allowlist, authentication, one-time 401 renewal, bounded idempotent retries, response limits, error mapping, and credential-free download redirects. |
| `remote/chat_service.py`, `remote/csa.py` | Private Teams service adapters for the chat catalog, chat history, chat members, and CSA updates. Their endpoints are undocumented and described as private in the research record. |
| `remote/graph.py`, `remote/graph_teams.py` | Microsoft Graph paging, batching, channel history, people/photos, file download, and the explicitly authorized message-send request. |
| `remote/substrate_search.py` | Search v2 message search and named-chat matches, and Teams Web's search-box people suggestions (`substrate.office.com`), all with the `m365.search` token. |
| `remote/substrate_summaries.py` | Teams' AI meeting summary from the search recommendations route (`substrate.office.com`, `MeetingCatchUp`), with the `m365.search` token; only `service/summaries.py` calls it. |
| `service/summaries.py` | The one place that reads a meeting's AI summary for exports, `get_messages` and transcript search, each only for the meetings its result returns. Nothing is cached. |
| `remote/presence.py`, `service/presence.py` | Presence of a one-on-one peer or signed-in user, read at the presence service AuthZ names (`presenceUPS`); the service keeps a result for a minute and fails open. |
| `remote/inline_images.py`, `remote/image_readers.py` | Pasted images: the marker name and attachment record of each image in a message (`inline_images.py`, no network); the AMS full-size and Graph hosted-copy readers (`image_readers.py`). |
| `remote/*_mapping.py` and `remote/teams_html.py` | Convert wire responses into domain records and readable message text. Wire-format field names remain inside this layer. |
| `store/db.py` | Account-bound SQLite store for the chat catalog and account metadata only. Holds no message content and no tokens. |
| `service/export/` | Text/JSONL formatting and safe output packaging. Exports read Teams page by page and spool to a temporary file, use exclusive temporary paths, and publish only after completion. |
| `service/passive_html.py` | The passive check for HTML sends: refuses active content, embedded media, declarations and unfinished markup. Its one rewrite points an `<img>` that names an attached image at the hosted copy. |
| `service/localfiles.py` | Per-user output directories, retention cleanup, and collision-safe file creation. |
| `src/teams_connector/config.py` | Product CLI/data names and fixed public service URLs. Tenant authority, client IDs, token profiles, scopes, and shared-cache configuration live in `lrh-m365-auth`. |

The service layer receives adapters through the ports and is responsible for decisions and coverage.
Only the `remote/` layer knows Microsoft request bodies, headers, endpoints, paging links, and IDs.
The two surfaces validate external inputs before calling services; internal service routines rely
on their typed contracts.

Shared rules live once in the service and domain layers: conversation lookup, history selection,
export packaging and domain errors. Each surface keeps its own protocol work: the UI parses calendar
dates with the browser's UTC offset while MCP takes ISO 8601 timestamps; the UI guards Host headers
and a session token while MCP runs over stdio; the UI streams downloads while MCP returns local
paths; each maps domain errors to its own responses.

## Services, ports and adapters

Each surface calls a service. A service depends only on the ports it needs, and `bootstrap.py` wires an adapter to
each port-typed role. A port can have several adapters in different roles: `HistoryReader` reads chats (`ChatService`)
and channels (`GraphChannels`), `ImageReader` reads AMS images and Graph hosted copies. The table maps each MCP tool and UI route to the service that answers it, the ports it reads or writes through,
and the adapters behind them. The split of backends (which private or public service backs each capability) is in
[requirements §4](requirements.md#4-backend-choice).

| Surface | Tool or route | Service | Ports | Adapters |
|---|---|---|---|---|
| MCP | `auth_status` | `TokenProvider` (shared auth package) | none | `TokenProvider` |
| MCP | `list_chats` | `Catalog.list` (a refresh reads the self chat's newest message too) | `ChatCatalog`, `ChannelDirectory`, `HistoryReader` | `CsaCatalog`, `GraphChannels`, `ChatService` |
| MCP | `search_chats` | `Catalog.search` (with `refresh`, after a catalog refresh as `list_chats`) | `ChatMemberSearch` (named chats and people), and the refresh's ports | `SubstrateChatSearch`, and the refresh's adapters |
| MCP | `get_messages` | `History.get_messages` | `HistoryReader` (chats and channels), `MeetingSummaryReader` | `ChatService`, `GraphChannels`, `SubstrateMeetingSummaries` |
| MCP | `search_messages` | `Search.search_messages` | `MessageSearchReader` | `SubstrateSearch` |
| MCP | `search_transcripts` | `TranscriptSearch.search` | `HistoryReader` (recording messages), `TranscriptReader`, `VideoSearch` (across meetings), `Catalog.active_since`, `MeetingSummaryReader` | `ChatService`, `GraphTranscripts`, `GraphVideoSearch`, `SubstrateMeetingSummaries` |
| MCP | `export_chats` | `Exports.export` | `HistoryReader` (`History.selection`), `FileReader` and `ImageReader` (`Files.save`), `MeetingSummaryReader` | `ChatService`, `GraphChannels`, `GraphFiles`, `AmsImages`, `GraphHostedImages`, `SubstrateMeetingSummaries` |
| MCP | `download_attachment` | `Files.download_attachments` (`History.resolve`) | `ChatHistoryReader` (finds the message, and a channel reply's thread), `ChannelHistoryReader`, `FileReader`, `ImageReader` | `ChatService`, `GraphChannels`, `GraphFiles`, `AmsImages`, `GraphHostedImages` |
| MCP | `send_message` | `Sender.send` (and `Catalog.recipient`) | `ChatWriter`, `ChatCatalog` (chats), `ChannelDirectory` (channels), `MemberReader` and `FileSharer` (files) | `GraphWriter`, `CsaCatalog`, `GraphChannels`, `ChatService`, `GraphFileSharer` |
| MCP | `delete_messages` | `Deleter.delete` (and `Catalog.recipient`, `History.message`) | `MessageDeleter`, `ChatHistoryReader`, `ChatCatalog` | `ChatService`, `CsaCatalog` |
| MCP | `get_presence` | `Presences.for_chat` | `PresenceReader` | `TeamsPresence` |
| UI | `GET /api/status` | token status | none | `TokenProvider` |
| UI | `GET /api/chats` | `Catalog.list` (or `ensure`) | as `list_chats` | as `list_chats` |
| UI | `GET /api/photos/{user_id}` | `Photos.photo` | `People` | `GraphPeople` |
| UI | `GET /api/presence/me`, `GET /api/presence/chat` | `Presences.for_self` / `Presences.for_chat` | active account id or catalog conversation | `TeamsPresence` |
| UI | `POST /api/preview` and its job routes | `Exports.preview` in the single cancellable preview slot | `HistoryReader` (`History.selection`), `MeetingSummaryReader`, `FileReader` (meeting notes only) | `ChatService`, `GraphChannels`, `SubstrateMeetingSummaries`, `GraphFiles` (text conversion only) |
| UI | `POST /api/exports` and its job routes | `Exports.export` in a background job | as `export_chats` | as `export_chats` |

## Authentication and account boundaries

The shared `lrh-m365-auth` package supplies six named profiles shared by the three connectors:

| Profile | Used for |
|---|---|
| `teams.spaces` | Teams AuthZ/session exchange and Teams account identity. |
| `teams.csa` | Teams conversation catalog updates. |
| `teams.graph` | Graph teams, channels, people, and message operations. |
| `outlook.graph` | Outlook and validated OneDrive/SharePoint Graph capabilities. |
| `outlook.api` | Outlook Web operations owned by the Outlook connector. |
| `m365.search` | Search v2 for online Teams message discovery (and Outlook calendar search), and the AI meeting summaries that exports read from `substrate.office.com`. |

The auth package owns MSAL state and the encrypted cross-process cache; it does not issue Teams or
Graph requests or exchange a Teams Skype token. Each connector keeps its product wire logic. An
explicit `auth` command from any connector bootstraps the common profile catalog, silently reuses
accepted profiles, and requests approval only when needed. Microsoft can ask for more than one
approval because the public clients and resource audiences differ.

One active Microsoft account is bound to the shared cache. Signing out and choosing another account
invalidates the shared profile state; long-running processes refresh their account binding on the
next token request. The Teams composition root checks the current identity at each service boundary
and builds an account-specific Store before serving account data. Offline status reports cached
OAuth client credentials separately from resource-specific access-token expiry; profiles sharing a
client can show the same credential state. Neither fact proves that a token can be acquired or that a
particular service or conversation will authorize the operation.

MCP, ordinary UI use, and status reads do not launch interactive login. Encrypted storage is
mandatory by default. The explicitly selected `--unsecure` mode uses a separate plaintext development
cache and must never be used as an implicit fallback.

## Requests, caching, and storage

The MCP server is a stdio process scoped to one client session. It performs no network calls until a
tool needs them. The UI runs only while explicitly started, binds to loopback, and closes on exit,
Ctrl+C, or its idle timeout. There is no recurring sync task, watcher, remote listener, or background
refresh process.
Each authenticated UI API request, including presence reads and preview-status polling, counts as
activity for the idle timeout.

The chat catalog may be reused for up to ten minutes. Explicit refresh waits for the remote read.
The local store holds the chat and channel catalog (names, kinds, last activity, photo ids, and the
channels that Graph's team listing omits), the catalog's sync token and refresh time, and the owning
account. It holds no message content and no chat member lists. Teams is the record: every history read,
search, export and download reads Teams again ([requirements §6](requirements.md#6-local-data-decided-2026-10-14)).

The database lives below `<Teams data directory>/accounts/<account fingerprint>/teams.sqlite3`.
Owner metadata is verified on open; an account mismatch raises an error instead of reusing another
account's store. SQLite WAL, busy timeouts, per-operation connections, and short transactions allow the
MCP and UI processes to use the same account store. The store is not encrypted by this application.

A damaged store is moved, without deletion, into a `corrupt-<timestamp>` folder, and a new one is built
from Teams.

Exports and downloaded attachments are written below the Teams data directory and are removed after seven
days when their folder is next used, subfolders included. Downloads are cached per source
(`downloads/<source>/<key>/`: AMS and Graph images by object id, data images by digest, shared files by drive
item and version), so a file already there is returned without fetching its content. A failed or cancelled
export publishes nothing.

## HTTP and safety rules

The shared transport accepts HTTPS only and checks Graph and Teams hosts before sending credentials.
Regional Chat Service URLs come from the Teams AuthZ response and are checked against the Teams
domain allowlist. Read-only requests retry a bounded set of transient failures; the retry delay
honors `Retry-After` within its limit. Writes are retried only when the failure proves nothing was
applied: the connection never opened, or the service answered 429 or 503 (requirements §4). A write sent
without a complete answer, or answered 504, is never resent: it raises `WriteOutcomeUnknown` so an agent
can inspect the target before taking another action.

Graph file-download endpoints can return a preauthenticated SharePoint redirect. The transport
allows one HTTPS redirect to a SharePoint download host and does not forward the Graph bearer token
to that host. Likewise, `Transport.upload_session` calls a Graph upload session's preauthenticated
`uploadUrl` on `*.sharepoint.com` with no credential, and logs no part of its URL. Downloads have caller-supplied byte limits and are streamed to temporary files before
publication. URLs, query strings, bodies, authorization headers, message content, and search terms
are omitted from operational logs; credential-like values are redacted as another safeguard.

Pasted images are fetched only from the URL their message names: AMS full-size views from `*.asm.skype.com`
or `*.asyncgw.teams.microsoft.com` (the two AMS hosts AuthZ names) with the session's Skype token,
and Graph's hosted copies of channel images with the Graph token. Nothing is downloaded while a message is read.

The local UI rejects non-local Host headers and requires a random per-process token on API calls.
It uses a restrictive Content Security Policy and renders message content as text. The browser page
does not receive a Microsoft token.

## Search behavior and boundaries

`search_messages` is the one Teams message search. It sends one Microsoft Search v2 query on the shared
Microsoft Search profile for each page. Search reads no history and writes nothing: each hit is a
pointer, and `get_messages` reads the history around it. Search v2 matches whole words and word prefixes,
and a quoted phrase matches its words in order; it does not match the middle of a word. Agents decide
whether to fetch surrounding history or request another page.

## Logging

The MCP process logs to a per-process daily-rotated file, keeping stdout for protocol traffic. Log
files are under the Teams data directory's `logs/` folder. UI logs also go to stdout. Operational
records include timing, status, and counts, not message bodies, search text, chat names, IDs, tokens,
or authorization headers. Set `TEAMS_CONNECTOR_LOG_LEVEL=DEBUG` to enable safe request and paging
diagnostics. httpx and httpcore stay at WARNING whatever the level: at INFO httpx logs every full request
URL, and these URLs carry conversation and team ids, sharing links (Graph share ids encode them) and
pre-authenticated upload URLs. The transport's own debug line records the method, the path with ids
replaced, the status and the time instead.

## MCP tools

### Find conversations and read messages

1. Call `list_chats` to browse current chats and channels. It returns up to 50 items by default,
   supports `limit` up to 200 and opaque continuation `cursor`, and can filter by `kind`. A cached
   catalog up to ten minutes old may answer unless `refresh=true`. `coverage.source` identifies
   whether the response used the local catalog or a refresh.
2. Use `search_chats` to match a conversation name. With `refresh=true` (the default) it also
   asks Teams, for a plain name or email, for the named group and meeting chats with a matching member
   (up to 100, read page by page) and for the people Teams suggests (Teams Web's search-box
   suggestions); a one-on-one whose id names a suggested person matches. Each match lists the people
   that matched, so an email, or a name written in another order, finds the one-on-one. Teams is asked
   once per search: the cursor carries its matches. Read the coverage notes: a query with query syntax
   is not sent to Teams, a cut answer is reported, and an older named chat whose member is not in its
   title and has no recent activity is not covered (Teams' chat search covers recent chats only; the
   catalog keeps no member lists).
3. Call `get_messages` with the selected `conversation_id`. Pages are newest first; records within
   each page are oldest first. `since` and `until` are inclusive. `message_limit` caps the latest
   matching messages, while `page_size` is the most messages a page holds. A page also holds whole
   messages up to `max_chars` (at least one), so no message text is cut; follow `cursor` to get the
   older messages. A channel's history comes as threads (a post with its replies) by latest activity,
   each thread in time order, as Graph pages them: a thread with a new reply comes before newer posts
   of quieter threads, and `message_limit` keeps the messages of the most recently active threads.

Example arguments for a bounded history read:

```json
{
  "conversation_id": "19:sample@thread.v2",
  "since": "2026-08-01T00:00:00Z",
  "until": "2026-08-31T23:59:59Z",
  "page_size": 25,
  "message_limit": 100
}
```

### Presence

`get_presence` reads the availability, activity, device type and last-active time of the other person in a
one-on-one chat. The local UI also reads the signed-in user's presence for the account avatar and visible
one-to-one rows for peer badges and last-seen subtitles. The UI routes resolve identities on the server:
the own-presence route takes the active account id, and the peer route takes a conversation id checked by
`Catalog`; the page cannot ask for an arbitrary person's presence. Reads use Unified Presence where AuthZ
names it for the account (`presenceUPS`, a `teams.microsoft.com/ups/<region>` route, as Teams Web does) with
the `teams.spaces` bearer, and share a one-minute process cache. A failed read returns no presence and a
reason. Other fields Teams returns (calendar, work location, capabilities) are not reported.

### Message search

`search_messages` takes `terms`: one to 25 phrases, each at most 200 characters, with no double quotes and no
control characters. A message matches when any term matches. Each term is sent as one quoted phrase, so it
is never split on its spaces and never read as query syntax. Search v2 matches the words of a phrase in
order, so `budget review` needs both words in that order, and `budget` also finds `budgets`.

The connector builds one Search v2 query from the terms and the optional scope and dates:

- `conversation_ids` (up to 100, from `list_chats` or `search_chats`) become `clientthreadid:` clauses,
  joined with OR;
- `since` and `until` become `sent >=` and `sent <=` clauses, written to the millisecond in UTC; both
  bounds are inclusive;
- soft-deleted messages are always left out (`NOT (isClientSoftDeleted:TRUE)`);
- the terms form one OR group of quoted phrases.

The whole query is limited to 1,000 characters; a longer one is refused with a request to use fewer terms or
conversations. Without a scope, the search covers every chat and channel the signed-in user can read.

Each call returns one page of up to 25 hits (`limit`). A hit carries `message_id`, the history id that
`get_messages` reads (the hit's `InternetMessageId`, verified 2026-10-14); `conversation_id` and
`conversation_name` only when the hit's conversation matches a known chat exactly; `sent_at`, `sender`,
`subject` and a `preview`. A hit without a `message_id` is reported in the coverage notes.

`coverage` reports `complete` (the result set was interpreted and no further page exists), `offset`,
`page_size`, `more_results_available`, and `total`, Search v2's count for the query. `total` is approximate and
can move between pages. A search with no hits is a confirmed empty page when Search v2 reports `Total: 0`;
a missing result set is never reported as zero (live check 2026-10-15). Follow `cursor` only while more
matches are useful. A cursor carries the query and paging, so its terms, scope and dates are not repeated.
Search v2 refuses pages that start beyond its offset limit; the connector stops with a note at that
point.

To read around a hit, call `get_messages` with its `conversation_id` (when present) and a bounded
`since`/`until` window around `sent_at`.

Example request for one conversation and a month:

```json
{
  "terms": ["budget review", "forecast"],
  "conversation_ids": ["19:sample@thread.v2"],
  "since": "2026-08-01T00:00:00Z",
  "until": "2026-08-31T23:59:59Z",
  "limit": 25
}
```

This operation does not discover recap or transcript text; those capabilities remain separate.

### Exports

`export_chats` writes `.txt` by default or `.jsonl` when requested, with inclusive date bounds and an optional
latest-message limit per conversation. Each conversation becomes one text or JSONL file, named after the
conversation and part of its id (with a counter if two still clash). In a text file, a message with no sender reads Teams when Teams posted it (an event or a meeting card) and Unknown sender otherwise. A single conversation with no
attachment saved (none referenced, `include_attachments=false`, or every download failed) downloads as that
file. Anything else is one ZIP, named after the conversation or `teams-conversations.zip` for several: each
file at its root and its saved attachments in a folder named after it, with no ZIP inside it. A
conversation retrieves at most 200 files, 100 MiB per file and 250 MiB in total. Its files are fetched
four at a time; the limits are then applied in the order the text names them, so the result does not
depend on which download finishes first. Later references and failed downloads stay in the text as
`[Attachment unavailable: …]`. Meeting notes go in the text instead, after their message (see below). The
result contains a local path; use the MCP client's local file-reading capability to inspect it.

An export reads each conversation from Teams page by page, newest first, and spools the pages to a temporary
file; the text is then written oldest first from those pages. The temporary folder is removed when the export
ends, so a failed export publishes nothing.

The local UI's `/api/preview` uses a separate cancellable job slot and the same text formatter. It reads no
binary attachments or transcript files; meeting summaries and Loop notes are text enrichment. An initial
preview reads at most the newest 200 messages and 1 MiB of text, reports when the excerpt may be incomplete,
and keeps chronological order. **View entire range** removes only those preview caps; the selected date
range and message limit still apply. Preview spools are temporary and removed on success, failure or
cancellation. The browser keeps at most ten completed results in memory for 60 seconds; no message text is
stored in the catalog database or browser storage.

`download_attachment` saves the files a message names: its shared files and its pasted images. Pass exactly
one of `message_id`, or `sent_at` (the time exactly as the TXT export prints it). `names` selects files by
their markers; all of them are saved when it is omitted. The message is read again from Teams: by id, or by
time through Chat Service, and a channel message is then read through Graph, as `get_messages` reads it, so
the names match the text; a channel reply is read through its thread, which Chat Service names. Several
messages at the same time are refused with their ids. A file that cannot be fetched is listed under
`unavailable` with its reason while the others are saved; a call that saves none fails. A shared SharePoint
page reads as `[Page: name] link`: it is opened, not downloaded. A meeting recording reads as
`[Recording: title]` and its OneDrive or SharePoint link; recordings are not downloaded. Each file is fetched
once into the download cache (see Storage), and the tool returns the local paths. General OneDrive and
SharePoint discovery belongs to the SharePoint connector. Email attachments belong to Outlook.

**Meeting notes.** When a meeting ends, its "Facilitator" app posts a link to the meeting's Loop notes
page ([research](api-research.md#loop-pages-and-meeting-notes)). A message from an allowlisted sender (`config.NOTES_SENDER_IDS`, the meeting's Facilitator app)
and whose HTML has a `FluidAutoEmbedLink` anchor to a `/:fl:/` link
reads as `[Meeting notes] <link>` instead of the app's sentence, and carries a `loop` attachment named
`Meeting notes.txt`; reading fetches nothing. The page is read only for such messages, through Graph's HTML conversion, which may redirect once to a regional media host (`config.notes_media_hosts()`, set per deployment by `TEAMS_CONNECTOR_NOTES_MEDIA_HOSTS`), followed without credentials. Any other message with such a link keeps its text. The `include_summaries` flag governs these notes too: without it, the message keeps its own text, no attachment is made, and no page is read.

Exports (with attachments) and `download_attachment` read the page with one Graph
`GET /shares/{share}/driveItem/content?format=html`, and `remote/loop_notes.py` keeps the agenda and goal,
the notes under their headings, and each task with its assignee and due date; the AI disclaimer, empty
paragraphs, checkboxes and "Task list category" labels are dropped. An export reads each page once,
within the conversation's attachment limits (200 items, 250 MiB), which its files share, and writes the notes after the message;
`download_attachment` saves them as a text file. The page is live, so the text starts with the time it was
read and says later edits are not included, and the page is read again on every call rather than served
from the download cache. A page Graph will not convert (no access, deleted, a conversion error) is
reported as an unavailable attachment, and the message text stays. Teams Web also checks the page's
`opStream/capabilities` (`canExport`, `canExtract`); the connector does not, by design
([requirements](requirements.md#8-history-exports-sending-and-deleting)): what the user can read in Teams
they can export.

**Meeting transcripts.** A recording's message card names the recording's drive item and, when Teams made a
transcript, the transcript (`remote/recording_cards.py`; no request is made to read the card). Its marker is
`[Transcript: title]`, and it carries a `transcript` attachment named by the title, with the recording it
belongs to kept out of serialization. Exports and `download_attachment` fetch the default transcript with
`GET …/media/transcripts/{id}/content?format=json` (`remote/graph_transcripts.py`, parsed by
`remote/transcript_json.py`) and write it as text (`service/transcript_text.py`): a header, then utterances grouped
by speaker with their times. An export writes it into the `transcripts/` folder beside the conversation's file.
`search_transcripts` (`service/transcripts.py`) has two paths. With conversation ids it reads their recording
messages and searches each transcript. Without them it runs Graph's `driveItem` search for the terms over
`filetype:mp4`, then keeps a video only when a meeting chat holds a recording card naming its drive item: it
reads the chats active from an hour before the video's creation, quietest first, at most 25 per video, and
counts videos it cannot place. The result is never complete across meetings.

**AI summaries.** The flag `include_summaries` (on by default) governs exports, `get_messages` and
`search_transcripts`. Each reads Teams' AI summary through `service/summaries.py` and the adapter in
`remote/substrate_summaries.py` (`MeetingSummaryReader`), labeled as AI-generated and possibly inaccurate. The request
names the meeting by an entity id built from the recording's drive id (api-research, "Meeting summary"). Teams sends the summary as a JSON document of topics, which `readable_summary` turns into text. A read happens
only for a meeting the call returns, once per meeting, and nothing is cached. An export puts the summary after the
transcript's marker; with `include_transcripts=false`, the marker reads `[Transcript not included: title]` so the
summary stays in the text. `get_messages` does the same after the marker, counts the summary toward `max_chars` and
keeps the choice in its cursor. `search_transcripts` lists each matched meeting's summary once in `summaries`; a
summary that cannot be read leaves the matches and the completeness unchanged. A meeting without a summary keeps its
marker alone; a failed read is marked as unavailable, and the call goes on.

### Send a message

`send_message` is one of the two Teams write tools, with `delete_messages`. The user must explicitly
authorize the recipient and the exact content, files and images. Pass the current conversation name from
`list_chats` as `expected_chat_name`; the service refreshes only that recipient (a chat's delta, or a
channel's name from its team), then checks the name before sending. Pass exactly one of `text` (plain
text; HTML characters are escaped) or `html` (HTML sent as written, after a passive check that refuses
scripts, frames, form controls, event-handler attributes, `javascript:` and `vbscript:` URLs, embedded
media, declarations and CDATA sections, and unfinished markup); with files or images both may be omitted.
The message is never resent after an uncertain outcome: a network failure after the request may leave the
result uncertain, so inspect the conversation before deciding whether another send is needed. A search result or request to
analyze a conversation does not authorize a send.

Files and images go to one-on-one and group chats and the self chat, and may share a message. Every
argument is checked before the first request.

`images` sends local PNG or JPEG files inline, as Graph `hostedContents` in the same request
([research](api-research.md#sending-files)). Teams shows only images it hosts, so an `<img>` in `html` is
allowed only when its `src` is the file name of one of `images`; the service rebuilds that tag to point at
`../hostedContents/N/$value`, keeping only `alt`, `title`, `width`, `height` and `style`. Any other `<img>`
is refused, so remote images stay blocked. Images the HTML does not name, and every image sent with
`text`, follow the text, one paragraph each. A file that is not PNG or JPEG (by its content) is refused,
and so are images that would push the request past about 4 MiB (base64 adds a third, so about 3 MB of
images), counting the body and a reserve for each file's reference; the error suggests `files` instead.

`files` attaches up to 10 local files, by absolute path, each up to `config.MAX_SEND_FILE_BYTES` (2 GiB by
default). The service does what Teams Web does, through Graph ([research](api-research.md#sending-files)):

1. Upload each file to `config.CHAT_FILES_FOLDER` in the sender's OneDrive. A name clash is renamed. A
   file under 250 MiB goes up in one `PUT`. A larger one uses an upload session: `createUploadSession`,
   then 10 MiB chunks read from disk and `PUT` to the session's `uploadUrl`, without credentials.
2. Share each file with the chat's other members (listed from Chat Service), without an email. The access
   follows `config.SHARED_FILE_ACCESS`: the tenant's default link (`chat_default`) or direct grants
   (`direct`). The self chat has nobody to share with, so this step is skipped there.
3. Post the message with one `reference` attachment per file.

Each request follows the transport's write rule (retried only on a 429, a 503 or a connection that never
opened). After a chunk that still fails, the uploader reads the session's `nextExpectedRanges` and resends
only the missing bytes, at most `graph_teams.UPLOAD_RESUMES` (3) times per file; then it cancels the
session (`DELETE uploadUrl`) and the send fails. If the chunk that closes the file fails and the session's
state cannot be read, the file may have been uploaded, so the outcome is reported as unknown. The message
is posted only after every upload and share has succeeded; a share that fails for some members (Graph's
`207 Multi-Status`) counts as failed. When the send fails, the files it uploaded are deleted again
(`DELETE /drives/{drive}/items/{item}`, to the OneDrive recycle bin), except when the post's outcome is
unknown, since the message may reference them. The error says which files were deleted, which are still
in the sender's OneDrive and why, and which upload's outcome is unknown. A member without an object id in the directory,
such as a federated user, makes the send fail before anything is uploaded. The result lists each file's
name, as renamed, and its link.

### Delete messages

`delete_messages` soft-deletes a set of the user's own messages in one chat (not a channel), by the ids
`get_messages` returns: up to 100 per call. The agent first lists the set (chat, count, first and last
message) and gets the user's explicit confirmation of the whole set; the connector cannot verify that,
so the rule lives in the tool description and the MCP instructions. The service checks
`expected_chat_name` against the chat's live name, as a send does, then for each id in order:

1. Reads the message again (`History.message`). A message it cannot read, someone else's, a system event
   or an already deleted message is skipped with the reason.
2. Sends Chat Service `DELETE …/messages/{id}?behavior=softDelete` with the Skype token
   ([research](api-research.md#writes-not-used-by-the-connector)). Teams Web's Delete does the same, and
   it can be undone in Teams.

A refused delete is skipped and the rest continue; an ambiguous one (no response, or HTTP 504) is listed
as uncertain and never resent. If sign-in fails after some deletes, the rest are skipped as not
attempted, so the result still shows what happened. The result lists `deleted` (each message's time and
the names of the files it shared, which stay in the sender's OneDrive, still shared with the chat),
`skipped` and `uncertain`. `get_messages` then shows a deleted message as `is_deleted` with no text.
