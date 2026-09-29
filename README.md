# Teams private API endpoint notes

Endpoint shapes observed in the [Teams web app](https://teams.cloud.microsoft/) JavaScript and the independent [teams-api](https://github.com/Maxim-Mazurok/teams-api) implementation. These are private interfaces, not Microsoft Graph contracts. Parameters and response fields below are observed examples, not exhaustive schemas.

`{chatService}` and `{middleTier}` denote regional base URLs returned by authentication. The teams-api implementation builds the Chat Service base as `https://{region}.ng.msg.teams.microsoft.com`. Follow server-provided pagination URLs as opaque links.

## Authentication

- **`POST https://teams.microsoft.com/api/authsvc/v1.0/authz`** — exchange a Microsoft access token for Teams service credentials.
  - Alternate URL: `https://authsvc.teams.microsoft.com/v1.0/authz`.
  - Headers: `Authorization: Bearer <access token>`; JSON `Accept` header.
  - Body: empty in the observed request.
  - Returns:
    - `tokens.skypeToken`: token for Chat Service requests.
    - `regionGtms.chatService` and `regionGtms.middleTier`: regional service URLs.
    - Other region and authentication metadata.
  - Source: Teams web app JavaScript authentication bundle.

- **`https://teams.microsoft.com/api/authsvc/v1.0/authzcore`** — alternate authentication route selected when AuthZ v2 is enabled.
  - Alternate URL: `https://authsvc.teams.microsoft.com/v1.0/authzcore`.
  - Method, parameters, and returns: not established from the inspected route constants.
  - Source: Teams web app JavaScript authentication bundle.

## Conversations and messages

- **`GET {chatService}/v1/users/ME/conversations`** — list conversations.
  - Query parameters:
    - `pageSize`: requested page size.
    - `view`: observed values `mychats` and `msnp24Equivalent`.
    - Pagination: follow `_metadata.backwardLink`; it may contain opaque continuation parameters.
  - Returns:
    - `conversations[]`: metadata such as `id`, `version`, `threadProperties`, `properties`, `lastMessage`, and sometimes a `messages` URL.
    - `_metadata.backwardLink`: URL for an older page, when available.
  - Authentication: `Authentication: skypetoken=<token>` in teams-api.
  - Source: [teams-api Chat Service source](https://github.com/Maxim-Mazurok/teams-api/blob/main/src/api/chat-service.ts) for `mychats`; `msnp24Equivalent` from earlier Chat Service request inspection. The exact route was not recovered from the saved web JavaScript.

- **`GET {chatService}/v1/users/ME/conversations/{conversationId}/messages`** — read message history.
  - Path parameter: URL-encoded `conversationId`.
  - Query parameters:
    - `pageSize`: requested page size.
    - Pagination: follow `_metadata.backwardLink` for older messages.
  - Returns:
    - `messages[]`: fields observed include `id`, `sequenceId`, `originalarrivaltime`, `composetime`, `content`, `contenttype`, and sender information. `content` can be HTML.
    - `_metadata.backwardLink`: URL for an older page, when available.
    - `_metadata.syncState`: optional synchronization state.
  - Authentication: `Authentication: skypetoken=<token>` in teams-api.
  - Source: [teams-api Chat Service source](https://github.com/Maxim-Mazurok/teams-api/blob/main/src/api/chat-service.ts). The exact route was not recovered from the saved web JavaScript.

- **`GET {chatService}/v1/threads/{conversationId}/members`** — list conversation members.
  - Path parameter: URL-encoded `conversationId`.
  - Returns: `members[]` with `id`, optional `userDisplayName`, and `role`.
  - Authentication: `Authentication: skypetoken=<token>` in teams-api.
  - Source: [teams-api Chat Service source](https://github.com/Maxim-Mazurok/teams-api/blob/main/src/api/chat-service.ts).

- **`GET {chatService}/v1/users/ME/properties`** — read the signed-in user's properties.
  - Parameters: none shown in teams-api.
  - Returns: a JSON properties object; a stable field list is not established here.
  - Authentication: `Authentication: skypetoken=<token>` in teams-api.
  - Source: [teams-api Chat Service source](https://github.com/Maxim-Mazurok/teams-api/blob/main/src/api/chat-service.ts).

## Profiles and discovery

- **`POST {middleTier}/beta/users/fetch`** — resolve user identifiers to profiles.
  - Query parameters observed: `isMailAddress`, `canBeSmtpAddress`, `enableGuest`, `includeIBBarredUsers`, `skypeTeamsInfo`, and `includeDisabledAccounts`.
  - Body: JSON array of MRI identifiers, for example `["8:orgid:<user-id>"]`.
  - Returns: `value[]` or `Value[]`, with fields such as `objectId`, `mri`, `displayName`, and `userPrincipalName`.
  - Authentication: Microsoft bearer token for the Teams service in the observed request.
  - Source: Teams web app JavaScript `beta/users/fetch` route and earlier request inspection.

- **`POST https://teams.cloud.microsoft/api/mt/{region}/beta/users/fetchShortProfile`** — fetch short member profiles.
  - Path parameter: `region`.
  - Query parameters observed: `isMailAddress=false`, `enableGuest=true`, `skypeTeamsInfo=true`.
  - Body: JSON array of MRI identifiers.
  - Returns: `value[]` with `mri`, `displayName`, `email`, `jobTitle`, `userLocation`, and `userType` when available.
  - Authentication: bearer token for `api.spaces.skype.com` in teams-api.
  - Source: [teams-api middle-tier source](https://github.com/Maxim-Mazurok/teams-api/blob/main/src/api/middle-tier.ts).

- **`POST https://substrate.office.com/search/api/v1/suggestions?scenario=peoplepicker.newChat`** — discover people or chats.
  - Query parameter: `scenario=peoplepicker.newChat`.
  - Body:
    - `EntityRequests[0].Query.QueryString`: search text.
    - `EntityRequests[0].EntityType`: `People` or `Chat`.
    - `EntityRequests[0].Size`: requested maximum results.
    - `Scenario.Name=peoplepicker.newChat` and `AppName=Microsoft Teams`.
    - The people request also sets `DisplayQueryString`, `Fields`, `Filter`, `Provenances`, and `From`.
  - Returns:
    - `Groups[]` with `Type=People` or `Type=Chat` and `Suggestions[]`.
    - People suggestions may contain `DisplayName`, `MRI`, email addresses, job title, and department.
    - Chat suggestions may contain `Name`, `ThreadId`, `ThreadType`, matching members, chat members, and member count.
  - Authentication: bearer token for `substrate.office.com` in teams-api.
  - Scope: people/chat discovery; full-text search of message bodies is not established by this request.
  - Source: [teams-api Substrate source](https://github.com/Maxim-Mazurok/teams-api/blob/main/src/api/substrate.ts).

## Search route candidates in Teams web JavaScript

The following route templates were found in the [Teams web app](https://teams.cloud.microsoft/) JavaScript. Their full request and response contracts remain unverified.

- **`search/api/v2/query` and `searchservice/api/v2/query`** — search query routes.
  - Known URL parameters: none in the inspected route literals.
  - Method and body: not established; client constants mention JSON and flight/language headers.
  - Returns: search results, with exact fields and pagination unverified.
  - Authentication: search constants mention the `https://outlook.office.com/` token resource; a Chat Service token alone is not confirmed sufficient.
  - Source: Teams web app JavaScript search bundles.

- **`search/api/v1/init` and `search/api/v2/init`** — initialize a search scenario.
  - Query parameters observed: `cvid` (conversation ID) and `scenario` (for example, `powerbar`).
  - Method and returns: not established.
  - Source: Teams web app JavaScript search bundles.

- **`search/api/v1/suggestions`** — search suggestions.
  - Query parameters observed across route templates: `scenario`, `query`, `cvid`, and `logicalId`.
  - Method and returns: not established. This route template should not be assumed identical to the Substrate request above.
  - Source: Teams web app JavaScript search bundles.

- **`beta/users/searchV2`** — middle-tier people search route.
  - Parameters, method, and returns: not established from the inspected route literal.
  - Source: Teams web app JavaScript search bundles.
