document.documentElement.dataset.theme = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';

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
