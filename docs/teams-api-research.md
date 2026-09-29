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

The startup response also exposes synchronization metadata, but the live behavior of the delta route is not established here. See the route templates above and the confidence notes below.

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

### 3. Teams Web IndexedDB conversation state

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

The route identifies the conversation directly in its path. A previously fetched conversation object may also expose a `messages` URL; captured implementations use the conversation ID to construct the route when that link is unavailable. The service supplies continuation URLs for older pages; count and date stopping conditions are client-side choices, not parameters defined by this route.

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

A live message also contained a semantic mention element in its HTML, marked with:

```text
itemtype="http://schema.skype.com/Mention"
```

The element wrapped the mentioned display name, while `properties.mentions` identified the mention and its `displayName`. A formatter can therefore render a delimited marker such as `@(Rhode, Lucas)` based on message markup/metadata rather than treating every matching name in plain text as a mention.

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

## Delta synchronization and open uncertainties

The CSA response exposes `metadata.syncToken` and public captures show an `x-ms-synctoken` request header for `teams/users/me/updates`. The delta route and its token progression were not verified in the live session described above.

The following details remain unverified or may vary by tenant/build:

- whether `metadata.isPartialData` or `metadata.hasMoreChats` can occur in ordinary full startup responses, and how all additional chat groups are requested;
- whether CSA’s timestamp-based ordering remains aligned with the Teams UI for ties, missing timestamps, hidden chats, and every chat type;
- whether `isSticky` maps to Favorites or another Teams concept;
- whether message pages are strictly monotonic by timestamp for all message and system-event types;
- the precise category of a raw JSON `members` event when the inspected record does not include its enclosing message type;
- whether `lastCompleteSegmentStartTime` and `lastCompleteSegmentEndTime` can safely terminate date-bounded history retrieval;
- exact page-size limits and whether all continuation links retain the original requested page size;
- which CSA headers beyond the resource bearer token and Skype token are mandatory across tenants.

# Source notes

Primary sources used for these notes:

1. A production Teams Web JavaScript build and HTML entry document collected on **2026-09-28**, inspected for route templates, resource names, headers, and client behavior.
2. Read-only live requests made with an explicitly authenticated Teams session on **2026-09-29**, including CSA startup, Middle Tier profile lookup, and Regional Chat Service message history.
3. The independent reverse-engineered project [Maxim-Mazurok/teams-api](https://github.com/Maxim-Mazurok/teams-api), used as corroborating implementation evidence.
4. Teams Web IndexedDB architecture notes from [gediz/teams-web-chat-exporter](https://github.com/gediz/teams-web-chat-exporter).
5. Public references including AADInternals, purple-teams, Microsoft Learn/Q&A, Microsoft Tech Community, and SkPy discussions.
6. [Microsoft Graph system messages for Teams](https://learn.microsoft.com/en-us/graph/system-messages), for the documented event-message model and distinctions among membership and meeting-policy events.
7. [Microsoft Learn Azure Communication Services chat message examples](https://learn.microsoft.com/en-us/rest/api/communication/chat/list-chat-messages/list-chat-messages?view=rest-communication-chat-2020-09-21-preview), for corroborating Skype-style `addmember` and `topicupdate` payload envelopes.

Third-party projects and captures are useful corroboration, not authoritative Microsoft API contracts. Routes and payload details can change without notice.
