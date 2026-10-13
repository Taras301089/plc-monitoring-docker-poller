// Порядок вкладок и списков меняется перетаскиванием (мышью и пальцем).
// Где хранится: у вошедшего пользователя на сервере (личный порядок, виден на любом его устройстве);
// администратор с Shift при отпускании задаёт порядок по умолчанию для всех и для табло без входа;
// без входа порядок помнит только этот браузер. Таблица на сервере та же, что у ширин столбцов (ключи order-...).
(function (root) {
  'use strict';

  // ---------- чистые функции (проверяются в node) ----------
  // Применить сохранённый порядок: известные идентификаторы по сохранённому порядку, неизвестные (новые) в конец
  // в прежнем взаимном порядке, пропавшие из сохранённого порядка игнорируются.
  function applyOrder(items, order, idOf) {
    const pos = new Map();
    (Array.isArray(order) ? order : []).forEach((id, i) => { if (!pos.has(String(id))) pos.set(String(id), i); });
    return items
      .map((it, i) => { const id = String(idOf(it)); return { it, i, p: pos.has(id) ? pos.get(id) : Infinity }; })
      .sort((a, b) => (a.p === b.p ? a.i - b.i : a.p - b.p))
      .map(x => x.it);
  }

  // Перенести идентификатор перед beforeId (null или неизвестный: в конец); исходный список не меняется
  function moveItem(ids, id, beforeId) {
    const rest = ids.filter(x => x !== id);
    if (!ids.includes(id)) return ids.slice();
    const at = beforeId == null ? -1 : rest.indexOf(beforeId);
    if (at < 0) rest.push(id); else rest.splice(at, 0, id);
    return rest;
  }

  const api = { applyOrder, moveItem };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (typeof window === 'undefined' || !root || !root.document) return;

  // ---------- хранение ----------
  const TIMEOUT = 6000;
  const HINT = 'Перетащите, чтобы изменить порядок (порядок хранится у вас на сервере)';
  const HINT_ADMIN = '. Shift при отпускании: порядок по умолчанию для всех пользователей и табло';
  const user = () => { const st = root.authState && root.authState(); return st && st.user ? st.user : null; };
  const isAdmin = () => { const u = user(); return !!u && u.role === 'admin'; };
  const toast = t => { if (typeof root.showToast === 'function') root.showToast(t); };

  const cache = {};            // ключ -> порядок с сервера (личный, иначе общий)
  const requested = new Set(); // ключи, которые уже запрошены
  const watchers = new Map();  // ключ -> Set функций, вызываются при получении порядка с сервера
  const local = {
    get(key) { try { const v = JSON.parse(localStorage.getItem('order:' + key)); return Array.isArray(v) ? v : null; } catch { return null; } },
    set(key, v) { try { if (v) localStorage.setItem('order:' + key, JSON.stringify(v)); else localStorage.removeItem('order:' + key); } catch { /* без сохранения */ } },
  };

  function get(key) {
    if (!requested.has(key)) load(key);
    if (!user()) { const l = local.get(key); if (l) return l; }
    return cache[key] || [];
  }

  async function load(key) {
    requested.add(key);
    try {
      const r = await fetch('/api/ui/order?key=' + encodeURIComponent(key), { credentials: 'same-origin', signal: AbortSignal.timeout(TIMEOUT) });
      if (!r.ok) return;
      const d = await r.json();
      if (!Array.isArray(d.order)) return;
      cache[key] = d.order.map(String);
    } catch { return; }   // при ошибке остаётся порядок по умолчанию
    (watchers.get(key) || []).forEach(fn => { try { fn(get(key)); } catch (e) { console.error(e); } });
  }

  // fn(order) вызывается сразу, если порядок уже получен, и после каждого получения с сервера (в том числе после входа и выхода)
  function watch(key, fn, immediate = true) {
    if (!watchers.has(key)) watchers.set(key, new Set());
    watchers.get(key).add(fn);
    if (requested.has(key) && key in cache) { if (immediate) { try { fn(get(key)); } catch (e) { console.error(e); } } }
    else if (!requested.has(key)) load(key);
  }

  async function save(key, order, shared) {
    const asShared = !!shared && isAdmin();
    if (!user()) { local.set(key, order); return; }
    try {
      const r = await fetch('/api/ui/order', {
        method: 'PUT', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ key, order, shared: asShared }), signal: AbortSignal.timeout(TIMEOUT),
      });
      if (!r.ok) throw new Error(String(r.status));
      if (asShared) { toast('Порядок сохранён по умолчанию для всех пользователей и табло'); await load(key); }
    } catch { toast('Не удалось сохранить порядок на сервере'); }
  }

  function reloadAll() {
    Object.keys(cache).forEach(k => delete cache[k]);
    [...requested].forEach(k => load(k));
  }
  const attachments = [];
  const prevAuth = root.onAuthChanged;
  root.onAuthChanged = () => {
    if (prevAuth) prevAuth();
    reloadAll();
    for (let i = attachments.length - 1; i >= 0; i--) { if (attachments[i].container.isConnected) attachments[i].markAll(); else attachments.splice(i, 1); }
  };

  // ---------- работа с DOM ----------
  const directItems = (container, sel) => [...container.querySelectorAll(sel)];

  // Переставить элементы контейнера по порядку (элементы остаются на месте блока, служебные соседи не двигаются)
  function applyToDom(container, itemSelector, order, idOf) {
    const items = directItems(container, itemSelector);
    if (items.length < 2) return;
    const sorted = applyOrder(items, order, idOf);
    if (sorted.every((el, i) => el === items[i])) return;
    const tail = items[items.length - 1].nextSibling;
    sorted.forEach(el => container.insertBefore(el, tail));
  }

  let busyFlag = false;

  function mark(el) {
    el.classList.add('reorder-item');
    let t = el.getAttribute('title') || '';
    if (!t.includes('Перетащите')) t = (t ? t + '. ' : '') + HINT;
    if (isAdmin() && !t.includes('Shift при отпускании: порядок')) t += HINT_ADMIN;
    if (t !== el.getAttribute('title')) el.setAttribute('title', t);
  }

  // opts: key, itemSelector, idOf(el), fixedSelector (не двигаются и не принимают), handleSelector (за что хватать внутри элемента),
  // ignoreSelector (где перетаскивание не начинается), axis ('x' | 'y', иначе по раскладке), onChange(ids, el)
  function attachReorder(container, opts) {
    const o = Object.assign({ idOf: el => el.dataset.id, ignoreSelector: 'input,select,textarea' }, opts);
    let st = null, suppressClick = false;

    const movable = () => directItems(container, o.itemSelector).filter(el => !(o.fixedSelector && el.matches(o.fixedSelector)));
    const visible = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
    const markAll = () => movable().forEach(mark);
    markAll();
    new MutationObserver(markAll).observe(container, { childList: true });

    function clearMarks() {
      container.querySelectorAll('.reorder-before,.reorder-after,.reorder-h').forEach(e => e.classList.remove('reorder-before', 'reorder-after', 'reorder-h'));
    }
    function finish() {
      if (!st) return;
      clearTimeout(st.timer);
      if (st.active) {
        st.item.classList.remove('reorder-dragging');
        try { st.item.releasePointerCapture(st.pointerId); } catch { /* уже отпущен */ }
        busyFlag = false; document.body.classList.remove('reorder-busy');
        suppressClick = true; setTimeout(() => { suppressClick = false; }, 60);
      }
      clearMarks();
      document.removeEventListener('pointermove', onMove, true);
      document.removeEventListener('pointerup', onUp, true);
      document.removeEventListener('pointercancel', onCancel, true);
      document.removeEventListener('keydown', onKey, true);
      document.removeEventListener('touchmove', blockScroll, true);
      st = null;
    }
    const blockScroll = e => { if (st && st.active && e.cancelable) e.preventDefault(); };

    function activate() {
      st.active = true; busyFlag = true; document.body.classList.add('reorder-busy');
      st.item.classList.add('reorder-dragging');
      try { st.item.setPointerCapture(st.pointerId); } catch { /* не критично */ }
    }

    function locate(e) {
      const others = movable().filter(el => el !== st.item && visible(el));
      if (!others.length) return { before: null, after: null, horiz: true };
      const rects = others.map(el => el.getBoundingClientRect());
      const spread = (f) => Math.max(...rects.map(f)) - Math.min(...rects.map(f));
      const horiz = o.axis ? o.axis === 'x' : spread(r => r.left) > spread(r => r.top);
      for (let i = 0; i < others.length; i++) {
        const r = rects[i];
        if (horiz ? e.clientX < r.left + r.width / 2 : e.clientY < r.top + r.height / 2) return { before: others[i], after: null, horiz };
      }
      return { before: null, after: others[others.length - 1], horiz };
    }

    function onMove(e) {
      if (!st || e.pointerId !== st.pointerId) return;
      const moved = Math.hypot(e.clientX - st.x, e.clientY - st.y);
      if (!st.active) {
        if (st.touch) { if (moved > 10) finish(); return; }   // палец сдвинулся до удержания: это прокрутка
        if (moved < 5) return;
        activate();
      }
      if (e.cancelable) e.preventDefault();
      clearMarks();
      st.drop = locate(e);
      const target = st.drop.before || st.drop.after;
      if (target) {
        target.classList.add(st.drop.before ? 'reorder-before' : 'reorder-after');
        if (st.drop.horiz) target.classList.add('reorder-h');
      }
    }

    function onUp(e) {
      if (!st || e.pointerId !== st.pointerId) return;
      if (!st.active) { finish(); return; }
      const { item, drop } = st, shift = e.shiftKey;
      finish();
      if (!drop) return;
      const before = drop.before;
      if (before === item || (!before && drop.after === item)) return;
      if (before) container.insertBefore(item, before);
      else if (drop.after) container.insertBefore(item, drop.after.nextSibling);
      const ids = directItems(container, o.itemSelector).map(el => String(o.idOf(el)));
      if (!(shift && isAdmin())) cache[o.key] = ids;   // сразу, чтобы перерисовка списка не вернула старый порядок
      save(o.key, ids, shift);
      if (o.onChange) o.onChange(ids, item);
    }
    function onCancel() { finish(); }
    function onKey(e) { if (e.key === 'Escape' && st) { if (st.active) e.stopPropagation(); finish(); } }

    container.addEventListener('pointerdown', e => {
      if (st || (e.pointerType === 'mouse' && e.button !== 0)) return;
      if (e.target.closest(o.ignoreSelector || 'input')) return;
      const item = e.target.closest(o.itemSelector);
      if (!item || !container.contains(item) || !movable().includes(item)) return;
      if (o.handleSelector && !(e.target.closest(o.handleSelector) && item.contains(e.target.closest(o.handleSelector)))) return;
      st = { item, pointerId: e.pointerId, x: e.clientX, y: e.clientY, touch: e.pointerType !== 'mouse', active: false, drop: null, timer: 0 };
      if (st.touch) st.timer = setTimeout(() => { if (st) activate(); }, 250);   // удержание 250 мс, чтобы не мешать прокрутке
      document.addEventListener('pointermove', onMove, true);
      document.addEventListener('pointerup', onUp, true);
      document.addEventListener('pointercancel', onCancel, true);
      document.addEventListener('keydown', onKey, true);
      document.addEventListener('touchmove', blockScroll, { capture: true, passive: false });
    });
    // перенос не должен срабатывать как нажатие (переход по вкладке, выбор пункта); без движения клик работает как раньше
    container.addEventListener('click', e => { if (suppressClick) { e.stopPropagation(); e.preventDefault(); } }, true);
    container.addEventListener('dragstart', e => { if (e.target.closest && e.target.closest(o.itemSelector)) e.preventDefault(); });
    container.addEventListener('contextmenu', e => { if (st && st.active) e.preventDefault(); });
    const handle = { markAll, container };
    attachments.push(handle);
    return handle;
  }

  root.attachReorder = attachReorder;
  root.reorder = Object.assign({ get, load, watch, save, mark, applyToDom, attach: attachReorder, busy: () => busyFlag, hint: HINT }, api);
})(typeof window !== 'undefined' ? window : globalThis);
