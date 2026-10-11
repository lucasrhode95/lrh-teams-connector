# Teams Web Private API Reverse-Engineering Notes

These notes describe how the Microsoft Teams web/desktop clients authenticate and communicate with Teams' private backend services, based on production JavaScript, public reverse-engineering work, and limited read-only live requests.

> **Important:** these are private, undocumented Microsoft interfaces. They can change at any time. Treat all endpoint shapes and fields as implementation details, not stable contracts.

---

## Confidence legend

The notes below use four evidence levels:

- **Observed live** — successfully called against a real Teams tenant/account during this project.
- **Teams Web bundle** — literal endpoint/resource/route information found in a production Teams Web JavaScript build collected on 2026-09-28.
- **Public reverse-engineering evidence** — independently published traffic captures, open-source clients, Microsoft community/Q&A posts, or public PowerShell implementations. Useful corroboration, but potentially historical.
- **Reverse-engineered implementation** — endpoint shape used by the independent [`Maxim-Mazurok/teams-api`](https://github.com/Maxim-Mazurok/teams-api) project. Useful evidence, but not authoritative Microsoft documentation.

Where evidence conflicts, prefer live traffic from the current Teams client.

## Portability and privacy

This is an anonymized compendium of observations from a tested environment, combined
with attributed public sources. Live results describe their dated sample, client and
resource configuration; they do not establish universal grants or denials. Names,
search examples and message text in local examples and fixtures are fictional.

For another account, tenant or changed policy, follow the investigation checklist in
[the architecture guide](project-architecture.md#porting-and-rechecking-an-environment).
Keep public API contracts, private request shapes and historical findings, and rerun
the relevant calls before selecting a backend. A missing permission, expired token,
bad request shape, transport failure and empty result are different outcomes.
No private account, tenant identity, corporate endpoint or captured credential is
required to use this record.

---

# High-level architecture

Teams chat functionality is not one single API, and it does not appear to use one universal token.

The current picture is roughly:

```text
Microsoft Entra / OAuth
        |
        +--> api.spaces.skype.com token
        |        |
        |        +--> Teams AuthZ
        |        |      |
        |        |      +--> Skype token
        |        |      +--> regionGtms service discovery
        |        |
        |        +--> Middle Tier profile APIs
        |
        +--> chatsvcagg.teams.microsoft.com token
        |        |
        |        +--> Chat Service Aggregator (CSA)
        |               chat/startup state + deltas
        |
        +--> substrate.office.com token
        |        |
        |        +--> people/chat/search services
        |
        +--> SharePoint token
        |        |
        |        +--> chat file attachments
        |
        +--> AMS/IC3 token
                 |
                 +--> media upload

Skype token from AuthZ
        |
        +--> Regional Chat Service
        |      conversations
        |      messages
        |      members
        |
        +--> some AMS/media downloads
```

The **CSA startup API** returns UI-oriented chat state, but its raw array order differed from the Teams sidebar in one live comparison. Descending `lastMessage.originalArrivalTime` matched the visible order in that session; this remains an observed behavior rather than a service guarantee.

Public captures show synchronization metadata and a delta route, consistent with CSA serving state updates in addition to startup data.

## Credential matrix

Different Teams backend services expect different credential combinations.

| Service / action | Bearer audience | Additional credential | Evidence |
|---|---|---|---|
| Teams AuthZ / service discovery | `https://api.spaces.skype.com` | none | **Observed live** |
| Regional Chat Service — conversations/messages/members | none required by the known request shape | Skype token from AuthZ | **Observed live** |
| Middle Tier profile resolution | `https://api.spaces.skype.com` | none | **Observed live** for `/beta/users/fetch`; broader profile routes also appear in the web bundle |
| CSA startup/chat state | `https://chatsvcagg.teams.microsoft.com` | Skype token | **Observed live**; see the live comparison below |
| Substrate people/chat search | `https://substrate.office.com` | none | **Teams Web bundle and public reverse-engineering evidence**; contracts are not fully verified live |
| SharePoint/OneDrive attachments | tenant SharePoint/OneDrive audience | none | **Teams Web bundle and public reverse-engineering evidence** |
| AMS media upload | AMS/IC3 resource | service-specific headers | **Teams Web bundle and public reverse-engineering evidence** |
| Some AMS image/transcript downloads | route-dependent; public captures show Skype token in `Authorization: skype_token ...` | service-specific | **Teams Web bundle and public reverse-engineering evidence** |

The important distinction is:

```text
api.spaces.skype.com bearer
        |
        v
POST /authz
        |
        v
Skype token
```

These are **different credentials**.

Likewise, a CSA bearer token is a separate token again:

```text
Teams public client ID
        |
        +--> api.spaces.skype.com bearer
        |        |
        |        +--> /authz -> Skype token
        |
        +--> chatsvcagg.teams.microsoft.com bearer
                 |
                 +--> CSA requests
                        + Skype token
```

The different audiences and headers indicate that a request to one Teams service should not be assumed to use the same credentials as a request to another.

---

# Authentication flow

## Teams client identity

The working OAuth experiment uses Microsoft's Teams desktop/mobile public client ID:

```text
1fec8e78-bce4-4aaf-ab1b-5451cc387264
```

Authority:

```text
https://login.microsoftonline.com/organizations
```

Requested scope/resource:

```text
https://api.spaces.skype.com/.default
```

This successfully returns a bearer token with an audience of:

```text
https://api.spaces.skype.com
```

Observed delegated scopes:

```text
Authorization.ReadWrite
Region.ReadWrite
user_impersonation
```

### Token persistence

A typical first-party client token flow is:

```text
load local cache
    |
    v
acquire_token_silent()
    |
    +--> valid cached access token
    |
    +--> MSAL refreshes using cached refresh token
    |
    +--> if neither works: device-code login
```

The local cache file should never be committed.

Example ignored filename:

```text
.teams_token_cache.json
```

## Multiple OAuth resource audiences

Public Teams implementations and captures indicate that the first-party Teams client requests **separate tokens for separate backend resources** rather than reusing the `api.spaces.skype.com` token everywhere.

Known/relevant resource audiences include:

```text
https://api.spaces.skype.com
https://chatsvcagg.teams.microsoft.com
https://substrate.office.com
SharePoint / OneDrive tenant resource
AMS / IC3 resource
```

The live read-only test on 2026-09-29 acquired a CSA token for:

```text
https://chatsvcagg.teams.microsoft.com/.default
```

using the Teams first-party public client ID and an app-owned interactive sign-in. The resulting token, together with the Skype token, was accepted by the CSA startup route. The response and ordering comparison are summarized under [Live observations and confidence](#live-observations-and-confidence-2026-09-29).

---

# Teams AuthZ / service discovery

## POST `https://teams.microsoft.com/api/authsvc/v1.0/authz`

**Evidence:** Teams Web bundle.

Alternative direct hostname:

```text
https://authsvc.teams.microsoft.com/v1.0/authz
```

**Evidence:** observed live and Teams Web bundle.

### Request

```http
POST /v1.0/authz
Authorization: Bearer <api.spaces.skype.com access token>
Accept: application/json; ver=1.0
```

The successful live request used an empty body.

### Likely response

Observed/expected fields include:

```json
{
  "region": "emea",
  "userRegion": "...",
  "partition": "...",
  "userPartition": "...",

  "regionGtms": {
    "chatService": "https://...",
    "middleTier": "https://...",
    "ams": "https://...",
    "...": "..."
  },

  "tokens": {
    "skypeToken": "...",
    "expiresIn": 3600
  },

  "licenseDetails": {},
  "regionSettings": {},
  "regionOverride": {},
  "consumerLicenseDetails": {},
  "mri": "...",
  "skypeId": "...",
  "ocdiRedirect": "..."
}
```

Older response shapes may place the Skype token under a nested `skypeToken.skypetoken`.

### Important outputs

Chat-related service discovery values observed in AuthZ responses include:

```text
region
regionGtms.chatService
regionGtms.middleTier
tokens.skypeToken
```

The returned service table should be preferred over hard-coding regional hosts.

---

## `authzcore`

Current Teams Web JavaScript also contains:

```text
https://teams.microsoft.com/api/authsvc/v1.0/authzcore
https://authsvc.teams.microsoft.com/v1.0/authzcore
```

**Evidence:** Teams Web bundle.

The precise request/response contract has not yet been established.

The current bundle suggests newer authentication flows may use `authzcore` differently from the older `authz` flow and may not always return a Skype token directly.

---

# Chat Service Aggregator (CSA)

CSA appears to be the service most closely associated with the Teams chat/startup state rather than raw message storage.

Teams Web declares a CSA resource/host in the family of:

```text
https://chatsvcagg.teams.microsoft.com
https://teams.microsoft.com/api/csa
```

with an API version prefix:

```text
/api/v1/
```

**Evidence:** Teams Web bundle.

Public Teams implementations independently identify this OAuth resource:

```text
https://chatsvcagg.teams.microsoft.com
```

and use the same first-party Teams client ID:

```text
1fec8e78-bce4-4aaf-ab1b-5451cc387264
```

**Evidence:** public reverse-engineering implementations and traffic captures.

## CSA authentication model

The likely model is:

```text
Teams public client ID
        |
        +--> bearer for api.spaces.skype.com
        |        |
        |        +--> /authz
        |               |
        |               +--> Skype token
        |
        +--> bearer for chatsvcagg.teams.microsoft.com
                 |
                 +--> CSA API

CSA request therefore uses:
- CSA bearer token
- Skype token
```

A historical/public request shape is:

```http
GET https://teams.microsoft.com/api/csa/api/v1/teams/users/me
    ?isPrefetch=false
    &enableMembershipSummary=true

Authorization: Bearer <CSA access token>
x-skypetoken: <Skype token>
```

Other headers observed in public captures include:

```text
x-ms-session-id
x-ms-synctoken
x-csendpoint
```

Do not assume every header is still required in the current client until verified live.

---

## Startup / chat list

Route template:

```text
teams/users/me
```

Likely full proxied route:

```text
https://teams.microsoft.com/api/csa/api/v1/teams/users/me
```

Internal Teams client name:

```text
startupclean
```

Observed/public query parameters:

```text
isPrefetch=false
enableMembershipSummary=true
```

### Publicly observed response shape

A historical/public Teams response shows a structure similar to:

```json
{
  "teams": [],

  "chats": [
    {
      "id": "19:...@thread.v2",

      "consumptionHorizon": {
        "originalArrivalTime": 0,
        "timeStamp": 0,
        "clientMessageId": "..."
      },

      "members": [],
      "title": null,

      "version": 0,
      "threadVersion": 0,

      "isRead": true,
      "isOneOnOne": true,
      "isSticky": false,
      "hidden": false,

      "lastMessage": {
        "messageType": "RichText/Html",
        "content": "...",
        "clientMessageId": "...",
        "imDisplayName": "...",
        "originalArrivalTime": "...",
        "from": "...",
        "sequenceId": 2
      },

      "createdAt": "...",
      "tenantId": "...",
      "chatType": "chat"
    }
  ],

  "users": [],
  "channels": [],
  "privateFeeds": [],

  "metadata": {
    "syncToken": "...",
    "forwardSyncToken": null,
    "isPartialData": false,
    "hasMoreChats": false
  }
}
```

This is materially richer than the raw Chat Service conversation index and includes UI-oriented state such as:

```text
isRead
isSticky
hidden
consumptionHorizon
lastMessage
```

These fields show that CSA carries UI-oriented chat state in addition to conversation identifiers and metadata.

**Live observation (2026-09-29):** the CSA `chats[]` array order differed from the visible Teams sidebar. In one signed-in session, sorting entries by descending `lastMessage.originalArrivalTime` matched the observed sidebar order; this is evidence for that session, not a guaranteed service contract.

---

## Delta startup updates

Route template:

```text
teams/users/me/updates
```

Likely full proxied route:

```text
https://teams.microsoft.com/api/csa/api/v1/teams/users/me/updates
```

Internal Teams client name:

```text
startupdelta
```

Public captures indicate that the synchronization token returned by the startup response is sent back on later requests using:

```http
x-ms-synctoken: <previous metadata.syncToken>
```

Conceptually:

```text
GET /teams/users/me
        |
        v
full/bootstrap state
metadata.syncToken = A

GET /teams/users/me/updates
x-ms-synctoken: A
        |
        v
delta/update state
metadata.syncToken = B
```

Relevant response metadata:

```text
metadata.syncToken
metadata.forwardSyncToken
metadata.isPartialData
metadata.hasMoreChats
```

This suggests CSA may support a persistent local chat catalog that is updated incrementally rather than repeatedly rebuilt from `/users/ME/conversations`.

**Read-only live probe (2026-09-30):** requests used the app connector's
existing encrypted token cache and the following non-secret header names:
`Authorization`, `x-skypetoken`, and `Accept: application/json`. No token values
or response contents were recorded. Timings are client elapsed time around the
request and JSON parsing, with zero retries; they are not server processing
times.

- Bootstrap: `GET /api/csa/api/v1/teams/users/me?isPrefetch=false&enableMembershipSummary=true`, with no sync-token header, returned 944 chats, 4 teams, 6 private feeds, and 0 users. `metadata.isPartialData` and `metadata.hasMoreChats` were both false, and a `metadata.syncToken` was present. It took 5.656 seconds. The separate connector authentication/setup before this request took 5.268 seconds.
- `GET /api/csa/api/v1/teams/users/me/updates` without `x-ms-synctoken` returned a full-sized response: 944 chats, 4 teams, 7 channels, 6 private feeds, and 0 users, with both completeness flags false and a sync token. Two requests measured 3.907 and 4.145 seconds.
- The same updates route with `x-ms-synctoken` set to the bootstrap token returned empty `chats`, `teams`, `users`, `channels`, and `privateFeeds` arrays in 0.457 seconds. Its sync token matched the request token.
- A separate two-call test first called `/updates` without a sync token, then immediately called it with the token returned by that response. The first response again contained 944 chats, 4 teams, 7 channels, 6 private feeds, and 0 users (4.145 seconds); the token-bearing call returned empty arrays and the same token in 0.547 seconds.

The no-token `/updates` response therefore behaves like a full catalog snapshot in
this account/build, while a fresh sync token produced an empty no-change result
much faster. This is consistent with an incremental/delta route. The downloaded
Web bundle confirms the `/users/me` and `/users/me/updates` route mappings and
declares a `SyncTokenExpired` error category; it does not expose a readable
nonempty delta example. No chat changes were made for this test, so the exact
representation of additions, edits, removals/deletions, paging, token rotation,
and stale-token recovery remains unverified.

---

## Other known CSA route templates

### Group chats

```text
teams/users/me/groupchats
```

Internal name:

```text
startupgroupchats
```

### Non-recent chats

```text
teams/users/me/nonrecentchats
```

Internal name:

```text
startupnonrecentchats
```

### Anonymous startup

```text
teams/users/anonymous
teams/users/anonymous/updates
```

Internal names:

```text
startupcleananonymous
startupdeltaanonymous
```

The detailed request/response contracts for these routes remain unverified.

---

## CSA and visible chat ordering

The live startup response and sidebar comparison are documented under [Live observations and confidence](#live-observations-and-confidence-2026-09-29). In that session, CSA returned a complete catalog, its array order differed from the sidebar, and sorting by descending `lastMessage.originalArrivalTime` matched the observed visible order. The result is account- and time-scoped; the API does not declare that ordering as a contract.

The startup response also exposes synchronization metadata. A live read-only
request to the delta route accepted the startup token and returned a complete
empty result; behavior for nonempty changes and token recovery remains
unverified. See the route templates above and the confidence notes below.

---

# Regional Chat Service

The Regional Chat Service is the most useful backend for **raw conversation/message access**.

Its conversation feed is not evidence of the exact Teams sidebar/chat-picker ordering. The CSA snapshot is also only a candidate input: the current Teams Web bundle declares a separate, sectioned chat-list GraphQL query, described under [Conversation catalog sources](#conversation-catalog-sources-raw-feed-vs-ui-state). The captured HAR did not include that query's live request or response, so its wire contract and end-to-end use by the visible sidebar remain unverified.

The base URL should come from:

```python
authz_response["regionGtms"]["chatService"]
```

An observed regional shape is:

```text
https://emea.ng.msg.teams.microsoft.com
```

The API prefix used below is:

```text
/v1
```

Authentication is normally:

```http
Authentication: skypetoken=<Skype token>
```

Some experiments also included the original `api.spaces.skype.com` bearer token. Current reverse-engineered implementations generally show the Skype-token header as the essential Chat Service credential.

Observed uses of this service include:
- raw conversation discovery,
- message history retrieval,
- conversation member lookup.

---

## Conversation catalog sources: raw feed vs UI state

These sources expose different views of chat state.

### 1. Raw Chat Service conversation feed

```text
GET {chatService}/v1/users/ME/conversations
```

This route returns the Chat Service conversation feed and its own continuation metadata. Its order is not evidence of the order displayed in the Teams sidebar.

### 2. CSA startup/chat state

```text
GET https://teams.microsoft.com/api/csa/api/v1/teams/users/me
```

CSA includes UI-oriented fields such as `isRead`, `isSticky`, `hidden`, `consumptionHorizon`, `lastMessage`, and synchronization metadata. In one live comparison, the raw `chats[]` order differed from the sidebar, while descending `lastMessage.originalArrivalTime` matched the visible ordering. See [Live observations and confidence](#live-observations-and-confidence-2026-09-29) for the scope and limits of that comparison.

### 3. Teams Web chat-list GraphQL operation

The supplied Teams Web bundle defines `DataResolversChatChatListWithCachedTitleQuery`. It asks for separate cursor connections for `recentsChats`, `pinnedChats`, `chatRequests`, `topFixedChats`, and other suggestion/contact groups. The recent-chat connection accepts `first`/`after`, filters, and `excludePinnedChats`; it returns edge cursors and `pageInfo`. Its selected recent-chat fields include the last message's `originalArrivalTime`. Separate connections establish that the data model has multiple lists, but do not tell us whether the client renders each edge sequence as returned or locally reorders it, or which comparator it uses.

The same bundle's schema defines `PinnedChat` with `conversationId` and `order`, and a `reorderPinnedChats` mutation that accepts ordered `PinnedChatInput` entries. This establishes a client-side explicit ordering value for pinned chats in this build. It does not establish the recent-chat comparator, tie-breaking rules, visibility/folder composition, or every section rendered by the current sidebar.

The source includes an explicit query call in a desktop experience warm-up path (`includePinnedChats: true`, `includeSuggestedContacts: false`). The supplied HARs contain no chat-list GraphQL request/response, and this source audit did not trace the full sidebar component-to-query-to-render path. Treat this as the strongest client-source lead for exact list behavior, not yet a verified endpoint contract or proof of final visible ordering. See [the dated source audit](#teams-web-chat-list-and-people-helper-source-audit-2026-10-11).

This absence is specifically about the Teams chat-list operation. The combined October captures do
contain ten Outlook `/outlookgatewayb2/graphql` POSTs during Recaps, documented in the linked
[cross-product search ledger](../../lrh-outlook-connector/docs/m365-search-findings-2026-10-10.md)
and Outlook research. Those operations concern calendar groups, tabs/entitlements, working hours,
auth settings and meeting response; they are not evidence of a Teams sidebar GraphQL call.

### 4. Teams Web IndexedDB conversation state

A current open-source Teams Web exporter reports that the web client stores conversation-manager state in IndexedDB stores such as:

```text
Teams:conversation-manager:react-web-client:<tenant>:<user>:<locale>
Teams:conversation-folder-manager:...
```

That project reports using this browser state for its chat picker because it more closely matched the browser UI, including meeting-derived and less common conversation types. This is evidence about the web client’s local state, not a backend API contract.

## GET `{chatService}/v1/users/ME/conversations`

List raw service conversations.

**Evidence:** observed live + reverse-engineered implementation.

This endpoint exposes the raw Chat Service conversation catalog. Its returned order should not be treated as the Teams sidebar order.

### Query parameters

Observed/used:

```text
pageSize=<N>
view=mychats
```

Other observed continuation URLs have used:

```text
view=msnp24Equivalent
syncState=<opaque value>
```

Do not assume these two `view` values are interchangeable.

### Pagination

The response contains `_metadata`, commonly including:

```json
{
  "_metadata": {
    "backwardLink": "https://...",
    "forwardLink": "",
    "syncState": "...",
    "totalCount": 99
  }
}
```

The correct pagination strategy is:

```text
first page
   |
   v
_metadata.backwardLink
   |
   v
older page
   |
   v
repeat
```

Treat `backwardLink` as an opaque continuation URL.

Avoid rebuilding `syncState` manually unless absolutely necessary.

### Conversation response shape

Typical raw fields include:

```json
{
  "id": "19:...@thread.v2",
  "version": 1234567890,

  "lastMessage": {
    "id": "...",
    "sequenceId": 123,
    "content": "...",
    "imdisplayname": "...",
    "messagetype": "RichText/Html",
    "originalarrivaltime": "..."
  },

  "messages": "https://.../conversations/<id>/messages",

  "properties": {
    "displayName": "...",
    "lastimreceivedtime": "...",
    "consumptionhorizon": "..."
  },

  "threadProperties": {
    "topic": "...",
    "threadType": "...",
    "productThreadType": "...",
    "memberCount": "...",
    "lastSequenceId": "...",
    "createdat": "..."
  }
}
```

### Human-readable conversation names

Meeting/group/channel names may live under:

```text
threadProperties.topic
threadProperties.topicThreadTopic
threadProperties.spaceThreadTopic
properties.displayName
```

One-to-one chats often have no explicit title.

Typical 1:1 conversation IDs look like:

```text
19:<user-guid>_<user-guid>@unq.gbl.spaces
```

One GUID is the current user; the other can be resolved through the Middle Tier.

---

## GET `{chatService}/v1/users/ME/conversations/{conversationId}/messages`

Read one page of message history.

**Evidence:** reverse-engineered implementation; strongly aligned with conversation objects that expose a `messages` URL.

### Path parameter

```text
conversationId
```

URL-encode it.

### Query parameters

Observed/reverse-engineered parameters include:

```text
pageSize=<N>
startTime=<Unix epoch milliseconds>
view=msnp24Equivalent
```

`startTime=0` requests the unbounded history available to the caller. Live
read-only tests confirmed that a nonzero `startTime` acts as an inclusive lower
time bound for the tested conversation and meeting-event cases. The connector
uses this parameter for `since` and retains a local timestamp check as a
correctness guard. A `since=` query parameter and an OData-style `$filter` were
ignored by the tested route.

`endTime` was also tested, but ordinary-message boundary behavior was too coarse
or inconsistent to use as a reliable inclusive upper bound. The current client
therefore applies `until` locally. These observations are empirical and may vary
by message type or service version.

Historical clients have also used richer `view` combinations such as:

```text
supportsExtendedHistory|msnp24Equivalent|supportsMessageProperties
```

Example:

```http
GET /v1/users/ME/conversations/19%3A.../messages
    ?startTime=0
    &pageSize=100
    &view=msnp24Equivalent
```

The minimum required parameter set has not been established. These values describe observed request shapes, not a complete mandatory-parameter contract.

### Likely response

```json
{
  "messages": [
    {
      "id": "...",
      "sequenceId": 123,
      "messagetype": "RichText/Html",
      "from": "...",
      "imdisplayname": "...",
      "content": "<p>Hello</p>",
      "originalarrivaltime": "...",
      "composetime": "...",
      "properties": {
        "subject": null,
        "edittime": "...",
        "deletetime": "...",
        "mentions": "...",
        "emotions": "...",
        "files": "..."
      }
    }
  ],

  "_metadata": {
    "backwardLink": "https://...",
    "syncState": "...",
    "lastCompleteSegmentStartTime": 1680650525872,
    "lastCompleteSegmentEndTime": 1681220311465
  }
}
```

Public captures show that the segment boundary fields may be present:

```text
lastCompleteSegmentStartTime
lastCompleteSegmentEndTime
```

Possible inference: these timestamps may describe a complete history segment and could help determine whether an older segment falls before a date boundary. Their precise semantics and safe use for early termination have not been verified.

### Pagination

Same general pattern as conversations:

```text
GET messages?pageSize=N
        |
        v
_metadata.backwardLink
        |
        v
older messages
```

Historical implementations expose both:

```text
startTime
pageSize
view
```

for an initial/history request and:

```text
syncState
pageSize
view
```

for continuation requests.

The preferred implementation strategy remains:

```python
next_url = response["_metadata"]["backwardLink"]
```

and then call that URL verbatim.

Treat `syncState` as opaque. Do not decode or manufacture it unless live behavior forces us to.

The route identifies the conversation directly in its path. A previously fetched conversation object may also expose a `messages` URL; captured implementations use the conversation ID to construct the route when that link is unavailable. The service supplies continuation URLs for older pages. `startTime` is a confirmed server-side lower-bound parameter in the tested live responses; the finite count and safe upper-bound decisions remain client-side.

---

## GET `{chatService}/v1/threads/{conversationId}/members`

List conversation members.

**Evidence:** reverse-engineered implementation.

### Response

Likely:

```json
{
  "members": [
    {
      "id": "8:orgid:...",
      "userDisplayName": "...",
      "role": "..."
    }
  ]
}
```

Useful for:

- naming 1:1 chats
- naming group chats
- resolving participants
- identifying bots (`28:` MRI prefix)

---

## GET `{chatService}/v1/users/ME/properties`

Read current Chat Service user properties.

**Evidence:** reverse-engineered implementation.

Returns an unstructured/raw JSON property object.

---

# Chat Service write operations

These are not required for conversation export, but are useful for understanding the API.

---

## POST `{chatService}/v1/users/ME/conversations/{conversationId}/messages`

Send a message.

**Evidence:** reverse-engineered implementation.

Example body:

```json
{
  "content": "<p>Hello</p>",
  "messagetype": "RichText/Html",
  "contenttype": "text",
  "clientmessageid": "1790123456789",
  "imdisplayname": "Sender Name",
  "properties": {
    "importance": "",
    "subject": null
  }
}
```

Optional:

```json
{
  "amsreferences": ["<AMS object id>"]
}
```

Likely response:

```json
{
  "OriginalArrivalTime": 1790123456789
}
```

---

## PUT `{chatService}/v1/users/ME/conversations/{conversationId}/messages/{messageId}`

Edit a message.

Example body:

```json
{
  "content": "...",
  "messagetype": "RichText/Html",
  "contenttype": "text",
  "skypeeditedid": "<messageId>",
  "imdisplayname": "Sender Name"
}
```

Possible response:

```json
{
  "edittime": "..."
}
```

or an empty body.

---

## DELETE `{chatService}/v1/users/ME/conversations/{conversationId}/messages/{messageId}`

Delete a message.

Usually succeeds with little or no response body.

---

## PUT/DELETE message reaction property

```text
{chatService}/v1/users/ME/conversations/{conversationId}/messages/{messageId}/properties?name=emotions
```

Add reaction:

```http
PUT
```

Remove reaction:

```http
DELETE
```

Body:

```json
{
  "emotions": {
    "key": "like",
    "value": "<messageId>"
  }
}
```

Other known reaction keys include values such as:

```text
heart
laugh
surprised
```

---

## POST `{chatService}/v1/users/ME/drafts`

Create a scheduled draft/message.

Example fields:

```json
{
  "draftType": "ScheduledDraft",
  "innerThreadId": "<conversation id>",
  "draftDetails": {
    "sendAt": "<epoch milliseconds>"
  },
  "message": {
    "...": "..."
  }
}
```

---

# Middle Tier

The Middle Tier is used for user/profile resolution and related metadata.

The preferred base URL should come from:

```python
authz_response["regionGtms"]["middleTier"]
```

Example shape:

```text
https://teams.microsoft.com/api/mt/part/emea-03
```

Do not assume the alternative:

```text
https://teams.cloud.microsoft/api/mt/{region}
```

is always equivalent.

Authentication uses the Teams bearer token whose audience is:

```text
https://api.spaces.skype.com
```

---

## POST `{middleTier}/beta/users/fetch`

Resolve user MRIs into profile information.

**Evidence:** Teams Web bundle + captured Teams Web request (2026-10-10) + live/reverse-engineering work.

### Query parameters observed

```text
isMailAddress
canBeSmtpAddress
enableGuest
includeIBBarredUsers
skypeTeamsInfo
includeDisabledAccounts
```

Typical values:

```text
isMailAddress=false
enableGuest=true
skypeTeamsInfo=true
```

### Body

```json
[
  "8:orgid:<user-id>",
  "8:orgid:<user-id>"
]
```

### Captured response shape

```json
{
  "value": [
    {
      "objectId": "...",
      "mri": "8:orgid:...",
      "displayName": "...",
      "userPrincipalName": "...",
      "email": "...",
      "jobTitle": "...",
      "userLocation": "...",
      "userType": "..."
    }
  ]
}
```

Some responses may use `Value` instead of `value`.

The 2026-10-10 Teams global-search capture contains one successful `fetch` POST. Its body was a
JSON array of identifiers and its response was an object with a `value` array. The observed
profile field names included `objectId`, `mri`, `displayName`, `userPrincipalName`, `email`,
`jobTitle`, `userLocation`, `userType`, `department`, `companyName`, `givenName`, `surname`,
`phones`, `type`, and `skypeTeamsInfo`. Values are not retained. This confirms a successful
browser call and field presence for that response; it does not prove every field is populated or
stable across account types.

---

## POST `{middleTier}/beta/users/fetchShortProfile`

Shorter profile lookup.

**Evidence:** Teams Web route family + captured Teams Web request (2026-10-10) + reverse-engineered implementation.

Typical query parameters:

```text
isMailAddress=false
enableGuest=true
skypeTeamsInfo=true
```

Body:

```json
[
  "8:orgid:<user-id>"
]
```

Likely returned fields:

```text
mri
displayName
email
jobTitle
userLocation
userType
```

The capture contains one successful `fetchShortProfile` POST with the same six query-key names as
`fetch`, a JSON-array body, and an object response with a `value` array. Observed fields included
`mri`, `objectId`, `displayName`, `userPrincipalName`, `email`, `jobTitle`, `userLocation`,
`userType`, `department`, `companyName`, `givenName`, `surname`, and `type`. The captured sample
does not establish that the short and full profile routes are interchangeable.

## POST `{middleTier}/beta/users/fetchfeaturesettings`

**CAPTURED:** three successful requests had no query string. Each JSON-array body contained
objects with an `Mri` field; each response was an array with per-entry `status`. The settings
payload and status meanings were not interpreted, so this is route and envelope evidence only.

## GET `{middleTier}/beta/users/{userId}/profilepicturev2/{mri}` and `{middleTier}/beta/users/{userId}/mergedProfilePicturev2`

**CAPTURED:** Teams Web loaded profile images through both routes. The individual-picture route
used query keys `displayname`, `size`, and, in some requests, `ETag` or `avatarETag`; the merged
route used `size` and `usersInfo`. Successful responses were image bytes (mostly JPEG; one PNG
variant). These calls hydrate profile images and do not search people. Identifier values are
omitted.

---

# Other Middle Tier routes found in Teams Web

The production bundle contains route templates in the following families.

The route family list below mixes source-only helpers with routes now observed in the 2026-10-10
Teams Web capture. The dated capture notes later in this file distinguish which contracts have
request/response evidence; presence in source alone remains insufficient to establish behavior.

## Profile lookup

```text
beta/users/fetchFederated
beta/users/fetchFederatedByEmails
beta/users/{upn}/
beta/users/me/
beta/users/me
```

Observed flags across these routes include:

```text
throwIfNotFound
isMailAddress
enableGuest
skypeTeamsInfo
forceRefresh
ephemeralMeProfile
ggEnabled
edEnabled
includeDisabledAccounts
```

---

## Shared-channel profile resolution

```text
beta/users/fetchForSharedChannel
beta/users/fetchForSharedChannels
```

Observed concepts:

```text
conversationId
hostTenantId
MRIs
profile flags
```

---

## Profile pictures

```text
beta/users/profilePicture
beta/groups/{threadId}/profilepicture
beta/threads/{threadId}/properties/picture
beta/users/defaultProfilePictureV2
beta/users/profilepicturepolicy
```

`defaultProfilePictureV2` has observed parameters such as:

```text
displayName
shortenSingleWord
size
```

Teams Web bundle inspection and the signed-in UI show this profile image URI pattern:

- `GET {middleTier}/beta/users/{currentUserId}/profilepicturev2/{targetMri}`
  - `currentUserId` is the signed-in user's object identifier.
  - `targetMri` is the target user's MRI, such as `8:orgid:{objectId}`.
  - `displayname` is a query parameter used as a fallback identity label.
  - `size` is a query parameter; `HR64x64` was observed on the current user's Teams Web avatar.
  - The URI can be assigned to an image element directly; the browser loads it for display. The application does not create or persist a separate image file.
- Teams Web bundle queries also include chat avatar fields such as `chat { id, avatarUrl }` and chat picture metadata.
  - The presence of these fields in the client bundle does not establish that every conversation response contains a usable image URI.
  - The connector accepts absolute HTTPS image URIs when a chat response supplies one. The local UI falls back to local SVG icons when an image is missing, fails, or takes too long to load.
- The inspected client bundles include GraphQL presence data and operations.
  - The application does not currently request presence; a stable read-only contract has not been established.

These are reverse-engineered client patterns, not a supported public API contract. Availability and access behavior may vary by tenant, account, and Teams client version.

---

## People/search/profile helpers

```text
Middle Tier / People Picker:
  beta/users/search
  beta/users/searchV2
  beta/users/searchUsers
  beta/users/searchExternalUsers
  beta/scd/lookup
  beta/users/me/lookup/phones
  beta/users/{searchQuery}/externalsearch
  beta/users/{searchQuery}/externalsearchv3
  beta/scd/searchability
Middle Tier / profile helper:
  beta/users/{mri}/statementUrl
People Profile Service:
  beta/workingSet/people
```

These names span more than one service and include lookup, sync, and mutation routes. The supplied production bundle establishes useful request and response transformations for several of them. The 2026-10-10 Teams global-search HAR now also captures `fetch`, `fetchShortProfile`, and `fetchfeaturesettings` requests, plus profile-image hydration; the per-route notes above record what the capture confirms. It does not contain requests to the free-form People Picker `search`/`searchV2`, `searchUsers`, external-user search, SCD lookup/searchability, or People Profile working-set routes. Those remain source-established only. A separate Substrate Powerbar People-suggestions request and response were captured; see [the capture notes](#captured-powerbar-people-suggestions).

| Client route | What the client source establishes | Evidence and boundary |
|---|---|---|
| `POST {middleTier}/beta/users/searchV2?{callerQuery}` or `POST {middleTier}/beta/users/search?{callerQuery}` | The People Picker selects `searchV2` when `enablePeopleSearchV2` is on, otherwise `search`. It sends the caller-provided body as JSON, parses a successful response as JSON, and turns `403` into a forbidden-search error. This model uses the Middle Tier discovery URL and `mtAuthResource` with source `peoplePhase2`. | **SOURCE.** The body and query-string schema are caller-owned and were not captured. No request to either route appears in the supplied HARs. |
| `POST {middleTier}/beta/users/searchExternalUsers?edEnabled={bool}&tflUsersOnly={bool}` | `getExternalProfilesByAliases` POSTs its input as JSON. The two flags come from extended-directory and external-user-search settings. This profile-service model uses `mtAuthWithSkypeXTokenResource`. | **SOURCE.** No request/response captured; alias input and returned profile schema remain unverified. |
| `POST {middleTier}/beta/users/searchUsers[?ggEnabled=true&resultCount={n}]` | The People Picker sends `{emails, phones, searchKeyWord?}`. The optional Golden Gate flag adds `ggEnabled=true`, and a supplied result limit adds `resultCount`. Its parser expects keyed result objects with `status` and `userProfiles`, then flattens the profiles and adds a local `searchKey`. This is batch resolution by email/phone, optionally with a keyword; it is distinct from the free-form `/search` and `/searchV2` picker route. | **SOURCE.** No request/response captured. The parser's local `searchKey` is not evidence of a wire response field. |
| `POST {middleTier}/beta/scd/lookup` | Sends JSON `{emails, phones}` and returns the response's `value` member. | **SOURCE.** No request/response captured. |
| `POST {middleTier}/beta/users/me/lookup/phones` | Sends JSON `{phones}` and returns the response's `results` member. | **SOURCE.** No request/response captured. |
| `GET {middleTier}/beta/users/{searchQuery}/externalsearch` or `/externalsearchv3` | Selects v3 for the v3/XTap path. Depending on the call, it adds `xTap=true`, `includeTFLUsers=true`, or `edEnabled=true&tflUsersOnly=true`; it parses a JSON response and explicitly handles `403` and `404`. | **SOURCE.** No request/response captured; query encoding, result schema, and which UI flow chooses each variant remain unverified. |
| `GET {peopleProfileService}/beta/workingSet/people?...` | Although its registry service name is Middle Tier, discovery uses the distinct `peopleProfileService` URL and `mtAuthWithSkypeXTokenResource`. It includes `maxSize` when positive (default 4,000), `maxPageSize` (default 1,000), and optional `skipToken`/`deltaToken`; the response body is passed through as JSON. This is working-set/contact synchronization, not an interactive search route. | **SOURCE** (worker module `66547`). No request/response captured; item schema, token semantics, and server limits are not established. |
| `POST {middleTier}/beta/scd/searchability?ggEnabled={bool}` | Wired to `updateMSASearchabilityStamping` and sends a JSON body. It is a profile-searchability mutation, not a result-search endpoint. | **SOURCE.** Body and response semantics are not fully established; no live call captured. |
| `GET {middleTier}/beta/users/{mri}/statementUrl` | The profile model extracts the response's `statementUrl` member for the tenant privacy-statement helper. | **SOURCE.** No request/response captured; this is a profile/privacy helper, not a search route. |

`fetch` and `fetchShortProfile` are profile-resolution helpers, not free-form people search. Their captured successful calls establish a real browser request and partial response-field contract, but not the behavior of the uncaptured People Picker search helpers. Substrate People suggestions are a third workflow, separate from Middle Tier profile resolution and full Search v2 `/searchservice/api/v2/query`.

---

## Teams Copilot/work-position profile routes

```text
beta/users/profile/copilotWorkPosition
beta/users/profile/fetchCopilotWorkPosition
beta/users/profile/copilotWorkPosition/{position_id}
```

Not relevant to the exporter right now.

---

# Substrate people/chat search

A separate service can search for people and chats.

Base:

```text
https://substrate.office.com
```

Endpoint:

```http
POST /search/api/v1/suggestions?scenario=peoplepicker.newChat
```

**Evidence:** reverse-engineered implementation.

Requires a bearer token whose audience matches the Substrate/Office search service, not merely the Chat Service Skype token.

---

## People search request

Example:

```json
{
  "EntityRequests": [
    {
      "Query": {
        "QueryString": "Charlie",
        "DisplayQueryString": "Charlie"
      },
      "EntityType": "People",
      "Size": 10,
      "Fields": [
        "Id",
        "MRI",
        "DisplayName",
        "EmailAddresses",
        "PeopleType",
        "PeopleSubtype",
        "UserPrincipalName",
        "GivenName",
        "Surname",
        "JobTitle",
        "Department",
        "ExternalDirectoryObjectId"
      ],
      "From": 0
    }
  ],
  "Scenario": {
    "Name": "peoplepicker.newChat"
  },
  "AppName": "Microsoft Teams"
}
```

Likely response:

```json
{
  "Groups": [
    {
      "Type": "People",
      "Suggestions": [
        {
          "DisplayName": "...",
          "MRI": "...",
          "EmailAddresses": ["..."],
          "UserPrincipalName": "...",
          "JobTitle": "...",
          "Department": "...",
          "ExternalDirectoryObjectId": "..."
        }
      ]
    }
  ]
}
```

---

## Chat search request

```json
{
  "EntityRequests": [
    {
      "Query": {
        "QueryString": "example project"
      },
      "EntityType": "Chat",
      "Size": 10
    }
  ],
  "Scenario": {
    "Name": "peoplepicker.newChat"
  },
  "AppName": "Microsoft Teams"
}
```

Likely chat suggestion fields:

```text
Name
ThreadId
ThreadType
MatchingMembers
ChatMembers
TotalChatMembersCount
```

The returned fields appear to describe chat suggestions and matched participants.

---

# Other Teams search routes

Current Teams Web JavaScript contains route templates in these families:

```text
search/api/v2/query
searchservice/api/v2/query

search/api/v1/init
search/api/v2/init

search/api/v1/suggestions
```

Observed query concepts include:

```text
cvid
scenario
query
logicalId
```

Some search constants reference a token resource in the `outlook.office.com` family.

These should not be assumed to use the same token as the Chat Service.

The supplied captures now confirm several submitted-search and suggestion variants; they do not
establish every route template above as a callable operation or a portable connector contract.
See [the captured search audit](#global-teams-search) for per-surface evidence and remaining gaps.

## Read-only feasibility check — 2026-09-29

The app-owned MSAL cache could silently obtain a token for the Substrate search resource. The token was held only in memory for the probe and was not written to disk, printed, or included in these notes.

The `teams-api` implementation sends chat suggestions to the endpoint above with a bearer token and JSON body containing an `EntityRequests` entry with `EntityType: "Chat"`, a `Query.QueryString`, and `Size`; the outer object includes `Scenario.Name: "peoplepicker.newChat"` and `AppName: "Microsoft Teams"`. Its implementation describes suggestion fields such as `Name`, `ThreadId`, `ThreadType`, `MatchingMembers`, and `ChatMembers`. This appears to be a new-chat people-picker/search suggestion flow, not a verified complete search over all existing conversations.

A read-only direct request using the cached Substrate token and that request shape returned HTTP 400 with a generic service error. It did not return a usable chat result. This does not establish whether a different Teams build, tenant policy, request context, or token acquisition path would succeed; the route is not used by the exporter.

The signed-in Teams Web UI returned keyword search results spanning messages and chats. The
2026-10-10 HAR captures `POST /searchservice/api/v2/query` for global search and three
`POST /search/api/v2/query` Transcript requests. Their request and response evidence is recorded
under [Global Teams search](#global-teams-search). Search-result navigation is also present in
the client bundle and is documented there. These browser observations still do not establish
standalone authentication, complete coverage, or a general continuation contract.

Separately, a read-only `GET /v1/threads/{conversationId}/members` request returned nine member records for one live group chat. Their display names were empty in the Chat Service response. The app's existing Middle Tier profile lookup resolved all nine identifiers during the same session. This supports local participant-name indexing after roster retrieval, but it is one sample and does not prove that all member identities resolve.

---

# AMS / Async Media Service

Teams uses AMS for inline media and some meeting artifacts.

The exact regional host should preferably come from:

```python
authz_response["regionGtms"]["ams"]
```

or directly from URLs embedded in messages.

Do not hard-code one global AMS hostname.

---

## GET AMS image

Message HTML can contain:

```html
<img
  itemtype="http://schema.skype.com/AMSImage"
  src="https://.../v1/objects/<OBJECT_ID>/views/imgo"
>
```

Useful views include:

```text
imgo
imgpsh_fullsize_anim
```

Request:

```http
GET <AMS URL>
Authorization: skype_token <Skype token>
```

Likely return:

```text
binary image data
```

---

## POST AMS object

Used when uploading inline images.

Reverse-engineered shape:

```http
POST https://.../v1/objects/
Authorization: Bearer <AMS/IC3 token>
Content-Type: application/json
x-ms-client-version: <Teams client version>
```

Body example:

```json
{
  "type": "pish/image",
  "permissions": {
    "<conversationId>": ["read"]
  },
  "sharingMode": "Attached",
  "filename": "image.png"
}
```

Likely response:

```json
{
  "id": "<AMS object id>"
}
```

---

## PUT AMS image content

```http
PUT /v1/objects/{amsObjectId}/content/imgpsh
Authorization: Bearer <AMS token>
Content-Type: application/octet-stream
x-ms-client-version: <Teams client version>
```

Body:

```text
raw image bytes
```

---

# Meeting transcripts

Meeting recording messages may contain XML such as:

```xml
<item type="amsTranscript" uri="https://..."/>
```

The transcript URL embedded in the message can be fetched directly.

```http
GET <amsTranscript URL>
Authorization: skype_token <Skype token>
```

Likely response:

```text
WEBVTT

00:00:01.000 --> 00:00:05.000
<v Speaker Name>Hello</v>
```

A recording message may use:

```text
messagetype = RichText/Media_CallRecording
```

and include metadata such as recording status and original meeting name.

---

# SharePoint / OneDrive file attachments

Teams chat file attachments are typically hosted in SharePoint/OneDrive rather than Chat Service itself.

Message metadata may contain a `properties.files` JSON structure with fields such as:

```text
itemid
fileName
fileType
fileInfo.fileUrl
fileInfo.shareUrl
objectUrl
```

---

## Download file

Typical shape:

```http
GET https://<tenant>-my.sharepoint.com/personal/<user>/_api/v2.0/drive/items/{itemId}/content
Authorization: Bearer <SharePoint token>
```

Response:

```text
raw file bytes
```

---

## Upload chat file

Typical target:

```text
Microsoft Teams Chat Files
```

Reverse-engineered upload shape:

```http
PUT https://<tenant>-my.sharepoint.com/personal/<user>/_api/v2.0/drive/root:/Microsoft%20Teams%20Chat%20Files/{filename}:/content
    ?@name.conflictBehavior=rename
    &$select=*,sharepointIds,webDavUrl
Authorization: Bearer <SharePoint token>
Content-Type: application/octet-stream
```

---

## Create SharePoint sharing link

```http
POST .../_api/v2.0/drive/items/{driveItemId}/createLink
Authorization: Bearer <SharePoint token>
Content-Type: application/json
```

Example body:

```json
{
  "type": "edit",
  "scope": "organization"
}
```

or:

```json
{
  "type": "edit",
  "scope": "users",
  "recipients": [
    {
      "email": "person@example.com"
    }
  ]
}
```

Likely return includes:

```text
shareId
link.webUrl
```

---

# Message schema observations

A raw message can include substantially more than plain text.

Potentially useful fields:

```text
id
sequenceId
conversationid
from
imdisplayname
messagetype
contenttype
content
originalarrivaltime
composetime
clientmessageid
amsreferences
properties
```

Important `properties` subfields seen/reverse-engineered:

```text
subject
edittime
deletetime
mentions
emotions
files
importance
```

### Message types

Common user-authored text types:

```text
Text
RichText/Html
```

Other system/media types exist, for example:

```text
MessageDelete
RichText/Media_CallRecording
```

### System and thread-activity events

Message history can include service-generated event records alongside user-authored messages. They are returned as message items, so a raw message count can include membership, meeting-policy, and topic changes. Microsoft Graph documents Teams system messages as `systemEventMessage` records with structured `eventDetail`; its supported event list distinguishes events such as members added, members joined, and meeting policy updated ([Microsoft Graph system messages](https://learn.microsoft.com/en-us/graph/system-messages)).

The Regional Chat Service can expose related activity in a raw Skype-style representation instead of Graph's normalized `eventDetail`. Microsoft Learn's Azure Communication Services chat example shows the same `<addmember>` and `<topicupdate>` XML envelopes, with message types `ThreadActivity/AddMember` and `ThreadActivity/TopicUpdate` ([chat message response example](https://learn.microsoft.com/en-us/rest/api/communication/chat/list-chat-messages/list-chat-messages?view=rest-communication-chat-2020-09-21-preview)). This corroborates the envelope and broad meaning; the Azure Communication Services schema is adjacent evidence, not a contract for Teams' private endpoint.

Interpretation of the live meeting-thread payloads inspected on 2026-09-29:

- `<addmember>` records a membership change. `initiator` identifies the actor and `target` the identity being added. In one observed event, both fields contained the same `8:orgid` identity, indicating that the actor and target were the same principal. This is consistent with that principal being added to or initialized in the meeting thread; it does not establish whether this was an automatic setup step or a deliberate user action.
- `<meetingpolicyupdated><value>ChatEnabled</value>` indicates that meeting chat was enabled. The corresponding Graph event detail exposes a `meetingChatEnabled` boolean; `true` means chat is enabled.
- A JSON record containing `initiator` and a `members` array is membership/roster activity. The inspected display showed opaque `8:orgid` identities and empty friendly names, but omitted the enclosing message type. Without that type, it is not possible to distinguish a members-added event from members-joined activity or another roster update.
- `<topicupdate><value>…</value>` records a conversation topic/name update. The value can match the meeting chat's displayed title.

The `eventtime` values are Unix epoch milliseconds and align with the surrounding message timestamps. A cluster of these events near the beginning of a `19:meeting_…@thread.v2` history is consistent with meeting-thread setup and roster initialization. It does not by itself prove that a meeting took place, who manually created the thread, or why the thread may be absent from the normal chat list.

### HTML detection

Teams may represent a normal rich-text message as:

```text
messagetype = RichText/Html
contenttype = text
```

Therefore exporters should **not** detect HTML only from `contenttype`.

A robust check should inspect both variants used by observed payloads:

```text
messagetype / messageType
contenttype / contentType
```

For example:

```python
is_html = (
    "html" in str(message.get("messagetype") or message.get("messageType") or "").lower()
    or "html" in str(message.get("contenttype") or message.get("contentType") or "").lower()
)
```

### Deleted messages

A message may be considered deleted when:

```text
messagetype == MessageDelete
```

or when a deletion timestamp exists under:

```text
properties.deletetime
```

These shapes are reverse-engineered observations, not a complete Microsoft contract. The local archive treats an explicit `MessageDelete` record or deletion timestamp as a tombstone: it keeps the stable message row and any previously retained body, reports the message as deleted, and excludes its old body from ordinary message search. A message missing from a later response is not treated as deleted because missing data can also mean retention expiry, incomplete paging, or lack of access.

`properties.edittime` is retained as a revision timestamp. The archive uses it to avoid letting an older or unversioned copy replace known edited text. This ordering rule is covered by sanitized fixtures, but this investigation did not compare a known live edited/deleted message against Teams Web. The exact association between edit/delete activity, message IDs, and timestamps therefore remains tenant/build dependent and should be validated before treating it as authoritative.

### Mentions

`properties.mentions` may be serialized JSON containing entries such as:

```json
[
  {
    "id": "...",
    "displayName": "..."
  }
]
```

A live message also contained a semantic mention element in its HTML, marked with:

```text
itemtype="http://schema.skype.com/Mention"
```

The element wrapped the mentioned display name, while `properties.mentions` identified the mention and its `displayName`. A formatter can therefore render a delimited marker such as `@(Example, User)` based on message markup/metadata rather than treating every matching name in plain text as a mention.

### Reactions

`properties.emotions` may be serialized JSON.

Each reaction entry can contain:

```text
key
users[]
```

A special emotion key such as:

```text
follow
```

may represent followers rather than a normal emoji reaction.

### Quoted replies

Quoted-message references can appear inside message HTML using markup such as:

```html
itemtype="http://schema.skype.com/Reply"
itemid="<message-id>"
```

### Inline images

Inline images can use:

```html
itemtype="http://schema.skype.com/AMSImage"
```

### File attachments

File attachments commonly live in:

```text
properties.files
```

and may use:

```text
@type = http://schema.skype.com/File
```

---

# Live observations and confidence — 2026-09-29

The following observations came from read-only requests made with the signed-in Teams session and comparisons against the visible Teams Web UI. They describe one enterprise account and one point in time; they are not Microsoft service guarantees.

## CSA startup catalog

A request to:

```http
GET https://teams.microsoft.com/api/csa/api/v1/teams/users/me?isPrefetch=false&enableMembershipSummary=true
```

succeeded with the CSA bearer token and Skype token. The response contained **942 chat entries** and reported:

```text
metadata.isPartialData = false
metadata.hasMoreChats = false
```

The raw `chats[]` order did not match the sidebar order. Sorting by descending `lastMessage.originalArrivalTime` matched the visible ordering in the observed session. After resolving untitled one-to-one chats, 18 of the first 20 visible sidebar entries matched a comparison against the catalog. This does not establish equivalence for every chat type, account, or future Teams build.

CSA can omit display names for one-to-one chats. In the inspected response, member records had identity fields but no display names, and the top-level `users` array was empty. A read-only request to the Middle Tier `POST {middleTier}/beta/users/fetch` route, using the participant identifiers in its JSON array body, returned a profile with a `displayName` for the tested participant. This confirms that Middle Tier profile lookup can fill at least some CSA naming gaps; it does not prove that every identifier or chat type resolves.

## CSA state flags

The live payload included `isRead` and `isSticky` fields. `isRead` is a server-supplied state value in this response shape. No read-state mutation was performed.

The relationship between `isSticky` and the Teams UI’s Favorites/pinned section remains uncertain. In the observed comparison, the flag did not consistently identify the visible Favorite row, so it should not be treated as a verified Favorites marker.

## Message history

A read-only Regional Chat Service request for a conversation’s message history succeeded. A request with `pageSize=10` returned ten messages in the tested case. The continuation URL remains the service-provided `_metadata.backwardLink`; its contents should be treated as opaque. One successful page-size sample does not establish a universal page-size cap or exact ordering guarantees across message types.

The message endpoint uses a conversation ID in its path, so retrieving history does not require a conversation-list URL to be present. Returned messages and pages may contain timestamps, sequence IDs, message types, HTML content, and pagination metadata as described above.

## Self chat and sender identity — 2026-09-30

Read-only inspection of the signed-in Teams Web session showed a self-chat row
labelled with the current user's name and “(You)”. Its selected conversation
header identified the thread as 48:notes; the participant list contained only
the signed-in user. No message was sent or edited.

Using the app-owned Teams session:

- The complete CSA startup result contained 943 rows and did not include
  48:notes.
- The paginated Chat Service catalog contained 960 rows and did include
  48:notes.
- A direct read of message history for 48:notes succeeded even though it was
  absent from CSA.
- In the returned message shape, from was a composite string rather than a
  bare ID. It contained 8:orgid followed by the account object ID, which
  matched the signed-in account's MSAL object ID. This provides a stable
  identity check that does not rely on display-name equality.
- The standard Chat Service conversation-members request returned an error for
  48:notes in this session. Its status and response body were not retained, so
  the exact failure reason is unknown.

The connector synthesizes a normalized self-chat row when CSA omits it and
extracts the sender's canonical MRI from the composite from field. These are
private Teams API observations from one account and build, not a Microsoft
contract. Public third-party code also uses 48:notes for Teams self-chat, but
that is corroboration rather than a guarantee that the private identifier will
remain stable.
An example of third-party corroboration is the
[msgvault Teams package](https://pkg.go.dev/go.kenn.io/msgvault%40v0.20.0/internal/teams),
which names that self-chat ID; it is not an official Microsoft API reference.

## Server-side filtering and pagination probe

Read-only live requests on 2026-09-29 tested the private Chat Service routes
against the signed-in account. No message bodies, chat names, identifiers, or
credentials are included here.

The browser automation interface did not expose the Teams tab's DevTools
Network event log, so these request behaviors were tested with direct,
read-only calls using the app's existing signed-in session instead of claiming
that the browser's requests were intercepted.

- `GET /v1/users/ME/conversations?view=mychats&pageSize=3` returned three rows
  and an opaque `backwardLink`. A full traversal with `pageSize=500` returned
  960 unique rows across three pages. No server-side conversation-name, type,
  or activity-date filter was identified in the tested request shape.
- A read-only probe on 2026-09-30 requested `pageSize=25`; the response had 24
  conversation rows and an opaque `backwardLink`. Following that exact link
  returned 24 more rows with no overlap. The two requests took about 1.5 and
  1.3 seconds in this session. This verifies cursor-based incremental page
  retrieval for the tested account/build; it does not establish universal
  ordering, row counts, or page-size behavior.
- The conversation-index page sequence was not compared across the full
  traversal against last-activity timestamps or the Teams sidebar. Do not
  assume raw cursor order is the desired display order; sort the normalized
  catalog locally when activity ordering is required.
- Full-traversal timings on 2026-09-30: with `pageSize=500`, three sequential
  pages returned 961 unique raw rows in 6.67 seconds of page requests (2.88,
  2.47, and 1.31 seconds); authentication took 1.69 seconds, for 8.36 seconds
  total. With `pageSize=25`, 42 pages returned the same 961 unique raw rows in
  55.44 seconds of page requests (mean 1.32 seconds, range 0.84–1.57);
  authentication took 2.28 seconds, for 57.73 seconds total. Neither run
  needed a retry. These are single-session timings for the raw catalog, which
  may contain non-chat rows; they are not a universal performance guarantee.
- The CSA startup response returned 943 chats and reported `isPartialData=false`
  and `hasMoreChats=false`; every CSA ID was present in the raw Chat Service
  list. The raw list had 17 additional rows: one notes-to-self conversation
  (`StreamOfNotes`), four `TeamsTeam`, six `TeamsStandardChannel`, one
  `TeamsSharedChannel`, and one each of `StreamOfNotifications`,
  `StreamOfMentions`, `StreamOfCallLogs`, `StreamOfAnnotations`, and
  `StreamOfGeneric`. Thus, in this snapshot, the only raw-only chat-scope row
  was notes-to-self; the other 16 rows were team/channel entities or service
  streams. Product type labels were read from `threadProperties`.
- The connector's catalog scope treats CSA chat/meeting rows as canonical,
  supplements the known `48:notes` row, and uses the paginated raw feed as a
  type-filtered fallback if CSA reports partial data or omits a completeness
  flag. The fallback accepts chat, one-to-one chat, meeting, and the known
  self-chat ID; it excludes identified team/channel and service-stream types.
  An unrecognized raw-only type is excluded and reported as incomplete coverage.
  This is application behavior based on the observed payloads, not a Microsoft
  contract.
- A first-page request with `pageSize=3` returned three messages. A 100-message
  page was also observed. This verifies those samples only, not a universal
  page-size limit.
- A nonzero `startTime` representing a seven-day lower cutoff returned only
  messages at or after that cutoff, with no older continuation page in the
  tested conversation. Supplying the exact timestamp of a message included
  that boundary message. A date-bounded meeting conversation returned four
  system activity messages inside the requested interval.
- Direct `backwardLink` traversal can return messages overlapping the previous
  page, so callers still need message-ID de-duplication.
- The legacy Chat Service route ignored `since=` and `$filter` parameters in
  tests. Its `endTime` behavior did not reliably include ordinary messages at
  the tested upper-bound edge, so no server-side `until` filter is enabled.

An additional focused history probe on 2026-09-30 used the user's selected
direct conversation with the existing app-owned session. The exact
display name resolved to one catalog entry; its two-member roster did not expose
the supplied email address, so the conversation identity was matched by that
unique display name rather than independently by email. No chat ID or message
content was retained in this note.

- An initial `pageSize=10` request returned ten messages and an opaque
  `backwardLink`. Following that exact link returned ten more messages, even
  when the caller passed `page_size=3` to the local page-reading method. The
  continuation URL itself retained `pageSize=10`; continuation links therefore
  control their own page-size behavior and must remain opaque.
- The two sampled pages had no overlapping message IDs. Each page was sorted
  oldest to newest by the client, and every timestamp on the continuation page
  was no newer than the first page. This is evidence for that conversation and
  sample only; it does not supersede the overlap observed in other history
  probes or establish ordering for system-event types generally.
- A request whose `startTime` exactly matched the first page's oldest message
  included that message. An exact `until` boundary was also included by the
  client-side filter, while messages newer than it were excluded from the
  returned page. The outgoing message request contained no `endTime` parameter.
- No transient retry was needed during this sample.

Teams Web bundles collected on 2026-09-28 contain GraphQL operations for
`messages(convId, replyChainId, cursor)` with `pageInfo.hasNextPage` and
`fetchRecentMessagesAfter(convId, replyChainId, lastMessageId, count)`. These
show cursor-based page loading and recent-message synchronization; the observed
operations did not expose an arbitrary date-range filter.

This is distinct from the documented Microsoft Graph chat-message API, which
documents `$top`, `$orderby`, and `$filter` for a date range on
`lastModifiedDateTime`, under a separate permission model. Graph support does
not prove that the private Teams Chat Service accepts those parameters. A
Microsoft Q&A moderator's answer says the Search API cannot scope a search to a
specific chat/channel in one call; it recommends filtering results client-side
by chat ID, or retrieving a specific chat's messages without keyword search.
Maxim-Mazurok/teams-api independently corroborates the private route and
continuation-link pattern, but its implementation is not a Microsoft API
contract. See [Graph list chat messages (beta)](https://learn.microsoft.com/en-us/graph/api/chat-list-messages?tabs=powershell&view=graph-rest-beta), [Microsoft Q&A on scoping Teams search](https://learn.microsoft.com/en-us/answers/questions/5510799/does-the-teams-search-api-provide-any-operator-to), and [Maxim-Mazurok/teams-api chat-service client](https://github.com/Maxim-Mazurok/teams-api/blob/main/src/api/chat-service.ts).

## Delta synchronization and open uncertainties

The CSA response exposes `metadata.syncToken`, and public captures show an
`x-ms-synctoken` request header for `teams/users/me/updates`. The live probes
above show that omitting the header returns a full-sized catalog response,
while sending a fresh token returns an empty response with the same token.
This verifies the no-change path but does not prove what a nonempty delta
contains or how the client applies it.

The following details remain unverified or may vary by tenant/build:

- whether `metadata.isPartialData` or `metadata.hasMoreChats` can occur in ordinary full startup responses, and how all additional chat groups are requested;
- whether CSA’s timestamp-based ordering remains aligned with the Teams UI for ties, missing timestamps, hidden chats, and every chat type;
- whether `isSticky` maps to Favorites or another Teams concept;
- whether message pages are strictly monotonic by timestamp for all message and system-event types;
- the precise category of a raw JSON `members` event when the inspected record does not include its enclosing message type;
- whether `lastCompleteSegmentStartTime` and `lastCompleteSegmentEndTime` can safely terminate date-bounded history retrieval;
- exact page-size limits and whether all continuation links retain the original requested page size;
- which CSA headers beyond the resource bearer token and Skype token are mandatory across tenants.
- how nonempty delta responses represent added, changed, or removed chats, and whether they can be paginated;
- how the client should recover from an expired, rejected, or otherwise unusable sync token.

For the catalog implementation, `catalog_complete` is intentionally scoped:
it means CSA reported complete, or that a paginated fallback finished without
an unclassified raw-only row. It does not prove that every hidden, archived,
or tenant-specific conversation is exposed by either service. When CSA marks
its catalog complete, the connector does not make an extra raw-feed request on
every listing; it includes the separately observed notes-to-self ID directly.
When CSA is partial or its flags are absent, the full `backwardLink` chain is
followed. Untitled fallback one-to-one chats can be named through a read-only
thread-members lookup when the roster provides one resolvable peer; this lookup
is bounded, and the catalog coverage reports unresolved fallback names.

---

# Live catalog diagnostic — 2026-10-01

This section records a later, read-only diagnostic of the current account's
CSA startup/bootstrap/delta responses, Chat Service conversation rows and
rosters, Middle Tier profile lookups, and the notes-to-self message history.
The evidence came from one account and one Teams build; the row counts and
field combinations below are observations, not protocol guarantees. Private
message bodies, credentials, and sync cursors are intentionally omitted.

## Observed raw API behavior

### Conversation classification fields

| Conversation kind | CSA fields observed | Chat Service fields observed |
|---|---|---|
| One-to-one | `chatType="chat"`, `isOneOnOne=true` | `threadType="chat"`, `productThreadType="OneToOneChat"` |
| Group | `chatType="chat"`, `isOneOnOne=false` | `threadType="chat"`, `productThreadType="Chat"` |
| Meeting | `chatType="meeting"`, `threadType="meeting"`, `isOneOnOne=false` | `threadType="meeting"`, `productThreadType="Meeting"` |
| Notes to self | Not present in CSA startup, bootstrap, or the inspected delta | ID `48:notes`, `threadType="streamofnotes"`, `productThreadType="StreamOfNotes"` |
| Channel | Present in the CSA `channels` collection | Chat Service uses product types such as `TeamsStandardChannel` and `TeamsSharedChannel` |

Ordinary group rows can also contain a `meetingInformation` object populated
with placeholder values, including null subjects and year-one timestamps.
That object's presence did not indicate a meeting; the explicit type fields
did.

### Identifiers seen in the captured responses

The following patterns occurred in this data. They are observed conventions,
not a complete or formally documented type system:

- `19:meeting_...@thread.v2` — an observed meeting conversation ID.
- `19:...@thread.v2` — observed thread-based chat and group IDs.
- `19:<id>_<id>@unq.gbl.spaces` — an observed unique one-to-one-style ID.
- `8:orgid:<GUID>` — an observed organizational-user MRI.
- `28:<id>` — an observed app/bot participant MRI.
- `48:notes` — the observed notes-to-self stream ID.

An ID prefix can help as a defensive hint when type metadata is absent, but
these samples do not establish that every conversation can be classified by
its identifier alone.

### App conversation naming

An untitled one-to-one row had a complete two-member roster consisting of the
signed-in user and an app participant with an MRI beginning `28:`. Its latest
message sender matched that app participant. The message also contained
`lastMessage.properties.fromAppMetadata.name` and
`lastMessage.properties.fromAppMetadata.id`. The app metadata ID is a separate
identifier from the `28:` MRI in this sample; they should not be assumed to be
the same identifier.

### Untitled group names

Some untitled group rows had no usable title/topic/display-name field in either
CSA or Chat Service. Their Teams UI labels appear to be synthesized from the
roster and resolved profile names; the generated label was not present in the
raw conversation row. A complete roster included the signed-in user, while
some member records lacked names and needed profile resolution.

### Notes-to-self activity and catalog counts

- `48:notes` was absent from the inspected CSA startup, bootstrap, and delta
  responses, but present in Chat Service as `StreamOfNotes`.
- A direct Chat Service history request for `48:notes` returned its latest
  message and timestamp. At diagnostic time, the local catalog's stored
  self-chat timestamp matched that latest message timestamp.
- In this snapshot, CSA startup/bootstrap each contained 944 chat rows and 7
  channel rows. The inspected delta contained 5 chat rows and no channel rows.
  The paginated Chat Service catalog contained 961 unique conversation rows.

These counts are account/build/time specific. They do not promise that every
tenant exposes the same catalog scope or that every delta contains all changed
conversation types.

## Inference and hypothesis

- `productThreadType` carries more specific Chat Service distinctions than the
  generic `threadType="chat"`; in the observed rows it separates one-to-one
  chats (`OneToOneChat`) from groups (`Chat`).
- The Teams UI appears to compose missing group labels from roster/profile
  data rather than receiving a server-generated conversation title.
- The self-chat's absence from CSA explains why CSA-only activity updates
  cannot reliably refresh its ordering. A one-message Chat Service history
  read supplies the missing activity timestamp.
- App metadata is a safe naming hint only when it is tied to the sole `28:`
  peer and that same peer sent the latest message. The metadata name alone is
  not evidence that the conversation itself is an app chat.

These are inferences from the captured payloads, not claims about undocumented
Microsoft guarantees.

## Implementation decisions

- Normalize both CSA and Chat Service rows to the stable catalog values
  `meeting`, `group`, `oneOnOne`, `channel`, or `chat` for notes-to-self;
  leave ambiguous types unknown. An explicit meeting type wins over the
  generic `isOneOnOne` flag, and a complete roster larger than two prevents an
  impossible one-to-one classification. `meetingInformation` alone is never
  used to classify a meeting.
- Use one CSA row normalizer for startup and updates so equivalent raw rows
  produce the same summary.
- Use `fromAppMetadata.name` only for an untitled, complete two-member
  one-to-one whose non-self member is a `28:` identity and whose latest sender
  matches that member. Do not send `28:` identifiers to the organizational-user
  profile lookup route.
- Generate a missing group label only from a complete roster. Exclude the
  signed-in user's name from the visible list, resolve other names from the
  roster/top-level users/batched profiles, sort case-insensitively, and join
  full names with ` | `. Add names until the next full name would exceed 87
  characters; if no name fits, truncate the first at 80 characters with an
  ellipsis. `+N` counts every roster participant not shown, including the
  signed-in user and unresolved/app participants. If no participant name can
  be resolved, retain the safe ID fallback.
- Keep self-chat enrichment in `TeamsService`, which combines CSA deltas with
  the persistent local catalog. The connector normalizes raw transport rows;
  the service owns merged-catalog activity and ordering. The service's
  one-message lookup refreshes `48:notes` after catalog updates without
  changing Teams state.
- Derive the UI's type label and fallback icon from the same canonical
  conversation presentation kind. The ID prefix is only a fallback when the
  normalized type is absent.

# Source notes

Primary sources used for these notes:

1. A production Teams Web JavaScript build and HTML entry document collected on **2026-09-28**, inspected for route templates, resource names, headers, and client behavior.
2. Read-only live requests made with an explicitly authenticated Teams session on **2026-09-29**, **2026-09-30**, and **2026-10-01**, including CSA startup and delta, Regional Chat Service catalog pagination, rosters and message history, and Middle Tier profile lookup.
3. The independent reverse-engineered project [Maxim-Mazurok/teams-api](https://github.com/Maxim-Mazurok/teams-api), used as corroborating implementation evidence.
4. Teams Web IndexedDB architecture notes from [gediz/teams-web-chat-exporter](https://github.com/gediz/teams-web-chat-exporter).
5. Public references including AADInternals, purple-teams, Microsoft Learn/Q&A, Microsoft Tech Community, and SkPy discussions.
6. [Microsoft Graph system messages for Teams](https://learn.microsoft.com/en-us/graph/system-messages), for the documented event-message model and distinctions among membership and meeting-policy events.
7. [Microsoft Learn Azure Communication Services chat message examples](https://learn.microsoft.com/en-us/rest/api/communication/chat/list-chat-messages/list-chat-messages?view=rest-communication-chat-2020-09-21-preview), for corroborating Skype-style `addmember` and `topicupdate` payload envelopes.

Third-party projects and captures are useful corroboration, not authoritative Microsoft API contracts. Routes and payload details can change without notice.

## Microsoft 365 search capture and source audit (2026-10-10)

**Evidence labels:** CAPTURED means the supplied Teams web HAR contains the request/response;
SOURCE means a targeted read of the downloaded Teams web client bundle found a builder or parser.
Neither label proves the browser routes are usable through this connector's app-owned
authentication. No captured tokens, cookies, query values, result text, account/site identifiers,
or raw capture data are retained here.

### Standalone connector known-hit behavior (2026-10-10)

One user-authorized synthetic self-chat message was used for a bounded local-search check. A
four-minute scoped `search_messages` request in exact mode returned zero; after the scoped
`get_messages` call fetched one item, substring mode returned one match. Preparation and coverage
were complete for that requested scope, with one page and one remote item scanned. The connector
reported `server_side_search_used=false`. This validates local substring matching over a fetched
archive item. It does not validate Teams web Global Search, Substrate Search v2, or Graph
`/search/query`; no message text or identifiers are retained.

### Named meeting-chat lead check (2026-10-10)

The user-provided prior walkthrough identifies two meeting chats containing transcript-related
material, reached through the recap UI and the meeting chat UI. The current Teams catalog matched
both named chats. Bounded `get_messages` reads returned complete remote history in one page per
chat (18 and 9 items); seven items in each contained a transcript/recap-related text signal. Chat
names, message bodies, and identifiers were not retained. No transcript URL was followed and no
transcript content was requested. This establishes accessible message-history context for the
known examples, not online transcript discovery, artifact access, or full-text transcript search.

The sanitized message-envelope check found `RichText/Media_CallRecording` and
`RichText/Media_CallTranscript` types in both histories. In each chat, five message items
contained an `amsTranscript` XML reference and one contained an ODSP transcript-path signal;
counts may overlap and are not distinct transcript objects. No URI was followed. This supports
transcript-reference discovery through meeting chat history only and leaves the recap download
permission boundary intact.

### Global Teams search

**CAPTURED:** submitted global-search requests use `POST /searchservice/api/v2/query`. The
request shape has `AnswerEntityRequests` and/or `EntityRequests`; the observed flows include the
entity categories `File`, `People`, `Message`, and `Event`. Search-box suggestions use a separate
`/search/api/v1/suggestions` route, while `/search/api/v1/events` is telemetry and not a search
result provider. Do not infer a result category or standalone contract from either of those
adjacent calls.

**CAPTURED, adjacent shell state:** the global-search HAR also contains two HTTP 200 calls to
`GET /api/csa/{region}/api/v1/teams/users/me/pinnedChannels`. This supplies pinned-channel state;
it is not the chat-list GraphQL operation or a global-search result request.

#### Search-result activation and exact-message navigation (SOURCE, 2026-10-11 audit)

The supplied Teams bundle contains a search-result activation path that reads a result's
`primaryAction.navigationId` and dispatches to the chat or channel navigation layer. The chat
branch passes the conversation target, `replyChainId`, `messageId`, a generated `searchSessionId`,
`dateTimeSent`, and (when enabled) `textToHighlight` to `chat.chatById`. The channel branch passes
the channel target, team identity, `replyChainId`, `deepLinkId`/message identity, and highlight
options to `channel.channelMessagesById`. A separate deep-link handler sets
`focusOnLoad=DeeplinkedMessage`. This is the client-side mechanism that lets Teams address and
focus a particular hit without a stable page number.

The same bundle's Message-result normalizer consumes `InternetMessageId`, `ClientThreadId`,
`DateTimeSent`, `ThreadSubType`, `ClientConversationId`, and `ParentMessageId`. It sets the UI
result `id` from `InternetMessageId`, uses `ClientThreadId` as the chat target, and builds the chat
`navigationId` from `replyChainId;ClientThreadId;DateTimeSent`. For one-to-one chats the source
derives `replyChainId` from `ParentMessageId` or `InternetMessageId`; other chat types parse it from
`ClientConversationId`. The eight captured Message `Source` objects share the core fields
`InternetMessageId`, `ClientThreadId`, `ClientConversationId`, and `DateTimeSent`. `ThreadSubType`
and `ParentMessageId` were not top-level keys on those captured `Source` objects, so their
extraction into the normalizer's input is not proven by this HAR. The core captured fields and
source transformation align, while the component caller and follow-up history request remain
untraced.

The capture set contains no request to a message-history route and no event-level click trace, so
the exact HTTP sequence started by these navigation commands remains unverified. The existing
Chat Service history route accepts `startTime` and opaque continuation state; using the result's
`dateTimeSent` as the remote lower-bound anchor is plausible, but the source audit did not prove
that mapping. The identity and timestamp passed to the navigation layer are established; their
wire-level use and surrounding-message window are not.

#### Captured Powerbar People suggestions

**CAPTURED, separate suggestion surface:** the supplied Teams HAR contains `POST
/search/api/v1/suggestions?scenario=powerbar` requests to Substrate. Captured request bodies use
`EntityRequests` inside a body that also includes `Scenario` (`Name`, `Dimensions`), `Cvid`,
`AppName`, `LogicalId`, and `dataSource`; the People request asks for
`QueryString`, `NormalizedQueryString`, and `DisplayQueryString`, plus `Size`, `From`, `Fields`,
`Filter`, and `Provenances`. One observed request asked for
five People suggestions and fields including `Id`, `MRI`, `DisplayName`, `EmailAddresses`,
`PeopleType`, `PeopleSubtype`, `Phones`, `UserPrincipalName`, `GivenName`, `Surname`, `Cid`,
`CompanyName`, `JobTitle`, `ImAddress`, `ExternalDirectoryObjectId`, `ConcatenatedId`, and
`Department`. Those are the fields requested by that UI build, not a guaranteed response schema.

Across the nine captured Powerbar HTTP 200 POST entries, one response contained a `Groups` entry
with `Type: People` and one suggestion. The response envelope also included `LayoutHints` and
`Instrumentation`. The suggestion had field names `Id`, `MRI`,
`DisplayName`, `EmailAddresses`, `ExternalDirectoryObjectId`, `PeopleSubtype`, `PeopleType`,
`PropertyHits`, `QueryText`, `ReferenceId`, `Text`, and `UserPrincipalName`; no values are retained.
Two category-limited requests that asked for People and File returned a File group only. In these
responses, an absent People group therefore cannot by itself distinguish an empty category from
an unsupported or denied one. The capture establishes a concrete request and one successful People
result envelope, but not matching semantics, paging behavior, completeness, or a stable schema.

This `scenario=powerbar` flow differs from the separately documented
`scenario=peoplepicker.newChat` suggestion request and from Middle Tier `/beta/users/searchV2`.
Neither suggestion scenario is the full global-search result request at
`/searchservice/api/v2/query`.

**SOURCE:** the client maps `FILEDOMAIN` to `File` and `MEETINGDOMAIN` to `Event`. Search request
builders expose fields including `EntityType`, `Fields`, `From`, `ContentSources`, `Query`, `Size`,
`Sort`, `TopResultsCount`, `Filter`, `PropertySet`, `ResultsMerge`, `HitHighlight`, `IdFormat`,
and `RefiningQueries`. Client page-size constants observed for Files, Files grid and Meetings are
25, 24 and 25 respectively; these are UI defaults, not a service guarantee. Exact request
variants and response hydration remain tied to their individual UI actions.

The global meeting-search default request body uses `Event`, `Exchange`, and
`SupportedResultSourceFormats`/`PreferredResultSourceFormat` hints. Source file parsers use fields
such as `FileName`, `Title`, `ReferenceId`, `FileType`, `FileExtension`, `AccessUrl`,
`AlternateAccessUrl`, `SPWebUrl`, `SpoUniqueId` and `SpoDocId`; additional SharePoint and
conversation-provenance fields are present. These names help identify result kinds and possible
follow-up references, but do not prove that each reference is readable through Graph. The exact
global Files content-source defaults remain unresolved.

The app-owned Microsoft Search probe recorded earlier in this file returned a generic HTTP 400;
this later browser capture does not change that standalone result. Graph `driveItem` search works
in the tested profile, while Graph `chatMessage` search was denied there. Those outcomes are
account-specific; see the dated G1 notes in `teams-connector-roadmap.md`.

### Meet search and recap

**SOURCE, separate Meet app builder:** `searchEvents` submits `POST /search/api/v2/query` with a
lower-camel `entityRequests` body for `Event`, `Exchange`, start/end date bounds, a proximity time
sort, and an initial `from=0`, `size=30`. Its parser reads
`EntitySets[0].ResultSets[0].Results[].Source`. This source route has not been tied to the global
Teams Meetings tab captured in the HAR and its page size must not be generalized to other flows.

**CAPTURED + SOURCE, meeting recap:** recap calls return `EntitySets[].MeetingInsights`. The
client's `getMeetingRecapCatchupItems` and `getMeetingRecapCallSummary` builders send
`EntityRequests[].QueryParameters` with entity type, entity id and context; time-bounded variants
also add start/end date, top count and a meeting-thread filter. The recap parser reads
`Source.MeetingRecording`, `MeetingSummary`, `PointsOfInterest` and `Speakers`. Suggestions for
meeting results link to chat plus recap for past meetings, or chat plus join for current/upcoming
meetings. A returned `PaginationSessionId` has been observed, but a caller feeding it to a next
request has not been found; continuation is unproven.

### Transcript retrieval

**CAPTURED + SOURCE:** the recap flow uses distinct retrieval families: legacy AMS transcript
view/status, Meeting Intelligence storage/scope, and ODSP drive-item transcript/media content.
The source route templates include `GET /v1/objects/{object}/views/transcript` and `/status`,
`/MeetingIntelligence/TranscriptService/storage/{storage}/scope/{scope}/`, and
`/drives/{drive}/items/{item}/media/transcripts/{transcript}/content`. The HAR recorded three HTTP 200 GETs to `/_api_cached/v2.1/drives/{drive}/items/{item}/cdnmedia/transcripts`. They are
per-artifact retrieval routes, not evidence of a global transcript text-search provider. The
storage/scope builder sets `isChannel=true` for channel meetings; this is an observed client
branch, not proof that other transcript retrieval is limited to non-channel meetings.

The captured ODSP transcript GET includes query keys `cTag`, `P1`–`P4`, `isformatjson`,
`transcriptkey`, `applyhighlights`, `applymediaedits` and `kid`. Their full semantics and any
continuation relationship remain unresolved. In the downloaded source, the ODSP media URL builder
conditionally adds `applymediaedits=true`; this is a content-control flag, not a pagination token.
No global transcript-text-search request or result attribution has been established in this captured path; the later Recaps UI observation is a separate in-page search surface.

In this Recaps capture, the `driveId`/`driveItemId` pairs returned in three meeting-resource
records also appear in ODSP cached-media requests and Microsoft Graph DriveItem preview requests.
That establishes a concrete metadata-to-storage-ID link for those three captured artifacts; it
does not map global Search v2 file hits to Graph IDs or establish the preview response contract.

### Teams Meeting Recaps live replay (2026-10-10)

**LIVE UI:** the Meeting Recaps app opened both user-provided known recaps directly. Each showed a loaded Transcript tab with an in-page search control. A generic query returned transcript matches in one target; no transcript text was copied into research notes. The Download control was disabled by the app permission boundary for one target; the viewer had already loaded transcript text, but no transcript file was exported or saved. This is evidence for a working signed-in web UI path, not an app-owned connector token or a global search contract.

The global Teams Search page exposed visible filters All, Messages, Files, People, Channels, Group chats and Meetings; Meetings displayed 25 rows for a common query. No Transcript filter was visible. A “2 more items” control opened, but its entries were not exposed in the accessibility tree, so hidden filters remain unknown.

**CAPTURED, meeting list:** `GET /api/mcps/{region}/user/meetings/contents` used `startDateTime`, `endDateTime`, `includeTypes` and `top`; observed category values were `MeetingChat`, `MeetingStage`, `Notes`, `Recording` and `TranscriptV2`. The response carried `value` and `paginationInfo`, including `hasNextPage`. A caller following that signal to a next page was not established.

**CAPTURED, per-meeting resources:** `GET /api/mcps/{region}/contents/` selected resources using `threadId` and, in variants, `recapCallId`, `iCalUid`, `organizerId`, `isSeriesMeeting` or `organizerEmail`. The response wrapper included `resources`, `paginationInfo`, `changeKey`, `deletedResources`, `invalidResources` and filtered-count metadata. Resource types included `AISummary`, `MeetingStage`, `Recording`, `TranscriptV2` and `notes`. Resource records exposed `addedBy`, `additionDateTime`, `id`, `mcoResourceId`, `metadata`, `subType` and `type`.

The observed resource metadata key names included `callId`, `containerId`, `correlationId`, `driveId`, `driveItemId`, `endTime`, `fileId`, `fileTitle`, `fileType`, `iCalUid`, `instanceICalUid`, `meetingJoinUrl`, `organizerId`, `organizerTenantId`, `ownerId`, `ownerTenantId`, `sharePointOnlineId`, `startTime`, `threadId`, `transcriptId`, `userId` and `webUrl`. Values are omitted; these are observed fields, not a complete schema or proof each pointer is retrievable.

**CAPTURED, recap content:** `POST /api/mcps/{region}/meetingcatchup/multilingual/aicontent` accepted a body with `EntityRequests`, `Flights`, `Scenario` and `TargetLanguage`; its response used `EntitySets[].MeetingInsights`. This is a recap-content path, separate from global search.

**SOURCE bundle boundary (2026-10-10):** the supplied Teams web bundle contains Search v2 and Meeting Recaps assets, but a targeted scan found no transcript-viewer search-control strings (`Search the transcript`, `transcriptSearchBox`, `TranscriptSearch` or `scrollbarMarkerContainerInternal`). The live page places the transcript viewer in a separate embedded SharePoint/Stream component whose source was not included. This bundle therefore does not show whether in-page search is client-side or server-side, or which request produced its matches.

**CAPTURED, global Transcript category:** three Search v2 requests to `/search/api/v2/query` specified `EntityType=Transcript`, `ContentSources=Teams` and a query string; all returned HTTP 200. One parseable response had a result count of zero and `MoreResultsAvailable=false`. The other two bodies were not interpretable as structured results, so they do not establish zero hits. This does not contradict the in-page transcript search above: the UI request that produced its matches was not mapped to a network route.

**CAPTURED, transcript artifact retrieval:** three HTTP 200 GETs used `/_api_cached/v2.1/drives/{drive}/items/{item}/cdnmedia/transcripts`. Query-key names included `cTag`, `P1`–`P4`, `isformatjson`, `transcriptkey`, `applyhighlights`, `applymediaedits` and `kid`; their semantics and continuation behavior remain unresolved. The Teams source bundle's separate ODSP media builder uses `/drives/{drive}/items/{item}/media/transcripts/{transcript}/content`. Neither route is a global text-search route.

### Supporting recap, event, and tab hydration routes (CAPTURED, 2026-10-11 audit)

The October 10 Recaps capture also contains successful service calls that sit beside search and
transcript retrieval. They describe how the UI discovers meeting resources and loads adjacent
context; they are not additional global search providers.

| Route | Captured shape | What it establishes / leaves open |
|---|---|---|
| `GET /api/mcps/{region}/user/meetings/contents` | Four HTTP 200 responses; query keys `startDateTime`, `endDateTime`, `includeTypes`, `top`. Response envelope includes `value` and `paginationInfo.hasNextPage`; resource records include `id`, `type`, `subType`, `addedBy`, `additionDateTime`, `mcoResourceId`, `metadata`, and `webUrl`. | Meeting-content discovery for the requested date/type window. The capture did not advance a page, so `hasNextPage` continuation is not exercised. |
| `GET /api/mcps/{region}/contents` | Thirteen HTTP 200 variants using `threadId` with `iCalUid`, `organizerId`, `organizerEmail`, `recapCallId`, or `isSeriesMeeting`; one request used `threadId` alone. The response includes meeting-resource metadata fields listed above. | Per-meeting resource lookup across several correlation keys. It does not prove that every key is required or interchangeable. |
| `GET /api/mcps/{region}/collab/readcollabobject/V2/{opaqueResourceKey}` | Five HTTP 200 responses; no query string. Interpreted response fields include `id`, `type`, `subType`, `metadata`, `addedBy`, `additionDateTime`, and `webUrl`. | A collaboration-object hydration path used by the Recaps workflow. The opaque key composition and resource selection rules remain unmapped. |
| `POST /search/api/v1/recommendations` | Five HTTP 200 responses; `EntityRequests` plus `Scenario`, with a `MeetingCatchUp` request. Interpretable responses include `EntitySets[].MeetingInsights`; some response envelopes were empty or not interpretable. | A separate recap/recommendation provider. `PaginationSessionId` appears in recap data, but no follow-up request using it was captured. |
| `POST /api/mcps/{region}/meetingcatchup/multilingual/aicontent` | One HTTP 200; request keys `EntityRequests`, `Flights`, `Scenario`, `TargetLanguage`; response includes `EntitySets[].MeetingInsights`. | Recap/AI-content retrieval, distinct from Search v2 discovery and transcript artifact retrieval. |
| `GET /api/apps/part/{region}/beta/chats/{chatId}/tabs` | One HTTP 200; response envelope has `value`. | Chat-tab discovery for the recap-associated chat; no search-result identity or history page is returned. |
| `GET /api/chatsvc/{region}/v1/threads/{threadId}/consumptionhorizons` | Two HTTP 200 responses; no query string; response exposes an `id` field. | A thread-level metadata call made during recap navigation. The captured response does not establish message-window bounds or a hit-to-page mapping. |
| `GET /api/teamsevents/{region}/beta/events/{eventId}` | One HTTP 200 event-detail response, with an event identity/title and participant/profile-shaped fields. | Event hydration after meeting discovery; this does not establish attendance semantics or an attachment download. |
| `GET /api/mt/part/{region}/v2.0/me/calendars/events/{eventId}?shouldDecryptData=...` | Two HTTP 200 responses. | Middle Tier calendar-event detail route. The response schema was not established in this pass. |
| `GET /_api_cached/v2.1/drives/{drive}/items/{item}/cdnmedia/thumbnails` | Three HTTP 200 image responses; query keys include `cTag`, `P1`–`P4`, `kid`, `thumbnaildescriptor`, and `thumbnailkey`. | Thumbnail retrieval adjacent to recap/transcript resources, not file search or transcript paging. |
| `POST https://graph.microsoft.com/v1.0/drives/{drive}/items/{item}/preview` | Four HTTP 200 POSTs plus two successful OPTIONS preflights, across three distinct drive/item pairs. | The three drive/item pairs match `driveId`/`driveItemId` in three MCPS meeting-resource records. This confirms those recap artifacts can be handed to a Graph preview route in this session; the request/response contract and relation to any Search v2 file hit were not established. |
| `GET /_api/v2.1/sites/{site}/drive/items/{item}/media/visualSegmentFrames/{frame}/content` | Twenty HTTP 200 `image/jpeg` GETs with twenty successful OPTIONS preflights; no query keys. | Visual-frame media hydration adjacent to recap playback. It is not a transcript-text search or global file-search result route. |
| `POST /ups/{region}/v1/presence/getpresence` and `POST /ups/global/v1/presence/getpresence` | Two regional HTTP 200 responses and two global-alias HTTP 404 responses. | Capture-specific presence lookup. The 404 does not establish a general service restriction. |
| `POST /ups/{region}/v1/pubsub/subscriptions/{subscriptionId}` | Two HTTP 200 responses in the Recaps capture. | Shell subscription activity; payload semantics and relation to search-result hydration are not established. One additional HTTP 200 call appears in the separate global-search capture. |
| `PUT /api/mt/part/{region}/v1/schedulingService/meetings` | Five HTTP 200 responses; request body included `SchedulingServiceUpdateUrls`. | Adjacent scheduling update traffic. This is a write route, was observed but not replayed, and is not a search/read contract. |

These additional routes are recorded to close the Recaps-HAR endpoint inventory. They are shell or
adjacent-UI support calls, not search result providers. The scheduling `PUT` was observed only and
was not replayed.

These browser-session routes and results are useful reverse-engineering evidence, but do not prove their use with this connector's sign-in. Keep P2.2 pending until a bounded known-hit operation succeeds through app-owned authentication and returns a useful pointer or resource.
### Captured response and paging details

**CAPTURED:** the Teams flow made four submitted Search v2 POSTs, all HTTP 200: two mixed searches,
one File-only request, then one Event-only answer request. The File-only request used `From=0`,
`Size=25`, and requested Exchange, OneDriveBusiness and SharePoint sources; its response contained
25 File hits. The Event-only request used `AnswerEntityRequests`, returned 25 Event hits through
`AnswerEntitySets`, and exposed `Subject`, `Start`, `End`, `OrganizerName`, `OrganizerAddress`,
`Attendees`, `EventId`, `OnlineMeeting`, and sometimes `EventConversationId`. It also reported
`Total=92` with `MoreResultsAvailable=false`; treat that total as a captured field, not a verified
pageable count. Mixed-search responses can contain both `EntitySets` and `AnswerEntitySets`.

Across the explicit search requests, every observed `From` was zero. Scope changes are captured,
but there is no request advancing to a later page. `MoreResultsAvailable=true` appeared on other
successful file-search responses, so continuation is signaled by the response but an actual
continuation request has not been captured. Some successful responses contain only `ApiVersion`,
`SearchTerms` and `Instrumentation`; those do not establish a zero-hit result.

#### Captured hit identity fields (2026-10-11 audit)

The parseable mixed Teams global-search response contained eight `Message` hits, three `File` hits,
and five `Event` hits. Across the eight message `Source` objects, the captured common field names
included `Id`, `InternetMessageId`, `ConversationId`, `ClientConversationId`, `ClientThreadId`,
`DateTimeSent`, `DateTimeReceived`, `DateTimeCreated`, `Preview`, and `Subject`. Thus this response
can tie a hit to a message identity, conversation/thread identity, and timestamp; it does not
provide a history-page number. The Web client's Message-result normalizer uses `InternetMessageId`
as the UI result id and composes the exact-hit navigation fields described above. It also consumes
`ThreadSubType` and `ParentMessageId`, whose extraction was not traced from the captured top-level
`Source` schema. `InternetMessageId` has not yet been cross-checked against the IDs returned by
Graph/Chat Service history, so direct reuse for connector context fetching remains an explicit
validation step.

The file hits exposed SharePoint-oriented identity/navigation fields. Common fields included
`UniqueId`, `SiteId`, `WebId`, `ListId`/`ListID`, `SpWebUrl`, `Path`, and `LinkingUrl`; the 25-hit
File-only response also included `IdentityListItemId`, `IdentityListId`,
`IdentitySiteCollectionId`, `IdentityWebId`, `FileID`, and `ParentLink` in its result-field union.
These are useful browser/SharePoint pointers, but no captured mapping converts them into Graph
`driveId`/`itemId` values.

The mixed response's Event `Source` objects shared `EventId`, `Start`, `End`, and `Subject`; the
25-hit Event-only answer response exposed the same identity/time fields. The event-detail calls
were not correlated to a specific search hit. Field names here describe the captured response;
values were not retained.

The raw Search v2 result wrappers exposed `Id` and `Type`, but not a `PrimaryAction` object. The
source-level Message normalizer creates `primaryAction.navigationId` from the message fields in
`Source`; the complete component caller is not identified, but the captured source fields match
the normalizer's input contract.

Other observed request fields include `EntityType`/`EntityTypes`, `ContentSources`, `QueryString`,
`DisplayQueryString`, `QueryTemplate`, `From`, `Size`, `Fields`, `Sort`, `Filter`,
`RefiningQueries`, `HitHighlight` and `ResultsMerge`. Captured request builders mix field naming
conventions, so this is a field inventory, not a replayable request schema. A follow-up company-file
GET under `/_api/v2.0/drives/{drive}/items/{item}/{operation}` selected item metadata and returned
`value`; it establishes a metadata lookup after search, not a file-content download.

**CAPTURED recap follow-up:** three Graph batch calls each contained four drive-item GETs with
`$select` and returned an outer `responses` envelope. This is metadata follow-up evidence, not
proof that every search result is retrievable or that file contents were downloaded. Transcript
capture had three successful GETs with the same named query-key family; path values and keys
`P1`, `P4`, `cTag`, `kid`, and `transcriptkey` varied. None had a `Range` header or explicit
offset/page/cursor key, and only the first response body was retained. The response envelopes
therefore cannot be compared to infer transcript paging. These calls establish neither scrolling
pagination nor global transcript-text search.

**Next:** finish the bounded source-to-capture mapping for the ODSP keys and the global Meetings
filter, then compare only supported, app-owned Graph operations. Keep broad discovery, recap
navigation and transcript retrieval as separate capabilities. Do not implement a browser-private
route based on capture success alone.

### Transcript URI reference in meeting chat history (2026-10-10)

A bounded read-only revisit of the two user-provided transcript-bearing meeting chats found five
`amsTranscript` marker occurrences in each complete message history. Each history also contained
one embedded `URIObject` with the sanitized route shape
`/drives/{drive}/items/{item}/{segment}/{segment}/media/transcripts/{transcript}/content`.
The observed `URIObject` attribute names were `format_version`, `type`, `uri`, `url_thumbnail`,
and `version`. The supplied Teams client bundle separately contains a builder for
`/drives/{drive}/items/{item}/media/transcripts/{transcript}/content`; the two extra path segments
in the message-embedded reference are not mapped.

This is an embedded URI reference, not a captured outgoing request. No URI value was retained or
followed, and no HTTP method, status, response shape, or standalone authentication behavior was
observed. Compare the reference with a sanitized browser network capture from the loaded transcript
viewer. Do not infer that the embedded URI can be fetched by the connector or use it to bypass the
separate download-permission restriction observed in the UI.

## Connector code-path audit (2026-10-10)

This is a static review of the current implementation, not a latency benchmark or a product change.

- `src/teams_connector/service/search.py` prepares remote history for each chat sequentially before searching locally. It shares a maximum page budget across the requested chats and reports per-chat coverage/errors. Bounded concurrency is a promising latency candidate, but it must preserve the shared budget, honest coverage and cancellation behavior, and must be measured against service throttling.
- `src/teams_connector/store/db.py` builds a temporary scope table for each search and matches normalized message text with leading-wildcard `LIKE` patterns. At larger archive sizes, text matching may scan many in-scope rows. Benchmark the current path with synthetic archives before considering SQLite FTS; verify normalization, literal/substring behavior, accents, ordering and keyset pagination. Measure temporary-scope setup separately before replacing it.
- `src/teams_connector/remote/chat_service.py` and the history service validate continuation and detect repeated links. Keep these reliability checks unless a measured alternative preserves the same loop and completeness protections.
- Current `search_messages` remains local archive search after remote preparation; its response reports `server_side_search_used=false`. Faster local matching would not provide broad online Teams discovery or transcript search.

No measured latency improvement, request reduction, or safe code removal has been established. Next, use synthetic fixtures to record elapsed time and request/page counts across chat counts, history sizes and archive sizes; compare bounded concurrency and local index strategies independently. Do not mix these results with Graph-migration parity decisions.

## Message identity and jump-to-hit navigation (2026-10-10)

The Teams Web search result click was observed to open the selected message in its conversation. The
2026-10-11 source audit now identifies the client handler: it sends message identity and reply-chain
context into the chat/channel navigation layer, and the chat branch also passes `dateTimeSent`, a
generated `searchSessionId`, and highlight text. This explains exact-hit navigation without a
history-page number. The resulting network request and surrounding-window load remain uncaptured;
see [Search-result activation and exact-message navigation](#search-result-activation-and-exact-message-navigation-source-2026-10-11-audit).

Documented link forms:

- Chat: `https://teams.microsoft.com/l/message/{chatId}/{messageId}?context={...}`
- Channel: `https://teams.microsoft.com/l/message/{channelId}/{messageId}?tenantId=...&groupId=...&parentMessageId=...&teamName=...&channelName=...&createdTime=...`

Keep the evidence levels separate: Search v2 discovers hits; client navigation receives the exact
message identity/timestamp; the deep link identifies a destination message; Graph message listing
can hydrate history; the private request used by the Teams Web click-through remains uncaptured.
The two Teams message-history transcript URI references documented above are also reference-only
and do not establish a request contract.

Primary references: [Teams deep links](https://learn.microsoft.com/en-us/microsoftteams/platform/concepts/build-and-test/deep-link-teams), [Graph chat-message search](https://learn.microsoft.com/en-us/graph/search-concept-chat-messages), and [Graph get chatMessage](https://learn.microsoft.com/en-us/graph/api/chatmessage-get?view=graph-rest-1.0). Cross-product endpoint and workflow map: [Microsoft 365 search findings in the Outlook connector research](../../lrh-outlook-connector/docs/m365-search-findings-2026-10-10.md).

## Teams Web chat-list and People helper source audit (2026-10-11)

This targeted read used the supplied production Teams bundle captured on 2026-10-10. These
source findings describe that client build; module IDs and private service routes can change.

### People/search/profile source mapping

- `precompiled-web-worker-innerservices_i-666682b126a641d0.worker.js`, module `49965`, defines
  `PeoplePickerEndpoints`: Middle Tier `search`/`searchV2`, email/phone lookup helpers, and
  external-search routes. The route table above records request methods, body construction,
  feature-flag query parameters, response transforms, and the status handling found in that module.
- The same worker's module `66547` defines `fetchWorkingSet` against
  `regionGtm("peopleProfileService")`. It constructs the `maxSize`, `maxPageSize`, `skipToken`,
  and `deltaToken` query parameters and passes the JSON response through. This is a distinct URL
  family from `regionGtm("middleTier")`, despite sharing the Middle Tier service label in the
  registry.
- The captured `scenario=powerbar` request/response establishes one successful Substrate People
  suggestion envelope, as documented under [Global Teams search](#captured-powerbar-people-suggestions).
  It does not exercise any of the Middle Tier or People Profile Service helpers.
- The capture set therefore establishes source-level request contracts for several People
  helpers and one captured Substrate response shape. It does not establish server acceptance,
  completeness, or use with the connector's app-owned authentication for the source-only routes.
- The connector's current `GraphPeople` adapter uses Graph batching for `/users/{id}` display-name
  lookups and Graph profile-photo reads; it does not call these private People Picker or working-set
  routes. The source findings alone do not show that replacing the current Graph enrichment would
  simplify code or improve latency/reliability.

### Chat-list ordering source mapping

- The GraphQL operation `DataResolversChatChatListWithCachedTitleQuery` has separate recent,
  pinned, request, top-fixed, and suggestion/contact connections. Its recent-chat fields include
  the last-message `originalArrivalTime`; the operation alone does not prove whether the rendered
  edges preserve server order or are locally re-sorted.
- The schema exposes `PinnedChat.order` and `reorderPinnedChats`. A desktop warm-up path calls the
  query with pinned chats enabled and suggested contacts disabled, but the full visible-sidebar
  component-to-query-to-render chain and a live GraphQL request/response are absent from the
  supplied evidence.
- `precompiled-web-worker-innerd-7237396a5e2cabdb.worker.js`, module `9919`, defines
  `ACTIVITY` and `RECOMMENDED` comparator descriptors for the Teams/Channels folder connection.
  `ACTIVITY` sorts `lastMessageTimeUtc` descending, then lower-case display name ascending, then
  the `CT` timestamp ascending. `RECOMMENDED` sorts its favorite/recommended predicate descending,
  then the same name and timestamp keys ascending. The nearby filter and channel-connection
  context scopes these comparators to Teams/Channels; no source call ties them to `recentsChats`.

The exact recent-chat comparator, pin/section composition in the rendered sidebar, and equivalence
of any candidate backend to the visible UI order remain unverified. The source is enough to correct
the assumption that CSA or the raw Chat Service feed alone defines the sidebar, but not enough to
claim a canonical recent-chat ordering contract.
