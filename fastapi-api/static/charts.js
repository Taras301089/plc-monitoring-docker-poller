// Вкладка «Графики»: показатели линии за период (план/факт, простои, причины, станции) и выгрузка каждого графика в Excel
(() => {
  'use strict';

  const pane = document.querySelector('#charts');
  const tabBtn = document.querySelector('.tab[data-tab="charts"]');
  if (!pane || !tabBtn) return;

  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const toast = t => (typeof showToast === 'function' ? showToast(t) : alert(t));
  const today = () => window.plantClock.isoDate();
  const addDays = (iso, n) => { const d = new Date(iso + 'T00:00:00Z'); d.setUTCDate(d.getUTCDate() + n); return d.toISOString().slice(0, 10); };
  const firstOfMonth = iso => iso.slice(0, 8) + '01';
  const fmtDay = iso => { const [, m, d] = iso.split('-'); return `${d}.${m}`; };
  const fmtNum = n => (Math.round(n * 10) / 10).toLocaleString('ru-RU');
  const pct = v => (v === null || v === undefined ? '—' : `${Math.round(v * 100)}%`);

  const KEY = 'charts:state';
  const state = { screen: null, from: '', to: '' };
  let screens = [];
  let data = null;
  let cum = null;
  let built = false;
  let loadToken = 0;
  let loadedAt = 0;

  const CARDS = [
    { kind: 'plan_fact', title: 'План, факт и % выполнения по дням', tip: 'Столбцы: план и факт кузовов за день; линия: процент выполнения плана (правая шкала)' },
    { kind: 'downtime', title: 'Простой по дням, мин', tip: 'Сколько минут линия простаивала в каждый день (отставание от плана)' },
    { kind: 'reasons', title: 'Парето причин простоя (топ-10)', tip: 'Причины по убыванию минут простоя; линия показывает накопленную долю: сколько простоя дают первые причины' },
    { kind: 'stations', title: 'Простой по станциям (топ-15)', tip: 'Станции с наибольшими минутами простоя за период' },
    { kind: 'cumulative', title: 'Накопленное производство за день', tip: 'Линии: накопленные план и факт по интервалам смены; столбцы: разница факта и плана (ниже нуля: отстаём от плана)' },
  ];

  const presets = () => {
    const t = today();
    const pm = addDays(firstOfMonth(t), -1);
    return {
      day: [t, t], week: [addDays(t, -6), t], month: [firstOfMonth(t), t],
      prev: [firstOfMonth(pm), pm],
    };
  };

  const saveState = () => { try { localStorage.setItem(KEY, JSON.stringify(state)); } catch { /* без сохранения */ } };
  const loadState = () => { try { return JSON.parse(localStorage.getItem(KEY)) || {}; } catch { return {}; } };

  // ---------- шкалы ----------
  const niceStep = v => {
    if (v <= 0) return 1;
    const p = Math.pow(10, Math.floor(Math.log10(v)));
    const f = v / p;
    return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * p;
  };
  const axis = maxVal => {
    const step = niceStep(maxVal / 4);
    const max = Math.max(step, Math.ceil(maxVal / step) * step);
    const ticks = [];
    for (let v = 0; v <= max + step / 1000; v += step) ticks.push(v);
    return { max, ticks };
  };
  const trunc = (s, n) => (s.length > n ? s.slice(0, n - 1) + '…' : s);
  const empty = text => `<div class="ch-empty">${esc(text)}</div>`;

  // ---------- графики (SVG; цвета из переменных темы) ----------
  const frame = (W, H, inner) => `<svg class="ch-svg" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img">${inner}</svg>`;
  const gridY = (a, m, W, H, fmt, side) => a.ticks.map(v => {
    const y = m.t + (H - m.t - m.b) * (1 - v / a.max);
    return `<line class="ch-grid" x1="${m.l}" x2="${W - m.r}" y1="${y}" y2="${y}"/>` +
      (side === 'r' ? `<text class="ch-tick" x="${W - m.r + 6}" y="${y + 4}">${fmt(v)}</text>` : `<text class="ch-tick" x="${m.l - 6}" y="${y + 4}" text-anchor="end">${fmt(v)}</text>`);
  }).join('');
  const xLabels = (labels, m, W, H) => {
    const n = labels.length, iw = W - m.l - m.r, gw = iw / n;
    const step = Math.max(1, Math.ceil(n / Math.max(1, Math.floor(iw / 48))));
    return labels.map((t, i) => (i % step ? '' :
      `<text class="ch-tick" x="${m.l + gw * i + gw / 2}" y="${H - m.b + 16}" text-anchor="middle">${esc(t)}</text>`)).join('');
  };

  const lab = d => d.label || fmtDay(d.date);
  window.andonChart = { planFact: (days, W) => chartPlanFact(days, W) };   // для предпросмотра отчёта на вкладке «Отчёты»

  function chartPlanFact(days, W) {
    if (!days.length) return empty('Нет данных за период');
    const H = 300, m = { l: 46, r: 46, t: 22, b: 34 };
    const n = days.length, iw = W - m.l - m.r, ih = H - m.t - m.b, gw = iw / n, bw = Math.max(3, Math.min(28, gw * 0.36));
    const ay = axis(Math.max(...days.map(d => Math.max(d.plan, d.fact)), 1));
    const maxPct = Math.max(1, ...days.map(d => d.pct || 0));
    const ap = axis(maxPct * 100);
    const yv = v => m.t + ih * (1 - v / ay.max);
    const yp = v => m.t + ih * (1 - v / ap.max);
    let bars = '', pts = [], dots = '';
    days.forEach((d, i) => {
      const x0 = m.l + gw * i + gw / 2;
      const tip = `${lab(d)}: план ${d.plan}, факт ${d.fact}, выполнение ${pct(d.pct)}`;
      bars += `<rect class="ch-plan" x="${x0 - bw - 1}" y="${yv(d.plan)}" width="${bw}" height="${ih - (yv(d.plan) - m.t)}"/>` +
        `<rect class="ch-fact" x="${x0 + 1}" y="${yv(d.fact)}" width="${bw}" height="${ih - (yv(d.fact) - m.t)}"/>`;
      if (gw >= 46) bars += `<text class="ch-val" x="${x0 + 1 + bw / 2}" y="${yv(d.fact) - 4}" text-anchor="middle">${d.fact}</text>`;
      if (d.pct !== null) { pts.push(`${x0},${yp(d.pct * 100)}`); dots += `<circle class="ch-dot" cx="${x0}" cy="${yp(d.pct * 100)}" r="3.5"/>`; }
      bars += `<rect class="ch-hit" x="${x0 - gw / 2}" y="${m.t}" width="${gw}" height="${ih}"><title>${esc(tip)}</title></rect>`;
    });
    return frame(W, H, gridY(ay, m, W, H, v => v, 'l') +
      ap.ticks.map(v => `<text class="ch-tick ch-tick-r" x="${W - m.r + 6}" y="${yp(v) + 4}">${v}%</text>`).join('') +
      bars + (pts.length > 1 ? `<polyline class="ch-line" points="${pts.join(' ')}"/>` : '') + dots + xLabels(days.map(lab), m, W, H));
  }

  function chartDowntime(days, W) {
    if (!days.length) return empty('Нет данных за период');
    const H = 260, m = { l: 46, r: 16, t: 22, b: 34 };
    const n = days.length, iw = W - m.l - m.r, ih = H - m.t - m.b, gw = iw / n, bw = Math.max(4, Math.min(34, gw * 0.62));
    const ay = axis(Math.max(...days.map(d => d.downtime), 1));
    const yv = v => m.t + ih * (1 - v / ay.max);
    let bars = '';
    days.forEach((d, i) => {
      const x0 = m.l + gw * i + gw / 2;
      const tip = `${fmtDay(d.date)}: простой ${fmtNum(d.downtime)} мин, описано ${fmtNum(d.described)} мин`;
      bars += `<rect class="ch-down" x="${x0 - bw / 2}" y="${yv(d.downtime)}" width="${bw}" height="${ih - (yv(d.downtime) - m.t)}"/>`;
      if (gw >= 40 && d.downtime > 0) bars += `<text class="ch-val" x="${x0}" y="${yv(d.downtime) - 4}" text-anchor="middle">${Math.round(d.downtime)}</text>`;
      bars += `<rect class="ch-hit" x="${x0 - gw / 2}" y="${m.t}" width="${gw}" height="${ih}"><title>${esc(tip)}</title></rect>`;
    });
    return frame(W, H, gridY(ay, m, W, H, v => v, 'l') + bars + xLabels(days.map(d => fmtDay(d.date)), m, W, H));
  }

  function chartPareto(reasons, W) {
    const rows = reasons.slice(0, 10);
    if (!rows.length) return empty('Простоев с причинами за период нет');
    const total = reasons.reduce((s, r) => s + r.min, 0);
    const H = 320, m = { l: 46, r: 46, t: 22, b: 96 };
    const n = rows.length, iw = W - m.l - m.r, ih = H - m.t - m.b, gw = iw / n, bw = Math.max(8, Math.min(44, gw * 0.6));
    const ay = axis(Math.max(...rows.map(r => r.min), 1));
    const yv = v => m.t + ih * (1 - v / ay.max);
    const yp = v => m.t + ih * (1 - v);
    let bars = '', pts = [], dots = '', labels = '', cum = 0;
    rows.forEach((r, i) => {
      const x0 = m.l + gw * i + gw / 2;
      cum += r.min;
      const share = total ? cum / total : 0;
      bars += `<rect class="ch-down" x="${x0 - bw / 2}" y="${yv(r.min)}" width="${bw}" height="${ih - (yv(r.min) - m.t)}"/>` +
        `<text class="ch-val" x="${x0}" y="${yv(r.min) - 4}" text-anchor="middle">${Math.round(r.min)}</text>` +
        `<rect class="ch-hit" x="${x0 - gw / 2}" y="${m.t}" width="${gw}" height="${ih}"><title>${esc(`${r.name}: ${fmtNum(r.min)} мин (${pct(total ? r.min / total : 0)}), накоплено ${pct(share)}`)}</title></rect>`;
      pts.push(`${x0},${yp(share)}`);
      dots += `<circle class="ch-dot ch-dot-b" cx="${x0}" cy="${yp(share)}" r="3.5"/>`;
      labels += `<text class="ch-tick" transform="translate(${x0 + 4} ${H - m.b + 12}) rotate(-35)" text-anchor="end">${esc(trunc(r.name, 18))}</text>`;
    });
    const rticks = [0, .25, .5, .75, 1].map(v => `<text class="ch-tick ch-tick-r" x="${W - m.r + 6}" y="${yp(v) + 4}">${Math.round(v * 100)}%</text>`).join('');
    return frame(W, H, gridY(ay, m, W, H, v => v, 'l') + rticks + bars + `<polyline class="ch-line ch-line-b" points="${pts.join(' ')}"/>` + dots + labels);
  }

  function chartStations(stations, W) {
    const rows = stations.slice(0, 15);
    if (!rows.length) return empty('Простоев по станциям за период нет');
    const m = { l: 190, r: 56, t: 8, b: 26 }, rh = 26;
    const H = m.t + m.b + rows.length * rh, iw = W - m.l - m.r;
    const ax = axis(Math.max(...rows.map(r => r.min), 1));
    const xv = v => m.l + iw * (v / ax.max);
    let g = ax.ticks.map(v => `<line class="ch-grid" x1="${xv(v)}" x2="${xv(v)}" y1="${m.t}" y2="${H - m.b}"/><text class="ch-tick" x="${xv(v)}" y="${H - m.b + 16}" text-anchor="middle">${v}</text>`).join('');
    rows.forEach((r, i) => {
      const y = m.t + i * rh, bh = rh * 0.62;
      const name = r.area && r.area !== '—' ? `${r.station} (${r.area})` : r.station;
      g += `<text class="ch-tick ch-tick-l" x="${m.l - 8}" y="${y + rh / 2 + 4}" text-anchor="end">${esc(trunc(name, 26))}</text>` +
        `<rect class="ch-fact" x="${m.l}" y="${y + (rh - bh) / 2}" width="${Math.max(1, xv(r.min) - m.l)}" height="${bh}"/>` +
        `<text class="ch-val" x="${xv(r.min) + 5}" y="${y + rh / 2 + 4}">${Math.round(r.min)}</text>` +
        `<rect class="ch-hit" x="0" y="${y}" width="${W}" height="${rh}"><title>${esc(`${name}: ${fmtNum(r.min)} мин`)}</title></rect>`;
    });
    return frame(W, H, g);
  }

  // накопленный план и факт линиями, разница столбцами от нулевой линии (одна шкала)
  function chartCumulative(rows, W) {
    if (!rows.length) return empty('Нет данных за выбранный день');
    const H = 300, m = { l: 46, r: 16, t: 22, b: 34 };
    const n = rows.length, iw = W - m.l - m.r, ih = H - m.t - m.b, gw = iw / n, bw = Math.max(4, Math.min(30, gw * 0.5));
    const top = Math.max(...rows.map(r => Math.max(r.cum_plan, r.cum_fact)), 1);
    const low = Math.min(0, ...rows.map(r => r.diff));
    const step = niceStep((top - low) / 5);
    const max = Math.ceil(top / step) * step, min = Math.floor(low / step) * step;
    const ticks = [];
    for (let v = min; v <= max + step / 1000; v += step) ticks.push(v);
    const yv = v => m.t + ih * (1 - (v - min) / (max - min));
    let g = ticks.map(v => `<line class="ch-grid" x1="${m.l}" x2="${W - m.r}" y1="${yv(v)}" y2="${yv(v)}"/><text class="ch-tick" x="${m.l - 6}" y="${yv(v) + 4}" text-anchor="end">${v}</text>`).join('');
    const pp = [], pf = [];
    let bars = '', dots = '';
    rows.forEach((r, i) => {
      const x0 = m.l + gw * i + gw / 2;
      const y0 = yv(0), yd = yv(r.diff);
      const tip = `${r.label}: план ${r.plan}, факт ${r.fact}; накоплено план ${r.cum_plan}, факт ${r.cum_fact}, разница ${r.diff > 0 ? '+' : ''}${r.diff}`;
      bars += `<rect class="${r.diff < 0 ? 'ch-down' : 'ch-ahead'}" x="${x0 - bw / 2}" y="${Math.min(y0, yd)}" width="${bw}" height="${Math.max(1, Math.abs(y0 - yd))}"/>`;
      pp.push(`${x0},${yv(r.cum_plan)}`); pf.push(`${x0},${yv(r.cum_fact)}`);
      dots += `<circle class="ch-dot ch-dot-b" cx="${x0}" cy="${yv(r.cum_plan)}" r="3.5"/><circle class="ch-dot ch-dot-f" cx="${x0}" cy="${yv(r.cum_fact)}" r="3.5"/>`;
      if (gw >= 46) dots += `<text class="ch-val" x="${x0}" y="${yv(Math.max(r.cum_plan, r.cum_fact)) - 8}" text-anchor="middle">${r.cum_fact}</text>`;
      dots += `<rect class="ch-hit" x="${x0 - gw / 2}" y="${m.t}" width="${gw}" height="${ih}"><title>${esc(tip)}</title></rect>`;
    });
    return frame(W, H, g + bars + `<polyline class="ch-line ch-line-b" points="${pp.join(' ')}"/><polyline class="ch-line ch-line-f" points="${pf.join(' ')}"/>` + dots + xLabels(rows.map(r => r.label), m, W, H));
  }

  // ---------- каркас вкладки ----------
  function summaryTiles(d) {
    const tp = d.days.reduce((s, x) => s + x.plan, 0), tf = d.days.reduce((s, x) => s + x.fact, 0);
    const dt = d.days.reduce((s, x) => s + x.downtime, 0), ds = d.days.reduce((s, x) => s + x.described, 0);
    const t = (label, val, tip, cls = '') => `<div class="ch-tile ${cls}" title="${esc(tip)}"><span>${esc(label)}</span><b>${esc(val)}</b></div>`;
    return t('Дней с данными', d.days.length, 'Сколько дней периода есть данные по линии') +
      t('План', tp, 'Плановое количество кузовов за период') + t('Факт', tf, 'Фактическое количество кузовов за период', 'ch-ok') +
      t('Выполнение', tp ? pct(tf / tp) : '—', 'Факт делённый на план') +
      t('Простой, мин', fmtNum(dt), 'Минуты простоя за период (отставание от плана)', 'ch-bad') +
      t('Не описано, мин', fmtNum(Math.max(0, dt - ds)), 'Минуты простоя, для которых ещё не указана причина');
  }

  function build() {
    const p = presets();
    pane.innerHTML = `
      <div class="ch-bar">
        <label title="Линия, по которой строятся графики: бренд (Main Line) или линия Finish Line">Линия
          <select id="ch-screen" title="Выберите линию для графиков"></select></label>
        <label title="Первый день периода">С <input type="date" id="ch-from" max="${today()}" title="Первый день периода"></label>
        <label title="Последний день периода (включительно)">По <input type="date" id="ch-to" max="${today()}" title="Последний день периода"></label>
        <span class="ch-presets">
          <button type="button" class="dt-sec" data-p="day" title="Только сегодняшний день">День</button>
          <button type="button" class="dt-sec" data-p="week" title="Последние 7 дней, включая сегодня">Неделя</button>
          <button type="button" class="dt-sec" data-p="month" title="С первого числа текущего месяца по сегодня">Месяц</button>
          <button type="button" class="dt-sec" data-p="prev" title="Весь прошлый календарный месяц">Прошлый месяц</button>
        </span>
        <button type="button" id="ch-report" class="dt-sec" title="Скачать полный отчёт по линии за период: сводка с графиками, таблицы по дням, причинам и станциям, список простоев">⬇ Отчёт за период</button>
      </div>
      <div class="ch-tiles" id="ch-tiles"></div>
      <div class="ch-grid-cards">${CARDS.map(c => `
        <section class="ch-card" data-kind="${c.kind}">
          <header><h3 title="${esc(c.tip)}">${esc(c.title)}</h3>
            <button type="button" class="dt-sec ch-xl" data-kind="${c.kind}" title="Выгрузить в Excel данные этого графика и сам график (диаграмма Excel) за выбранный период">⬇ Excel</button></header>
          ${c.kind === 'cumulative' ? '<div class="ch-legend"><i class="ch-l-plan"></i>Накопленный план<i class="ch-l-fact"></i>Накопленный факт<i class="ch-l-down"></i>Отставание от плана<i class="ch-l-ahead"></i>Опережение плана</div>' : ''}
          ${c.kind === 'plan_fact' ? '<div class="ch-legend"><i class="ch-l-plan"></i>План<i class="ch-l-fact"></i>Факт<i class="ch-l-pct"></i>% выполнения</div>' : ''}
          <div class="ch-body" data-body="${c.kind}"></div>
        </section>`).join('')}</div>`;
    const sel = pane.querySelector('#ch-screen');
    sel.innerHTML = screens.map(s => `<option value="${s.id}">${esc(s.name)}</option>`).join('');
    sel.value = String(state.screen);
    pane.querySelector('#ch-from').value = state.from;
    pane.querySelector('#ch-to').value = state.to;
    sel.addEventListener('change', () => { state.screen = Number(sel.value); saveState(); load(); });
    const onDates = () => {
      const f = pane.querySelector('#ch-from').value, t = pane.querySelector('#ch-to').value;
      if (!f || !t || t < f) return;
      state.from = f; state.to = t; saveState(); load();
    };
    pane.querySelector('#ch-from').addEventListener('change', onDates);
    pane.querySelector('#ch-to').addEventListener('change', onDates);
    pane.querySelectorAll('[data-p]').forEach(b => b.addEventListener('click', () => {
      [state.from, state.to] = presets()[b.dataset.p];
      pane.querySelector('#ch-from').value = state.from;
      pane.querySelector('#ch-to').value = state.to;
      saveState(); load();
    }));
    pane.querySelector('#ch-report').addEventListener('click', e => exportXlsx(e.currentTarget, `/api/kpi/screens/${state.screen}/period.xlsx?from=${state.from}&to=${state.to}&src=charts`));
    pane.querySelectorAll('.ch-xl').forEach(b => b.addEventListener('click', e =>
      exportXlsx(e.currentTarget, `/api/kpi/screens/${state.screen}/chart.xlsx?kind=${b.dataset.kind}&from=${state.from}&to=${state.to}&src=charts`)));
    built = true;
  }

  async function exportXlsx(btn, url) {
    btn.disabled = true;
    try { await window.xlsxDownload(url); } catch (ex) { toast(ex.message); }
    btn.disabled = false;
  }

  function draw() {
    if (!built || !data) return;
    const W = Math.max(320, Math.floor(pane.querySelector('[data-body="plan_fact"]').clientWidth) || 560);
    const body = k => pane.querySelector(`[data-body="${k}"]`);
    pane.querySelector('#ch-tiles').innerHTML = summaryTiles(data);
    body('plan_fact').innerHTML = chartPlanFact(data.days, W);
    body('downtime').innerHTML = chartDowntime(data.days, W);
    body('reasons').innerHTML = chartPareto(data.reasons, W);
    body('stations').innerHTML = chartStations(data.stations, W);
    drawCumulative(W);
  }

  // график за один день: при периоде из нескольких дней скрыт, вместо него пояснение
  function drawCumulative(W) {
    const card = pane.querySelector('[data-kind="cumulative"]'), b = card.querySelector('[data-body="cumulative"]');
    const one = state.from === state.to;
    card.querySelector('.ch-xl').hidden = !one;
    card.querySelector('.ch-legend').hidden = !one;
    if (!one) { b.innerHTML = empty('График показывается за один день: выберите «День» или одну и ту же дату в полях «С» и «По»'); return; }
    b.innerHTML = cum && cum.date === state.from ? chartCumulative(cum.intervals, W) : empty('Загрузка…');
  }

  async function loadCumulative(token) {
    cum = null;
    if (state.from !== state.to) return;
    try {
      const r = await fetch(`/api/kpi/charts/cumulative?screen_id=${state.screen}&date=${state.from}`);
      if (!r.ok) throw new Error(`Ошибка ${r.status}`);
      const d = await r.json();
      if (token !== loadToken) return;
      cum = d; draw();
    } catch (ex) { if (token === loadToken) toast(ex.message); }
  }

  function markPreset() {
    const p = presets();
    pane.querySelectorAll("[data-p]").forEach(b => b.classList.toggle("on", p[b.dataset.p][0] === state.from && p[b.dataset.p][1] === state.to));
  }

  async function load() {
    markPreset();
    if (!built || state.screen === null) return;
    const token = ++loadToken;
    cum = null;
    try {
      const r = await fetch(`/api/kpi/charts?screen_id=${state.screen}&from=${state.from}&to=${state.to}`);
      if (!r.ok) {
        let msg = `Ошибка ${r.status}`;
        try { const j = await r.json(); if (j.detail) msg = j.detail; } catch { /* оставим код */ }
        throw new Error(msg);
      }
      const d = await r.json();
      if (token !== loadToken) return;
      data = d; loadedAt = Date.now();
      draw();
      loadCumulative(token);
    } catch (ex) { if (token === loadToken) toast(ex.message); }
  }

  async function show() {
    if (!built) {
      try {
        const r = await fetch('/api/kpi/screens');
        screens = (await r.json()).filter(s => s.template === 'body_counter' || s.template === 'fl_counter');
      } catch { toast('Не удалось загрузить список линий'); return; }
      if (!screens.length) { pane.innerHTML = '<div class="ch-empty">Нет линий с данными для графиков</div>'; return; }
      const saved = loadState();
      const andon = Number(localStorage.getItem('andonScreen'));
      const ids = screens.map(s => s.id);
      state.screen = ids.includes(saved.screen) ? saved.screen : (ids.includes(andon) ? andon : ids[0]);
      const p = presets().month;
      state.from = saved.from || p[0];
      state.to = saved.to || p[1];
      build();
      load();
    } else if (Date.now() - loadedAt > 30000) {
      load();
    }
  }

  tabBtn.addEventListener('click', show);
  window.addEventListener('resize', () => { if (pane.classList.contains('active')) requestAnimationFrame(draw); });
})();
