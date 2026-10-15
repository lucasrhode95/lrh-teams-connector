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

- **Restrictions:** Teams Web checks `opStream/capabilities` (`canExport`, `canExtract`) before showing a
  page. Find out whether Graph's `?format=html` refuses a page that forbids export. If it does not, the
  connector must check, which needs a SharePoint-audience token: a new shared auth profile, for the user
  to decide.
- **Pasted Loop components:** do they use the same embed? A person's message with one keeps its text and
  link today; handle it like the notes if the page converts the same way.
- **Page name in the marker:** the app's message carries no page name, so reading shows
  `[Meeting notes] <link>`; the title appears only once the page is read. Is there a name to show without
  reading the page?
- Should the PDF (`?format=pdf`) be offered as the downloaded file instead of text?

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
recording you choose. The user named a recurring standup meeting chat whose recordings have
transcripts (2026-10-15); it is also the chat behind N9's findings. Graph's per-meeting transcript API needs `OnlineMeetingTranscript.Read.All`,
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
