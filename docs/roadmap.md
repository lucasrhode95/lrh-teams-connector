# Teams Connector Roadmap

Open work only. What the connector must do is in the [requirements](requirements.md); how it is built is
in the [architecture](architecture.md); dated evidence is in the [research record](api-research.md).
Delete an item once it is done. Codes are never reused.

Work is split by what it needs: **implementation** is code still to write; **decisions** wait for the
user; **live verification** checks behavior that is already built against a real tenant, and becomes
implementation only if a check fails.

Snapshot **2026-11-09**.

## Implementation

### Bugs

Write each bug as **Seen** (a synthetic example: the call and what came back), **Expected**,
**Cause** and **Fix**.

None open.

### Improvements

Working behavior that could be cheaper, simpler or better informed.

#### H1 — Revisit HTML-to-text conversion

Review the custom message and Loop HTML converters, especially table structure. A synthetic ordinary
message table with cells `Name`, `Role` and `Sam`, `Host` currently becomes `NameRole\n\nSamHost`:
row boundaries survive, but cells run together. Loop notes use a separate row/task converter.

Preserve cell boundaries, links, lists, code, mentions and image references in the text agents receive.
Compare library conversion and minimal HTML for complex tables with the existing parsers; consolidate
where it reduces code without losing Teams-specific source metadata. Cover merged/nested tables and
malformed HTML with synthetic examples, and decide whether readers need an HTML option.

### Enhancements

#### P1.5 — Send files to meeting chats

Files and inline images work in one-on-one and group chats and the self chat, including files of 250 MiB
or more through upload sessions ([architecture](architecture.md#85-send-a-message),
[research](api-research.md#sending-files)). Extend this support to meeting chats, which currently refuse
these sends and have not yet been tried live.

#### P1.8 — Edit the user's own messages

Requested 2026-11-03, to revise a draft posted in the self chat instead of deleting it and sending it again.
Not implemented: edits are a non-goal today ([requirements §3](requirements.md#3-non-goals)), and
`send_message` and `delete_messages` are the only write tools. Implementing this makes editing a third
write path. That needs the requirements and the agent instructions changed, with the same safeguards as
sending: explicit authorization for the message and for its new content, the user's own messages only
(`is_self`), one attempt and no automatic retry.

- **Route:** Chat Service `PUT …/messages/{messageId}`, sending the whole message with new `content` and
  `properties.edittime`, was captured but never called ([research](api-research.md#writes)). Graph edits
  were refused with the current grants ([research](api-research.md#grants-2026-10-01)). Try the Chat
  Service route on one disposable self-chat message first.
- **Scope:** decide whether an edit may change files and inline images, or only the text.
- **Result:** confirm with `get_messages`, which shows the newest text and `edited_at`. Teams keeps no
  readable copy of the replaced text ([research](api-research.md#edits-and-deletes)).

#### P2.2f — Meeting recaps

The AI summary of each transcribed meeting is in exports, `get_messages` and transcript search
(architecture, "Meeting transcripts"; [research](api-research.md#meeting-summary)). The rest of the recap,
its mentions and tasks, is not built.

## Decisions

None open.

## Live verification

Built behavior not yet checked on a live tenant. Each check needs the user's authorization for the live
read or write it makes; record the dated result in the research record.

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

### N9 — Loop pages beyond the meeting notes

The Facilitator's meeting notes are read and checked live (2026-10-16; architecture, "Exports"). Still to
check:

- **Pasted Loop components:** does a person's pasted component use the same embed? Such a message keeps
  its text and link today; if the page converts the same way, handling it like the notes becomes an
  enhancement.
- **Page name in the marker:** the app's message carries no page name, so it reads as
  `[Meeting notes] <link>` until the page is read. Is there a name to show without reading the page?

### P2.2g — Transcripts beyond meeting chats

Transcripts in meeting chats are verified live (2026-10-16; [research](api-research.md#transcripts)).
Still to check:

- **Channel recordings:** their transcripts are untested; transcript search skips channel conversations
  with a note.
- **Group chats and calls:** a recording posted in a group chat or a one-on-one call is verified the same
  way in code, but none has been checked live.
- **Across meetings:** each video costs a few chat reads (at most 25 chats, 20 pages each), so a video old
  enough that many chats were active since is reported as unverified. Measure on a busy tenant before
  raising the bound.

### A1 — Fresh accounts and other tenants

The 2026-10-12 shared-auth check reused existing grants
([research](api-research.md#credentials-by-service)). A new account's consent for the Teams profiles and
`m365.search` has not been tried, and the transcript and meeting-summary routes are verified on one
account only. Organization tools passed live on the existing account on 2026-11-09
([research](api-research.md#organization-lookups)); recheck manager access with fresh-account/tenant grants,
keeping denials distinct from empty hierarchies. Reassess these capabilities per deployment.
