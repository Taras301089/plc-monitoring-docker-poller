// Раздел «Простые названия сигналов» на вкладке «Справочники»: сигнал ПЛК → простое название и «где смотреть» для ответов киповцам (signalnames.py).
// Таблица как в Excel: сортировка по заголовку, фильтр по каждому столбцу, выделение ячеек и Ctrl+C, ширина столбцов attachColWidths; выгрузка в Excel с теми же столбцами.
(() => {
  'use strict';

  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const toast = t => (typeof showToast === 'function' ? showToast(t) : alert(t));
  const STATUS = { draft: 'черновик', checked: 'проверено' };

  const COLS = [
    { k: 'plc', t: 'ПЛК', tip: 'Контроллер, в программе которого используется сигнал; названия разных ПЛК не смешиваются', get: r => r.plc },
    { k: 'tag', t: 'Сигнал (технический)', tip: 'Имя переменной так, как оно записано в программе ПЛК; по нему система находит простое название', get: r => r.tag },
    { k: 'simple_name', t: 'Простое название', tip: 'Короткое название для слесаря-КИПовца, например «датчик положения кузова на выдаче»; его видно в ответах бота', get: r => r.simple_name },
    { k: 'place', t: 'Где смотреть', tip: 'Физическое место сигнала: станция, шкаф, датчик или блок; слесарь по нему находит, что проверить', get: r => r.place },
    { k: 'status', t: 'Статус', tip: 'Черновик: название ещё не проверено. Проверено: название подтвердил человек, знающий оборудование', get: r => r.status_name },
    { k: 'source_text', t: 'Исходный комментарий', tip: 'Комментарий к сигналу из проекта (английский или китайский текст) или сообщение журнала аварий: подсказка для заполнения', get: r => r.source_text },
    { k: 'author', t: 'Автор', tip: 'Кто создал строку; удалить строку может только автор и администратор, история изменений сохраняется', get: r => r.author },
    { k: 'act', t: '', tip: 'Действия со строкой: посмотреть историю изменений (кто, когда, было, стало) или удалить строку, если вы её автор', get: () => '' },
  ];
  const DATA_COLS = COLS.length - 1;
  const st = { items: [], plcs: [], limits: { simple_name: 120, place: 160, source_text: 500 }, plc: '', status: '', q: '', sorts: [], filters: {}, sel: null, anchor: null, host: null, tmr: null };
  const role = () => { const s = window.authState && window.authState(); return s && s.user ? s.user.role : null; };
  const canEdit = () => !!role() && role() !== 'viewer';

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
  const err = (r, dflt) => (r.data && typeof r.data.detail === 'string') ? r.data.detail : dflt;
  const query = () => { const p = new URLSearchParams(); if (st.plc) p.set('plc_id', st.plc); if (st.status) p.set('status', st.status); if (st.q.trim()) p.set('q', st.q.trim()); return p.toString(); };

  async function load() {
    const r = await call('GET', '/api/signal-names' + (query() ? '?' + query() : ''));
    if (!st.host || !st.host.isConnected) return;
    if (!r.ok || !r.data) { st.host.querySelector('.sn-body').innerHTML = `<div class="empty">${esc(err(r, 'Не удалось загрузить названия сигналов'))}</div>`; return; }
    st.items = r.data.items; st.plcs = r.data.plcs; st.limits = r.data.limits || st.limits;
    fillPlcs();
    draw();
  }

  function fillPlcs() {
    const sel = st.host.querySelector('.sn-plc'), cur = st.plc;
    sel.innerHTML = '<option value="">Все ПЛК</option>' + st.plcs.map(p => `<option value="${p.id}">${esc(p.name)}</option>`).join('');
    sel.value = cur;
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
    const ed = canEdit();
    const head = COLS.map(c => {
      const si = st.sorts.findIndex(s => s.k === c.k);
      const arrow = si < 0 ? '' : `<i class="dl-arrow">${st.sorts[si].dir === 1 ? '▲' : '▼'}${st.sorts.length > 1 ? `<sup>${si + 1}</sup>` : ''}</i>`;
      return `<th data-k="${c.k}" class="${si >= 0 ? 'dl-sorted' : ''}"><span class="${c.k === 'act' ? '' : 'dl-sort'}" title="${esc(c.tip)}${c.k === 'act' ? '' : '. Нажмите, чтобы отсортировать; Shift+нажатие добавляет вторую сортировку'}">${esc(c.t)}${arrow}</span></th>`;
    }).join('');
    const filt = COLS.map(c => (c.k === 'act' ? '<th></th>'
      : `<th><input type="text" class="nm-filter" data-k="${c.k}" value="${esc(st.filters[c.k] || '')}" placeholder="фильтр" title="Показать только строки, где в этом столбце есть введённый текст; регистр не важен"></th>`)).join('');
    const inp = (cls, v, max, tip) => (ed ? `<input type="text" class="${cls}" maxlength="${max}" value="${esc(v)}" title="${tip}">` : esc(v));
    const body = rows.map(({ r }, ri) => `<tr data-id="${r.id}" data-ri="${ri}">
        <td data-c="0">${esc(r.plc)}</td>
        <td data-c="1" class="sn-tag" title="Технический сигнал так, как он записан в программе ПЛК: ${esc(r.tag)}">${esc(r.tag)}</td>
        <td data-c="2">${inp('sn-name', r.simple_name, st.limits.simple_name, 'Введите короткое понятное название сигнала для слесаря и нажмите Enter или уйдите из поля, чтобы сохранить')}</td>
        <td data-c="3">${inp('sn-place', r.place, st.limits.place, 'Укажите, где физически смотреть: станция, шкаф, датчик; сохранится при нажатии Enter или уходе из поля')}</td>
        <td data-c="4">${ed ? `<select class="sn-status" title="Черновик: название не проверено. Проверено: название подтверждено; нужно заполненное простое название"><option value="draft"${r.status === 'draft' ? ' selected' : ''}>черновик</option><option value="checked"${r.status === 'checked' ? ' selected' : ''}>проверено</option></select>` : esc(r.status_name)}</td>
        <td data-c="5">${inp('sn-src', r.source_text, st.limits.source_text, 'Исходный комментарий из проекта или сообщение журнала аварий; помогает подобрать простое название')}</td>
        <td data-c="6" title="Автор строки; последнее изменение внесли (кто и когда): ${esc(r.updated_by)}, ${esc(r.updated_at.replace('T', ' ').slice(0, 16))}">${esc(r.author)}</td>
        <td data-c="7"><button type="button" class="sn-log dl-btn dt-sec" title="Показать, кто и когда менял эту строку и что было раньше">История</button>${ed ? ` <button type="button" class="sn-del dl-btn dt-sec" ${r.can_delete ? '' : 'disabled'} title="Удалить строку: может автор строки и администратор; запись об удалении остаётся в истории">Удалить</button>` : ''}</td>
      </tr>`).join('');
    const active = document.activeElement && document.activeElement.classList.contains('nm-filter') ? document.activeElement.dataset.k : null;
    host.querySelector('.sn-body').innerHTML = `<table class="users-table dl-table dict-table nm-table sn-table"><thead><tr>${head}</tr><tr>${filt}</tr></thead><tbody>${body}</tbody></table>${rows.length ? '' : '<div class="empty">Нет сигналов по выбранным фильтрам</div>'}`;
    host.querySelector('.sn-count').textContent = `Строк: ${rows.length}` + (rows.length !== st.items.length ? ` из ${st.items.length}` : '');
    const table = host.querySelector('.sn-table');
    if (window.attachColWidths) window.attachColWidths(table, 'signal-names');
    bind(table, rows);
    if (active) { const el = host.querySelector(`.nm-filter[data-k="${active}"]`); if (el) { el.focus(); el.setSelectionRange(el.value.length, el.value.length); } }
    paintSel(table);
  }

  async function save(tr, row) {
    const f = {
      simple_name: tr.querySelector('.sn-name').value.trim(), place: tr.querySelector('.sn-place').value.trim(),
      status: tr.querySelector('.sn-status').value, source_text: tr.querySelector('.sn-src').value.trim(),
    };
    if (f.simple_name === row.simple_name && f.place === row.place && f.status === row.status && f.source_text === row.source_text) return;
    const r = await call('PUT', `/api/signal-names/${row.id}`, f);
    if (!r.ok) { toast(err(r, 'Не сохранено: проверьте заполнение')); await load(); return; }
    Object.assign(row, f, { status_name: STATUS[f.status] });
    toast('Сохранено');
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
  function tsv(rows, s) {
    const out = [];
    for (let r = s.r1; r <= s.r2; r++) { const line = []; for (let c = s.c1; c <= Math.min(s.c2, DATA_COLS - 1); c++) line.push(String(COLS[c].get(rows[r].r))); out.push(line); }
    return window.xlgrid ? window.xlgrid.toTsv(out) : out.map(l => l.join('\t')).join('\n');
  }
  function copy(text, ok) {
    const done = () => toast(ok);
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(done, () => toast('Не удалось скопировать'));
    else { const ta = document.createElement('textarea'); ta.value = text; document.body.appendChild(ta); ta.select(); document.execCommand('copy'); ta.remove(); done(); }
  }

  async function showLog(row) {
    const r = await call('GET', `/api/signal-names/${row.id}/log`);
    if (!r.ok) { toast('История не загружена'); return; }
    const names = { create: 'создана', update: 'изменена', delete: 'удалена' };
    const fld = { simple_name: 'название', place: 'где смотреть', status: 'статус', source_text: 'комментарий' };
    const line = it => {
      const w = it.was || {}, n = it.now || {};
      const diff = Object.keys(fld).filter(k => (w[k] || '') !== (n[k] || '')).map(k => `${fld[k]}: «${w[k] || ''}» → «${n[k] || ''}»`).join('; ');
      return `${it.at.replace('T', ' ').slice(0, 16)} · ${names[it.action]} · ${it.by}${diff ? ' · ' + diff : ''}`;
    };
    toast(r.data.items.map(line).join('\n') || 'Изменений нет');
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
      tr.querySelectorAll('input,select').forEach(inp => {
        inp.addEventListener('change', () => save(tr, row));
        inp.addEventListener('keydown', e => { if (e.key === 'Enter') inp.blur(); });
      });
      tr.querySelector('.sn-log').addEventListener('click', () => showLog(row));
      const del = tr.querySelector('.sn-del');
      if (del) del.addEventListener('click', async () => {
        const r = await call('DELETE', `/api/signal-names/${row.id}`);
        if (!r.ok) { toast(err(r, 'Строка не удалена')); return; }
        toast('Строка удалена, история сохранена');
        load();
      });
      tr.querySelectorAll('td').forEach((td, c) => td.addEventListener('mousedown', e => {
        if (e.target.closest('input,button,select')) return;
        const r = Number(tr.dataset.ri);
        if (e.shiftKey && st.anchor) st.sel = { r1: st.anchor.r, c1: st.anchor.c, r2: r, c2: c };
        else { st.anchor = { r, c }; st.sel = { r1: r, c1: c, r2: r, c2: c }; }
        paintSel(table);
      }));
    });
    table.addEventListener('keydown', e => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'c' && st.sel && !e.target.closest('input,select')) {
        e.preventDefault(); copy(tsv(rows, selRange()), 'Скопировано выделение');
      }
    });
    table.tabIndex = 0;
  }

  async function addRow(host) {
    if (!st.plc) { toast('Сначала выберите ПЛК в списке сверху'); return; }
    const inp = host.querySelector('.sn-new-tag');
    const tag = inp.value.trim();
    if (!tag) { toast('Введите технический сигнал'); return; }
    const r = await call('POST', '/api/signal-names', { plc_id: Number(st.plc), tag });
    if (!r.ok) { toast(err(r, 'Строка не добавлена')); return; }
    inp.value = '';
    toast('Строка добавлена: заполните простое название и место');
    load();
  }

  async function drafts(host, url, body, okText) {
    const r = await call('POST', url, body);
    if (!r.ok) { toast(err(r, 'Заготовки не созданы')); return; }
    toast(`${okText}: добавлено ${r.data.added}, уже было ${r.data.skipped}`);
    load();
  }

  async function mount(host) {
    if (!role()) { host.innerHTML = ''; return; }
    st.host = host;
    const ed = canEdit();
    host.innerHTML = `<div class="dict-head"><h3 title="Простые названия сигналов ПЛК для ответов слесарям-КИПовцам: что за сигнал и где его смотреть">Простые названия сигналов</h3>
      <span class="dict-hint">Сигнал ПЛК → понятное название и место для слесаря. Заготовки заполняются автоматически, названия вносит человек.</span>
      <span class="dl-spacer"></span><span class="sn-count dict-hint"></span></div>
      <div class="dict-head sn-bar">
        <select class="sn-plc" title="Показать сигналы одного ПЛК; этот же ПЛК используется при добавлении строки и создании заготовок"><option value="">Все ПЛК</option></select>
        <select class="sn-st" title="Показать только черновики, которые ещё надо заполнить, или только названия, проверенные человеком, знающим оборудование"><option value="">Любой статус</option><option value="draft">черновик</option><option value="checked">проверено</option></select>
        <input type="text" class="sn-q" placeholder="поиск" title="Поиск по сигналу, простому названию, месту и исходному комментарию сразу; регистр не важен">
        <button type="button" class="sn-xlsx dl-btn dt-sec" title="Скачать эту таблицу в Excel с теми же столбцами и фильтрами ПЛК, статуса и поиска, что на экране">Excel</button>
        <button type="button" class="nm-reset-filters dl-btn dt-sec" title="Снять все фильтры по столбцам и сортировку таблицы простых названий">Сбросить фильтры</button>
        <button type="button" class="nm-copy dl-btn dt-sec" title="Скопировать всю показанную таблицу с заголовками: её можно вставить в Excel">Копировать таблицу</button>
      </div>
      ${ed ? `<div class="dict-head sn-bar">
        <input type="text" class="sn-new-tag" placeholder="технический сигнал" title="Имя переменной как в программе ПЛК, например ITL_GWM_DB.&quot;From GWM&quot;.BodyCheck1; выберите ПЛК сверху и нажмите «Добавить»">
        <button type="button" class="sn-add dl-btn dt-sec" title="Добавить строку для этого сигнала выбранного ПЛК: простое название и место заполните прямо в таблице">Добавить</button>
        <input type="text" class="sn-out" placeholder="выход, например OPMode08_DB.EL300.Goto3rd" title="Выход программы ПЛК, например команда движения лифта; заготовки берутся из всех сигналов, от которых он зависит">
        <button type="button" class="sn-gen dl-btn dt-sec" title="Создать заготовки для оборудования: строки-черновики для сигналов, от которых зависит этот выход, без названий; нужен выбранный ПЛК">Создать заготовки для оборудования</button>
        <button type="button" class="sn-gen-al dl-btn dt-sec" title="Создать заготовки из 20 самых частых сообщений журнала аварий выбранного ПЛК за последние 7 дней; уже имеющиеся строки не меняются">Заготовки из аварий</button>
      </div>` : ''}
      <div class="sn-body dl-body"><div class="empty">Загрузка…</div></div>`;
    const q = s => host.querySelector(s);
    q('.sn-plc').addEventListener('change', e => { st.plc = e.target.value; load(); });
    q('.sn-st').addEventListener('change', e => { st.status = e.target.value; load(); });
    q('.sn-q').addEventListener('input', e => { st.q = e.target.value; clearTimeout(st.tmr); st.tmr = setTimeout(load, 300); });
    q('.sn-xlsx').addEventListener('click', () => { window.location.href = '/api/signal-names.xlsx' + (query() ? '?' + query() : ''); });
    q('.nm-reset-filters').addEventListener('click', () => { st.filters = {}; st.sorts = []; draw(); });
    q('.nm-copy').addEventListener('click', () => {
      const rows = view();
      copy(COLS.slice(0, DATA_COLS).map(c => c.t).join('\t') + '\n' + tsv(rows, { r1: 0, c1: 0, r2: rows.length - 1, c2: DATA_COLS - 1 }), 'Таблица скопирована');
    });
    if (ed) {
      q('.sn-add').addEventListener('click', () => addRow(host));
      q('.sn-new-tag').addEventListener('keydown', e => { if (e.key === 'Enter') addRow(host); });
      q('.sn-gen').addEventListener('click', () => {
        const output = q('.sn-out').value.trim();
        if (!st.plc) { toast('Сначала выберите ПЛК в списке сверху'); return; }
        if (!output) { toast('Введите выход, например OPMode08_DB.EL300.Goto3rd'); return; }
        drafts(host, '/api/signal-names/drafts', { plc_id: Number(st.plc), output }, 'Заготовки из логики');
      });
      q('.sn-gen-al').addEventListener('click', () => {
        if (!st.plc) { toast('Сначала выберите ПЛК в списке сверху'); return; }
        drafts(host, '/api/signal-names/drafts-alarms', { plc_id: Number(st.plc), limit: 20, days: 7 }, 'Заготовки из аварий');
      });
    }
    await load();
  }

  window.signalNamesSection = { mount, COLS };
})();
