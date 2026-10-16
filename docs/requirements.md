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
- search message text across Teams or in a chosen scope, on the server;
- treat Teams as the record: keep no local copy of message content (§6);
- send a text message the user explicitly authorized.

Presence (one-on-one, read only) is part of the reads. Outlook, Teams and
SharePoint are separately installed connectors that share one sign-in (`lrh-m365-auth`); each keeps
its own product logic.

## 2. Ownership (2026-10-11)

Outlook owns Exchange calendar events: schedule, agenda, organizer, invitees, event attachments and
the join link. Teams owns a Teams meeting's chat, recordings, transcripts, recap and meeting notes.
Only verified identifiers connect the two. General OneDrive and SharePoint file discovery belongs to
the SharePoint connector, and email to the Outlook connector. Which product hosts a shared search
endpoint does not change who owns a resource.

## 3. Non-goals

- Scheduling, summarizing or delivering digests. Agents build these from the MCP tools.
- A background sync process, watcher or collector.
- Keeping messages Teams no longer returns: deleted text, earlier versions of edited messages, or
  messages past the organization's retention policy (decided 2026-10-14).
- Matching inside words. Teams search matches words, word prefixes and phrases only.
- Teams writes other than sending a message and deleting the user's own chat messages: no edits,
  reactions or read-state changes, and nothing written as someone else.
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

The current split ([research](api-research.md#microsoft-graph-with-the-teams-client)):

| Capability | Backend | Why |
|---|---|---|
| Chat catalog (one-on-one, group, meeting, self) | CSA, private | Graph cannot list chats: the Teams client has no `Chat.*` read grant |
| Chat history and chat members | Chat Service, private | Same |
| Channels, channel history and members | Graph | Lists every joined channel; the private catalog adds only activity |
| People's names and photos | Graph | |
| Sending | Graph | Returns the server message id |
| Downloading files linked from messages | Graph shares | |
| Downloading pasted images | AMS, private (full-size views); Graph hosted copies for channel images | AMS holds them and the Skype token reads them ([research](api-research.md#ams-async-media-service)); a channel's Graph copy is read with the Graph token |
| Online message discovery | Search v2 (`m365.search`) | Graph `chatMessage` search is denied to the tested client |
| Presence of one-on-one peers | Teams Unified Presence, private, at the route AuthZ names (`presenceUPS`) | Graph presence needs `Presence.Read`, which the client lacks; the AuthZ route answers the `teams.spaces` bearer ([research](api-research.md#unified-presence)) |

Teams-native services stay where they are richer than Graph: presence and read state,
broad heterogeneous search, and AMS media.

## 5. Catalog

- Every conversation has one normalized kind (`one_on_one`, `group`, `meeting`, `self`, `channel`),
  used by MCP results and by the UI's icon and label. Explicit meeting semantics win over the generic
  `isOneOnOne` flag. Id prefixes are only a guarded fallback.
- Untitled chats get a name: a one-on-one takes the other person's name, or a verified app's name;
  an unnamed group takes its members' sorted names, cut at 87 characters with ` +N`. An opaque id is
  shown only when no name resolves.
- The exact rules and the evidence behind them are in the research's
  [naming rules](api-research.md#chat-rows).
- The catalog answers from the local store for up to ten minutes; an explicit refresh waits for
  Teams.

## 6. Local data (decided 2026-10-14)

**Teams is the record.** The connector keeps no local copy of message content. Teams itself keeps
no readable copy of deleted text or of an edited message's earlier version
([research](api-research.md#edits-and-deletes)), so the connector shows what Teams returns now: an
edited message with its newest text and `edited_at`, and a deleted message marked deleted with
`deleted_at` and no text.

A small local cache of the chat list stays, holding no message text: the catalog sync token, the
names worked out for untitled chats, the channels Graph's listing omits, and photo ids. It holds no
chat member lists; participant matches come from Teams' chat search. A damaged store is moved aside,
not deleted, and the catalog is rebuilt from Teams
([architecture](architecture.md#requests-caching-and-storage)). Downloaded attachments are kept per source for a week, so a repeat download is served from disk; they are files Teams shares, not message text.

## 7. Search

| Layer | Surface | Behavior |
|---|---|---|
| Conversation filter | Local UI | Filters the loaded catalog in the browser. Typing never calls Teams. |
| Conversation search | MCP (`search_chats`) | Matches conversation names in the catalog and, with a refresh (the default), the named group and meeting chats Teams matches by a plain name or email, with the members that matched, and the one-on-ones with the people Teams suggests for it ([research](api-research.md#people-suggestions)). `refresh=false` searches the cache only. Reports what it could not cover; older named chats matched only by a member are a known gap. |
| Message search | MCP (`search_messages`) | Search v2 on the server, across Teams or scoped to chosen conversations and dates. Takes a list of terms, any of which matches; each term is one phrase, never split and never read as a query operator. Matches words, word prefixes and phrases; returns message ids, totals, pages, and a confirmed zero. No local match modes (decided 2026-10-15).

Calendar events, meeting recaps, transcript lists, transcript content and transcript-text search are
separate capabilities. Report each one as verified, blocked or unverified; message search alone never
counts as broad Microsoft 365 search.

## 8. History, exports, sending and deleting

- **History selection** is one rule for `get_messages`, MCP exports and UI exports: `since` and
  `until` are inclusive, dates filter first, then `message_limit` keeps the latest N matching
  messages. A channel's history is threads (a post with its replies) by latest activity, each thread
  in time order, so for a channel the latest N are those of the most recently active threads
  (decided 2026-10-15). A page holds whole messages: `page_size` and `max_chars` decide how many, and no message
  text is cut to fit a response (decided 2026-10-15).
- **Exports** produce one download (decided 2026-10-16, as the Outlook connector does): a single
  conversation with no attachment saved is its plain TXT/JSONL file; anything else is one ZIP with each
  conversation's file at its root and its attachments in a folder named after that file, never a ZIP
  inside a ZIP. File names carry the conversation's name and part of its id, with a counter if two
  still clash.
  Attachments are included by default, up to 200 references per conversation, 100 MiB per file and
  250 MiB per conversation. A failed download keeps the text and marks the attachment unavailable.
  TXT is for reading and keeps a lean header; JSONL is for code and keeps every field.
- **Attachments by name** (decided and implemented 2026-10-15): one text extraction serves
  `get_messages`, exports and downloads. Files and pasted images appear in the text as markers with
  names derived from the message; `download_attachment` fetches the named files of one message, at
  full resolution, to local paths, and fetches each source once; a file it cannot fetch is reported
  while the others are saved. SharePoint pages and meeting recordings are links, not downloads. Base64
  image data never reaches a response, and nothing is downloaded while reading.
- **Meeting notes** (decided and implemented 2026-10-15): the meeting app's link to a Loop notes page reads
  as a `[Meeting notes]` marker with the link. Exports and `download_attachment` read the live page once
  per call and keep only its agenda and goal, notes, and tasks with assignee and due date, saying when it
  was read. A page that cannot be read is reported like an unavailable attachment. **Export restrictions
  are ignored by design** (decided 2026-10-16): a page the user can read in Teams can also be read,
  screenshotted or retyped, so the connector does not check a page's export or extract permissions. A
  page Graph itself refuses to convert is still reported as unavailable.
- **Meeting and membership events** read as one short line each, never as their raw markup. The
  line keeps what Teams shows (a meeting's subject, duration and participants, members who joined or
  left, a new topic) and drops identifiers and flags.
- **Reading never changes a message or chat** (decided 2026-10-15). Reading history, searching,
  exporting and downloading use only reads; nothing marks a message or chat as read or seen, so the
  user's unread state in Teams stays as it was. Checked 2026-10-15 on unread meeting chats: after history reads, a download and an export, they were still unread.
- **Sending** needs the user's explicit authorization for the recipient and the exact content, files
  and images. The current conversation name must match `expected_chat_name`. Text is escaped, HTML
  (`html`, decided 2026-10-15) is sent as written after the passive check, and the message is sent once;
  the server message id is returned. Verified live for the self chat, one-on-one and group chats
  (2026-10-14); channels and meeting chats are roadmap S3.
- **Images and files** (decided and implemented 2026-10-15) go to one-on-one and group chats and the
  self chat; channels and meeting chats are roadmap P1.5. Images and files may share a message.
  - Images are PNG or JPEG only, sent inline in the same request; `<img src="file name">` in `html`
    places one, and the others follow the text. A file that is not PNG or JPEG, or images that would
    push the request past about 4 MiB (about 3 MB of images), are refused before anything is sent, with
    a suggestion to send them as files. Any other `<img>` is refused.
  - Files, like Teams, go to the sender's OneDrive, are shared with the chat's other members at the
    tenant-default access without an email, and are referenced by the message, which is posted only
    after every upload and share has succeeded. Files uploaded before a failure are reported, never
    deleted. A file may be up to `config.MAX_SEND_FILE_BYTES` (2 GiB by default); from 250 MiB it goes up
    in parts.
  - **Exception to making each write once, approved by the user:** when a part of a large upload fails,
    the connector asks which ranges are still missing and resends only those, a few times at most; then
    it cancels the upload and fails the send.
- **Deleting the user's own messages** (decided and implemented 2026-10-15) is the second write path.
  `delete_messages` soft-deletes a set of the user's own messages in one chat (not a channel), as Teams'
  Delete does. The agent lists the set (chat, count, first and last message) and gets one explicit yes
  for it; the connector cannot verify that, so it is a rule in the tool description and the MCP
  instructions. Anyone else's message is skipped. Each delete is attempted once, and the result reports
  each message as deleted, skipped or uncertain. Files a deleted message shared are reported, not
  removed or unshared.
