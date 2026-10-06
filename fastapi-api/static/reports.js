// Вкладка «Отчёты»: отчёт дня по выбранной линии (график и таблица, как в файле Excel) и скачивание;
// ниже настройка автосохранения отчётов в папку на ПК с Docker (только администратор)
(() => {
  'use strict';

  const tab = document.querySelector('#tab-reports');
  const pane = document.querySelector('#reports');
  if (!tab || !pane) return;

  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const toast = t => (typeof showToast === 'function' ? showToast(t) : alert(t));
  const isAdmin = () => { const st = window.authState && window.authState(); return !!(st && st.user && st.user.role === 'admin'); };
  const today = () => window.plantClock.isoDate();
  const addDays = (iso, n) => { const d = new Date(iso + 'T00:00:00Z'); d.setUTCDate(d.getUTCDate() + n); return d.toISOString().slice(0, 10); };
  const fmtNum = n => (Math.round(n * 10) / 10).toLocaleString('ru-RU');
  const KEY = 'reports:state';

  const state = { screen: null, from: '', to: '' };
  let screens = [];
  let token = 0;

  async function api(url, opts) {
    const r = await fetch(url, opts);
    if (!r.ok) {
      let msg = `Ошибка ${r.status}`;
      try { const j = await r.json(); if (j.detail) msg = j.detail; } catch { /* оставим код */ }
      throw new Error(msg);
    }
    return r.json();
  }

  const saveState = () => { try { localStorage.setItem(KEY, JSON.stringify(state)); } catch { /* без сохранения */ } };
  const loadState = () => { try { return JSON.parse(localStorage.getItem(KEY)) || {}; } catch { return {}; } };

  const status = x => {
    if (x.last_error) return `<span class="rep-err">${esc(x.last_error)}</span>`;
    if (x.last_saved_at) return `Сохранён ${esc(window.plantClock.fmt(x.last_saved_at, true))}`;
    return '<span class="dim">пока не сохранялся</span>';
  };

  // ---------- отчёт за выбранный период ----------
  const fmtD = iso => iso.split('-').reverse().join('.');
  const pct1 = v => (v === null || v === undefined ? '—' : `${Math.round(v * 100)}%`);
  const tile = (label, val, tip, cls = '') => `<div class="ch-tile ${cls}" title="${esc(tip)}"><span>${esc(label)}</span><b>${esc(val)}</b></div>`;
  const table = (heads, rows, cls = '') => `<table class="users-table rep-prev-t ${cls}"><thead><tr>${heads.map(h => `<th>${esc(h)}</th>`).join('')}</tr></thead>
    <tbody>${rows.map(r => `<tr>${r.map(v => `<td>${esc(v)}</td>`).join('')}</tr>`).join('')}</tbody></table>`;
  const card = (title, tip, body) => `<section class="ch-card rep-table-card"><header><h3 title="${esc(tip)}">${esc(title)}</h3></header><div class="rep-prev-table">${body}</div></section>`;

  // один день: строки отчёта дня и график по часам (как в файле Excel)
  async function drawDay(my, q) {
    const d = await api(`/api/kpi/screens/${state.screen}/day-preview?date=${state.from}`);
    if (my !== token) return;
    q('file').textContent = d.file;
    q('chart-title').textContent = 'План и факт по часам';
    q('chart-title').title = 'Плановое и фактическое количество кузовов по часовым интервалам дня; линия показывает % выполнения';
    if (!d.intervals.length) { q('tiles').innerHTML = ''; q('chart').innerHTML = ''; q('tables').innerHTML = '<div class="empty">За этот день нет данных: отчёт не создаётся</div>'; return; }
    const plan = d.intervals.reduce((s, x) => s + x.plan, 0), fact = d.intervals.reduce((s, x) => s + x.fact, 0);
    const seen = new Set();
    let down = 0;
    d.rows.forEach(r => { if (!seen.has(r[2])) { seen.add(r[2]); down += Number(r[10]) || 0; } });
    q('tiles').innerHTML = tile('План', plan, 'Плановое количество кузовов за день') + tile('Факт', fact, 'Фактическое количество кузовов за день', 'ch-ok')
      + tile('Выполнение', plan ? pct1(fact / plan) : '—', 'Факт делённый на план') + tile('Простой, мин', fmtNum(down), 'Минуты простоя за день (отставание от плана)', 'ch-bad');
    const days = d.intervals.map(x => ({ label: x.label, plan: x.plan, fact: x.fact, pct: x.plan ? x.fact / x.plan : null }));
    q('chart').innerHTML = window.andonChart ? window.andonChart.planFact(days, Math.max(360, q('chart').clientWidth || 640)) : '';
    const rows = d.rows.slice(0, 500);
    q('tables').innerHTML = card('Таблица отчёта', 'Строки отчёта: по строке на причину простоя, интервалы без простоя одной строкой', table(d.headers, rows, 'rep-day')
      + (d.rows.length > rows.length ? `<div class="hint">Показаны первые ${rows.length} из ${d.rows.length} строк, остальные в файле Excel</div>` : ''));
  }

  // несколько дней: итоги по дням, причины и станции (как листы сводного отчёта)
  async function drawPeriod(my, q) {
    const d = await api(`/api/kpi/charts?screen_id=${state.screen}&from=${state.from}&to=${state.to}`);
    if (my !== token) return;
    q('file').textContent = `Период: ${fmtD(state.from)} — ${fmtD(state.to)}`;
    q('chart-title').textContent = 'План, факт и % выполнения по дням';
    q('chart-title').title = 'Столбцы: план и факт кузовов за день; линия: процент выполнения плана (правая шкала)';
    if (!d.days.length) { q('tiles').innerHTML = ''; q('chart').innerHTML = ''; q('tables').innerHTML = '<div class="empty">За этот период нет данных по линии</div>'; return; }
    const sum = k => d.days.reduce((s, x) => s + x[k], 0);
    const plan = sum('plan'), fact = sum('fact'), down = sum('downtime'), desc = sum('described');
    q('tiles').innerHTML = tile('Дней с данными', d.days.length, 'Сколько дней периода есть данные по линии') + tile('План', plan, 'Плановое количество кузовов за период')
      + tile('Факт', fact, 'Фактическое количество кузовов за период', 'ch-ok') + tile('Выполнение', plan ? pct1(fact / plan) : '—', 'Факт делённый на план')
      + tile('Простой, мин', fmtNum(down), 'Минуты простоя за период (отставание от плана)', 'ch-bad') + tile('Не описано, мин', fmtNum(Math.max(0, down - desc)), 'Минуты простоя, для которых ещё не указана причина');
    q('chart').innerHTML = window.andonChart ? window.andonChart.planFact(d.days, Math.max(360, q('chart').clientWidth || 640)) : '';
    const totalR = d.reasons.reduce((s, x) => s + x.min, 0), totalS = d.stations.reduce((s, x) => s + x.min, 0);
    q('tables').innerHTML =
      card('По дням', 'Итоги по каждому дню периода: план, факт, выполнение и простой', table(['Дата', 'План', 'Факт', '±', '% выполнения', 'Простой, мин', 'Описано, мин', 'Не описано, мин'],
        d.days.map(x => [fmtD(x.date), x.plan, x.fact, x.fact - x.plan, pct1(x.pct), fmtNum(x.downtime), fmtNum(x.described), fmtNum(Math.max(0, x.downtime - x.described))])))
      + card('Причины простоя', 'Минуты простоя по причинам за период, от самых долгих', table(['Причина', 'Минут', 'Доля'], d.reasons.map(x => [x.name, fmtNum(x.min), totalR ? pct1(x.min / totalR) : '—'])))
      + card('Простой по станциям', 'Минуты простоя по станциям за период, от самых долгих', table(['Участок', 'Станция', 'Минут', 'Доля'], d.stations.map(x => [x.area, x.station, fmtNum(x.min), totalS ? pct1(x.min / totalS) : '—'])));
  }

  async function loadReport() {
    const my = ++token;
    const q = s => pane.querySelector(`[data-f="${s}"]`);
    q('tables').innerHTML = '<div class="empty">Загрузка…</div>';
    markPreset();
    try {
      if (state.from === state.to) await drawDay(my, q); else await drawPeriod(my, q);
      if (my === token && window.attachColWidths) q('tables').querySelectorAll('table').forEach((t, i) => window.attachColWidths(t, `report-${state.from === state.to ? 'day' : 'p' + i}`));
    } catch (ex) { if (my === token) q('tables').innerHTML = `<div class="empty">${esc(ex.message)}</div>`; }
  }

  const presets = () => {
    const t = today();
    const pm = addDays(t.slice(0, 8) + '01', -1);
    return { day: [t, t], week: [addDays(t, -6), t], month: [t.slice(0, 8) + '01', t], prev: [pm.slice(0, 8) + '01', pm] };
  };
  function markPreset() {
    const p = presets();
    pane.querySelectorAll('[data-p]').forEach(b => b.classList.toggle('on', p[b.dataset.p][0] === state.from && p[b.dataset.p][1] === state.to));
  }

  // ---------- настройка автосохранения (администратор) ----------
  async function autosaveHtml() {
    if (!isAdmin()) return '';
    let data;
    try { data = await api('/api/reports/settings'); } catch (ex) { return `<div class="empty">${esc(ex.message)}</div>`; }
    return `<details class="rep-auto">
      <summary title="Автоматическое сохранение отчётов дня по линиям в папку на ПК с сервисом (только администратор)">Автосохранение отчётов</summary>
      <p class="hint">Отчёт дня по каждой линии сохраняется в Excel в папку на ПК, где запущен сервис: <b class="rep-dir">${esc(data.dir)}</b> (внутри папки «год-месяц», затем линия). В первый день месяца в то же время сохраняется месячный отчёт с графиками за прошлый месяц.</p>
      <table class="rep-table"><thead><tr>
        <th title="Включить автоматическое сохранение отчётов для линии">Вкл.</th>
        <th title="Линия (экран Andon)">Линия</th>
        <th title="Во сколько каждый день сохранять отчёт: после окончания работы линии, когда данные за день уже полные">Время сохранения</th>
        <th title="Когда отчёт последний раз сохранялся автоматически или причина, по которой не получилось">Состояние</th>
        <th title="Сохранить отчёт сегодняшнего дня прямо сейчас, не дожидаясь указанного времени"></th>
      </tr></thead><tbody>${data.screens.map(x => `<tr data-id="${x.id}">
        <td><input type="checkbox" data-f="enabled"${x.enabled ? ' checked' : ''} title="Сохранять отчёты линии ${esc(x.name)} каждый день автоматически"></td>
        <td class="rep-name">${esc(x.name)}</td>
        <td><input type="time" data-f="time" value="${esc(x.save_time)}" title="Время сохранения отчёта линии ${esc(x.name)}; рекомендуется ${esc(x.default_time)}: конец последнего интервала плюс 15 минут"></td>
        <td class="rep-st" data-f="st">${status(x)}</td>
        <td class="rep-btns"><button type="button" class="dt-sec" data-act="now" title="Сохранить отчёт линии ${esc(x.name)} за сегодня сейчас">Сохранить сейчас</button></td></tr>`).join('')}</tbody></table>
      <div class="rep-actions"><button type="button" data-act="save" title="Сохранить включение и время автосохранения по всем линиям">Сохранить настройки</button></div>
    </details>`;
  }

  function bindAutosave() {
    const save = pane.querySelector('[data-act="save"]');
    if (!save) return;
    save.addEventListener('click', async e => {
      const b = e.currentTarget;
      b.disabled = true;
      try {
        for (const tr of pane.querySelectorAll('.rep-table tbody tr')) {
          const time = tr.querySelector('[data-f="time"]').value;
          if (!time) throw new Error(`Укажите время для линии «${tr.querySelector('.rep-name').textContent}»`);
          await api(`/api/reports/settings/${tr.dataset.id}`, {
            method: 'PUT', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled: tr.querySelector('[data-f="enabled"]').checked, save_time: time }),
          });
        }
        toast('Настройки автосохранения сохранены');
      } catch (ex) { toast(ex.message); }
      b.disabled = false;
    });
    pane.querySelectorAll('[data-act="now"]').forEach(b => b.addEventListener('click', async () => {
      const tr = b.closest('tr');
      b.disabled = true;
      try {
        const r = await api(`/api/reports/save-now/${tr.dataset.id}`, { method: 'POST' });
        tr.querySelector('[data-f="st"]').innerHTML = `Сохранено: <span class="rep-file">${esc(r.file)}</span>`;
      } catch (ex) { tr.querySelector('[data-f="st"]').innerHTML = `<span class="rep-err">${esc(ex.message)}</span>`; }
      b.disabled = false;
    }));
  }

  // ---------- вкладка ----------
  async function render() {
    if (!screens.length) {
      try { screens = (await api('/api/kpi/screens')).filter(s => s.template === 'body_counter' || s.template === 'fl_counter'); } catch { toast('Не удалось загрузить список линий'); return; }
      if (!screens.length) { pane.innerHTML = '<div class="empty">Нет линий с данными для отчёта</div>'; return; }
    }
    const saved = loadState();
    const ids = screens.map(s => s.id);
    const andon = Number(localStorage.getItem('andonScreen'));
    if (!ids.includes(state.screen)) state.screen = ids.includes(saved.screen) ? saved.screen : (ids.includes(andon) ? andon : ids[0]);
    if (!state.from) { state.from = saved.from && saved.from <= today() ? saved.from : today(); state.to = saved.to && saved.to <= today() && saved.to >= state.from ? saved.to : state.from; }

    pane.innerHTML = `<div class="rep-pane">
      <div class="ch-bar">
        <label title="Линия, по которой показан отчёт: бренд (Main Line) или линия Finish Line">Линия
          <select data-f="screen" title="Выберите линию для отчёта">${screens.map(s => `<option value="${s.id}">${esc(s.name)}</option>`).join('')}</select></label>
        <button type="button" class="dt-sec" data-f="prev" title="Сдвинуть период назад на его длину">◀</button>
        <label title="Первый день периода">С <input type="date" data-f="from" max="${today()}" title="Первый день периода"></label>
        <label title="Последний день периода (включительно)">По <input type="date" data-f="to" max="${today()}" title="Последний день периода"></label>
        <button type="button" class="dt-sec" data-f="next" title="Сдвинуть период вперёд на его длину (не позже сегодняшнего дня)">▶</button>
        <span class="ch-presets">
          <button type="button" class="dt-sec" data-p="day" title="Только сегодняшний день: отчёт по часам">День</button>
          <button type="button" class="dt-sec" data-p="week" title="Последние 7 дней, включая сегодня">Неделя</button>
          <button type="button" class="dt-sec" data-p="month" title="С первого числа текущего месяца по сегодня">Месяц</button>
          <button type="button" class="dt-sec" data-p="prev" title="Весь прошлый календарный месяц">Прошлый месяц</button>
        </span>
        <button type="button" data-f="xlsx" title="Скачать отчёт за выбранный период в Excel: один день даёт таблицу дня, период даёт сводный отчёт с графиками">⬇ Excel</button>
        <span class="rep-prev-file" data-f="file"></span>
      </div>
      <div class="ch-tiles" data-f="tiles"></div>
      <section class="ch-card">
        <header><h3 data-f="chart-title"></h3></header>
        <div class="ch-legend"><i class="ch-l-plan"></i>План<i class="ch-l-fact"></i>Факт<i class="ch-l-pct"></i>% выполнения</div>
        <div class="ch-body" data-f="chart"></div>
      </section>
      <div data-f="tables"></div>
      <div data-f="auto"></div>
    </div>`;
    const q = s => pane.querySelector(`[data-f="${s}"]`);
    q('screen').value = String(state.screen);
    q('from').value = state.from;
    q('to').value = state.to;
    const go = () => { q('from').value = state.from; q('to').value = state.to; saveState(); loadReport(); };
    const setRange = (f, t) => { state.from = f; state.to = t < f ? f : t; go(); };
    q('screen').addEventListener('change', () => { state.screen = Number(q('screen').value); go(); });
    q('from').addEventListener('change', () => { if (q('from').value) setRange(q('from').value, state.to < q('from').value ? q('from').value : state.to); });
    q('to').addEventListener('change', () => { if (q('to').value) setRange(state.from > q('to').value ? q('to').value : state.from, q('to').value); });
    pane.querySelectorAll('[data-p]').forEach(b => b.addEventListener('click', () => { const r = presets()[b.dataset.p]; setRange(r[0], r[1]); }));
    const span = () => Math.round((new Date(state.to) - new Date(state.from)) / 86400000) + 1;
    q('prev').addEventListener('click', () => { const n = span(); setRange(addDays(state.from, -n), addDays(state.to, -n)); });
    q('next').addEventListener('click', () => {
      const n = span();
      if (state.to >= today()) return;
      const to = addDays(state.to, n) > today() ? today() : addDays(state.to, n);
      setRange(addDays(state.from, n) > to ? to : addDays(state.from, n), to);
    });
    q('xlsx').addEventListener('click', async e => {
      const b = e.currentTarget; b.disabled = true;
      const url = state.from === state.to ? `/api/kpi/screens/${state.screen}/day.xlsx?date=${state.from}&src=reports` : `/api/kpi/screens/${state.screen}/period.xlsx?from=${state.from}&to=${state.to}&src=reports`;
      try { await window.xlsxDownload(url); } catch (ex) { toast(ex.message); }
      b.disabled = false;
    });
    loadReport();
    const auto = await autosaveHtml();
    if (auto) { q('auto').innerHTML = auto; bindAutosave(); }
  }

  tab.addEventListener('click', render);
  window.addEventListener('resize', () => { if (pane.classList.contains('active') && pane.querySelector('[data-f="chart"] svg')) loadReport(); });
})();
