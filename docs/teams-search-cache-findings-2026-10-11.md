# Teams search and local cache findings (2026-10-11)

This note records code-path and live behavior measurements for the current Teams connector. Live counts and timings describe one connected account and are not service-wide guarantees. No message bodies, identifiers, chat names, or query strings are retained here.

## Current search path

`search_messages` does not use a verified server-side content-search route. It fetches the requested chat/date scope from Teams, archives fetched messages in the local SQLite store, then applies exact, substring, or all-terms matching locally. Search coverage reports `server_side_search_used=false`.

The page budget applies to each call. When preparation is incomplete, the returned cursor resumes that same search's preparation at its saved chat/page position; after preparation is complete, cursor paging applies to local match results. A fresh search does not reuse a completed preparation cursor.

## Live measurements

- Repeated the same exact query over one chat and a one-day range twice. Each independent search fetched one remote page; each completed in about one second. This demonstrates that fresh searches repeat the requested remote read even when the local store already holds the page.
- Ran an all-catalog search with a five-page budget over a one-day range. It requested 966 chat IDs, fetched five pages in 8.7 seconds, remained incomplete, and returned a continuation cursor. No matches were returned for the synthetic unique query. This is a bounded sample, not a full-history crawl or a benchmark of every catalog chat.
- The index status reported 966 catalog rows and 14,723 indexed messages, with no complete message histories. A refreshed chat listing reported 734 chats and 232 empty chats excluded. The 966-row search scope therefore includes entries excluded from the visible non-empty chat list; whether all empty entries need to be visited is an optimization to validate.

These measurements show that one global-search call does not download the whole database, but a fresh global search can repeat remote page reads across its requested chat/date scope over multiple cursor calls. The cursor avoids restarting that preparation only when the caller continues the same search.

## Cache-removal implications

Moving message matching to a verified remote Teams search route is the preferred direction. Removing the persisted message-history index is plausible after verifying that the route covers chat message text, required meeting recaps/transcripts, paging, scope, and result hydration. A remote search hit must still be hydrated for the requested excerpt or full message.

Removing the entire local store has additional dependencies: participant/chat search currently uses locally indexed rosters, and catalog/index coverage state is persisted. Those features need a verified remote replacement or should be intentionally removed. Outlook is a useful precedent for avoiding a durable content cache while retaining minimal local application state; it does not imply that Teams can remove every local store before its feature dependencies are mapped.

## Remote-search candidate found

Microsoft documents `POST /v1.0/search/query` with `entityTypes: ["chatMessage"]` for server-side Teams message search. It searches message bodies and supported message attachments in the signed-in user's Teams messages. The documented delegated permission is `Chat.Read`; the Search API entity table also lists `ChannelMessage.Read.All` for channel messages. A live connector request with the required permission has not yet been measured.

This route can replace per-chat page preparation for message text. Search hits provide a summary plus chat and message identifiers, but not all `chatMessage` properties, so hydrate only returned hits. The documented API doesn't support custom sorting for chat messages, and the reported `total` is only the current page size. It doesn't combine `chatMessage` with other entity types, so M365-wide search must issue separate searches and merge the results.

Transcripts remain a separate route: Graph can list and retrieve transcript content per online meeting with `OnlineMeetingTranscript.Read.All`, subject to tenant settings, but no Graph full-text transcript search was identified. Copilot Meeting AI Insights exposes structured meeting notes and action items with separate permission and licensing prerequisites; it is not a search endpoint.

Next, validate the Graph search permission and compare remote hit recall, KQL/date behavior, paging, hydration cost, and repeated-query latency against the current connector. Separately map Teams web requests for recap/transcript content and check whether transcript files are discoverable through OneDrive/SharePoint search. Do not remove the message-history cache until those paths and the remaining local roster/catalog dependencies are covered.

Sources reviewed on 2026-10-11:

- https://learn.microsoft.com/en-us/graph/search-concept-chat-messages
- https://learn.microsoft.com/en-us/graph/api/resources/search-api-overview?view=graph-rest-1.0
- https://learn.microsoft.com/en-us/graph/api/onlinemeeting-list-transcripts?view=graph-rest-1.0
- https://learn.microsoft.com/en-us/microsoftteams/platform/graph-api/meeting-transcripts/meeting-insights
