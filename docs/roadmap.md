# Teams Connector Roadmap

Open bugs, improvements, enhancements and validation only. What the connector must do is in the
[requirements](requirements.md); how it is built is in the [architecture](architecture.md); dated
evidence is in the [research record](api-research.md). Delete an item once it is done. Codes are
never reused.

Snapshot **2026-10-15**.

## Bugs

Write each bug as **Seen** (a synthetic example: the call and what came back), **Expected**,
**Cause** and **Fix**.

### B2 — Online search cannot confirm zero hits

**Seen:** `search_remote_messages` for `zzqxv7npl` returns no items, `coverage.complete: false` and
"Search returned no interpretable Message result set; zero hits are not confirmed."

**Expected:** `coverage.complete: true` with zero items.

**Cause:** Search v2 answers an empty search with a result set holding `Total: 0`,
`MoreResultsAvailable: false` and no `Results` key
([research](api-research.md#search-v2-message-search)). The adapter counts a result set as found
only when `Results` is a list (`_message_rows` in
[`substrate_search.py`](../src/teams_connector/remote/substrate_search.py)).

**Fix:** treat a result set that reports `Total: 0` as a confirmed empty page.

### B3 — Meeting and membership events come back as raw markup

**Seen:** `get_messages` on a meeting chat returns, for the meeting that ended,
`text: "<ended/><partlist alt=\"\" count=\"3\"><part identity=\"8:orgid:…\"><name>…</name><displayName>Alex Kim</displayName><duration>2502</duration></part>…<meetingDetails>…"`
(about 1,800 characters, cut and `text_truncated`), `system_event: "Event/Call"`. A member joining
returns its JSON: `{"eventtime":…,"initiator":"8:orgid:…","members":[{"id":"8:orgid:…","friendlyname":"Sam Lee",…}]}`.
In one meeting chat, 8 of 15 consecutive messages were such records.

**Expected:** each event shown as one short line with only what Teams shows, in `get_messages` and in
exports alike:

```text
[Meeting started: Weekly sync]
[Meeting ended · 41m 42s · Alex Kim, Sam Lee, Jo Park]
[Sam Lee joined]
[Sam Lee left]
```

Organizer addresses, iCal ids, transcript and recap flags and identity strings are left out. An event
type without a known rendering becomes `[<type>]`, never its raw content.

**Cause:** `to_message` in
[`chat_service_mapping.py`](../src/teams_connector/remote/chat_service_mapping.py) renders every
record's `content` through the HTML renderer; event records carry XML or JSON, not message HTML
([research](api-research.md#meeting-and-membership-events)).

**Fix:** render `Event/Call` (started; ended with duration and participants) and
`ThreadActivity/MemberJoined`, `MemberLeft`, `AddMember` and `TopicUpdate` from their fields; keep
`system_event` as the type.

## Improvements

Working behavior that could be cheaper, simpler or better informed.

### D2 — Remove the message archive

Decided 2026-10-14: Teams is the record and the connector keeps no message content
([requirements §6](requirements.md#6-local-data-decided-2026-10-14)). What replaces each use of the
archive:

| Today | Replacement |
|---|---|
| `search_messages`: fetch a scope, match locally with `match_mode` (`exact`, `substring`, `all_terms`) | One Search v2 tool replacing both `search_messages` and `search_remote_messages` (P2.2d). `match_mode` goes: Search v2 matches words, word prefixes and phrases, so local string modes have no meaning there. |
| `search_chats` participant matches from indexed rosters | P2.2b |
| `get_messages` merges remote pages with retained ones | Read Teams only; drop the archive merge and its paging floor |
| `get_message_content` reads the archived message | Removed by R2 |
| `download_attachment` reads the archived message | Re-read the one message from Chat Service, by id or by time (N1) |
| Exports stream from SQLite | Stream through the export's own temporary file |
| `sync_archive`, `archive_status`, the `backup` command | Remove |
| Catalog, sync token, untitled-chat names, omitted channels, photo ids | Keep, in a small account-bound cache with no message text |

Then reset local state; no migration. Update the README, architecture, MCP server instructions and
tests in the same change.

### D3 — Organize the documentation by service

The code already separates the services: services depend only on the capability ports in
[`remote/ports.py`](../src/teams_connector/remote/ports.py) (`ChatCatalog`, `HistoryReader`,
`ChatWriter`, `FileReader`, `MessageSearchReader` and others), one adapter per service implements
them, and only `bootstrap.py` wires the two. That is what let sending move from Chat Service to Graph
without touching a service. The documentation does not show this split: the research record mixes
services by topic and date, and the architecture has no table of which service backs each tool.
Follow the Outlook connector's layout
([architecture](https://github.com/lucasrhode95/lrh-outlook-connector/blob/main/docs/architecture.md),
[research](https://github.com/lucasrhode95/lrh-outlook-connector/blob/main/docs/api-research.md)):

- **Research:** regroup by owner: Microsoft Graph; the Teams private services (AuthZ, Chat Service,
  CSA, Middle Tier, AMS); Microsoft Search (Search v2); SharePoint and OneDrive. Move findings with
  their dates; do not rewrite evidence. Keep or redirect every anchor that the roadmap, requirements,
  architecture, README and the Outlook repository link to.
- **Architecture:** one table mapping each MCP tool and UI route to its port, adapter and service,
  consistent with the backend split in [requirements §4](requirements.md#4-backend-choice); one
  references the other instead of repeating it.
- **README and docs index:** say which service backs what, in the Outlook README's style.

Example: a reader looking up how a pasted image is downloaded goes to the AMS section of the
research, and the architecture table shows `download_attachment` → `FileReader` → AMS and Graph
`/shares`.

### P2.2d — One Search v2 message search

The hit id is the history message id, and Teams Web shows how to scope, filter and page
([research](api-research.md#search-v2-message-search)). With D2, `search_messages` becomes the one
message search, on Search v2, and `search_remote_messages` goes. It:

- takes `terms`, a list of 1 or more phrases (decided 2026-10-15). A message matches when any term
  matches (OR). Each term is sent as one quoted phrase and never split on its spaces, so agent text
  is never read as KQL (`NOT IN THE CHAT` acted as an operator in Teams Web). Search v2 matches whole
  words and word prefixes: `["budget"]` also finds "budgets", `["udget"]` finds nothing, and
  `["budget review"]` needs those words in that order. The tool description says so;
- takes optional `conversation_ids`, `since` and `until`, sent as `clientthreadid:` and `sent` terms;
- adds `NOT (isClientSoftDeleted:TRUE)`;
- returns `message_id` (the hit's `InternetMessageId`) with each hit;
- reports `Total` and `MoreResultsAvailable`, and a confirmed zero (B2).

**Example:** `{"terms": ["budget review", "forecast"], "conversation_ids": ["19:sample@thread.v2"],
"since": "2026-08-01T00:00:00Z"}` sends `clientthreadid:19:sample@thread.v2 AND sent >=
2026-08-01T00:00:00Z AND NOT (isClientSoftDeleted:TRUE) AND ("budget review" OR "forecast")` and
returns hits with `message_id`.

### P2.2b — Find chats by participant without indexed rosters

Three server routes together cover most of it ([research](api-research.md#finding-chats-by-person)):

1. **One-on-ones:** People suggestions return the person's object id, which forms the chat id
   `19:{a}_{b}@unq.gbl.spaces`. Works with the current `m365.search` profile.
2. **Named group and meeting chats:** the Search v2 `Chat` entity, queried with the person's email.
   Works with `m365.search`; returns only named chats, about the last five months.
3. **Unnamed groups:** the catalog's generated names contain members' names, so name search finds
   them unless the person was cut off by the 87-character limit.

Not covered: an older named chat where the person is a member but not in the title. Decide whether
the chat-list cache keeps member lists for that case. Chat *suggestions* need a Teams-client
Substrate token, a new `lrh-m365-auth` profile, and return fewer chats than route 2, so they are not
needed.

### P1.6 — Look up omitted channels in one batch

Graph's channel listing omits some channels, mostly shared ones, so every catalog refresh asks each
joined team for each omitted channel, one request at a time
([`GraphChannels.locate`](../src/teams_connector/remote/graph_teams.py)). The list includes every
omitted channel stored earlier, so the cost recurs on each refresh.

**Example:** 4 joined teams and 3 omitted channels cost up to 12 sequential GETs per refresh, and
`send_message` refreshes the catalog before each send (S2).

**Fix:** send the team × channel lookups as one Graph `$batch` (`Graph.batch` already exists), or
remember which team each omitted channel belongs to and ask only that team.

### S2 — Check a send's recipient without a full catalog refresh

`send_message` refreshes the whole catalog to confirm the recipient's current name
([`Sender.send`](../src/teams_connector/service/send.py)). That costs a CSA delta, the joined-teams
and channel listings of every team, the omitted-channel lookups (P1.6) and a self-chat history read,
where the check needs only the one conversation.

**Fix:** refresh only what the recipient needs: the CSA delta for a chat, or one Graph GET of
`/teams/{team}/channels/{channel}` for a channel. The live name check itself stays.

### R1 — Multi-line messages read back double-spaced

**Seen:** a message sent as two lines (`<p>first<br>second</p>`) shows as two lines in Teams, but
`get_messages` returns `"first\n\nsecond"`.

**Expected:** `"first\nsecond"`, matching what Teams shows.

**Cause:** the text rendering turns each paragraph into a blank line
([`teams_html.py`](../src/teams_connector/remote/teams_html.py)), and Teams stored the message as
separate paragraphs. Teams Web displays consecutive paragraphs one line apart.

**Fix:** confirm the stored markup of a multi-line message, then render consecutive paragraphs with
one line break. Low priority.

### R2 — Whole messages per page; remove `get_message_content`

`get_messages` fits each page into `max_chars` (30,000 by default) by cutting long messages and
marking them `text_truncated`; the agent then calls `get_message_content` to read the rest of each
one. Keep the page budget, since up to 50 long messages could otherwise fill the agent's context, but
spend it on whole messages: fill the page until the next message would exceed `max_chars`, then stop
and return the cursor there, as Outlook's `get_conversation` does.

**Example:** a page of 25 messages where three are 12,000-character pasted logs. Today the page holds
all 25, the logs cut to fit, plus three `get_message_content` calls to finish them. After the change
the first page ends before the second log, and the cursor's next page starts with it, whole.

- A page always holds at least one message, whole, even when it alone exceeds `max_chars`, so a walk
  always advances. Teams bounds a message's size (roughly 28 KB; confirm), so that page stays bounded.
- `page_size` becomes "at most": a page can hold fewer messages when they are long.
- Remove `text_truncated` and `get_message_content`. This also removes one of the archive reads that
  D2 would otherwise have to rebuild on Teams.

## Enhancements

### P1.5 — Send host files as attachments

`send_message` sends text only. Graph documents a file send in two steps: upload the file to the
sender's OneDrive, then post a chat message whose `attachments` entry references that drive item
(`contentType: "reference"`) and whose body carries a matching `<attachment id="…">` tag
([Graph: send chatMessage](https://learn.microsoft.com/en-us/graph/api/chatmessage-post?view=graph-rest-1.0)).
The Teams client's Graph token held `Files.ReadWrite.All` on 2026-10-01. Not tried yet.

Settle before relying on it:

- that a recipient can open the file; a successful upload is not proof of delivery;
- what the user sees when the upload succeeds and the post fails: report the uploaded file, never
  retry the post;
- the Teams Web private path (chunked upload, `POST /v1/threads/{threadId}/files`,
  `properties.files`; [research](api-research.md#upload-chat-file)) is a fallback only if Graph's path
  loses something.

### N7 — HTML sends

`send_message` takes plain text only and escapes it, so an agent cannot send bold text, lists, links
or code. Add `html` beside `text` as two mutually exclusive body arguments: exactly one is required,
and passing both or neither is refused. Nothing is parsed or converted. `text` stays escaped as
today, so Markdown in it arrives as literal asterisks; `html` is sent as written. Agents write HTML
well, so there is no Markdown conversion.

**Example:** the agent sends

```html
<p><strong>Due Friday</strong></p>
<ul><li>review the <code>budget</code> sheet</li><li><a href="https://example.com/tracker">tracker</a></li></ul>
```

as `html`, and Teams shows a bold line followed by a two-item list with inline code and a link. The
same content sent as `text` shows the tags literally.

Settle before relying on it:

- Refuse unsafe HTML where it enters instead of rewriting it: scripts, frames, form controls,
  event-handler attributes, `javascript:` URLs and unfinished markup. The Outlook connector's
  [passive HTML check](https://github.com/lucasrhode95/lrh-outlook-connector/blob/main/src/outlook_connector/service/passive_html.py)
  is the reference rule set; the Teams code keeps its own copy.
- Which elements Teams renders: paragraphs, line breaks, bold, italics, inline and block code, lists
  and links are expected; headings, tables and images need a check. Document what Teams strips.
- The 10,000-character limit applies to the HTML as sent.
- Check rendering with disposable self-chat messages.

### P2.2f — Meeting recaps and transcripts

Four separate capabilities, none implemented: finding recaps, listing a meeting's transcripts,
reading a transcript, and searching transcript text. The Teams Web routes are now known
([research](api-research.md#teams-web-source-build-26091712222)):

- list a recording's transcripts: `{site}/_api/v2.0/drives/{drive}/items/{item}/media/transcripts`,
  taking the one flagged `isDefault`;
- read one: `…/media/transcripts/{id}/content` (`?format=docx`, or `?is=1` for VTT), or
  `{site}/_api/v2.1/drives/{drive}/items/{item}/versions/current/media/transcripts/{id}/streamContent`;
- meeting chats already carry a reference to that last route
  ([research](api-research.md#transcript-uri-reference-in-meeting-chat-history-2026-10-10)), and
  recording messages carry an `amsTranscript` item ([research](api-research.md#meeting-transcripts)).

**Next:** try the listing route with a SharePoint-audience token from the Teams client on one
recording you choose. Graph's per-meeting transcript API needs `OnlineMeetingTranscript.Read.All`,
which requires admin consent
([research](api-research.md#meeting-recaps-and-transcripts-as-search-sources-2026-10-10)). Respect
the download restriction the Teams UI showed for one meeting; a reference is not permission.

### N1 — Attachments and inline images by name

Decided 2026-10-15. Today pasted images appear only as
`[Image: https://…/views/imgo]` in the text, never as attachments, so exports and
`download_attachment` skip them; the URL is a compressed preview; downloads go only through Graph
`/shares`; and an emoji sent as a `data:` URI puts its base64 into the text
([research](api-research.md#inline-images-ams)).

**One extraction, three uses.** The same text rendering serves every route:

- `get_messages` returns text only. Each attachment or inline image is a marker with a stable name.
- `download_attachment` fetches the files the agent chooses, so it can judge from context what is
  worth downloading and what is a joke GIF.
- `export_chats` wraps the same extraction: by default it downloads every file its text names and
  writes each beside the text under that exact name; `include_attachments=false` writes text only.

**Markers.** Names come from the message itself, so the same message always gives the same names:

| Source | Marker | Name from |
|---|---|---|
| AMS image (`itemtype=…/AMSImage`) | `[Image: 0-neu-d1-0123….png]` | `itemid` (the AMS object id) and `itemscope` (the type) |
| `data:` image | `[Image: sha-3f9a1c2e.png]` | SHA-256 prefix of the decoded bytes and the MIME type |
| SharePoint or OneDrive file (`properties.files`) | `[File: budget-report.xlsx]` | the file's own name |

Equal names within one message get ` (2)`, ` (3)` in message order. Emoji, custom emoji and stickers
render as their `alt` or `title` text, not as markers. External images such as Giphy render as
`[GIF: <title>] <link>` and are never fetched. Base64 never reaches a response.

**`download_attachment` contract:** `conversation_id`; exactly one of `message_id` or `sent_at` (the
time exactly as the TXT export prints it); `names`, optional, all of the message's files when
omitted. It reads that one message again from Chat Service, by id
(`GET …/messages/{id}`) or by time (`startTime` = `endTime`, then the exact-time match), runs the
same extraction and resolves the names. Both are plain reads that leave the chat's read state as it
was ([requirements §8](requirements.md#8-history-exports-and-sending)). No identity cache is kept:
the message is the index. Several
messages at the same time is an error that lists their ids. Results are local paths, never MCP
resources or inline bytes.

**Fetching:** always full resolution. AMS images: `GET <AMS host>/v1/objects/{id}/views/imgpsh_fullsize`
with `Authorization: skype_token`; previews such as `imgo` are never used. Add the AMS hosts
(`*.asm.skype.com`, `*.asyncgw.teams.microsoft.com`) to the allowed hosts. `data:` images are decoded
from the message. SharePoint and OneDrive files keep the Graph `/shares` route.

**Download once:** the disk is the cache. Files go to a folder named by their source, under the
marker name: AMS by object id, `data:` images by SHA-256, SharePoint and OneDrive by drive item id
and version. An existing file is returned without fetching. Write to a temporary file and rename, so
concurrent processes are safe; the seven-day cleanup applies. Nothing downloads while reading
([requirements §6](requirements.md#6-local-data-decided-2026-10-14)).

**Exports:** TXT keeps today's lean header (exact UTC time, sender, reply and deletion marks) with the
markers in place of image URLs, and no ids; JSONL keeps every field, ids included.

**Settle while building:** channel history is read through Graph, which presents pasted images as
`hostedContents` URLs rather than AMS elements. Chat Service also serves channels (single reads were
checked live), so read channel history there too, or map Graph's hosted content to the same names.

### N5 — Presence

Show availability, activity, last-active time and device type, and nothing richer (no status notes
or calendar text) by default. Graph presence is not granted to the Teams client; Teams Web uses
Unified Presence `v1/presence/getpresence/`
([research](api-research.md#teams-web-source-build-26091712222)). Check it with app-owned sign-in.
Cache it briefly, fail open, and never block chat listing.

### N6 — Favorites

`GET {csa}/api/v1/teams/users/me/conversationFolders` returns the sidebar folders, including
`Favorites` with its chats, using the CSA credential the connector already has
([research](api-research.md#sidebar-folders-live)). Old pinned chats were migrated into Favorites.
Expose the Favorites folder (for example a `favorite` flag in `list_chats`), behind a setting until
checked on more accounts. Do not infer it from `isSticky`.

## Validation

### P1.1 — Catalog against the Teams UI

The type and naming rules are implemented ([requirements §5](requirements.md#5-catalog)). Compare a
live catalog with what Teams shows:

- a chat whose CSA row says `chatType=meeting` with `isOneOnOne=false` is a `meeting`, not a
  `group`; this precedence has no dedicated synthetic test yet;
- an untitled one-on-one with an app is named after the app;
- an untitled group reads like `Alex Doe | Sam Roe | Kim Poe +2`;
- the UI icon and label agree with `kind`.

Then reset the local catalog and bootstrap fresh. The connector orders chats by last activity; Teams'
own recent-chat order is unknown ([research](api-research.md#chat-list-ordering-source-mapping)).

### S3 — Sending to channels and meeting chats

Graph sends to the self chat, a one-on-one and a group chat were verified on 2026-10-14
([research](api-research.md#sending-through-graph)). Channels go through a different Graph route
(`/teams/{team}/channels/{channel}/messages`) and meeting chats may carry meeting policies. Send one
disposable, user-approved message to each, then check that the returned id matches history and that
a refusal is reported clearly.

### N2 — Unread state

`list_chats` reports CSA's `unread` flag. Check that it follows the Teams UI after a chat is read in
another client, and that reading an unread chat through the connector (`get_messages`,
`download_attachment`, an export) leaves it unread in Teams.

### A1 — Fresh-account sign-in

The 2026-10-12 shared-auth check reused existing grants
([research](api-research.md#shared-auth-live-validation-2026-10-12)). A new account's consent for the
Teams profiles and `m365.search` has not been tried.
