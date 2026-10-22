// Раздел «Аварии: что делать» на вкладке «Справочники»: для каждой аварии простое описание, частые причины и шаги исправления (alarmhelp.py).
// Таблица как в Excel: сортировка по заголовку, фильтр по каждому столбцу, выделение ячеек и Ctrl+C, ширина столбцов attachColWidths; правка в окне; Excel с теми же столбцами.
(() => {
  'use strict';

  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const toast = t => (typeof showToast === 'function' ? showToast(t) : alert(t));
  const STATUS = { draft: 'черновик', checked: 'проверено' };

  const COLS = [
    { k: 'plc', t: 'ПЛК', tip: 'Контроллер, в журнале которого возникает авария; справка для разных ПЛК ведётся отдельно', get: r => r.plc },
    { k: 'bit_index', t: '№ аварии', tip: 'Номер аварии, как в журнале аварий и словаре аварий ПЛК; по нему бот находит справку', get: r => String(r.bit_index) },
    { k: 'alarm_text_src', t: 'Исходный текст аварии', tip: 'Текст аварии из словаря аварий ПЛК на английском или китайском языке, как его выдаёт система', get: r => r.alarm_text_src },
    { k: 'simple_description', t: 'Что случилось', tip: 'Простыми словами, что произошло на оборудовании, чтобы слесарь сразу понял суть аварии', get: r => r.simple_description },
    { k: 'typical_causes', t: 'Частые причины', tip: 'Список частых причин этой аварии, от самой вероятной к редким; помогает искать неисправность по порядку', get: r => r.typical_causes },
    { k: 'fix_steps', t: 'Что сделать', tip: 'Шаги исправления по порядку: что проверить, что нажать, кому сообщить, если не помогло', get: r => r.fix_steps },
    { k: 'status', t: 'Статус', tip: 'Черновик: текст ещё не проверен. Проверено: текст подтвердил человек, знающий оборудование и аварию', get: r => r.status_name },
    { k: 'author', t: 'Автор', tip: 'Кто создал строку; удалить строку может только автор и администратор, история изменений сохраняется', get: r => r.author },
    { k: 'act', t: '', tip: 'Действия со строкой: изменить в окне, посмотреть историю изменений или удалить строку, если вы её автор', get: () => '' },
  ];
  const DATA_COLS = COLS.length - 1;
  const st = { items: [], limits: {}, plc: '', status: '', q: '', sorts: [], filters: {}, sel: null, anchor: null, host: null, tmr: null };
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
    const r = await call('GET', '/api/alarm-help' + (query() ? '?' + query() : ''));
    if (!st.host || !st.host.isConnected) return;
    if (!r.ok || !r.data) { st.host.querySelector('.ah-body').innerHTML = `<div class="empty">${esc(err(r, 'Не удалось загрузить справку по авариям'))}</div>`; return; }
    st.items = r.data.items; st.limits = r.data.limits || {};
    const sel = st.host.querySelector('.ah-plc');
    sel.innerHTML = '<option value="">Все ПЛК</option>' + r.data.plcs.map(p => `<option value="${p.id}">${esc(p.name)}</option>`).join('');
    sel.value = st.plc;
    draw();
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
          const c = String(col.get(a.r)).localeCompare(String(col.get(b.r)), 'ru', { numeric: true });
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
    const body = rows.map(({ r }, ri) => `<tr data-id="${r.id}" data-ri="${ri}">
        ${COLS.slice(0, DATA_COLS).map((c, ci) => `<td data-c="${ci}" class="ah-c-${c.k}">${esc(c.get(r))}</td>`).join('')}
        <td data-c="${DATA_COLS}"><button type="button" class="ah-edit dl-btn dt-sec" title="${ed ? 'Открыть окно, чтобы заполнить описание, причины, шаги исправления и статус аварии' : 'Открыть окно с полным текстом справки по этой аварии'}">${ed ? 'Изменить' : 'Открыть'}</button>
          <button type="button" class="ah-log dl-btn dt-sec" title="Показать, кто и когда менял эту строку и что было раньше">История</button>${ed ? ` <button type="button" class="ah-del dl-btn dt-sec" ${r.can_delete ? '' : 'disabled'} title="Удалить строку: может автор строки и администратор; запись об удалении остаётся в истории">Удалить</button>` : ''}</td>
      </tr>`).join('');
    const active = document.activeElement && document.activeElement.classList.contains('nm-filter') ? document.activeElement.dataset.k : null;
    host.querySelector('.ah-body').innerHTML = `<table class="users-table dl-table dict-table nm-table ah-table"><thead><tr>${head}</tr><tr>${filt}</tr></thead><tbody>${body}</tbody></table>${rows.length ? '' : '<div class="empty">Нет аварий по выбранным фильтрам</div>'}`;
    host.querySelector('.ah-count').textContent = `Строк: ${rows.length}` + (rows.length !== st.items.length ? ` из ${st.items.length}` : '');
    const table = host.querySelector('.ah-table');
    if (window.attachColWidths) window.attachColWidths(table, 'alarm-help');
    bind(table, rows);
    if (active) { const el = host.querySelector(`.nm-filter[data-k="${active}"]`); if (el) { el.focus(); el.setSelectionRange(el.value.length, el.value.length); } }
    paintSel(table);
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
    const r = await call('GET', `/api/alarm-help/${row.id}/log`);
    if (!r.ok) { toast('История не загружена'); return; }
    const names = { create: 'создана', update: 'изменена', delete: 'удалена' };
    const fld = { simple_description: 'что случилось', typical_causes: 'причины', fix_steps: 'что сделать', status: 'статус', alarm_text_src: 'исходный текст' };
    const line = it => {
      const w = it.was || {}, n = it.now || {};
      const diff = Object.keys(fld).filter(k => (w[k] || '') !== (n[k] || '')).map(k => `${fld[k]}: «${w[k] || ''}» → «${n[k] || ''}»`).join('; ');
      return `${it.at.replace('T', ' ').slice(0, 16)} · ${names[it.action]} · ${it.by}${diff ? ' · ' + diff : ''}`;
    };
    toast(r.data.items.map(line).join('\n') || 'Изменений нет');
  }

  // Окно правки: длинные тексты удобнее в нескольких строках
  function openEditor(row) {
    const ed = canEdit();
    const ov = document.createElement('div');
    ov.className = 'ah-overlay';
    const ta = (k, label, tip, rows) => `<label class="ah-field" title="${esc(tip)}"><span>${label}</span><textarea data-f="${k}" rows="${rows}" maxlength="${st.limits[k] || 1500}" ${ed ? '' : 'readonly'}>${esc(row[k])}</textarea></label>`;
    ov.innerHTML = `<div class="ah-dialog" role="dialog" aria-modal="true">
      <h3 title="Справка по аварии: описание простыми словами, частые причины и шаги исправления для слесаря">Авария № ${row.bit_index} · ${esc(row.plc)}</h3>
      ${ta('alarm_text_src', 'Исходный текст аварии', 'Текст аварии из словаря ПЛК на английском или китайском языке; можно поправить, если словарь неполный', 2)}
      ${ta('simple_description', 'Что случилось', 'Простыми словами, что произошло на оборудовании, одним-двумя предложениями для слесаря-КИПовца', 3)}
      ${ta('typical_causes', 'Частые причины', 'Список частых причин, каждая с новой строки, от самой вероятной к редким; так проще искать неисправность', 4)}
      ${ta('fix_steps', 'Что сделать', 'Шаги исправления по порядку, каждый с новой строки: что проверить, что нажать, кому сообщить', 5)}
      <label class="ah-field" title="Черновик: текст не проверен. Проверено: нужно заполнить поля «Что случилось» и «Что сделать»"><span>Статус</span>
        <select data-f="status" ${ed ? '' : 'disabled'}><option value="draft">черновик</option><option value="checked">проверено</option></select></label>
      <div class="ah-actions">${ed ? '<button type="button" class="ah-save dl-btn" title="Сохранить справку по этой аварии; изменение попадёт в историю с вашей фамилией и временем">Сохранить</button>' : ''}
        <button type="button" class="ah-close dl-btn dt-sec" title="Закрыть окно без сохранения изменений; закрыть можно и клавишей Esc или щелчком вне окна">${ed ? 'Отмена' : 'Закрыть'}</button></div></div>`;
    document.body.appendChild(ov);
    ov.querySelector('select').value = row.status;
    const close = () => { ov.remove(); document.removeEventListener('keydown', onKey); };
    const onKey = e => { if (e.key === 'Escape') close(); };
    document.addEventListener('keydown', onKey);
    ov.addEventListener('mousedown', e => { if (e.target === ov) close(); });
    ov.querySelector('.ah-close').addEventListener('click', close);
    const save = ov.querySelector('.ah-save');
    if (save) save.addEventListener('click', async () => {
      const f = {};
      ov.querySelectorAll('[data-f]').forEach(el => { f[el.dataset.f] = el.value; });
      const r = await call('PUT', `/api/alarm-help/${row.id}`, f);
      if (!r.ok) { toast(err(r, 'Не сохранено: проверьте заполнение')); return; }
      close();
      toast('Сохранено');
      load();
    });
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
      tr.querySelector('.ah-edit').addEventListener('click', () => openEditor(row));
      tr.querySelector('.ah-log').addEventListener('click', () => showLog(row));
      const del = tr.querySelector('.ah-del');
      if (del) del.addEventListener('click', async () => {
        const r = await call('DELETE', `/api/alarm-help/${row.id}`);
        if (!r.ok) { toast(err(r, 'Строка не удалена')); return; }
        toast('Строка удалена, история сохранена');
        load();
      });
      tr.querySelectorAll('td').forEach((td, c) => td.addEventListener('mousedown', e => {
        if (e.target.closest('button')) return;
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

  async function addRow(host) {
    if (!st.plc) { toast('Сначала выберите ПЛК в списке сверху'); return; }
    const inp = host.querySelector('.ah-new');
    const bit = parseInt(inp.value, 10);
    if (!(bit >= 0)) { toast('Введите номер аварии из журнала аварий'); return; }
    const r = await call('POST', '/api/alarm-help', { plc_id: Number(st.plc), bit_index: bit });
    if (!r.ok) { toast(err(r, 'Строка не добавлена')); return; }
    inp.value = '';
    toast('Строка добавлена: нажмите «Изменить» и заполните справку');
    load();
  }

  async function mount(host) {
    if (!role()) { host.innerHTML = ''; return; }
    st.host = host;
    const ed = canEdit();
    host.innerHTML = `<div class="dict-head"><h3 title="Справка по авариям для слесарей: что случилось простыми словами, частые причины и шаги исправления">Аварии: что делать</h3>
      <span class="dict-hint">Для каждой аварии из журнала: что случилось, частые причины, шаги исправления. Заготовки создаются из самых частых аварий.</span>
      <span class="dl-spacer"></span><span class="ah-count dict-hint"></span></div>
      <div class="dict-head sn-bar">
        <select class="ah-plc" title="Показать аварии одного ПЛК; этот же ПЛК используется при добавлении строки и создании заготовок"><option value="">Все ПЛК</option></select>
        <select class="ah-st" title="Показать только черновики, которые ещё надо заполнить, или только тексты, проверенные человеком, знающим оборудование"><option value="">Любой статус</option><option value="draft">черновик</option><option value="checked">проверено</option></select>
        <input type="text" class="ah-q" placeholder="поиск" title="Поиск по номеру, тексту аварии, описанию, причинам и шагам сразу; регистр не важен">
        <button type="button" class="ah-xlsx dl-btn dt-sec" title="Скачать эту таблицу в Excel с теми же столбцами и фильтрами ПЛК, статуса и поиска, что на экране">Excel</button>
        <button type="button" class="nm-reset-filters dl-btn dt-sec" title="Снять все фильтры по столбцам и сортировку таблицы справки по авариям">Сбросить фильтры</button>
        <button type="button" class="nm-copy dl-btn dt-sec" title="Скопировать всю показанную таблицу с заголовками: её можно вставить в Excel">Копировать таблицу</button>
      </div>
      ${ed ? `<div class="dict-head sn-bar">
        <input type="number" min="0" class="ah-new" placeholder="№ аварии" title="Номер аварии из журнала аварий; выберите ПЛК сверху и нажмите «Добавить», текст аварии подставится из словаря">
        <button type="button" class="ah-add dl-btn dt-sec" title="Добавить строку для этой аварии выбранного ПЛК: описание, причины и шаги заполните в окне правки">Добавить</button>
        <button type="button" class="ah-gen dl-btn dt-sec" title="Создать заготовки для 20 самых частых аварий выбранного ПЛК за последние 7 дней; уже имеющиеся строки не меняются">Заготовки из частых аварий</button>
      </div>` : ''}
      <div class="ah-body dl-body"><div class="empty">Загрузка…</div></div>`;
    const q = s => host.querySelector(s);
    q('.ah-plc').addEventListener('change', e => { st.plc = e.target.value; load(); });
    q('.ah-st').addEventListener('change', e => { st.status = e.target.value; load(); });
    q('.ah-q').addEventListener('input', e => { st.q = e.target.value; clearTimeout(st.tmr); st.tmr = setTimeout(load, 300); });
    q('.ah-xlsx').addEventListener('click', () => { window.location.href = '/api/alarm-help.xlsx' + (query() ? '?' + query() : ''); });
    q('.nm-reset-filters').addEventListener('click', () => { st.filters = {}; st.sorts = []; draw(); });
    q('.nm-copy').addEventListener('click', () => {
      const rows = view();
      copy(COLS.slice(0, DATA_COLS).map(c => c.t).join('\t') + '\n' + tsv(rows, { r1: 0, c1: 0, r2: rows.length - 1, c2: DATA_COLS - 1 }), 'Таблица скопирована');
    });
    if (ed) {
      q('.ah-add').addEventListener('click', () => addRow(host));
      q('.ah-new').addEventListener('keydown', e => { if (e.key === 'Enter') addRow(host); });
      q('.ah-gen').addEventListener('click', async () => {
        if (!st.plc) { toast('Сначала выберите ПЛК в списке сверху'); return; }
        const r = await call('POST', '/api/alarm-help/drafts', { plc_id: Number(st.plc), limit: 20, days: 7 });
        if (!r.ok) { toast(err(r, 'Заготовки не созданы')); return; }
        toast(`Заготовки из аварий: добавлено ${r.data.added}, уже было ${r.data.skipped}`);
        load();
      });
    }
    await load();
  }

  window.alarmHelpSection = { mount, COLS };
})();
