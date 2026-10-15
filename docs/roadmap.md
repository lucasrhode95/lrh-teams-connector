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

`send_message` sends text only. Decided 2026-10-15:

- **Shape:** an optional list of host file paths on `send_message`, beside `text` or `html`. One
  authorization covers the recipient, the text and the files. Any readable path the user approves.
- **Automatic, like Teams:** the caller names files only. The connector uploads each one to the sender's
  OneDrive `Microsoft Teams Chat Files` folder (renaming on a clash), grants the conversation's other members
  access, and posts the message referencing the files
  ([research](api-research.md#sending-files-not-used-by-the-connector)).
- **Access:** the tenant default Teams reads for the file: for chats on 2026-10-15, edit rights for the
  specific people in the chat. Keep this behind one policy setting so "anyone in the organization" or a
  fixed "chat members only" can replace it.
- **Several files, all or nothing:** every upload and grant finishes before the post. If one fails, nothing
  is posted.
- **Partial outcomes:** if the files were uploaded but the post failed or is uncertain, report the uploaded
  files and their links. Never retry the post, and never delete the uploads.
- **Order:** one-on-one and group chats first. Channels upload to the team's SharePoint folder instead and
  come later. Pasted images (AMS upload, `<img>` in the HTML) follow files; until then, HTML sends keep
  refusing `<img>`.

**Next:**

1. Choose the route. Graph documents upload, `driveItem/invite` and a `reference` attachment, and the Teams
   client's Graph token held `Files.ReadWrite.All` (2026-10-01). Not yet known:
   - whether the invite stays silent (`sendInvitation: false`);
   - whether a `reference` attachment looks like a Teams file share to the recipient;
   - how to read the tenant default without the SharePoint `commands` route.

   Teams Web's own route needs a SharePoint-audience token for the OneDrive host and a Chat Service post
   with `properties.files`.
2. A probe needs the user's authorization for its destination, file and text. A real check needs a
   recipient, because the self chat has no members to grant.
3. Size: an 8 MB file uploaded in one PUT. Find the single-request limit before adding upload sessions.

Validation: the 2026-10-15 captures don't show that recipients could open the files. Check that with the
first connector send to a one-on-one and a group chat.

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
