/**
 * Scrape Teams Chat (ID-Based)
 *
 * Paste this into the Teams web console after opening the target chat.
 * Click inside a visible message first, then run scrapeTeamsByID(50) or scrapeTeamsByID().
 */
async function scrapeTeamsByID(limit = 0) {
    if (globalThis.__teamsScrapeRun) {
        console.warn("A Teams chat scrape is already running. Use stop() to finish it first.");
        return { ok: false, message: "A Teams chat scrape is already running." };
    }

    const run = { stopped: false, wake: null, phase: "scraping", previousStop: globalThis.stop, stopHandler: stopTeamsChatScrape };
    globalThis.__teamsScrapeRun = run;
    globalThis.stop = run.stopHandler;
    console.clear();

    try {
        return await runTeamsChatScrape(limit, run);
    } catch (error) {
        reportFailure(error);
        return {
            ok: false,
            message: error?.message || String(error),
            details: error?.details || {}
        };
    } finally {
        if (globalThis.__teamsScrapeRun === run) {
            delete globalThis.__teamsScrapeRun;
        }
        if (globalThis.stop === run.stopHandler) {
            globalThis.stop = run.previousStop;
        }
    }
}

function stopTeamsChatScrape() {
    const run = globalThis.__teamsScrapeRun;
    if (!run || run.phase !== "scraping") {
        console.info("No Teams chat extraction is running.");
        return false;
    }

    run.stopped = true;
    run.wake?.();
    console.info("Stopping Teams chat extraction; collected messages will be available to export shortly.");
    return true;
}

class TeamsScrapeError extends Error {
    constructor(message, details = {}) {
        super(message);
        this.name = "TeamsScrapeError";
        this.details = details;
    }
}

async function runTeamsChatScrape(limit, run) {
    const normalizedLimit = normalizeLimit(limit);
    console.log(`STARTED: Scraping chat... (Limit: ${normalizedLimit === 0 ? "None" : normalizedLimit})`);

    // --- 1. FIND CONTAINER ---
    const startup = getStartupDiagnostics();
    console.info("Startup diagnostics:", startup);

    const { chatContainer, source, startNode } = findChatContainer();
    if (!chatContainer) {
        throw new TeamsScrapeError(
            "Could not find the Teams chat scroll container.",
            {
                likelyFix: "Click directly inside a visible chat message bubble, then run scrapeTeamsByID(...) again.",
                startNode: describeNode(startNode),
                visibleMessageBlocksInDocument: startup.visibleMessageBlocks,
                visibleContentBlocksInDocument: startup.visibleContentBlocks,
                activeElement: describeNode(document.activeElement),
                selectionText: startup.selectionText
            }
        );
    }

    console.info(`Using scroll container found from ${source}:`, {
        container: describeNode(chatContainer),
        scrollTop: chatContainer.scrollTop,
        scrollHeight: chatContainer.scrollHeight,
        clientHeight: chatContainer.clientHeight
    });

    const initialMessagesInContainer = chatContainer.querySelectorAll('[data-tid="chat-pane-message"]').length;
    if (initialMessagesInContainer === 0) {
        throw new TeamsScrapeError(
            "Found a scrollable container, but it does not contain any Teams message blocks.",
            {
                likelyFix: "The selected element is probably not inside the message pane. Click message text in the open chat, not the chat list, header, or compose box.",
                container: describeNode(chatContainer),
                visibleMessageBlocksInDocument: startup.visibleMessageBlocks
            }
        );
    }

    const previousBorder = chatContainer.style.border;
    chatContainer.style.border = "4px solid #0078D4";

    try {
        await scrollToNewestMessages(chatContainer, run);
        const result = await scrapeVisibleAndHistoricalMessages(chatContainer, normalizedLimit, run);
        const finalText = buildFinalText(result.collectedMessages);

        if (!finalText) {
            if (run.stopped) {
                console.info("Stopped before any readable messages were collected.");
                return { ok: true, count: 0, delivery: "nothing to export", stoppedBecause: "stopped by user", text: "" };
            }
            throw new TeamsScrapeError(
                "Scraping finished, but no readable message text was collected.",
                {
                    collectedMessageCount: result.collectedMessages.size,
                    visibleMessageBlocksInContainer: initialMessagesInContainer,
                    likelyFix: "Check whether the visible messages are images/cards only, or whether Teams changed the content/author/timestamp element IDs."
                }
            );
        }

        run.phase = "delivery";
        const delivery = await copyOrDownload(finalText, result.collectedMessages.size);
        console.info("DONE:", {
            collectedMessages: result.collectedMessages.size,
            delivery,
            stoppedBecause: result.stopReason
        });

        return {
            ok: true,
            count: result.collectedMessages.size,
            delivery,
            stoppedBecause: result.stopReason,
            text: finalText
        };
    } finally {
        chatContainer.style.border = previousBorder;
    }
}

function normalizeLimit(limit) {
    const numericLimit = Number(limit);
    if (!Number.isFinite(numericLimit) || numericLimit < 0) {
        throw new TeamsScrapeError(
            "Invalid limit supplied to scrapeTeamsByID.",
            {
                suppliedLimit: limit,
                expected: "Use 0 for no limit, or a positive number like scrapeTeamsByID(50)."
            }
        );
    }

    return Math.floor(numericLimit);
}

function getStartupDiagnostics() {
    const selection = document.getSelection();

    return {
        url: location.href,
        title: document.title,
        visibleMessageBlocks: document.querySelectorAll('[data-tid="chat-pane-message"]').length,
        visibleContentBlocks: document.querySelectorAll('[id^="content-"]').length,
        activeElement: describeNode(document.activeElement),
        selectionAnchorNode: describeNode(selection?.anchorNode),
        selectionText: (selection?.toString() || "").slice(0, 200)
    };
}

function findChatContainer() {
    const selection = document.getSelection();
    const startNode = selection?.anchorNode || document.activeElement;

    let chatContainer = getScrollParent(startNode);
    if (chatContainer?.querySelector('[data-tid="chat-pane-message"]')) {
        return { chatContainer, source: "selection/active element", startNode };
    }

    const selectedMessage = toElement(startNode)?.closest?.('[data-tid="chat-pane-message"]');
    if (selectedMessage) {
        chatContainer = getScrollParent(selectedMessage);
        if (chatContainer?.querySelector('[data-tid="chat-pane-message"]')) {
            return { chatContainer, source: "selected message ancestor", startNode };
        }
    }

    const firstVisibleMessage = document.querySelector('[data-tid="chat-pane-message"]');
    if (firstVisibleMessage) {
        chatContainer = getScrollParent(firstVisibleMessage);
        if (chatContainer?.querySelector('[data-tid="chat-pane-message"]')) {
            return { chatContainer, source: "first visible message fallback", startNode };
        }
    }

    return { chatContainer: null, source: "not found", startNode };
}

function getScrollParent(node) {
    let current = toElement(node);

    while (current) {
        const style = window.getComputedStyle(current);
        const canScrollY = style.overflowY === "auto" || style.overflowY === "scroll";

        if (canScrollY && current.scrollHeight > current.clientHeight) {
            return current;
        }

        current = current.parentElement;
    }

    return null;
}

function toElement(node) {
    if (!node) return null;
    if (node.nodeType === Node.ELEMENT_NODE) return node;
    return node.parentElement || null;
}

async function scrapeVisibleAndHistoricalMessages(chatContainer, limit, run) {
    const collectedMessages = new Map();
    let consecutiveNoLoad = 0;
    let stopReason = "unknown";
    let pass = 0;
    let lastLoggedTotal = 0;

    const MAX_RETRIES = 20;
    const LOAD_WAIT_MS = 900;
    const STEP_WAIT_MS = 200;
    const WIGGLE_WAIT_MS = 200;

    while (true) {
        pass++;

        if (limit > 0 && collectedMessages.size >= limit) {
            stopReason = `limit reached (${limit})`;
            break;
        }

        const beforeCount = collectedMessages.size;
        const visibleMessages = collectVisibleMessages(chatContainer, collectedMessages, limit);
        const addedCount = collectedMessages.size - beforeCount;
        const atTop = isNearTop(chatContainer);

        if (pass === 1 || pass % 10 === 0 || atTop) {
            const addedSinceLastLog = collectedMessages.size - lastLoggedTotal;
            console.log(
                `Pass ${pass}: visible=${visibleMessages}, added=${addedSinceLastLog} since last log, total=${collectedMessages.size}, scrollTop=${Math.round(chatContainer.scrollTop)}, scrollHeight=${chatContainer.scrollHeight}, atTop=${atTop}`
            );
            lastLoggedTotal = collectedMessages.size;
        }

        if (run.stopped) {
            stopReason = "stopped by user";
            break;
        }

        if (limit > 0 && collectedMessages.size >= limit) {
            stopReason = `limit reached (${limit})`;
            break;
        }

        if (visibleMessages === 0) {
            throw new TeamsScrapeError(
                "The scroll container stopped exposing Teams message blocks.",
                {
                    pass,
                    collectedMessages: collectedMessages.size,
                    container: describeNode(chatContainer),
                    likelyFix: "Teams may have re-rendered while scraping. Click a visible message again and rerun the scraper."
                }
            );
        }

        if (!atTop) {
            consecutiveNoLoad = 0;
            scrollUpOnePage(chatContainer);
            await sleep(STEP_WAIT_MS, run);
            continue;
        }

        const prevScrollHeight = chatContainer.scrollHeight;
        const prevScrollTop = chatContainer.scrollTop;
        const prevOldestMessageId = getOldestMessageId(chatContainer);
        chatContainer.scrollTop = 0;
        await sleep(LOAD_WAIT_MS, run);
        if (run.stopped) {
            stopReason = "stopped by user";
            break;
        }

        const scrollHeightDelta = Math.abs(chatContainer.scrollHeight - prevScrollHeight);
        const scrollTopDelta = Math.abs(chatContainer.scrollTop - prevScrollTop);
        const oldestMessageChanged = getOldestMessageId(chatContainer) !== prevOldestMessageId;

        if (scrollHeightDelta < 10 && scrollTopDelta < 10 && !oldestMessageChanged && addedCount === 0) {
            consecutiveNoLoad++;
            console.info(`No older messages detected (${consecutiveNoLoad}/${MAX_RETRIES}). Trying a small scroll wiggle.`);

            chatContainer.scrollTop = 50;
            await sleep(WIGGLE_WAIT_MS, run);
            if (run.stopped) {
                stopReason = "stopped by user";
                break;
            }
            chatContainer.scrollTop = 0;
            await sleep(WIGGLE_WAIT_MS, run);
            if (run.stopped) {
                stopReason = "stopped by user";
                break;
            }

            // Teams can finish loading during the wiggle, including on the
            // final retry. Check again before deciding history has ended.
            if (Math.abs(chatContainer.scrollHeight - prevScrollHeight) >= 10 ||
                Math.abs(chatContainer.scrollTop - prevScrollTop) >= 10 ||
                getOldestMessageId(chatContainer) !== prevOldestMessageId) {
                consecutiveNoLoad = 0;
                continue;
            }

            if (consecutiveNoLoad >= MAX_RETRIES) {
                stopReason = `no older messages loaded after ${MAX_RETRIES} retries`;
                break;
            }
        } else {
            consecutiveNoLoad = 0;
        }
    }

    return { collectedMessages, stopReason };
}

function getOldestMessageId(chatContainer) {
    return chatContainer.querySelector('[data-tid="chat-pane-message"]')?.getAttribute("data-mid") || null;
}

async function scrollToNewestMessages(chatContainer, run) {
    const MAX_WAIT_MS = 30000;
    const MIN_SETTLE_MS = 1200;
    const POLL_WAIT_MS = 200;
    const REQUIRED_STABLE_PASSES = 3;

    const startedAt = Date.now();
    let stablePasses = 0;
    let previousState = null;
    let currentState = getBottomState(chatContainer);

    while (Date.now() - startedAt < MAX_WAIT_MS) {
        if (run.stopped) return;
        chatContainer.scrollTop = chatContainer.scrollHeight;
        await sleep(POLL_WAIT_MS, run);
        if (run.stopped) return;

        currentState = getBottomState(chatContainer);
        const isStable = previousState &&
            Math.abs(currentState.scrollTop - previousState.scrollTop) < 5 &&
            Math.abs(currentState.scrollHeight - previousState.scrollHeight) < 5 &&
            currentState.newestMessageId === previousState.newestMessageId;

        if (isStable && currentState.atBottom) {
            stablePasses++;
        } else {
            stablePasses = 0;
        }

        previousState = currentState;

        if (
            Date.now() - startedAt >= MIN_SETTLE_MS &&
            stablePasses >= REQUIRED_STABLE_PASSES
        ) {
            console.info("Starting scrape from newest loaded messages:", currentState);
            return;
        }
    }

    currentState = getBottomState(chatContainer);
    if (!currentState.atBottom) {
        throw new TeamsScrapeError(
            "Teams did not reach the newest messages before the startup timeout.",
            {
                waitedMs: Date.now() - startedAt,
                ...currentState,
                likelyFix: "Wait for the chat to finish loading, click a recent message, and run the scraper again."
            }
        );
    }

    console.warn("The newest-message pane did not fully stabilize before the startup timeout; continuing from the bottom-most loaded message.", currentState);
}

function getBottomState(chatContainer) {
    const messageBlocks = chatContainer.querySelectorAll('[data-tid="chat-pane-message"]');
    const newestBlock = messageBlocks[messageBlocks.length - 1];

    return {
        scrollTop: Math.round(chatContainer.scrollTop),
        scrollHeight: chatContainer.scrollHeight,
        clientHeight: chatContainer.clientHeight,
        atBottom: isNearBottom(chatContainer),
        visibleMessages: messageBlocks.length,
        newestMessageId: newestBlock?.getAttribute("data-mid") || null
    };
}

function scrollUpOnePage(chatContainer) {
    const step = Math.max(Math.floor(chatContainer.clientHeight * 0.75), 350);
    chatContainer.scrollTop = Math.max(0, chatContainer.scrollTop - step);
}

function isNearTop(chatContainer) {
    return chatContainer.scrollTop <= 20;
}

function isNearBottom(chatContainer) {
    return chatContainer.scrollTop + chatContainer.clientHeight >= chatContainer.scrollHeight - 20;
}

function collectVisibleMessages(chatContainer, collectedMessages, limit) {
    // Teams keeps a virtualized window of messages mounted in chronological
    // DOM order. Walk that window backwards so a limit means "latest N"
    // instead of "the first N currently mounted". The old ordering could,
    // for example, keep 57 nodes mounted and make a limit of 50 omit the
    // seven newest messages.
    const messageBlocks = Array.from(
        chatContainer.querySelectorAll('[data-tid="chat-pane-message"]')
    ).reverse();

    messageBlocks.forEach(block => {
        if (limit > 0 && collectedMessages.size >= limit) return;

        const mid = block.getAttribute("data-mid");
        if (!mid || collectedMessages.has(mid)) return;

        const message = extractMessage(block, mid);
        if (message) {
            collectedMessages.set(mid, message);
        } else {
            console.warn(`Skipped message ${mid}: could not find readable content.`, {
                block: describeNode(block)
            });
        }
    });

    return messageBlocks.length;
}

function extractMessage(block, mid) {
    const textEl = document.getElementById(`content-${mid}`) || block.querySelector('[id^="content-"]');
    const authorEl = document.getElementById(`author-${mid}`);
    const timeEl = document.getElementById(`timestamp-${mid}`);

    if (!textEl) return null;

    const text = cleanText(textEl.innerText) ||
        cleanText(textEl.getAttribute("aria-label")) ||
        "[empty or non-text message]";
    const author = cleanText(authorEl?.innerText) || "Unknown author";
    const time = cleanText(timeEl?.getAttribute("title")) ||
        cleanText(timeEl?.innerText) ||
        "Unknown time";
    const timestamp = Number.parseInt(mid, 10);

    return {
        timestamp: Number.isFinite(timestamp) ? timestamp : Number.MAX_SAFE_INTEGER,
        formatted: `[${time}] ${author}: ${text}`
    };
}

function cleanText(value) {
    return (value || "").replace(/\s+/g, " ").trim();
}

function buildFinalText(collectedMessages) {
    return Array.from(collectedMessages.values())
        .sort((a, b) => a.timestamp - b.timestamp)
        .map(message => message.formatted)
        .join("\n");
}

async function copyOrDownload(finalText, count) {
    return showDeliveryPanel(finalText, count);
}

function showDeliveryPanel(finalText, count) {
    return new Promise(resolve => {
        const panel = document.createElement("div");
        const title = document.createElement("div");
        const status = document.createElement("div");
        const buttons = document.createElement("div");
        const copyButton = document.createElement("button");
        const downloadButton = document.createElement("button");
        const closeButton = document.createElement("button");

        panel.style.cssText = [
            "position:fixed",
            "right:16px",
            "bottom:16px",
            "z-index:2147483647",
            "width:360px",
            "padding:14px",
            "border:1px solid #c8c8c8",
            "border-radius:8px",
            "box-shadow:0 8px 24px rgba(0,0,0,.22)",
            "background:white",
            "color:#242424",
            "font:14px/1.35 Segoe UI, Arial, sans-serif"
        ].join(";");

        title.textContent = `Teams scrape complete: ${count} messages`;
        title.style.cssText = "font-weight:600;margin-bottom:8px";
        status.textContent = "Choose how to export the scraped text.";
        status.style.cssText = "margin-bottom:12px";
        buttons.style.cssText = "display:flex;gap:8px;justify-content:flex-end";

        copyButton.textContent = "Copy";
        downloadButton.textContent = "Download";
        closeButton.textContent = "Close";

        [copyButton, downloadButton, closeButton].forEach(button => {
            button.style.cssText = "padding:6px 10px;border:1px solid #bbb;border-radius:4px;background:#f5f5f5;cursor:pointer";
        });

        copyButton.addEventListener("click", async () => {
            const result = await writeClipboard(finalText);

            if (result.ok) {
                status.textContent = "Copied to clipboard.";
                resolve(result.method);
                setTimeout(() => panel.remove(), 750);
                return;
            }

            console.warn("Clipboard copy failed; showing manual copy box.", result.error);
            status.textContent = "Clipboard was blocked. Press Ctrl+C in the selected text box below, or use Download.";
            showManualCopyBox(panel, finalText);
            resolve("manual copy box shown after clipboard failure");
        });

        downloadButton.addEventListener("click", () => {
            downloadFile(finalText);
            status.textContent = "Downloaded text file.";
            resolve("downloaded from delivery panel");
        });

        closeButton.addEventListener("click", () => {
            panel.remove();
            resolve("delivery panel closed without copy/download");
        });

        buttons.append(copyButton, downloadButton, closeButton);
        panel.append(title, status, buttons);
        document.body.appendChild(panel);
    });
}

async function writeClipboard(text) {
    if (navigator.clipboard?.writeText) {
        try {
            await navigator.clipboard.writeText(text);
            return { ok: true, method: "copied with navigator.clipboard.writeText" };
        } catch (error) {
            console.warn("navigator.clipboard.writeText failed; trying document.execCommand fallback.", error);
        }
    } else {
        console.warn("navigator.clipboard.writeText is not available in this browser context; trying document.execCommand fallback.");
    }

    if (copyToClipboardFallback(text)) {
        return { ok: true, method: "copied with document.execCommand fallback" };
    }

    return { ok: false, error: "Both navigator.clipboard.writeText and document.execCommand('copy') failed." };
}

function showManualCopyBox(panel, text) {
    let textArea = panel.querySelector("textarea");
    if (!textArea) {
        textArea = document.createElement("textarea");
        textArea.style.cssText = "box-sizing:border-box;width:100%;height:160px;margin-top:10px;font:12px Consolas, monospace";
        panel.appendChild(textArea);
    }

    textArea.value = text;
    textArea.focus();
    textArea.select();
}

function copyToClipboardFallback(text) {
    const textArea = document.createElement("textarea");
    textArea.value = text;
    textArea.style.position = "fixed";
    textArea.style.left = "-9999px";
    textArea.style.top = "0";
    document.body.appendChild(textArea);

    try {
        textArea.focus();
        textArea.select();
        return document.execCommand("copy");
    } catch (error) {
        console.error("document.execCommand('copy') failed.", error);
        return false;
    } finally {
        document.body.removeChild(textArea);
    }
}

function downloadFile(content) {
    try {
        const blob = new Blob([content], { type: "text/plain" });
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");

        link.href = url;
        link.download = buildExportFileName();
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
        URL.revokeObjectURL(url);
    } catch (error) {
        throw new TeamsScrapeError(
            "Could not download the scraped chat text.",
            {
                originalError: error?.message || String(error),
                likelyFix: "Check whether downloads are blocked for teams.microsoft.com, then try copying from the returned result.text in the console."
            }
        );
    }
}

function buildExportFileName() {
    try {
        const timestamp = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
        const chatLabel = getChatLabel();
        const prefix = sanitizeFileName(chatLabel);

        return prefix
            ? `${prefix}_${timestamp}.txt`
            : `teams_chat_export_${timestamp}.txt`;
    } catch {
        return `teams_chat_export_${new Date().toISOString().slice(0, 10)}.txt`;
    }
}

function getChatLabel() {
    const candidates = [
        '[data-tid="chat-header-title"]',
        '[data-tid="chat-title"]',
        '[data-tid="channel-title"]',
        'h1[aria-label]',
        'h1',
        '[role="main"] [aria-label]'
    ];

    for (const selector of candidates) {
        const element = document.querySelector(selector);
        const label = cleanText(element?.innerText) || cleanText(element?.getAttribute?.("aria-label"));

        if (label && !/^(Microsoft Teams|Chat|Teams)$/i.test(label)) {
            return label;
        }
    }

    return cleanText(document.title.replace(/\|.*$/, ""));
}

function sanitizeFileName(value) {
    return cleanText(value)
        .replace(/[<>:"/\\|?*\u0000-\u001f]/g, "_")
        .replace(/\s+/g, "_")
        .replace(/_+/g, "_")
        .replace(/^_+|_+$/g, "")
        .slice(0, 80);
}

function describeNode(node) {
    const element = toElement(node);
    if (!element) {
        return node ? `${node.nodeName || "unknown node"} (not an Element)` : "null";
    }

    const id = element.id ? `#${element.id}` : "";
    const dataTid = element.getAttribute("data-tid") ? `[data-tid="${element.getAttribute("data-tid")}"]` : "";
    const role = element.getAttribute("role") ? `[role="${element.getAttribute("role")}"]` : "";
    const classes = element.className && typeof element.className === "string"
        ? `.${element.className.trim().split(/\s+/).slice(0, 3).join(".")}`
        : "";

    return `${element.tagName.toLowerCase()}${id}${dataTid}${role}${classes}`;
}

function reportFailure(error) {
    const message = error?.message || String(error);
    const details = error?.details || {};

    console.group("Teams chat scrape failed");
    console.error(message);
    if (Object.keys(details).length > 0) {
        console.info("Details:", details);
    }
    console.error(error);
    console.groupEnd();

    alert(`Teams chat scrape failed:\n\n${message}\n\nOpen the console for details.`);
}

function sleep(ms, run) {
    if (run.stopped) return Promise.resolve();

    return new Promise(resolve => {
        const finish = () => {
            clearTimeout(timer);
            if (run.wake === finish) run.wake = null;
            resolve();
        };
        const timer = setTimeout(finish, ms);
        run.wake = finish;
    });
}

// USAGE:
//   scrapeTeamsByID();     // scrape as much as Teams will lazy-load
//   scrapeTeamsByID(50);   // stop after 50 messages
//   stop();                 // stop promptly and export messages collected so far
scrapeTeamsByID();
