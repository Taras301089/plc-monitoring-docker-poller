// Галочки как в Excel во вкладке «ПЛК»: «Выбрать все», протяжка мышью, диапазон Shift+клик.
// Чистая логика (без DOM) наверху, подключение к странице внизу.
(function (root) {
  'use strict';

  // Индексы от a до b включительно в любом порядке (протяжка вверх и вниз)
  function range(a, b) {
    const lo = Math.min(a, b), hi = Math.max(a, b), out = [];
    for (let i = lo; i <= hi; i++) out.push(i);
    return out;
  }

  // Какие индексы из диапазона a..b надо переключить в value: только доступные (enabled) и отличающиеся
  function rangeChanges(states, enabled, a, b, value) {
    return range(a, b).filter(i => i >= 0 && i < states.length && enabled[i] && !!states[i] !== !!value);
  }

  // Состояние шапочной галочки по доступным строкам: 'none' | 'some' | 'all'
  function allState(states, enabled) {
    let on = 0, total = 0;
    states.forEach((s, i) => { if (enabled[i]) { total++; if (s) on++; } });
    if (!total || !on) return 'none';
    return on === total ? 'all' : 'some';
  }

  // Что делает щелчок по шапочной галочке: всё включено -> снять у всех, иначе -> включить у всех
  function headerValue(states, enabled) { return allState(states, enabled) !== 'all'; }

  const api = { range, rangeChanges, allState, headerValue };
  root.checkgrid = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;

  // ---------- DOM ----------
  if (typeof document === 'undefined') return;
  const COLS = ['select', 'archive', 'grafana', 'alarm'];
  let rowsEl = null;
  let drag = null;           // {cls, value, last}
  const anchors = {};        // cls -> последний нажатый checkbox (для Shift+клик)
  let refreshQueued = false;

  const boxes = cls => Array.from(rowsEl.querySelectorAll('li.var-item[data-leaf] input.' + cls));
  const states = list => list.map(b => b.checked);
  const enabledOf = list => list.map(b => !b.disabled);

  function setBox(b, value) {
    if (b.disabled || b.checked === value) return;
    b.checked = value;
    b.dispatchEvent(new Event('change', { bubbles: true })); // дальше работает прежнее сохранение строки
  }

  function applyRange(cls, a, b, value) {
    const list = boxes(cls);
    rangeChanges(states(list), enabledOf(list), a, b, value).forEach(i => setBox(list[i], value));
  }

  function refreshHeader() {
    refreshQueued = false;
    document.querySelectorAll('.var-head input[data-cg]').forEach(h => {
      const list = boxes(h.dataset.cg), en = enabledOf(list);
      const st = allState(states(list), en);
      h.checked = st === 'all';
      h.indeterminate = st === 'some';
      h.disabled = !en.some(Boolean);
    });
  }
  function queueRefresh() {
    if (refreshQueued) return;
    refreshQueued = true;
    requestAnimationFrame(refreshHeader);
  }

  function colOf(el) { return el && el.classList ? COLS.find(c => el.classList.contains(c)) : null; }

  function onDown(e) {
    if (e.button !== undefined && e.button !== 0) return;
    const b = e.target.closest && e.target.closest('li.var-item[data-leaf] input[type="checkbox"]');
    const cls = colOf(b);
    if (!b || !cls || b.disabled) return;
    const list = boxes(cls), idx = list.indexOf(b), value = !b.checked;
    e.preventDefault();
    const from = e.shiftKey && anchors[cls] && list.indexOf(anchors[cls]) >= 0 ? list.indexOf(anchors[cls]) : idx;
    applyRange(cls, from, idx, value);
    anchors[cls] = b;
    drag = { cls, value, last: idx, shift: e.shiftKey };
    b.dataset.cgHandled = '1';
  }
  function onMove(e) {
    if (!drag || drag.shift) return;
    const el = document.elementFromPoint(e.clientX, e.clientY);
    const li = el && el.closest && el.closest('li.var-item[data-leaf]');
    const b = li && li.querySelector('input.' + drag.cls);
    if (!b) return;
    const idx = boxes(drag.cls).indexOf(b);
    if (idx < 0 || idx === drag.last) return;
    applyRange(drag.cls, drag.last, idx, drag.value);
    drag.last = idx;
  }
  function onUp() { drag = null; }
  // Браузер после нажатия всё равно присылает click: гасим, чтобы галочка не переключилась второй раз
  function onClick(e) {
    const b = e.target;
    if (b && b.dataset && b.dataset.cgHandled) { delete b.dataset.cgHandled; e.preventDefault(); }
  }

  function onHeader(e) {
    const h = e.target;
    if (!h.dataset || !h.dataset.cg) return;
    const list = boxes(h.dataset.cg);
    const value = headerValue(states(list), enabledOf(list));
    applyRange(h.dataset.cg, 0, list.length - 1, value);
    queueRefresh();
  }

  function init() {
    rowsEl = document.querySelector('#rows');
    if (!rowsEl) return;
    rowsEl.addEventListener('pointerdown', onDown);
    rowsEl.addEventListener('click', onClick, true);
    rowsEl.addEventListener('change', queueRefresh);
    document.addEventListener('pointermove', onMove);
    document.addEventListener('pointerup', onUp);
    document.addEventListener('pointercancel', onUp);
    document.querySelectorAll('.var-head input[data-cg]').forEach(h => h.addEventListener('change', onHeader));
    new MutationObserver(queueRefresh).observe(rowsEl, { childList: true });
    refreshHeader();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})(typeof window !== 'undefined' ? window : globalThis);
