# Teams Connector Roadmap

Open bugs, improvements, enhancements and validation only. What the connector must do is in the
[requirements](requirements.md); how it is built is in the [architecture](architecture.md); dated
evidence is in the [research record](api-research.md). Delete an item once it is done. Codes are
never reused.

Snapshot **2026-10-14**.

## Bugs

Write each bug as **Seen** (a synthetic example: the call and what came back), **Expected**,
**Cause** and **Fix**.

### B1 — `exact` search mode compares the whole message

**Seen:** `search_messages` with `match_mode: "exact"` and query `alpha7q2 budget`, over a chat
holding the message `CACHEPROBE alpha7q2 budget`, returns no items with `coverage.complete: true`.

**Expected:** the message is found; `exact` means "this exact phrase".

**Cause:** the store compares the whole folded text (`m.text_folded = ?` in
[`Store.search`](../src/teams_connector/store/db.py)). An agent searching for a phrase concludes the
text is absent.

**Fix:** none if D2 lands first, because server-side search replaces the modes with quoted phrases.
Otherwise match the phrase on word boundaries.

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

## Improvements

Working behavior that could be cheaper, simpler or better informed.

### D2 — Remove the message archive

Decided 2026-10-14: Teams is the record and the connector keeps no message content
([requirements §6](requirements.md#6-local-data-decided-2026-10-14)). What replaces each use of the
archive:

| Today | Replacement |
|---|---|
| `search_messages`: fetch a scope, match locally | Search v2 scoped the way Teams Web scopes it: `clientthreadid:<id>`, `sent >= … AND sent < …`, a `conversationType` filter, `NOT (isClientSoftDeleted:TRUE)`. Merge it with `search_remote_messages` into one tool (P2.2d). |
| `search_chats` participant matches from indexed rosters | P2.2b |
| `get_messages` merges remote pages with retained ones | Read Teams only; drop the archive merge and its paging floor |
| `get_message_content`, `download_attachment` read the archived message | Re-read the message: chat message ids are epoch milliseconds, so `startTime` just before the id returns it first; channels by Graph message id |
| Exports stream from SQLite | Stream through the export's own temporary file |
| `sync_archive`, `archive_status`, the `backup` command | Remove |
| Catalog, sync token, untitled-chat names, omitted channels, photo ids | Keep, in a small account-bound cache with no message text |

Then reset local state; no migration. Update the README, architecture, MCP server instructions and
tests in the same change.

### P2.2d — Search v2 message search improvements

The hit id is the history message id, and Teams Web shows how to scope, filter and page
([research](api-research.md#search-v2-message-search)). Change `search_remote_messages` to:

- return `message_id` (the hit's `InternetMessageId`) and drop the "not verified" note;
- take optional `conversation_ids`, `since` and `until`, sent as `clientthreadid:` and `sent` terms;
- add `NOT (isClientSoftDeleted:TRUE)`;
- report `Total` and `MoreResultsAvailable`, and a confirmed zero (B2);
- keep agent text from being read as KQL: `NOT IN THE CHAT` acted as an operator in Teams Web.
  Quote or escape terms unless the caller asks for KQL.

**Example:** `{"query": "budget", "conversation_ids": ["19:sample@thread.v2"], "since":
"2026-08-01T00:00:00Z"}` should send `clientthreadid:19:sample@thread.v2 AND sent >=
2026-08-01T00:00:00Z AND NOT (isClientSoftDeleted:TRUE) budget` and return hits with `message_id`.

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

### N7 — Markdown to Teams rich text

Agents write Markdown, but `send_message` escapes it and Teams shows the asterisks. Convert a safe
subset to Teams HTML instead.

**Example:** the agent sends

```markdown
**Due Friday**
- review the `budget` sheet
- [tracker](https://example.com/tracker)
```

and Teams should receive

```html
<p><strong>Due Friday</strong></p>
<ul><li>review the <code>budget</code> sheet</li><li><a href="https://example.com/tracker">tracker</a></li></ul>
```

Support paragraphs and line breaks, bold, italics, inline code, fenced code, lists and links; tables
only if Teams renders them reliably. Escape raw HTML, and degrade anything unsupported to readable
text rather than failing the send. Keep a plain-text mode for exact sends. Check rendering with
disposable self-chat messages.

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

### N1 — Conversation viewer with authenticated resources

The UI exports conversations and serves only profile photos (`/api/photos/{user_id}`). A viewer that
shows message images and files needs a resource gateway. It would also cover sender pictures (former
N3) and rendering fidelity (former N4).

- Serve `GET /api/resources/{opaque_id}`, never `?url=https://…`: an arbitrary URL would leak into
  logs and make the server an open proxy.
- Resolve the opaque id to a resource found earlier, with its validated host, path family and fetch
  strategy. Allow only Teams, AMS and the tenant's SharePoint/OneDrive hosts.
- Never forward the caller's headers or return a Microsoft token; stream the bytes, keeping only the
  type, length and file name.
- Use one fetch layer for photos, AMS images and SharePoint files, shared with exports. Optionally
  expose the same resource as an MCP Resource.

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
another client.

### A1 — Fresh-account sign-in

The 2026-10-12 shared-auth check reused existing grants
([research](api-research.md#shared-auth-live-validation-2026-10-12)). A new account's consent for the
Teams profiles and `m365.search` has not been tried.
