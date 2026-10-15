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

## Enhancements

### P1.5 — Send host files as attachments

Files on `send_message` work for one-on-one and group chats (implemented 2026-10-15; see
[architecture](architecture.md#send-a-message)): Graph upload to `config.CHAT_FILES_FOLDER`, a
`driveItem/invite` per file following `config.SHARED_FILE_ACCESS`, then one post with `reference`
attachments. The 2026-10-15 probes of these Graph calls were confirmed by their recipients
([research](api-research.md#sending-files)).

The first connector send, on 2026-10-15, sent one small text file with a caption to a one-on-one chat.
It was sent once and returned `accepted` with the file's name and OneDrive link. `get_messages` read it
back with the caption and the `[File: …]` marker. The file was renamed on a clash, and the result and
marker both used the new name. The recipient confirmed that the file opened.

Open:

- **Files of 250 MiB or more:** refused today. A single PUT takes at most 262,143,999 bytes; larger files
  need a Graph upload session (`createUploadSession`, then ranged PUTs to its `uploadUrl`).
- **Pasted images:** `hostedContents` (`<img src="../hostedContents/1/$value">`) was stored as an AMS image,
  as a paste is. Once implemented, HTML sends can stop refusing such an `<img>`.
- **Other conversations:**
  - Channels store files in the team's SharePoint folder instead.
  - Meeting chats and the self chat are untried with Graph `reference` attachments; the self chat has no
    one to share with.
- **Known gaps against Teams Web:**
  - Graph attachments leave `permissionScope` and `shareUrl` empty; recipients saw no difference.
  - Graph cannot read the chat-audience default (`driveItem/commands`); `getDefaultLink` matched it on one
    tenant.

### P1.7 — Delete the user's own messages

Requested 2026-10-15, to clear test sends from a chat. Not implemented: deletes are out of scope today
([requirements §3](requirements.md#3-non-goals)), and sending is the only write path. Implementing this makes deleting
a second write path. That needs the requirements and the agent instructions changed, with the same
safeguards as sending: explicit authorization for the chat and for every message, the user's own
messages only (`is_self`), one attempt and no automatic retry.

- **Route:** Graph `POST /chats/{chat}/messages/{message}/softDelete` is untried. Chat Service
  `DELETE …/messages/{messageId}?behavior=softDelete` was captured, with an undo
  ([research](api-research.md#writes-not-used-by-the-connector)). Probe Graph first, as was done for sends.
- **Shared files:** deleting a message leaves its files in `config.CHAT_FILES_FOLDER`, still shared with
  the chat. Decide whether to also remove the file, revoke its sharing, or report it.
- **Result:** confirm with `get_messages`, which shows a deleted message as `is_deleted` with no text
  ([research](api-research.md#edits-and-deletes)).

### P2.2f — Meeting recaps and transcripts

Four separate capabilities, none implemented: finding recaps, listing a meeting's transcripts,
reading a transcript, and searching transcript text. The Teams Web routes are now known
([research](api-research.md#transcripts)):

- list a recording's transcripts: `{site}/_api/v2.0/drives/{drive}/items/{item}/media/transcripts`,
  taking the one flagged `isDefault`;
- read one: `…/media/transcripts/{id}/content` (`?format=docx`, or `?is=1` for VTT), or
  `{site}/_api/v2.1/drives/{drive}/items/{item}/versions/current/media/transcripts/{id}/streamContent`;
- meeting chats already carry a reference to that last route
  ([research](api-research.md#transcripts)), and
  recording messages carry an `amsTranscript` item ([research](api-research.md#recording-cards)).

**Next:** try the listing route with a SharePoint-audience token from the Teams client on one
recording you choose. Graph's per-meeting transcript API needs `OnlineMeetingTranscript.Read.All`,
which requires admin consent
([research](api-research.md#transcripts)). Respect
the download restriction the Teams UI showed for one meeting; a reference is not permission.

## Validation

### P1.1 — Catalog against the Teams UI

The type and naming rules are implemented ([requirements §5](requirements.md#5-catalog)). Compare a
live catalog with what Teams shows:

- an untitled one-on-one with an app is named after the app;
- an untitled group reads like `Alex Doe | Sam Roe | Kim Poe +2`;
- the UI icon and label agree with `kind`.

Then reset the local catalog and bootstrap fresh. The connector orders chats by last activity; Teams'
own recent-chat order is unknown ([research](api-research.md#sidebar-order)).

### S3 — Sending to channels and meeting chats

Graph sends to the self chat, a one-on-one and a group chat were verified on 2026-10-14
([research](api-research.md#behavior-beyond-the-documentation)). Channels go through a different Graph route
(`/teams/{team}/channels/{channel}/messages`) and meeting chats may carry meeting policies. Send one
disposable, user-approved message to each, then check that the returned id matches history and that
a refusal is reported clearly.

### A1 — Fresh-account sign-in

The 2026-10-12 shared-auth check reused existing grants
([research](api-research.md#credentials-by-service)). A new account's consent for the
Teams profiles and `m365.search` has not been tried.
