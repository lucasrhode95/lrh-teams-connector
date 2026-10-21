// Behavioral tests of the shipped export page script, with Node's built-in runner and a minimal DOM.
// Synthetic chats only; the page's own functions and state are read from its script context.
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');

const STATIC = join(__dirname, '../../src/teams_connector/surfaces/web/static');
const source = readFileSync(join(STATIC, 'app.js'), 'utf8');
const page = readFileSync(join(STATIC, 'index.html'), 'utf8');

// ------------------------------------------------------------------ minimal DOM

class Element {
  constructor(tagName = 'div', owner = null) {
    this.tagName = tagName.toUpperCase();
    this.ownerDocument = owner;
    this.children = [];
    this.parentNode = null;
    this.attributes = new Map();
    this.dataset = {};
    this.style = {};
    this.listeners = new Map();
    this.hidden = false;
    this.disabled = false;
    this.checked = false;
    this.value = '';
    this.validity = {};
    this.scrollTop = 0;
    this.scrollHeight = 1000;
    this.clientHeight = 400;
    this.offsetHeight = 20;
    this.ownText = '';
    const classes = new Set();
    this.classList = {
      add: (...names) => names.forEach((name) => classes.add(name)),
      remove: (...names) => names.forEach((name) => classes.delete(name)),
      contains: (name) => classes.has(name),
      toggle: (name, force) => {
        const on = force ?? !classes.has(name);
        if (on) classes.add(name);
        else classes.delete(name);
        return on;
      },
      values: () => [...classes],
    };
  }
  get className() { return this.classList.values().join(' '); }
  set className(value) {
    for (const name of this.classList.values()) this.classList.remove(name);
    this.classList.add(...String(value).split(/\s+/).filter(Boolean));
  }
  get id() { return this.attributes.get('id') || ''; }
  set id(value) { this.attributes.set('id', value); }
  get textContent() { return this.ownText + this.children.map((child) => child.textContent).join(''); }
  set textContent(value) { this.children = []; this.ownText = String(value); }
  get isConnected() {
    let node = this;
    while (node.parentNode) node = node.parentNode;
    return node === this.ownerDocument?.body;
  }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  getAttribute(name) { return this.attributes.has(name) ? this.attributes.get(name) : null; }
  removeAttribute(name) { this.attributes.delete(name); }
  hasAttribute(name) { return this.attributes.has(name); }
  appendChild(child) {
    if (typeof child === 'string') { this.ownText += child; return child; }
    child.parentNode = this;
    this.children.push(child);
    return child;
  }
  append(...children) { children.forEach((child) => this.appendChild(child)); }
  replaceChildren(...children) {
    this.children.forEach((child) => { child.parentNode = null; });
    this.children = [];
    this.ownText = '';
    this.append(...children);
  }
  remove() {
    if (!this.parentNode) return;
    this.parentNode.children = this.parentNode.children.filter((child) => child !== this);
    this.parentNode = null;
  }
  contains(node) {
    for (let current = node; current; current = current.parentNode) if (current === this) return true;
    return false;
  }
  addEventListener(type, listener) {
    this.listeners.set(type, [...(this.listeners.get(type) || []), listener]);
  }
  removeEventListener() {}
  dispatch(type, init = {}) {
    let stopped = false;
    const event = {
      type, target: this, key: init.key, ctrlKey: !!init.ctrlKey, metaKey: !!init.metaKey,
      preventDefault() {}, stopPropagation() { stopped = true; },
    };
    for (let node = this; node && !stopped; node = node.parentNode) {
      for (const listener of node.listeners.get(type) || []) listener(event);
    }
  }
  click(init) { this.dispatch('click', init); }
  focus() { if (this.ownerDocument) this.ownerDocument.activeElement = this; }
  blur() {}
  getBoundingClientRect() { return { top: 0, bottom: 20, left: 0, right: 100, width: 100, height: 20 }; }
  matches(selector) { return selector.split(',').some((part) => matchesSimple(this, part.trim())); }
  descendants() { return this.children.flatMap((child) => [child, ...child.descendants()]); }
  querySelectorAll(selector) { return this.descendants().filter((node) => node.matches(selector)); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}

// Tag, class, id and [attr="value"] selectors; pseudo-classes such as :hover never match here.
function matchesSimple(node, selector) {
  if (selector.includes(':')) return false;
  const tag = selector.match(/^[a-z]+/i)?.[0];
  if (tag && node.tagName !== tag.toUpperCase()) return false;
  for (const [, name] of selector.matchAll(/\.([\w-]+)/g)) if (!node.classList.contains(name)) return false;
  for (const [, id] of selector.matchAll(/#([\w-]+)/g)) if (node.id !== id) return false;
  for (const [, name, value] of selector.matchAll(/\[([\w-]+)(?:="?([^"\]]*)"?)?\]/g)) {
    const actual = name in node && name !== 'id' ? node[name] : node.getAttribute(name);
    if (value === undefined ? actual == null : String(actual) !== value) return false;
  }
  return true;
}

// ------------------------------------------------------------------ page harness

const CHATS = [
  { id: '19:alpha@unq.gbl.spaces', name: 'Ava Example', kind: 'one_on_one' },
  { id: '19:crew@thread.v2', name: 'Release crew', kind: 'group' },
  { id: '19:meeting_review@thread.v2', name: 'Go-live review', kind: 'meeting' },
];

async function loadPage({ preview = () => ({ status: 'complete', result: { text: 'Header\n\n[2026-10-16] Ava\nHello', messages: 1, capped: false } }) } = {}) {
  const document = {
    body: null, activeElement: null,
    documentElement: { dataset: {} },
    querySelector: (selector) => (selector.startsWith('meta') ? { content: 'synthetic-token' } : null),
    addEventListener() {},
    createElement: (tag) => new Element(tag, document),
    createTextNode: (text) => text,
  };
  document.body = new Element('body', document);
  const byId = new Map();
  const hiddenIds = new Set([...page.matchAll(/<[^>]*\bid="([^"]+)"[^>]*>/g)].filter(([tag]) => /\shidden[\s>]/.test(tag)).map(([, id]) => id));
  document.getElementById = (id) => {
    if (!byId.has(id)) {
      const node = new Element('div', document);
      node.id = id;
      node.hidden = hiddenIds.has(id);
      document.body.appendChild(node);
      byId.set(id, node);
    }
    return byId.get(id);
  };
  const calls = [];
  const respond = (status, body) => ({ ok: status < 400, status, json: async () => body, blob: async () => ({}) });
  const fetch = async (path, options = {}) => {
    assert.equal(options.headers['X-Session-Token'], 'synthetic-token');
    calls.push(`${options.method || 'GET'} ${path}`);
    if (path.startsWith('/api/chats')) return respond(200, { account: { name: 'Mia Example', email: 'mia@example.com', photo_user_id: 'me' }, chats: CHATS });
    if (path === '/api/preview') return respond(202, { id: 'job-1', status: 'running' });
    if (path.startsWith('/api/preview/job-1/cancel')) return respond(202, { id: 'job-1', status: 'cancelled' });
    if (path.startsWith('/api/preview/job-1')) return respond(200, { id: 'job-1', completed_at: new Date().toISOString(), ...preview() });
    if (path.startsWith('/api/presence')) return respond(200, { presence: null, reason: 'synthetic' });
    return respond(404, { error: 'not found' });
  };
  class IntersectionObserver { observe() {} unobserve() {} }
  const window = {
    matchMedia: () => ({ matches: false, addEventListener() {} }),
    addEventListener() {}, innerWidth: 1200, innerHeight: 800,
    setTimeout, clearTimeout, requestAnimationFrame: (callback) => callback(),
  };
  const context = vm.createContext({
    document, window, fetch, IntersectionObserver, setTimeout, clearTimeout, setInterval() {},
    URL: { createObjectURL: () => 'blob:synthetic' }, console,
  });
  vm.runInContext(source, context);
  await settle();
  const run = (expression) => vm.runInContext(expression, context);
  const rows = () => run('[...list.querySelectorAll(".chat-row")]');
  const row = (name) => rows().find((item) => item.dataset.name === name);
  return { context, run, calls, rows, row, $: document.getElementById };
}

const settle = (milliseconds = 30) => new Promise((resolve) => setTimeout(resolve, milliseconds));

// ------------------------------------------------------------------ tests

test('a row click previews without selecting, and the add button selects without changing the preview', async () => {
  const ui = await loadPage();
  ui.row('Ava Example').click();
  assert.equal(ui.run('previewConversationId'), CHATS[0].id);
  assert.equal(ui.run('selected.size'), 0);

  ui.row('Release crew').querySelector('.select-toggle').click();
  assert.equal(ui.run('JSON.stringify([...selected])'), JSON.stringify([CHATS[1].id]));
  assert.equal(ui.run('previewConversationId'), CHATS[0].id, 'the add button leaves the preview alone');
  assert.equal(ui.row('Release crew').querySelector('.select-toggle').getAttribute('aria-pressed'), 'true');
});

test('conversation rows have no chat type or last activity tooltip', async () => {
  const ui = await loadPage();
  const row = ui.row('Release crew');
  assert.equal(row.querySelector('[role="tooltip"]'), null);
  assert.equal(row.getAttribute('aria-describedby'), null);
});

test('ctrl+click previews a row and toggles its selection', async () => {
  const ui = await loadPage();
  ui.row('Go-live review').click({ ctrlKey: true });
  assert.equal(ui.run('previewConversationId'), CHATS[2].id);
  assert.equal(ui.run('JSON.stringify([...selected])'), JSON.stringify([CHATS[2].id]));
  ui.row('Go-live review').click({ metaKey: true });
  assert.equal(ui.run('selected.size'), 0);
});

test('Clear all deselects every conversation and keeps the preview', async () => {
  const ui = await loadPage();
  ui.row('Ava Example').click();
  ui.row('Release crew').querySelector('.select-toggle').click();
  ui.row('Go-live review').querySelector('.select-toggle').click();
  assert.equal(ui.$('selected-chats').querySelectorAll('.selected-pill').length, 2);
  ui.$('selected-chats').querySelector('.clear-selection').click();
  assert.equal(ui.run('selected.size'), 0);
  assert.equal(ui.$('selected-chats').children.length, 0);
  assert.equal(ui.run('previewConversationId'), CHATS[0].id);
  assert.equal(ui.$('download').disabled, true);
});

test('last seen shows only for inactive people, and parts join with one separator', async () => {
  const ui = await loadPage();
  const seen = (presence) => ui.run(`lastSeenText(${JSON.stringify(presence)})`);
  const at = '2026-10-16T13:41:00Z';
  assert.match(seen({ availability: 'Away', activity: 'Away', last_active_at: at, device_type: 'Mobile' }), /^Last seen: .+ · Mobile$/);
  assert.match(seen({ availability: 'Offline', activity: 'Offline', last_active_at: at }), /^Last seen: [^·]+$/);
  assert.equal(seen({ availability: 'Busy', activity: 'InACall', last_active_at: at, device_type: 'Desktop' }), 'Desktop');
  assert.equal(seen({ availability: 'Available', activity: 'Available', device_type: 'ExternalApp' }), 'External App');
  assert.equal(seen(null), '');
});

test('presence the page cannot read is unknown, never offline', async () => {
  const ui = await loadPage();
  assert.equal(ui.run('presenceState(null)'), 'unknown');
  assert.equal(ui.run('presenceState({ availability: "PresenceUnknown" })'), 'unknown');
  assert.equal(ui.run('presenceState({ availability: "Offline" })'), 'offline');
  assert.equal(ui.run('presenceLabel(null)'), 'Presence unavailable');
});

test('the preview cache expires from job completion and keeps the ten most recent entries', async () => {
  const ui = await loadPage();
  const result = { text: 'synthetic', messages: 1, capped: false };
  ui.context.result = result;
  ui.run('cachePreview("old", result, new Date(Date.now() - 61_000).toISOString())');
  assert.equal(ui.run('getCachedPreview("old")'), null, 'a result finished over a minute ago is not kept');
  ui.run('cachePreview("fresh", result, new Date(Date.now() - 59_000).toISOString())');
  assert.equal(ui.run('getCachedPreview("fresh")'), result);
  ui.run('clearPreviewCache(); for (let i = 0; i < 11; i += 1) cachePreview("k" + i, result, new Date().toISOString())');
  assert.equal(ui.run('previewCache.size'), 10);
  assert.equal(ui.run('getCachedPreview("k0")'), null, 'the least recently used entry was evicted');
});

test('a preview opens at its newest message', async () => {
  const ui = await loadPage();
  ui.row('Ava Example').click();
  await settle(400);
  const content = ui.$('preview-content');
  assert.ok(content.querySelector('.preview-text-block'));
  assert.equal(content.scrollTop, content.scrollHeight);
});

test('a failed preview with nothing shown replaces the spinner with the message and Retry', async () => {
  const ui = await loadPage({ preview: () => ({ status: 'failed', error: { message: 'Synthetic denial.', kind: 'Upstream' } }) });
  ui.row('Release crew').click();
  await settle(400);
  const content = ui.$('preview-content');
  assert.equal(content.querySelectorAll('.spinner').length, 0);
  const box = content.querySelector('.preview-empty');
  assert.ok(box.classList.contains('error'));
  assert.match(box.textContent, /Synthetic denial\./);
  assert.ok(box.querySelector('button'), 'Retry is offered in the pane');
});
