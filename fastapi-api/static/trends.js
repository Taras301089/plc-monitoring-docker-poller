// Вкладка «Тренды»: технические графики; сейчас «Связь с ПЛК» за период (ленты по времени для каждого ПЛК) и выгрузка в Excel
(() => {
  'use strict';

  const pane = document.querySelector('#trends');
  const tabBtn = document.querySelector('.tab[data-tab="trends"]');
  if (!pane || !tabBtn) return;

  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const toast = t => (typeof showToast === 'function' ? showToast(t) : alert(t));
  const today = () => window.plantClock.isoDate();
  const addDays = (iso, n) => { const d = new Date(iso + 'T00:00:00Z'); d.setUTCDate(d.getUTCDate() + n); return d.toISOString().slice(0, 10); };
  const firstOfMonth = iso => iso.slice(0, 8) + '01';
  const lastOfMonth = iso => { const d = new Date(iso.slice(0, 8) + '01T00:00:00Z'); d.setUTCMonth(d.getUTCMonth() + 1); d.setUTCDate(0); return d.toISOString().slice(0, 10); };
  const daysBetween = (a, b) => Math.round((Date.parse(b + 'T00:00:00Z') - Date.parse(a + 'T00:00:00Z')) / 86400000) + 1;
  const pad = n => String(n).padStart(2, '0');
  const fmtNum = n => (Math.round(n * 10) / 10).toLocaleString('ru-RU');
  const trunc = (s, n) => (s.length > n ? s.slice(0, n - 1) + '…' : s);

  const KEY = 'trends:state';
  const MAX_DAYS = 93;
  const STATE_TEXT = { ok: 'связь есть', no_link: 'связи нет', unknown: 'нет данных' };
  const state = { from: '', to: '' };
  let data = null;
  let built = false;
  let loadToken = 0;
  let loadedAt = 0;
  let failStreak = 0;
  let timer = null;

  const presets = () => {
    const t = today();
    const pm = addDays(firstOfMonth(t), -1);
    return { day: [t, t], week: [addDays(t, -6), t], month: [firstOfMonth(t), t], prev: [firstOfMonth(pm), lastOfMonth(pm)] };
  };
  const saveState = () => { try { localStorage.setItem(KEY, JSON.stringify(state)); } catch { /* без сохранения */ } };
  const loadState = () => { try { return JSON.parse(localStorage.getItem(KEY)) || {}; } catch { return {}; } };

  // время завода: сдвиг сервера (offset_hours) прибавляется к UTC, часы браузера не участвуют
  const off = () => (data ? data.offset_hours : 5) * 3600000;
  const parts = ms => { const d = new Date(ms + off()); return { d: pad(d.getUTCDate()), m: pad(d.getUTCMonth() + 1), h: pad(d.getUTCHours()), mi: pad(d.getUTCMinutes()) }; };
  const fmtDT = ms => { const p = parts(ms); return `${p.d}.${p.m} ${p.h}:${p.mi}`; };
  const fmtDur = min => {
    const m = Math.round(min);
    return m >= 60 ? `${Math.floor(m / 60)} ч ${m % 60} мин` : `${m} мин`;
  };
  const pct = v => (v === null || v === undefined ? '—' : `${Math.round(v * 1000) / 10}%`.replace('.', ','));

  function tiles(d, lost) {
    const t = (label, val, tip, cls = '') => `<div class="ch-tile ${cls}" title="${esc(tip)}"><span>${esc(label)}</span><b>${esc(val)}</b></div>`;
    const v = x => (lost ? '—' : x);
    return t('ПЛК со сбоями', v(d.totals.plcs_failed), 'Сколько ПЛК теряли связь за выбранный период', d.totals.plcs_failed ? 'ch-bad' : 'ch-ok') +
      t('Минут без связи', v(fmtNum(d.totals.no_link_min)), 'Суммарно по всем ПЛК: минуты, когда сборщик данных не мог подключиться к ПЛК', d.totals.no_link_min ? 'ch-bad' : 'ch-ok') +
      t('Потерь связи', v(d.totals.losses), 'Сколько раз связь с ПЛК пропадала за период (по всем ПЛК)');
  }

  function ticks(ps, pe, bw) {
    const days = Math.round((pe - ps) / 86400000);
    const out = [];
    if (days <= 1) {
      const step = bw < 420 ? 4 : 2;
      for (let h = 0; h <= 24; h += step) out.push({ ms: ps + h * 3600000, label: `${pad(h % 24)}:00` });
    } else {
      const k = Math.max(1, Math.ceil(days / Math.max(1, Math.floor(bw / 48))));
      for (let i = 0; i < days; i += k) { const p = parts(ps + i * 86400000); out.push({ ms: ps + i * 86400000, label: `${p.d}.${p.m}` }); }
    }
    return out;
  }

  function chart(d, W, lost) {
    if (!d.plcs.length) return '<div class="ch-empty">Нет ПЛК в списке</div>';
    const narrow = W < 520;
    const m = { l: narrow ? 96 : 170, r: narrow ? 52 : 64, t: 8, b: 28 };
    const rowH = 26, gap = 10, bw = W - m.l - m.r;
    const H = m.t + d.plcs.length * (rowH + gap) + m.b;
    const ps = Date.parse(d.period_start), pe = Date.parse(d.period_end);
    const X = ms => m.l + (Math.min(Math.max(ms, ps), pe) - ps) / (pe - ps) * bw;
    let inner = '';
    for (const tk of ticks(ps, pe, bw)) {
      const x = X(tk.ms);
      inner += `<line class="ch-grid" x1="${x}" x2="${x}" y1="${m.t}" y2="${H - m.b}"/><text class="ch-tick" x="${x}" y="${H - m.b + 16}" text-anchor="middle">${esc(tk.label)}</text>`;
    }
    d.plcs.forEach((p, i) => {
      const y = m.t + i * (rowH + gap);
      const name = p.is_active ? p.name : `${p.name} (выкл.)`;
      inner += `<text class="ch-tick tr-name${p.is_active ? '' : ' tr-off'}" x="${m.l - 8}" y="${y + rowH / 2 + 4}" text-anchor="end"><title>${esc(p.is_active ? p.name : p.name + ': ПЛК выключен из опроса флагом «активен»')}</title>${esc(trunc(name, narrow ? 13 : 24))}</text>`;
      inner += `<rect class="tr-track" x="${m.l}" y="${y}" width="${bw}" height="${rowH}"/>`;
      if (!lost) {
        for (const s of p.segments) {
          const a = Date.parse(s.start), b = Date.parse(s.end);
          const x1 = X(a), x2 = X(b);
          const tip = `${p.name}: ${STATE_TEXT[s.state]}, ${fmtDT(a)} — ${fmtDT(b)} (${fmtDur(s.minutes)})${s.reason ? `, причина: ${s.reason}` : ''}`;
          inner += `<rect class="tr-${s.state}" x="${x1}" y="${y}" width="${Math.max(1.5, x2 - x1)}" height="${rowH}"><title>${esc(tip)}</title></rect>`;
        }
      }
      inner += `<text class="ch-val tr-pct" x="${W - m.r + 8}" y="${y + rowH / 2 + 4}"><title>${esc(`${p.name}: доля времени со связью за период (от времени, по которому есть данные)`)}</title>${lost ? '—' : esc(pct(p.summary.pct_ok))}</text>`;
    });
    return `<svg class="ch-svg" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img">${inner}</svg>`;
  }

  function build() {
    pane.innerHTML = `
      <div class="ch-bar">
        <button type="button" id="tr-prev" class="dt-sec" title="Сдвинуть период назад на его длину (например, неделя назад или предыдущий день)">◀</button>
        <label title="Первый день периода">С <input type="date" id="tr-from" max="${today()}" title="Первый день периода"></label>
        <label title="Последний день периода (включительно); период не больше ${MAX_DAYS} дней">По <input type="date" id="tr-to" max="${today()}" title="Последний день периода"></label>
        <button type="button" id="tr-next" class="dt-sec" title="Сдвинуть период вперёд на его длину; дата не позже сегодняшней">▶</button>
        <span class="ch-presets">
          <button type="button" class="dt-sec" data-p="day" title="Только сегодняшний день">День</button>
          <button type="button" class="dt-sec" data-p="week" title="Последние 7 дней, включая сегодня">Неделя</button>
          <button type="button" class="dt-sec" data-p="month" title="С первого числа текущего месяца по сегодня">Месяц</button>
          <button type="button" class="dt-sec" data-p="prev" title="Весь прошлый календарный месяц">Прошлый месяц</button>
        </span>
        <span id="tr-badge" class="live-badge off" title="Состояние запроса к серверу: красный значок означает, что данные не получены и цифры скрыты">● нет данных</span>
      </div>
      <div class="ch-tiles" id="tr-tiles"></div>
      <section class="ch-card">
        <header><h3 title="Для каждого ПЛК лента времени: зелёный отрезок связь есть, красный связи нет, серый нет данных (сборщик данных не работал); справа доля времени со связью">Связь с ПЛК</h3>
          <button type="button" id="tr-xl" class="dt-sec ch-xl" title="Выгрузить в Excel сводку по ПЛК и список потерь связи за выбранный период">⬇ Excel</button></header>
        <div class="ch-legend"><i class="tr-l-ok"></i>Связь есть<i class="tr-l-no"></i>Связи нет<i class="tr-l-un"></i>Нет данных</div>
        <div class="ch-body" id="tr-body"></div>
      </section>
      <section class="ch-card tr-rec" id="tr-rec" hidden>
        <header><h3 title="Настройка записи аналоговых переменных, отмеченных «Архив»: как часто сборщик данных пишет их значения в базу">Запись переменных</h3>
          <span class="tr-rec-btns"><button type="button" id="tr-rec-save" class="dt-sec" disabled title="Сохранить все изменённые строки: каждая проверяется и отправляется на сервер, сборщик данных применит значения со следующего опроса">Сохранить изменения</button>
          <button type="button" id="tr-rec-undo" class="dt-sec" disabled title="Отменить несохранённые правки и вернуть в таблицу значения, сохранённые на сервере">Отменить изменения</button></span></header>
        <p class="hint" title="Мёртвая зона экономит место в базе: малые колебания значения не пишутся, но контрольная запись делается раз в минуту. Ячейки выделяются мышью, как в Excel; Ctrl+C и Ctrl+V работают с Excel">Мёртвая зона: значение пишется, только если изменилось не меньше этой величины; вне порогов пишется каждый опрос. Потяните квадрат в углу ячейки вниз, чтобы заполнить строки ниже.</p>
        <div class="tr-rec-body" id="tr-rec-body" tabindex="0" title="Таблица как в Excel: стрелки, Tab, Enter, F2, Delete, Ctrl+C, Ctrl+V, Ctrl+D, Ctrl+A"></div>
        <div class="tr-rec-msg" id="tr-rec-msg" aria-live="polite"></div>
      </section>`;
    pane.querySelector('#tr-from').value = state.from;
    pane.querySelector('#tr-to').value = state.to;
    const apply = (f, t) => {
      if (!f || !t || t < f) return;
      if (t > today()) t = today();
      if (daysBetween(f, t) > MAX_DAYS) { toast(`Период не больше ${MAX_DAYS} дней`); return; }
      state.from = f; state.to = t; saveState();
      pane.querySelector('#tr-from').value = f; pane.querySelector('#tr-to').value = t;
      load();
    };
    const dates = () => [pane.querySelector('#tr-from').value, pane.querySelector('#tr-to').value];
    pane.querySelector('#tr-from').addEventListener('change', () => apply(...dates()));
    pane.querySelector('#tr-to').addEventListener('change', () => apply(...dates()));
    pane.querySelectorAll('[data-p]').forEach(b => b.addEventListener('click', () => apply(...presets()[b.dataset.p])));
    const shift = k => {
      const n = daysBetween(state.from, state.to) * k;
      const t = addDays(state.to, n);
      if (t > today() && k > 0) return;
      apply(addDays(state.from, n), t);
    };
    pane.querySelector('#tr-prev').addEventListener('click', () => shift(-1));
    pane.querySelector('#tr-next').addEventListener('click', () => shift(1));
    pane.querySelector('#tr-xl').addEventListener('click', async e => {
      const b = e.currentTarget;
      b.disabled = true;
      try { await window.xlsxDownload(`/api/trends/link.xlsx?from=${state.from}&to=${state.to}&src=trends`); } catch (ex) { toast(ex.message); }
      b.disabled = false;
    });
    initGrid();
    built = true;
    syncAdmin();
  }

  // ---- «Запись переменных»: только администратор
  const isAdmin = () => { const st = window.authState && window.authState(); return !!(st && st.user && st.user.role === 'admin'); };
  let tags = null;
  let tagsErr = '';
  const numVal = v => (v === null || v === undefined ? '' : String(v));
  const parseNum = txt => { const t = String(txt).trim().replace(',', '.'); return t === '' ? null : Number(t); };

  async function errText(r) {
    let msg = `Ошибка ${r.status}`;
    try { const j = await r.json(); if (typeof j.detail === 'string') msg = j.detail; } catch { /* оставим код */ }
    return msg;
  }

  // ---- Excel-подобная таблица настроек записи
  const XG = window.xlgrid;
  const FIELDS = ['deadband', 'limit_low', 'limit_high'];
  let draft = [];              // правки: по строке объект с текстами трёх полей
  let srvErr = {};             // tag_id -> текст ошибки сервера
  let anchor = { r: 0, c: 0 }, focus = { r: 0, c: 0 };
  let fillTo = null;           // строка, до которой тянут маркер заполнения
  let editing = null;          // { r, c, input }
  let drag = null;             // 'sel' | 'fill'
  let saving = false;

  const rowsN = () => (tags ? tags.length : 0);
  const rng = () => ({ r0: Math.min(anchor.r, focus.r), r1: Math.max(anchor.r, focus.r), c0: Math.min(anchor.c, focus.c), c1: Math.max(anchor.c, focus.c) });
  const cellAt = (r, c) => pane.querySelector(`#tr-rec-body td[data-r="${r}"][data-c="${c}"]`);
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  const setDraft = (r, c, v) => { if (draft[r]) { draft[r][FIELDS[c]] = v; delete srvErr[tags[r].tag_id]; } };
  const rowChanged = r => FIELDS.some(f => !XG.sameValue(draft[r][f], tags[r][f]));
  const resetDraft = () => { draft = (tags || []).map(t => ({ deadband: numVal(t.deadband), limit_low: numVal(t.limit_low), limit_high: numVal(t.limit_high) })); srvErr = {}; };
  const changedRows = () => (tags || []).map((t, r) => r).filter(rowChanged);

  function drawTags() {
    const box = pane.querySelector('#tr-rec-body');
    if (!box) return;
    editing = null;
    if (tagsErr) { box.innerHTML = `<div class="ch-empty">${esc(tagsErr)}</div>`; updateButtons(); return; }
    if (!tags) { box.innerHTML = '<div class="ch-empty">Загрузка…</div>'; updateButtons(); return; }
    if (!tags.length) { box.innerHTML = '<div class="ch-empty">Отметьте переменные галочкой «Архив» в «Переменные ПЛК», и здесь появятся аналоговые для настройки записи.</div>'; updateButtons(); return; }
    const cell = (r, c, tip) => `<td class="xg" data-r="${r}" data-c="${c}" data-tip="${esc(tip)}" title="${esc(tip)}"></td>`;
    box.innerHTML = `<table class="tr-rec-t"><thead><tr>
      <th title="ПЛК, которому принадлежит переменная">ПЛК</th>
      <th title="Имя аналоговой переменной, отмеченной «Архив»">Переменная</th>
      <th title="Минимальное изменение значения, при котором делается запись; пусто: писать каждый опрос">Мёртвая зона</th>
      <th title="Ниже этого значения (например, просадка напряжения) запись идёт каждый опрос; пусто: порог не задан">Нижний порог</th>
      <th title="Выше этого значения запись идёт каждый опрос; пусто: порог не задан">Верхний порог</th>
      <th title="Действия со строкой: подставить типовые значения для напряжения (без сохранения)">Действия</th></tr></thead><tbody>` +
      tags.map((t, r) => `<tr data-id="${t.tag_id}">
        <td title="${esc(t.plc_name)}">${esc(t.plc_name)}</td>
        <td title="${esc(t.node_id)}">${esc(t.name)}</td>
        ${cell(r, 0, 'Мёртвая зона: не отрицательное число; значение пишется, только если изменилось не меньше; пусто: писать каждый опрос')}
        ${cell(r, 1, 'Нижний порог: ниже него значение пишется каждый опрос; пусто: порог не задан')}
        ${cell(r, 2, 'Верхний порог: выше него значение пишется каждый опрос; пусто: порог не задан')}
        <td class="tr-act"><button type="button" class="dt-sec tr-volt" title="Подставить для напряжения: мёртвая зона 1, нижний порог 207, верхний 253; сохраняется кнопкой «Сохранить изменения»">Для напряжения</button></td></tr>`).join('') +
      '</tbody></table>';
    if (window.attachColWidths) window.attachColWidths(box.querySelector('table'), 'trends-rec');
    anchor = { r: clamp(anchor.r, 0, rowsN() - 1), c: clamp(anchor.c, 0, 2) };
    focus = { r: clamp(focus.r, 0, rowsN() - 1), c: clamp(focus.c, 0, 2) };
    updateView();
  }

  function updateButtons() {
    const chg = changedRows().length;
    const sv = pane.querySelector('#tr-rec-save'), un = pane.querySelector('#tr-rec-undo');
    if (sv) sv.disabled = !chg || saving;
    if (un) un.disabled = !chg || saving;
  }

  // Подсветка выделения, правок и ошибок без пересборки таблицы
  function updateView() {
    const box = pane.querySelector('#tr-rec-body');
    if (!box || !rowsN()) { updateButtons(); return; }
    const sel = rng();
    const fillR1 = fillTo !== null && fillTo > sel.r1 ? fillTo : sel.r1;
    box.querySelectorAll('td.xg').forEach(td => {
      const r = +td.dataset.r, c = +td.dataset.c, f = FIELDS[c];
      const inSel = r >= sel.r0 && r <= sel.r1 && c >= sel.c0 && c <= sel.c1;
      const inFill = fillTo !== null && r > sel.r1 && r <= fillR1 && c >= sel.c0 && c <= sel.c1;
      const on = inSel || inFill;
      const err = XG.validateRow(draft[r])[f] || srvErr[tags[r].tag_id] || '';
      td.classList.toggle('xg-sel', on);
      td.classList.toggle('xg-act', r === focus.r && c === focus.c);
      td.classList.toggle('xg-chg', !XG.sameValue(draft[r][f], tags[r][f]));
      td.classList.toggle('xg-bad', !!err);
      td.classList.toggle('xg-t', on && r === sel.r0);
      td.classList.toggle('xg-b', on && r === fillR1);
      td.classList.toggle('xg-l', on && c === sel.c0);
      td.classList.toggle('xg-r', on && c === sel.c1);
      td.title = err || td.dataset.tip;
      if (editing && editing.r === r && editing.c === c) return;
      td.textContent = draft[r][f];
      if (r === sel.r1 && c === sel.c1 && !editing && drag !== 'sel') {
        const h = document.createElement('span');
        h.className = 'xg-handle';
        h.title = 'Маркер заполнения: потяните вниз, чтобы скопировать выделенные значения в строки ниже, как в Excel';
        td.appendChild(h);
      }
    });
    box.querySelectorAll('tbody tr').forEach((tr, r) => tr.classList.toggle('xg-rowchg', rowChanged(r)));
    updateButtons();
  }

  function msg(t) { const m = pane.querySelector('#tr-rec-msg'); if (m) m.textContent = t || ''; }

  function setSel(a, f) {
    anchor = { r: clamp(a.r, 0, rowsN() - 1), c: clamp(a.c, 0, 2) };
    focus = f ? { r: clamp(f.r, 0, rowsN() - 1), c: clamp(f.c, 0, 2) } : { ...anchor };
    updateView();
    const td = cellAt(focus.r, focus.c);
    if (td && td.scrollIntoView) td.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  }

  function applyCells(list) { list.forEach(x => { if (x.r < rowsN() && x.c < 3) setDraft(x.r, x.c, x.v); }); updateView(); }

  function startEdit(init) {
    if (editing || !rowsN()) return;
    const td = cellAt(focus.r, focus.c);
    if (!td) return;
    const input = document.createElement('input');
    input.type = 'text'; input.inputMode = 'decimal'; input.className = 'tr-in';
    input.title = 'Значение ячейки: число, запятая или точка; Enter подтверждает, Esc отменяет, пусто: не задано';
    input.value = init === undefined ? draft[focus.r][FIELDS[focus.c]] : init;
    editing = { r: focus.r, c: focus.c, input };
    td.textContent = '';
    td.appendChild(input);
    input.focus();
    input.addEventListener('keydown', e => {
      e.stopPropagation();
      if (e.key === 'Enter' || e.key === 'Tab') {
        e.preventDefault();
        commitEdit(true);
        if (e.key === 'Enter') move(e.shiftKey ? -1 : 1, 0, false); else move(0, e.shiftKey ? -1 : 1, false);
      } else if (e.key === 'Escape') {
        e.preventDefault(); commitEdit(false);
      }
    });
    input.addEventListener('blur', () => { if (editing && editing.input === input) commitEdit(true); });
  }

  function commitEdit(keep) {
    if (!editing) return;
    const e = editing; editing = null;
    if (keep) setDraft(e.r, e.c, e.input.value.trim());
    updateView();
    pane.querySelector('#tr-rec-body').focus({ preventScroll: true });
  }

  function move(dr, dc, extend) {
    let r = focus.r + dr, c = focus.c + dc;
    if (dc) {
      if (c > 2) { c = 0; r++; } else if (c < 0) { c = 2; r--; }
    }
    r = clamp(r, 0, rowsN() - 1);
    setSel(extend ? anchor : { r, c: clamp(c, 0, 2) }, { r, c: clamp(c, 0, 2) });
  }

  function selMatrix() {
    const s = rng(), out = [];
    for (let r = s.r0; r <= s.r1; r++) { const row = []; for (let c = s.c0; c <= s.c1; c++) row.push(draft[r][FIELDS[c]]); out.push(row); }
    return out;
  }

  function fillSel(toRow) {
    const s = rng();
    if (toRow <= s.r1) return;
    const cells = [];
    XG.fillDown(selMatrix(), toRow - s.r1).forEach((row, i) => row.forEach((v, j) => cells.push({ r: s.r1 + 1 + i, c: s.c0 + j, v })));
    applyCells(cells);
    anchor = { r: s.r0, c: s.c0 }; focus = { r: toRow, c: s.c1 };
    updateView();
  }

  const isKey = (e, latin, cyr) => e.key === latin || e.key === latin.toUpperCase() || e.key === cyr || e.key === cyr.toUpperCase();

  function onKey(e) {
    if (editing || !rowsN()) return;
    const ctrl = e.ctrlKey || e.metaKey;
    const k = e.key;
    const nav = { ArrowDown: [1, 0], ArrowUp: [-1, 0], ArrowRight: [0, 1], ArrowLeft: [0, -1] }[k];
    if (nav) { e.preventDefault(); move(nav[0], nav[1], e.shiftKey); }
    else if (k === 'Tab') { e.preventDefault(); move(0, e.shiftKey ? -1 : 1, false); }
    else if (k === 'Enter') { e.preventDefault(); move(e.shiftKey ? -1 : 1, 0, false); }
    else if (k === 'F2') { e.preventDefault(); startEdit(); }
    else if (k === 'Delete' || k === 'Backspace') { e.preventDefault(); const s = rng(); applyCells(XG.clearCells(s.r0, s.r1, s.c0, s.c1)); }
    else if (ctrl && isKey(e, 'a', 'ф')) { e.preventDefault(); setSel({ r: 0, c: 0 }, { r: rowsN() - 1, c: 2 }); }
    else if (ctrl && isKey(e, 'd', 'в')) {
      e.preventDefault();
      const s = rng(), cells = [];
      for (let r = s.r0 + 1; r <= s.r1; r++) for (let c = s.c0; c <= s.c1; c++) cells.push({ r, c, v: draft[s.r0][FIELDS[c]] });
      applyCells(cells);
    }
    else if (!ctrl && !e.altKey && /^[0-9.,+-]$/.test(k)) { e.preventDefault(); startEdit(k); }
  }

  function cellFromEvent(e) {
    const el = document.elementFromPoint(e.clientX, e.clientY);
    const td = el && el.closest ? el.closest('#tr-rec-body td.xg') : null;
    return td ? { r: +td.dataset.r, c: +td.dataset.c } : null;
  }

  function initGrid() {
    const box = pane.querySelector('#tr-rec-body');
    box.addEventListener('keydown', onKey);
    box.addEventListener('mousedown', e => {
      if (e.button !== 0 || e.target.closest('input')) return;
      if (editing) commitEdit(true);
      const td = e.target.closest('td.xg');
      if (!td) return;
      e.preventDefault();
      box.focus({ preventScroll: true });
      if (e.target.closest('.xg-handle')) { drag = 'fill'; fillTo = rng().r1; return; }
      const p = { r: +td.dataset.r, c: +td.dataset.c };
      drag = 'sel';
      if (e.shiftKey) setSel(anchor, p); else setSel(p);
    });
    document.addEventListener('mousemove', e => {
      if (!drag) return;
      const p = cellFromEvent(e);
      if (!p) return;
      if (drag === 'sel') { if (p.r !== focus.r || p.c !== focus.c) { focus = p; updateView(); } }
      else if (p.r !== fillTo) { fillTo = Math.max(p.r, rng().r1); updateView(); }
    });
    document.addEventListener('mouseup', () => {
      if (!drag) return;
      const was = drag, to = fillTo;
      drag = null; fillTo = null;
      if (was === 'fill' && to !== null) fillSel(to); else updateView();
    });
    box.addEventListener('dblclick', e => { if (e.target.closest('td.xg')) startEdit(); });
    box.addEventListener('click', e => {
      const b = e.target.closest('.tr-volt');
      if (!b) return;
      const r = [...box.querySelectorAll('tbody tr')].indexOf(b.closest('tr'));
      if (r >= 0) { setDraft(r, 0, '1'); setDraft(r, 1, '207'); setDraft(r, 2, '253'); updateView(); }
    });
    box.addEventListener('copy', e => {
      if (editing || !rowsN()) return;
      e.preventDefault();
      e.clipboardData.setData('text/plain', XG.toTsv(selMatrix().map(row => row.map(v => { const n = XG.parseNum(v); return Number.isNaN(n) ? v : n; }))));
    });
    box.addEventListener('paste', e => {
      if (editing || !rowsN()) return;
      e.preventDefault();
      const m = XG.parseTsv((e.clipboardData || window.clipboardData).getData('text'));
      const s = rng();
      applyCells(XG.pasteCells(m, s.r0, s.c0, rowsN(), 3));
      setSel({ r: s.r0, c: s.c0 }, { r: s.r0 + m.length - 1, c: s.c0 + Math.max(...m.map(x => x.length)) - 1 });
    });
    pane.querySelector('#tr-rec-save').addEventListener('click', saveAll);
    pane.querySelector('#tr-rec-undo').addEventListener('click', () => { resetDraft(); msg(''); updateView(); });
  }

  async function loadTags() {
    if (!isAdmin()) return;
    try {
      const r = await fetch('/api/trends/tags', { signal: AbortSignal.timeout(6000) });
      if (!r.ok) throw new Error(await errText(r));
      tags = await r.json(); tagsErr = '';
      resetDraft();
    } catch (ex) {
      tagsErr = ex.name === 'TimeoutError' ? 'Сервер не ответил за 6 секунд' : ex.message;
    }
    drawTags();
  }

  async function saveAll() {
    if (saving) return;
    const rows = changedRows();
    if (!rows.length) return;
    saving = true; updateView();
    let ok = 0, invalid = 0, failed = 0;
    for (const r of rows) {
      const d = draft[r];
      if (Object.keys(XG.validateRow(d)).length) { invalid++; continue; }
      const body = { deadband: XG.parseNum(d.deadband), limit_low: XG.parseNum(d.limit_low), limit_high: XG.parseNum(d.limit_high) };
      try {
        const resp = await fetch(`/api/trends/tags/${tags[r].tag_id}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), signal: AbortSignal.timeout(6000) });
        if (!resp.ok) throw new Error(await errText(resp));
        const upd = await resp.json();
        tags[r] = upd;
        draft[r] = { deadband: numVal(upd.deadband), limit_low: numVal(upd.limit_low), limit_high: numVal(upd.limit_high) };
        ok++;
      } catch (ex) {
        srvErr[tags[r].tag_id] = ex.name === 'TimeoutError' ? 'Сервер не ответил за 6 секунд' : ex.message;
        failed++;
      }
    }
    saving = false;
    msg(`Сохранено ${ok}` + (invalid ? `; не отправлено из-за ошибок в значениях: ${invalid}` : '') + (failed ? `; отклонено сервером: ${failed}` : ''));
    toast(`Сохранено ${ok}`);
    updateView();
  }

  function syncAdmin() {
    const box = pane.querySelector('#tr-rec');
    if (!box) return;
    const admin = isAdmin();
    box.hidden = !admin;
    if (!admin) { tags = null; tagsErr = ''; return; }
    if (pane.classList.contains('active') && !tags) loadTags();
  }

  function markPreset() {
    const p = presets();
    pane.querySelectorAll('[data-p]').forEach(b => b.classList.toggle('on', p[b.dataset.p][0] === state.from && p[b.dataset.p][1] === state.to));
  }

  function draw() {
    if (!built) return;
    const lost = failStreak >= 2;
    const badge = pane.querySelector('#tr-badge');
    badge.className = 'live-badge ' + (failStreak >= 2 ? 'tr-bad' : (failStreak === 0 && data ? 'on' : 'off'));
    badge.textContent = failStreak >= 2 ? '● нет связи' : (data ? '● данные получены' : '● загрузка');
    if (!data) return;
    const body = pane.querySelector('#tr-body');
    const W = Math.max(320, Math.floor(body.clientWidth) || 640);
    pane.querySelector('#tr-tiles').innerHTML = tiles(data, lost);
    body.innerHTML = chart(data, W, lost);
  }

  async function load() {
    markPreset();
    if (!built) return;
    const token = ++loadToken;
    try {
      const r = await fetch(`/api/trends/link?from=${state.from}&to=${state.to}`, { signal: AbortSignal.timeout(6000) });
      if (!r.ok) {
        let msg = `Ошибка ${r.status}`;
        try { const j = await r.json(); if (j.detail) msg = j.detail; } catch { /* оставим код */ }
        throw new Error(msg);
      }
      const d = await r.json();
      if (token !== loadToken) return;
      data = d; failStreak = 0; loadedAt = Date.now();
      draw();
    } catch (ex) {
      if (token !== loadToken) return;
      failStreak += 1;
      if (failStreak === 1) toast(ex.name === 'TimeoutError' ? 'Сервер не ответил за 6 секунд' : ex.message);
      draw();
    }
  }

  // «сегодня»: обновление раз в 10 секунд, пока вкладка открыта; прошлый период читается при открытии
  function tick() {
    if (!pane.classList.contains('active') || document.hidden) return;
    if (state.to === today()) load();
  }

  function show() {
    if (!built) {
      const saved = loadState();
      const p = presets().day;
      state.from = saved.from || p[0];
      state.to = saved.to || p[1];
      if (state.to > today()) state.to = today();
      if (state.from > state.to) state.from = state.to;
      build();
      load();
    } else if (Date.now() - loadedAt > 10000) {
      load();
    }
    if (!timer) timer = setInterval(tick, 10000);
    syncAdmin();
  }

  const prevAuth = window.onAuthChanged;
  window.onAuthChanged = () => { if (prevAuth) prevAuth(); syncAdmin(); };

  tabBtn.addEventListener('click', show);
  window.addEventListener('resize', () => { if (pane.classList.contains('active')) requestAnimationFrame(draw); });
})();
