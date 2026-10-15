# Teams Connector Roadmap

Open bugs, improvements, enhancements and validation only. What the connector must do is in the
[requirements](requirements.md); how it is built is in the [architecture](architecture.md); dated
evidence is in the [research record](api-research.md). Delete an item once it is done. Codes are
never reused.

Snapshot **2026-10-15**.

## Bugs

Write each bug as **Seen** (a synthetic example: the call and what came back), **Expected**,
**Cause** and **Fix**.

## Improvements

Working behavior that could be cheaper, simpler or better informed.

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

### P2.2b — Find chats by participant (partial)

Done 2026-10-15: route 2 (below) is in `search_chats` with `refresh=true`, and route 3 (names) already
worked. Live check on 2026-10-15: a person's name matched 18 chats, 14 of them only through Teams'
member match, which name matching in the catalog cannot find ([research](api-research.md#chat-search-by-member-2026-10-15)).

Still open, and the only remaining part:

1. **One-on-ones by person:** People suggestions would return the person's object id, which forms the
   chat id `19:{a}_{b}@unq.gbl.spaces`. Blocked: on 2026-10-15 every People suggestion request tried
   returned 400 (or 404 on another path), with the `m365.search` token and the captured request shapes.
   Decide whether to find the working route and token, or to accept name matching for one-on-ones.
2. **The uncovered case:** an older named chat where the person is a member but not in the title.
   Teams' chat search covers only named group and meeting chats with recent activity, as the coverage
   notes say. Decide whether the chat catalog should keep member lists for that case; until then, a chat
   matched only through an older member may be missing from `search_chats`.

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
