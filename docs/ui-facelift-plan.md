# UI facelift implementation plan

Date: 2026-10-16. Status: implemented on `codex/ui-facelift`; reviewed and corrected 2026-10-16.

This plan turns the agreed layout and interactions for the local Teams export UI into incremental
work. Contributors should be able to implement it from this document alone. The reference sketch
contains private data and stays outside the repository.

## 1. Intended result

Replace the centered, width-limited card with a panel that fills the viewport. Remove the
"Choose conversations and filters. Message attachments are included when available." subtitle.
Keep the existing typography, Teams purple, avatars and light/dark palette as the starting point.

Use four rows, with equal-width left and right columns aligned across rows two through four:

| Row | Left | Right |
|---|---|---|
| Header | Teams connector eyebrow above Download conversations | Signed-in account, avatar and presence at the far right |
| Controls | Conversation filter, growing to fill the space before a naturally sized Refresh chats button | Equal-width date range and message limit fields, then a fixed-size settings (cog) button |
| Content | Independently scrolling conversation list | Independently scrolling plain-text preview |
| Footer | Selected conversation pills | Status, export cancellation when applicable, and Download |

The header and footer stay visible while the panes scroll. Use grid/flex sizing with
`min-width: 0` and `min-height: 0` so long content cannot stretch the panel. The preview wraps
whitespace and long links and scrolls vertically only. Bound the pill area's height and let it
scroll, so a large selection cannot push the panes out of view.

At narrow widths, stack the controls and panes in a usable reading order, keeping both the list
and the preview reachable and the footer actions visible. Check small windows and browser zoom
rather than forcing two unusably narrow columns.

## 2. Selection and preview target

Keep two separate pieces of state:

- `selectedConversationIds`: the conversations the download includes, in selection order.
- `previewConversationId`: the conversation shown in the preview, or none.

Start with nothing selected, no pills and an empty preview prompt. Download is disabled until at
least one conversation is selected.

Clicking a row (or Enter or Space on it) only makes it the preview target, so browsing previews never
changes the download (changed 2026-10-16; rows first toggled the selection too). A small round button at
the row's right adds or removes the conversation: it shows on hover and keyboard focus, always on touch
screens, and stays visible as a filled check while the conversation is selected. It does not change the
preview. Ctrl+click (Cmd+click on macOS) on a row previews it and toggles its selection. Opening the
chat already previewed keeps its view and scroll.

Each selected conversation has one pill with its name and an accessible remove button. Removing a
pill deselects the conversation only: the preview, its contents and the list scroll position stay as
they are, and no other conversation is selected. Removing the last pill disables Download.

Selected rows use the current hover background; the previewed row uses a slightly stronger one,
selected or not, and is marked `aria-current`. Keep a distinct keyboard focus indicator.

Filtering the list keeps selections, pills and the preview, even for chats the filter hides. A
catalog refresh drops conversations that no longer exist; if the previewed one is gone, the
preview is cleared with a short explanation.

Render conversations in the order the backend returns them (latest activity first, unknown activity
last; `Store.chats()` in `src/teams_connector/store/db.py`), and filter without sorting again.
Presence, selection and preview changes never reorder rows; a catalog refresh may.

## 3. Controls and settings menu

The cog opens a menu with three on/off switches: AI meeting summaries (which also governs the
Facilitator's Loop meeting notes), meeting transcripts and message attachments. They start at
their current defaults. Each switch's thumb slides between ends, with a distinct track for each
state; clicking the label also toggles it. Use switch semantics with an exposed checked state and
keyboard operation, and respect reduced motion.

A switch change applies immediately and leaves the menu open. An outside click or Escape closes it.

Every setting (date range, message limit and all three switches) applies to both the download and
the preview, so any change reloads the active preview. Debounce typed numbers and use the date
picker's committed range, so intermediate edits start no requests. Clearing a control also
reloads. A setting change returns the preview to its default (capped) view; showing the entire
range needs another click. Nothing is fetched until a conversation is opened.

The conversation filter stays a local filter. Refresh chats re-reads the catalog and, if a preview
is open, re-reads it as well, keeping its settings and view. With no preview open, it refreshes
only the catalog. Neither selects a chat or changes the selection, except to drop missing chats.

## 4. Preview

### One pipeline, text only

The preview is the conversation's export text, produced by the same export pipeline and honoring
every setting: date range, message limit, AI summaries (with the Facilitator's notes), transcripts
and attachments. Transcript markers, meeting notes, summaries and attachment names therefore read
exactly as in the download.

The only difference is that a preview fetches no binary files: no attachments and no transcript
files. File markers name the files as the download would. No disclaimer is shown for that.

### Endpoint

Add a dedicated preview job route, separate from the export job routes. `POST /api/preview` takes
one conversation, the same settings as an export, and `entire_range` (default `false`), then returns
a job id. `GET /api/preview/{job_id}` returns status, progress, errors, the result, and its UTC
completion time; the terminal result is released from the server slot when read. `POST
/api/preview/{job_id}/cancel` cancels a running preview. This job shape supports cancellation and
progress when viewing the entire range.
The result includes the text and coverage:

- `entire_range=false`: the backend returns a capped excerpt, the latest part of the selected range,
  in the formatter's usual chronological order. The backend owns the cap; start with the latest 200
  messages and a 1 MiB text budget as named constants, and tune them there. A smaller user message
  limit wins. Bounds apply while reading, not after building unlimited output.
- `entire_range=true`: everything the user's date range and message limit select, still without
  files. With no filters, that is the conversation's whole history.

The response says whether the text was capped (or completeness is unknown). The UI shows the
**View entire range** button only then, as a sticky button at the top of the preview pane that does
not cover text. A preview never counts as a download, and its caps never reach a download request.

### Backend scheduling

The backend runs previews in a single slot of their own, separate from export jobs, so opening a
chat never occupies the export slot or blocks Download. A newer preview request cancels the one in
progress, and a download that starts cancels a running preview. The UI polls job status while work
is active and releases the result after receiving it.

Preview work leaves no files behind: temporary spools are removed on completion, cancellation or
error, and nothing is published to the export folder.

### Loading and rendering

Show one spinner while a preview is prepared: in the pane while it is empty, in the toolbar while
earlier text stays visible. A loaded preview opens scrolled to its end, the newest messages. Clicking
the previewed row again only toggles its selection; the preview, its view and scroll stay. For
`entire_range`, show progress and a cancel action,
keep the excerpt visible meanwhile, and on failure or cancellation keep the excerpt and offer retry.
Never label partial text as complete.

Render large results in chunks (for example per block of messages, using `content-visibility: auto`)
so long ranges stay responsive, with wrapped text and vertical scrolling only.

Track a request generation in the page so a slow response never overwrites a newer chat or newer
settings. Render all names and message content as text. Show distinct empty, loading, ready,
capped and failed states; a failure offers retry without touching the selection.

### Caching (decided 2026-10-16)

Completed previews are kept in browser memory for the current page only: 60 seconds of freshness
from completion, at most 10 entries, least recently used evicted first. A cache hit does not extend
freshness, and there is no size accounting. Entries are keyed by account, conversation and every
setting (date bounds with their UTC offsets, message limit, the three switches) plus
`entire_range`, since every setting changes the text. An excerpt is never served as an entire range.

Switching back to a fresh entry shows it immediately; an expired one needs a new read. Clear the
cache on Refresh chats, account change and authentication failure; a page reload starts empty.
Never use localStorage, IndexedDB or the catalog database for message text.

This is an approved exception to the "message content is not kept" requirement, limited to the
open page's memory; the requirements are updated with the implementation.

## 5. Presence and row metadata

Add Teams-style presence badges to the signed-in avatar and to one-to-one chats with people. Map the
availability and activity the service returns to familiar indicators, with accessible text so
colour is not the only signal. Unknown presence must not look like a confirmed offline status.

Move the conversation kind and last-message time into a tooltip, also shown on keyboard focus. Replace
the one-to-one subtitle with `Last seen: <localized last-active time> · <device>`, using presence's
last-active field, never the last message time. Show each part only when Teams reports it, with the
separator only between two parts. As Teams Web's persona card does, show the time only for someone away,
be right back, out of office or offline: Teams omits it for available people and sends one for busy
people that Teams does not show (probed 2026-10-16). Teams also omits the device for offline people, so
a row may show just the device, or no subtitle. Groups, channels, meetings and bot chats (`28:` peers)
get no badge and no person subtitle. The self chat may use the signed-in user's presence.

Reuse `service/presence.py` and `remote/presence.py`. The own-presence route uses the active account
id, and the peer route accepts only a catalog conversation id; the page cannot request an arbitrary
person. Both share the service's one-minute cache. Presence loads separately from the catalog and
preview, for the signed-in user and visible one-to-one rows only (an IntersectionObserver), with four
concurrent row requests. There is no presence polling. Failures leave the list usable and show
presence as unavailable.

Own-presence reads are unverified: cover them with tests, and verify live only if separately
authorized, recording dated findings in the research docs.

## 6. Dynamic system theme

The UI reads `prefers-color-scheme` once at startup. Also listen for changes to that media query
and apply the theme immediately, without a reload and without resetting selection, preview, scroll
positions or an open menu. Check selected and previewed rows, badges, tooltips, the menu, spinner,
wrapped text and footer in both themes.

## 7. Implementation steps

Branch from the current `main`. Use these commit-sized steps, updating tests and documentation in
each:

1. **Layout and theme.** Four-row grid, control sizing, settings menu with switches, footer, narrow
   layout; remove the subtitle; add the theme-change listener.
2. **Selection and pills.** Separate selection from the preview target; row clicks preview and
   the add button selects; highlights, pill removal, keyboard access, filter persistence.
3. **Preview endpoint.** A text-only export mode in the export pipeline (no binary files), the
   `POST /api/preview` route with `entire_range`, backend-owned caps with coverage, the separate
   preview slot with cancellation, and spool cleanup.
4. **Preview in the page.** Wire selection and settings to the endpoint; loading, capped and failed
   states; the View entire range button; chunked rendering; request generations; the memory cache.
5. **Presence and metadata.** Own and peer presence routes, visible-row loading, badges, last-seen
   subtitles and tooltips.
6. **Review and fixes.** Review the whole diff and workflow, remove duplication, finish docs.

Run all four repository checks (`uv run ruff check .`, `uv run ruff format --check .`,
`uv run pyright`, `uv run pytest`) before every commit, and fix failures without weakening them.
Add focused tests and browser checks where the Python suite cannot reach the page.

The UI must also count preview and presence requests as activity for its idle shutdown.

## 8. Verification

Use synthetic conversations, identities and messages with fake transports only.

- Empty start; a row click previews without selecting; the add button toggles once without
  previewing; preview highlight; pill removal keeps the preview;
  hidden selections survive filtering; missing chats are dropped on refresh.
- Row order follows the backend and does not change with presence, selection or preview.
- Switches slide, work by label and keyboard, expose their state and respect reduced motion; the
  menu stays open while they change.
- The preview text matches the download text for the same settings (summaries, notes, transcript
  markers, attachment names), and the preview fetches no binary files.
- Capped previews report it and show View entire range; `entire_range` honors date range and message
  limit; long messages, long links and empty chats render correctly.
- A newer preview cancels an older one; a download cancels a running preview; a slow response never
  overwrites newer state; previews leave no files behind.
- Cache: a fresh entry is reused; it expires 60 seconds after completion even when hit; the
  eleventh entry evicts the least recently used; any setting change misses; Refresh chats, account
  change and authentication failure clear it.
- Downloads keep their own status, cancellation and TXT/ZIP packaging while previews run.
- Presence: own and peer, missing values, unknown statuses, bot and group chats, failures; only
  visible rows are read.
- Full viewport, narrow windows, zoom, keyboard focus, overflowing pills, live theme switching.
- Route validation, session-token and Host guards, account ownership, text-only rendering, no
  message content in logs.

Update the README, architecture and requirements for the preview route, its caps and the text-only
mode; update the roadmap; record new live findings in the research docs only with dates. Link this
plan from the documentation index.

## 9. Pull request

Push the branch, open a PR against `main`, and describe the behavior, the preview/download relation,
presence limitations and the verification done. Address review findings with incremental commits,
rerunning the checks before each. Leave merging to the user.
