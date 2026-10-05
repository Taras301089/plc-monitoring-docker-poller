// Вкладка «Простои»: все простои за период, одна строка на каждую причину. Таблица работает как в Excel:
// сортировка по заголовкам (Shift — второй уровень), фильтры в заголовках, выделение и копирование ячеек (Ctrl+C)
(() => {
  'use strict';

  const tab = document.querySelector('#tab-downtimes');
  const pane = document.querySelector('#downtimes');
  const f = { preset: 'today', from: '', to: '', rest: false, comments: false, mine: false };
  let today = null;
  let data = [];
  let built = false;
  let sorts = [];                 // [{ k, dir }] — dir: 1 по возрастанию, -1 по убыванию
  const colFilters = {};          // ключ столбца -> Set разрешённых значений
  let view = [];                  // строки, показанные сейчас (после фильтров и сортировки)
  let sel = null;                 // выделение { r1, c1, r2, c2 }
  let anchor = null;
  let dragging = false;

  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const pad = n => String(n).padStart(2, '0');
  const hhmm = m => (m === null || m === undefined ? '' : `${pad(Math.floor(m / 60) % 24)}:${pad(m % 60)}`);
  const fmt = n => String(Math.round(n));
  const fmtDay = iso => { const [, m, d] = iso.split('-'); return `${d}.${m}`; };
  const fmtTs = ts => plantClock.fmt(ts);
  const me = () => { const st = window.authState && window.authState(); return st && st.user ? st.user : null; };
  const addDays = (iso, n) => { const d = new Date(iso + 'T00:00:00Z'); d.setUTCDate(d.getUTCDate() + n); return d.toISOString().slice(0, 10); };
  const toast = t => (typeof showToast === 'function' ? showToast(t) : alert(t));

  // Столбцы: от общего к частному (бренд, участок, станция). get — значение для показа и копирования, val — для сортировки
  const rest = r => r.kind === 'rest';
  const COLS = [
    { k: 'date', t: 'Дата', tip: 'Производственные сутки', get: r => fmtDay(r.date), val: r => r.date, filter: true, dup: true },
    { k: 'time', t: 'Время', tip: 'Интервал, в котором возник простой', get: r => `${hhmm(r.start_min)}–${hhmm(r.end_min)}`, val: r => r.start_min ?? 0, dup: true, nowrap: true },
    { k: 'brand', t: 'Бренд', tip: 'Бренд или Finish Line', get: r => r.group, filter: true, dup: true },
    { k: 'area', t: 'Участок', tip: 'Участок бренда (Main Line, Floor subassembly, Body side) или линия Finish Line', get: r => r.label, filter: true, dup: true },
    { k: 'station', t: 'Станция', tip: 'Станция или пост, на которой был простой', get: r => (rest(r) ? '' : (r.station || '')), filter: true },
    { k: 'plan', t: 'План', tip: 'Плановое количество кузовов за интервал', get: r => String(r.plan), val: r => r.plan, num: true, dup: true },
    { k: 'fact', t: 'Факт', tip: 'Фактическое количество кузовов за интервал', get: r => String(r.fact), val: r => r.fact, num: true, dup: true },
    { k: 'total', t: 'Простой, мин', tip: 'Всего минут простоя в интервале (повторяется у каждой причины интервала)', get: r => fmt(r.minutes), val: r => r.minutes, num: true, dup: true },
    { k: 'reason', t: 'Причина', tip: 'Причина простоя; «Не описано» — минуты, для которых причина ещё не указана', get: r => (rest(r) ? 'Не описано' : (r.reason || '')), filter: true },
    { k: 'mins', t: 'Мин.', tip: 'Сколько минут из простоя приходится на эту причину', get: r => fmt(r.item_minutes), val: r => r.item_minutes, num: true },
    { k: 'note', t: 'Описание', tip: 'Что произошло (текст строки описания)', get: r => (rest(r) ? '' : (r.note || '')) },
    { k: 'who', t: 'Внёс', tip: 'Кто внёс строку описания', get: r => (rest(r) ? '' : (r.created_by_name || '')), filter: true },
    { k: 'cm', t: '💬', tip: 'Количество комментариев к строке', get: r => (r.comments ? String(r.comments) : ''), val: r => r.comments || 0, num: true },
  ];
  const colIdx = k => COLS.findIndex(c => c.k === k);
  const sortVal = (c, r) => (c.val ? c.val(r) : c.get(r));

  async function getJson(url) {
    try { const r = await fetch(url); return r.ok ? await r.json() : null; } catch { return null; }
  }

  function range() {
    if (!today) return {};
    if (f.preset === 'today') return { from: today, to: today };
    if (f.preset === 'yesterday') return { from: addDays(today, -1), to: addDays(today, -1) };
    if (f.preset === '7') return { from: addDays(today, -6), to: today };
    if (f.preset === '30') return { from: addDays(today, -29), to: today };
    return { from: f.from || today, to: f.to || f.from || today };
  }

  async function load() {
    if (!built) build();
    if (!today) {
      const first = await getJson('/api/kpi/downtimes-list');
      if (first) today = first.today;
    }
    const r = range();
    const res = await getJson(r.from ? `/api/kpi/downtimes-list?from=${r.from}&to=${r.to}` : '/api/kpi/downtimes-list');
    if (!res) { pane.querySelector('#dl-body').innerHTML = '<div class="empty">Не удалось загрузить простои</div>'; return; }
    today = res.today;
    data = res.rows;
    pane.querySelector('#dl-from').value = res.from || '';
    pane.querySelector('#dl-to').value = res.to || '';
    draw();
  }

  function build() {
    built = true;
    pane.innerHTML = `
      <div class="users-top"><span class="grow">Все простои за период, по строке на каждую причину. Как в Excel: сортировка по заголовку, фильтры ▾, копирование Ctrl+C. Двойной щелчок по строке открывает простой.</span></div>
      <div class="dl-filters">
        <span class="act-periods" id="dl-presets">${[['today', 'Сегодня'], ['yesterday', 'Вчера'], ['7', '7 дней'], ['30', '30 дней']].map(([k, v]) =>
          `<button type="button" data-p="${k}" class="act-per" title="Показать простои за период: ${v.toLowerCase()}">${v}</button>`).join('')}</span>
        <label class="dl-lbl" title="Начало периода (производственные сутки)">с <input type="date" id="dl-from" title="Начало периода: выберите дату"></label>
        <label class="dl-lbl" title="Конец периода">по <input type="date" id="dl-to" title="Конец периода: выберите дату"></label>
        <label class="dl-chk" title="Показать только минуты простоя, для которых ещё не указана причина"><input type="checkbox" id="dl-rest"> Только неописанные</label>
        <label class="dl-chk" title="Показать только строки, у которых есть комментарии"><input type="checkbox" id="dl-cm"> С комментариями</label>
        <label class="dl-chk" title="Показать только строки, которые внесли вы (нужен вход в систему)"><input type="checkbox" id="dl-mine"> Только мои</label>
        <span class="dl-spacer"></span>
        <button type="button" id="dl-reset" class="dt-sec dl-btn" title="Снять все фильтры по столбцам и сортировку">Сбросить фильтры</button>
        <button type="button" id="dl-copyall" class="dt-sec dl-btn" title="Скопировать всю показанную таблицу с заголовками: её можно вставить в Excel">Копировать таблицу</button>
      </div>
      <div id="dl-sum" class="dl-sum"></div><div id="dl-body" class="dl-body" tabindex="0" title="Выделите ячейки мышью и нажмите Ctrl+C, чтобы скопировать"></div>`;
    pane.querySelectorAll('#dl-presets button').forEach(b => b.addEventListener('click', () => { f.preset = b.dataset.p; load(); }));
    const dates = () => { f.preset = 'custom'; f.from = pane.querySelector('#dl-from').value; f.to = pane.querySelector('#dl-to').value; load(); };
    pane.querySelector('#dl-from').addEventListener('change', dates);
    pane.querySelector('#dl-to').addEventListener('change', dates);
    pane.querySelector('#dl-rest').addEventListener('change', e => { f.rest = e.target.checked; draw(); });
    pane.querySelector('#dl-cm').addEventListener('change', e => { f.comments = e.target.checked; draw(); });
    pane.querySelector('#dl-mine').addEventListener('change', e => { f.mine = e.target.checked; draw(); });
    pane.querySelector('#dl-reset').addEventListener('click', () => {
      Object.keys(colFilters).forEach(k => delete colFilters[k]);
      sorts = []; f.rest = f.comments = f.mine = false;
      ['#dl-rest', '#dl-cm', '#dl-mine'].forEach(s => { pane.querySelector(s).checked = false; });
      draw();
    });
    pane.querySelector('#dl-copyall').addEventListener('click', () => copy({ r1: 0, c1: 0, r2: view.length - 1, c2: COLS.length - 1 }, true));
    const body = pane.querySelector('#dl-body');
    body.addEventListener('keydown', onKey);
    document.addEventListener('mouseup', () => { dragging = false; });
  }

  // ---------- фильтры, сортировка ----------
  function pipeline() {
    const u = me();
    let rows = data.filter(r =>
      (!f.rest || rest(r)) && (!f.comments || r.comments > 0) && (!f.mine || (u && r.created_by === u.id)));
    Object.entries(colFilters).forEach(([k, set]) => {
      const c = COLS[colIdx(k)];
      rows = rows.filter(r => set.has(c.get(r) || '(пусто)'));
    });
    if (sorts.length) {
      rows = rows.map((r, i) => [r, i]).sort((a, b) => {
        for (const s of sorts) {
          const c = COLS[colIdx(s.k)], x = sortVal(c, a[0]), y = sortVal(c, b[0]);
          const d = typeof x === 'number' && typeof y === 'number' ? x - y : String(x).localeCompare(String(y), 'ru', { numeric: true });
          if (d) return d * s.dir;
        }
        return a[1] - b[1];
      }).map(x => x[0]);
    }
    return rows;
  }

  function toggleSort(k, multi) {
    const cur = sorts.find(s => s.k === k);
    if (!multi) {
      sorts = !cur ? [{ k, dir: 1 }] : cur.dir === 1 ? [{ k, dir: -1 }] : [];
    } else if (!cur) sorts.push({ k, dir: 1 });
    else if (cur.dir === 1) cur.dir = -1;
    else sorts = sorts.filter(s => s.k !== k);
    draw();
  }

  // ---------- окно фильтра столбца (как в Excel: поиск и список значений с галочками) ----------
  function openFilter(k, anchorEl) {
    document.querySelectorAll('.dl-pop').forEach(x => x.remove());
    const c = COLS[colIdx(k)];
    const all = [...new Set(data.map(r => c.get(r) || '(пусто)'))].sort((a, b) => String(a).localeCompare(String(b), 'ru', { numeric: true }));
    const pop = document.createElement('div');
    pop.className = 'dl-pop';
    const rc = anchorEl.getBoundingClientRect();
    pop.style.top = `${rc.bottom + 4}px`;
    pop.style.left = `${Math.max(8, Math.min(rc.left, window.innerWidth - 270))}px`;
    pop.innerHTML = `<input type="search" class="dl-q" placeholder="Поиск" title="Начните вводить, чтобы найти нужное значение в списке">
      <label class="dl-chk dl-all" title="Выбрать или снять все значения в списке"><input type="checkbox" class="dl-allbox"> (Выделить все)</label>
      <div class="dl-list"></div>
      <div class="dl-pop-act"><button type="button" data-a="clear" class="dt-sec dl-btn" title="Снять фильтр по этому столбцу">Сбросить</button><button type="button" data-a="close" class="dt-sec dl-btn" title="Закрыть окно фильтра">Закрыть</button></div>`;
    document.body.appendChild(pop);
    const list = pop.querySelector('.dl-list'), q = pop.querySelector('.dl-q'), allBox = pop.querySelector('.dl-allbox');
    const current = () => colFilters[k] || new Set(all);
    function renderList() {
      const term = q.value.trim().toLowerCase();
      const vals = all.filter(v => String(v).toLowerCase().includes(term));
      const set = current();
      list.innerHTML = vals.map(v => `<label class="dl-chk"><input type="checkbox" data-v="${esc(v)}"${set.has(v) ? ' checked' : ''}> ${esc(v)}</label>`).join('') || '<div class="dl-none">Ничего не найдено</div>';
      allBox.checked = vals.length > 0 && vals.every(v => set.has(v));
      list.querySelectorAll('input').forEach(i => i.addEventListener('change', () => {
        const s = new Set(current());
        if (i.checked) s.add(i.dataset.v); else s.delete(i.dataset.v);
        apply(s);
      }));
    }
    function apply(set) {
      if (set.size >= all.length) delete colFilters[k]; else colFilters[k] = set;
      allBox.checked = vals().every(v => set.has(v));
      draw();
    }
    const vals = () => all.filter(v => String(v).toLowerCase().includes(q.value.trim().toLowerCase()));
    allBox.addEventListener('change', () => {
      const s = new Set(current());
      vals().forEach(v => (allBox.checked ? s.add(v) : s.delete(v)));
      apply(s); renderList();
    });
    q.addEventListener('input', renderList);
    pop.querySelector('[data-a="clear"]').addEventListener('click', () => { delete colFilters[k]; draw(); renderList(); });
    pop.querySelector('[data-a="close"]').addEventListener('click', () => pop.remove());
    renderList();
    q.focus();
    const off = e => { if (!pop.contains(e.target) && !anchorEl.contains(e.target)) { pop.remove(); document.removeEventListener('mousedown', off, true); } };
    document.addEventListener('mousedown', off, true);
  }

  // ---------- выделение и копирование ----------
  const norm = s => ({ r1: Math.min(s.r1, s.r2), r2: Math.max(s.r1, s.r2), c1: Math.min(s.c1, s.c2), c2: Math.max(s.c1, s.c2) });

  function paintSel() {
    const body = pane.querySelector('#dl-body');
    const s = sel ? norm(sel) : null;
    body.querySelectorAll('td[data-c]').forEach(td => {
      const r = Number(td.parentNode.dataset.i), c = Number(td.dataset.c);
      td.classList.toggle('dl-sel', !!s && r >= s.r1 && r <= s.r2 && c >= s.c1 && c <= s.c2);
    });
  }

  function copy(range, withHead) {
    const s = norm(range);
    if (!view.length || s.r2 < 0) return;
    const clean = v => String(v).replace(/[\t\r\n]+/g, ' ');
    const lines = [];
    if (withHead) lines.push(COLS.slice(s.c1, s.c2 + 1).map(c => clean(c.t)).join('\t'));
    for (let r = s.r1; r <= s.r2; r++) lines.push(COLS.slice(s.c1, s.c2 + 1).map(c => clean(c.get(view[r]))).join('\t'));
    const text = lines.join('\n');
    const done = () => toast(`Скопировано: ${s.r2 - s.r1 + 1} строк × ${s.c2 - s.c1 + 1} столбцов`);
    const fallback = () => {
      const ta = document.createElement('textarea');
      ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
      document.body.appendChild(ta); ta.select();
      try { document.execCommand('copy'); done(); } catch { toast('Не удалось скопировать'); }
      ta.remove();
    };
    if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(done, fallback); else fallback();
  }

  function onKey(e) {
    const mod = e.ctrlKey || e.metaKey;
    if (mod && e.code === 'KeyC' && sel) { e.preventDefault(); copy(sel, false); }
    else if (mod && e.code === 'KeyA') { e.preventDefault(); sel = { r1: 0, c1: 0, r2: view.length - 1, c2: COLS.length - 1 }; paintSel(); }
    else if (e.key === 'Escape') { sel = null; paintSel(); }
  }

  // ---------- отрисовка ----------
  function draw() {
    pane.querySelectorAll('#dl-presets button').forEach(b => b.classList.toggle('active', b.dataset.p === f.preset));
    const u = me();
    const mineBox = pane.querySelector('#dl-mine');
    mineBox.disabled = !u;
    if (!u) { mineBox.checked = false; f.mine = false; }

    view = pipeline();
    sel = null;
    let described = 0, restSum = 0;
    const seen = new Set();
    view.forEach(r => { seen.add(r.downtime_id); if (rest(r)) restSum += r.item_minutes; else described += r.item_minutes; });
    pane.querySelector('#dl-sum').innerHTML = view.length
      ? `Простоев: <b>${seen.size}</b> · всего минут: <b>${fmt(described + restSum)}</b> · описано: <b>${fmt(described)}</b> · не описано: <b class="${restSum > 0.5 ? 'dl-warn' : ''}">${fmt(restSum)}</b> · строк: <b>${view.length}</b>`
      : '';

    const body = pane.querySelector('#dl-body');
    const head = COLS.map((c, i) => {
      const si = sorts.findIndex(s => s.k === c.k);
      const arrow = si < 0 ? '' : `<i class="dl-arrow">${sorts[si].dir === 1 ? '▲' : '▼'}${sorts.length > 1 ? `<sup>${si + 1}</sup>` : ''}</i>`;
      const on = !!colFilters[c.k];
      return `<th data-k="${c.k}" data-c="${i}" class="${c.num ? 'dl-num' : ''}${si >= 0 ? ' dl-sorted' : ''}">
        <span class="dl-sort" title="${esc(c.tip)}. Нажмите, чтобы отсортировать; Shift+нажатие добавляет вторую сортировку; Ctrl+нажатие выделяет весь столбец">${esc(c.t)}${arrow}</span>${c.filter ? `<button type="button" class="dl-fbtn${on ? ' on' : ''}" title="Фильтр по столбцу «${esc(c.t)}»${on ? ' (включён)' : ''}">▾</button>` : ''}</th>`;
    }).join('') + '<th class="dl-act" title="Открыть простой"></th>';
    if (!view.length) { body.innerHTML = `<table class="users-table dl-table"><thead><tr>${head}</tr></thead></table><div class="empty">Нет простоев за выбранный период и фильтры</div>`; bindHead(body); return; }
    let prevId = null;
    body.innerHTML = `<table class="users-table dl-table"><thead><tr>${head}</tr></thead><tbody>${view.map((r, i) => {
      const dup = prevId === r.downtime_id; prevId = r.downtime_id;
      return `<tr class="dl-row${dup ? '' : ' dl-first'}${rest(r) ? ' dl-rest' : ''}" data-i="${i}" title="Двойной щелчок — открыть простой: описать, изменить или прокомментировать">${COLS.map((c, ci) => {
        const v = c.get(r);
        const cls = [c.num ? 'dl-num' : '', c.nowrap ? 'dl-nw' : '', c.dup && dup ? 'dl-dup' : '', c.k === 'reason' && rest(r) ? 'dl-warn' : '', c.k === 'note' ? 'dl-note' : '', c.k === 'who' ? 'dl-who' : '', c.k === 'cm' && v ? 'dt-cm' : '']
          .filter(Boolean).join(' ');
        const tip = c.k === 'who' && !rest(r) ? ` title="${esc(`Внёс: ${r.created_by_name}, ${fmtTs(r.created_at)}${r.updated_by_name && r.updated_by_name !== r.created_by_name ? `. Изменил: ${r.updated_by_name}, ${fmtTs(r.updated_at)}` : ''}`)}"` : '';
        return `<td data-c="${ci}" class="${cls}"${tip}>${c.k === 'cm' && v ? '💬' : ''}${esc(v)}</td>`;
      }).join('')}<td class="dl-act"><button type="button" class="dl-open" title="Открыть простой: описать, изменить или прокомментировать">›</button></td></tr>`;
    }).join('')}</tbody></table>`;
    bindHead(body);
    body.querySelectorAll('tbody tr').forEach(tr => {
      const r = view[Number(tr.dataset.i)];
      const open = () => { if (window.dtOpenById) window.dtOpenById(r.downtime_id, r.item_id, true); };
      tr.addEventListener('dblclick', open);
      tr.querySelector('.dl-open').addEventListener('click', e => { e.stopPropagation(); open(); });
    });
    body.querySelectorAll('tbody td[data-c]').forEach(td => {
      const pos = () => ({ r: Number(td.parentNode.dataset.i), c: Number(td.dataset.c) });
      td.addEventListener('mousedown', e => {
        if (e.button !== 0) return;
        const { r, c } = pos();
        body.focus();
        if (e.shiftKey && anchor) sel = { r1: anchor.r, c1: anchor.c, r2: r, c2: c };
        else { anchor = { r, c }; sel = { r1: r, c1: c, r2: r, c2: c }; dragging = true; }
        paintSel();
        if (e.shiftKey) e.preventDefault();
      });
      td.addEventListener('mouseenter', () => {
        if (!dragging || !anchor) return;
        const { r, c } = pos();
        sel = { r1: anchor.r, c1: anchor.c, r2: r, c2: c };
        paintSel();
      });
    });
  }

  function bindHead(body) {
    body.querySelectorAll('thead th[data-k]').forEach(th => {
      const k = th.dataset.k;
      th.querySelector('.dl-sort').addEventListener('click', e => {
        if (e.ctrlKey || e.metaKey) {      // Ctrl+клик: выделить весь столбец
          const c = Number(th.dataset.c);
          sel = { r1: 0, c1: c, r2: view.length - 1, c2: c }; anchor = { r: 0, c };
          paintSel(); body.focus(); return;
        }
        toggleSort(k, e.shiftKey);
      });
      const fb = th.querySelector('.dl-fbtn');
      if (fb) fb.addEventListener('click', e => { e.stopPropagation(); openFilter(k, fb); });
    });
  }

  tab.addEventListener('click', load);
  window.dtListRefresh = () => { if (pane.classList.contains('active')) load(); };
  const prev = window.onAuthChanged;
  window.onAuthChanged = () => { if (prev) prev(); if (built) draw(); };
  // пока вкладка открыта и не открыто окно простоя, список обновляется сам (выделение при этом сбрасывается)
  setInterval(() => {
    if (pane.classList.contains('active') && !document.hidden && !document.querySelector('.dt-box, .dl-pop') && !dragging) load();
  }, 30000);
})();
