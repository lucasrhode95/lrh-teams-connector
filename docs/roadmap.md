# Teams Connector Roadmap

Open bugs, improvements, enhancements and validation only. What the connector must do is in the
[requirements](requirements.md); how it is built is in the [architecture](architecture.md); dated
evidence is in the [research record](api-research.md). Delete an item once it is done. Codes are
never reused.

Snapshot **2026-10-15**.

## Bugs

Write each bug as **Seen** (a synthetic example: the call and what came back), **Expected**,
**Cause** and **Fix**.

None open.

## Improvements

Working behavior that could be cheaper, simpler or better informed.

### D3 — Rewrite the research record

`docs/api-research.md` (about 3,700 lines) is meant for what Microsoft does not document, or documents
ambiguously enough that it had to be tested. It has grown into a chronological log that also restates
documented Graph behavior and keeps findings later ones replaced. Rewrite it by service (AuthZ, CSA,
Chat Service, AMS, Search v2 and Substrate, SharePoint and OneDrive, presence, Teams Web), each with the
routes the connector uses, their request and response shapes, limits and dated evidence:

- documented Graph behavior moves out; only Graph findings that contradict or sharpen the documentation stay;
- superseded findings are deleted (git keeps them), as are references to removed features;
- routes the connector does not use (Chat Service edits, deletes, reactions and drafts, Middle Tier
  profile routes, AMS upload, sharing links, other search routes) stay, trimmed to route, shape and status,
  as a source for later work;
- links from the other documents, and the two from the Outlook research, follow the new anchors.

### S4 — Check the recipient against the stored catalog (decision pending)

`send_message` refreshes the recipient from Teams before comparing `expected_chat_name` (S2). The
message is addressed by id; the name catches an agent that copied the wrong id, and the stored name
catches that as well. Proposed: compare against the stored catalog, and answer an id that is not stored
with "run `list_chats` first". This drops the recipient refresh (`Catalog.recipient`'s refresh paths,
`_refresh_channel` and the single-channel read).

What the refresh catches today and the stored name would not, both since the agent's last listing:

- a renamed chat or channel: the send is refused until the agent lists again; the id, and so the
  audience, is unchanged;
- an untitled group whose members changed: its generated name changes, so the send is refused. A named
  group whose members changed is not caught today either, since its name stays.

Not affected: Teams refuses a send to a chat that is gone or that the user left. Decide whether the
untitled-group case is worth the refresh.

### N8 — AMS images on the newer media host

Pasted images are fetched only from `*.asm.skype.com`, the host every inline image used on 2026-10-15
(67 of 67). AuthZ also names `amsV2` (`*.asyncgw.teams.microsoft.com`), which recording cards already use.
If an image arrives there, it shows its marker but is not downloadable. Allow that host for image views when
one is seen.

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
- pasted images: Graph sends an inline image as a `hostedContents` entry with `<img
  src="../hostedContents/1/$value">` in the body; Teams Web uploads to AMS and references the object. HTML
  sends refuse `<img>` until one of these is implemented;
- the Teams Web private path (chunked upload, `POST /v1/threads/{threadId}/files`,
  `properties.files`; [research](api-research.md#upload-chat-file)) is a fallback only if Graph's path
  loses something.

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

## Validation

### P1.1 — Catalog against the Teams UI

The type and naming rules are implemented ([requirements §5](requirements.md#5-catalog)). Compare a
live catalog with what Teams shows:

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

### A1 — Fresh-account sign-in

The 2026-10-12 shared-auth check reused existing grants
([research](api-research.md#shared-auth-live-validation-2026-10-12)). A new account's consent for the
Teams profiles and `m365.search` has not been tried.
