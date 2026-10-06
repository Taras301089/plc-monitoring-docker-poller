// Изменение ширины столбцов перетаскиванием границы в заголовке (как в Excel).
// Два режима: таблица с <colgroup> в процентах (Andon: общая ширина остаётся 100%) и обычная таблица
// (например «Простои»: меняется ширина одного столбца, таблица при необходимости прокручивается по горизонтали).
// Где хранится: вошедший пользователь, на сервере (личные ширины, видны на любом его устройстве); администратор может
// сохранить общие (Shift при отпускании границы): они действуют для всех, у кого нет личных, и на табло без входа;
// без входа в браузере.
(() => {
  'use strict';
  const MIN = 3;       // минимальная ширина столбца в процентном режиме, % от таблицы
  const MIN_PX = 44;   // минимальная ширина столбца в обычном режиме, пикселей
  const GRIP_BASE = 'Потяните, чтобы изменить ширину столбца; двойной щелчок возвращает ширины по умолчанию.';
  const GRIP_ADMIN = ' Shift при отпускании: сохранить ширины для всех пользователей и табло; Shift + двойной щелчок: сбросить общие ширины.';

  const toast = t => { if (typeof showToast === 'function') showToast(t); };
  const user = () => { const st = window.authState && window.authState(); return st && st.user ? st.user : null; };
  const isAdmin = () => { const u = user(); return !!u && u.role === 'admin'; };

  const server = { shared: {}, mine: {} };
  const registry = [];   // таблицы на странице: при загрузке данных с сервера или смене пользователя ширины применяются заново

  const local = {
    get(key) { try { return JSON.parse(localStorage.getItem('colw:' + key)); } catch { return null; } },
    set(key, w) { try { if (w) localStorage.setItem('colw:' + key, JSON.stringify(w)); else localStorage.removeItem('colw:' + key); } catch { /* без сохранения */ } },
  };
  const validSaved = (saved, n, min) => Array.isArray(saved) && saved.length === n && saved.every(v => Number.isFinite(v) && v >= min);

  // сохранённые ширины таблицы: личные (сервер) > личные без входа (браузер) > общие (сервер)
  const current = key => server.mine[key] || local.get(key) || server.shared[key] || null;

  async function api(method, url, body) {
    try {
      const r = await fetch(url, { method, credentials: 'same-origin', headers: body ? { 'Content-Type': 'application/json' } : {}, body: body ? JSON.stringify(body) : undefined });
      return { ok: r.ok, data: await r.json().catch(() => null) };
    } catch { return { ok: false, data: null }; }
  }

  function reapplyAll() {
    for (let i = registry.length - 1; i >= 0; i--) {
      if (!registry[i].table.isConnected) registry.splice(i, 1);
      else registry[i].applySaved();
    }
  }

  async function loadServer() {
    const r = await api('GET', '/api/ui/col-widths');
    if (r.ok && r.data) { server.shared = r.data.shared || {}; server.mine = r.data.mine || {}; }
    reapplyAll();
  }

  // shared: сохранить для всех (только администратор); иначе личные ширины вошедшего пользователя или браузера
  async function save(key, widths, shared) {
    if (shared && isAdmin()) {
      const r = await api('PUT', `/api/ui/col-widths/${key}`, { widths, shared: true });
      if (r.ok) { server.shared[key] = widths; toast('Ширины столбцов сохранены для всех пользователей'); } else toast('Не удалось сохранить общие ширины');
    } else if (user()) {
      const r = await api('PUT', `/api/ui/col-widths/${key}`, { widths, shared: false });
      if (r.ok) server.mine[key] = widths; else { local.set(key, widths); toast('Нет связи с сервером: ширины сохранены только в этом браузере'); }
    } else local.set(key, widths);
  }

  async function reset(key, shared) {
    if (shared && isAdmin()) {
      const r = await api('DELETE', `/api/ui/col-widths/${key}?shared=true`);
      if (r.ok) { delete server.shared[key]; toast('Общие ширины столбцов сброшены'); }
    } else {
      if (user()) { const r = await api('DELETE', `/api/ui/col-widths/${key}`); if (r.ok) delete server.mine[key]; }
      local.set(key, null);
      delete server.mine[key];
    }
  }

  // ручка у правой границы заголовка; onDown(startX) возвращает {move(x), up(shift)}
  function addGrip(th, onDown, onReset) {
    const grip = document.createElement('span');
    grip.className = 'col-grip';
    grip.title = GRIP_BASE + (isAdmin() ? GRIP_ADMIN : '');
    th.appendChild(grip);
    grip.addEventListener('pointerdown', e => {
      e.preventDefault();
      e.stopPropagation();
      grip.setPointerCapture(e.pointerId);
      grip.classList.add('drag');
      const h = onDown(e.clientX);
      const move = ev => h.move(ev.clientX);
      const up = ev => {
        grip.classList.remove('drag');
        grip.removeEventListener('pointermove', move);
        grip.removeEventListener('pointerup', up);
        grip.removeEventListener('pointercancel', up);
        h.up(!!ev.shiftKey);
      };
      grip.addEventListener('pointermove', move);
      grip.addEventListener('pointerup', up);
      grip.addEventListener('pointercancel', up);
    });
    grip.addEventListener('dblclick', e => { e.stopPropagation(); onReset(!!e.shiftKey); });
    grip.addEventListener('click', e => e.stopPropagation());
    grip.addEventListener('mousedown', e => e.stopPropagation());
  }

  function attachPercent(table, key, cols, ths) {
    const apply = w => cols.forEach((c, i) => { c.style.width = w[i] + '%'; });
    const defaults = cols.map(c => parseFloat(c.style.width));
    const applySaved = () => { const s = current(key); apply(validSaved(s, cols.length, MIN) ? s : defaults); };
    applySaved();
    registry.push({ table, applySaved });
    ths.slice(0, -1).forEach((th, i) => addGrip(th, start => {
      const w0 = cols.map(c => parseFloat(c.style.width));
      const total = table.getBoundingClientRect().width;
      return {
        move: x => {
          const d = Math.max(MIN - w0[i], Math.min(w0[i + 1] - MIN, (x - start) / total * 100));
          const w = w0.slice();
          w[i] = +(w0[i] + d).toFixed(2);
          w[i + 1] = +(w0[i + 1] - d).toFixed(2);
          apply(w);
        },
        up: shared => save(key, cols.map(c => parseFloat(c.style.width)), shared),
      };
    }, async shared => { await reset(key, shared); applySaved(); }));
  }

  function attachPixels(table, key, ths) {
    const natural = ths.map(th => Math.round(th.getBoundingClientRect().width));
    if (natural.some(w => w < 1)) return;   // таблица сейчас не показана: ширины измерить нельзя
    const colgroup = document.createElement('colgroup');
    const cols = natural.map(() => colgroup.appendChild(document.createElement('col')));
    table.insertBefore(colgroup, table.firstChild);
    table.classList.add('col-px');
    table.style.tableLayout = 'fixed';
    const apply = w => { cols.forEach((c, i) => { c.style.width = w[i] + 'px'; }); table.style.width = w.reduce((a, b) => a + b, 0) + 'px'; };
    const applySaved = () => { const s = current(key); apply(validSaved(s, cols.length, 10) ? s : natural); };
    applySaved();
    registry.push({ table, applySaved });
    ths.slice(0, -1).forEach((th, i) => addGrip(th, start => {
      const w0 = cols.map(c => parseFloat(c.style.width));
      return {
        move: x => { const w = w0.slice(); w[i] = Math.max(MIN_PX, Math.round(w0[i] + x - start)); apply(w); },
        up: shared => save(key, cols.map(c => parseFloat(c.style.width)), shared),
      };
    }, async shared => { await reset(key, shared); applySaved(); }));
  }

  window.attachColWidths = (table, key) => {
    if (!table) return;
    const ths = [...table.querySelectorAll(':scope > thead th')];
    if (!ths.length) return;
    const cols = [...table.querySelectorAll(':scope > colgroup > col')];
    if (cols.length) { if (cols.length === ths.length) attachPercent(table, key, cols, ths); } else attachPixels(table, key, ths);
  };

  // загрузить ширины с сервера сразу и после входа или выхода пользователя
  const prevAuth = window.onAuthChanged;
  window.onAuthChanged = () => { if (prevAuth) prevAuth(); loadServer(); };
  loadServer();
})();
