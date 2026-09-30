from __future__ import annotations

import json
import re
import shutil
import tempfile
import zipfile
from datetime import date, datetime, time, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from teams_exporter.teams_connector import AuthenticationRequiredError, ChatSummary
from teams_exporter.teams_exporter import TeamsExporter
from teams_exporter.teams_service import TeamsService


MAX_REQUEST_BYTES = 1_000_000
MAX_CHAT_CATALOG_SIZE = 10_000
MAX_MESSAGE_LIMIT = 1_000_000
DOWNLOAD_CHUNK_SIZE = 64 * 1024


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Teams conversation export</title>
  <style>
    :root { color-scheme: light; --teams-purple: #5b5fc7; --teams-purple-dark: #444791; --teams-purple-light: #e8ebfa; --teams-neutral: #f5f5f5; font: 15px/1.45 system-ui, sans-serif; color: #242424; background: var(--teams-neutral); }
    * { box-sizing: border-box; }
    body { margin: 0; min-height: 100vh; display: grid; place-items: center; padding: 28px 16px; }
    main { width: min(820px, 100%); padding: 28px; background: white; border: 1px solid #e1dfdd; border-radius: 10px; box-shadow: 0 12px 36px #1e293b12; }
    h1 { margin: 0; font-size: 1.6rem; letter-spacing: -.02em; }
    p { margin: 8px 0 0; color: #616161; }
    .eyebrow { margin: 0 0 6px; color: var(--teams-purple); font-size: .75rem; font-weight: 700; letter-spacing: .09em; text-transform: uppercase; }
    header { display: flex; align-items: flex-start; justify-content: space-between; gap: 20px; }
    .account-info { display: grid; justify-items: end; gap: 2px; padding-top: 3px; text-align: right; }
    .account-info strong { color: #242424; font-size: .9rem; font-weight: 700; }
    .account-info span { color: #737373; font-size: .78rem; font-weight: 400; }
    .filters { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin: 24px 0 18px; }
    label, .filter-field { display: grid; gap: 6px; color: #424242; font-weight: 600; }
    .field-label { color: #424242; font-weight: 600; }
    input[type="number"], input[type="search"], .range-toggle { width: 100%; min-height: 40px; border: 1px solid #c8c6c4; border-radius: 4px; padding: 8px 10px; color: inherit; font: inherit; background: white; }
    input:focus-visible, button:focus-visible { outline: 3px solid #bdbdf2; outline-offset: 2px; }
    input[type="checkbox"] { accent-color: var(--teams-purple); }
    .combine { grid-column: 1 / -1; display: flex; align-items: center; gap: 9px; font-weight: 500; }
    .combine input { width: 16px; height: 16px; margin: 0; }
    .list-heading { display: flex; align-items: end; justify-content: space-between; gap: 16px; margin: 18px 0 8px; }
    .list-heading label { flex: 1; max-width: 340px; }
    .count { color: #616161; white-space: nowrap; font-size: .9rem; }
    .chat-list { max-height: min(44vh, 390px); overflow: auto; border: 1px solid #e1dfdd; border-radius: 4px; }
    .chat-row { display: flex; grid-template-columns: auto 1fr; align-items: start; gap: 11px; padding: 11px 13px; border-bottom: 1px solid #f0f0f0; cursor: pointer; }
    .chat-row:last-child { border-bottom: 0; }
    .chat-row:hover { background: #f5f5ff; }
    .chat-row input { margin: 4px 0 0; }
    .chat-name { display: block; color: #242424; font-weight: 600; overflow-wrap: anywhere; }
    .chat-meta { display: block; margin-top: 2px; color: #737373; font-size: .82rem; }
    .empty { padding: 20px; color: #737373; text-align: center; }
    .database-notice { padding: 11px 12px; border-radius: 7px; background: #fff7e6; color: #594522; }
    .actions { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-top: 18px; }
    button { min-height: 40px; border: 0; border-radius: 4px; padding: 9px 15px; font: inherit; font-weight: 650; cursor: pointer; }
    button.primary { color: white; background: var(--teams-purple); }
    button.primary:hover:not(:disabled) { background: var(--teams-purple-dark); }
    button.secondary { color: #424242; background: #f0f0f0; }
    button:disabled { opacity: .52; cursor: not-allowed; }
    #status { min-height: 1.4em; color: #616161; }
    #status.error { color: #a83232; }
    .auth-help { margin-top: 12px; padding: 12px; border-radius: 7px; background: #fff7e6; color: #594522; }
    code { font-family: ui-monospace, Consolas, monospace; }
    .range-picker { position: relative; }
    .range-toggle { display: flex; align-items: center; justify-content: space-between; text-align: left; font-weight: 400; }
    .calendar { position: absolute; z-index: 5; top: calc(100% + 7px); left: 0; width: min(352px, calc(100vw - 72px)); padding: 14px; background: white; border: 1px solid #e1dfdd; border-radius: 8px; box-shadow: 0 10px 28px #24242424; }
    .calendar-header { display: grid; grid-template-columns: 36px 1fr 36px; align-items: center; gap: 8px; margin-bottom: 10px; text-align: center; }
    .calendar-header strong { font-size: .95rem; }
    .calendar-nav { min-height: 34px; padding: 4px; color: var(--teams-purple-dark); background: transparent; font-size: 1.2rem; }
    .weekdays, .calendar-days { display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); justify-items: center; }
    .weekdays { margin-bottom: 4px; color: #737373; font-size: .75rem; font-weight: 600; }
    .calendar-day { display: grid; place-items: center; width: 38px; height: 38px; min-height: 0; padding: 0; border-radius: 50%; color: #242424; background: transparent; font-weight: 400; }
    .calendar-day:hover { background: #f1f0ff; }
    .calendar-day.outside-month { color: #a19f9d; }
    .calendar-day.in-range { background: var(--teams-purple-light); border-radius: 0; }
    .calendar-day.range-start, .calendar-day.range-end { color: white; background: var(--teams-purple); font-weight: 650; }
    .calendar-footer { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-top: 12px; border-top: 1px solid #f0f0f0; padding-top: 10px; }
    .calendar-hint { flex: 1; color: #616161; font-size: .72rem; line-height: 1.3; }
    .calendar-footer button { min-height: 32px; padding: 6px 9px; font-size: .8rem; }
    @media (max-width: 560px) { main { padding: 20px; } header { gap: 12px; } .account-info { max-width: 42%; overflow-wrap: anywhere; } .filters { grid-template-columns: 1fr; } .combine { grid-column: auto; } .list-heading { align-items: stretch; flex-direction: column; } .list-heading label { max-width: none; } .actions { align-items: stretch; flex-direction: column-reverse; } .actions button { width: 100%; } }
  </style>
</head>
<body>
<main>
  <header>
    <div>
      <p class="eyebrow">Local Teams export</p>
      <h1>Download conversations</h1>
      <p>Choose one or more chats, set optional filters, and download a text export.</p>
    </div>
    <div id="account-info" class="account-info" aria-label="Signed-in account" hidden>
      <strong id="account-name"></strong>
      <span id="account-email"></span>
    </div>
  </header>
  <p id="database-notice" class="database-notice" role="status" hidden></p>
  <section class="filters" aria-label="Export filters">
    <div class="filter-field range-picker" id="range-picker">
      <span class="field-label">Message date range</span>
      <button id="range-toggle" class="range-toggle" type="button" aria-haspopup="dialog" aria-expanded="false">
        <span id="range-label">Any date</span><span aria-hidden="true">⌄</span>
      </button>
      <div id="calendar" class="calendar" role="dialog" aria-label="Choose an inclusive message date range" hidden>
        <div class="calendar-header">
          <button id="previous-month" class="calendar-nav" type="button" aria-label="Previous month">‹</button>
          <strong id="calendar-month"></strong>
          <button id="next-month" class="calendar-nav" type="button" aria-label="Next month">›</button>
        </div>
        <div class="weekdays" aria-hidden="true"><span>M</span><span>T</span><span>W</span><span>T</span><span>F</span><span>S</span><span>S</span></div>
        <div id="calendar-days" class="calendar-days"></div>
        <div class="calendar-footer">
          <button id="clear-dates" class="secondary" type="button">Clear</button>
          <span id="calendar-hint" class="calendar-hint">Click once for one day; click again to set the end date.</span>
          <button id="calendar-done" class="secondary" type="button">Done</button>
        </div>
      </div>
      <input id="from-date" type="hidden">
      <input id="to-date" type="hidden">
    </div>
    <label for="message-limit">Maximum messages per chat <input id="message-limit" type="number" min="1" step="1" placeholder="Unlimited"></label>
    <label class="combine"><input id="combine" type="checkbox" disabled><span>Combine selected conversations into one text file</span></label>
  </section>
  <section aria-label="Conversation selection">
    <div class="list-heading">
      <label for="chat-search">Find a conversation <input id="chat-search" type="search" placeholder="Chat name"></label>
      <span id="selection-count" class="count">0 selected</span>
    </div>
    <div id="chat-list" class="chat-list" aria-live="polite"><div class="empty">Loading conversations…</div></div>
  </section>
  <div class="actions">
    <p id="status" role="status" aria-live="polite"></p>
    <div>
      <button id="refresh" class="secondary" type="button">Refresh chats</button>
      <button id="download" class="primary" type="button" disabled>Download</button>
    </div>
  </div>
  <p id="auth-help" class="auth-help" hidden>Sign in separately from the project folder with <code>python -m teams_exporter auth</code>, then refresh this page.</p>
</main>
<script>
  const chats = [];
  const selected = new Set();
  const list = document.getElementById('chat-list');
  const search = document.getElementById('chat-search');
  const count = document.getElementById('selection-count');
  const downloadButton = document.getElementById('download');
  const combineInput = document.getElementById('combine');
  const statusText = document.getElementById('status');
  const authHelp = document.getElementById('auth-help');
  const databaseNotice = document.getElementById('database-notice');
  const accountInfo = document.getElementById('account-info');
  const calendar = document.getElementById('calendar');
  const calendarDays = document.getElementById('calendar-days');
  const calendarMonthLabel = document.getElementById('calendar-month');
  const rangeToggle = document.getElementById('range-toggle');
  const rangeLabel = document.getElementById('range-label');
  const fromDateInput = document.getElementById('from-date');
  const toDateInput = document.getElementById('to-date');
  let calendarMonth = new Date();
  calendarMonth.setDate(1);
  let choosingEnd = false;
  let rangeStartDraft = null;

  function dateKey(date) {
    return date.getFullYear() + '-' + String(date.getMonth() + 1).padStart(2, '0') + '-' + String(date.getDate()).padStart(2, '0');
  }

  function dateFromKey(value) {
    const parts = value.split('-').map(Number);
    return new Date(parts[0], parts[1] - 1, parts[2]);
  }

  function formatCalendarDate(value) {
    return dateFromKey(value).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
  }

  function updateRangeLabel() {
    if (!fromDateInput.value) {
      rangeLabel.textContent = 'Any date';
      return;
    }
    rangeLabel.textContent = fromDateInput.value === toDateInput.value
      ? formatCalendarDate(fromDateInput.value)
      : formatCalendarDate(fromDateInput.value) + ' – ' + formatCalendarDate(toDateInput.value);
  }

  function setDateRange(start, end) {
    fromDateInput.value = start || '';
    toDateInput.value = end || '';
    updateRangeLabel();
    renderCalendar();
  }

  function setCalendarOpen(open) {
    calendar.hidden = !open;
    rangeToggle.setAttribute('aria-expanded', String(open));
    if (open) {
      calendarMonth = fromDateInput.value ? dateFromKey(fromDateInput.value) : new Date();
      calendarMonth.setDate(1);
      choosingEnd = false;
      rangeStartDraft = null;
      renderCalendar();
    }
  }

  function renderCalendar() {
    calendarMonthLabel.textContent = calendarMonth.toLocaleDateString(undefined, { month: 'long', year: 'numeric' });
    document.getElementById('calendar-hint').textContent = choosingEnd
      ? 'Choose an end date, or press Done to keep one day.'
      : 'Click once for one day; click again to set the end date.';
    calendarDays.replaceChildren();
    const firstOfMonth = new Date(calendarMonth.getFullYear(), calendarMonth.getMonth(), 1);
    const mondayOffset = (firstOfMonth.getDay() + 6) % 7;
    const firstVisible = new Date(calendarMonth.getFullYear(), calendarMonth.getMonth(), 1 - mondayOffset);
    for (let index = 0; index < 42; index += 1) {
      const day = new Date(firstVisible.getFullYear(), firstVisible.getMonth(), firstVisible.getDate() + index);
      const key = dateKey(day);
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'calendar-day';
      button.textContent = String(day.getDate());
      button.setAttribute('aria-label', day.toLocaleDateString(undefined, { dateStyle: 'full' }));
      if (day.getMonth() !== calendarMonth.getMonth()) button.classList.add('outside-month');
      const start = fromDateInput.value;
      const end = toDateInput.value;
      if (start && end && key >= start && key <= end) button.classList.add('in-range');
      if (start && key === start) button.classList.add('range-start');
      if (end && key === end) button.classList.add('range-end');
      button.addEventListener('click', function () { chooseCalendarDate(key); });
      calendarDays.appendChild(button);
    }
  }

  function chooseCalendarDate(key) {
    if (!choosingEnd) {
      rangeStartDraft = key;
      choosingEnd = true;
      setDateRange(key, key);
      return;
    }
    if (key < rangeStartDraft) {
      rangeStartDraft = key;
      setDateRange(key, key);
      return;
    }
    setDateRange(rangeStartDraft, key);
    choosingEnd = false;
    setCalendarOpen(false);
  }

  function browserUtcOffsetMinutes(value, endOfDay) {
    const date = dateFromKey(value);
    const probe = new Date(
      date.getFullYear(), date.getMonth(), date.getDate(),
      endOfDay ? 23 : 0, endOfDay ? 59 : 0
    );
    return probe.getTimezoneOffset();
  }

  function setStatus(message, isError) {
    statusText.textContent = message;
    statusText.classList.toggle('error', Boolean(isError));
  }

  function updateControls() {
    count.textContent = selected.size + ' selected';
    downloadButton.disabled = selected.size === 0 || downloadButton.dataset.busy === 'true';
    combineInput.disabled = selected.size < 2 || downloadButton.dataset.busy === 'true';
    if (combineInput.disabled) combineInput.checked = false;
  }

  function renderChats() {
    const query = search.value.trim().toLocaleLowerCase();
    const visible = chats.filter(function (chat) {
      return !query || chat.name.toLocaleLowerCase().includes(query);
    });
    list.replaceChildren();
    if (!visible.length) {
      const empty = document.createElement('div');
      empty.className = 'empty';
      empty.textContent = chats.length ? 'No conversations match that filter.' : 'No conversations were found.';
      list.appendChild(empty);
      return;
    }
    visible.forEach(function (chat) {
      const row = document.createElement('label');
      row.className = 'chat-row';
      const checkbox = document.createElement('input');
      checkbox.type = 'checkbox';
      checkbox.checked = selected.has(chat.id);
      checkbox.addEventListener('change', function () {
        if (checkbox.checked) selected.add(chat.id);
        else selected.delete(chat.id);
        updateControls();
      });
      const text = document.createElement('span');
      const name = document.createElement('span');
      name.className = 'chat-name';
      name.textContent = chat.name;
      const metadata = document.createElement('span');
      metadata.className = 'chat-meta';
      const dateText = chat.last_message_at ? new Date(chat.last_message_at).toLocaleString() : 'No recent activity shown';
      metadata.textContent = (chat.chat_type || 'Conversation') + ' · ' + dateText;
      text.append(name, metadata);
      row.append(checkbox, text);
      list.appendChild(row);
    });
  }

  function showApiError(response, body) {
    if (response.status === 401 && body.error && body.error.code === 'authentication_required') {
      authHelp.hidden = false;
      setStatus('Teams sign-in is required.', true);
      return;
    }
    setStatus(body.error ? body.error.message : 'The request failed (' + response.status + ').', true);
  }

  async function loadChats() {
    authHelp.hidden = true;
    setStatus('Loading conversations…', false);
    document.getElementById('refresh').disabled = true;
    try {
      const response = await fetch('/api/chats', { cache: 'no-store' });
      const body = await response.json();
      if (!response.ok) {
        showApiError(response, body);
        return;
      }
      chats.splice(0, chats.length, ...body.chats);
      databaseNotice.textContent = body.database_notice || '';
      databaseNotice.hidden = !body.database_notice;
      document.getElementById('account-name').textContent = body.account?.name || '';
      document.getElementById('account-email').textContent = body.account?.email || '';
      accountInfo.hidden = !body.account;
      const known = new Set(chats.map(function (chat) { return chat.id; }));
      Array.from(selected).forEach(function (id) { if (!known.has(id)) selected.delete(id); });
      renderChats();
      updateControls();
      setStatus(chats.length + ' conversations loaded.', false);
    } catch (_) {
      setStatus('Could not reach the local server. Reload this page and try again.', true);
    } finally {
      document.getElementById('refresh').disabled = false;
    }
  }

  function downloadName(response) {
    const header = response.headers.get('Content-Disposition') || '';
    const encoded = header.match(/filename\*=UTF-8''([^;]+)/i);
    if (encoded) {
      try { return decodeURIComponent(encoded[1]); } catch (_) { return 'teams-export'; }
    }
    const plain = header.match(/filename="?([^";]+)"?/i);
    return plain ? plain[1] : 'teams-export';
  }

  async function downloadSelected() {
    if (!selected.size) return;
    authHelp.hidden = true;
    downloadButton.dataset.busy = 'true';
    downloadButton.textContent = 'Preparing…';
    updateControls();
    setStatus('Retrieving and formatting the selected conversations…', false);
    const limitText = document.getElementById('message-limit').value.trim();
    try {
      const response = await fetch('/api/download', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          chat_ids: Array.from(selected),
          from_date: fromDateInput.value || null,
          to_date: toDateInput.value || null,
          from_utc_offset_minutes: fromDateInput.value ? browserUtcOffsetMinutes(fromDateInput.value, false) : null,
          to_utc_offset_minutes: toDateInput.value ? browserUtcOffsetMinutes(toDateInput.value, true) : null,
          message_limit: limitText || null,
          concatenate: combineInput.checked
        })
      });
      if (!response.ok) {
        const body = await response.json();
        showApiError(response, body);
        return;
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = downloadName(response);
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
      setStatus('Download ready.', false);
    } catch (_) {
      setStatus('The export could not be completed. Check the Teams session and try again.', true);
    } finally {
      delete downloadButton.dataset.busy;
      downloadButton.textContent = 'Download';
      updateControls();
    }
  }

  search.addEventListener('input', renderChats);
  rangeToggle.addEventListener('click', function () { setCalendarOpen(calendar.hidden); });
  document.getElementById('previous-month').addEventListener('click', function () {
    calendarMonth.setMonth(calendarMonth.getMonth() - 1);
    renderCalendar();
  });
  document.getElementById('next-month').addEventListener('click', function () {
    calendarMonth.setMonth(calendarMonth.getMonth() + 1);
    renderCalendar();
  });
  document.getElementById('clear-dates').addEventListener('click', function () {
    choosingEnd = false;
    rangeStartDraft = null;
    setDateRange('', '');
    setCalendarOpen(false);
  });
  document.getElementById('calendar-done').addEventListener('click', function () { setCalendarOpen(false); });
  document.addEventListener('pointerdown', function (event) {
    if (!calendar.hidden && !document.getElementById('range-picker').contains(event.target)) setCalendarOpen(false);
  });
  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape' && !calendar.hidden) setCalendarOpen(false);
  });
  document.getElementById('refresh').addEventListener('click', loadChats);
  downloadButton.addEventListener('click', downloadSelected);
  loadChats();
</script>
</body>
</html>
"""


class LocalTeamsHTTPServer(HTTPServer):
    """Single-threaded loopback HTTP server for the local export page."""

    def __init__(self, server_address: tuple[str, int], service: TeamsService | None = None):
        self.service = service or TeamsService()
        self.exporter = TeamsExporter(self.service.connector, self.service.formatter)
        super().__init__(server_address, LocalTeamsRequestHandler)


class LocalTeamsRequestHandler(BaseHTTPRequestHandler):
    server: LocalTeamsHTTPServer
    server_version = "TeamsExporter"
    sys_version = ""

    def do_GET(self) -> None:
        if not self._request_is_local():
            self._send_json(403, self._error("local_only", "This server accepts local browser requests only."))
            return
        path = urlsplit(self.path).path
        if path == "/":
            body = INDEX_HTML.encode("utf-8")
            self.send_response(200)
            self._security_headers("text/html; charset=utf-8")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/chats":
            self._list_chats()
            return
        self._send_json(404, self._error("not_found", "Not found."))

    def do_POST(self) -> None:
        if not self._request_is_local():
            self._send_json(403, self._error("local_only", "This server accepts local browser requests only."))
            return
        if not self._origin_is_local():
            self._send_json(403, self._error("origin_rejected", "The request origin is not allowed."))
            return
        if urlsplit(self.path).path != "/api/download":
            self._send_json(404, self._error("not_found", "Not found."))
            return
        try:
            payload = self._read_json_body()
            with tempfile.TemporaryDirectory(prefix="teams-export-") as temp_dir:
                filename, content_type, output_path = self._build_download(payload, Path(temp_dir))
                self._send_file(output_path, filename, content_type)
        except AuthenticationRequiredError:
            self._send_json(401, self._error("authentication_required", "Teams sign-in is required."))
        except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._send_json(400, self._error("invalid_request", str(exc)))
        except Exception as exc:
            self._send_json(502, self._error("export_failed", f"Teams export failed ({type(exc).__name__})."))

    def log_message(self, format: str, *args: Any) -> None:
        # Request bodies and chat names can contain private information; keep the
        # simple local server quiet rather than writing access logs by default.
        return

    def _list_chats(self) -> None:
        try:
            chats: list[dict[str, Any]] = []
            page_token = None
            seen_tokens: set[str] = set()
            while True:
                result = self.server.service.list_chats(page_size=100, page_token=page_token)
                chats.extend(result["items"])
                if len(chats) > MAX_CHAT_CATALOG_SIZE:
                    raise RuntimeError("Chat catalog exceeds the local UI size limit.")
                next_token = result.get("next_page_token")
                if next_token is None:
                    break
                if not isinstance(next_token, str) or next_token in seen_tokens:
                    raise RuntimeError("Teams returned an invalid chat-list continuation.")
                seen_tokens.add(next_token)
                page_token = next_token
            self._send_json(
                200,
                {
                    "chats": chats,
                    "count": len(chats),
                    "database_notice": getattr(self.server.service, "database_notice", None),
                    "account": getattr(self.server.service, "account_info", None),
                },
            )
        except AuthenticationRequiredError:
            self._send_json(401, self._error("authentication_required", "Teams sign-in is required."))
        except Exception as exc:
            self._send_json(502, self._error("catalog_failed", f"Could not load chats ({type(exc).__name__})."))

    def _build_download(self, payload: Any, temp_dir: Path) -> tuple[str, str, Path]:
        if not isinstance(payload, dict):
            raise ValueError("The request body must be a JSON object.")
        chat_ids = payload.get("chat_ids")
        if not isinstance(chat_ids, list) or not chat_ids or any(
            not isinstance(chat_id, str) or not chat_id.strip() for chat_id in chat_ids
        ):
            raise ValueError("Select one or more conversations.")
        chat_ids = list(dict.fromkeys(chat_id.strip() for chat_id in chat_ids))

        from_date_value = payload.get("from_date")
        to_date_value = payload.get("to_date")
        since = self._parse_local_date(
            from_date_value,
            utc_offset_minutes=payload.get("from_utc_offset_minutes"),
        )
        until = self._parse_local_date(
            to_date_value,
            end_of_day=True,
            utc_offset_minutes=payload.get("to_utc_offset_minutes"),
        )
        if since is not None and until is not None and since > until:
            raise ValueError("from_date must be earlier than or equal to to_date.")
        limit = self._parse_message_limit(payload.get("message_limit"))
        concatenate = payload.get("concatenate", False)
        if not isinstance(concatenate, bool):
            raise ValueError("concatenate must be true or false.")

        chats = self._all_chats()
        by_id = {chat["id"]: chat for chat in chats}
        missing = [chat_id for chat_id in chat_ids if chat_id not in by_id]
        if missing:
            raise ValueError("One or more selected chats are no longer available. Refresh the list and try again.")
        selected_chats = [by_id[chat_id] for chat_id in chat_ids]

        if len(selected_chats) == 1:
            chat = self._summary(selected_chats[0])
            output_path = self.server.exporter.export_chat(
                chat,
                temp_dir / f"{self.server.exporter.filename_stem(chat)}.txt",
                limit=limit,
                since=since,
                until=until,
            )
            return output_path.name, "text/plain; charset=utf-8", output_path

        files_dir = temp_dir / "chats"
        files_dir.mkdir()
        exported = self.server.exporter.export_chats(
            [self._summary(chat) for chat in selected_chats],
            files_dir,
            limit=limit,
            since=since,
            until=until,
        )
        if concatenate:
            combined_path = temp_dir / "conversations.txt"
            with combined_path.open("wb") as combined:
                for index, path in enumerate(exported):
                    if index:
                        combined.write(b"\n")
                    with path.open("rb") as source:
                        shutil.copyfileobj(source, combined, DOWNLOAD_CHUNK_SIZE)
            return combined_path.name, "text/plain; charset=utf-8", combined_path

        archive_path = temp_dir / "teams-conversations.zip"
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in exported:
                archive.write(path, arcname=path.name)
        return archive_path.name, "application/zip", archive_path

    def _all_chats(self) -> list[dict[str, Any]]:
        chats: list[dict[str, Any]] = []
        page_token = None
        seen_tokens: set[str] = set()
        while True:
            result = self.server.service.list_chats(page_size=100, page_token=page_token)
            chats.extend(result["items"])
            if len(chats) > MAX_CHAT_CATALOG_SIZE:
                raise ValueError("Chat catalog exceeds the local UI size limit.")
            next_token = result.get("next_page_token")
            if next_token is None:
                return chats
            if not isinstance(next_token, str) or next_token in seen_tokens:
                raise RuntimeError("Teams returned an invalid chat-list continuation.")
            seen_tokens.add(next_token)
            page_token = next_token

    @staticmethod
    def _summary(record: dict[str, Any]) -> ChatSummary:
        timestamp = record.get("last_message_at")
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00")) if timestamp else None
        return ChatSummary(
            id=record["id"],
            display_name=record["name"],
            last_message_at=parsed,
            chat_type=record.get("chat_type"),
        )

    @staticmethod
    def _parse_local_date(
        value: Any,
        *,
        end_of_day: bool = False,
        utc_offset_minutes: Any = None,
    ) -> datetime | None:
        field_name = "to_date" if end_of_day else "from_date"
        if value in (None, ""):
            return None
        if not isinstance(value, str):
            raise ValueError(f"{field_name} must be a calendar date.")
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"{field_name} must use YYYY-MM-DD format.") from exc
        if utc_offset_minutes is None:
            local_time = time.max if end_of_day else time.min
            return datetime.combine(parsed, local_time).astimezone()
        if (
            isinstance(utc_offset_minutes, bool)
            or not isinstance(utc_offset_minutes, int)
            or not -840 <= utc_offset_minutes <= 840
        ):
            raise ValueError("UTC offset must be a whole number of minutes from -840 to 840.")
        local_offset = timezone(timedelta(minutes=-utc_offset_minutes))
        local_time = time.max if end_of_day else time.min
        return datetime.combine(parsed, local_time, tzinfo=local_offset)

    @staticmethod
    def _parse_message_limit(value: Any) -> int | None:
        if value in (None, ""):
            return None
        if isinstance(value, bool):
            raise ValueError("message_limit must be a positive whole number or blank.")
        if isinstance(value, str) and re.fullmatch(r"[0-9]+", value.strip()):
            value = int(value.strip())
        if not isinstance(value, int) or value < 1 or value > MAX_MESSAGE_LIMIT:
            raise ValueError(f"message_limit must be between 1 and {MAX_MESSAGE_LIMIT}, or blank.")
        return value

    def _read_json_body(self) -> Any:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise ValueError("Content-Type must be application/json.")
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError as exc:
            raise ValueError("Content-Length is required.") from exc
        if length < 0:
            raise ValueError("Content-Length must not be negative.")
        if length > MAX_REQUEST_BYTES:
            raise ValueError("Request body is too large.")
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def _request_is_local(self) -> bool:
        host = self.headers.get("Host", "")
        try:
            parsed = urlsplit("//" + host)
            return (
                parsed.hostname in {"127.0.0.1", "localhost"}
                and parsed.port in {None, self.server.server_port}
                and not parsed.username
                and not parsed.password
            )
        except ValueError:
            return False

    def _origin_is_local(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        try:
            parsed = urlsplit(origin)
            return (
                parsed.scheme == "http"
                and parsed.hostname in {"127.0.0.1", "localhost"}
                and parsed.port == self.server.server_port
                and not parsed.username
                and not parsed.password
            )
        except ValueError:
            return False

    def _send_file(self, path: Path, filename: str, content_type: str) -> None:
        fallback = re.sub(r"[^A-Za-z0-9._-]", "_", filename) or "teams-export"
        disposition = (
            f'attachment; filename="{fallback}"; '
            f"filename*=UTF-8''{quote(filename, safe='')}"
        )
        self.send_response(200)
        self._security_headers(content_type)
        self.send_header("Content-Disposition", disposition)
        self.send_header("Content-Length", str(path.stat().st_size))
        self.end_headers()
        with path.open("rb") as source:
            shutil.copyfileobj(source, self.wfile, DOWNLOAD_CHUNK_SIZE)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._security_headers("application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _security_headers(self, content_type: str) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")

    @staticmethod
    def _error(code: str, message: str) -> dict[str, Any]:
        return {"error": {"code": code, "message": message}}


def serve_local_ui(port: int = 8765) -> None:
    """Serve the local export UI on loopback until interrupted."""
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535.")
    server = LocalTeamsHTTPServer(("127.0.0.1", port))
    print(f"Teams export UI: http://127.0.0.1:{server.server_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
