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

None open.

## Enhancements

### P1.5 — Send host files as attachments

Files and inline images work in one-on-one and group chats and the self chat, including files of 250 MiB
or more through upload sessions ([architecture](architecture.md#send-a-message),
[research](api-research.md#sending-files)). Still open:

- **Channels and meeting chats:** files and images are refused there today. Channels store files in the
  team's SharePoint folder instead; meeting chats are untried.

### N9 — Meeting notes from Loop pages

The meeting app's notes link reads as a marker, and exports and `download_attachment` read the page as
text ([architecture](architecture.md#exports)). Still open:

- **Pasted Loop components:** do they use the same embed? A person's message with one keeps its text and
  link today; handle it like the notes if the page converts the same way.
- **Page name in the marker:** the app's message carries no page name, so reading shows
  `[Meeting notes] <link>`; the title appears only once the page is read. Is there a name to show without
  reading the page?
- Decided 2026-10-16: the downloaded file is text, not the PDF (`?format=pdf`).

### P2.2f — Meeting transcripts

Treat a meeting's transcript like an attachment of its recording message. Graph's `media/transcripts` route
works with the existing `teams.graph` token, with no SharePoint token or admin consent
([research](api-research.md#transcripts)).

- **Marker:** in `get_messages` and exports, a recording message gets a transcript marker like its file
  markers, for example `[Transcript: <meeting>, 2026-10-14 11:00–11:30, 4 speakers]`. Reading fetches nothing.
- **Download:** exports and `download_attachment` fetch the default transcript (`isDefault`) with
  `?format=json`. They write it as text, grouping consecutive entries by `speakerDisplayName` with the start
  offset (`Name  0:17` and then the lines), into a `transcripts/` folder next to the attachments folder.
- **Flag:** `include_transcripts`, on by default in MCP and the UI, like `include_attachments`.
- **Failures:** a missing or unreadable transcript is reported like an unavailable attachment.
- **Access (decided 2026-10-16):** the connector serves a person who already has access to the meeting, so
  it reads transcripts even where the Teams UI disables Download. Record this in requirements §8 when
  building.
- **Search:** a `search_transcripts(terms, conversation_ids?, since?, until?)` tool with two paths and one
  result shape: meeting, conversation, recording, speaker, offset, line. Its coverage says which path ran.
  - *Scoped* (conversations given): read those chats' recording messages, fetch each JSON transcript, and
    match in memory. The result is complete within the scope. If long chats make this slow, run the
    unscoped search first and keep only the scope's recordings.
  - *Unscoped:* Graph `POST /search/query` over `driveItem` with `filetype:mp4` and the terms finds
    recordings by their spoken words across all meetings, ranked, with approximate totals. Then fetch each
    hit's JSON transcript to find the speaker and offset, and to drop title-only hits.
- **Later:** recaps (AI summary, mentions, tasks; an internal service).

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
