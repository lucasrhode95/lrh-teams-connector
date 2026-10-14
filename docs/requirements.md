# Teams Connector — Requirements

**Evidence:** [Teams API research](api-research.md) · **How it is built:** [architecture](architecture.md) · **Open work:** [roadmap](roadmap.md)

This document records what the connector must do and the decisions behind it. How each tool behaves
is in the [architecture's MCP tool reference](architecture.md#mcp-tools); open work is in the
roadmap. Do not resolve an open roadmap item by assumption.

## 1. Product

A local application for **one user's Microsoft Teams chats and team channels**. It has two surfaces
over one engine:

- **MCP server:** agents find conversations, read history, search message text, discover messages
  online, export conversations, and send a message the user explicitly authorized.
- **Local UI:** the user exports conversations as text or ZIP bundles without an AI client.

The connector must:

- find one-on-one, group, meeting, self and channel conversations with stable ids, useful names,
  correct types and honest catalog coverage;
- read history with reliable paging and inclusive date and count filters;
- search message text in a chosen scope, and discover messages online;
- keep every message it fetches in an account-bound archive, filled by reads, searches, exports and
  explicit syncs, never by a background collector;
- send a text message the user explicitly authorized.

Presence, Favorites, sender pictures, Markdown sends and a conversation viewer are optional extras,
tracked in the roadmap. Outlook, Teams and SharePoint are separately installed connectors that share
one sign-in (`lrh-m365-auth`); each keeps its own product logic.

## 2. Ownership (2026-10-11)

Outlook owns Exchange calendar events: schedule, agenda, organizer, invitees, event attachments and
the join link. Teams owns a Teams meeting's chat, recordings, transcripts, recap and meeting notes.
Only verified identifiers connect the two. General OneDrive and SharePoint file discovery belongs to
the SharePoint connector, and email to the Outlook connector. Which product hosts a shared search
endpoint does not change who owns a resource.

## 3. Non-goals

- Scheduling, summarizing or delivering digests. Agents build these from the MCP tools.
- A background sync process, watcher or collector.
- Edit history. An edit overwrites the stored text.
- Teams writes other than sending a message: no edits, deletes, reactions or read-state changes.
- Resuming an export after a process restart, or other export resilience machinery, unless a real
  failure calls for it.
- One combined text file for several conversations. Each conversation is its own artifact.
- An Outlook client. Mail and calendar belong to the
  [Outlook connector](https://github.com/lucasrhode95/lrh-outlook-connector).

## 4. Backend choice

- **Prefer Microsoft Graph wherever a live check shows it is at least as complete as the private
  Teams service.** Keep a private service only where Graph is unavailable, not granted to the
  client, or materially less complete.
- **One backend per capability.** Do not keep two implementations for compatibility. A migration is
  finished when representative output matches the old path (ids, ordering, metadata, paging,
  formatting-relevant fields and errors), regression tests cover it, the private path is removed and
  reconstructable local state is reset.
- **A failed or ambiguous send is never retried**, through the same backend or another one.
- **Grants are per tenant.** The current split rests on one account's grants; reassess it before
  choosing a backend elsewhere.

The current split ([research](api-research.md#microsoft-graph-with-the-teams-client-2026-10-01)):

| Capability | Backend | Why |
|---|---|---|
| Chat catalog (one-on-one, group, meeting, self) | CSA, private | Graph cannot list chats: the Teams client has no `Chat.*` read grant |
| Chat history and rosters | Chat Service, private | Same |
| Channels, channel history and members | Graph | Lists every joined channel; the private catalog adds only activity |
| People's names and photos | Graph | |
| Sending | Graph | Returns the server message id |
| Downloading files linked from messages | Graph shares | |
| Online message discovery | Search v2 (`m365.search`) | Graph `chatMessage` search is denied to the tested client |

Teams-native services stay where they are richer than Graph: presence, Favorites and read state,
broad heterogeneous search, and AMS media.

## 5. Catalog

- Every conversation has one normalized kind (`one_on_one`, `group`, `meeting`, `self`, `channel`),
  used by MCP results and by the UI's icon and label. Explicit meeting semantics win over the generic
  `isOneOnOne` flag. Id prefixes are only a guarded fallback.
- Untitled chats get a name: a one-on-one takes the other person's name, or a verified app's name;
  an unnamed group takes its members' sorted names, cut at 87 characters with ` +N`. An opaque id is
  shown only when no name resolves.
- The exact rules and the evidence behind them are in the research's
  [implementation decisions](api-research.md#implementation-decisions).
- The catalog answers from the local store for up to ten minutes; an explicit refresh waits for
  Teams.

## 6. Archive and retention

Every message the connector fetches is kept in the account-bound SQLite archive until the user
removes the archive.

- **Edits** overwrite the stored text and keep `edited_at`. An older or unversioned record never
  replaces a known edit.
- **Soft deletes** keep the last known text and mark the row deleted with `deleted_at`. A later record
  without the body never erases known text. A message first seen already deleted is stored without
  invented text. A stale record does not restore a deleted message; a later edit does.
- **Exports** label deleted messages so retained text cannot pass for a live message.
- **Search** returns deleted messages with `is_deleted` and `deleted_at`.

## 7. Search

Four layers solve different problems and stay separate:

| Layer | Surface | Behavior |
|---|---|---|
| Conversation filter | Local UI | Filters the loaded catalog in the browser. Typing never calls Teams. |
| `search_chats` | MCP | Matches conversation names and indexed participant names. Refreshes the catalog by default; `refresh=false` searches the cache only. Reports roster coverage instead of syncing every roster. |
| `search_messages` | MCP | The caller chooses chats and dates. The connector fetches that scope into the archive and matches locally, with explicit coverage. |
| `search_remote_messages` | MCP | Online discovery of Teams messages through Search v2, one bounded page at a time. Returns pointers, not archived messages. |

Calendar events, meeting recaps, transcript lists, transcript content and transcript-text search are
separate capabilities. Report each one as verified, blocked or unverified; message search alone never
counts as broad Microsoft 365 search.

## 8. History, exports and sending

- **History selection** is one rule for `get_messages`, MCP exports and UI exports: `since` and
  `until` are inclusive, dates filter first, then `message_limit` keeps the latest N matching
  messages. Page size and response size are presentation only.
- **Exports** produce one artifact per conversation: the plain TXT/JSONL file, or a ZIP with a folder
  holding the text and its attachments. Several conversations are wrapped in one outer ZIP.
  Attachments are included by default, up to 200 references per conversation, 100 MiB per file and
  250 MiB per conversation. A failed download keeps the text and marks the attachment unavailable.
- **Sending** needs the user's explicit authorization for both the recipient and the exact content.
  The current conversation name must match `expected_chat_name`. Text is escaped, sent once, and the
  server message id is returned.
