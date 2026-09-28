# Scrape Teams Chat

Small browser-console helper for exporting text from a Microsoft Teams chat.

## Where to run it

Run `scrape_teams_chat.js` in the Microsoft Teams V2 web UI, not in Node.js.

Use Teams in a browser, for example:

- `https://teams.microsoft.com/v2/`
- `https://teams.cloud.microsoft/`

The script depends on Teams' web page DOM, so it will not work from the desktop app unless you can open and run JavaScript in that app's embedded web view.

## How to use

1. Open Teams V2 in your browser.
2. Open the chat you want to scrape.
3. Click directly inside a visible chat message bubble.
   - Do not click the left chat list, the chat header, or the compose/input box.
   - Selecting or focusing a message helps the script find the correct scroll container.
4. Open the browser developer console.
5. Paste the contents of `scrape_teams_chat.js` into the console and run it.
6. When the scrape finishes, use the on-screen panel to copy or download the exported text.

By default, the script runs:

```js
scrapeTeamsByID();
```

That attempts to scrape as much history as Teams will lazy-load. To limit the export, run:

```js
scrapeTeamsByID(50);
```

To finish a running extraction and export the messages collected so far, enter this in the same browser console:

```js
stop();
```

The export panel appears after the current collection pass. `stop()` also interrupts the script's waits, including the initial move to the newest messages. If you stop before any readable messages are collected, there is nothing to export. A second scrape cannot start until the first export panel is closed or used.

Console progress shows `added` since the previous printed line and `total` collected so far. The script prints every tenth pass while scrolling, so `added` includes messages collected in the intervening passes.

## Tips and caveats

- Start from the actual target chat before running the script. It scrapes the currently open chat.
- Click message text or a message bubble first. If the script says it cannot find the chat scroll container, click a visible message and run it again.
- Avoid the compose box. If the input area has focus, the script may identify the wrong part of the page.
- Leave the Teams tab active while it runs. Teams lazy-loads older messages as the script scrolls upward.
- Large chats can take a while because Teams only exposes part of the chat history at a time. At the top of the loaded history, the script allows up to 20 consecutive attempts for older messages to appear. The counter resets whenever Teams shows progress, and `stop()` lets you export early.
- The export is text-focused. Images, reactions, cards, files, and other rich content may be skipped or represented incompletely.
- If clipboard access is blocked by the browser, use the Download button or copy from the manual text box shown by the script.
- Teams DOM attributes can change. If Microsoft updates the Teams V2 UI, the selectors in the script may need adjustment.
