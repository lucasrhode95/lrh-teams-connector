// Teams connector UI: choose conversations, filter by date and count, download an export.
// Every /api call carries the per-run session token. Message content is never rendered as HTML.

const TOKEN = document.querySelector('meta[name="session-token"]').content;
const themePreference = window.matchMedia("(prefers-color-scheme: dark)");

function applyTheme() {
  document.documentElement.dataset.theme = themePreference.matches ? "dark" : "light";
}

applyTheme();
themePreference.addEventListener("change", applyTheme);

const chats = [];
const selected = new Set();
let previewConversationId = null;
const $ = (id) => document.getElementById(id);
const list = $("chat-list");
const search = $("chat-search");
const messageLimitInput = $("message-limit");
const count = $("selection-count");
const downloadButton = $("download");
const cancelButton = $("cancel-export");
const includeAttachmentsInput = $("include-attachments");
const includeTranscriptsInput = $("include-transcripts");
const includeSummariesInput = $("include-summaries");
const statusText = $("status");
const authHelp = $("auth-help");
const accountInfo = $("account-info");
const selectedChats = $("selected-chats");
const previewContent = $("preview-content");
const settingsToggle = $("settings-toggle");
const settingsMenu = $("settings-menu");
const calendar = $("calendar");
const calendarDays = $("calendar-days");
const calendarMonthLabel = $("calendar-month");
const rangeToggle = $("range-toggle");
const rangeLabel = $("range-label");
const fromDateInput = $("from-date");
const toDateInput = $("to-date");
let calendarMonth = new Date();
calendarMonth.setDate(1);
let choosingEnd = false;
let rangeStartDraft = null;
let activeExportId = null;
let activePreviewJobId = null;
let previewGeneration = 0;
let previewEntireRange = false;
let previewIsCapped = false;
let previewHasText = false;
let previewDebounce = null;
let currentAccountKey = null;
let chatRefreshInProgress = false;
const previewCache = new Map();
const PREVIEW_CACHE_MS = 60_000;
const PREVIEW_CACHE_LIMIT = 10;

class ApiError extends Error {
  constructor(status, body) {
    super(body?.error || `The request failed (${status}).`);
    this.status = status;
    this.kind = body?.kind;
  }
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    cache: "no-store",
    ...options,
    headers: { "X-Session-Token": TOKEN, ...(options.headers || {}) },
  });
  if (!response.ok) {
    let body = null;
    try { body = await response.json(); } catch (_) { /* not JSON */ }
    throw new ApiError(response.status, body);
  }
  return response;
}

const json = async (path, options) => (await api(path, options)).json();

// ------------------------------------------------------------------ dates

function dateKey(date) {
  return date.getFullYear() + "-" + String(date.getMonth() + 1).padStart(2, "0") + "-" + String(date.getDate()).padStart(2, "0");
}

function dateFromKey(value) {
  const parts = value.split("-").map(Number);
  return new Date(parts[0], parts[1] - 1, parts[2]);
}

function formatCalendarDate(value) {
  return dateFromKey(value).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

function setDateRange(start, end) {
  fromDateInput.value = start || "";
  toDateInput.value = end || "";
  rangeLabel.textContent = !start ? "Any date"
    : start === end ? formatCalendarDate(start) : formatCalendarDate(start) + " – " + formatCalendarDate(end);
  $("date-filter-clear").hidden = !start;
  renderCalendar();
  reloadPreviewForSettings();
}

function focusCalendarDate(key) {
  calendarDays.querySelector(`[data-date="${key}"]`)?.focus();
}

function setCalendarOpen(open, { restoreFocus = false } = {}) {
  calendar.hidden = !open;
  rangeToggle.setAttribute("aria-expanded", String(open));
  if (!open) {
    if (restoreFocus) rangeToggle.focus();
    return;
  }
  // Open towards whichever side has room for the calendar.
  const box = $("range-picker").getBoundingClientRect();
  calendar.classList.toggle("align-right", box.left + 370 > window.innerWidth);
  calendarMonth = fromDateInput.value ? dateFromKey(fromDateInput.value) : new Date();
  calendarMonth.setDate(1);
  choosingEnd = false;
  rangeStartDraft = null;
  renderCalendar();
  focusCalendarDate(fromDateInput.value || dateKey(new Date()));
}

function renderCalendar() {
  calendarMonthLabel.textContent = calendarMonth.toLocaleDateString(undefined, { month: "long", year: "numeric" });
  const first = new Date(calendarMonth.getFullYear(), calendarMonth.getMonth(), 1);
  const sundayOffset = first.getDay(); // weeks start on Sunday, as in the Outlook connector
  const start = choosingEnd && rangeStartDraft ? rangeStartDraft : fromDateInput.value;
  const end = choosingEnd && rangeStartDraft ? rangeStartDraft : toDateInput.value;
  const today = dateKey(new Date());
  const days = [];
  for (let index = 0; index < 42; index += 1) {
    const day = new Date(first.getFullYear(), first.getMonth(), 1 - sundayOffset + index);
    const key = dateKey(day);
    const inRange = Boolean(start && end && key >= start && key <= end);
    const button = document.createElement("button");
    button.type = "button";
    button.className = "calendar-day";
    button.textContent = String(day.getDate());
    button.dataset.date = key;
    button.setAttribute("aria-label", day.toLocaleDateString(undefined, { dateStyle: "full" }));
    button.setAttribute("aria-pressed", String(inRange));
    button.disabled = key > today;
    if (day.getMonth() !== calendarMonth.getMonth()) button.classList.add("outside-month");
    if (inRange) button.classList.add("in-range");
    if (start && key === start) button.classList.add("range-start");
    if (end && key === end) button.classList.add("range-end");
    button.addEventListener("click", () => chooseCalendarDate(key));
    days.push(button);
  }
  calendarDays.replaceChildren(...days);
}

// The first click picks one day; the second makes the range between both clicks, in either order.
// The calendar stays open until Done, Escape or a click outside it.
function chooseCalendarDate(key) {
  if (key > dateKey(new Date())) return;
  if (!choosingEnd) {
    rangeStartDraft = key;
    choosingEnd = true;
    renderCalendar();
  } else {
    choosingEnd = false;
    setDateRange(key < rangeStartDraft ? key : rangeStartDraft, key < rangeStartDraft ? rangeStartDraft : key);
  }
  focusCalendarDate(key);
}

function changeCalendarMonth(offset) {
  calendarMonth.setMonth(calendarMonth.getMonth() + offset);
  renderCalendar();
}

function clearDateRange() {
  choosingEnd = false;
  rangeStartDraft = null;
  setDateRange("", "");
  setCalendarOpen(false, { restoreFocus: true });
}

function browserUtcOffsetMinutes(value, endOfDay) {
  const date = dateFromKey(value);
  return new Date(date.getFullYear(), date.getMonth(), date.getDate(), endOfDay ? 23 : 0, endOfDay ? 59 : 0)
    .getTimezoneOffset();
}

// ------------------------------------------------------------------ avatars

const photoUrls = new Map(); // user id -> object URL, or null when the person has no photo
const photoQueue = [];
const presenceResults = new Map(); // conversation id -> presence result for the current page
const presenceQueue = [];
const presencePending = new Map(); // conversation id -> current presence generation
let photosInFlight = 0;
let presenceInFlight = 0;
let presenceGeneration = 0;
let ownPresenceKey = null;
let ownPresenceRequest = null;

function initials(name) {
  const words = String(name || "").replace(/\(.*?\)/g, " ").split(/[\s,]+/).filter(Boolean);
  if (!words.length) return "?";
  const letters = words.length > 1 ? words[0][0] + words[words.length - 1][0] : words[0].slice(0, 2);
  return letters.toLocaleUpperCase();
}

function chatKind(chat) {
  return chat.kind || (String(chat.id || "").toLowerCase().startsWith("19:meeting_") ? "meeting" : "unknown");
}

function fallbackIcon(kind) {
  if (kind === "meeting") return "/static/chat-call.svg";
  if (kind === "group" || kind === "channel") return "/static/chat-group.svg";
  return null;
}

function showImage(box, src) {
  const image = document.createElement("img");
  image.alt = "";
  image.src = src;
  box.replaceChildren(image);
}

function fillAvatar(box, kind, name, userId) {
  const icon = fallbackIcon(kind);
  if (icon) {
    showImage(box, icon);
    return;
  }
  box.textContent = initials(name);
  if (!userId) return;
  if (photoUrls.has(userId)) {
    if (photoUrls.get(userId)) showImage(box, photoUrls.get(userId));
    return;
  }
  photoQueue.push({ box, userId });
  pumpPhotos();
}

function pumpPhotos() {
  while (photosInFlight < 6 && photoQueue.length) {
    const { box, userId } = photoQueue.shift();
    if (!box.isConnected) continue;
    if (photoUrls.has(userId)) {
      if (photoUrls.get(userId)) showImage(box, photoUrls.get(userId));
      continue;
    }
    photosInFlight += 1;
    api("/api/photos/" + encodeURIComponent(userId))
      .then(async (response) => {
        const url = URL.createObjectURL(await response.blob());
        photoUrls.set(userId, url);
        showImage(box, url);
      })
      .catch((error) => {
        if (error instanceof ApiError && error.status === 401) void showError(error);
        photoUrls.set(userId, null);
      })
      .finally(() => {
        photosInFlight -= 1;
        pumpPhotos();
      });
  }
}

function presenceState(presence) {
  const value = String(presence?.availability || "").toLowerCase().replace(/[\s_-]/g, "");
  if (["available", "online"].includes(value)) return "available";
  if (["busy", "outofoffice"].includes(value)) return "busy";
  if (["donotdisturb", "dnd"].includes(value)) return "dnd";
  if (["away", "berightback"].includes(value)) return "away";
  if (value === "offline") return "offline";
  return "unknown";
}

function presenceLabel(presence) {
  if (!presence) return "Presence unavailable";
  const state = presenceState(presence);
  const labels = { available: "Available", busy: "Busy", dnd: "Do not disturb", away: "Away", offline: "Offline", unknown: "Presence unavailable" };
  const activity = String(presence.activity || "");
  const activityKey = activity.toLowerCase().replace(/[^a-z]/g, "");
  const activityLabels = { inacall: "In a call", inconferencecall: "In a conference call", presenting: "Presenting" };
  const humanActivity = activity.replace(/([a-z])([A-Z])/g, "$1 $2").toLowerCase();
  const knownActivity = activityLabels[activityKey] || (humanActivity ? humanActivity.replace(/^./, (char) => char.toUpperCase()) : "");
  return labels[state] + (knownActivity && state !== "unknown" ? " · " + knownActivity : "");
}

function setPresenceBadge(badge, presence) {
  const state = presenceState(presence);
  const label = presenceLabel(presence);
  badge.className = "presence-dot presence-" + state + (badge.classList.contains("chat-presence-dot") ? " chat-presence-dot" : "");
  badge.setAttribute("role", "img");
  badge.setAttribute("aria-label", label);
  badge.title = label;
}

function lastSeenText(presence) {
  if (!presence?.last_active_at) return "Last seen: unavailable";
  const lastActive = new Date(presence.last_active_at);
  if (Number.isNaN(lastActive.getTime())) return "Last seen: unavailable";
  const device = String(presence.device_type || "").replace(/([a-z])([A-Z])/g, "$1 $2").replace(/[_-]+/g, " ").trim();
  return "Last seen: " + lastActive.toLocaleString() + (device ? " · " + device : "");
}

function applyPresenceToRow(row, result) {
  const presence = result?.presence || null;
  const metadata = row.querySelector(".chat-meta");
  const badge = row.querySelector(".chat-presence-dot");
  if (metadata) metadata.textContent = lastSeenText(presence);
  if (badge) setPresenceBadge(badge, presence);
}

function isRowVisible(row) {
  const rowRect = row.getBoundingClientRect();
  const listRect = list.getBoundingClientRect();
  return rowRect.width > 0 && rowRect.height > 0 && rowRect.bottom > Math.max(listRect.top, 0) &&
    rowRect.top < Math.min(listRect.bottom, window.innerHeight) && rowRect.right > Math.max(listRect.left, 0) &&
    rowRect.left < Math.min(listRect.right, window.innerWidth);
}

function pumpPresence() {
  while (presenceInFlight < 4 && presenceQueue.length) {
    const job = presenceQueue.shift();
    const { conversationId, generation } = job;
    if (presencePending.get(conversationId) !== generation) continue;
    if (generation !== presenceGeneration) {
      presencePending.delete(conversationId);
      continue;
    }
    let { row } = job;
    if (!row.isConnected) {
      row = Array.from(list.querySelectorAll(".chat-row")).find((item) => item.dataset.conversationId === conversationId);
    }
    if (!row) {
      presencePending.delete(conversationId);
      continue;
    }
    if (!isRowVisible(row)) {
      presencePending.delete(conversationId);
      presenceObserver.observe(row);
      continue;
    }
    if (presenceResults.has(conversationId)) {
      presencePending.delete(conversationId);
      applyPresenceToRow(row, presenceResults.get(conversationId));
      continue;
    }
    presenceInFlight += 1;
    json("/api/presence/chat?conversation_id=" + encodeURIComponent(conversationId))
      .catch((error) => {
        if (generation === presenceGeneration && error instanceof ApiError && error.status === 401) void showError(error);
        return { presence: null, reason: "Presence unavailable." };
      })
      .then((result) => {
        if (generation !== presenceGeneration) return;
        if (result.presence) presenceResults.set(conversationId, result);
        for (const current of list.querySelectorAll(".chat-row")) {
          if (current.dataset.conversationId === conversationId) applyPresenceToRow(current, result);
        }
      })
      .finally(() => {
        presenceInFlight -= 1;
        if (presencePending.get(conversationId) === generation) presencePending.delete(conversationId);
        pumpPresence();
      });
  }
}

function clearPresenceResults() {
  presenceResults.clear();
  presenceQueue.length = 0;
  presencePending.clear();
  presenceGeneration += 1;
}

function loadOwnPresence(accountId) {
  if (!accountId) return;
  if (ownPresenceKey === accountId && ownPresenceRequest) return;
  ownPresenceKey = accountId;
  const badge = $("account-presence");
  setPresenceBadge(badge, null);
  ownPresenceRequest = json("/api/presence/me")
    .then((result) => {
      if (ownPresenceKey === accountId && currentAccountKey === accountId) setPresenceBadge(badge, result.presence || null);
    })
    .catch((error) => {
      if (ownPresenceKey === accountId && currentAccountKey === accountId && error instanceof ApiError && error.status === 401) {
        void showError(error);
      }
      if (ownPresenceKey === accountId && currentAccountKey === accountId) setPresenceBadge(badge, null);
    })
    .finally(() => {
      if (ownPresenceKey === accountId) ownPresenceRequest = null;
    });
}

function positionChatTooltip(row, tooltip) {
  window.requestAnimationFrame(() => {
    if (!row.matches(":hover, :focus-within")) return;
    const rect = row.getBoundingClientRect();
    const width = Math.min(340, window.innerWidth - 16);
    tooltip.style.width = width + "px";
    const height = tooltip.offsetHeight;
    const top = rect.top > height + 12 ? rect.top - height - 8 : Math.min(window.innerHeight - height - 8, rect.bottom + 8);
    const left = Math.max(8, Math.min(rect.left, window.innerWidth - width - 8));
    tooltip.style.left = left + "px";
    tooltip.style.top = Math.max(8, top) + "px";
  });
}

const avatarObserver = new IntersectionObserver((entries) => {
  for (const entry of entries) {
    if (!entry.isIntersecting) continue;
    avatarObserver.unobserve(entry.target);
    const { kind, name, user } = entry.target.dataset;
    fillAvatar(entry.target, kind, name, user || null);
  }
}, { root: list, rootMargin: "200px" });

const presenceObserver = new IntersectionObserver((entries) => {
  for (const entry of entries) {
    if (!entry.isIntersecting) continue;
    presenceObserver.unobserve(entry.target);
    const row = entry.target;
    const conversationId = row.dataset.presenceConversation;
    if (!conversationId) continue;
    if (presenceResults.has(conversationId)) applyPresenceToRow(row, presenceResults.get(conversationId));
    else {
      const generation = presenceGeneration;
      if (presencePending.get(conversationId) === generation) continue;
      presencePending.set(conversationId, generation);
      presenceQueue.push({ row, conversationId, generation });
      pumpPresence();
    }
  }
}, { root: list, rootMargin: "0px" });

function repositionChatTooltip() {
  const row = list.querySelector(".chat-row:hover, .chat-row:focus-within");
  const tooltip = row?.querySelector(".chat-tooltip");
  if (row && tooltip) positionChatTooltip(row, tooltip);
}

list.addEventListener("scroll", repositionChatTooltip);
window.addEventListener("resize", repositionChatTooltip);

// ------------------------------------------------------------------ list

function setStatus(message, isError) {
  statusText.textContent = message;
  statusText.classList.toggle("error", Boolean(isError));
}

function chatTypeLabel(chat) {
  const labels = { self: "Notes to self", meeting: "Meeting", group: "Group", one_on_one: "One-to-one", channel: "Channel" };
  return labels[chatKind(chat)] || "Conversation";
}

function updateControls() {
  const busy = downloadButton.dataset.busy === "true";
  count.textContent = selected.size + " selected";
  downloadButton.disabled = selected.size === 0 || busy;
  cancelButton.hidden = !busy;
  cancelButton.disabled = !activeExportId;
}

function renderSelection() {
  count.textContent = selected.size + " selected";
  downloadButton.disabled = selected.size === 0 || downloadButton.dataset.busy === "true";
  selectedChats.replaceChildren();
  for (const id of selected) {
    const chat = chats.find((item) => item.id === id);
    if (!chat) continue;
    const pill = document.createElement("span");
    pill.className = "selected-pill";
    const name = document.createElement("span");
    name.className = "selected-pill-name";
    name.textContent = chat.name;
    const remove = document.createElement("button");
    remove.className = "pill-remove";
    remove.type = "button";
    remove.textContent = "×";
    remove.setAttribute("aria-label", "Remove " + chat.name + " from download selection");
    remove.addEventListener("click", (event) => {
      event.stopPropagation();
      selected.delete(id);
      renderSelection();
      updateControls();
      renderChatStates();
    });
    pill.append(name, remove);
    selectedChats.appendChild(pill);
  }
}

function renderChatStates() {
  for (const row of list.querySelectorAll(".chat-row")) {
    const id = row.dataset.conversationId;
    const checked = selected.has(id);
    row.classList.toggle("selected", checked);
    row.classList.toggle("previewed", id === previewConversationId);
    const checkbox = row.querySelector('input[type="checkbox"]');
    if (checkbox) checkbox.checked = checked;
  }
}

function setChatSelection(id, checked, { preview = true } = {}) {
  if (checked) selected.add(id);
  else selected.delete(id);
  if (preview) {
    previewConversationId = id;
    previewEntireRange = false;
    previewIsCapped = false;
    void startPreview({ entireRange: false });
  }
  renderSelection();
  renderChatStates();
  updateControls();
}

function renderChats() {
  const query = search.value.trim().toLocaleLowerCase();
  const visible = chats.filter((chat) => !query || chat.name.toLocaleLowerCase().includes(query));
  for (const avatar of list.querySelectorAll(".chat-avatar")) avatarObserver.unobserve(avatar);
  for (const row of list.querySelectorAll(".chat-row")) presenceObserver.unobserve(row);
  list.replaceChildren();
  if (!visible.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = chats.length ? "No conversations match that filter." : "No conversations were found.";
    list.appendChild(empty);
    return;
  }
  for (const [index, chat] of visible.entries()) {
    const row = document.createElement("div");
    row.className = "chat-row";
    row.tabIndex = 0;
    row.setAttribute("role", "group");
    row.dataset.conversationId = chat.id;
    const tooltipText = chatTypeLabel(chat) + " · " + (chat.last_message_at
      ? new Date(chat.last_message_at).toLocaleString() : "No recent activity shown");
    const tooltip = document.createElement("span");
    tooltip.className = "chat-tooltip";
    tooltip.id = "chat-tooltip-" + index;
    tooltip.setAttribute("role", "tooltip");
    tooltip.textContent = tooltipText;
    row.setAttribute("aria-describedby", tooltip.id);
    row.addEventListener("mouseenter", () => positionChatTooltip(row, tooltip));
    row.addEventListener("focusin", () => positionChatTooltip(row, tooltip));
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.setAttribute("aria-label", "Include " + chat.name + " in download");
    checkbox.checked = selected.has(chat.id);
    checkbox.addEventListener("change", () => {
      setChatSelection(chat.id, checkbox.checked);
    });
    const avatarWrap = document.createElement("span");
    avatarWrap.className = "chat-avatar-wrap";
    const avatar = document.createElement("span");
    avatar.className = "chat-avatar avatar";
    avatar.setAttribute("aria-hidden", "true");
    avatar.dataset.kind = chatKind(chat);
    avatar.dataset.name = chat.name;
    if (chat.photo_user_id) avatar.dataset.user = chat.photo_user_id;
    const personChat = chatKind(chat) === "one_on_one" && chat.photo_user_id && !chat.photo_user_id.startsWith("28:");
    if (personChat) row.dataset.presenceConversation = chat.id;
    avatarWrap.appendChild(avatar);
    if (personChat) {
      const badge = document.createElement("span");
      badge.className = "presence-dot presence-unknown chat-presence-dot";
      badge.setAttribute("role", "img");
      badge.setAttribute("aria-label", "Presence unavailable");
      badge.title = "Presence unavailable";
      avatarWrap.appendChild(badge);
    }
    avatarObserver.observe(avatar);
    const text = document.createElement("span");
    const name = document.createElement("span");
    name.className = "chat-name";
    name.textContent = chat.name;
    const metadata = document.createElement("span");
    metadata.className = "chat-meta";
    if (personChat) metadata.textContent = "Last seen: loading presence…";
    text.appendChild(name);
    if (personChat) text.appendChild(metadata);
    row.append(checkbox, avatarWrap, text, tooltip);
    row.addEventListener("click", (event) => {
      if (event.target === checkbox) return;
      setChatSelection(chat.id, !selected.has(chat.id));
    });
    row.addEventListener("keydown", (event) => {
      if (event.target === checkbox || (event.key !== "Enter" && event.key !== " ")) return;
      event.preventDefault();
      setChatSelection(chat.id, !selected.has(chat.id));
    });
    row.classList.toggle("selected", selected.has(chat.id));
    row.classList.toggle("previewed", chat.id === previewConversationId);
    list.appendChild(row);
    if (personChat) {
      if (presenceResults.has(chat.id)) applyPresenceToRow(row, presenceResults.get(chat.id));
      else presenceObserver.observe(row);
    }
  }
}

async function showError(error) {
  if (error instanceof ApiError && error.status === 401) {
    clearPreviewCache();
    previewGeneration += 1;
    if (activePreviewJobId) void cancelServerPreview(activePreviewJobId);
    activePreviewJobId = null;
    showPreviewEmpty("Teams sign-in is required to load conversation previews.");
    try {
      const status = await json("/api/status");
      authHelp.textContent = "Teams sign-in is required. In a terminal, run `" + status.sign_in_command +
        "`, complete Microsoft's sign-in, then reload this page.";
    } catch (_) {
      authHelp.textContent = "Teams sign-in is required. Run `teams-connector auth` in a terminal, then reload this page.";
    }
    authHelp.hidden = false;
    setStatus("Teams sign-in is required.", true);
    return;
  }
  setStatus(error instanceof ApiError ? error.message : "Could not reach the local server.", true);
}

function clearPreviewCache() {
  previewCache.clear();
}

function showPreviewEmpty(message) {
  previewHasText = false;
  previewIsCapped = false;
  previewEntireRange = false;
  previewContent.replaceChildren();
  const empty = document.createElement("div");
  empty.className = "preview-empty";
  empty.textContent = message;
  previewContent.appendChild(empty);
  setPreviewToolbar();
}

function setPreviewToolbar({ view = false, status = "", spinning = false, error = false, retry = false, cancel = false } = {}) {
  const toolbar = $("preview-toolbar");
  const statusBox = $("preview-status");
  $("view-entire-range").hidden = !view;
  $("retry-preview").hidden = !retry;
  $("cancel-preview").hidden = !cancel;
  statusBox.replaceChildren();
  statusBox.classList.toggle("error", error);
  if (spinning) {
    const spinner = document.createElement("span");
    spinner.className = "spinner";
    spinner.setAttribute("aria-hidden", "true");
    statusBox.appendChild(spinner);
  }
  if (status) statusBox.appendChild(document.createTextNode(status));
  toolbar.hidden = !view && !status && !retry && !cancel;
}

function renderPreviewText(result) {
  previewContent.replaceChildren();
  const blocks = result.text.split(/\n{2,}/);
  for (const block of blocks) {
    const element = document.createElement("div");
    element.className = "preview-text-block";
    element.textContent = block;
    previewContent.appendChild(element);
  }
  previewHasText = true;
  previewIsCapped = Boolean(result.capped);
}

function exportSettingsPayload() {
  const limit = getMessageLimit();
  if (!limit.valid) return null;
  return {
    from_date: fromDateInput.value || null,
    to_date: toDateInput.value || null,
    from_utc_offset_minutes: fromDateInput.value ? browserUtcOffsetMinutes(fromDateInput.value, false) : null,
    to_utc_offset_minutes: toDateInput.value ? browserUtcOffsetMinutes(toDateInput.value, true) : null,
    message_limit: limit.value,
    include_attachments: includeAttachmentsInput.checked,
    include_transcripts: includeTranscriptsInput.checked,
    include_summaries: includeSummariesInput.checked,
  };
}

function previewPayload(conversationId, entireRange) {
  const settings = exportSettingsPayload();
  if (!settings) return null;
  return {
    conversation_ids: [conversationId],
    ...settings,
    entire_range: entireRange,
  };
}

function previewCacheKey(conversationId, payload) {
  return JSON.stringify([currentAccountKey, conversationId, payload.from_date, payload.from_utc_offset_minutes,
    payload.to_date, payload.to_utc_offset_minutes, payload.message_limit, payload.include_summaries,
    payload.include_transcripts, payload.include_attachments, payload.entire_range]);
}

function getCachedPreview(key) {
  const entry = previewCache.get(key);
  if (!entry) return null;
  if (entry.expiresAt <= Date.now()) {
    previewCache.delete(key);
    return null;
  }
  previewCache.delete(key);
  previewCache.set(key, entry);
  return entry.result;
}

function cachePreview(key, result) {
  previewCache.delete(key);
  previewCache.set(key, { result, expiresAt: Date.now() + PREVIEW_CACHE_MS });
  while (previewCache.size > PREVIEW_CACHE_LIMIT) previewCache.delete(previewCache.keys().next().value);
}

function reloadPreviewForSettings() {
  window.clearTimeout(previewDebounce);
  if (!previewConversationId) return;
  previewGeneration += 1;
  const oldJobId = activePreviewJobId;
  activePreviewJobId = null;
  if (oldJobId) void cancelServerPreview(oldJobId);
  previewDebounce = window.setTimeout(() => {
    previewEntireRange = false;
    void startPreview({ entireRange: false });
  }, 220);
}

async function cancelServerPreview(jobId) {
  if (!jobId) return;
  try {
    await api("/api/preview/" + encodeURIComponent(jobId) + "/cancel", { method: "POST" });
  } catch (_) { /* a newer request may already have replaced this job */ }
}

function previewProgress(progress) {
  if (!progress) return "Loading the entire range…";
  const parts = [];
  if (progress.pages_fetched) parts.push(progress.pages_fetched + " pages");
  if (progress.messages_retrieved) parts.push(progress.messages_retrieved + " messages read");
  return "Loading the entire range" + (parts.length ? " · " + parts.join(" · ") : "…");
}

async function startPreview({ entireRange = false, forceRefresh = false, keepCurrent = false } = {}) {
  const conversationId = previewConversationId;
  const chat = chats.find((item) => item.id === conversationId);
  if (!conversationId || !chat) {
    showPreviewEmpty("Select a conversation to preview its export text.");
    return;
  }
  const payload = previewPayload(conversationId, entireRange);
  if (!payload) {
    setPreviewToolbar({ view: previewIsCapped && !previewEntireRange, status: "Enter a positive whole number for the message limit, or leave it blank.", error: true });
    return;
  }

  const generation = ++previewGeneration;
  const oldJobId = activePreviewJobId;
  activePreviewJobId = null;
  if (oldJobId) await cancelServerPreview(oldJobId);
  if (generation !== previewGeneration) return;

  previewEntireRange = entireRange;
  if (!keepCurrent) {
    previewHasText = false;
    previewIsCapped = false;
    previewContent.replaceChildren();
    const loading = document.createElement("div");
    loading.className = "loading-state";
    loading.setAttribute("role", "status");
    const spinner = document.createElement("span");
    spinner.className = "spinner";
    spinner.setAttribute("aria-hidden", "true");
    const label = document.createElement("span");
    label.textContent = "Loading preview for " + chat.name + "…";
    loading.append(spinner, label);
    previewContent.appendChild(loading);
  }
  setPreviewToolbar({
    view: previewIsCapped && !entireRange,
    status: entireRange ? "Preparing the entire range…" : "Preparing preview…",
    spinning: true,
    cancel: entireRange,
  });

  const key = previewCacheKey(conversationId, payload);
  const cached = forceRefresh ? null : getCachedPreview(key);
  if (cached) {
    renderPreviewText(cached);
    setPreviewToolbar({
      view: cached.capped && !entireRange,
      status: (entireRange ? "Entire range" : "Preview") + " · " + cached.messages + " messages",
    });
    return;
  }

  try {
    const started = await json("/api/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (generation !== previewGeneration) {
      await cancelServerPreview(started.id);
      return;
    }
    activePreviewJobId = started.id;
    for (;;) {
      const job = await json("/api/preview/" + encodeURIComponent(started.id));
      if (generation !== previewGeneration) return;
      if (job.status === "running") {
        setPreviewToolbar({
          view: previewIsCapped && !entireRange,
          status: entireRange ? previewProgress(job.progress) : "Preparing preview…",
          spinning: true,
          cancel: entireRange,
        });
        await wait(300);
        continue;
      }
      activePreviewJobId = null;
      if (job.status === "complete" && job.result) {
        cachePreview(key, job.result);
        renderPreviewText(job.result);
        setPreviewToolbar({
          view: job.result.capped && !entireRange,
          status: (entireRange ? "Entire range" : "Preview") + " · " + job.result.messages + " messages" +
            (job.result.capped ? " · excerpt limited" : ""),
        });
        return;
      }
      if (job.status === "cancelled") {
        if (entireRange && previewHasText) {
          previewEntireRange = false;
          setPreviewToolbar({ view: previewIsCapped, status: "Entire-range view cancelled. Showing the excerpt." });
        } else {
          setPreviewToolbar({ status: "Preview cancelled.", retry: true });
        }
        return;
      }
      const message = job.error?.message || "The preview could not be loaded.";
      setPreviewToolbar({ view: previewIsCapped && !entireRange, status: message, error: true, retry: true });
      if (job.error?.kind === "AuthenticationRequired") await showError(new ApiError(401, job.error));
      return;
    }
  } catch (error) {
    if (generation !== previewGeneration) return;
    activePreviewJobId = null;
    const message = error instanceof ApiError ? error.message : "Could not reach the local server.";
    setPreviewToolbar({ view: previewIsCapped && !entireRange, status: message, error: true, retry: true });
    if (error instanceof ApiError && error.status === 401) await showError(error);
  }
}

async function cancelPreview() {
  if (!activePreviewJobId) return;
  const jobId = activePreviewJobId;
  previewGeneration += 1;
  activePreviewJobId = null;
  await cancelServerPreview(jobId);
  if (previewEntireRange && previewHasText) {
    previewEntireRange = false;
    setPreviewToolbar({ view: previewIsCapped, status: "Entire-range view cancelled. Showing the excerpt." });
  } else {
    setPreviewToolbar({ status: "Preview cancelled.", retry: true });
  }
}

function applyCatalog(body) {
  const nextAccountKey = body.account?.photo_user_id || body.account?.email || null;
  if (currentAccountKey && nextAccountKey !== currentAccountKey) {
    clearPreviewCache();
    clearPresenceResults();
    ownPresenceKey = null;
    ownPresenceRequest = null;
    setPresenceBadge($("account-presence"), null);
    selected.clear();
    previewConversationId = null;
    previewGeneration += 1;
    if (activePreviewJobId) void cancelServerPreview(activePreviewJobId);
    activePreviewJobId = null;
    showPreviewEmpty("Select a conversation to preview its export text.");
  }
  currentAccountKey = nextAccountKey;
  chats.splice(0, chats.length, ...body.chats);
  $("account-name").textContent = body.account?.name || "";
  $("account-email").textContent = body.account?.email || "";
  fillAvatar($("account-avatar"), "self", body.account?.name, body.account?.photo_user_id);
  accountInfo.hidden = !body.account;
  loadOwnPresence(body.account?.photo_user_id);
  const known = new Set(chats.map((chat) => chat.id));
  for (const id of Array.from(selected)) if (!known.has(id)) selected.delete(id);
  if (previewConversationId && !known.has(previewConversationId)) {
    previewConversationId = null;
    previewGeneration += 1;
    if (activePreviewJobId) void cancelServerPreview(activePreviewJobId);
    activePreviewJobId = null;
    showPreviewEmpty("The previewed conversation is no longer available.");
  }
  renderChats();
  renderSelection();
  updateControls();
}

async function loadChats() {
  if (chatRefreshInProgress) return;
  chatRefreshInProgress = true;
  const refreshPreviewId = previewConversationId;
  const refreshPreviewEntireRange = previewEntireRange;
  clearPreviewCache();
  clearPresenceResults();
  previewGeneration += 1;
  const oldPreviewJobId = activePreviewJobId;
  activePreviewJobId = null;
  if (oldPreviewJobId) void cancelServerPreview(oldPreviewJobId);
  authHelp.hidden = true;
  const refreshButton = $("refresh");
  refreshButton.disabled = true;
  list.setAttribute("aria-busy", "true");
  setStatus(chats.length ? "Checking Teams for conversation updates…" : "Loading conversations…", false);
  let refreshed = false;
  let cacheShown = chats.length > 0;
  let refreshError = null;
  // The Teams refresh starts immediately; the saved catalog is shown meanwhile.
  const refresh = json("/api/chats?refresh=true")
    .then((body) => { applyCatalog(body); refreshed = true; })
    .catch((error) => { refreshError = error; });
  const cached = cacheShown ? Promise.resolve() : json("/api/chats?cached=true")
    .then((body) => {
      if (!refreshed && body.chats.length) {
        applyCatalog(body);
        cacheShown = true;
        setStatus("Showing saved conversations while checking Teams for updates…", false);
      }
    })
    .catch(() => { /* the live refresh decides */ });
  try {
    await Promise.all([refresh, cached]);
    if (refreshPreviewId && previewConversationId === refreshPreviewId && chats.some((chat) => chat.id === refreshPreviewId)) {
      void startPreview({ entireRange: refreshPreviewEntireRange, forceRefresh: true, keepCurrent: true });
    }
    if (refreshed) setStatus(chats.length + " conversations loaded.", false);
    else if (refreshError) {
      await showError(refreshError);
      if (!(cacheShown && chats.length)) list.replaceChildren();
    }
  } finally {
    chatRefreshInProgress = false;
    refreshButton.disabled = false;
    list.removeAttribute("aria-busy");
  }
}

// ------------------------------------------------------------------ export

function getMessageLimit() {
  const text = messageLimitInput.value.trim();
  const v = messageLimitInput.validity;
  if (v.badInput || v.stepMismatch || v.rangeUnderflow || v.rangeOverflow) return { valid: false, value: null };
  if (!text) return { valid: true, value: null };
  if (!/^[0-9]+$/.test(text)) return { valid: false, value: null };
  const value = Number(text);
  return { valid: Number.isSafeInteger(value) && value >= 1 && value <= 1000000, value };
}

const wait = (milliseconds) => new Promise((resolve) => window.setTimeout(resolve, milliseconds));

function describeProgress(progress) {
  if (!progress) return "Export is running…";
  const parts = [];
  if (progress.chat_index && progress.chats_total) parts.push("chat " + progress.chat_index + " of " + progress.chats_total);
  if (progress.pages_fetched) parts.push(progress.pages_fetched + " pages");
  if (progress.messages_retrieved) parts.push(progress.messages_retrieved + " messages retrieved");
  if (progress.phase === "writing" && progress.messages_written) parts.push(progress.messages_written + " messages written");
  if (progress.attachments_total) {
    const done = (progress.attachments_downloaded || 0) + (progress.attachments_failed || 0);
    parts.push(done + "/" + progress.attachments_total + " attachments processed");
  }
  const label = progress.phase === "writing" ? "Writing" : progress.phase === "attachments" ? "Downloading attachments" : "Retrieving";
  return label + (parts.length ? " · " + parts.join(" · ") : "…");
}

async function saveFile(path, filename) {
  const blob = await (await api(path)).blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

async function downloadSelected() {
  if (!selected.size) return;
  const settings = exportSettingsPayload();
  if (!settings) {
    messageLimitInput.setCustomValidity("Enter a positive whole number, or leave the field blank for unlimited.");
    messageLimitInput.reportValidity();
    setStatus("Maximum messages per chat must be a positive whole number or blank.", true);
    return;
  }
  messageLimitInput.setCustomValidity("");
  const payload = {
    conversation_ids: Array.from(selected),
    ...settings,
  };
  const previewJobId = activePreviewJobId;
  previewGeneration += 1;
  activePreviewJobId = null;
  if (previewJobId) {
    if (previewEntireRange && previewHasText) previewEntireRange = false;
    setPreviewToolbar({ view: previewIsCapped, status: "Preview stopped for download.", retry: true });
  }
  authHelp.hidden = true;
  downloadButton.dataset.busy = "true";
  downloadButton.textContent = "Preparing…";
  updateControls();
  setStatus("Retrieving and formatting the selected conversations…", false);
  try {
    if (previewJobId) await cancelServerPreview(previewJobId);
    const started = await json("/api/exports", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    activeExportId = started.id;
    updateControls();
    let job;
    for (;;) {
      job = await json("/api/exports/" + encodeURIComponent(started.id));
      if (job.status === "complete") break;
      if (job.status === "cancelled") {
        setStatus("Export stopped. " + (job.progress?.pages_fetched || 0) + " pages had completed.", false);
        return;
      }
      if (job.status === "failed") {
        if (job.error?.kind === "AuthenticationRequired") await showError(new ApiError(401, job.error));
        else setStatus(job.error?.message || "The export failed.", true);
        return;
      }
      setStatus(describeProgress(job.progress), false);
      await wait(500);
    }
    setStatus("Preparing the download…", false);
    await saveFile("/api/exports/" + encodeURIComponent(started.id) + "/file", job.artifact.filename);
    setStatus("Download ready: " + job.artifact.message_count + " messages.", false);
  } catch (error) {
    await showError(error);
  } finally {
    delete downloadButton.dataset.busy;
    downloadButton.textContent = "Download";
    activeExportId = null;
    updateControls();
  }
}

async function cancelExport() {
  if (!activeExportId) return;
  cancelButton.disabled = true;
  setStatus("Requesting cancellation…", false);
  try {
    await api("/api/exports/" + encodeURIComponent(activeExportId) + "/cancel", { method: "POST" });
    setStatus("Stopping after the current network request.", false);
  } catch (error) {
    await showError(error);
    cancelButton.disabled = false;
  }
}

// ------------------------------------------------------------------ wiring

search.addEventListener("input", renderChats);
messageLimitInput.addEventListener("input", () => {
  const valid = getMessageLimit().valid;
  messageLimitInput.setCustomValidity(valid ? "" : "Enter a positive whole number, or leave the field blank for unlimited.");
  reloadPreviewForSettings();
});
rangeToggle.addEventListener("click", () => setCalendarOpen(calendar.hidden));
$("previous-month").addEventListener("click", () => changeCalendarMonth(-1));
$("next-month").addEventListener("click", () => changeCalendarMonth(1));
let wheelTimeout = null;
calendar.addEventListener("wheel", (event) => {
  if (calendar.hidden || event.deltaY === 0) return;
  event.preventDefault();
  if (wheelTimeout !== null) return;
  changeCalendarMonth(event.deltaY < 0 ? -1 : 1);
  wheelTimeout = setTimeout(() => { wheelTimeout = null; }, 220);
}, { passive: false });
let swipeStart = null;
calendar.addEventListener("touchstart", (event) => {
  swipeStart = event.touches.length === 1 ? { x: event.touches[0].clientX, y: event.touches[0].clientY } : null;
}, { passive: true });
calendar.addEventListener("touchend", (event) => {
  const touch = event.changedTouches[0];
  const begin = swipeStart;
  swipeStart = null;
  if (!begin || !touch) return;
  const deltaX = touch.clientX - begin.x;
  const deltaY = touch.clientY - begin.y;
  if (Math.abs(deltaX) < 48 || Math.abs(deltaX) < Math.abs(deltaY) * 1.25) return;
  changeCalendarMonth(deltaX > 0 ? -1 : 1);
}, { passive: true });
calendar.addEventListener("touchcancel", () => { swipeStart = null; }, { passive: true });
$("clear-dates").addEventListener("click", clearDateRange);
$("date-filter-clear").addEventListener("click", clearDateRange);
$("today-date").addEventListener("click", () => {
  const today = new Date();
  calendarMonth = new Date(today.getFullYear(), today.getMonth(), 1);
  renderCalendar();
  focusCalendarDate(dateKey(today));
});
$("calendar-done").addEventListener("click", () => {
  if (choosingEnd && rangeStartDraft) {
    const day = rangeStartDraft;
    choosingEnd = false;
    rangeStartDraft = null;
    setDateRange(day, day);
  }
  setCalendarOpen(false, { restoreFocus: true });
});
document.addEventListener("pointerdown", (event) => {
  if (!calendar.hidden && !$("range-picker").contains(event.target)) setCalendarOpen(false);
  if (!settingsMenu.hidden && !$("settings-wrap")?.contains(event.target)) setSettingsOpen(false);
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !calendar.hidden) setCalendarOpen(false, { restoreFocus: true });
  if (event.key === "Escape" && !settingsMenu.hidden) setSettingsOpen(false, { restoreFocus: true });
});
function setSettingsOpen(open, { restoreFocus = false } = {}) {
  settingsMenu.hidden = !open;
  settingsToggle.setAttribute("aria-expanded", String(open));
  if (!open && restoreFocus) settingsToggle.focus();
}
settingsToggle.addEventListener("click", () => setSettingsOpen(settingsMenu.hidden));
$("view-entire-range").addEventListener("click", () => void startPreview({ entireRange: true, keepCurrent: true }));
$("retry-preview").addEventListener("click", () => void startPreview({ entireRange: previewEntireRange, forceRefresh: true, keepCurrent: previewHasText }));
$("cancel-preview").addEventListener("click", cancelPreview);
$("refresh").addEventListener("click", loadChats);
downloadButton.addEventListener("click", downloadSelected);
cancelButton.addEventListener("click", cancelExport);
for (const input of [includeAttachmentsInput, includeTranscriptsInput, includeSummariesInput]) {
  input.addEventListener("change", () => {
    updateControls();
    reloadPreviewForSettings();
  });
}
setInterval(() => api("/api/heartbeat", { method: "POST" }).catch(() => {}), 60_000);
loadChats();
