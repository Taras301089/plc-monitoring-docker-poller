const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../fastapi-api/static/theme.js'), 'utf8');

function storageHub(saved = null) {
  let value = saved;
  const clients = [];
  return {
    get value() { return value; },
    add(window) {
      const storage = {
        getItem: () => value,
        setItem(key, next) {
          if (next === value) return;
          value = next;
          clients.filter(client => client.window !== window).forEach(client => {
            client.window.emit('storage', { key, newValue: next, storageArea: client.storage });
          });
        }
      };
      clients.push({ window, storage });
      return storage;
    }
  };
}

function screen({ hub = storageHub(), parent, dark = false, textButton = false, blockedStorage = false } = {}) {
  const listeners = {};
  const documentListeners = {};
  const attributes = {};
  const observers = [];
  const buttonListeners = {};
  let ready = false;
  const button = {
    textContent: '', title: '', attributes: {},
    getAttribute: key => key === 'data-theme-label' && textButton ? 'text' : null,
    setAttribute(key, value) { this.attributes[key] = value; },
    addEventListener(key, callback) { buttonListeners[key] = callback; },
    click() { buttonListeners.click(); }
  };
  const element = {
    style: {},
    getAttribute: key => attributes[key] || null,
    setAttribute(key, value) {
      attributes[key] = value;
      observers.slice().forEach(observer => {
        if (observer.options.attributeFilter.includes(key)) observer.callback();
      });
    },
    observers
  };
  const document = {
    documentElement: element,
    readyState: 'loading',
    getElementById: () => ready ? button : null,
    addEventListener(key, callback) { documentListeners[key] = callback; }
  };
  let mediaCallback;
  const media = { matches: dark, addEventListener(key, callback) { mediaCallback = callback; } };
  const window = {
    document,
    matchMedia: () => media,
    addEventListener(key, callback) { (listeners[key] ||= []).push(callback); },
    emit(key, event) { (listeners[key] || []).forEach(callback => callback(event)); },
    MutationObserver: class {
      constructor(callback) { this.callback = callback; }
      observe(target, options) {
        this.disconnect(); this.target = target; this.options = options;
        target.observers.push(this);
      }
      disconnect() {
        if (this.target) this.target.observers.splice(this.target.observers.indexOf(this), 1);
        this.target = null;
      }
    }
  };
  window.parent = parent || window;
  if (blockedStorage) Object.defineProperty(window, 'localStorage', { get() { throw new Error('SecurityError'); } });
  else window.localStorage = hub.add(window);
  vm.runInNewContext(source, { window, document });
  return {
    window, document, button, hub,
    theme: () => window.PMMTheme.get(),
    ready() { ready = true; document.readyState = 'complete'; documentListeners.DOMContentLoaded(); },
    systemDark(value) { media.matches = value; mediaCallback(); }
  };
}

test('saved light applies before DOM ready and the SCADA button persists its change', () => {
  const page = screen({ hub: storageHub('light'), dark: true, textButton: true });
  assert.equal(page.theme(), 'light');
  assert.equal(page.document.documentElement.style.colorScheme, 'light');
  page.ready();
  assert.equal(page.button.textContent, 'Тёмная тема');
  page.button.click();
  assert.equal(page.theme(), 'dark');
  assert.equal(page.button.textContent, 'Светлая тема');
  assert.equal(page.hub.value, 'dark');
  assert.equal(screen({ hub: page.hub }).theme(), 'dark');
});

test('PMM and standalone SCADA synchronize in both directions across browser tabs', () => {
  const hub = storageHub('light');
  const pmm = screen({ hub });
  const scada = screen({ hub, textButton: true });
  pmm.ready(); scada.ready();
  pmm.button.click();
  assert.equal(scada.theme(), 'dark');
  assert.equal(scada.button.textContent, 'Светлая тема');
  scada.button.click();
  assert.equal(pmm.theme(), 'light');
  assert.equal(pmm.button.textContent, '🌙');
});

test('same-origin iframe follows host changes and can change the host with storage blocked', () => {
  const pmm = screen({ blockedStorage: true });
  pmm.ready();
  pmm.button.click();
  const scada = screen({ parent: pmm.window, blockedStorage: true });
  scada.ready();
  assert.equal(scada.theme(), 'dark');
  pmm.button.click();
  assert.equal(scada.theme(), 'light');
  scada.window.PMMTheme.toggle();
  assert.equal(pmm.theme(), 'dark');
  assert.equal(scada.theme(), 'dark');
});

test('theme updates reach already embedded SCADA as well as a separate tab', () => {
  const hub = storageHub('light');
  const pmm = screen({ hub });
  const frame = screen({ hub, parent: pmm.window });
  const standalone = screen({ hub });
  standalone.window.PMMTheme.set('dark');
  assert.deepEqual([pmm.theme(), frame.theme(), standalone.theme()], ['dark', 'dark', 'dark']);
  frame.window.PMMTheme.set('light');
  assert.deepEqual([pmm.theme(), frame.theme(), standalone.theme()], ['light', 'light', 'light']);
});

test('missing or invalid preference follows the system until a manual choice', () => {
  const page = screen({ hub: storageHub('invalid'), dark: true });
  assert.equal(page.theme(), 'dark');
  page.systemDark(false);
  assert.equal(page.theme(), 'light');
  page.window.PMMTheme.set('dark');
  page.systemDark(false);
  assert.equal(page.theme(), 'dark');
  page.window.PMMTheme.set('invalid');
  assert.equal(page.theme(), 'dark');
  page.window.emit('storage', { key: 'theme', newValue: null });
  assert.equal(page.theme(), 'light');
});

test('cross-origin frame parent and inaccessible storage cannot break the theme switch', () => {
  const parent = {};
  Object.defineProperty(parent, 'document', { get() { throw new Error('SecurityError'); } });
  const page = screen({ parent, blockedStorage: true });
  page.ready();
  assert.equal(page.theme(), 'light');
  page.button.click();
  assert.equal(page.theme(), 'dark');
});

test('restoring an iframe from page cache resumes host theme synchronization', () => {
  const pmm = screen({ blockedStorage: true });
  const frame = screen({ parent: pmm.window, blockedStorage: true });
  frame.window.emit('pagehide', {});
  pmm.window.PMMTheme.set('dark');
  assert.equal(frame.theme(), 'light');
  frame.window.emit('pageshow', {});
  assert.equal(frame.theme(), 'dark');
  pmm.window.PMMTheme.set('light');
  assert.equal(frame.theme(), 'light');
});
