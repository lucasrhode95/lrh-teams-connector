# Teams Web Private API Reverse-Engineering Notes

This repository explores how the Microsoft Teams web/desktop clients authenticate and communicate with Teams' private backend services, with the eventual goal of exporting one or more conversations directly from Python.

> **Important:** these are private, undocumented Microsoft interfaces. They can change at any time. Treat all endpoint shapes and fields as implementation details, not stable contracts.

The current Python direction is to hide all Teams-specific authentication, routing, pagination, and private API work behind a small `TeamsConnector` interface so the rest of the application does not care how messages are retrieved.

---

## Confidence legend

The notes below use three evidence levels:

- **Observed live** — successfully called against a real Teams tenant/account during this project.
- **Teams Web bundle** — literal endpoint/resource/route information found in a production Teams Web JavaScript build collected on 2026-09-28.
- **Public reverse-engineering evidence** — independently published traffic captures, open-source clients, Microsoft community/Q&A posts, or public PowerShell implementations. Useful corroboration, but potentially historical.
- **Reverse-engineered implementation** — endpoint shape used by the independent [`Maxim-Mazurok/teams-api`](https://github.com/Maxim-Mazurok/teams-api) project. Useful evidence, but not authoritative Microsoft documentation.

Where evidence conflicts, prefer live traffic from the current Teams client.

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

A key reverse-engineering question is whether the **CSA startup APIs** return chats in the exact order used by the Teams UI. The older regional Chat Service conversation endpoint does not appear to reproduce sidebar ordering reliably when sorted locally.

Public historical captures strongly suggest CSA is a stateful synchronization API rather than just another paginated list endpoint.

## Credential matrix

Different Teams backend services expect different credential combinations.

| Service / action | Bearer audience | Additional credential | Status in this project |
|---|---|---|---|
| Teams AuthZ / service discovery | `https://api.spaces.skype.com` | none | **Working live** |
| Regional Chat Service — conversations/messages/members | none required by the known request shape | Skype token from AuthZ | **Working live / implemented** |
| Middle Tier profile resolution | `https://api.spaces.skype.com` | none | **Implemented; endpoint behavior should continue to be verified live** |
| CSA startup/chat state | `https://chatsvcagg.teams.microsoft.com` | Skype token | **Not yet tested live in this Python connector** |
| Substrate people/chat search | `https://substrate.office.com` | none | **Not needed yet** |
| SharePoint/OneDrive attachments | tenant SharePoint/OneDrive audience | none | **Not implemented** |
| AMS media upload | AMS/IC3 audience | service-specific headers | **Not implemented** |
| Some AMS image/transcript downloads | none | Skype token in `Authorization: skype_token ...` | **Not implemented** |

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

The Python connector should therefore avoid one universal request helper that blindly sends the same credentials to every Teams service.

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

The Python connector currently uses `msal.SerializableTokenCache`.

Typical flow:

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

For the exporter, the two highest-priority resources are:

```text
api.spaces.skype.com
    -> AuthZ
    -> Middle Tier
    -> obtain Skype token

chatsvcagg.teams.microsoft.com
    -> CSA startup/chat synchronization
```

Public implementations have historically requested the CSA token using the **same Teams first-party public client ID**:

```text
1fec8e78-bce4-4aaf-ab1b-5451cc387264
```

A useful current experiment is therefore:

```python
csa_result = app.acquire_token_silent(
    scopes=["https://chatsvcagg.teams.microsoft.com/.default"],
    account=account,
)
```

If no cached grant exists yet, test the same scope through the existing device-code flow.

**Status:** public/historical evidence; not yet tested live in this Python project.

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

The working Python implementation sends an empty body.

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

For chat work, the most important values are:

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

That makes CSA the strongest current candidate for reproducing the Teams sidebar ordering/state.

**Important:** array order has not yet been verified against the current enterprise Teams UI.

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

## Why CSA matters

The current Python connector has struggled with conversation ordering because it fetches raw Chat Service conversations and applies local sorting heuristics.

CSA may eliminate that entire problem.

The next high-value experiment is:

```text
1. Acquire bearer token for:
   https://chatsvcagg.teams.microsoft.com/.default

2. Reuse the existing Skype token.

3. Call:
   GET https://teams.microsoft.com/api/csa/api/v1/teams/users/me
       ?isPrefetch=false
       &enableMembershipSummary=true

4. Print chats[] exactly in returned order.

5. Compare the first 20 entries directly with the Teams sidebar.

6. If they match, test /teams/users/me/updates using metadata.syncToken.
```

Do not add more local conversation-sorting rules until this has been tested.

---

# Regional Chat Service

The Regional Chat Service is the most useful backend for **raw conversation/message access**.

It should not currently be assumed to be the canonical source for the exact Teams sidebar/chat-picker ordering.

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

The Python connector currently uses this service for:
- conversation discovery/fallback listing,
- message retrieval,
- future member lookups.

---

## Conversation catalog sources: raw feed vs UI state

There are now three distinct concepts that should not be conflated.

### 1. Raw Chat Service conversation feed

```text
GET {chatService}/v1/users/ME/conversations
```

Useful for:
- discovering conversation IDs,
- obtaining raw metadata,
- pagination experiments,
- fallback operation in a pure Python client.

It is **not yet proven to reproduce the visible Teams sidebar ordering**.

### 2. CSA startup/chat state

```text
/api/csa/api/v1/teams/users/me
```

This is the strongest current candidate for a pure-Python conversation catalog because public captures show it returning richer UI-oriented state:

```text
isRead
isSticky
hidden
consumptionHorizon
lastMessage
metadata.syncToken
```

Its exact ordering still needs current live verification.

### 3. Teams Web IndexedDB conversation state

A current open-source Teams Web exporter reports that modern Teams Web maintains conversation state in IndexedDB stores such as:

```text
Teams:conversation-manager:react-web-client:<tenant>:<user>:<locale>
Teams:conversation-folder-manager:...
```

That implementation stopped using `/v1/users/ME/conversations` as the picker/list source because IndexedDB matched the browser UI more faithfully, including meeting-derived and niche conversation types.

This is important **architectural evidence**, but not necessarily something the Python impersonator should reproduce directly.

For this project, the likely preference order is:

```text
CSA, if live testing shows it matches the UI well
        |
        v
raw Chat Service conversation listing as fallback
```

Do not keep adding local sorting heuristics merely to emulate browser sidebar order until CSA has been tested.

---

## GET `{chatService}/v1/users/ME/conversations`

List raw service conversations.

**Evidence:** observed live + reverse-engineered implementation.

This endpoint is useful and currently implemented, but should be treated as a **low-level/fallback catalog source** rather than proof of current Teams sidebar order.

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
startTime=0
view=msnp24Equivalent
```

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

Not every parameter should be assumed mandatory; the current minimal implementation only needs to add those proven necessary by live testing.

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

These timestamps may be useful for date-bounded export because a client could potentially determine that an entire next segment lies before the requested cutoff without inspecting every message first.

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

This maps naturally to the intended exporter API:

```python
connector.get_messages(
    conversation_id,
    limit=500,
    since=datetime(...),
)
```

A `conversation_id` should ultimately be sufficient to retrieve messages.

If a loaded conversation object already contains a `messages` URL, the connector may reuse it. That URL should be treated as a convenience/optimization, not a requirement that callers first browse the conversation list.

Retrieval should walk backward only until one of these conditions is reached:

```text
message count >= limit
OR
next page/message is older than since
```

Either condition may be unlimited.

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

**Evidence:** Teams Web bundle + live/reverse-engineering work.

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

### Likely result

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

---

## POST `{middleTier}/beta/users/fetchShortProfile`

Shorter profile lookup.

**Evidence:** Teams Web route family + reverse-engineered implementation.

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

---

# Other Middle Tier routes found in Teams Web

The production bundle contains route templates in the following families.

Not all method/parameter/response contracts have been verified.

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

---

## People/search/profile helpers

```text
beta/users/searchExternalUsers
beta/users/searchV2
beta/workingSet/people
beta/scd/searchability
beta/users/{mri}/statementUrl
```

Contracts are not yet fully established.

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
        "QueryString": "Juniper",
        "DisplayQueryString": "Juniper"
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
        "QueryString": "analytics"
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

This may later be useful for a UI search box.

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

The exact request/response contracts remain unverified.

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
    or
    "html" in str(message.get("contenttype") or message.get("contentType") or "").lower()
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

# Python connector design

The public abstraction should remain deliberately small.

Conceptually:

```python
connector = TeamsConnector()
connector.connect()

user = connector.get_user_metadata()

chats = connector.list_chats(limit=20)

messages = connector.get_messages(
    conversation_id,
    limit=500,
    since=None,
)
```

Callers should not need to know about:

```text
MSAL
refresh tokens
Teams client ID
OAuth audiences
AuthZ
Skype tokens
regionGtms
Chat Service
Middle Tier
CSA
syncState
backwardLink
```

Those details belong inside `TeamsConnector`.

The connector does **not** need to be split into many classes yet.

A small set of service-specific internal request helpers would be enough:

```python
_request_chat_service(...)
_request_middle_tier(...)
_request_csa(...)
```

This avoids incorrectly sending one credential combination to every endpoint while keeping the public API simple.

---

# Current implementation status

The current Python code already implements a substantial part of the exporter path.

| Capability | Current state |
|---|---|
| Teams first-party MSAL authentication | implemented / working |
| Persistent `SerializableTokenCache` | implemented / working |
| Silent auth + device-code fallback | implemented / working |
| `api.spaces.skype.com` access token | implemented / working |
| `/authz` exchange | implemented / working |
| Skype-token extraction | implemented / working |
| `regionGtms` service discovery | implemented / working |
| Raw Chat Service conversation pagination | implemented |
| Local chat filtering | implemented, but should be considered fallback/provisional |
| Local conversation ordering by thread `version` | implemented, but not authoritative sidebar semantics |
| Middle Tier profile lookup | implemented |
| Message-history pagination | implemented |
| Message count limit | implemented |
| `since` date cutoff | implemented, but stop semantics should be hardened |
| Chronological message output | implemented |
| Plain-text export | implemented |
| Basic Teams HTML-to-text conversion | implemented |
| CSA bearer acquisition | not implemented |
| CSA startup catalog | not implemented |
| CSA delta synchronization | not implemented |
| Substrate search | not implemented |
| Attachments/images/transcripts | not implemented |
| Small UI | not implemented |

## Known current implementation caveats

### Universal request helper

The current connector sends both:

```text
Authorization: Bearer <api.spaces token>
Authentication: skypetoken=<Skype token>
```

from a general-purpose request method.

That is convenient for experiments but should be replaced with service-specific credential handling before CSA/Substrate/attachments are added.

### Message endpoint dependency

The current `get_messages()` expects the selected conversation to already be present in the connector's in-memory conversation snapshot so it can reuse the returned `messages` URL.

Long term, this should not be required.

Given:

```text
conversation_id
regionGtms.chatService
```

the connector can construct the messages endpoint itself.

### HTML detection

The current text exporter should detect `RichText/Html` from `messagetype` as well as `contenttype`.

### Debug `main.py`

`main.py` is intentionally a debugging/verification tool.

It may:
- print raw metadata,
- use private helper methods,
- expose thread versions,
- compare ordering behavior.

It does not need to be treated as the final application interface.

---

# Desired conversation API semantics

Two useful low-level hooks remain:

```python
connector.list_conversations(limit=15)
```

and:

```python
connector.get_conversations(start=80, end=120)
```

These should represent the **raw Chat Service feed**.

Desired behavior:

- ranges are inclusive,
- indexing is 1-based,
- results are stable within the lifetime of a connector instance,
- pagination fetches only what is necessary,
- server-provided continuation links are followed rather than reconstructed.

A higher-level API such as:

```python
connector.list_chats(limit=20)
```

should eventually represent the user-facing chat catalog.

Its preferred source should be:

```text
CSA, if current live testing validates it
```

with raw Chat Service listing retained as a fallback.

Local sorting by guessed timestamps or thread versions should not be treated as authoritative Teams UI behavior.

---

# Desired message retrieval semantics

The intended API is:

```python
connector.get_messages(
    conversation_id,
    limit=500,
    since=datetime(...),
)
```

A `conversation_id` should be enough; callers should not be required to preload the conversation catalog first.

Both filters are optional.

### Count only

```python
limit=500
since=None
```

Return the latest 500 messages regardless of age.

### Date only

```python
limit=None
since=datetime(2026, 1, 1, tzinfo=...)
```

Return all available messages from that date forward.

### Count + date

```python
limit=500
since=datetime(2026, 1, 1, tzinfo=...)
```

Stop when either:

```text
500 messages have been retrieved
OR
history is definitively older than the date cutoff
```

### Unlimited

```python
limit=None
since=None
```

Walk backward until Teams stops returning older history.

### Timezone policy

The public connector API should prefer or require **timezone-aware** datetimes.

Avoid silently interpreting:

```python
datetime(2026, 9, 1)
```

according to whichever local timezone happens to run the process.

The UI may accept a local date and explicitly convert its boundary to a timezone-aware value before calling the connector.

### Efficiency

Do not download all history and filter afterward if pagination allows stopping early.

Internally the service naturally works newest -> oldest.

For export, messages should ultimately be returned oldest -> newest.

### Date-cutoff safety

The current implementation stops paging once it encounters messages older than `since`.

That is efficient but relies on the assumption that page/history ordering is monotonic with the chosen message timestamp.

This has not yet been proven for every Teams message/system-event type.

Public captures show page metadata such as:

```text
lastCompleteSegmentStartTime
lastCompleteSegmentEndTime
```

These may offer a more reliable way to determine that an entire continuation segment lies before the requested cutoff.

Until verified live, date-bound stopping should be conservative.

Potential approaches:

```text
1. use verified segment boundaries, if present;
2. otherwise stop only when the whole page establishes that older continuations cannot satisfy the cutoff;
3. or fetch one additional page after first crossing the boundary.
```

The goal is to avoid silently dropping valid messages because of an ordering assumption.

---

# Testing strategy

At this stage, fixture-driven tests will provide more value than additional abstraction.

Save **sanitized real API responses** such as:

```text
tests/
└── fixtures/
    ├── authz.json
    ├── conversations_page_1.json
    ├── conversations_page_2.json
    ├── messages_page_1.json
    ├── messages_page_2.json
    ├── profiles.json
    └── csa_startup.json          # once captured
```

Useful tests include:

```text
conversation pagination
1-based conversation slicing
duplicate conversation IDs
repeated backwardLink detection
no-progress page detection
mixed camelCase/lowercase Teams fields
profile resolution fallback
message pagination
duplicate message IDs
message limit cutoff
date cutoff
chronological final ordering
HTML detection via messagetype
HTML-to-text conversion
empty/system messages
CSA startup parsing
CSA delta sync-token progression
```

A small injectable HTTP opener/client is sufficient for testing.

There is no need to introduce a large HTTP/service/repository abstraction solely for tests.

---

# UI direction

The final application should remain small.

A possible local UI:

```text
[x] Person A
[ ] Meeting B
[x] Group C

Message limit:
[ 500 ]   [ ] Unlimited

Oldest date:
[ 2026-01-01 ]   [ ] Unlimited

[ Download selected conversations ]
```

The UI should only understand:

```text
conversations
selected conversation IDs
message limit
date cutoff
export
```

It should not contain Teams authentication or pagination logic.

Possible architecture:

```text
tiny local UI
      |
      v
TeamsConnector
      |
      +--> Auth / AuthZ
      +--> CSA
      +--> Chat Service
      +--> Middle Tier
      +--> attachments later
```

---

# Current priorities

## 1. Separate service-specific authentication/request handling

Before adding more services, stop treating all Teams HTTP calls as though they use the same credentials.

Keep the public class simple, but add internal request paths for:

```text
Chat Service
Middle Tier
CSA
```

Expected credential model:

```text
Chat Service:
    Skype token

Middle Tier:
    api.spaces.skype.com bearer

CSA:
    chatsvcagg.teams.microsoft.com bearer
    + Skype token
```

---

## 2. Test CSA authentication and catalog ordering

Acquire a token for:

```text
https://chatsvcagg.teams.microsoft.com/.default
```

using the existing Teams public client ID and MSAL cache.

Then call:

```text
GET https://teams.microsoft.com/api/csa/api/v1/teams/users/me
    ?isPrefetch=false
    &enableMembershipSummary=true
```

with:

```text
Authorization: Bearer <CSA token>
x-skypetoken: <Skype token>
```

Compare `chats[]` directly against the visible Teams sidebar **without local sorting**.

If it matches well enough, use CSA as the user-facing conversation catalog and keep `/users/ME/conversations` as a raw/fallback source.

---

## 3. Test CSA delta synchronization

Take:

```text
metadata.syncToken
```

from startup and test:

```text
/teams/users/me/updates
```

with:

```text
x-ms-synctoken
```

Determine whether a local catalog can be maintained incrementally.

---

## 4. Harden message retrieval

`get_messages()` already exists.

Next verification work:

```text
- allow conversation_id retrieval without preloading the catalog
- verify exact page ordering
- verify pageSize behavior
- inspect segment timestamp metadata
- harden date-cutoff stopping
- require/normalize timezone-aware since values
```

A useful live request is:

```text
GET {chatService}/v1/users/ME/conversations/{id}/messages
    ?startTime=0
    &pageSize=10
    &view=msnp24Equivalent
```

Verify:

- exact response order,
- whether `pageSize=10` is honored exactly,
- `_metadata.backwardLink`,
- `_metadata.syncState`,
- `lastCompleteSegmentStartTime`,
- `lastCompleteSegmentEndTime`,
- behavior on subsequent pages.

---

## 5. Fix/export message rendering details

Before building the UI:

```text
- detect HTML from messagetype as well as contenttype
- preserve useful hyperlinks where practical
- decide representation of non-text/system messages
```

Initial export formats can remain:

```text
.txt
.json
```

At minimum preserve:

```text
timestamp
author
message content
```

Later, optionally:

```text
message IDs
edits
deletions
reactions
mentions
replies
attachments
images
```

---

## 6. Add fixture-based tests

Lock down the already-working behavior before adding the UI.

---

## 7. Build the small UI

Only after:

```text
catalog source is trustworthy enough
message retrieval is stable
export behavior is tested
```

The UI should remain a thin layer over `TeamsConnector`.

---

# Public research follow-up — 2026-09-29

Additional public research produced several useful corroborating findings.

## CSA resource identity

Public Entra service-principal inventories identify:

```text
Microsoft Teams Chat Aggregator

App ID:
b1379a75-ce5e-4fa3-80c6-89bb39bf646c

Service principal/resource:
https://chatsvcagg.teams.microsoft.com
```

This explains why CSA requires a separate OAuth audience rather than accepting the `api.spaces.skype.com` bearer token.

## Public implementations/captures consulted

Examples used as corroborating evidence include:

```text
AADInternals / Teams PowerShell implementation
https://www.powershellgallery.com/packages/AADInternals/

purple-teams
https://github.com/EionRobb/purple-teams

Microsoft Tech Community Teams developer discussions
https://techcommunity.microsoft.com/

Microsoft Learn / Q&A discussions
https://learn.microsoft.com/answers/

SkPy Teams/Skype protocol discussion
https://github.com/Terrance/SkPy/discussions/

Maxim-Mazurok/teams-api
https://github.com/Maxim-Mazurok/teams-api
```

These are not treated as authoritative Microsoft API contracts.

Older code/captures are most valuable when they independently match:
- routes present in the current Teams Web bundle,
- current AuthZ resource names,
- or behavior observed live in this project.

## Current Teams Web IndexedDB evidence

A current open-source Teams Web exporter documents that the web client keeps its own conversation-manager state in IndexedDB using stores in the family of:

```text
Teams:conversation-manager:react-web-client:<tenant>:<userUuid>:<locale>
Teams:conversation-folder-manager:...
```

That project reports using IndexedDB rather than:

```text
/v1/users/ME/conversations
```

for its chat picker because the local Teams state better matches the browser UI and includes conversation types that the raw Chat Service feed may omit.

This does **not** mean the Python impersonator should depend on browser IndexedDB.

It does strengthen the conclusion that:

```text
raw Chat Service conversation feed != authoritative Teams Web sidebar state
```

and increases the value of investigating CSA as the pure-Python catalog source.

Public reference:

```text
https://github.com/gediz/teams-web-chat-exporter
```

## Still unresolved after public research

The following remain genuinely open:

```text
authzcore exact request/response contract

whether current CSA chats[] order exactly equals current sidebar order

which CSA headers are mandatory today versus historical client artifacts

whether CSA exposes all conversation types needed by the intended UI

current enterprise behavior of:
    startupgroupchats
    startupnonrecentchats

exact page-size limits/caps for Chat Service conversations/messages

whether message pages are strictly monotonic by original-arrival timestamp
for every relevant message/system-event type

whether lastCompleteSegmentStartTime / lastCompleteSegmentEndTime can be
relied upon for early date-cutoff termination
```

These should be resolved through current Teams Web network captures rather than further guesswork.

---

# Source notes

Primary sources used while compiling these notes:

1. A production Teams Web JavaScript build collected on **2026-09-28** and inspected for route/resource constants and client behavior.
2. Live experiments from the Python connector, including successful:
   - Teams desktop-client OAuth
   - local MSAL token persistence
   - `api.spaces.skype.com` token acquisition
   - Teams `/authz`
   - Skype-token acquisition
   - regional service discovery
   - conversation listing
3. Source code from the independent reverse-engineered project:
   - https://github.com/Maxim-Mazurok/teams-api
4. Current Teams Web exporter architecture notes showing browser-side conversation state in IndexedDB:
   - https://github.com/gediz/teams-web-chat-exporter
5. The current Python implementation in this repository, used to distinguish already-working behavior from planned/researched behavior.

Third-party projects are useful corroboration and implementation references, but are not treated as authoritative Microsoft API contracts.

When possible, verify uncertain behavior against current Teams Web network traffic.
