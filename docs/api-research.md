# Teams API research

What Microsoft does not document about the services this connector uses, or documents too vaguely to
build on without testing: Teams' private services, the Teams client's Graph grants, and the search
routes Teams Web calls. Documented Microsoft Graph behavior is not repeated here; only Graph findings
that contradict or sharpen the documentation are.

> These are private, undocumented interfaces. They can change without notice. Treat every route,
> field and status below as observed behavior, not a contract.

## Contents

- [How to read this record](#how-to-read-this-record)
- [Credentials by service](#credentials-by-service)
- [AuthZ](#authz)
- [Chat Service Aggregator (CSA)](#chat-service-aggregator-csa)
- [Chat Service](#chat-service)
- [Message content](#message-content)
- [AMS (Async Media Service)](#ams-async-media-service)
- [Unified Presence](#unified-presence)
- [Microsoft Graph with the Teams client](#microsoft-graph-with-the-teams-client)
- [Search v2 and Substrate suggestions](#search-v2-and-substrate-suggestions)
- [SharePoint and OneDrive files](#sharepoint-and-onedrive-files)
- [Meeting recaps and transcripts](#meeting-recaps-and-transcripts)
- [Middle Tier](#middle-tier)
- [Teams Web client behavior](#teams-web-client-behavior)
- [Sources](#sources)

## How to read this record

Each finding carries its evidence and date:

| Label | Meaning |
|---|---|
| **Live** | Called through the connector's own sign-in (or an app-owned probe) on one account. |
| **Capture** | Seen in a Teams Web browser capture taken around a controlled action. The capture is not kept. |
| **Source** | Read in a production Teams Web JavaScript bundle. Shows what the client builds, not what the server accepts. |
| **Third party** | An open-source client or public write-up. Corroboration only. |

Live results describe one account, client and tenant at one time. A grant or denial seen here is not
universal: rerun the call before choosing a backend for another account, and keep a missing permission, a
bad request shape, a transport failure and an empty result apart. Sections marked **not used by the
connector** keep routes trimmed to what a later feature would need.

Method: read-only probes unless the user authorized a write for its exact destination and content (the
writes recorded here were synthetic messages to the user's own chats); one user action per capture,
marked with a unique string; no tokens, cookies, sync cursors, message text, names or real ids in this
record or in fixtures. Example ids are synthetic.

## Credentials by service

The connector signs in once with the shared `lrh-m365-auth` profiles
([architecture](architecture.md#authentication-and-account-boundaries)). The Teams profiles use the Teams
first-party public client (`1fec8e78-bce4-4aaf-ab1b-5451cc387264`). Each Teams service expects its own
credential:

| Service | Credential | Evidence |
|---|---|---|
| AuthZ | Bearer, audience `https://api.spaces.skype.com` (`teams.spaces`) | Live |
| Chat Service | `Authentication: skypetoken=<Skype token>` from AuthZ; no bearer needed | Live |
| CSA | Bearer, audience `https://chatsvcagg.teams.microsoft.com` (`teams.csa`), plus `x-skypetoken` | Live |
| AMS images | `Authorization: skype_token <Skype token>` | Live, 2026-10-15 |
| Unified Presence | Bearer `teams.spaces`; the Skype token is refused (401) | Live, 2026-10-15 |
| Search v2, People suggestions | Bearer from the Outlook Web client for `outlook.office.com/search` (`m365.search`) | Live, 2026-10-14 |
| Substrate Chat suggestions | Teams client bearer for `https://substrate.office.com/.default` | Live, 2026-10-14 |
| Graph | Teams client bearer for `https://graph.microsoft.com/.default` (`teams.graph`) | Live |
| Middle Tier | Bearer `teams.spaces` | Live (`/beta/users/fetch`, 2026-09-29) |
| OneDrive file sends | Teams Web client bearer for `https://<tenant>-my.sharepoint.com/.default`; not tried with the connector's client | Capture, 2026-10-15 |

**Shared sign-in, live 2026-10-12:** `teams-connector auth` reused existing grants: no device code, no MFA
prompt. Every Teams profile then acquired a token silently. A fresh account's consent has not been tried
(roadmap A1). In the offline `status` output, `client_credential_cached=false` for a Teams profile did not
mean its token was missing.

## AuthZ

`POST https://authsvc.teams.microsoft.com/v1.0/authz` (also proxied at
`https://teams.microsoft.com/api/authsvc/v1.0/authz`), `teams.spaces` bearer, `Accept: application/json;
ver=1.0`, empty body. Live.

- `tokens.skypeToken` and `tokens.expiresIn` hold the Skype token. Older shapes nest it as
  `skypeToken.skypetoken` (third party).
- `regionGtms` names every regional service. Prefer it over hard-coded hosts. On 2026-10-15 it held 112
  entries; those the connector uses, for one EMEA account:

| Entry | Value (host and path) | Used for |
|---|---|---|
| `chatService` | `emea.ng.msg.teams.microsoft.com` | history, members, single messages |
| `presenceUPS` | `teams.microsoft.com/ups/emea` | presence |
| `ams` | `eu-api.asm.skype.com` | pasted images |
| `amsV2` | `eu-prod.asyncgw.teams.microsoft.com` | recording and transcript items (not fetched) |
| `unifiedPresence` | `presence.teams.microsoft.com` | global presence host; redirects to the regional one |
| `middleTier` | `teams.microsoft.com/api/mt/part/<region>` | not used |

Teams Web also has `authzcore` at the same hosts (Source); its contract is unknown.

## Chat Service Aggregator (CSA)

The chat catalog with UI state. Base `https://teams.microsoft.com/api/csa/api/v1/`, `teams.csa` bearer plus
`x-skypetoken`.

### Catalog snapshot and delta

| Request | Result (live) |
|---|---|
| `GET teams/users/me?isPrefetch=false&enableMembershipSummary=true` (startup) | Full catalog: `chats`, `teams`, `channels`, `users`, `privateFeeds`, `metadata` |
| `GET teams/users/me/updates`, no `x-ms-synctoken` | The same full snapshot (2026-09-30: 944 chats in about 4 s) |
| `GET teams/users/me/updates`, `x-ms-synctoken: <metadata.syncToken>` | Only what changed since; unchanged: empty arrays and the same token in about 0.5 s |

- `metadata` carries `syncToken`, `forwardSyncToken`, `isPartialData` and `hasMoreChats`; every snapshot seen
  had both flags false (958 chats on 2026-10-12).
- The bundle declares a `SyncTokenExpired` error category (Source). The connector treats a rejected or expired
  token as a request for the full snapshot.
- Channel rows (`channels`) carry activity only; the connector's channel list comes from Graph.
- The self chat `48:notes` is never in CSA: not in the startup, snapshot or delta responses.
- Reading history, downloading a file or exporting through the connector did not change a chat's `isRead`
  (live, 2026-10-15, on unread meeting chats).

### Chat rows

Fields seen on a row: `id`, `title`, `chatType`, `isOneOnOne`, `threadType`, `isRead`, `isSticky`,
`hidden`, `consumptionHorizon`, `members`, `lastMessage` (`originalArrivalTime`, `from`, `imDisplayName`,
`properties.fromAppMetadata`), `meetingInformation`, `createdAt`, `version`.

| Kind | CSA | Chat Service (`threadProperties`) |
|---|---|---|
| One-on-one | `chatType="chat"`, `isOneOnOne=true` | `threadType="chat"`, `productThreadType="OneToOneChat"` |
| Group | `chatType="chat"`, `isOneOnOne=false` | `threadType="chat"`, `productThreadType="Chat"` |
| Meeting | `chatType="meeting"`, `threadType="meeting"` | `threadType="meeting"`, `productThreadType="Meeting"` |
| Self chat | absent | id `48:notes`, `productThreadType="StreamOfNotes"` |
| Channel | in `channels` | `TeamsStandardChannel`, `TeamsSharedChannel` |

Live 2026-10-01 unless dated otherwise:

- Ordinary group rows can carry a `meetingInformation` object of placeholders (null subject, year-one
  times). Only the explicit type fields mark a meeting.
- Id shapes: `19:meeting_…@thread.v2` (meeting), `19:…@thread.v2` (group), `19:<oid>_<oid>@unq.gbl.spaces`
  (one-on-one: the two members' object ids), `8:orgid:<oid>` (person), `28:<id>` (app or bot). Prefixes are
  a fallback hint only.
- One-on-one rows often have no title, and member records no display names; `users` was empty.
- Untitled groups have no title anywhere; Teams composes the label from the roster.
- An untitled one-on-one with an app has a `28:` peer, and `lastMessage.properties.fromAppMetadata.name`
  names the app. Its `fromAppMetadata.id` is not the `28:` id.
- `isSticky` did not consistently match the Favorites section (2026-09-29).

**Connector naming rules** (decided 2026-10-01): an explicit meeting type wins over `isOneOnOne`, and a
complete roster of more than two members is never a one-on-one. An app's name names a chat only when the
chat is untitled, its complete roster is the user and one `28:` peer, and that peer sent the latest message.
An untitled group is named from its complete roster without the user: names sorted case-insensitively,
joined with ` | ` up to 87 characters (the first cut at 80 with an ellipsis if none fits), then `+N` for
every participant not shown, the user included.

### Sidebar order

- Live, 2026-09-29: the raw `chats[]` order differed from the Teams sidebar. Sorting by descending
  `lastMessage.originalArrivalTime` matched 18 of the first 20 visible rows.
- Source, 2026-10-11: the sidebar is fed by a GraphQL query, `DataResolversChatChatListWithCachedTitleQuery`,
  with separate connections for recent, pinned, request, top-fixed and suggested chats. Pinned chats carry an
  explicit `PinnedChat.order` (`reorderPinnedChats` mutation). Comparators `ACTIVITY` (last message time
  descending, then name, then creation time) and `RECOMMENDED` are defined for the Teams and Channels folder,
  not tied to recent chats. No capture holds the query, so Teams' recent-chat order is unknown.

### Other CSA routes (not used by the connector)

- `GET teams/users/me/conversationFolders` (live, 2026-10-14): `conversationFolders` (each with
  `folderType`, `id`, `name`, `version` and `conversationFolderItems` of `conversationId`, `threadType` and
  times), `conversationFolderOrder`, `folderHierarchyVersion`, `migratedPinnedToFavorites`,
  `isPartialResponse`. Folder types: `Favorites`, `RecentChats`, `TeamsAndChannels`; with
  `supportsAdditionalSystemGeneratedFolders=true` also `QuickViews`, `MeetingChats`, `MutedChats` and
  `EngageCommunities`. The catalog routes carry no folders.
- `GET /api/csa/{region}/api/v1/teams/users/me/pinnedChannels` (Capture, 2026-10-10): pinned channels.
- `teams/users/me/groupchats`, `teams/users/me/nonrecentchats`, `teams/users/anonymous[/updates]` (Source):
  contracts unknown.

## Chat Service

Raw conversations and messages. Base from `regionGtms.chatService`, prefix `/v1`, header
`Authentication: skypetoken=<Skype token>`.

### History pages

`GET /v1/users/ME/conversations/{id}/messages?view=msnp24Equivalent&pageSize=<n>[&startTime=<epoch ms>]`, the
conversation id URL-encoded. Live unless dated otherwise:

- The first page is the newest; `_metadata.backwardLink` leads to older pages. Follow it verbatim: it keeps
  its own `pageSize` (a link from a `pageSize=10` page returned 10 messages when 3 were asked for) and an
  opaque `syncState`. Consecutive pages can overlap, so de-duplicate by message id.
- `startTime` is an inclusive lower bound; `startTime=0` reads all history. `since=` and `$filter` are
  ignored.
- `endTime` did not reliably include ordinary messages at the edge, so it is not used for ranges. An equal
  `startTime` and `endTime` returns exactly the message sent at that time, in chats and channels
  (2026-10-15).
- `_metadata` can also carry `lastCompleteSegmentStartTime` and `lastCompleteSegmentEndTime`; their meaning
  is unverified.
- A message id equalled its arrival time in epoch milliseconds in every message checked. The connector does
  not rely on it.
- Teams Web opens a search hit by reading forward from about 72 minutes before it:
  `view=msnp24Equivalent|supportsMessageProperties&pageSize=200&startTime=<epoch ms>` (Capture, 2026-10-14).
- The Teams Web bundle also has GraphQL `messages(convId, replyChainId, cursor)` and
  `fetchRecentMessagesAfter`; neither takes a date range (Source, 2026-09-28).
- A meeting chat in the catalog whose roster does not include the account answers 403 with `errorCode`
  209 and a `message` holding `{"subCode":"AclCheckFailed","details":…}`. The details name the account's
  MRI and the thread (2026-10-16, local UI preview). The connector reports that the account is not a
  member and leaves out the details. Why the catalog lists such a chat (for example a meeting the account
  was invited to but never joined) is unverified.

### One message

`GET /v1/users/ME/conversations/{id}/messages/{messageId}` returns the same row history returns, with or
without `view` (live, 2026-10-15). An unknown id answers HTTP 403, not 404. In a channel, a reply's row
carries `rootMessageId` and a `conversationLink` naming its thread; Graph needs that root to read the reply
(see [Graph](#microsoft-graph-with-the-teams-client)).

### Members

`GET /v1/threads/{id}/members` returns `members[]` with `id` (MRI), `userDisplayName` and `role`. Display
names can be empty (nine of nine in one group chat, 2026-09-29); Graph's user lookup names them. The request
fails for `48:notes`.

### Conversation feed

`GET /v1/users/ME/conversations?view=mychats&pageSize=<n>`, paged by `_metadata.backwardLink`. The connector
reads it only when CSA reports a partial catalog. Live 2026-09-30: 961 rows over three pages of 500; every
CSA chat was in it, plus `48:notes`, teams, channels and service streams (`StreamOfNotifications`,
`StreamOfMentions`, `StreamOfCallLogs`, `StreamOfAnnotations`, `StreamOfGeneric`). No server-side filter by
name, type or date was found, and its order is not the sidebar's. A row carries `id`, `version`,
`lastMessage`, a `messages` URL, `properties` (`displayName`, `lastimreceivedtime`, `consumptionhorizon`) and
`threadProperties` (`topic`, `threadType`, `productThreadType`, `memberCount`, `createdat`); names can sit in
`topic`, `topicThreadTopic`, `spaceThreadTopic` or `displayName`.

### The self chat

`48:notes` is the user's notes chat: absent from CSA, present in the feed, and its history readable (live,
2026-09-30). A message's `from` is a composite string holding `8:orgid:<object id>`, which matches the
signed-in account and identifies the user's own messages without comparing names. Third-party clients use
the same id ([msgvault](https://pkg.go.dev/go.kenn.io/msgvault%40v0.20.0/internal/teams)).

### Writes (not used by the connector)

The connector sends through Graph, which returns the new id in its body. Chat Service writes, for later work
(except the soft delete, which `delete_messages` uses since 2026-10-15):

| Route | Shape | Evidence |
|---|---|---|
| `POST …/conversations/{id}/messages` | `content`, `messagetype`, `contenttype`, `clientmessageid`, `imdisplayname`, `properties`, optional `amsreferences`. The body returns only `OriginalArrivalTime`; the new id is the last segment of the `Location` header. | Capture, Source, 2026-10-01 |
| `PUT …/messages/{messageId}` | Edit: the whole message with new `content` and `properties.edittime` | Capture, 2026-10-14 |
| `DELETE …/messages/{messageId}?behavior=softDelete` | Delete; undo is `DELETE …/messages/{messageId}/properties?name=deletetime`. With the Skype token it answered 200 for the user's own message in a group chat, and history then showed empty `content` and `deletetime` | Capture, 2026-10-14; Live, 2026-10-15 |
| `PUT` / `DELETE …/messages/{messageId}/properties?name=emotions` | Add or remove a reaction: `{"emotions": {"key": "like", "value": "<messageId>"}}` | Third party |
| `POST /v1/users/ME/drafts` | Scheduled message: `draftType: "ScheduledDraft"`, `innerThreadId`, `draftDetails.sendAt` | Third party |
| `POST /v1/threads/{threadId}/files` | Thread file reference (Source, 2026-10-01); no file send called it ([sending files](#sending-files)) | Capture, 2026-10-15 |
| `GET /v1/users/ME/properties`, `GET /v1/threads/{id}/consumptionhorizons` | User properties; read horizons | Third party; Capture |

## Message content

What history rows hold, from Chat Service (and Graph for channels). Live unless dated otherwise.

### Types and fields

- Row fields: `id`, `sequenceId`, `messagetype`, `contenttype`, `content`, `from`, `imdisplayname`,
  `originalarrivaltime`, `composetime`, `clientmessageid`, `amsreferences`, `properties` (`subject`,
  `edittime`, `deletetime`, `mentions`, `emotions`, `files`, `importance`).
- User messages are `Text` or `RichText/Html`. A rich message can have `contenttype=text`, so detect HTML
  from `messagetype` as well (both spellings, `messagetype` and `messageType`, occur).
- `properties.mentions` is a JSON string of `id` and `displayName`; the HTML wraps the name in an element
  with `itemtype="http://schema.skype.com/Mention"`.
- `properties.emotions` is a JSON string of `key` and `users[]`; the key `follow` marks followers, not a
  reaction.
- A quoted reply is a `<blockquote itemtype="http://schema.skype.com/Reply" itemid="<message id>">` holding the
  quoted author and text, followed by the reply; forwards use `…/Forward`. 131 of 2,833 recent HTML messages
  had one (2026-10-15).
- Line breaks (2,833 HTML messages of 40 chats, 2026-10-15): adjacent paragraphs in 217, empty paragraphs in
  126, `<br>` in 64, a `<br>` between paragraphs in 17, two `<br>` in a row in 11. Teams shows consecutive
  paragraphs as consecutive lines.
- A message sent through Graph is stored as `<p>…</p>` paragraphs joined by CRLF, `RichText/Html`, with
  `properties.s2spartnername: teamsgraph` (2026-10-14).

### Edits and deletes

Live, 2026-10-14. Teams keeps no readable copy of replaced or deleted text:

| Message | `content` | Properties |
|---|---|---|
| Edited | the newest text only | `edittime` |
| Deleted | empty (or `messagetype: MessageDelete`) | `deletetime` |

Search v2 dropped the deleted message and the edited message's old text within minutes.

### Images and emoji

- A pasted image is `<img itemtype="http://schema.skype.com/AMSImage" itemid="<AMS object id>"
  itemscope="png" src="https://<AMS host>/v1/objects/<id>/views/imgo">`. It is not in `properties.files`;
  `amsreferences` lists the same object ids. 65 of 67 had `itemscope="png"`, two none (2026-10-15). See
  [AMS](#ams-async-media-service).
- In channels (Graph) a pasted image's `src` is a Graph `hostedContents/<id>/$value` URL, and its `itemid`
  is the AMS object id, not the hosted-content id.
- Emoji: `<img itemtype="http://schema.skype.com/Emoji">` with the emoji in `alt` and its name in `title`,
  either on the Teams CDN or as a `data:image/png` URI.
- GIFs: `itemtype="http://schema.skype.com/Giphy"` or a `*.giphy.com` source, with an `alt`.

### Shared files

`properties.files` is a JSON string (2026-10-15: 27 files in 21 of 1,682 messages). Each file has
`fileName`, `fileType`, `itemid`, `version`, `@type` (`http://schema.skype.com/File`) and `fileInfo` with
`shareUrl`, `fileUrl`, `siteUrl` and `serverRelativeUrl`, and either `itemId` or `shareID` (a spelling
variant). Graph channel messages carry `reference` attachments instead.

### Meeting and membership events

Meeting chats hold service records that history returns as messages (12 meeting chats, 123 records,
2026-10-15):

| `messagetype` | Count | Content |
|---|---|---|
| `Event/Call` | 103 | XML: a started meeting has `meetingDetails` (`organizerUpn`, `meetingType`, `iCalUid`, `hasEphemeralTranscript`, `isRecapEnabled`, `instanceDetails` with `startTime`, `endTime`, `subject`); an ended one has `<ended/>` and `partlist/part` (`identity`, `name`, `displayName`, `duration` in seconds) |
| `ThreadActivity/MemberJoined`, `MemberLeft` | 7 | JSON: `eventtime`, `initiator`, `members[]` with `id`, `friendlyname` (sometimes missing), `meetingMemberType` |
| `ThreadActivity/AddMember` | 4 | XML: `eventtime`, `initiator`, `target`, `rosterVersion`, `detailedtargetinfo` (an id only) |
| `ThreadActivity/TopicUpdate` | 4 | XML: `eventtime`, `initiator`, `topicupdate/value` |
| `ThreadActivity/MeetingPolicyUpdated`, `AddCustomApp`, `DeleteMember` | 5 | not read (`ChatEnabled` marks meeting chat switched on) |

`eventtime` is epoch milliseconds. In one meeting chat, 8 of 15 consecutive messages were such records.
Graph exposes the same events as `systemEventMessage` with `eventDetail`; Azure Communication Services
documents the same `addmember` and `topicupdate` envelopes.

### Recording cards

A meeting recording is a `RichText/Media_CallRecording` message whose content is one `URIObject` of type
`Video.2/CallRecording.1` (25 chats, 91 cards, 2026-10-15):

- `RecordingStatus` (`Initial`, `ChunkFinished`, `Success`), `SessionEndReason`, `ChunkEndReason`;
- `Title`, and an `<a href>` to the OneDrive or SharePoint copy (a `/:v:/` sharing link) on 23 cards;
- `OriginalName` (the `.mp4` name), organizer, initiator and call ids, iCal uids, `RecordingStorage`,
  `Identifiers`;
- `RecordingContent` items `amsVideo`, `amsTranscript`, `rosterevents` and thumbnails on the AMS v2 host,
  and `onedriveForBusinessVideo` and `onedriveForBusinessTranscript` on SharePoint.

The type name carries no `Html`, so the card is plain text to a reader that keys on the type.

**Transcript identifiers in the card** (live, 2026-10-16): the `onedriveForBusinessVideo` item carries the
recording's `driveId` and `driveItemId`. The `onedriveForBusinessTranscript` item carries the same drive and
item ids and the transcript's `id`, which equals the id of the default transcript that `media/transcripts`
lists. The card's `amsTranscript` item has no drive ids. Transcripts
also appear as `RichText/Media_CallTranscript` messages. Such a message is a notice: its content is a JSON document of
  identifiers and storage flags (`scopeId`, `storageId`, `callId`, `isExportedToOdsp`, and others). The connector
  renders it as `[Transcript saved]` and never shows the document (2026-10-16).

## AMS (Async Media Service)

Pasted images. Hosts from `regionGtms.ams` (`*.asm.skype.com`) and `regionGtms.amsV2`
(`*.asyncgw.teams.microsoft.com`). Pasted images seen on 2026-10-15 were on the first host (67 of 67); the
second host is checked below.

Teams Web authenticates image loads with a cookie (it posts the Skype token to `<AMS host>/v1/skypetokenauth`;
Source), but the header alone works. Live 2026-10-15, one self-chat image and one posted by someone else:

| Request (`Authorization: skype_token <token>`) | Result |
|---|---|
| `GET /v1/objects/{id}` | 200 JSON: `id`, `type` (`pish/image`), a generated `filename`, `creation_date`, `expiry_date`, `geo`, `tenant_id`, and `contents` with one entry and `length` per view |
| `GET …/views/imgo` without `Authorization` | 401 |
| `GET …/views/imgo` | 200 `image/jpeg`, about 25 KB: the preview the message references |
| `GET …/views/imgpsh_fullsize` | 200 `image/png`: the full image, as long as `contents.imgpsh` |
| `GET …/content/original` | 404 for pasted images |

- Every image listed `imgo`, `imgpsh_fullsize` and `imgpsh_fullsize_anim`; some also `img_small`, `imgpsh`
  and `imgpsh_thumbnail_sx`. The bundle names more views (`imgt1`, `imgpsh_mobile_save`, …).
- Teams Web saves every image as `MicrosoftTeams-image.png` (Source), so the connector names images by
  object id instead.

**Second host, live 2026-10-15** (read-only, the 25 most recent chats; no ids, bodies or bytes kept). The 172
pasted images in those chats: 171 on the first host, 1 on the second. With `Authorization: skype_token <token>`:

- `views/imgpsh_fullsize` on the second host answered 200 `image/png` for all 10 objects first posted on the
  first host, and 200 `image/jpeg` for the one image posted on the second host;
- the same view without credentials answered 401 on the second host;
- `GET /v1/objects/{id}` on the second host answered 200 for all 10 objects.

The connector's AMS reader downloaded the second-host image in full (200,914 bytes, JPEG). So the Skype token
reads pasted-image views on either host.

**Uploads (not used by the connector; Capture 2026-10-15, one pasted PNG in a one-on-one).** Teams Web
uploads a pasted image to AMS before sending and references it from the message:

1. `POST <amsV2 host>/v1/objects/` with JSON `{"type": "pish/image", "permissions": {"<conversation id>":
   ["read"]}, "sharingMode": "Inline", "filename": "MicrosoftTeams-image.png"}` → 201 `{"id": …}`.
2. `PUT …/v1/objects/{id}/content/imgpsh` with the raw bytes → 201.
3. The message is a Chat Service post:
   - the `<img>` `src` is on the **first** host: `https://<ams host>/v1/objects/{id}/views/imgo`;
   - the tag carries `itemtype="http://schema.skype.com/AMSImage"`, `itemid`, `itemscope="png"`, `width`,
     `height` and `data-file-size`;
   - `amsreferences` lists the id, `properties.files` is `[]`, and `properties.blurHash` holds a
     placeholder.

The capture strips credentials, so the upload's auth isn't shown; Source points to the Skype token.

## Unified Presence

`POST {presenceUPS}/v1/presence/getpresence/` with the `teams.spaces` bearer and a JSON list of MRIs. Live,
2026-10-15:

- Answers one row per MRI: `mri`, `etag`, `etagMatch`, `status`, and `presence` with `availability`,
  `activity`, `deviceType`, `lastActiveTime`, `calendarData`, `workLocation`, `forcedAvailability`,
  `capabilities` and `sourceNetwork`. The connector keeps the first four.
- `lastActiveTime` is not always present (2026-10-16, observed in the local UI, not a dedicated probe): one
  peer read Away came with a time, and the same peer read Available minutes later came without one. This
  matches Teams showing "last seen" only for people who are not active; the exact rule is unverified.
- The global host (`unifiedPresence`) answers 301 to the regional one; the Skype token gets 401 on both.
- Teams Web takes its base from `regionGtm("unifiedPresence")` (or an AFD route) and also subscribes at
  `v1/pubsub/subscriptions/` (Source; Capture of `/ups/{region}/…`, where a global `/ups/global/` alias
  answered 404).

## Microsoft Graph with the Teams client

What the Teams first-party client's Graph token can do, which no documentation states, and Graph behavior
that differed from the documentation. Live.

### Grants (2026-10-01)

| Capability | Result |
|---|---|
| Current user and photo; joined teams, channels, channel members | works |
| Channel messages and replies | works |
| OneDrive and `driveItem` search | works |
| Send to the self chat, a one-on-one and a group chat (2026-10-14) | works (HTTP 201) |
| List chats, or read chat metadata, members or history | refused: no `Chat.ReadBasic`, `Chat.Read` or `Chat.ReadWrite` |
| Read back or edit a sent chat message | refused |
| Presence | refused: no `Presence.Read` |
| Search `chatMessage` (v1.0 and beta) | refused |

Asking for `Chat.Read` explicitly returned `AADSTS65002`. The token held `Files.ReadWrite.All` and
`Sites.ReadWrite.All`. This table is why chats come from CSA and Chat Service while Graph serves channels,
people, sending and files ([requirements](requirements.md#4-backend-choice)).

### Behavior beyond the documentation

- **Sent message ids (2026-10-14):** the id Graph returns is the id Chat Service history shows, and it is
  the send time in epoch milliseconds (`2026-08-14T12:30:05.123Z` → `1786710605123`). Escaped markup stays
  literal.
- **HTML sends (2026-10-15):** Teams stored every element sent (`h2`, `p`, `strong`, `em`, `u`, `code`,
  `pre`, `ul`, `ol`, `li`, `blockquote`, `a`, `table`, `tr`, `td`, `br`) and added a `tbody`.
- **Channel replies by id (2026-10-15):** `GET /teams/{team}/channels/{channel}/messages/{reply id}` answers
  404; `…/messages/{root id}/replies/{reply id}` answers 200. Find the root through Chat Service
  ([one message](#one-message)).
- **Hosted images (2026-10-15):** `$expand=hostedContents($select=id,contentType)` is refused with 400;
  `$expand=hostedContents` works. `GET …/hostedContents/{id}/$value` returns the image.
- **`$batch` (2026-10-15):** sub-request paths are percent-decoded, so channel ids with `:` and `@` must be
  encoded in them.
- **File and image sends (2026-10-15):** OneDrive upload, `driveItem/invite` and chat messages with
  `reference` attachments or `hostedContents` all work with this token
  ([sending files](#sending-files)).
- **`/me/joinedTeams` (2026-10-12):** `$top` is refused with 400 (`Query option 'Top' is not allowed`).

## Search v2 and Substrate suggestions

### Message search

`POST https://outlook.office.com/searchservice/api/v2/query`, `m365.search` bearer,
`X-AnchorMailbox: Oid:<object id>@<tenant id>`. The connector sends:

```json
{"Cvid": "<guid>", "LogicalId": "<guid>", "Scenario": {"Name": "owa.react"},
 "TimeZone": "UTC", "TextDecorations": "Off",
 "EntityRequests": [{"EntityType": "Message", "ContentSources": ["Teams"],
   "Query": {"QueryString": "<KQL>", "DisplayQueryString": "<KQL>"},
   "From": 0, "Size": 25, "Sort": [], "PropertySet": "Optimized"}]}
```

Teams Web posts to `teams.microsoft.com/searchservice/api/v2/query` with camel-case keys,
`scenario.Name: powerbar`, dimensions such as `QueryType: Messages`, and a `PUID:` anchor (Capture,
2026-10-14). Live 2026-10-14 and 2026-10-15 unless stated:

- `queryString` is KQL: a user's `NOT IN THE CHAT` applied `NOT` as an operator.
- Matching is by whole word and word prefix: `alpha7` finds `alpha7q2`, `pha7q2` finds nothing. A quoted
  phrase matches in order. Teams Web gets the same results.
- Scope by conversation: `clientthreadid:<id>`; several as `(clientthreadid:<a> OR clientthreadid:<b>)`.
- Dates: `sent >= <ISO UTC>` and `sent <= <ISO UTC>` include a message sent exactly then, `sent <` excludes
  it; bounds are compared to the millisecond (`sent <= <whole second>` excluded a message a fraction later).
- Teams Web's filters (Capture): chats only by `filter: {term: {conversationType: "OneToOne"}}` and
  `NOT (Extension_SkypeSpaces_ConversationPost_Extension_ThreadType_String:(topic OR space))` (channels are
  `topic OR space`); deleted messages left out by `NOT (isClientSoftDeleted:TRUE)`; a person filter adds the
  person's email as a term.
- Paging: `from` 0, 25, 50 with `size` 25, the same `cvid` and `logicalId`. `Total` is approximate (834, 833,
  837 across pages); `MoreResultsAvailable` says whether to continue. No sort is offered.
- A query matching nothing returns a result set with `Total: 0`, `MoreResultsAvailable: false` and no
  `Results` key.
- A hit's `Source` holds `InternetMessageId` (the history message id), `ClientThreadId` (the conversation
  id), `ClientConversationId` (for a one-on-one with a `;messageid=<id>` suffix), `ConversationId` (an
  Exchange id), `DateTimeSent`, `Sender`, `Subject`, `Preview` and `HitHighlightedSummary`.
- New, edited and deleted messages showed in results within about six minutes. Scoped searches reached
  messages months old. A query of about 880 characters was accepted.

### Chats by name and member

Search v2 with `entityType: "Chat"`, `contentSources: ["Teams"]` and a plain name or email (live,
2026-10-14 and 2026-10-15): 14 rows, all stored catalog chats, but only named group and meeting chats with
activity in about the last five months, never one-on-ones or unnamed groups. Each row's `Source` carries
`ThreadId`, `Name` and `MatchingMembers` (`MRI`, `DisplayName`, `GivenName`, `Surname`). A quoted name
returned nothing.

### People suggestions

What Teams Web sends as the user types (Capture, 2026-10-14; the same literals are in the bundle):

```http
POST https://substrate.office.com/search/api/v1/suggestions?scenario=powerbar
X-AnchorMailbox: Oid:<object id>@<tenant id>

{"EntityRequests": [
   {"EntityType": "People", "Size": 5, "From": 0,
    "Query": {"QueryString": "<text>", "DisplayQueryString": "<text>", "NormalizedQueryString": "<text>"},
    "Fields": ["Id", "MRI", "DisplayName", "EmailAddresses", "PeopleType", "PeopleSubtype",
               "UserPrincipalName", "ExternalDirectoryObjectId", …],
    "Filter": {"And": [{"Or": [{"Term": {"PeopleType": "Person"}}, {"Term": {"PeopleType": "Other"}}]},
                       {"Or": [{"Term": {"PeopleSubtype": "OrganizationUser"}}, … "MTOUser", "PersonalContact", "Guest"]},
                       {"Or": [{"Term": {"Flags": "NonHidden"}}]}]},
    "Provenances": ["Mailbox", "Directory"]},
   … File, Chat, Team, Channel, Text, Message, Event and App requests …],
 "Scenario": {"Name": "powerbar",
              "Dimensions": [{"DimensionName": "QueryType", "DimensionValue": "PeopleCentricSearch"}]},
 "Cvid": "<guid>", "LogicalId": "<guid>", "AppName": "Microsoft Teams", "dataSource": "personScoped"}
```

Live, 2026-10-15:

| Token | People alone | People and Chat in one call |
|---|---|---|
| `m365.search` (Outlook Web client) | 200 | 500 ("Chat entity type is not configured in response layout") |
| Teams client, `substrate.office.com` | 200 | 200 |

- `Dimensions` must be a list, with `dataSource` and the filter; sent as an object it answered 400.
- A People suggestion carries `ExternalDirectoryObjectId` and `MRI` (the object id that is one half of a
  one-on-one chat id), `DisplayName`, `UserPrincipalName`, `EmailAddresses`, `PeopleType`,
  `PeopleSubtype`, `PropertyHits`, `QueryText`, `ReferenceId`, `Text` and `Id`.
- The Chat group (Teams token only) carries `ThreadId`, `ThreadType`, `Name`, `ChatMembers`,
  `MatchingMembers`, `TotalChatMembersCount` and `LastMessageTime`, matched by member.
- Connector use: on three one-on-ones, a query of given then family name missed the chat name (stored family
  name first) in 3 of 3 and found the chat through the suggested person in 3 of 3.
- Third-party clients use `scenario=peoplepicker.newChat` with a simpler body. It answered 400 with a
  Substrate token (2026-09-29) and with `m365.search` (2026-10-15).

### Other search routes (not used by the connector)

Capture and Source, 2026-10-10 and 2026-10-11:

- `POST /searchservice/api/v2/query` also serves Teams global search for `File`, `People` and `Event`, some
  through `AnswerEntityRequests` (answers in `AnswerEntitySets`). File hits carry SharePoint pointers
  (`UniqueId`, `SiteId`, `WebId`, `ListId`, `SpWebUrl`, `Path`, `LinkingUrl`); Event hits `EventId`,
  `Subject`, `Start`, `End`, `OrganizerName`, `Attendees`, `OnlineMeeting`. No later page was captured.
- `POST /search/api/v2/query` with `EntityType=Transcript`, `ContentSources=Teams`: three 200s, one with zero
  results and two not interpretable, so transcript search is neither shown nor ruled out.
- The Meet app's `searchEvents` posts lower-camel `entityRequests` for `Event` on `Exchange` with ISO
  `startDateTime`/`endDateTime`, `sort: [{"Time": "Proximity"}]`, `from=0`, `size=30` to
  `https://substrate.office.com/search/api/v2/query`, and reads
  `EntitySets[0].ResultSets[0].Results[].Source`. Not tied to the global Meetings tab.
- `POST /search/api/v1/recommendations` with a `MeetingCatchUp` request returns
  `EntitySets[].MeetingInsights`; a `PaginationSessionId` appears, but no follow-up request using it.
- `/search/api/v1/init` and `/search/api/v2/init` exist (Source); `/search/api/v1/events` is telemetry.
- Teams Web's search token is configured with token URL `https://outlook.office.com/` and base
  `https://substrate.office.com`; Copilot routes use `https://substrate.office.com/M365.Access` (Source,
  2026-10-14).

## SharePoint and OneDrive files

Shared files live in SharePoint or OneDrive; a message names them in `properties.files`
([shared files](#shared-files)). The connector downloads them through Graph's documented `/shares` route.
Beyond the documentation (live, 2026-10-15): a SharePoint site page (`.aspx`, such as a news post) shared in
a message is refused by `/shares` with HTTP 400, so it is a link, not a file.

### Loop pages and meeting notes

Live, 2026-10-15, read-only, one meeting chat.

**The message.** When a meeting ends, a bot posts a link to the meeting's Loop notes page:
- Sender: `from` is a `28:` app id, `imdisplayname` and `properties.fromAppMetadata.name` are
  "Facilitator", and `s2spartnername` is `SMBA`.
- `properties.botMetadata.aiMetadata.botAiSkill` holds a JSON string with `eventType: TM_MeetingEnded` and
  the `callId`.
- `content` is one `RichText/Html` paragraph ("Everyone—that's a wrap…"). In it, an empty
  `<a href="…/:fl:/p/<owner>/…" itemtype="http://schema.skype.com/FluidAutoEmbedLink" itemid="<guid>">`
  carries the link; the `/:fl:/` path marks a Loop file. `properties.atp` repeats the URL.
- There is no `properties.files`, so the connector reads the message as text plus a link today.

**The page.** The link resolves through Graph `/shares/{u!…}/driveItem` with the `teams.graph` token. The
item is a `.loop` file in the meeting organizer's OneDrive (about 95 KB, `application/octet-stream`).

| `GET /shares/{share}/driveItem/content` | Result |
|---|---|
| no format | 200 binary Fluid container (`Microsoft.Prague` nodes); not readable as text |
| `?format=html` | 200 `text/html`, about 12 KB, `<meta name="GENERATOR" content="Microsoft Loop">` |
| `?format=pdf` | 200 `application/pdf`, about 22 KB |

The HTML marks blocks with `data-docparser-context` (`paragraph`, `label`, `labelname`, `labelvalues`):
- `h1` sections: the agenda with a goal, the meeting notes, and follow-up tasks.
- The tasks are one `table` with Task, Assigned to, Due date and Bucket columns, and one row per task.
- Boilerplate: a leading "AI-generated content in notes may be incorrect" note with a "Learn more" link,
  empty paragraphs, a checkbox `input` per task, and a "Task list category : N" label after each task.

The page is live: it shows its contents at read time, including later edits. Reading it needs
access to the organizer's file, which an attendee had in this case.

**How Teams Web renders the card** (Capture, 2026-10-15). Each meeting occurrence has its own page, named
`<meeting> <date>.loop`, in a `Meetings` folder of the organizer's OneDrive. For each page, Teams Web:
1. reads the item and its sharing (`/shares/{share}`, `GetSharingInformation`);
2. checks `GET {site}/_api/v2.1/drives/{drive}/items/{item}/opStream/capabilities?$select=canWrite,canPrint,canExtract,canExport,supportsCLP`
   (all true here except `supportsCLP`);
3. loads the page as a live Fluid document: `opStream/snapshots/trees/latest`, `opStream` ops,
   `opStream/joinSession` and a push socket.

That is a collaborative editing session; the connector's HTML conversion needs none of it. The connector
reads meeting notes through `?format=html` since 2026-10-15 (exports and downloads only).
`canExport`/`canExtract` are where a page would say it forbids export; whether Graph's conversion honors
them is untested. The connector does not check them, by design (decided 2026-10-16).
Loop components that people paste into a chat may use the same `FluidAutoEmbedLink` embed; untested.

**Live, 2026-10-16** (the standup's rundown message, the connector's read, and a browser capture of a reload):
- The browser loads the page as a live Fluid document from SharePoint: `POST …/_api/v2.1/drives/{drive}/items/{item}/opStream/snapshots/trees/latest` (`application/ms-fluid`; two responses of about 97 KB and 149 KB, both containing the page's wording). The HAR strips credential headers, so the capture does not show which credential it used. The connector has no SharePoint-audience token, so it does not use this route.
- The connector's read, `GET /shares/{share}/driveItem/content?format=html` with the Teams client's Graph token, answered with a redirect to a regional Fluid media host, `francecentral1-mediap.svc.ms` for this tenant. The browser capture has no request to that host. The read followed the redirect once, without credentials, once the host was named in the notes media-host setting (`TEAMS_CONNECTOR_NOTES_MEDIA_HOSTS`; the default is this tenant's region); the converted page then parsed into headings and bullets, and the AI disclaimer was dropped as before.
- The rundown's sender is the Facilitator app: one sender id across its three messages in the standup chat. `config.NOTES_SENDER_IDS` allowlists that Microsoft app's id, so a link from it is read whether or not the meeting-ended record is present. The rundown message has the embed but no meeting-ended metadata.

### Sending files

Teams Web, Capture 2026-10-15: a one-on-one and a four-member group chat, a repeated name, an 8 MB file and an
existing OneDrive file. Channel sends were not captured. The OneDrive calls use a Teams Web client
(`5e3ce6c0-2b1f-4285-8d4b-75ee78787346`) bearer for `https://<tenant>-my.sharepoint.com/.default`. The post
uses Chat Service. `{my site}` is `https://<tenant>-my.sharepoint.com/personal/<user>`. Every send took
three steps:

1. **Upload** when the file is attached: `PUT {my site}/_api/v2.0/drive/root:/Microsoft%20Teams%20Chat%20Files/{name}:/content?@name.conflictBehavior=rename&$select=*,sharepointIds,webDavUrl`
   with the raw bytes. 201 returns the drive item: `id`, `eTag`, `webUrl`, `parentReference.driveId`,
   `sharepointIds.listItemUniqueId`. A repeated name became `name 1.txt`, then `name 2.txt`. The 8 MB file
   went up in one PUT (about 3 s), so the switch to `StartUpload`/`ContinueUpload` (Source) happens above
   that size.
2. **Grant**, when Send is pressed:
   - Teams first reads the tenant default with `GET {my site}/_api/v2.0/sites/root/items/{listItemUniqueId}/driveItem/commands`
     and `Prefer: audienceType="chat"`. The reply was `invite.defaultScope: users` and `defaultType: edit`
     ("People in this chat with the link can edit"). Source also knows `organization`, `anyone` and
     `existing`.
   - It then sends `POST …/driveItem/invite` with `Prefer: ReturnSpecificErrorCode,getDefaultLink` and
     `{"recipients": [{"objectId": "<member>"}, …], "roles": ["write"]}`. The recipients are every member
     except the sender: 1 in the one-on-one, 3 in the group.
   - The reply is one permission: `id`, `shareId`, `roles`, `grantedToIdentitiesV2[]`, and `link`
     (`scope: users`, `type: edit`, `webUrl`).
   - An existing file returned its existing specific-people link, with every earlier grantee.
3. **Post**: Chat Service `POST …/conversations/{id}/messages`, `RichText/Html`.
   - `content` holds only the caption, which may be empty, with no attachment tag.
   - `properties.files` is a JSON string with one entry per file: `itemid` and `id` (the
     `listItemUniqueId`), `fileName`, `fileType`, `title`, `type`, `@type`, `version: 2`, `state: active`,
     `baseUrl`, `objectUrl`, `permissionScope: SpecificPeople`, `sharepointIds` (with `driveId`) and
     `fileInfo`.
   - `fileInfo` holds `itemId` (the drive item id), `fileUrl`, `siteUrl`, and `shareUrl` and `shareId`
     (the invite's `link.webUrl` and permission `id`).

Not seen in any send: `createLink`, `POST /v1/threads/{id}/files`, or Graph. The **Manage access** dialog is
SharePoint's own page (`sharedialog.aspx`, SharePoint REST `GetSharingInformation` and `ShareLink`), not
part of a send.

**Through Graph, live 2026-10-15; the connector's route.** Teams client `teams.graph` token. Three sends each to two one-on-one
chats gave the same results in both. One send went to a group chat, and its message was deleted right
afterwards. Each write was made once.

| Step | Request | Result |
|---|---|---|
| Upload | `PUT /me/drive/root:/Microsoft Teams Chat Files/{name}:/content?@microsoft.graph.conflictBehavior=rename` | 201 drive item. Clashes renamed as in Teams Web. A missing folder is created: the first PUT into a new folder answered 201. The largest single PUT accepted was 262,143,999 bytes (250 MiB minus one). Exactly 250 MiB answered 499 `clientDisconnected`, and anything larger answered 400 `invalidRequest`, both only after the whole body had been sent (about 20 s). |
| Grant | `POST /drives/{drive}/items/{item}/invite`, `{"recipients": [{"objectId": …}], "roles": ["write"], "requireSignIn": true, "sendInvitation": false}` | 200. Without a `Prefer` header: a direct permission (`grantedToV2`) and no link. With `Prefer: getDefaultLink`: the same `users`/`edit` link Teams Web gets, with `shareId`. In a four-member group chat, one invite for the three other members returned one link with three grantees. |
| Post | `POST /chats/{id}/messages`: `<attachment id="{eTag GUID}">` in the HTML, plus `attachments[]` entries of `contentType: reference` with `contentUrl` (the item's `webUrl`) and `name` | 201. Two files in one message worked. |

What Chat Service stored:

- **Files:** `properties.files` holds one entry per file, with the same keys as a Teams Web send.
  - `state` and `fileChicletState.state` are `reference`; `fileChicletState.serviceName` is `teamsGraph`.
  - `permissionScope` and `fileInfo.shareUrl`/`shareId` are `null`. A Graph attachment has no field for
    them.
  - `content` keeps only the caption, without the attachment tag.
- **Inline image:** `hostedContents` (`<img src="../hostedContents/1/$value">`, a base64 PNG) was stored as
  a pasted image would be: an `AMSImage` on the first AMS host (`views/imgpsh_fullsize`), listed in
  `amsreferences`, with a `blurHash`.

Graph has no route for the chat-audience default Teams Web reads (`driveItem/commands` with
`audienceType="chat"`). The invite's `getDefaultLink` returned the same specific-people edit link on this
tenant, but nothing shows the two always agree.

Both recipients opened every file, saw normal file cards and previews despite the empty `permissionScope`,
and received no invitation email (`sendInvitation: false`). Confirmed by the recipients the same day.

**More Graph sends, live 2026-10-15** (the self chat, and one message to a one-on-one chat; each write
once):

- **Inline images:**
  - `hostedContents` accepts only `image/png` and `image/jpeg` (or `image/jpg`). GIF and SVG answered 400
    "Unsupported content type in hostedContent".
  - Two images in one message became two AMS images.
  - The limit is on the whole request, at about 4 MiB: a 3.06 MB PNG (a 4.08 MB request) was accepted,
    and a 4.47 MB request answered 400 "maximum number of bytes allowed to be read from the stream has
    been exceeded". Base64 adds a third, so a message's images total under about 3 MB.
- **Images and files together:** one message with `hostedContents` and a `reference` attachment worked.
  The recipient of the one-on-one send saw the JPEG inline next to the file card.
- **Self chat:** files (`reference` attachments, with no share step, since nobody else is in the chat) and
  inline images both worked in `48:notes`.
- **Upload session:** `POST /me/drive/root:/{folder}/{name}:/createUploadSession` with
  `{"item": {"@microsoft.graph.conflictBehavior": "rename"}}` answered 200 with `uploadUrl`,
  `expirationDateTime` (about 15 minutes ahead) and `nextExpectedRanges`.
  - The `uploadUrl` is on the user's OneDrive SharePoint host and needs no `Authorization` header.
  - A 300 MiB file went up in 10 MiB `PUT`s with `Content-Range` in about 47 s. Each chunk answered 202
    with `nextExpectedRanges`; the last answered 201 with the drive item (`id`, `eTag`, `webUrl`,
    `parentReference.driveId`, `name`), as a single PUT returns.
  - Chunks must be multiples of 320 KiB.

## Meeting recaps and transcripts

Not used by the connector; the starting point for roadmap P2.2f.

### Transcripts

- A recording's transcripts are listed at `{site}/_api/v2.0/drives/{drive}/items/{item}/media/transcripts`
  (the default flagged `isDefault`). Content is `…/media/transcripts/{id}/content` (`?format=docx`, or `?is=1`
  for VTT), or `{site}/_api/v2.1/drives/{drive}/items/{item}/versions/current/media/transcripts/{id}/streamContent`
  (Source, 2026-10-14).
- Meeting chats carry a `URIObject` referencing that last route; its two extra segments are
  `versions/current`. Recording cards carry an `amsTranscript` item on the AMS v2 host
  ([recording cards](#recording-cards)). No reference was followed (2026-10-10).
- Teams Web also loaded `GET /_api_cached/v2.1/drives/{drive}/items/{item}/cdnmedia/transcripts` with keys
  `cTag`, `P1`–`P4`, `isformatjson`, `transcriptkey`, `applyhighlights`, `applymediaedits`, `kid` (Capture,
  three 200s). Older families: AMS `/v1/objects/{id}/views/transcript[/status]` and
  `/MeetingIntelligence/TranscriptService/storage/{storage}/scope/{scope}/` (`isChannel=true` for channel
  meetings) (Source).
- In the Recaps app, both known recaps showed a searchable transcript tab; for one, Download was disabled by
  the meeting's permissions (live UI, 2026-10-10). Viewing is not permission to download.
- Graph's own transcript API needs `OnlineMeetingTranscript.Read.All`, which requires admin consent.

**Live, 2026-10-16, one recording, read-only with the Teams client's Graph token** (no transcript text kept):
- `GET /shares/{share}/driveItem` resolves the recording (`video/mp4`).
- `GET /v1.0/drives/{drive}/items/{item}/media/transcripts` answers 200 with `value[]`, and `beta` the same.
  This works **without** `OnlineMeetingTranscript.Read.All` and without a SharePoint token.
- The SharePoint host `_api/v2.0/.../media/transcripts` answers 401 to the Graph token, so Teams' private
  SharePoint route needs its own token.
- A transcript's fields: `id`, `displayName`, `cTag`, `size`, `languageTag`, `transcriptType`, `source`,
  `isAutoGenerated`, `isDefault`, `isVisible`.
- `GET …/media/transcripts/{id}/content` answers 200 `text/vtt` (also 206 for a byte range). The file is WebVTT:
  66 cues over about 5 min 42 s, with no `<v>` speaker tags and no `NOTE` blocks.
  The VTT cues hold only an id, the timing and the text, with no speaker.
- `?format=json` on the same route answers 200 `application/json`, about 20 KB for that meeting:
  - `entries[]`: `id`, `speechServiceResultId`, `text`, `rawText`, `speakerId`, `speakerDisplayName`,
    `confidence`, `startOffset`, `endOffset`, `hasBeenEdited`, `roomId`, `spokenLanguageTag`. 41 entries came
    from 4 distinct speakers, matching the speaker count on the Teams recap card.
  - `events[]`: `id`, `eventType`, `userId`, `userDisplayName`, `startOffset`.
  - So speaker names come from the service, not from the client. `?format=docx` answered 400.

**Finding the meeting behind a video** (live, 2026-10-16, one account):
- A search hit's `createdDateTime` is the recording's start: 14:00:21 UTC for the standup, and its recording
  card was sent six minutes later, at 14:06:23.
- `createdBy.application.displayName` is `Messaging Async Media` for some recordings and absent for others,
  so it does not identify meetings.
- `media/transcripts` answers with a transcript for videos that are not meetings too, so a transcript does
  not identify one either.
- Search v2's Message index does not find a recording card by its file name, nor by the word "Recording". A
  meeting's title matches 87 messages across many meetings, so it cannot locate one card.
- The connector therefore verifies a video by reading the chats active from an hour before its creation,
  quietest first, for a recording card naming its drive item, at most 25 chats per video.

**Searching transcript text** (2026-10-16):
- **Search v2:** `EntityType: Transcript` with `ContentSources: ["Teams"]` answered 200 with no result set
  (live). Teams Web sends the same entity with `Scenario: TranscriptFQ`, query `*`, `Fields`, `HitHighlight`
  and `SupportedResultSourceFormats`, and got `Total: 0` (Capture). `Message` with a `Transcripts` source is
  refused with 400.
- **The recap's transcript search box is client-side** (Capture): typing a term sent no request. The tab
  loads the transcript once from `{site}/_api_cached/v2.1/drives/{drive}/items/{item}/cdnmedia/transcripts`
  (keys `cTag`, `P1`–`P4`, `isformatjson`, `transcriptkey`, `applyhighlights`, `applymediaedits`, `kid`;
  the body is encoded).
- **Graph search finds recordings by spoken words** (live, `teams.graph` token, no admin consent):
  `POST /v1.0/search/query` with `entityTypes: ["driveItem"]` and `filetype:mp4` plus the terms.
  - An exact phrase from one transcript returned exactly that recording.
  - A single common word returned 1,375 recordings.
  - Each hit has `resource` (the recording `driveItem`) and `summary`, a snippet of about 140 characters with
    `<c0>` highlights but no speaker or offset.
  - A hit may match the file name instead of the speech.
  - This matches Microsoft's description of Microsoft Search indexing transcribed recordings by what was said.

### Meeting summary

Live, 2026-10-16, one meeting with a transcript and a summary, read-only with the connector's `m365.search`
sign-in (the summary text is not kept):

- `POST https://substrate.office.com/search/api/v1/recommendations?setflight=AiTasksV2,AiNotesV2,AllowResultsWithoutMeeting,PeopleMentionsV2`
  answers 200 with the summary. The body is `{"entityRequests": [{"context": {"entityId": …}, "queryParameters":
  [{"entityType": "MeetingCatchUp"}]}], "scenario": {"name": "MeetingCatchup"}}`. Teams Web's capture of the same
  call has this shape. Its captured request carried no `X-AnchorMailbox`; the connector sends one, as its other
  substrate calls do, and the answer matched the capture.
- The summary is `EntitySets[0].ResultSets[0].Results[0].Source.MeetingSummary`, a JSON document, not plain text
  (corrected 2026-10-16; an earlier note said plain text). Its `KeyTopics` list holds, per topic, a `Headline`, a
  `Summary` and `DetailedSummaries` (`topic` and `text` pairs), with `Citations` (utterance ids) and model fields
  alongside. About 2,300 characters for the probed meeting. The same name under `InsightProcessingStates` is a
  processing state (`ProcessedSuccessfully`), not the text.
- The entity id is `SPO_` + the base64url of the site, web and library GUIDs as comma-joined text + `_` + the
  recording's drive item id. The GUIDs come from the drive id: `b!` + the base64url of three 16-byte blocks in
  little-endian GUID byte order, in the order site, web, library. The id built from the card's `driveId` and
  `driveItemId` matched the captured id.
- Teams Web's client code (a saved copy of its scripts, captured 2026-10-14) builds the same id and sends the
  same route and scenario.
- In the same probe, the web client's other body shape (`Context.EntityId`, scenario `MeetingCatchUp.MeetingRecap`)
  answered with an upstream failure, so the connector sends the captured shape.
- Limits: the route is private and undocumented, and its flights are Teams Web's and may change. A meeting
  without a summary gets an answer without one. One meeting on one account is verified; reassess per deployment
  (roadmap A1). The Recaps route `meetingcatchup/multilingual/aicontent` below answers related content with
  `MeetingInsights` and is not used.

### Recaps and meeting resources

Capture, 2026-10-10 (Recaps app); all HTTP 200:

| Route | Shape |
|---|---|
| `GET /api/mcps/{region}/user/meetings/contents?startDateTime&endDateTime&includeTypes&top` | Meeting contents by window; `includeTypes` seen: `MeetingChat`, `MeetingStage`, `Notes`, `Recording`, `TranscriptV2`. `value`, `paginationInfo.hasNextPage` (never advanced). |
| `GET /api/mcps/{region}/contents/?threadId=…` | A meeting's resources, keyed by `threadId` with `recapCallId`, `iCalUid`, `organizerId`, `organizerEmail` or `isSeriesMeeting`. Records: `id`, `type` (`AISummary`, `MeetingStage`, `Recording`, `TranscriptV2`, `notes`), `subType`, `mcoResourceId`, `metadata` (including `driveId`, `driveItemId`, `transcriptId`, `threadId`, `iCalUid`, `webUrl`), `addedBy`, `additionDateTime`. |
| `POST /api/mcps/{region}/meetingcatchup/multilingual/aicontent` | `EntityRequests`, `Flights`, `Scenario`, `TargetLanguage` → `EntitySets[].MeetingInsights` (recording, summary, points of interest, speakers). |
| `GET /api/mcps/{region}/collab/readcollabobject/V2/{key}` | Collaboration-object metadata; the key's composition is unknown. |
| `POST https://graph.microsoft.com/v1.0/drives/{drive}/items/{item}/preview` | Preview of a recap artifact; its `driveId`/`driveItemId` came from the MCPS metadata. |

Adjacent calls in the same session: `GET /api/teamsevents/{region}/beta/events/{id}` (event detail),
`GET /api/mt/part/{region}/v2.0/me/calendars/events/{id}`, `GET /api/apps/part/{region}/beta/chats/{id}/tabs`,
SharePoint `media/visualSegmentFrames/{frame}/content` (video frames), `cdnmedia/thumbnails`, and a
`PUT /api/mt/part/{region}/v1/schedulingService/meetings` write that was not replayed.

## Middle Tier

Not used by the connector: Graph names people and serves photos. Base `regionGtms.middleTier`, bearer
`teams.spaces`.

- `POST /beta/users/fetch?isMailAddress=false&enableGuest=true&skypeTeamsInfo=true` with a JSON array of MRIs
  returns `value[]` with `objectId`, `mri`, `displayName`, `userPrincipalName`, `email`, `jobTitle`,
  `userLocation`, `userType`, `department`, `companyName`, `givenName`, `surname`, `phones`, `type` and
  `skypeTeamsInfo` (live 2026-09-29: it named a one-on-one peer CSA left unnamed; Capture 2026-10-10).
  `fetchShortProfile` takes the same input and returns fewer fields. Some responses use `Value`.
- `POST /beta/users/fetchfeaturesettings` with `[{"Mri": …}]` returns per-entry `status` (Capture).
- Photos: `GET /beta/users/{user id}/profilepicturev2/{mri}?displayname&size` (`HR64x64` seen) and
  `…/mergedProfilePicturev2?size&usersInfo` return image bytes (Capture).
- Other routes in the bundle (Source only, contracts not captured): `fetchFederated`,
  `fetchFederatedByEmails`, `fetchForSharedChannel[s]`, `users/me`, `users/{upn}`; People Picker
  `users/search` and `searchV2` (caller-owned JSON body; 403 means search is forbidden), `searchUsers`
  (`{emails, phones, searchKeyWord}`), `searchExternalUsers`, `{query}/externalsearch[v3]`, `scd/lookup`,
  `users/me/lookup/phones`; profile pictures and picture policy; `{mri}/statementUrl`; and the People Profile
  Service's `beta/workingSet/people` (`maxSize`, `maxPageSize`, `skipToken`, `deltaToken`) under
  `regionGtms.peopleProfileService`.

## Teams Web client behavior

Source unless stated; module numbers change between builds.

- **Opening a search hit:** the result's `primaryAction.navigationId` is built from
  `replyChainId;ClientThreadId;DateTimeSent` and dispatched to `chat.chatById` (with `messageId`,
  `dateTimeSent`, a `searchSessionId` and highlight text) or `channel.channelMessagesById`; a deep link uses
  `focusOnLoad=DeeplinkedMessage`. The history read that follows is in [history pages](#history-pages)
  (Capture, 2026-10-14). The UI result id is `InternetMessageId`.
- **Chat-by-member search** (`chats_by_members_search` in telemetry) loads from a chunk the bundle did not
  contain (2026-10-14).
- **Local state:** Teams Web keeps conversation state in IndexedDB stores named
  `Teams:conversation-manager:…` and `Teams:conversation-folder-manager:…`
  ([teams-web-chat-exporter](https://github.com/gediz/teams-web-chat-exporter), third party).

## Sources

- Teams Web production bundles collected on 2026-09-28 and 2026-10-10, and build `26091712222` (2026-10-14).
- Teams Web captures of 2026-10-10 (global search, Recaps) and 2026-10-14 (search, sends, edits, deletes),
  read outside the repository.
- Live calls through the connector's sign-in from 2026-09-29 to 2026-10-15.
- Third party: [Maxim-Mazurok/teams-api](https://github.com/Maxim-Mazurok/teams-api),
  [gediz/teams-web-chat-exporter](https://github.com/gediz/teams-web-chat-exporter), AADInternals,
  purple-teams and SkPy discussions.
- Microsoft: [Teams system messages](https://learn.microsoft.com/en-us/graph/system-messages),
  [Azure Communication Services chat messages](https://learn.microsoft.com/en-us/rest/api/communication/chat/list-chat-messages/list-chat-messages?view=rest-communication-chat-2020-09-21-preview),
  [send chatMessage](https://learn.microsoft.com/en-us/graph/api/chatmessage-post?view=graph-rest-1.0),
  [transcript permissions](https://learn.microsoft.com/en-us/graph/permissions-reference?view=graph-rest-1.0).
