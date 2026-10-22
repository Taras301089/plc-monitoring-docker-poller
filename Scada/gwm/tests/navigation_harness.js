// Dependency-free DOM/event harness for the PMM tab/SCADA iframe lifecycle.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class Element {
  constructor(tag, doc) {
    this.tagName = tag.toUpperCase(); this.doc = doc; this.children = [];
    this.dataset = {}; this.attributes = {}; this.listeners = {}; this.style = {};
    this.className = ''; this.hidden = false;
    this.classList = {
      contains: name => this.className.split(/\s+/).includes(name),
      add: name => { if (!this.classList.contains(name)) this.className += ' ' + name; },
      remove: name => { this.className = this.className.split(/\s+/).filter(x => x !== name).join(' '); },
      toggle: (name, value) => { this.classList[value ? 'add' : 'remove'](name); },
    };
  }
  setAttribute(name, value) { this.attributes[name] = value; }
  removeAttribute(name) { delete this.attributes[name]; }
  appendChild(child) { if (child.parent) child.parent.children = child.parent.children.filter(x => x !== child); child.parent = this; this.children.push(child); return child; }
  append(...children) { children.forEach(child => this.appendChild(child)); }
  replaceChildren(...children) { this.children.forEach(c => { c.parent = null; }); this.children = []; this.append(...children); }
  contains(child) { return child === this || this.children.some(c => c.contains(child)); }
  addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); }
  emit(name, props = {}) {
    const event = { target: this, preventDefault() { this.prevented = true; }, ...props };
    (this.listeners[name] || []).forEach(fn => fn(event)); return event;
  }
  click() { this.emit('click'); }
  focus() { this.doc.activeElement = this; }
  getBoundingClientRect() { return { left: 900, bottom: 44, width: 180 }; }
  get firstElementChild() { return this.children[0]; }
  querySelectorAll(selector) {
    const direct = selector.startsWith(':scope > ');
    selector = selector.replace(':scope > ', '');
    const all = direct ? this.children : this.children.flatMap(c => [c, ...c.descendants()]);
    return all.filter(el => {
      if (selector.startsWith('.tab')) {
        return el.classList.contains(selector.startsWith('.tab-content') ? 'tab-content' : 'tab') &&
          (!selector.includes('[data-tab]') || Boolean(el.dataset.tab));
      }
      if (selector === '.active') return el.classList.contains('active');
      if (selector === '[data-screen]') return Boolean(el.dataset.screen);
      const screen = selector.match(/^\[data-screen="([^"]+)"\]$/);
      if (screen) return el.dataset.screen === screen[1];
      if (selector === '[role="menuitem"]') return el.attributes.role === 'menuitem';
      return el.tagName.toLowerCase() === selector;
    });
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  descendants() { return this.children.flatMap(c => [c, ...c.descendants()]); }
}

function fixture(stored, failStorage = false) {
  const doc = { activeElement: null, listeners: {} };
  doc.createElement = tag => new Element(tag, doc);
  doc.body = doc.createElement('body');
  doc.querySelectorAll = s => doc.body.querySelectorAll(s);
  doc.getElementById = id => doc.body.descendants().find(el => el.id === id);
  doc.addEventListener = Element.prototype.addEventListener;
  doc.emit = Element.prototype.emit;
  const nav = doc.createElement('nav'); doc.body.appendChild(nav);
  function node(parent, tag, id, className) {
    const el = doc.createElement(tag); el.id = id; el.className = className; parent.appendChild(el); return el;
  }
  const andon = node(nav, 'button', 'tab-andon', 'tab active'); andon.dataset.tab = 'andon';
  const scada = node(nav, 'button', 'tab-scada', 'tab'); scada.dataset.tab = 'scada';
  const andonPanel = node(doc.body, 'section', 'andon', 'tab-content active');
  const panel = node(doc.body, 'section', 'scada', 'tab-content');
  const sidebar = node(panel, 'nav', 'scada-screens', '');
  const content = node(panel, 'div', 'scada-content', '');
  const storage = new Map([['scada-screen', stored]]);
  const attachments = [], watchers = [], observerCallbacks = [], timers = new Map();
  let timerId = 0;
  const window = {
    document: doc, innerWidth: 955, listeners: {},
    addEventListener: Element.prototype.addEventListener,
    localStorage: {
      getItem: key => { if (failStorage) throw Error('blocked'); return storage.get(key); },
      setItem: (key, value) => { if (failStorage) throw Error('blocked'); storage.set(key, value); },
    },
    reorder: {
      busy: () => false, get: () => ['chery', 'gwm', 'main', 'changan'],
      watch: (key, fn) => watchers.push({ key, fn }),
      attach: (container, opts) => attachments.push({ container, opts }),
      applyToDom(container, selector, ids) {
        container.children.sort((a, b) => ids.indexOf(a.dataset.screen) - ids.indexOf(b.dataset.screen));
      },
    },
  };
  const context = vm.createContext({
    window, document: doc, history: { replaceState() {} },
    MutationObserver: class { constructor(fn) { observerCallbacks.push(fn); } observe() {} },
    setTimeout: fn => { timers.set(++timerId, fn); return timerId; }, clearTimeout: id => timers.delete(id),
  });
  // Execute the actual PMM tab-switch handler before the feature script, as index.html does.
  const index = fs.readFileSync(process.argv[3], 'utf8');
  const tabHandler = index.split('// Tab switching')[1].split('// Порядок вкладок:')[0];
  vm.runInContext(tabHandler, context);
  vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), context);
  return { doc, window, andon, scada, andonPanel, panel, sidebar, content, storage,
    menu: doc.getElementById('scada-menu'), attachments, watchers, observerCallbacks, timers };
}

let f = fixture('gwm');
assert.equal(f.content.children.length, 0, 'inactive SCADA must not load/poll an iframe');
assert.deepEqual(f.sidebar.children.map(x => x.textContent), ['Chery', 'GWM', 'Main Layout', 'Changan']);
assert.equal(f.attachments.length, 2, 'both selectors participate in PMM drag ordering');
f.scada.click();
const firstFrame = f.content.children[0];
assert.equal(firstFrame.tagName, 'IFRAME');
assert.equal(firstFrame.src, '/scada/gwm/gwm-scada.html?embedded=1');
assert.equal(f.menu.hidden, false);
assert.equal(f.menu.style.left, '767px', 'menu must stay within a 955px viewport');
f.scada.click();
assert.equal(f.content.children[0], firstFrame, 'clicking active tab does not restart live data');
f.andon.click();
assert.equal(f.content.children.length, 0, 'leaving tab unloads live document');
f.scada.click();
assert.notEqual(f.content.children[0], firstFrame, 'return mounts fresh readings');
f.menu.querySelector('[data-screen="changan"]').click();
assert.equal(f.storage.get('scada-screen'), 'changan');
assert.equal(f.menu.hidden, true);
assert.equal(f.content.children[0].tagName, 'SECTION');
assert.match(f.content.children[0].children[1].textContent, /пока не добавлена/);
assert.equal(f.doc.activeElement, f.sidebar.querySelector('[data-screen="changan"]'));
assert.equal(f.sidebar.querySelector('[data-screen="changan"]').attributes['aria-current'], 'page');
f.sidebar.querySelector('[data-screen="main"]').click();
assert.match(f.content.children[0].children[1].textContent, /расположение линий/);
f.sidebar.querySelector('[data-screen="gwm"]').click();
assert.equal(f.content.children[0].tagName, 'IFRAME');
f.scada.emit('keydown', { key: 'ArrowDown' });
assert.equal(f.doc.activeElement.dataset.screen, 'gwm');
f.menu.emit('keydown', { key: 'End' });
assert.equal(f.doc.activeElement.dataset.screen, 'changan');
f.menu.emit('keydown', { key: 'ArrowDown' });
assert.equal(f.doc.activeElement.dataset.screen, 'chery');
f.menu.emit('keydown', { key: 'ArrowUp' });
assert.equal(f.doc.activeElement.dataset.screen, 'changan');
f.menu.emit('keydown', { key: 'Home' });
assert.equal(f.doc.activeElement.dataset.screen, 'chery');
f.doc.emit('keydown', { key: 'Escape' });
assert.equal(f.menu.hidden, true);
assert.equal(f.doc.activeElement, f.scada);
f.scada.emit('mouseenter');
f.doc.emit('click', { target: f.content });
assert.equal(f.menu.hidden, true, 'outside clicks close menu');
f.scada.emit('mouseenter');
f.menu.emit('keydown', { key: 'Tab' });
assert.equal(f.menu.hidden, true, 'Tab leaves the dropdown');
f.panel.classList.remove('active');
f.observerCallbacks.forEach(fn => fn());
assert.equal(f.content.children.length, 0, 'external/role tab changes also stop live data');

f = fixture('chery');
f.scada.click();
assert.equal(f.content.children[0].children[0].textContent, 'Chery', 'remember selected layout across loads');
f = fixture('https://example.invalid/untrusted');
f.scada.click();
assert.equal(f.content.children[0].src, '/scada/gwm/gwm-scada.html?embedded=1', 'stored selection is allowlisted');
f = fixture('chery', true);
f.scada.click();
assert.equal(f.content.children[0].tagName, 'IFRAME', 'blocked storage still permits navigation');
f.sidebar.querySelector('[data-screen="chery"]').click();
assert.equal(f.content.children[0].children[0].textContent, 'Chery');
console.log('SCADA navigation lifecycle, accessibility and persistence passed');
