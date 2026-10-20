// Раздел «Названия» на вкладке «Справочники» (только администратор): окно словаря подписей интерфейса.
// Таблица как в Excel: сортировка по заголовку, фильтр по каждому столбцу, выделение ячеек и Ctrl+C, ширина столбцов attachColWidths.
// Названия хранятся в базе (labels.py); экран и Excel берут их оттуда, стандартные названия остаются в коде.
(() => {
  'use strict';

  const SOFT_LIMIT = 24;     // длиннее этого названия могут не поместиться в шапку таблицы на узком экране (955 px)
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const toast = t => (typeof showToast === 'function' ? showToast(t) : alert(t));

  const COLS = [
    { k: 'where', t: 'Где', tip: 'Где на сайте и в файлах Excel видна эта подпись: вкладка, плитка, график или колонка таблицы', get: r => r.where },
    { k: 'text', t: 'Стандартное', tip: 'Стандартное название, которое записано в коде и показывается, пока своё название не задано', get: r => r.text },
    { k: 'mine', t: 'Ваше название', tip: 'Ваше название вместо стандартного: оно появится на экране и в Excel не позднее чем через десять секунд', get: r => r.custom_text },
    { k: 'tip', t: 'Подсказка (title)', tip: 'Подсказка, которая появляется при наведении мыши на подпись; пусто означает стандартную подсказку', get: r => r.custom_title || r.title },
    { k: 'act', t: '', tip: 'Сбросить название и подсказку на стандартные', get: () => '' },
  ];
  const st = { items: [], sorts: [], filters: {}, sel: null, anchor: null, host: null, max: 40 };
  const isAdmin = () => { const s = window.authState && window.authState(); return !!(s && s.user && s.user.role === 'admin'); };

  async function call(method, url, body) {
    try {
      const r = await fetch(url, {
        method, credentials: 'same-origin', signal: AbortSignal.timeout(6000),
        headers: body ? { 'Content-Type': 'application/json' } : {}, body: body ? JSON.stringify(body) : undefined,
      });
      let d = null;
      try { d = await r.json(); } catch { /* пустой ответ */ }
      return { ok: r.ok, data: d };
    } catch { return { ok: false, data: { detail: 'Нет связи с сервером' } }; }
  }

  function view() {
    let rows = st.items.map((r, i) => ({ r, i }));
    for (const [k, v] of Object.entries(st.filters)) {
      const col = COLS.find(c => c.k === k), q = v.toLowerCase();
      if (q) rows = rows.filter(x => String(col.get(x.r)).toLowerCase().includes(q));
    }
    if (st.sorts.length) {
      rows.sort((a, b) => {
        for (const s of st.sorts) {
          const col = COLS.find(c => c.k === s.k);
          const c = String(col.get(a.r)).localeCompare(String(col.get(b.r)), 'ru');
          if (c) return c * s.dir;
        }
        return a.i - b.i;
      });
    }
    return rows;
  }

  function draw() {
    const host = st.host;
    if (!host || !host.isConnected) return;
    const rows = view();
    const head = COLS.map(c => {
      const si = st.sorts.findIndex(s => s.k === c.k);
      const arrow = si < 0 ? '' : `<i class="dl-arrow">${st.sorts[si].dir === 1 ? '▲' : '▼'}${st.sorts.length > 1 ? `<sup>${si + 1}</sup>` : ''}</i>`;
      return `<th data-k="${c.k}" class="${si >= 0 ? 'dl-sorted' : ''}"><span class="${c.k === 'act' ? '' : 'dl-sort'}" title="${esc(c.tip)}${c.k === 'act' ? '' : '. Нажмите, чтобы отсортировать; Shift+нажатие добавляет вторую сортировку'}">${esc(c.t)}${arrow}</span></th>`;
    }).join('');
    const filt = COLS.map(c => (c.k === 'act' ? '<th></th>'
      : `<th><input type="text" class="nm-filter" data-k="${c.k}" value="${esc(st.filters[c.k] || '')}" placeholder="фильтр" title="Показать только строки, где в этом столбце есть введённый текст; регистр не важен"></th>`)).join('');
    const body = rows.map(({ r }, ri) => {
      const long = r.custom_text.length > SOFT_LIMIT;
      return `<tr data-key="${esc(r.key)}" data-ri="${ri}">
        <td data-c="0">${esc(r.where)}</td>
        <td data-c="1"><b>${esc(r.text)}</b></td>
        <td data-c="2"><input type="text" class="nm-text" maxlength="${st.max}" value="${esc(r.custom_text)}" placeholder="${esc(r.text)}" title="Введите своё название не длиннее ${st.max} символов и нажмите Enter или уйдите из поля, чтобы сохранить">
          ${long ? `<div class="dl-warn nm-warn" title="Длинное название может не поместиться в шапку таблицы или плитку на узком экране шириной 955 пикселей">Длинное название: может не поместиться в шапку</div>` : ''}</td>
        <td data-c="3">${r.title ? `<input type="text" class="nm-title" maxlength="300" value="${esc(r.custom_title)}" placeholder="${esc(r.title)}" title="Введите свою подсказку, которая появляется при наведении мыши; пусто оставляет стандартную подсказку">` : '<span class="dict-hint" title="У этой подписи нет собственной подсказки: она не показывается как элемент интерфейса">нет</span>'}</td>
        <td data-c="4"><button type="button" class="nm-reset dl-btn dt-sec" ${r.custom_text || r.custom_title ? '' : 'disabled'} title="Вернуть стандартные название и подсказку для этой подписи; на экране и в Excel они появятся не позднее чем через десять секунд">Сбросить</button></td>
      </tr>`;
    }).join('');
    const active = document.activeElement && document.activeElement.classList.contains('nm-filter') ? document.activeElement.dataset.k : null;
    host.querySelector('.nm-body').innerHTML = `<table class="users-table dl-table dict-table nm-table"><thead><tr>${head}</tr><tr>${filt}</tr></thead><tbody>${body || ''}</tbody></table>${rows.length ? '' : '<div class="empty">Нет подписей по выбранным фильтрам</div>'}`;
    const table = host.querySelector('.nm-table');
    if (window.attachColWidths) window.attachColWidths(table, 'ui-labels');
    bind(table, rows);
    if (active) { const el = host.querySelector(`.nm-filter[data-k="${active}"]`); if (el) { el.focus(); el.setSelectionRange(el.value.length, el.value.length); } }
    paintSel(table);
  }

  async function save(tr, row) {
    const text = tr.querySelector('.nm-text').value.trim();
    const t = tr.querySelector('.nm-title');
    const title = t ? t.value.trim() : '';
    if (text === row.custom_text && title === row.custom_title) return;
    const r = await call('PUT', `/api/ui-labels/${row.key}`, { text, title });
    if (!r.ok) { toast((r.data && typeof r.data.detail === 'string') ? r.data.detail : 'Название не сохранено: проверьте длину'); return; }
    row.custom_text = text; row.custom_title = title;
    toast(text || title ? 'Название сохранено: оно появится на экране и в Excel не позднее чем через 10 секунд' : 'Название сброшено на стандартное');
    if (window.loadLabels) window.loadLabels();
    draw();
  }

  function selRange() {
    if (!st.sel) return null;
    const { r1, c1, r2, c2 } = st.sel;
    return { r1: Math.min(r1, r2), r2: Math.max(r1, r2), c1: Math.min(c1, c2), c2: Math.max(c1, c2) };
  }
  function paintSel(table) {
    const s = selRange();
    table.querySelectorAll('tbody tr').forEach(tr => tr.querySelectorAll('td').forEach((td, c) => {
      const r = Number(tr.dataset.ri);
      td.classList.toggle('dl-sel', !!s && r >= s.r1 && r <= s.r2 && c >= s.c1 && c <= s.c2);
    }));
  }
  const cellText = (r, c) => String(COLS[c].get(r));
  function tsv(rows, s) {
    const out = [];
    for (let r = s.r1; r <= s.r2; r++) { const line = []; for (let c = s.c1; c <= Math.min(s.c2, 3); c++) line.push(cellText(rows[r].r, c)); out.push(line); }
    return window.xlgrid ? window.xlgrid.toTsv(out) : out.map(l => l.join('\t')).join('\n');
  }
  function copy(text, ok) {
    const done = () => toast(ok);
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(done, () => toast('Не удалось скопировать'));
    else { const ta = document.createElement('textarea'); ta.value = text; document.body.appendChild(ta); ta.select(); document.execCommand('copy'); ta.remove(); done(); }
  }

  function bind(table, rows) {
    table.querySelectorAll('thead th[data-k] .dl-sort').forEach(sp => sp.addEventListener('click', e => {
      const k = sp.closest('th').dataset.k, i = st.sorts.findIndex(s => s.k === k);
      if (e.shiftKey) { if (i < 0) st.sorts.push({ k, dir: 1 }); else if (st.sorts[i].dir === 1) st.sorts[i].dir = -1; else st.sorts.splice(i, 1); }
      else st.sorts = i === 0 && st.sorts.length === 1 ? (st.sorts[0].dir === 1 ? [{ k, dir: -1 }] : []) : [{ k, dir: 1 }];
      draw();
    }));
    table.querySelectorAll('.nm-filter').forEach(inp => inp.addEventListener('input', () => { st.filters[inp.dataset.k] = inp.value; draw(); }));
    table.querySelectorAll('tbody tr').forEach(tr => {
      const row = rows[Number(tr.dataset.ri)].r;
      tr.querySelectorAll('input').forEach(inp => {
        inp.addEventListener('change', () => save(tr, row));
        inp.addEventListener('keydown', e => { if (e.key === 'Enter') inp.blur(); });
      });
      tr.querySelector('.nm-reset').addEventListener('click', async () => {
        const r = await call('DELETE', `/api/ui-labels/${row.key}`);
        if (!r.ok) { toast('Сброс не выполнен'); return; }
        row.custom_text = ''; row.custom_title = '';
        toast('Название сброшено на стандартное');
        if (window.loadLabels) window.loadLabels();
        draw();
      });
      tr.querySelectorAll('td').forEach((td, c) => td.addEventListener('mousedown', e => {
        if (e.target.closest('input,button')) return;
        const r = Number(tr.dataset.ri);
        if (e.shiftKey && st.anchor) st.sel = { r1: st.anchor.r, c1: st.anchor.c, r2: r, c2: c };
        else { st.anchor = { r, c }; st.sel = { r1: r, c1: c, r2: r, c2: c }; }
        paintSel(table);
      }));
    });
    table.addEventListener('keydown', e => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'c' && st.sel && !e.target.closest('input')) {
        e.preventDefault(); copy(tsv(rows, selRange()), 'Скопировано выделение');
      }
    });
    table.tabIndex = 0;
  }

  async function mount(host) {
    if (!isAdmin()) { host.innerHTML = ''; return; }
    st.host = host;
    host.innerHTML = `<div class="dict-head"><h3 title="Названия подписей интерфейса: вкладки, плитки, графики и колонки. Изменение видно на экране и в Excel">Названия</h3>
      <span class="dict-hint">Свои названия вместо стандартных на экране и в Excel; сброс возвращает стандартные. Не длиннее ${st.max} символов.</span>
      <span class="dl-spacer"></span>
      <button type="button" class="nm-reset-filters dl-btn dt-sec" title="Снять все фильтры по столбцам и сортировку таблицы названий">Сбросить фильтры</button>
      <button type="button" class="nm-copy dl-btn dt-sec" title="Скопировать всю показанную таблицу с заголовками: её можно вставить в Excel">Копировать таблицу</button></div>
      <div class="nm-body dl-body"><div class="empty">Загрузка…</div></div>`;
    host.querySelector('.nm-reset-filters').addEventListener('click', () => { st.filters = {}; st.sorts = []; draw(); });
    host.querySelector('.nm-copy').addEventListener('click', () => {
      const rows = view();
      copy(tsv(rows, { r1: 0, c1: 0, r2: rows.length - 1, c2: 3 }).replace(/^/, COLS.slice(0, 4).map(c => c.t).join('\t') + '\n'), 'Таблица скопирована');
    });
    const r = await call('GET', '/api/ui-labels/catalog');
    if (!r.ok || !r.data) { host.querySelector('.nm-body').innerHTML = '<div class="empty">Не удалось загрузить названия</div>'; return; }
    st.items = r.data.items; st.max = r.data.max_text || 40;
    draw();
  }

  window.namesSection = { mount };
})();
