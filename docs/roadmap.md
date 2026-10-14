# Teams Connector Roadmap

Open bugs, improvements, enhancements and validation only. What the connector must do is in the
[requirements](requirements.md); how it is built is in the [architecture](architecture.md); dated
evidence is in the [research record](api-research.md). Delete an item once it is done. Codes are
never reused.

Snapshot **2026-10-14**.

## Bugs

None open. Write each bug as **Seen** (a synthetic example: the call and what came back),
**Expected**, **Cause** and **Fix**.

## Improvements

Working behavior that could be cheaper, simpler or better informed.

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

### P2.2b — Find chats by participant without indexed rosters

`search_chats` matches participants only in rosters that `sync_archive` has indexed, so a chat with
a person is invisible until its roster is synced. Teams has a chat-suggestion search
(`EntityType: "Chat"`, fields `ThreadId`, `MatchingMembers`, `ChatMembers`) and a People suggestion
search. The chat request returned HTTP 400 on 2026-09-29, but that probe used the older Substrate
token; the `m365.search` profile now answers Search v2 on the Outlook host, and the browser's People
suggestions (`scenario=powerbar`) succeeded in the 2026-10-11 capture
([research](api-research.md#substrate-peoplechat-search)).

**Next:** retry the chat suggestion through the `m365.search` profile. If it answers, map
`ThreadId` to the catalog and report roster-indexed and suggested matches separately.

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
reading a transcript, and searching transcript text. Leads, best first:

- **The archive already holds transcript references.** Recording messages in meeting chats carry an
  `amsTranscript` item. Public reverse-engineering fetches its URL with the Teams session token and
  gets WEBVTT back ([research](api-research.md#meeting-transcripts)); the connector already has
  that session. Two known meeting chats also carry a drive-hosted reference, `/drives/{drive}/items/{item}/{segment}/{segment}/media/transcripts/{transcript}/content`,
  whose two middle segments are unmapped
  ([research](api-research.md#transcript-uri-reference-in-meeting-chat-history-2026-10-10)).
- **Graph** lists and reads transcripts per online meeting, but needs
  `OnlineMeetingTranscript.Read.All`, which requires admin consent
  ([research](api-research.md#meeting-recaps-and-transcripts-as-search-sources-2026-10-10)).
- Transcript searches that returned empty or unreadable envelopes do not prove search unsupported.

Respect the download restriction the Teams UI showed for one meeting; a reference is not permission.

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
or calendar text) by default. Graph presence is not granted to the Teams client, so this needs Teams
Unified Presence with app-owned sign-in. Cache it briefly, fail open, and never block chat listing.

### N6 — Favorites and pinned chats

Show the chats and channels the user keeps at the top of Teams. Whether the UI's Favorites are the
same as the pinned state below is unverified. The 2026-10-11 capture shows `GET /api/csa/{region}/api/v1/teams/users/me/pinnedChannels`, and the client's chat-list
GraphQL has a pinned connection with `PinnedChat.order`
([research](api-research.md#global-teams-search),
[source audit](api-research.md#chat-list-ordering-source-mapping)). Read the real pinned state; do
not infer it from `isSticky`. Put it behind a setting until it is checked on more accounts.

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

### P2.1 — Edit and delete behavior, live

The retention rules are implemented and tested synthetically
([requirements §6](requirements.md#6-archive-and-retention)). How Chat Service reports an edit and a
delete (`edittime`, `deletetime`, message type, body) has not been observed live.

**Authorized** by the user: disposable messages in the user's own self chat (`48:notes`) only, from a
temporary script outside the repository. Send a few uniquely prefixed messages, read them into the
archive, edit one more than once, delete another, read the raw history after each change, then check
that the archive, exports and search follow the rules. Clean up the test messages where possible.
Never touch another chat, and never print tokens or headers. Record the observed contract in the
research.

### P2.2d — Online message search through the connector

`search_remote_messages` has synthetic tests only; it has not been called live through this
connector. Check:

- **Hit to history message.** Teams Web opens a hit from `ClientThreadId` and `DateTimeSent`, without
  a page number ([research](api-research.md#search-result-activation-and-exact-message-navigation-source-2026-10-11-audit)).
  Teams chat message ids look like the send time in epoch milliseconds. If a hit's `DateTimeSent` of
  `2026-08-14T12:30:05.123Z` matches history message id `1786710605123`, the tool can return a
  `message_id` directly. One known chat's identities matched in the standalone replay; a second
  target did not resolve.
- **Conversation match.** Teams Web takes the chat target from `ClientThreadId` and parses a
  reply-chain id out of `ClientConversationId` for group and channel hits, so `ClientConversationId`
  may never equal a catalog id. The adapter tries it first; check that `ClientThreadId` is the one
  that matches.

### N2 — Unread state

`list_chats` reports CSA's `unread` flag. Check that it follows the Teams UI after a chat is read in
another client.

### A1 — Fresh-account sign-in

The 2026-10-12 shared-auth check reused existing grants
([research](api-research.md#shared-auth-live-validation-2026-10-12)). A new account's consent for the
Teams profiles and `m365.search` has not been tried.
