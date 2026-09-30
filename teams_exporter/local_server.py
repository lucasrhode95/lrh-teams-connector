from __future__ import annotations

import json
import re
import shutil
import tempfile
import zipfile
import threading
import time as time_module
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

from teams_exporter.teams_connector import (
    AuthenticationRequiredError,
    ChatSummary,
    ExtractionCancelled,
)
from teams_exporter.teams_exporter import TeamsExporter
from teams_exporter.teams_service import TeamsService


MAX_REQUEST_BYTES = 1_000_000
MAX_CHAT_CATALOG_SIZE = 10_000
MAX_MESSAGE_LIMIT = 1_000_000
DOWNLOAD_CHUNK_SIZE = 64 * 1024
LOCAL_IMAGE_ASSETS = {
    "avatar-person.svg",
    "chat-call.svg",
    "chat-group.svg",
}


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Teams conversation export</title>
  <script>
    document.documentElement.dataset.theme = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  </script>
  <style>
    :root { color-scheme: light; --teams-purple: #5b5fc7; --teams-purple-dark: #444791; --teams-purple-light: #e8ebfa; --page-bg: #f5f5f5; --surface: #fff; --surface-border: #e1dfdd; --text: #242424; --muted: #616161; --field-border: #c8c6c4; --line: #f0f0f0; --hover: #f5f5ff; --focus: #bdbdf2; --selection: #e8ebfa; --button-bg: #f0f0f0; --notice-bg: #fff7e6; --notice-text: #594522; --error: #a83232; --avatar-bg: #e8ebfa; --shadow: 0 12px 36px #1e293b12; font: 15px/1.45 system-ui, sans-serif; color: var(--text); background: var(--page-bg); }
    :root[data-theme="dark"] { color-scheme: dark; --teams-purple: #898cf5; --teams-purple-dark: #a7a9f7; --teams-purple-light: #39395c; --page-bg: #141414; --surface: #1f1f1f; --surface-border: #3b3a39; --text: #f5f5f5; --muted: #b3b0ad; --field-border: #5c5a58; --line: #333; --hover: #2b2b38; --focus: #8b8ef4; --selection: #373764; --button-bg: #333; --notice-bg: #3a3426; --notice-text: #f2dca6; --error: #f17b7b; --avatar-bg: #34355f; --shadow: 0 12px 36px #0008; }
    * { box-sizing: border-box; }
    body { margin: 0; min-height: 100vh; display: grid; place-items: center; padding: 28px 16px; background: var(--page-bg); color: var(--text); }
    main { width: min(820px, 100%); padding: 28px; background: var(--surface); border: 1px solid var(--surface-border); border-radius: 10px; box-shadow: var(--shadow); }
    h1 { margin: 0; font-size: 1.6rem; letter-spacing: -.02em; }
    p { margin: 8px 0 0; color: var(--muted); }
    .eyebrow { margin: 0 0 6px; color: var(--teams-purple); font-size: .75rem; font-weight: 700; letter-spacing: .09em; text-transform: uppercase; }
    header { display: flex; align-items: flex-start; justify-content: space-between; gap: 20px; }
    .account-info { display: flex; align-items: center; gap: 8px; padding-top: 3px; text-align: left; }
    .account-copy { display: grid; gap: 2px; }
    .account-info strong { color: var(--text); font-size: .9rem; font-weight: 700; }
    .account-info .account-email { color: var(--muted); font-size: .78rem; font-weight: 400; }
    .account-avatar { width: 40px; height: 40px; flex: 0 0 40px; border: 1px solid var(--surface-border); border-radius: 50%; object-fit: cover; background: var(--avatar-bg); }
    .filters { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin: 24px 0 18px; }
    label, .filter-field { display: grid; gap: 6px; color: var(--text); font-weight: 600; }
    .field-label { color: var(--text); font-weight: 600; }
    input[type="number"], input[type="search"], .range-toggle { width: 100%; min-height: 40px; border: 1px solid var(--field-border); border-radius: 4px; padding: 8px 10px; color: var(--text); font: inherit; background: var(--surface); }
    input::placeholder { color: var(--muted); opacity: 1; }
    input:focus-visible, button:focus-visible { outline: 3px solid var(--focus); outline-offset: 2px; }
    input[type="checkbox"] { accent-color: var(--teams-purple); }
    .combine { grid-column: 1 / -1; display: flex; align-items: center; gap: 9px; font-weight: 500; }
    .combine input { width: 16px; height: 16px; margin: 0; }
    .list-heading { display: flex; align-items: end; justify-content: space-between; gap: 16px; margin: 18px 0 8px; }
    .list-heading label { flex: 1; max-width: 340px; }
    .count { color: var(--muted); white-space: nowrap; font-size: .9rem; }
    .chat-list { max-height: min(44vh, 390px); overflow: auto; border: 1px solid var(--surface-border); border-radius: 4px; }
    .chat-row { display: grid; grid-template-columns: auto 36px minmax(0, 1fr); align-items: center; gap: 11px; padding: 10px 13px; border-bottom: 1px solid var(--line); cursor: pointer; }
    .chat-row:last-child { border-bottom: 0; }
    .chat-row:hover { background: var(--hover); }
    .chat-row input { margin: 4px 0 0; }
    .chat-avatar { width: 36px; height: 36px; border: 1px solid var(--surface-border); border-radius: 50%; object-fit: cover; background: var(--avatar-bg); }
    .chat-name { display: block; color: var(--text); font-weight: 600; overflow-wrap: anywhere; }
    .chat-meta { display: block; margin-top: 2px; color: var(--muted); font-size: .82rem; }
    .empty { padding: 20px; color: var(--muted); text-align: center; }
    .database-notice { padding: 11px 12px; border-radius: 7px; background: var(--notice-bg); color: var(--notice-text); }
    .actions { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-top: 18px; }
    .action-buttons { display: flex; align-items: center; gap: 8px; }
    button { min-height: 40px; border: 0; border-radius: 4px; padding: 9px 15px; font: inherit; font-weight: 650; cursor: pointer; }
    button.primary { color: white; background: var(--teams-purple); }
    button.primary:hover:not(:disabled) { background: var(--teams-purple-dark); }
    button.secondary { color: var(--text); background: var(--button-bg); }
    button:disabled { opacity: .52; cursor: not-allowed; }
    #status { min-height: 1.4em; color: var(--muted); }
    #status.error { color: var(--error); }
    .auth-help { margin-top: 12px; padding: 12px; border-radius: 7px; background: var(--notice-bg); color: var(--notice-text); }
    code { font-family: ui-monospace, Consolas, monospace; }
    .range-picker { position: relative; }
    .range-toggle { display: flex; align-items: center; justify-content: space-between; text-align: left; font-weight: 400; }
    .calendar { position: absolute; z-index: 5; top: calc(100% + 7px); left: 0; width: min(352px, calc(100vw - 72px)); padding: 14px; background: var(--surface); border: 1px solid var(--surface-border); border-radius: 8px; box-shadow: var(--shadow); }
    .calendar-header { display: grid; grid-template-columns: 36px 1fr 36px; align-items: center; gap: 8px; margin-bottom: 10px; text-align: center; }
    .calendar-header strong { font-size: .95rem; }
    .calendar-nav { min-height: 34px; padding: 4px; color: var(--teams-purple-dark); background: transparent; font-size: 1.2rem; }
    .weekdays, .calendar-days { display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); justify-items: center; }
    .weekdays { margin-bottom: 4px; color: var(--muted); font-size: .75rem; font-weight: 600; }
    .calendar-day { display: grid; place-items: center; width: 38px; height: 38px; min-height: 0; padding: 0; border-radius: 50%; color: var(--text); background: transparent; font-weight: 400; }
    .calendar-day:hover { background: var(--hover); }
    .calendar-day.outside-month { color: var(--muted); opacity: .65; }
    .calendar-day.in-range { background: var(--teams-purple-light); border-radius: 0; }
    .calendar-day.range-start, .calendar-day.range-end { color: white; background: var(--teams-purple); font-weight: 650; }
    .calendar-footer { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-top: 12px; border-top: 1px solid var(--line); padding-top: 10px; }
    .calendar-hint { flex: 1; color: var(--muted); font-size: .72rem; line-height: 1.3; }
    .calendar-footer button { min-height: 32px; padding: 6px 9px; font-size: .8rem; }
    @media (max-width: 560px) { main { padding: 20px; } header { gap: 12px; } .account-info { max-width: 48%; overflow-wrap: anywhere; } .account-avatar { width: 34px; height: 34px; flex-basis: 34px; } .filters { grid-template-columns: 1fr; } .combine { grid-column: auto; } .list-heading { align-items: stretch; flex-direction: column; } .list-heading label { max-width: none; } .actions { align-items: stretch; flex-direction: column-reverse; } .actions button { width: 100%; } }
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
      <img id="account-picture" class="account-avatar" alt="">
      <span class="account-copy">
        <strong id="account-name"></strong>
        <span id="account-email" class="account-email"></span>
      </span>
    </div>
  </header>
  <p id="database-notice" class="database-notice" role="status" hidden></p>
  <section class="filters" aria-label="Export filters">
    <div class="filter-field range-picker" id="range-picker">
      <span class="field-label">Message date range</span>
      <button id="range-toggle" class="range-toggle" type="button" aria-haspopup="dialog" aria-expanded="false">
        <span id="range-label">Unlimited</span><span aria-hidden="true">⌄</span>
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
          <button id="clear-dates" class="secondary" type="button" aria-label="Clear date range (unlimited)">Clear</button>
          <span id="calendar-hint" class="calendar-hint">Click once for one day; click again to set the end date.</span>
          <button id="calendar-done" class="secondary" type="button">Done</button>
        </div>
      </div>
      <input id="from-date" type="hidden">
      <input id="to-date" type="hidden">
    </div>
    <label for="message-limit">Maximum messages per chat <input id="message-limit" type="number" min="1" max="1000000" step="1" placeholder="Unlimited"></label>
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
    <div class="action-buttons">
      <button id="refresh" class="secondary" type="button">Refresh chats</button>
      <button id="cancel-export" class="secondary" type="button" hidden>Cancel export</button>
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
  const messageLimitInput = document.getElementById('message-limit');
  const count = document.getElementById('selection-count');
  const downloadButton = document.getElementById('download');
  const cancelButton = document.getElementById('cancel-export');
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
  const fallbackPersonAvatar = '/assets/avatar-person.svg';
  let calendarMonth = new Date();
  calendarMonth.setDate(1);
  let choosingEnd = false;
  let rangeStartDraft = null;
  let activeDownloadId = null;

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
      rangeLabel.textContent = 'Unlimited';
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

  function setAvatar(image, url, fallback) {
    let fallbackApplied = false;
    let fallbackTimer = null;
    function showFallback() {
      if (fallbackApplied) return;
      fallbackApplied = true;
      if (fallbackTimer !== null) window.clearTimeout(fallbackTimer);
      image.src = fallback;
    }
    image.onload = function () {
      if (fallbackTimer !== null) window.clearTimeout(fallbackTimer);
    };
    image.onerror = function () {
      showFallback();
    };
    let source = fallback;
    if (typeof url === 'string' && url.length <= 2048) {
      try {
        const parsed = new URL(url, window.location.origin);
        if (parsed.protocol === 'https:' || parsed.origin === window.location.origin) source = parsed.href;
      } catch (_) {}
    }
    if (source !== fallback) {
      fallbackTimer = window.setTimeout(showFallback, 5000);
    }
    image.src = source;
  }

  function chatFallbackAvatar(chat) {
    const type = String(chat.chat_type || '').toLocaleLowerCase();
    const id = String(chat.id || '').toLocaleLowerCase();
    if (type.includes('meeting') || type.includes('call') || id.startsWith('19:meeting_') || id.startsWith('19:call_')) {
      return '/assets/chat-call.svg';
    }
    if (type.includes('group')) return '/assets/chat-group.svg';
    return fallbackPersonAvatar;
  }

  function updateControls() {
    count.textContent = selected.size + ' selected';
    downloadButton.disabled = selected.size === 0 || downloadButton.dataset.busy === 'true';
    cancelButton.hidden = downloadButton.dataset.busy !== 'true';
    cancelButton.disabled = !activeDownloadId;
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
      const avatar = document.createElement('img');
      avatar.className = 'chat-avatar';
      avatar.alt = '';
      avatar.setAttribute('aria-hidden', 'true');
      setAvatar(avatar, chat.avatar_url, chatFallbackAvatar(chat));
      const text = document.createElement('span');
      const name = document.createElement('span');
      name.className = 'chat-name';
      name.textContent = chat.name;
      const metadata = document.createElement('span');
      metadata.className = 'chat-meta';
      const dateText = chat.last_message_at ? new Date(chat.last_message_at).toLocaleString() : 'No recent activity shown';
      metadata.textContent = (chat.chat_type || 'Conversation') + ' · ' + dateText;
      text.append(name, metadata);
      row.append(checkbox, avatar, text);
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
      setAvatar(document.getElementById('account-picture'), body.account?.picture_url, fallbackPersonAvatar);
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

  function getMessageLimit() {
    const limitText = messageLimitInput.value.trim();
    if (
      messageLimitInput.validity.badInput ||
      messageLimitInput.validity.stepMismatch ||
      messageLimitInput.validity.rangeUnderflow ||
      messageLimitInput.validity.rangeOverflow
    ) {
      return { valid: false, value: null };
    }
    if (!limitText) return { valid: true, value: null };
    if (!/^[0-9]+$/.test(limitText)) return { valid: false, value: null };
    const value = Number(limitText);
    return {
      valid: Number.isSafeInteger(value) && value >= 1 && value <= 1000000,
      value: value
    };
  }

  function wait(milliseconds) {
    return new Promise(function (resolve) { window.setTimeout(resolve, milliseconds); });
  }

  function describeProgress(progress) {
    if (!progress) return 'Export is running…';
    const parts = [];
    if (progress.chat_index && progress.chats_total) {
      parts.push('chat ' + progress.chat_index + ' of ' + progress.chats_total);
    }
    if (progress.pages_fetched) parts.push(progress.pages_fetched + ' pages');
    if (progress.messages_retrieved) parts.push(progress.messages_retrieved + ' messages retrieved');
    if (progress.phase === 'formatting' && progress.messages_written) {
      parts.push(progress.messages_written + ' messages written');
    }
    if (progress.retry_count) parts.push(progress.retry_count + ' retries');
    return (progress.phase === 'formatting' ? 'Formatting' : 'Retrieving') +
      (parts.length ? ' · ' + parts.join(' · ') : '…');
  }

  async function downloadSelected() {
    if (!selected.size) return;
    const parsedLimit = getMessageLimit();
    if (!parsedLimit.valid) {
      messageLimitInput.setCustomValidity(
        'Enter a positive whole number, or leave the field blank for unlimited.'
      );
      messageLimitInput.reportValidity();
      setStatus('Maximum messages per chat must be a positive whole number or blank.', true);
      return;
    }
    messageLimitInput.setCustomValidity('');
    authHelp.hidden = true;
    downloadButton.dataset.busy = 'true';
    downloadButton.textContent = 'Preparing…';
    updateControls();
    setStatus('Retrieving and formatting the selected conversations…', false);
    try {
      const startResponse = await fetch('/api/download', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          chat_ids: Array.from(selected),
          from_date: fromDateInput.value || null,
          to_date: toDateInput.value || null,
          from_utc_offset_minutes: fromDateInput.value ? browserUtcOffsetMinutes(fromDateInput.value, false) : null,
          to_utc_offset_minutes: toDateInput.value ? browserUtcOffsetMinutes(toDateInput.value, true) : null,
          message_limit: parsedLimit.value,
          concatenate: combineInput.checked
        })
      });
      const startBody = await startResponse.json();
      if (!startResponse.ok) {
        showApiError(startResponse, startBody);
        return;
      }
      activeDownloadId = startBody.request_id;
      updateControls();
      let job;
      while (activeDownloadId === startBody.request_id) {
        const statusResponse = await fetch(
          '/api/download-status?request_id=' + encodeURIComponent(startBody.request_id),
          { cache: 'no-store' }
        );
        job = await statusResponse.json();
        if (!statusResponse.ok) {
          showApiError(statusResponse, job);
          return;
        }
        if (job.status === 'complete') break;
        if (job.status === 'cancelled') {
          setStatus('Export stopped. ' + (job.progress?.pages_fetched || 0) + ' pages had completed.', false);
          return;
        }
        if (job.status === 'failed') {
          if (job.error?.code === 'authentication_required') authHelp.hidden = false;
          setStatus(job.error?.message || 'The export failed.', true);
          return;
        }
        setStatus(describeProgress(job.progress), false);
        await wait(500);
      }
      if (!job || job.status !== 'complete') return;
      setStatus('Preparing the download…', false);
      const response = await fetch(
        '/api/download-file?request_id=' + encodeURIComponent(startBody.request_id),
        { cache: 'no-store' }
      );
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
      activeDownloadId = null;
      updateControls();
    }
  }

  async function cancelDownload() {
    if (!activeDownloadId) return;
    cancelButton.disabled = true;
    setStatus('Requesting cancellation…', false);
    try {
      const response = await fetch('/api/cancel', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ request_id: activeDownloadId })
      });
      const body = await response.json();
      if (!response.ok) showApiError(response, body);
      else setStatus('Stopping after the current network request or page.', false);
    } catch (_) {
      setStatus('Could not reach the local server to cancel the export.', true);
      cancelButton.disabled = false;
    }
  }

  search.addEventListener('input', renderChats);
  messageLimitInput.addEventListener('input', function () {
    const parsedLimit = getMessageLimit();
    messageLimitInput.setCustomValidity(
      parsedLimit.valid ? '' : 'Enter a positive whole number, or leave the field blank for unlimited.'
    );
  });
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
  cancelButton.addEventListener('click', cancelDownload);
  loadChats();
</script>
</body>
</html>
"""


@dataclass(frozen=True)
class DownloadRequest:
    chats: list[dict[str, Any]]
    since: datetime | None
    until: datetime | None
    limit: int | None
    concatenate: bool


@dataclass
class DownloadJob:
    request_id: str
    cancel_event: threading.Event = field(default_factory=threading.Event)
    finished_at: float | None = None
    status: str = "queued"
    progress: dict[str, Any] = field(default_factory=dict)
    error: dict[str, str] | None = None
    filename: str | None = None
    content_type: str | None = None
    output_path: Path | None = None
    artifact_directory: Path | None = None


class LocalTeamsHTTPServer(ThreadingHTTPServer):
    """Loopback server with background, observable export jobs."""

    daemon_threads = True
    block_on_close = False
    # HTTPServer enables SO_REUSEADDR by default. On Windows that can let two
    # copies of this local app bind the same loopback port, so requests may
    # reach an older process even after the user starts a new one.
    allow_reuse_address = False
    JOB_RETENTION_SECONDS = 60 * 60

    def __init__(self, server_address: tuple[str, int], service: TeamsService | None = None):
        self.service = service or TeamsService()
        self.exporter = TeamsExporter(
            self.service.connector,
            self.service.formatter,
            service=self.service,
        )
        self._jobs: dict[str, DownloadJob] = {}
        self._jobs_lock = threading.RLock()
        super().__init__(server_address, LocalTeamsRequestHandler)

    def start_download(self, request: DownloadRequest) -> DownloadJob:
        with self._jobs_lock:
            self._prune_jobs_locked()
            if any(job.status in {"queued", "running"} for job in self._jobs.values()):
                raise RuntimeError("Another export is already running. Wait for it or cancel it first.")
            job = DownloadJob(request_id=uuid.uuid4().hex)
            self._jobs[job.request_id] = job
            worker = threading.Thread(
                target=self._run_download,
                args=(job, request),
                name=f"teams-export-{job.request_id[:8]}",
                daemon=True,
            )
            worker.start()
            return job

    def get_download_job(self, request_id: str) -> DownloadJob | None:
        with self._jobs_lock:
            self._prune_jobs_locked()
            return self._jobs.get(request_id)

    def _run_download(self, job: DownloadJob, request: DownloadRequest) -> None:
        artifact_directory = Path(tempfile.mkdtemp(prefix="teams-export-job-"))
        with self._jobs_lock:
            job.artifact_directory = artifact_directory
            job.status = "running"

        def report_progress(update: dict[str, Any]) -> None:
            with self._jobs_lock:
                if job.status in {"queued", "running"}:
                    job.progress = {
                        key: value
                        for key, value in update.items()
                        if key in {
                            "phase",
                            "chat_index",
                            "chats_total",
                            "pages_fetched",
                            "messages_retrieved",
                            "messages_written",
                            "retry_count",
                        }
                    }

        try:
            filename, content_type, output_path = self._build_download(
                request,
                artifact_directory,
                cancel_event=job.cancel_event,
                progress_callback=report_progress,
            )
            if job.cancel_event.is_set():
                raise ExtractionCancelled("Export cancelled.")
            with self._jobs_lock:
                job.filename = filename
                job.content_type = content_type
                job.output_path = output_path
                job.status = "complete"
                job.finished_at = time_module.monotonic()
        except ExtractionCancelled:
            with self._jobs_lock:
                job.status = "cancelled"
                job.finished_at = time_module.monotonic()
            shutil.rmtree(artifact_directory, ignore_errors=True)
        except AuthenticationRequiredError as exc:
            with self._jobs_lock:
                job.status = "failed"
                job.error = {
                    "code": "authentication_required",
                    "message": str(exc),
                }
                job.finished_at = time_module.monotonic()
            shutil.rmtree(artifact_directory, ignore_errors=True)
        except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            with self._jobs_lock:
                job.status = "failed"
                job.error = {"code": "invalid_request", "message": str(exc)}
                job.finished_at = time_module.monotonic()
            shutil.rmtree(artifact_directory, ignore_errors=True)
        except Exception as exc:
            with self._jobs_lock:
                job.status = "failed"
                job.error = {
                    "code": "export_failed",
                    "message": f"Teams export failed: {TeamsService._safe_error(exc)}",
                }
                job.finished_at = time_module.monotonic()
            shutil.rmtree(artifact_directory, ignore_errors=True)

    def _build_download(
        self,
        request: DownloadRequest,
        temp_dir: Path,
        *,
        cancel_event: threading.Event,
        progress_callback,
    ) -> tuple[str, str, Path]:
        self._raise_if_cancelled(cancel_event)
        if len(request.chats) == 1:
            chat = LocalTeamsRequestHandler._summary(request.chats[0])
            output_path = self.exporter.export_chat(
                chat,
                temp_dir / f"{self.exporter.filename_stem(chat)}.txt",
                limit=request.limit,
                since=request.since,
                until=request.until,
                cancel_event=cancel_event,
                progress_callback=progress_callback,
            )
            return output_path.name, "text/plain; charset=utf-8", output_path

        files_dir = temp_dir / "chats"
        files_dir.mkdir()
        exported = self.exporter.export_chats(
            [LocalTeamsRequestHandler._summary(chat) for chat in request.chats],
            files_dir,
            limit=request.limit,
            since=request.since,
            until=request.until,
            cancel_event=cancel_event,
            progress_callback=progress_callback,
        )
        self._raise_if_cancelled(cancel_event)
        if request.concatenate:
            combined_path = temp_dir / "conversations.txt"
            with combined_path.open("wb") as combined:
                for index, path in enumerate(exported):
                    self._raise_if_cancelled(cancel_event)
                    if index:
                        combined.write(b"\n")
                    with path.open("rb") as source:
                        while chunk := source.read(DOWNLOAD_CHUNK_SIZE):
                            self._raise_if_cancelled(cancel_event)
                            combined.write(chunk)
            return combined_path.name, "text/plain; charset=utf-8", combined_path

        archive_path = temp_dir / "teams-conversations.zip"
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in exported:
                self._raise_if_cancelled(cancel_event)
                with path.open("rb") as source, archive.open(path.name, "w") as target:
                    while chunk := source.read(DOWNLOAD_CHUNK_SIZE):
                        self._raise_if_cancelled(cancel_event)
                        target.write(chunk)
        return archive_path.name, "application/zip", archive_path

    @staticmethod
    def _raise_if_cancelled(cancel_event: threading.Event) -> None:
        if cancel_event.is_set():
            raise ExtractionCancelled("Export cancelled.")

    def _prune_jobs_locked(self) -> None:
        now = time_module.monotonic()
        for request_id, job in list(self._jobs.items()):
            if (
                job.status not in {"queued", "running"}
                and job.finished_at is not None
                and now - job.finished_at > self.JOB_RETENTION_SECONDS
            ):
                if job.artifact_directory is not None:
                    shutil.rmtree(job.artifact_directory, ignore_errors=True)
                del self._jobs[request_id]


class LocalTeamsRequestHandler(BaseHTTPRequestHandler):
    server: LocalTeamsHTTPServer
    server_version = "TeamsExporter"
    sys_version = ""

    def do_GET(self) -> None:
        if not self._request_is_local():
            self._send_json(403, self._error("local_only", "This server accepts local browser requests only."))
            return
        parsed_target = urlsplit(self.path)
        path = parsed_target.path
        if path == "/":
            body = INDEX_HTML.encode("utf-8")
            self.send_response(200)
            self._security_headers("text/html; charset=utf-8")
            self.send_header("Content-Security-Policy", "default-src 'none'; img-src 'self' https:; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/chats":
            self._list_chats()
            return
        if path == "/api/download-status":
            request_id = self._query_request_id(parsed_target.query)
            if request_id is None:
                self._send_json(400, self._error("invalid_request", "A valid request_id is required."))
                return
            job = self.server.get_download_job(request_id)
            if job is None:
                self._send_json(404, self._error("not_found", "The export request was not found."))
                return
            with self.server._jobs_lock:
                result = {
                    "request_id": job.request_id,
                    "status": job.status,
                    "progress": dict(job.progress),
                    "download_ready": job.status == "complete",
                    "error": dict(job.error) if job.error else None,
                }
            self._send_json(200, result)
            return
        if path == "/api/download-file":
            request_id = self._query_request_id(parsed_target.query)
            if request_id is None:
                self._send_json(400, self._error("invalid_request", "A valid request_id is required."))
                return
            job = self.server.get_download_job(request_id)
            if job is None:
                self._send_json(404, self._error("not_found", "The export request was not found."))
                return
            with self.server._jobs_lock:
                if job.status != "complete" or job.output_path is None or job.filename is None:
                    self._send_json(409, self._error("export_not_ready", "The export file is not ready."))
                    return
                path_to_send = job.output_path
                filename = job.filename
                content_type = job.content_type or "application/octet-stream"
            if not path_to_send.is_file():
                self._send_json(410, self._error("export_expired", "The export file is no longer available."))
                return
            self._send_file(path_to_send, filename, content_type)
            return
        if path.startswith("/assets/"):
            asset_name = path.removeprefix("/assets/")
            if asset_name not in LOCAL_IMAGE_ASSETS:
                self._send_json(404, self._error("not_found", "Not found."))
                return
            asset_path = Path(__file__).parent / asset_name
            try:
                body = asset_path.read_bytes()
            except OSError:
                self._send_json(404, self._error("not_found", "Not found."))
                return
            self.send_response(200)
            self._security_headers("image/svg+xml; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._send_json(404, self._error("not_found", "Not found."))

    def do_POST(self) -> None:
        if not self._request_is_local():
            self._send_json(403, self._error("local_only", "This server accepts local browser requests only."))
            return
        if not self._origin_is_local():
            self._send_json(403, self._error("origin_rejected", "The request origin is not allowed."))
            return
        path = urlsplit(self.path).path
        if path not in {"/api/download", "/api/cancel"}:
            self._send_json(404, self._error("not_found", "Not found."))
            return
        try:
            payload = self._read_json_body()
            if path == "/api/cancel":
                if not isinstance(payload, dict):
                    raise ValueError("The request body must be a JSON object.")
                request_id = payload.get("request_id")
                if not self._valid_request_id(request_id):
                    raise ValueError("A valid request_id is required.")
                job = self.server.get_download_job(request_id)
                if job is None:
                    self._send_json(404, self._error("not_found", "The export request was not found."))
                    return
                with self.server._jobs_lock:
                    active = job.status in {"queued", "running"}
                    if active:
                        job.cancel_event.set()
                self._send_json(
                    202 if active else 200,
                    {"request_id": request_id, "cancellation_requested": active, "status": job.status},
                )
                return

            request = self._prepare_download(payload)
            try:
                job = self.server.start_download(request)
            except RuntimeError as exc:
                self._send_json(409, self._error("export_busy", str(exc)))
                return
            self._send_json(202, {"request_id": job.request_id, "status": job.status})
        except AuthenticationRequiredError as exc:
            self._send_json(401, self._error("authentication_required", str(exc)))
        except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._send_json(400, self._error("invalid_request", str(exc)))
        except Exception as exc:
            self._send_json(
                502,
                self._error("export_failed", f"Teams export failed: {TeamsService._safe_error(exc)}"),
            )

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
                result = self.server.service.list_chats(
                    page_size=100,
                    page_token=page_token,
                    include_avatar_uris=True,
                )
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
        except AuthenticationRequiredError as exc:
            self._send_json(401, self._error("authentication_required", str(exc)))
        except Exception as exc:
            self._send_json(
                502,
                self._error("catalog_failed", f"Could not load chats: {TeamsService._safe_error(exc)}"),
            )

    def _prepare_download(self, payload: Any) -> DownloadRequest:
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

        return DownloadRequest(
            chats=selected_chats,
            since=since,
            until=until,
            limit=limit,
            concatenate=concatenate,
        )

    @staticmethod
    def _valid_request_id(value: Any) -> bool:
        if not isinstance(value, str) or len(value) != 32:
            return False
        try:
            return uuid.UUID(hex=value).hex == value
        except ValueError:
            return False

    @classmethod
    def _query_request_id(cls, query: str) -> str | None:
        values = parse_qs(query, keep_blank_values=True).get("request_id", [])
        if len(values) != 1 or not cls._valid_request_id(values[0]):
            return None
        return values[0]

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
            is_self_chat=bool(record.get("is_self_chat")),
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
