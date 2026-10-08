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
        <header><h3 title="Настройка записи аналоговых переменных, отмеченных «Архив»: как часто сборщик данных пишет их значения в базу">Запись переменных</h3></header>
        <p class="hint" title="Мёртвая зона экономит место в базе: малые колебания значения не пишутся, но контрольная запись делается раз в минуту">Мёртвая зона: значение пишется, только если изменилось не меньше этой величины; вне порогов пишется каждый опрос.</p>
        <div class="tr-rec-body" id="tr-rec-body"></div>
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
    pane.querySelector('#tr-rec-body').addEventListener('click', e => {
      const tr = e.target.closest('tr[data-id]');
      if (!tr) return;
      if (e.target.closest('.tr-volt')) {
        tr.querySelector('.tr-db').value = '1'; tr.querySelector('.tr-lo').value = '207'; tr.querySelector('.tr-hi').value = '253';
      } else if (e.target.closest('.tr-save')) {
        saveTag(tr, e.target.closest('.tr-save'));
      }
    });
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

  function drawTags() {
    const box = pane.querySelector('#tr-rec-body');
    if (!box) return;
    if (tagsErr) { box.innerHTML = `<div class="ch-empty">${esc(tagsErr)}</div>`; return; }
    if (!tags) { box.innerHTML = '<div class="ch-empty">Загрузка…</div>'; return; }
    if (!tags.length) { box.innerHTML = '<div class="ch-empty">Отметьте переменные галочкой «Архив» в «Переменные ПЛК», и здесь появятся аналоговые для настройки записи.</div>'; return; }
    const inp = (cls, v, tip) => `<input type="text" inputmode="decimal" class="tr-in ${cls}" value="${esc(numVal(v))}" title="${esc(tip)}">`;
    box.innerHTML = `<table class="tr-rec-t"><thead><tr>
      <th title="ПЛК, которому принадлежит переменная">ПЛК</th>
      <th title="Имя аналоговой переменной, отмеченной «Архив»">Переменная</th>
      <th title="Минимальное изменение значения, при котором делается запись; пусто: писать каждый опрос">Мёртвая зона</th>
      <th title="Ниже этого значения (например, просадка напряжения) запись идёт каждый опрос; пусто: порог не задан">Нижний порог</th>
      <th title="Выше этого значения запись идёт каждый опрос; пусто: порог не задан">Верхний порог</th>
      <th title="Действия со строкой: подставить типовые значения для напряжения и сохранить настройку"></th></tr></thead><tbody>` +
      tags.map(t => `<tr data-id="${t.tag_id}">
        <td title="${esc(t.plc_name)}">${esc(t.plc_name)}</td>
        <td title="${esc(t.node_id)}">${esc(t.name)}</td>
        <td>${inp('tr-db', t.deadband, 'Мёртвая зона: не отрицательное число; значение пишется, только если изменилось не меньше; пусто: писать каждый опрос')}</td>
        <td>${inp('tr-lo', t.limit_low, 'Нижний порог: ниже него значение пишется каждый опрос; пусто: порог не задан')}</td>
        <td>${inp('tr-hi', t.limit_high, 'Верхний порог: выше него значение пишется каждый опрос; пусто: порог не задан')}</td>
        <td class="tr-act"><button type="button" class="dt-sec tr-volt" title="Подставить для напряжения: мёртвая зона 1, нижний порог 207, верхний 253; сохраняется кнопкой «Сохранить»">Для напряжения</button>
          <button type="button" class="tr-save" title="Сохранить мёртвую зону и пороги этой переменной; сборщик данных применит их со следующего опроса">Сохранить</button></td></tr>`).join('') +
      '</tbody></table>';
  }

  async function loadTags() {
    if (!isAdmin()) return;
    try {
      const r = await fetch('/api/trends/tags', { signal: AbortSignal.timeout(6000) });
      if (!r.ok) throw new Error(await errText(r));
      tags = await r.json(); tagsErr = '';
    } catch (ex) {
      tagsErr = ex.name === 'TimeoutError' ? 'Сервер не ответил за 6 секунд' : ex.message;
    }
    drawTags();
  }

  async function saveTag(tr, btn) {
    const v = c => parseNum(tr.querySelector(c).value);
    const body = { deadband: v('.tr-db'), limit_low: v('.tr-lo'), limit_high: v('.tr-hi') };
    if (Object.values(body).some(x => x !== null && !Number.isFinite(x))) { toast('Введите числа (пустое поле: не задано)'); return; }
    btn.disabled = true;
    try {
      const r = await fetch(`/api/trends/tags/${tr.dataset.id}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), signal: AbortSignal.timeout(6000) });
      if (!r.ok) throw new Error(await errText(r));
      const upd = await r.json();
      const i = tags.findIndex(t => t.tag_id === upd.tag_id);
      if (i >= 0) tags[i] = upd;
      tr.querySelector('.tr-db').value = numVal(upd.deadband);
      tr.querySelector('.tr-lo').value = numVal(upd.limit_low);
      tr.querySelector('.tr-hi').value = numVal(upd.limit_high);
      toast(`Сохранено: ${upd.name}`);
    } catch (ex) {
      toast(ex.name === 'TimeoutError' ? 'Сервер не ответил за 6 секунд' : ex.message);
    }
    btn.disabled = false;
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
