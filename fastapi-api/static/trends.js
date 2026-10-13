// Вкладка «Тренды»: «Связь с ПЛК» за период (ленты по времени), «Графики переменных» (онлайн и архив, линейка, масштаб, границы), «Запись переменных»
(function (root) {
  'use strict';

  // ================= «Графики переменных»: чистая логика (без DOM, проверяется в node) =================
  const core = (() => {
    const MIN_SPAN = 2000;                 // самое мелкое окно по времени, мс
    const p2 = n => String(n).padStart(2, '0');

    // последний индекс i, где arr[i] <= x (массив по возрастанию), иначе -1
    function lastLE(arr, x) {
      let lo = 0, hi = arr.length - 1, r = -1;
      while (lo <= hi) { const mid = (lo + hi) >> 1; if (arr[mid] <= x) { r = mid; lo = mid + 1; } else hi = mid - 1; }
      return r;
    }

    // ряд с сервера -> удобный вид; у корзин время = середина корзины, пропуск = не меньше 2,5 корзины
    function normalize(s, bucketSec) {
      const agg = s.mode === 'agg';
      const half = agg ? bucketSec * 500 : 0;
      const o = { id: s.tag_id, name: s.name, plc: s.plc_name, step: !!s.step, mode: s.mode, t: [], v: [], mn: [], mx: [], n: [],
        gapMs: Math.max((s.gap_sec || 10) * 1000, agg ? bucketSec * 2500 : 0), prior: s.prior ? { t: s.prior[0], v: s.prior[1] } : null,
        lo: s.limit_low === undefined ? null : s.limit_low, hi: s.limit_high === undefined ? null : s.limit_high, deadband: s.deadband === undefined ? null : s.deadband, stats: s.stats };
      for (const p of s.points) {
        if (agg) { o.t.push(p[0] + half); o.mn.push(p[1]); o.v.push(p[2]); o.mx.push(p[3]); o.n.push(p[4]); }
        else { o.t.push(p[0]); o.v.push(p[1]); o.mn.push(p[1]); o.mx.push(p[1]); o.n.push(1); }
      }
      return o;
    }

    // Линия ряда в данных: список отрезков, каждый список точек [t, v, min, max].
    // Ступенька: значение держится до следующей записи (начало: запись до окна; конец: до min(конец окна, endMs)), разрывов нет.
    // Ломаная: разрыв, если между соседними точками дольше gapMs.
    function lineSegments(s, from, to, endMs) {
      const n = s.t.length;
      if (s.step) {
        const pts = [];
        if (s.prior) pts.push([from, s.prior.v, s.prior.v, s.prior.v]);
        for (let i = 0; i < n; i++) if (s.t[i] >= from && s.t[i] <= to) pts.push([s.t[i], s.v[i], s.mn[i], s.mx[i]]);
        if (!pts.length) return [];
        const st = [pts[0]];
        for (let i = 1; i < pts.length; i++) { st.push([pts[i][0], pts[i - 1][1], pts[i - 1][2], pts[i - 1][3]]); st.push(pts[i]); }
        const last = pts[pts.length - 1];
        const end = Math.min(to, endMs === undefined || endMs === null ? to : endMs);
        if (end > last[0]) st.push([end, last[1], last[2], last[3]]);
        return [st];
      }
      const out = [];
      let cur = [];
      for (let i = 0; i < n; i++) {
        if (s.t[i] < from - s.gapMs || s.t[i] > to + s.gapMs) { if (cur.length) { out.push(cur); cur = []; } continue; }
        if (cur.length && s.t[i] - cur[cur.length - 1][0] > s.gapMs) { out.push(cur); cur = []; }
        cur.push([s.t[i], s.v[i], s.mn[i], s.mx[i]]);
      }
      if (cur.length) out.push(cur);
      return out;
    }

    // Участки линии за границами [lo, hi]: рёбра, у которых хотя бы один конец вне границ (у корзин сравниваются min и max)
    function outPaths(seg, lo, hi) {
      const bad = p => (lo !== null && lo !== undefined && p[2] < lo) || (hi !== null && hi !== undefined && p[3] > hi);
      const runs = [];
      let cur = null;
      if (seg.length === 1) return bad(seg[0]) ? [[seg[0]]] : [];
      for (let i = 0; i + 1 < seg.length; i++) {
        if (bad(seg[i]) || bad(seg[i + 1])) { if (!cur) { cur = [seg[i]]; runs.push(cur); } cur.push(seg[i + 1]); } else cur = null;
      }
      return runs;
    }

    // Путь SVG по отрезку: X и Y переводят время и значение в пиксели
    function pathD(seg, X, Y) {
      const r = x => Math.round(x * 10) / 10;
      return seg.map((p, i) => `${i ? 'L' : 'M'}${r(X(p[0]))},${r(Y(p[1]))}`).join('');
    }

    // Полоса min–max по отрезку (многоугольник); отрезок из одной точки не рисуется
    function bandD(seg, X, Y) {
      if (seg.length < 2) return '';
      const r = x => Math.round(x * 10) / 10;
      const up = seg.map((p, i) => `${i ? 'L' : 'M'}${r(X(p[0]))},${r(Y(p[3]))}`).join('');
      const dn = seg.slice().reverse().map(p => `L${r(X(p[0]))},${r(Y(p[2]))}`).join('');
      return `${up}${dn}Z`;
    }

    // Действующее значение на момент t: ближайшая запись слева; ступенька держит значение, ломаная после пропуска даёт null
    function valueAt(s, t, endMs) {
      const i = lastLE(s.t, t);
      if (s.step) {
        if (endMs !== undefined && endMs !== null && t > endMs) return null;
        if (i >= 0) return { t: s.t[i], v: s.v[i] };
        return s.prior ? { t: s.prior.t, v: s.prior.v } : null;
      }
      if (i < 0 || t - s.t[i] > s.gapMs) return null;
      return { t: s.t[i], v: s.v[i] };
    }

    // Минимум и максимум ряда в окне [a, b] (для подбора оси Y); ступенька учитывает и значение, действовавшее на начало окна
    function viewExtent(s, a, b, endMs) {
      let lo = Infinity, hi = -Infinity;
      const i0 = Math.max(0, lastLE(s.t, a)), n = s.t.length;
      for (let i = i0; i < n && s.t[i] <= b; i++) {
        if (s.t[i] < a && !s.step) continue;
        if (s.mn[i] < lo) lo = s.mn[i];
        if (s.mx[i] > hi) hi = s.mx[i];
      }
      if (s.step) { const h = valueAt(s, a, endMs); if (h) { lo = Math.min(lo, h.v); hi = Math.max(hi, h.v); } }
      return lo <= hi ? [lo, hi] : null;
    }

    // Ось Y по данным с полями (не от нуля); zero: от нуля; extents: список [min, max]
    function autoRange(extents, o) {
      const opt = o || {};
      let lo = Infinity, hi = -Infinity;
      for (const e of extents) { if (!e) continue; if (e[0] < lo) lo = e[0]; if (e[1] > hi) hi = e[1]; }
      if (!Number.isFinite(lo)) return null;
      if (opt.zero) { lo = Math.min(0, lo); hi = Math.max(0, hi); }
      const span = hi - lo;
      if (span < Math.abs(hi) * 1e-4 + 1e-9) {
        const pad = Math.max(Math.abs(hi) * 0.005, 0.5);
        return opt.zero ? [Math.min(0, lo), hi + pad] : [lo - pad, hi + pad];
      }
      const pad = span * (opt.pad === undefined ? 0.08 : opt.pad);
      return [opt.zero && lo === 0 ? 0 : lo - pad, hi + pad];
    }

    // Красивые деления оси: шаг 1, 2, 5 × 10^k; возвращает { ticks, step }
    function niceTicks(lo, hi, count) {
      const span = hi - lo;
      if (!(span > 0)) return { ticks: [lo], step: 1 };
      const raw = span / Math.max(1, count);
      const mag = Math.pow(10, Math.floor(Math.log10(raw)));
      const f = raw / mag;
      const step = (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * mag;
      const ticks = [];
      for (let v = Math.ceil(lo / step - 1e-9) * step; v <= hi + step * 1e-9; v += step) ticks.push(Math.round(v / step * 1e6) / 1e6 * step);
      return { ticks, step };
    }

    // Подписи оси времени по времени завода: offMs сдвигает UTC к времени завода
    function timeTicks(from, to, offMs, maxTicks) {
      const S = 1000, M = 60 * S, H = 60 * M, D = 24 * H;
      const steps = [S, 2 * S, 5 * S, 10 * S, 15 * S, 30 * S, M, 2 * M, 5 * M, 10 * M, 15 * M, 30 * M, H, 2 * H, 3 * H, 6 * H, 12 * H, D, 2 * D, 7 * D, 14 * D];
      const span = to - from;
      let step = steps[steps.length - 1];
      for (const s of steps) if (span / s <= maxTicks) { step = s; break; }
      const out = [];
      for (let t = Math.ceil((from + offMs) / step) * step - offMs; t <= to; t += step) {
        const d = new Date(t + offMs);
        const hm = `${p2(d.getUTCHours())}:${p2(d.getUTCMinutes())}`, dm = `${p2(d.getUTCDate())}.${p2(d.getUTCMonth() + 1)}`;
        let label;
        if (step >= D) label = dm;
        else if (span >= 2 * D) label = `${dm} ${hm}`;
        else if (step < M) label = `${hm}:${p2(d.getUTCSeconds())}`;
        else label = hm;
        out.push({ t, label });
      }
      return out;
    }

    // Окно просмотра внутри границ: не короче MIN_SPAN и не длиннее границ
    function clampView(v, b) {
      const span = Math.min(Math.max(v.b - v.a, MIN_SPAN), b.b - b.a);
      let a = v.a;
      if (a < b.a) a = b.a;
      if (a + span > b.b) a = b.b - span;
      return { a, b: a + span };
    }
    // Масштаб вокруг момента c: k < 1 приближает
    function zoomView(v, k, c, b) {
      const span = (v.b - v.a) * k, frac = (c - v.a) / (v.b - v.a);
      const a = c - frac * span;
      return clampView({ a, b: a + span }, b);
    }
    function panView(v, dt, b) { return clampView({ a: v.a + dt, b: v.b + dt }, b); }

    // Число для экрана: запятая, число знаков по величине; пусто -> прочерк
    function fmtVal(v) {
      if (v === null || v === undefined || Number.isNaN(v)) return '—';
      const a = Math.abs(v), d = a >= 1000 ? 0 : a >= 100 ? 1 : a >= 1 ? 2 : 3;
      return v.toLocaleString('ru-RU', { minimumFractionDigits: d, maximumFractionDigits: d });
    }
    // Время завода «дд.мм чч:мм:сс» из мс (UTC) со сдвигом offMs
    function fmtClock(ms, offMs, withDate) {
      const d = new Date(ms + offMs);
      const t = `${p2(d.getUTCHours())}:${p2(d.getUTCMinutes())}:${p2(d.getUTCSeconds())}`;
      return withDate === false ? t : `${p2(d.getUTCDate())}.${p2(d.getUTCMonth() + 1)} ${t}`;
    }
    const tsv = rows => rows.map(r => r.map(c => String(c === null || c === undefined ? '' : c).replace(/[\t\r\n]+/g, ' ')).join('\t')).join('\n');

    // Переменные по умолчанию: три фазы напряжения (Voltage L1-N, L2-N, L3-N), иначе первые три
    function defaultSelection(tags) {
      const ph = [];
      for (const t of tags) { const m = /voltage\s*L([123])\s*-\s*N/i.exec(t.name || ''); if (m) ph.push([+m[1], t.tag_id]); }
      if (ph.length) return ph.sort((x, y) => x[0] - y[0]).slice(0, 3).map(x => x[1]);
      return tags.slice(0, 3).map(t => t.tag_id);
    }
    // Цвет линии по номеру в выборе: набор --chart-1…8
    const colorVar = i => `var(--chart-${(i % 8) + 1})`;

    return { MIN_SPAN, lastLE, normalize, lineSegments, outPaths, pathD, bandD, valueAt, viewExtent, autoRange, niceTicks, timeTicks,
      clampView, zoomView, panView, fmtVal, fmtClock, tsv, defaultSelection, colorVar };
  })();
  root.trendsCore = core;
  if (typeof module !== 'undefined' && module.exports) module.exports = core;
  if (typeof document === 'undefined') return;

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
      ${gHtml()}
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
    gInit();
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
    gShow();
  }

  const prevAuth = window.onAuthChanged;
  window.onAuthChanged = () => { if (prevAuth) prevAuth(); syncAdmin(); };

  // ================= «Графики переменных» =================
  const GKEY = 'trends:graph';
  const G_MAX = 8;
  const g = {
    mode: 'online', onlineMin: 5, from: '', to: '', plcId: null, selByPlc: {}, hidden: [], zero: false, ymin: '', ymax: '', pan: false, paused: false,
    vars: null, varsErr: '', offH: 5, built: false,
    series: [], bounds: null, view: null, dataView: null, full: true, curT: null, fail: 0, token: 0, loadedAt: 0, nowMs: 0, sc: null, drag: false, raf: 0, fetchT: 0, err: '',
  };
  const gOff = () => g.offH * 3600000;
  const gNow = () => window.plantClock.now();
  const gSave = () => {
    try { localStorage.setItem(GKEY, JSON.stringify({ mode: g.mode, onlineMin: g.onlineMin, from: g.from, to: g.to, plcId: g.plcId, selByPlc: g.selByPlc, hidden: g.hidden, zero: g.zero, ymin: g.ymin, ymax: g.ymax, pan: g.pan })); } catch { /* без сохранения */ }
  };
  const gRestore = () => { try { Object.assign(g, JSON.parse(localStorage.getItem(GKEY)) || {}); } catch { /* по умолчанию */ } };
  const gPlc = () => (g.vars ? g.vars.plcs.find(p => p.plc_id === g.plcId) || null : null);
  const gSelIds = () => { const p = gPlc(); if (!p) return []; const ids = new Set(p.tags.map(t => t.tag_id)); return (g.selByPlc[g.plcId] || []).filter(i => ids.has(i)); };

  // окно «весь период»: онлайн последние N минут; архив с 00:00 первого дня до конца последнего (сегодня только до «сейчас»)
  function gFullWindow() {
    const now = gNow();
    if (g.mode === 'online') return { a: now - g.onlineMin * 60000, b: now };
    const a = Date.parse(g.from + 'T00:00:00Z') - gOff();
    const end = Date.parse(addDays(g.to, 1) + 'T00:00:00Z') - gOff();
    return { a, b: g.to === today() ? Math.min(end, Math.max(now, a + 60000)) : end };
  }
  const gPlotWidth = () => { const el = pane.querySelector('#trg-plot'); return Math.max(300, Math.floor(el ? el.clientWidth : 0) || 700); };

  function gHtml() {
    const dv = v => `<button type="button" class="dt-sec" data-gp="${v[0]}" title="${v[2]}">${v[1]}</button>`;
    return `
      <section class="ch-card trg" id="trg">
        <header><h3 title="Графики записанных переменных (например, напряжение по фазам): линейка со значениями, масштаб, границы, ступеньки для записи по изменению">Графики переменных</h3>
          <button type="button" id="trg-xl" class="dt-sec ch-xl" title="Выгрузить в Excel выбранные переменные за показанный период: статистика, график и данные">⬇ Excel</button></header>
        <div class="ch-bar trg-bar">
          <label title="ПЛК, переменные которого показываются на графике">ПЛК <select id="trg-plc" title="ПЛК, переменные которого показываются на графике"></select></label>
          <span class="ch-presets">
            <button type="button" class="dt-sec" data-gm="online" title="Живой график: последние минуты, обновляется каждые 5 секунд и прокручивается сам">Онлайн</button>
            <button type="button" class="dt-sec" data-gm="archive" title="Архив: любой период из базы (день, неделя, месяц или свои даты)">Архив</button>
          </span>
          <span class="ch-presets" id="trg-online">
            <button type="button" class="dt-sec" data-gmin="1" title="Показать последнюю 1 минуту">1 мин</button>
            <button type="button" class="dt-sec" data-gmin="5" title="Показать последние 5 минут">5 мин</button>
            <button type="button" class="dt-sec" data-gmin="15" title="Показать последние 15 минут">15 мин</button>
            <button type="button" class="dt-sec" data-gmin="60" title="Показать последний 1 час">60 мин</button>
            <button type="button" id="trg-pause" class="dt-sec" title="Остановить прокрутку, чтобы рассмотреть график; при масштабе и сдвиге пауза включается сама">⏸ Пауза</button>
          </span>
          <span class="ch-presets" id="trg-archive">
            <button type="button" id="trg-prev" class="dt-sec" title="Сдвинуть период назад на его длину">◀</button>
            <label title="Первый день периода">С <input type="date" id="trg-from" max="${today()}" title="Первый день периода"></label>
            <label title="Последний день периода, включительно; период не больше ${MAX_DAYS} дней">По <input type="date" id="trg-to" max="${today()}" title="Последний день периода"></label>
            <button type="button" id="trg-next" class="dt-sec" title="Сдвинуть период вперёд на его длину; дата не позже сегодняшней">▶</button>
            ${dv(['day', 'День', 'Только сегодняшний день'])}${dv(['week', 'Неделя', 'Последние 7 дней, включая сегодня'])}${dv(['month', 'Месяц', 'С первого числа текущего месяца по сегодня'])}${dv(['prev', 'Прошлый месяц', 'Весь прошлый календарный месяц'])}
          </span>
          <span id="trg-badge" class="live-badge off" title="Состояние запроса к серверу: красный значок означает, что данные не получены и значения скрыты">● загрузка</span>
        </div>
        <div class="trg-vars" id="trg-vars" title="Записанные аналоговые переменные выбранного ПЛК: отметьте до ${G_MAX} штук"></div>
        <div class="ch-bar trg-tools">
          <span class="ch-presets">
            <button type="button" class="dt-sec" data-gt="box" title="Мышью: выделить рамкой участок времени, чтобы приблизить его (Shift временно включает сдвиг)">Рамка</button>
            <button type="button" class="dt-sec" data-gt="pan" title="Мышью: перетаскивать график по времени (Shift временно включает рамку). Колесо мыши приближает и отдаляет">Сдвиг</button>
          </span>
          <button type="button" id="trg-reset" class="dt-sec" title="Показать весь период целиком (двойной щелчок по графику делает то же самое)">Весь период</button>
          <label title="Ось Y начинается с нуля; иначе масштаб подбирается по видимым данным"><input type="checkbox" id="trg-zero" title="Ось Y начинается с нуля; иначе масштаб подбирается по видимым данным"> Ось Y от нуля</label>
          <label title="Нижний край оси Y вручную; пусто: подбирается по данным">Y мин <input type="text" inputmode="decimal" id="trg-ymin" class="trg-num" title="Нижний край оси Y вручную; пусто: подбирается по данным"></label>
          <label title="Верхний край оси Y вручную; пусто: подбирается по данным">Y макс <input type="text" inputmode="decimal" id="trg-ymax" class="trg-num" title="Верхний край оси Y вручную; пусто: подбирается по данным"></label>
          <button type="button" id="trg-yauto" class="dt-sec" title="Снять ручные границы оси Y и вернуть подбор по данным">Y авто</button>
          <span id="trg-range" class="trg-range" title="Показанный период по времени завода"></span>
        </div>
        <div class="ch-legend trg-legend" id="trg-legend"></div>
        <div class="trg-main">
          <div class="trg-plot" id="trg-plot" title="Колесо мыши: масштаб; перетаскивание: рамка или сдвиг; двойной щелчок: весь период; на сенсорном экране: касание ставит линейку, один палец сдвигает, два пальца меняют масштаб"></div>
          <div class="trg-side">
            <div class="trg-cap"><b title="Значения всех линий в момент, где стоит линейка">Линейка</b>
              <button type="button" id="trg-cp-cur" class="dt-sec" title="Скопировать таблицу линейки в буфер обмена с табуляцией: вставляется в Excel">Копировать</button></div>
            <div class="trg-tw" id="trg-ct-w" tabindex="0" title="Таблица: Ctrl+C копирует её целиком, вставляется в Excel"></div>
          </div>
        </div>
        <div class="trg-cap"><b title="Минимум, среднее, максимум и число точек по каждой линии за показанный период">Статистика за видимый период</b>
          <button type="button" id="trg-cp-st" class="dt-sec" title="Скопировать таблицу статистики в буфер обмена с табуляцией: вставляется в Excel">Копировать</button></div>
        <div class="trg-tw" id="trg-st-w" tabindex="0" title="Таблица: Ctrl+C копирует её целиком, вставляется в Excel"></div>
      </section>`;
  }

  function gInit() {
    gRestore();
    const t = today();
    if (!g.from || !g.to || g.to > t || g.from > g.to) { g.from = presets().day[0]; g.to = presets().day[1]; }
    pane.querySelector('#trg-from').value = g.from;
    pane.querySelector('#trg-to').value = g.to;
    pane.querySelector('#trg-zero').checked = !!g.zero;
    pane.querySelector('#trg-ymin').value = g.ymin;
    pane.querySelector('#trg-ymax').value = g.ymax;
    const q = s => pane.querySelector(s);
    pane.querySelectorAll('[data-gm]').forEach(b => b.addEventListener('click', () => { g.mode = b.dataset.gm; g.paused = false; gSave(); gSyncBar(); gLoad(true); }));
    pane.querySelectorAll('[data-gmin]').forEach(b => b.addEventListener('click', () => { g.onlineMin = +b.dataset.gmin; g.mode = 'online'; g.paused = false; gSave(); gSyncBar(); gLoad(true); }));
    q('#trg-pause').addEventListener('click', () => { g.paused = !g.paused; gSyncBar(); if (!g.paused) gLoad(true); });
    const apply = (f, to) => {
      if (!f || !to || to < f) return;
      if (to > today()) to = today();
      if (daysBetween(f, to) > MAX_DAYS) { toast(`Период не больше ${MAX_DAYS} дней`); return; }
      g.from = f; g.to = to; g.mode = 'archive'; gSave();
      q('#trg-from').value = f; q('#trg-to').value = to;
      gSyncBar(); gLoad(true);
    };
    const dates = () => [q('#trg-from').value, q('#trg-to').value];
    q('#trg-from').addEventListener('change', () => apply(...dates()));
    q('#trg-to').addEventListener('change', () => apply(...dates()));
    pane.querySelectorAll('[data-gp]').forEach(b => b.addEventListener('click', () => apply(...presets()[b.dataset.gp])));
    const shift = k => {
      const n = daysBetween(g.from, g.to) * k, to = addDays(g.to, n);
      if (to > today() && k > 0) return;
      apply(addDays(g.from, n), to);
    };
    q('#trg-prev').addEventListener('click', () => shift(-1));
    q('#trg-next').addEventListener('click', () => shift(1));
    pane.querySelectorAll('[data-gt]').forEach(b => b.addEventListener('click', () => { g.pan = b.dataset.gt === 'pan'; gSave(); gSyncBar(); }));
    q('#trg-reset').addEventListener('click', () => { g.paused = false; gSyncBar(); gLoad(true); });
    q('#trg-zero').addEventListener('change', e => { g.zero = e.target.checked; gSave(); gDraw(); });
    const yIn = () => { g.ymin = q('#trg-ymin').value.trim(); g.ymax = q('#trg-ymax').value.trim(); gSave(); gDraw(); };
    q('#trg-ymin').addEventListener('change', yIn);
    q('#trg-ymax').addEventListener('change', yIn);
    q('#trg-yauto').addEventListener('click', () => { q('#trg-ymin').value = ''; q('#trg-ymax').value = ''; yIn(); });
    q('#trg-plc').addEventListener('change', e => { g.plcId = +e.target.value; gEnsureSel(); gSave(); gRenderVars(); gLoad(true); });
    q('#trg-xl').addEventListener('click', async e => {
      const b = e.currentTarget, ids = gSelIds(), w = g.mode === 'online' ? (g.view || gFullWindow()) : (g.bounds || gFullWindow());
      if (!ids.length) { toast('Отметьте хотя бы одну переменную'); return; }
      b.disabled = true;
      try {
        const plc = gPlc();
        await window.xlsxDownload(`/api/trends/series.xlsx?from=${encodeURIComponent(new Date(w.a).toISOString())}&to=${encodeURIComponent(new Date(w.b).toISOString())}&tags=${ids.join(',')}&plc=${encodeURIComponent(plc ? plc.name : '')}&src=trends`);
      } catch (ex) { toast(ex.message); }
      b.disabled = false;
    });
    const copyTable = (sel, btn) => {
      const rows = [...pane.querySelectorAll(`${sel} tr`)].map(tr => [...tr.children].map(c => c.textContent.trim()));
      const text = core.tsv(rows);
      const done = () => toast('Таблица скопирована');
      if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(done, () => toast('Не удалось скопировать'));
      else toast('Не удалось скопировать');
      if (btn) btn.blur();
    };
    q('#trg-cp-cur').addEventListener('click', e => copyTable('#trg-ct-w', e.currentTarget));
    q('#trg-cp-st').addEventListener('click', e => copyTable('#trg-st-w', e.currentTarget));
    for (const [wrap, id] of [['#trg-ct-w', '#trg-ct-w'], ['#trg-st-w', '#trg-st-w']]) {
      q(wrap).addEventListener('copy', e => { e.preventDefault(); e.clipboardData.setData('text/plain', core.tsv([...pane.querySelectorAll(`${id} tr`)].map(tr => [...tr.children].map(c => c.textContent.trim())))); });
    }
    q('#trg-legend').addEventListener('click', e => {
      const b = e.target.closest('[data-id]');
      if (!b) return;
      const id = +b.dataset.id;
      g.hidden = g.hidden.includes(id) ? g.hidden.filter(x => x !== id) : g.hidden.concat(id);
      gSave(); gDraw();
    });
    q('#trg-vars').addEventListener('change', e => {
      const cb = e.target.closest('input[data-tag]');
      if (!cb) return;
      const cur = g.selByPlc[g.plcId] || [];
      const id = +cb.dataset.tag;
      if (cb.checked && cur.length >= G_MAX) { cb.checked = false; toast(`Не больше ${G_MAX} переменных на график`); return; }
      g.selByPlc[g.plcId] = cb.checked ? cur.concat(id) : cur.filter(x => x !== id);
      gSave(); gLoad(false);
    });
    q('#trg-vars').addEventListener('input', e => {
      if (!e.target.matches('#trg-vfilter')) return;
      const f = e.target.value.trim().toLowerCase();
      pane.querySelectorAll('#trg-vars label[data-n]').forEach(l => { l.hidden = !!f && !l.dataset.n.includes(f); });
    });
    gBindPlot();
    g.built = true;
    gSyncBar();
  }

  function gEnsureSel() {
    const p = gPlc();
    if (!p) return;
    const ids = gSelIds();
    if (!ids.length) g.selByPlc[g.plcId] = core.defaultSelection(p.tags);
  }

  function gSyncBar() {
    pane.querySelectorAll('[data-gm]').forEach(b => b.classList.toggle('on', b.dataset.gm === g.mode));
    pane.querySelectorAll('[data-gmin]').forEach(b => b.classList.toggle('on', g.mode === 'online' && +b.dataset.gmin === g.onlineMin));
    pane.querySelectorAll('[data-gt]').forEach(b => b.classList.toggle('on', (b.dataset.gt === 'pan') === g.pan));
    const pr = presets();
    pane.querySelectorAll('[data-gp]').forEach(b => b.classList.toggle('on', g.mode === 'archive' && pr[b.dataset.gp][0] === g.from && pr[b.dataset.gp][1] === g.to));
    pane.querySelector('#trg-online').hidden = g.mode !== 'online';
    pane.querySelector('#trg-archive').hidden = g.mode !== 'archive';
    const pb = pane.querySelector('#trg-pause');
    pb.textContent = g.paused ? '▶ Продолжить' : '⏸ Пауза';
    pb.title = g.paused ? 'Продолжить прокрутку: график снова показывает последние минуты и обновляется' : 'Остановить прокрутку, чтобы рассмотреть график; при масштабе и сдвиге пауза включается сама';
    pb.classList.toggle('on', g.paused);
  }

  function gRenderVars() {
    const box = pane.querySelector('#trg-vars'), sel = pane.querySelector('#trg-plc');
    if (!box) return;
    if (g.varsErr) { box.innerHTML = `<div class="ch-empty">${esc(g.varsErr)}</div>`; return; }
    if (!g.vars) { box.innerHTML = '<div class="ch-empty">Загрузка…</div>'; return; }
    if (!g.vars.plcs.length) { box.innerHTML = '<div class="ch-empty">Нет записанных переменных: отметьте аналоговые переменные галочкой «Архив» в «Переменные ПЛК».</div>'; sel.innerHTML = ''; return; }
    sel.innerHTML = g.vars.plcs.map(p => `<option value="${p.plc_id}"${p.plc_id === g.plcId ? ' selected' : ''}>${esc(p.name)}</option>`).join('');
    const p = gPlc(), on = new Set(gSelIds());
    const many = p.tags.length > 12;
    box.innerHTML = (many ? '<input type="search" id="trg-vfilter" class="trg-vf" placeholder="Поиск по имени" title="Показать только переменные, в имени которых есть введённый текст (регистр не важен)">' : '') +
      '<div class="trg-vlist">' + p.tags.map(t => {
        const tip = `${t.name}${t.deadband > 0 ? `; запись по изменению (мёртвая зона ${core.fmtVal(t.deadband)}), линия ступенькой` : '; запись по времени, линия ломаной'}${t.limit_low !== null || t.limit_high !== null ? `; границы ${t.limit_low === null ? '—' : core.fmtVal(t.limit_low)} … ${t.limit_high === null ? '—' : core.fmtVal(t.limit_high)}` : ''}`;
        return `<label data-n="${esc(t.name.toLowerCase())}" title="${esc(tip)}"><input type="checkbox" data-tag="${t.tag_id}" title="${esc(tip)}"${on.has(t.tag_id) ? ' checked' : ''}> ${esc(t.name)}</label>`;
      }).join('') + '</div>';
  }

  async function gLoadVars() {
    try {
      const r = await fetch('/api/trends/vars', { signal: AbortSignal.timeout(6000) });
      if (!r.ok) throw new Error(await errText(r));
      g.vars = await r.json(); g.varsErr = '';
      g.offH = g.vars.offset_hours;
      if (!g.vars.plcs.some(p => p.plc_id === g.plcId)) {
        const def = g.vars.plcs.find(p => p.name === 'AE_WBS_2') || g.vars.plcs[0];
        g.plcId = def ? def.plc_id : null;
      }
      gEnsureSel();
    } catch (ex) {
      g.varsErr = ex.name === 'TimeoutError' ? 'Не удалось загрузить: сервер не ответил за 6 секунд' : `Не удалось загрузить: ${ex.message}`;
    }
    gRenderVars();
  }

  async function gLoad(reset) {
    if (!g.built || !g.vars) return;
    const ids = gSelIds();
    const token = ++g.token;
    if (!ids.length) { g.series = []; g.err = ''; g.fail = 0; g.view = g.view || gFullWindow(); g.bounds = gFullWindow(); g.view = { ...g.bounds }; gDraw(); return; }
    if (reset) { g.bounds = gFullWindow(); g.view = { ...g.bounds }; g.full = true; g.curT = null; }
    else if (g.full) { g.bounds = gFullWindow(); g.view = { ...g.bounds }; }
    const w = { ...g.view };
    const buckets = Math.min(1200, Math.max(100, gPlotWidth()));
    const url = `/api/trends/series?from=${encodeURIComponent(new Date(w.a).toISOString())}&to=${encodeURIComponent(new Date(w.b).toISOString())}&tags=${ids.join(',')}&buckets=${buckets}`;
    try {
      const r = await fetch(url, { signal: AbortSignal.timeout(6000) });
      if (!r.ok) throw new Error(await errText(r));
      const j = await r.json();
      if (token !== g.token) return;
      g.offH = j.offset_hours; g.nowMs = j.now_ms;
      g.series = j.series.map(s => core.normalize(s, j.bucket_sec));
      g.dataView = { a: j.from_ms, b: j.to_ms };
      g.fail = 0; g.err = ''; g.loadedAt = Date.now();
    } catch (ex) {
      if (token !== g.token) return;
      g.fail += 1;
      g.err = ex.name === 'TimeoutError' ? 'сервер не ответил за 6 секунд' : ex.message;
      if (g.fail === 1) toast(`Не удалось загрузить график: ${g.err}`);
    }
    gDraw();
  }

  function gSchedFetch() { clearTimeout(g.fetchT); g.fetchT = setTimeout(() => gLoad(false), 350); }
  function gSetView(v) {
    if (!g.bounds) return;
    g.view = v;
    g.full = Math.abs(v.a - g.bounds.a) < 1 && Math.abs(v.b - g.bounds.b) < 1;
    if (g.mode === 'online' && !g.paused && !g.full) { g.paused = true; gSyncBar(); }
    if (!g.raf) g.raf = requestAnimationFrame(() => { g.raf = 0; gDraw(); });
    gSchedFetch();
  }

  const G_M = { l: 58, r: 14, t: 10, b: 26 };

  function gManualY() {
    const lo = XG.parseNum(g.ymin), hi = XG.parseNum(g.ymax);
    if (lo === null || hi === null || Number.isNaN(lo) || Number.isNaN(hi) || lo >= hi) return null;
    return [lo, hi];
  }

  function gDraw() {
    const box = pane.querySelector('#trg-plot');
    if (!box || !g.built) return;
    const W = gPlotWidth(), H = W < 560 ? 280 : (W > 1500 ? 460 : 360);
    const m = G_M, iw = W - m.l - m.r, ih = H - m.t - m.b;
    const v = g.view || gFullWindow();
    const lost = g.fail >= 2;
    const vis = g.series.filter(s => !g.hidden.includes(s.id));
    const endMs = Math.min(v.b, g.nowMs || gNow());
    const X = t => m.l + (t - v.a) / (v.b - v.a) * iw;
    const manual = gManualY();
    let yr = manual || core.autoRange(vis.map(s => core.viewExtent(s, v.a, v.b, endMs)), { zero: g.zero }) || [0, 1];
    if (yr[1] <= yr[0]) yr = [yr[0] - 1, yr[1] + 1];
    const Y = val => m.t + (1 - (val - yr[0]) / (yr[1] - yr[0])) * ih;
    let inner = `<defs><clipPath id="trg-clip"><rect x="${m.l}" y="${m.t}" width="${iw}" height="${ih}"/></clipPath></defs>`;
    for (const tk of core.timeTicks(v.a, v.b, gOff(), Math.max(3, Math.floor(iw / 80)))) {
      const x = X(tk.t);
      inner += `<line class="ch-grid" x1="${x}" x2="${x}" y1="${m.t}" y2="${m.t + ih}"/><text class="ch-tick" x="${x}" y="${H - 8}" text-anchor="middle">${esc(tk.label)}</text>`;
    }
    for (const tv of core.niceTicks(yr[0], yr[1], Math.max(3, Math.floor(ih / 40))).ticks) {
      const y = Y(tv);
      inner += `<line class="ch-grid" x1="${m.l}" x2="${m.l + iw}" y1="${y}" y2="${y}"/><text class="ch-tick" x="${m.l - 6}" y="${y + 4}" text-anchor="end">${esc(core.fmtVal(tv))}</text>`;
    }
    const empty = !g.series.length ? 'Отметьте переменные для графика' : (lost ? 'Не удалось загрузить' : (vis.length ? '' : 'Все линии выключены в легенде'));
    let any = false;
    if (!lost) {
      inner += '<g clip-path="url(#trg-clip)">';
      const lims = new Set();
      for (const s of vis) {
        const ci = g.series.indexOf(s), col = `style="--c:${core.colorVar(gSelIds().indexOf(s.id) >= 0 ? gSelIds().indexOf(s.id) : ci)}"`;
        const segs = core.lineSegments(s, v.a, v.b, endMs);
        for (const sg of segs) {
          any = true;
          if (s.mode === 'agg') { const d = core.bandD(sg, X, Y); if (d) inner += `<path class="trg-band" ${col} d="${d}"/>`; }
          if (sg.length === 1) { inner += `<circle class="trg-dot" ${col} cx="${X(sg[0][0])}" cy="${Y(sg[0][1])}" r="2.5"/>`; continue; }
          inner += `<path class="trg-line" ${col} d="${core.pathD(sg, X, Y)}"/>`;
          for (const run of core.outPaths(sg, s.lo, s.hi)) inner += `<path class="trg-out" d="${core.pathD(run, X, Y)}"/>`;
        }
        for (const [val, kind] of [[s.lo, 'нижняя граница'], [s.hi, 'верхняя граница']]) {
          if (val === null || val === undefined || val < yr[0] || val > yr[1]) continue;
          const key = `${val}`;
          if (lims.has(key)) continue;
          lims.add(key);
          const y = Y(val);
          inner += `<line class="trg-lim" x1="${m.l}" x2="${m.l + iw}" y1="${y}" y2="${y}"><title>${esc(`${s.name}: ${kind} ${core.fmtVal(val)}`)}</title></line>` +
            `<text class="trg-limt" x="${m.l + iw - 4}" y="${y - 3}" text-anchor="end">${esc(core.fmtVal(val))}</text>`;
        }
      }
      inner += '</g>';
    }
    const msg = empty || (!any && !lost ? 'Нет записей за показанный период' : '');
    if (msg) inner += `<text class="trg-msg" x="${m.l + iw / 2}" y="${m.t + ih / 2}" text-anchor="middle">${esc(msg)}</text>`;
    inner += `<rect class="trg-frame" x="${m.l}" y="${m.t}" width="${iw}" height="${ih}"/>`;
    inner += `<line id="trg-cur" class="trg-cur" y1="${m.t}" y2="${m.t + ih}" x1="-10" x2="-10"/><g id="trg-dots"></g><rect id="trg-sel" class="trg-sel" y="${m.t}" height="${ih}" x="0" width="0" visibility="hidden"/>`;
    box.innerHTML = `<svg class="ch-svg trg-svg" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="Графики переменных">${inner}</svg>`;
    g.sc = { m, iw, ih, W, H, v, X, Y, lost, yr, endMs };
    const badge = pane.querySelector('#trg-badge');
    badge.className = 'live-badge ' + (g.fail >= 2 ? 'tr-bad' : (g.fail === 0 && g.series.length ? 'on' : 'off'));
    badge.textContent = g.fail >= 2 ? '● нет связи' : (g.series.length ? '● данные получены' : '● нет данных');
    pane.querySelector('#trg-range').textContent = `${core.fmtClock(v.a, gOff())} — ${core.fmtClock(v.b, gOff())}`;
    gLegend();
    gStats(lost);
    gCursor();
  }

  function gLegend() {
    const ids = gSelIds();
    pane.querySelector('#trg-legend').innerHTML = g.series.map(s => {
      const off = g.hidden.includes(s.id), ci = Math.max(0, ids.indexOf(s.id));
      return `<button type="button" class="trg-chip${off ? ' off' : ''}" data-id="${s.id}" aria-pressed="${!off}" title="${esc(`${s.name}: клик ${off ? 'включает' : 'выключает'} линию на графике`)}"><i style="background:${core.colorVar(ci)}"></i>${esc(s.name)}</button>`;
    }).join('');
  }

  function gStats(lost) {
    const ids = gSelIds();
    const rows = g.series.map(s => {
      const st = s.stats || {}, ci = Math.max(0, ids.indexOf(s.id));
      const c = k => `<td class="num" title="${esc(`${s.name}: ${k[1]} за показанный период`)}">${lost || st[k[0]] === null || st[k[0]] === undefined ? '—' : core.fmtVal(st[k[0]])}</td>`;
      return `<tr${g.hidden.includes(s.id) ? ' class="off"' : ''}><td title="${esc(s.name)}"><i class="trg-sw" style="background:${core.colorVar(ci)}"></i>${esc(s.name)}</td>${c(['min', 'минимум'])}${c(['avg', 'среднее'])}${c(['max', 'максимум'])}` +
        `<td class="num" title="${esc(`${s.name}: число точек за показанный период (записей или суммарно по корзинам)`)}">${lost ? '—' : (st.count || 0).toLocaleString('ru-RU')}</td></tr>`;
    }).join('');
    pane.querySelector('#trg-st-w').innerHTML = `<table class="trg-t"><thead><tr><th title="Переменная и цвет её линии">Переменная</th><th class="num" title="Наименьшее значение за показанный период">Минимум</th><th class="num" title="Среднее значение за показанный период">Среднее</th><th class="num" title="Наибольшее значение за показанный период">Максимум</th><th class="num" title="Сколько записей в показанном периоде">Точек</th></tr></thead><tbody>${rows}</tbody></table>`;
  }

  // Линейка: вертикальная линия, точки на линиях и таблица «Имя, Значение, Время»
  function gCursor() {
    const sc = g.sc;
    if (!sc) return;
    const v = sc.v;
    let t = g.curT === null ? v.b : g.curT;
    if (t < v.a || t > v.b) t = v.b;
    const x = sc.X(t);
    const line = pane.querySelector('#trg-cur'), dots = pane.querySelector('#trg-dots');
    if (line) { line.setAttribute('x1', x); line.setAttribute('x2', x); }
    const ids = gSelIds();
    let rows = '', dotHtml = '';
    for (const s of g.series) {
      const off = g.hidden.includes(s.id), ci = Math.max(0, ids.indexOf(s.id));
      const pt = sc.lost ? null : core.valueAt(s, t, sc.endMs);
      const bad = pt && ((s.lo !== null && pt.v < s.lo) || (s.hi !== null && pt.v > s.hi));
      if (pt && !off) dotHtml += `<circle class="trg-cdot" style="--c:${core.colorVar(ci)}" cx="${x}" cy="${sc.Y(pt.v)}" r="4"/>`;
      rows += `<tr${off ? ' class="off"' : ''}><td title="${esc(s.name)}"><i class="trg-sw" style="background:${core.colorVar(ci)}"></i>${esc(s.name)}</td>` +
        `<td class="num${bad ? ' bad' : ''}" title="${esc(bad ? `${s.name}: значение вне границ ${s.lo === null ? '—' : core.fmtVal(s.lo)} … ${s.hi === null ? '—' : core.fmtVal(s.hi)}` : `${s.name}: значение в момент линейки (ближайшая запись слева)`)}">${pt ? core.fmtVal(pt.v) : '—'}</td>` +
        `<td class="num" title="Время записи, по времени завода">${pt ? core.fmtClock(pt.t, gOff()) : '—'}</td></tr>`;
    }
    if (dots) dots.innerHTML = dotHtml;
    pane.querySelector('#trg-ct-w').innerHTML = `<table class="trg-t"><thead><tr><th title="Переменная и цвет её линии">Имя</th><th class="num" title="Значение в момент линейки: действующее, ближайшее слева; прочерк: записей нет">Значение</th><th class="num" title="Время записи, по времени завода">Время</th></tr></thead><tbody>${rows}</tbody></table>`;
  }

  function gBindPlot() {
    const box = pane.querySelector('#trg-plot');
    const ptrs = new Map();
    let drag = null, pinch = null;
    const local = e => { const r = box.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top }; };
    const inPlot = p => g.sc && p.x >= g.sc.m.l && p.x <= g.sc.m.l + g.sc.iw && p.y >= g.sc.m.t && p.y <= g.sc.m.t + g.sc.ih;
    const tAt = (x, v) => v.a + (Math.min(Math.max(x, G_M.l), G_M.l + g.sc.iw) - G_M.l) / g.sc.iw * (v.b - v.a);
    const setCursor = x => { g.curT = tAt(x, g.view); gCursor(); };
    box.addEventListener('pointerdown', e => {
      if (!g.sc || (e.pointerType === 'mouse' && e.button !== 0)) return;
      const p = local(e);
      ptrs.set(e.pointerId, p);
      try { box.setPointerCapture(e.pointerId); } catch { /* без захвата */ }
      if (ptrs.size === 2) { const [a, b] = [...ptrs.values()]; pinch = { d: Math.abs(a.x - b.x) || 1, view: { ...g.view }, cx: (a.x + b.x) / 2 }; drag = null; return; }
      if (!inPlot(p)) return;
      const touch = e.pointerType !== 'mouse';
      drag = { x0: p.x, view: { ...g.view }, kind: touch ? 'pan' : ((g.pan !== e.shiftKey) ? 'pan' : 'box'), moved: false };
      g.drag = true;
    });
    box.addEventListener('pointermove', e => {
      const p = local(e);
      if (ptrs.has(e.pointerId)) ptrs.set(e.pointerId, p);
      if (pinch && ptrs.size >= 2) {
        const [a, b] = [...ptrs.values()], d = Math.abs(a.x - b.x) || 1;
        gSetView(core.zoomView(pinch.view, pinch.d / d, tAt(pinch.cx, pinch.view), g.bounds));
        return;
      }
      if (drag) {
        const dx = p.x - drag.x0;
        if (Math.abs(dx) > 3) drag.moved = true;
        if (!drag.moved) return;
        if (drag.kind === 'pan') gSetView(core.panView(drag.view, -dx / g.sc.iw * (drag.view.b - drag.view.a), g.bounds));
        else {
          const sel = pane.querySelector('#trg-sel');
          if (sel) { sel.setAttribute('x', Math.min(drag.x0, p.x)); sel.setAttribute('width', Math.abs(dx)); sel.setAttribute('visibility', 'visible'); }
        }
        return;
      }
      if (e.pointerType === 'mouse' && inPlot(p)) setCursor(p.x);
    });
    const end = e => {
      const p = local(e);
      ptrs.delete(e.pointerId);
      if (pinch) { if (ptrs.size < 2) pinch = null; return; }
      if (!drag) return;
      const d = drag; drag = null; g.drag = false;
      const sel = pane.querySelector('#trg-sel');
      if (sel) sel.setAttribute('visibility', 'hidden');
      if (!d.moved) { if (inPlot(p)) setCursor(p.x); return; }
      if (d.kind === 'box') {
        const t0 = tAt(d.x0, d.view), t1 = tAt(p.x, d.view);
        const a = Math.min(t0, t1), b = Math.max(t0, t1);
        if (b - a >= core.MIN_SPAN) gSetView(core.clampView({ a, b }, g.bounds));
      }
    };
    box.addEventListener('pointerup', end);
    box.addEventListener('pointercancel', e => { ptrs.delete(e.pointerId); pinch = null; drag = null; g.drag = false; });
    box.addEventListener('wheel', e => {
      const p = local(e);
      if (!inPlot(p)) return;
      e.preventDefault();
      gSetView(core.zoomView(g.view, e.deltaY < 0 ? 0.8 : 1.25, tAt(p.x, g.view), g.bounds));
    }, { passive: false });
    box.addEventListener('dblclick', e => { if (inPlot(local(e))) { g.paused = false; gSyncBar(); gLoad(true); } });
  }

  // обновление: онлайн раз в 5 секунд, архив с сегодняшним днём раз в 10 секунд (прошлые периоды читаются при открытии)
  function gTick() {
    if (!g.built || !g.vars || !pane.classList.contains('active') || document.hidden || g.drag) return;
    const age = Date.now() - g.loadedAt;
    if (g.mode === 'online') { if (!g.paused && age >= 5000) gLoad(false); }
    else if (g.to === today() && age >= 10000) gLoad(false);
  }

  async function gShow() {
    if (!g.built) return;
    if (!g.vars) {
      if (g.loading) return;
      g.loading = true;
      await gLoadVars();
      g.loading = false;
      gSyncBar();
      await gLoad(true);
      return;
    }
    if (Date.now() - g.loadedAt > 10000) gLoad(false); else gDraw();
  }
  setInterval(() => { try { gTick(); } catch { /* следующий тик */ } }, 1000);

  tabBtn.addEventListener('click', show);
  window.addEventListener('resize', () => { if (pane.classList.contains('active')) requestAnimationFrame(() => { draw(); gDraw(); }); });
})(typeof window !== 'undefined' ? window : globalThis);
