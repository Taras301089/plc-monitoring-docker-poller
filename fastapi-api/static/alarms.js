// Вкладка «Аварии»: журнал событий ПЛК (активные и история), фильтры, период, таблица как в Excel, выгрузка, справочник аварий
(function (root) {
  'use strict';

  // ---------- чистая логика (проверяется в node) ----------
  const pad = n => String(n).padStart(2, '0');

  // Длительность: 0 с, 59 с, 1 мин 1 с, 1 ч, 26 ч 20 мин 53 с
  function fmtDuration(sec) {
    if (sec === null || sec === undefined || Number.isNaN(Number(sec))) return '—';
    const s = Math.max(0, Math.floor(Number(sec)));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
    const out = [];
    if (h) out.push(`${h} ч`);
    if (m) out.push(`${m} мин`);
    if (x || !out.length) out.push(`${x} с`);
    return out.join(' ');
  }

  // ISO UTC (с «Z» или без суффикса) -> миллисекунды
  function parseUtc(iso) {
    if (!iso) return NaN;
    const s = String(iso);
    return Date.parse(/(Z|[+-]\d\d:?\d\d)$/.test(s) ? s : s + 'Z');
  }

  // Время завода = UTC + offset_hours сервера; формат ДД.ММ.ГГГГ, ЧЧ:ММ:СС
  function toPlant(iso, offsetHours) {
    const ms = parseUtc(iso);
    if (Number.isNaN(ms)) return '—';
    const d = new Date(ms + (Number(offsetHours) || 0) * 3600000);
    return `${pad(d.getUTCDate())}.${pad(d.getUTCMonth() + 1)}.${d.getUTCFullYear()}, ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}`;
  }

  // Адрес запроса: пустые значения не передаются, значения экранируются (кириллица)
  function buildUrl(base, p) {
    const keys = ['plc_id', 'state', 'station', 'device', 'class', 'q', 'from', 'to', 'sort', 'dir', 'limit', 'offset', 'src', 'brand', 'all_hours', 'metric'];
    const parts = [];
    keys.forEach(k => {
      const v = p[k];
      if (v === undefined || v === null || v === '') return;
      parts.push(`${k}=${encodeURIComponent(v)}`);
    });
    return parts.length ? `${base}?${parts.join('&')}` : base;
  }

  // Подпись под таблицей: «Страница 2 · показано 200»
  function pageLabel(page, shown) { return `Страница ${page} · показано ${shown}`; }

  // Живая длительность активной аварии: от started_at до «сейчас» сервера плюс время с момента ответа
  function liveSeconds(startedIso, serverNowIso, fetchedAtMs, nowMs) {
    const st = parseUtc(startedIso), sn = parseUtc(serverNowIso);
    if (Number.isNaN(st) || Number.isNaN(sn)) return null;
    return Math.max(0, (sn + (nowMs - fetchedAtMs) - st) / 1000);
  }

  // Следующая сортировка по клику: по возрастанию, по убыванию, сброс
  function nextSort(cur, key) {
    if (!cur || cur.sort !== key) return { sort: key, dir: 'asc' };
    if (cur.dir === 'asc') return { sort: key, dir: 'desc' };
    return null;
  }

  // Бренд ПЛК: часть имени до первого «_» (GWM_UB -> GWM); у имени без «_» бренд равен имени (Changan)
  function plcBrand(name) {
    const n = String(name ?? '').trim();
    const i = n.indexOf('_');
    return i > 0 ? n.slice(0, i) : n;
  }

  // Порядок брендов в меню: GWM, Chery, Changan, AE, затем прочие по алфавиту; повторы убираются
  const BRAND_FIRST = ['GWM', 'Chery', 'Changan', 'AE'];
  function orderBrands(brands) {
    const rest = [...new Set((brands || []).filter(b => b && !BRAND_FIRST.includes(b)))].sort((a, b) => a.localeCompare(b, 'ru'));
    return [...BRAND_FIRST, ...rest];
  }

  // ---------- аналитика: чистая логика ----------
  // Участок ПЛК: часть имени после первого «_» (GWM_UB -> UB, AE_WBS_1 -> WBS 1); у имени без «_» пусто
  function plcArea(name) {
    const n = String(name ?? '').trim();
    const i = n.indexOf('_');
    return i > 0 ? n.slice(i + 1).replace(/_/g, ' ') : '';
  }

  // Бренды, которые есть среди ПЛК, в порядке меню
  function anBrands(list) {
    const present = new Set((list || []).map(p => plcBrand(p.name)));
    return orderBrands([...present]).filter(b => present.has(b));
  }

  // Участки бренда: [{id, name}]; пусто, если у ПЛК бренда нет участка в имени (Changan)
  function anAreas(list, brand) {
    const own = (list || []).filter(p => plcBrand(p.name) === brand);
    if (!own.some(p => plcArea(p.name))) return [];
    return own.map(p => ({ id: String(p.id), name: plcArea(p.name) || p.name }));
  }

  // Параметры запроса аналитики из состояния экрана: участок (ПЛК) важнее бренда; «вне смены» передаётся только когда включено
  function anQueryOf(s) {
    return { from: s.from, to: s.to, brand: s.anPlc ? '' : s.anBrand, plc_id: s.anPlc || '', all_hours: s.anAll ? 'true' : '' };
  }

  const fmtMin = sec => (Number(sec || 0) / 60).toFixed(1).replace('.', ',');
  const fmtPct = x => (Number(x || 0) * 100).toFixed(1).replace('.', ',');
  const fmtDate = iso => { const p = String(iso).split('-'); return p.length === 3 ? `${p[2]}.${p[1]}.${p[0]}` : String(iso); };

  // Строка под заголовком аналитики; тот же текст в Excel
  function anNote(includeAll, excluded) {
    return includeAll
      ? 'Учитываются все аварии, в том числе вне смены и в дни без выпуска'
      : `Учитываются аварии, начавшиеся в рабочее время линии. Не учтено вне смены и в дни без выпуска: ${excluded}`;
  }

  const AN_TILES = ['Событий', 'Суммарное время аварий', 'Разных аварий'];
  const AN_PARETO_TITLE = 'Парето аварий (топ-10)';
  const AN_METRICS = { count: 'по числу появлений', time: 'по времени' };
  const AN_DAYS_TITLE = 'Динамика по дням';
  const AN_DAYS_COUNT = 'Число аварий по дням';
  const AN_DAYS_MIN = 'Минуты аварий по дням';
  const AN_PARETO_COUNT = 'По числу';
  const AN_PARETO_TIME = 'По времени';

  // Таблицы аналитики: названия и колонки те же, что в Excel; text = как показано и копируется, val = для сортировки
  function anTables() {
    const col = (k, t, tip, text, val, num) => ({ k, t, tip, text, val: val || text, num: !!num });
    const plc = col('plc', 'ПЛК', 'ПЛК, на котором возникла авария', r => r.plc);
    const station = col('station', 'Станция', 'Станция линии, к которой относится авария; прочерк, если станция не определена', r => r.station);
    const code = col('code', 'Код', 'Код аварии из словаря; у аварий самого ПЛК (диагностика) кода нет, прочерк', r => r.code);
    const text = col('text', 'Авария', 'Текст аварии из словаря ПЛК или сообщение диагностики самого ПЛК', r => r.text);
    const num = (k, t, tip, f, g) => col(k, t, tip, r => f(r), r => g(r), true);
    const count = num('count', 'Появлений', 'Сколько раз авария появилась за период', r => String(r.count), r => r.count);
    const total = num('total', 'Суммарно, мин', 'Суммарная длительность аварий в минутах (с учётом рабочего времени линии)', r => fmtMin(r.total_sec), r => r.total_sec);
    const avg = num('avg', 'Среднее, мин', 'Средняя длительность одного появления в минутах', r => fmtMin(r.avg_sec), r => r.avg_sec);
    const mx = num('max', 'Максимум, мин', 'Самое долгое появление этой аварии в минутах', r => fmtMin(r.max_sec), r => r.max_sec);
    const alarmCols = shareCol => [plc, station, code, text, count, total, avg, mx, shareCol];
    const sumRows = (rows, f) => rows.reduce((s, r) => s + f(r), 0);
    return {
      frequent: { title: 'Самые частые', cols: alarmCols(num('share', 'Доля по числу, %', 'Доля аварии в общем числе появлений, проценты', r => fmtPct(r.share_count), r => r.share_count)) },
      longest: { title: 'Самые длительные', cols: alarmCols(num('share', 'Доля по времени, %', 'Доля аварии в суммарной длительности всех аварий, проценты', r => fmtPct(r.share_time), r => r.share_time)) },
      cases: {
        title: 'Самые долгие случаи',
        cols: [plc, station, code, text,
          col('started', 'Появление', 'Когда авария появилась, время завода', (r, c) => toPlant(r.started_at, c.off), r => parseUtc(r.started_at)),
          num('dur', 'Длительность, мин', 'Длительность одного случая в минутах (с учётом рабочего времени линии)', r => fmtMin(r.duration_sec), r => r.duration_sec)],
      },
      stations: {
        title: 'По станциям',
        cols: [plc, station, count, total, avg, num('share', 'Доля по времени, %', 'Доля станции в суммарной длительности всех аварий, проценты', r => fmtPct(r.share_time), r => r.share_time)],
        totals: rows => {
          const n = sumRows(rows, r => r.count), s = sumRows(rows, r => r.total_sec);
          return ['Итого', '', String(n), fmtMin(s), fmtMin(n ? s / n : 0), fmtPct(sumRows(rows, r => r.share_time))];
        },
      },
      days: {
        title: 'По дням',
        cols: [col('date', 'Дата', 'День, в который авария появилась (время завода)', r => fmtDate(r.date), r => r.date), count, total, avg],
        totals: rows => {
          const n = sumRows(rows, r => r.count), s = sumRows(rows, r => r.total_sec);
          return ['Итого', String(n), fmtMin(s), fmtMin(n ? s / n : 0)];
        },
      },
    };
  }

  // Сортировка в несколько уровней: sorts = [{k, dir}], стабильная; числа сравниваются как числа
  function sortRows(rows, cols, sorts, ctx) {
    if (!sorts || !sorts.length) return rows.slice();
    return rows.map((r, i) => [r, i]).sort((x, y) => {
      for (const s of sorts) {
        const c = cols.find(c => c.k === s.k);
        if (!c) continue;
        const a = c.val(x[0], ctx), b = c.val(y[0], ctx);
        const d = typeof a === 'number' && typeof b === 'number' ? a - b : String(a).localeCompare(String(b), 'ru', { numeric: true });
        if (d) return s.dir === 'desc' ? -d : d;
      }
      return x[1] - y[1];
    }).map(p => p[0]);
  }

  // Фильтры по столбцам: filters = { k: [допустимые тексты ячеек] }; пустой список не фильтрует
  function filterRows(rows, cols, filters, ctx) {
    const act = Object.entries(filters || {}).filter(([, v]) => v && v.length).map(([k, v]) => [cols.find(c => c.k === k), new Set(v)]).filter(([c]) => c);
    return act.length ? rows.filter(r => act.every(([c, set]) => set.has(c.text(r, ctx)))) : rows.slice();
  }

  // Клик по заголовку: по возрастанию, по убыванию, сброс; с Shift столбец добавляется вторым уровнем
  function nextSorts(sorts, k, multi) {
    const list = sorts || [];
    const cur = list.find(s => s.k === k);
    const nd = !cur ? 'asc' : (cur.dir === 'asc' ? 'desc' : null);
    if (!multi) return nd ? [{ k, dir: nd }] : [];
    if (!cur) return [...list, { k, dir: 'asc' }];
    return nd ? list.map(s => (s.k === k ? { k, dir: nd } : s)) : list.filter(s => s.k !== k);
  }

  function niceMax(v) {
    if (!(v > 0)) return 1;
    const p = Math.pow(10, Math.floor(Math.log10(v))), f = v / p;
    return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * p;
  }

  // Верх оси: для числа аварий кратен 4, чтобы деления (четверти) были целыми; для минут «круглое» число
  function axisMax(v, metric) {
    return metric === 'count' ? Math.max(4, Math.ceil(v / 4) * 4) : niceMax(v);
  }

  // Парето: синие столбцы (число появлений или минуты) и зелёная линия накопленной доли; возвращает строку SVG
  function paretoSvg(rows, metric, W) {
    if (!rows || !rows.length) return '';
    const H = 250, m = { l: 40, r: 44, t: 22, b: 96 };
    const n = rows.length, iw = W - m.l - m.r, ih = H - m.t - m.b, gw = iw / n, bw = Math.max(10, Math.min(48, gw * 0.6));
    const max = axisMax(Math.max(...rows.map(r => r.value)), metric);
    const yv = v => m.t + ih * (1 - v / max), yp = v => m.t + ih * (1 - v);
    const trunc = (s, k) => (String(s).length > k ? String(s).slice(0, k - 1) + '…' : String(s));
    const e = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    let g = [0, .25, .5, .75, 1].map(v => `<line class="al-ch-grid" x1="${m.l}" x2="${W - m.r}" y1="${yv(max * v)}" y2="${yv(max * v)}"/>` +
      `<text class="al-ch-tick" x="${m.l - 6}" y="${yv(max * v) + 4}" text-anchor="end">${Math.round(max * v * 10) / 10}</text>` +
      `<text class="al-ch-tick" x="${W - m.r + 6}" y="${yp(v) + 4}">${Math.round(v * 100)}%</text>`).join('');
    const pts = [];
    rows.forEach((r, i) => {
      const x = m.l + gw * i + gw / 2;
      const tip = `${r.label} (${r.plc}${r.station && r.station !== '—' ? ', ' + r.station : ''}): ${metric === 'count' ? r.value + ' появл.' : fmtMin(r.total_sec) + ' мин'}, доля ${fmtPct(r.share)} %, накоплено ${fmtPct(r.cum)} %`;
      g += `<rect class="al-bar" x="${x - bw / 2}" y="${yv(r.value)}" width="${bw}" height="${ih - (yv(r.value) - m.t)}"/>` +
        `<text class="al-ch-val" x="${x}" y="${yv(r.value) - 4}" text-anchor="middle">${metric === 'count' ? r.value : Math.round(r.value)}</text>` +
        `<rect class="al-ch-hit" x="${x - gw / 2}" y="${m.t}" width="${gw}" height="${ih}"><title>${e(tip)}</title></rect>` +
        `<text class="al-ch-tick" transform="translate(${x + 4} ${H - m.b + 12}) rotate(-35)" text-anchor="end">${e(trunc(r.label, 22))}</text>`;
      pts.push(`${x},${yp(r.cum)}`);
    });
    g += `<polyline class="al-cum" points="${pts.join(' ')}"/>` + pts.map(p => { const [x, y] = p.split(','); return `<circle class="al-dot" cx="${x}" cy="${y}" r="3.5"/>`; }).join('');
    return `<svg class="al-svg" viewBox="0 0 ${W} ${H}" role="img" aria-label="${e(AN_PARETO_TITLE)}" preserveAspectRatio="xMidYMid meet">${g}</svg>`;
  }

  // ---------- «убрано из статистики»: чистая логика ----------
  // Кнопки «Убрать» и «Вернуть» видят вошедшие пользователи, кроме роли «Просмотр» (право проверяется и на сервере)
  const anCanExclude = user => !!user && user.role !== 'viewer';

  // Строка «Убрано из статистики: N (аварий A, станций S)»; пусто, если ничего не убрано; тот же текст в Excel
  function anRemovedNote(removed) {
    const a = Number(removed && removed.alarms) || 0, s = Number(removed && removed.stations) || 0;
    return a + s ? `Убрано из статистики: ${a + s} (аварий ${a}, станций ${s})` : '';
  }

  // Тело запроса POST /api/alarms/exclusions из строки таблицы; причина необязательна
  function anExclusionBody(kind, row, reason) {
    const b = { plc_id: row.plc_id, kind };
    if (kind === 'alarm') b.bit_index = row.bit_index; else b.station = row.station;
    const r = String(reason || '').trim();
    if (r) b.reason = r;
    return b;
  }

  // Строка списка «Убрано из статистики»: что убрано, «Фамилия, должность · ДД.ММ ЧЧ:ММ», причина
  function anRemovedItem(it, offsetHours) {
    const ms = parseUtc(it.created_at);
    const d = Number.isNaN(ms) ? null : new Date(ms + (Number(offsetHours) || 0) * 3600000);
    const when = d ? `${pad(d.getUTCDate())}.${pad(d.getUTCMonth() + 1)} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}` : '';
    const what = it.kind === 'station' ? `Станция ${it.station} целиком` : [it.station, it.code, it.text].filter(Boolean).join(' · ');
    return { what, author: [it.created_name, when].filter(Boolean).join(' · '), reason: it.reason || '' };
  }

  // Запрос к /api/alarms/exclusions: при ошибке бросает Error с текстом сервера (например, «У вашей роли нет права менять настройки»)
  async function exclusionRequest(doFetch, method, url, body) {
    const r = await doFetch(url, { method, headers: { 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body), credentials: 'same-origin' });
    let j = null;
    try { j = await r.json(); } catch { /* без тела */ }
    if (!r.ok) throw new Error(j && typeof j.detail === 'string' ? j.detail : `Ошибка ${r.status}`);
    return j;
  }

  // ---------- аварии самого ПЛК (OPC UA Alarms & Conditions, origin = 'ac') ----------
  // Подсказка у состояния: когда авария квитирована на ПЛК (время завода)
  function ackedTitle(iso, offsetHours) { return `Квитирована на ПЛК: ${toPlant(iso, offsetHours)}`; }

  // Кнопка «Убрать» у аварии: только у битов словаря; аварии ПЛК убираются из статистики только станцией целиком
  const anCanRemoveAlarm = row => !!row && row.origin !== 'ac';

  // Журнал ПЛК настроен: ответ /api/alarms/filters (словарь аварий, аварии ПЛК или флажок «Аварии ПЛК»)
  function filtersConfigured(f) {
    if (!f) return false;
    return typeof f.configured === 'boolean' ? f.configured : (f.stations || []).length > 0;
  }

  // Динамика по дням: синие столбцы (число аварий или минуты) по дням; возвращает строку SVG
  function daysSvg(days, metric, W) {
    if (!days || !days.length) return '';
    const H = 170, m = { l: 40, r: 10, t: 22, b: 30 };
    const n = days.length, iw = W - m.l - m.r, ih = H - m.t - m.b, gw = iw / n, bw = Math.max(4, Math.min(40, gw * 0.7));
    const val = d => (metric === 'count' ? d.count : Math.round(d.total_sec / 6) / 10);
    const max = axisMax(Math.max(...days.map(val)), metric);
    const yv = v => m.t + ih * (1 - v / max);
    const step = Math.ceil(n / 12);
    let g = [0, .25, .5, .75, 1].map(v => `<line class="al-ch-grid" x1="${m.l}" x2="${W - m.r}" y1="${yv(max * v)}" y2="${yv(max * v)}"/>` +
      `<text class="al-ch-tick" x="${m.l - 6}" y="${yv(max * v) + 4}" text-anchor="end">${Math.round(max * v * 10) / 10}</text>`).join('');
    days.forEach((d, i) => {
      const x = m.l + gw * i + gw / 2, v = val(d), p = String(d.date).split('-');
      const dm = p.length === 3 ? `${p[2]}.${p[1]}` : String(d.date);
      const tip = `${fmtDate(d.date)}: ${d.count} авар., ${fmtMin(d.total_sec)} мин`;
      g += `<rect class="al-bar" x="${x - bw / 2}" y="${yv(v)}" width="${bw}" height="${ih - (yv(v) - m.t)}"/>` +
        (n <= 16 ? `<text class="al-ch-val" x="${x}" y="${yv(v) - 4}" text-anchor="middle">${String(v).replace('.', ',')}</text>` : '') +
        `<rect class="al-ch-hit" x="${x - gw / 2}" y="${m.t}" width="${gw}" height="${ih}"><title>${tip}</title></rect>` +
        (i % step === 0 ? `<text class="al-ch-tick" x="${x}" y="${H - m.b + 16}" text-anchor="middle">${dm}</text>` : '');
    });
    return `<svg class="al-svg" viewBox="0 0 ${W} ${H}" role="img" aria-label="${metric === 'count' ? AN_DAYS_COUNT : AN_DAYS_MIN}" preserveAspectRatio="xMidYMid meet">${g}</svg>`;
  }

  const util = { ackedTitle, anCanRemoveAlarm, filtersConfigured, anCanExclude,anRemovedNote, anExclusionBody, anRemovedItem, exclusionRequest, daysSvg, AN_DAYS_TITLE, AN_DAYS_COUNT, AN_DAYS_MIN, AN_PARETO_COUNT, AN_PARETO_TIME, fmtDuration, parseUtc, toPlant, buildUrl, pageLabel, liveSeconds, nextSort, plcBrand, orderBrands,
    plcArea, anBrands, anAreas, anQueryOf, anNote, anTables, sortRows, filterRows, nextSorts, paretoSvg, fmtMin, fmtPct, AN_TILES, AN_PARETO_TITLE, AN_METRICS };
  root.alarmsUtil = util;
  if (typeof module !== 'undefined' && module.exports) module.exports = util;
  if (typeof document === 'undefined') return;

  // ---------- экран ----------
  const pane = document.querySelector('#alarms');
  const tabBtn = document.querySelector('.tab[data-tab="alarms"]');
  if (!pane || !tabBtn) return;

  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const toast = t => (typeof showToast === 'function' ? showToast(t) : alert(t));
  const today = () => window.plantClock.isoDate();
  const addDays = (iso, n) => { const d = new Date(iso + 'T00:00:00Z'); d.setUTCDate(d.getUTCDate() + n); return d.toISOString().slice(0, 10); };
  const firstOfMonth = iso => iso.slice(0, 8) + '01';
  const lastOfMonth = iso => { const d = new Date(iso.slice(0, 8) + '01T00:00:00Z'); d.setUTCMonth(d.getUTCMonth() + 1); d.setUTCDate(0); return d.toISOString().slice(0, 10); };
  const daysBetween = (a, b) => Math.round((Date.parse(b + 'T00:00:00Z') - Date.parse(a + 'T00:00:00Z')) / 86400000) + 1;
  const presets = () => {
    const t = today(), pm = addDays(firstOfMonth(t), -1);
    return { day: [t, t], week: [addDays(t, -6), t], month: [firstOfMonth(t), t], prev: [firstOfMonth(pm), lastOfMonth(pm)] };
  };

  const KEY = 'alarms:state';
  const PAGE = 200;
  const TIMEOUT = () => AbortSignal.timeout(6000);
  const st = Object.assign({ plc: '', brand: '', mode: 'active', from: '', to: '', station: '', device: '', cls: '', hstate: 'all', q: '', sort: null, anBrand: '', anPlc: '', anAll: false, anMetric: 'count' }, (() => { try { return JSON.parse(localStorage.getItem(KEY)) || {}; } catch { return {}; } })());
  const save = () => { try { localStorage.setItem(KEY, JSON.stringify(st)); } catch { /* без сохранения */ } };

  let built = false, plcs = [], configured = new Set(), plcsLoading = null, filters = null, data = null, rows = [], page = 0;
  let fetchedAt = 0, failStreak = 0, token = 0, ticks = 0, loadedOnce = false;
  let sel = null, dragging = false;

  // Колонки: порядок от общего к частному; sort = параметр сервера (нет: сортировки на сервере нет)
  const off = () => (data ? data.offset_hours : 5);
  const lostNow = () => failStreak >= 2;
  const COLS = [
    { k: 'started', t: 'Появление', sort: 'started_at', tip: 'Когда авария появилась, время завода; сортировка по времени появления', get: r => toPlant(r.started_at, off()) + (r.active_at_start ? ' · была активна при обнаружении' : '') },
    { k: 'ended', t: 'Снятие', sort: 'ended_at', tip: 'Когда авария снята, время завода; прочерк у ещё активных', get: r => (r.ended_at ? toPlant(r.ended_at, off()) : '—') },
    { k: 'station', t: 'Станция', sort: 'station', fil: 'station', tip: 'Станция линии, к которой относится авария', get: r => r.station || '—' },
    { k: 'device', t: 'Устройство', fil: 'device', tip: 'Устройство на станции (например, частотный преобразователь); прочерк, если не указано', get: r => r.device || '—' },
    { k: 'code', t: 'Код', sort: 'code', tip: 'Код аварии из справочника HMI или частотника; у аварий самого ПЛК (диагностика) кода нет', get: r => r.code || '—' },
    { k: 'text', t: 'Текст аварии', tip: 'Текст аварии из справочника; у аварий самого ПЛК это сообщение диагностики ПЛК', get: r => r.text || '—' },
    { k: 'src', t: 'Источник / Класс', fil: 'cls', tip: 'Откуда получена авария и к какому классу относится: HMI и класс из справочника или «ПЛК · диагностика» (аварии самого ПЛК: модули, приводы, Profinet)', get: r => `${r.source || '—'} · ${r.class || '—'}` },
    { k: 'state', t: 'Состояние', fil: 'hstate', tip: 'Активна: авария есть сейчас; Снята: авария закончилась', get: r => (r.active ? 'Активна' : 'Снята') },
    { k: 'dur', t: 'Длительность', sort: 'duration', num: true, tip: 'Сколько длится или длилась авария; у активных растёт каждую секунду', get: r => (lostNow() ? '—' : fmtDuration(r.active ? liveSeconds(r.started_at, data.now, fetchedAt, Date.now()) : r.duration_sec)) + (r.active_at_start ? ' · наблюдение неполное' : '') },
  ];

  const q = s => pane.querySelector(s);

  function params(extra) {
    const hist = st.mode === 'history';
    return Object.assign({
      plc_id: st.plc, state: st.mode === 'active' ? 'active' : (st.hstate === 'ended' ? 'history' : 'all'),
      station: st.station, device: st.device, class: st.cls, q: st.q,
      from: hist ? st.from : '', to: hist ? st.to : '',
      sort: st.sort ? st.sort.sort : '', dir: st.sort ? st.sort.dir : '',
    }, extra);
  }

  async function getJson(url) {
    const r = await fetch(url, { signal: TIMEOUT() });
    if (!r.ok) throw new Error(`Ошибка ${r.status}`);
    return r.json();
  }

  // ---------- каркас ----------
  function build() {
    built = true;
    pane.innerHTML = `
      <div class="al-head">
        <h2 id="al-title" class="al-title" title="Выбранный ПЛК: по нему показывается журнал событий (сменить ПЛК можно кнопками рядом)"></h2>
        <div id="al-plcs" class="al-plcs" role="group" title="ПЛК выбранного бренда: нажмите кнопку, чтобы открыть журнал этого ПЛК"></div>
        <div class="al-tabs">
          <button type="button" id="al-t-active" class="al-tab" title="Аварии, которые есть сейчас; список обновляется каждые 5 секунд">Активные <b id="al-n-active">—</b></button>
          <button type="button" id="al-t-history" class="al-tab" title="Все события за выбранный период, в том числе снятые аварии">История <b id="al-n-history">—</b></button>
          <button type="button" id="al-t-an" class="al-tab" title="Топы и Парето аварий за период: самые частые и длительные, по станциям и дням; можно выбрать линию и участок">Аналитика</button>
        </div>
        <div class="ch-bar al-period" id="al-period" hidden>
          <button type="button" id="al-prev" class="dt-sec" title="Сдвинуть период назад на его длину (например, неделя назад или предыдущий день)">◀</button>
          <label title="Первый день периода">С <input type="date" id="al-from" title="Первый день периода"></label>
          <label title="Последний день периода (включительно)">По <input type="date" id="al-to" title="Последний день периода"></label>
          <button type="button" id="al-next" class="dt-sec" title="Сдвинуть период вперёд на его длину; дата не позже сегодняшней">▶</button>
          <span class="ch-presets">
            <button type="button" class="dt-sec" data-p="day" title="Только сегодняшний день">День</button>
            <button type="button" class="dt-sec" data-p="week" title="Последние 7 дней, включая сегодня">Неделя</button>
            <button type="button" class="dt-sec" data-p="month" title="С первого числа текущего месяца по сегодня">Месяц</button>
            <button type="button" class="dt-sec" data-p="prev" title="Весь прошлый календарный месяц">Прошлый месяц</button>
          </span>
        </div>
        <div class="al-filters al-an-filters" id="al-an-filters" hidden>
          <label class="al-f" title="Линия (бренд): общий свод по всем линиям или только одна из них">Линия<select id="al-an-brand" title="Выберите «Все линии» для общего свода или бренд: GWM, Chery, Changan, AE"><option value="">Все линии</option></select></label>
          <label class="al-f" id="al-an-area-l" title="Участок (ПЛК) внутри выбранной линии" hidden>Участок<select id="al-an-area" title="Выберите «Все участки» или один участок выбранной линии, например UB, MB или BS"><option value="">Все участки</option></select></label>
          <label class="al-check" title="По умолчанию в аналитику входят только аварии, начавшиеся в рабочее время линии: в день с выпуском и внутри включённых интервалов смены. Включите, чтобы увидеть вообще все аварии"><input type="checkbox" id="al-an-all" title="Включить в аналитику аварии вне смены, в перерывах и в дни без выпуска"> Учитывать аварии вне смены и в дни без выпуска</label>
          <button type="button" id="al-an-reset" class="dt-sec" title="Сбросить фильтры и сортировку во всех таблицах аналитики">Сбросить фильтры</button>
          <button type="button" id="al-an-xl" class="dt-sec" title="Выгрузить аналитику в Excel за выбранный период, линию и участок; в файле плитки, график Парето и те же таблицы">⬇ Excel</button>
        </div>
        <span id="al-chip"class="live-badge off al-chip" title="Актуальность данных: зелёный значит сборщик читает ПЛК, красный значит связи нет и цифры скрыты, серый значит ПЛК выключен из опроса">● нет данных</span>
      </div>
      <section class="al-card">
        <div class="al-filters">
          <label class="al-f" data-f="station" title="Показать аварии только одной станции">Станция<select id="al-station" title="Фильтр по станции: выберите станцию или «Все станции»"><option value="">Все станции</option></select></label>
          <label class="al-f" data-f="device" title="Показать аварии только одного устройства">Устройство<select id="al-device" title="Фильтр по устройству: выберите устройство или «Все устройства»"><option value="">Все устройства</option></select></label>
          <label class="al-f" data-f="cls" title="Показать аварии только одного класса">Класс<select id="al-cls" title="Фильтр по классу аварии: выберите класс или «Все классы»"><option value="">Все классы</option></select></label>
          <label class="al-f" id="al-hstate-l" data-f="hstate" title="В истории: все события или только уже снятые аварии">Состояние<select id="al-hstate" title="Фильтр по состоянию: все события истории или только снятые аварии"><option value="all">Все</option><option value="ended">Снятые</option></select></label>
          <label class="al-f al-grow" data-f="q" title="Поиск по тексту аварии или коду; применяется по Enter и через 0,4 секунды после ввода">Поиск<input type="search" id="al-q" placeholder="Текст или код" title="Введите часть текста аварии или код, например F32; список фильтруется сразу"></label>
          <button type="button" id="al-reset" class="dt-sec" title="Сбросить все фильтры, поиск и сортировку">Сбросить</button>
          <button type="button" id="al-copy" class="dt-sec" title="Скопировать всю показанную таблицу с заголовками; её можно вставить в Excel">Копировать таблицу</button>
          <button type="button" id="al-xl" class="dt-sec" title="Выгрузить журнал в Excel по текущим фильтрам и периоду">⬇ Excel</button>
        </div>
        <div class="al-body" id="al-body" tabindex="0" title="Выделите ячейки мышью и нажмите Ctrl+C, чтобы скопировать их как текст для Excel"></div>
        <div class="al-pager">
          <span id="al-pinfo" class="al-muted" title="Номер страницы и сколько строк показано"></span>
          <span class="al-pbtns">
            <button type="button" id="al-back" class="dt-sec" title="Предыдущая страница журнала">← Назад</button>
            <button type="button" id="al-more" class="dt-sec" title="Следующая страница журнала">Далее →</button>
          </span>
        </div>
      </section>
      <section class="al-card" id="al-an" hidden>
        <p id="al-an-note" class="al-muted" title="Какие аварии входят в расчёт"></p>
        <div id="al-an-removed" class="al-rm" title="Аварии и станции, убранные из статистики аналитики: на журнал событий это не влияет; нажмите, чтобы раскрыть список и вернуть убранное" hidden>
          <button type="button" id="al-an-removed-btn" class="al-rm-sum" aria-expanded="false" title="Показать или скрыть список убранных из статистики аварий и станций"></button>
          <div id="al-an-removed-list" class="al-rm-list" hidden></div>
        </div>
        <div id="al-an-empty" class="ch-empty" hidden></div>
        <div id="al-an-main">
          <div id="al-an-tiles" class="al-tiles"></div>
          <section class="al-an-sec" id="al-s-dyn">
            <div class="al-an-h"><h3 title="Сколько аварий и сколько минут они длились в каждый день; за один день показаны последние 14 дней, чтобы видеть динамику и снижать число и время аварий">${AN_DAYS_TITLE}</h3></div>
            <div class="al-an-pair">
              <div class="al-an-card"><h4 title="Сколько аварий началось в каждый день (с учётом выбранных линии, участка и правила рабочего времени)">${AN_DAYS_COUNT}</h4><div id="al-an-dcount"></div></div>
              <div class="al-an-card"><h4 title="Сколько минут суммарно длились аварии в каждый день (обрезано по рабочему времени линии)">${AN_DAYS_MIN}</h4><div id="al-an-dmin"></div></div>
            </div>
          </section>
          <section class="al-an-sec">
            <div class="al-an-h"><h3 title="Парето: самые частые и самые долгие аварии и накопленная доля, чтобы увидеть, на какие аварии приходится большая часть потерь">${AN_PARETO_TITLE}</h3></div>
            <div class="al-an-pair">
              <div class="al-an-card"><h4 title="Столбцы по числу появлений аварии, по убыванию">${AN_PARETO_COUNT}</h4><div id="al-an-pareto-count"></div>
                <div class="al-legend al-an-leg"><span class="al-leg"><i class="al-sw al-sw-bar"></i>Число появлений</span><span class="al-leg"><i class="al-sw al-sw-line"></i>Накопленный %</span></div></div>
              <div class="al-an-card"><h4 title="Столбцы по суммарной длительности аварии в минутах, по убыванию">${AN_PARETO_TIME}</h4><div id="al-an-pareto-time"></div>
                <div class="al-legend al-an-leg"><span class="al-leg"><i class="al-sw al-sw-bar"></i>Суммарно, мин</span><span class="al-leg"><i class="al-sw al-sw-line"></i>Накопленный %</span></div></div>
            </div>
          </section>
          ${anSectionsHtml()}
        </div>
      </section>
      <details class="al-card al-cat" id="al-cat">
        <summary title="Справочник всех аварий выбранного ПЛК: код, текст, станция, устройство и класс; загружается при раскрытии">Справочник аварий</summary>
        <div class="al-cat-bar"><input type="search" id="al-cq" placeholder="Поиск по коду или тексту" title="Поиск по справочнику: часть кода или текста аварии">
          <button type="button" id="al-cimp" class="dt-sec" hidden title="Загрузить справочник аварий выбранного ПЛК из Excel-файла, выгруженного из TIA Portal (HMI alarms, Discrete alarms); текущий справочник этого ПЛК будет заменён">Загрузить из TIA (.xlsx)</button>
          <input type="file" id="al-cfile" accept=".xlsx" hidden title="Выбор файла справочника аварий (.xlsx, выгрузка из TIA) для загрузки в систему"></div>
        <div id="al-cbody" class="al-body"></div>
        <div class="al-pager"><span id="al-cinfo" class="al-muted" title="Номер страницы справочника"></span><span class="al-pbtns">
          <button type="button" id="al-cback" class="dt-sec" title="Предыдущая страница справочника">← Назад</button>
          <button type="button" id="al-cmore" class="dt-sec" title="Следующая страница справочника">Далее →</button></span></div>
      </details>
      <p class="al-foot" id="al-foot" title="Как собираются данные журнала">Фоновый сбор работает при закрытом браузере. Время UTC+5 (по смещению сервера). Начало и снятие фиксируются при опросе, короткие импульсы могут быть пропущены.</p>`;
    bind();
  }

  function bind() {
    q('#al-plcs').addEventListener('click', e => { const b = e.target.closest('[data-plc]'); if (b) selectPlc(b.dataset.plc); });
    q('#al-t-active').addEventListener('click', () => setMode('active'));
    q('#al-t-an').addEventListener('click', () => setMode('analytics'));
    q('#al-t-history').addEventListener('click', () => setMode('history'));
    const dates = () => applyPeriod(q('#al-from').value, q('#al-to').value);
    q('#al-from').addEventListener('change', dates);
    q('#al-to').addEventListener('change', dates);
    pane.querySelectorAll('[data-p]').forEach(b => b.addEventListener('click', () => applyPeriod(...presets()[b.dataset.p])));
    const shift = k => {
      const n = daysBetween(st.from, st.to) * k, t = addDays(st.to, n);
      if (k > 0 && t > today()) return;
      applyPeriod(addDays(st.from, n), t);
    };
    q('#al-prev').addEventListener('click', () => shift(-1));
    q('#al-next').addEventListener('click', () => shift(1));
    [['#al-station', 'station'], ['#al-device', 'device'], ['#al-cls', 'cls'], ['#al-hstate', 'hstate']].forEach(([s, k]) =>
      q(s).addEventListener('change', e => { st[k] = e.target.value; page = 0; save(); syncControls(); refresh(true); }));
    let t = null;
    const search = () => { clearTimeout(t); st.q = q('#al-q').value.trim(); page = 0; save(); syncControls(); refresh(true); };
    q('#al-q').addEventListener('input', () => { clearTimeout(t); t = setTimeout(search, 400); });
    q('#al-q').addEventListener('keydown', e => { if (e.key === 'Enter') search(); });
    q('#al-reset').addEventListener('click', () => { st.station = st.device = st.cls = st.q = ''; st.hstate = 'all'; st.sort = null; page = 0; save(); syncControls(); refresh(true); });
    q('#al-copy').addEventListener('click', () => copy({ r1: 0, c1: 0, r2: rows.length - 1, c2: COLS.length - 1 }, true));
    q('#al-xl').addEventListener('click', async e => {
      const b = e.currentTarget; b.disabled = true;
      try { await window.xlsxDownload(buildUrl('/api/alarms/events.xlsx', params({ src: 'alarms' }))); } catch (ex) { toast(ex.message); }
      b.disabled = false;
    });
    q('#al-back').addEventListener('click', () => { if (page > 0) { page--; refresh(true); } });
    q('#al-more').addEventListener('click', () => { page++; refresh(true); });
    q('#al-body').addEventListener('keydown', onKey);
    document.addEventListener('mouseup', () => { dragging = false; });
    q('#al-cat').addEventListener('toggle', () => { if (q('#al-cat').open) loadCatalog(true); });
    let ct = null;
    q('#al-cq').addEventListener('input', () => { clearTimeout(ct); ct = setTimeout(() => { catPage = 0; loadCatalog(); }, 400); });
    q('#al-cimp').addEventListener('click', () => {
      if (catTotal > 0 && !confirm(`Справочник этого ПЛК уже содержит ${catTotal} записей. Заменить его данными из файла?`)) return;
      q('#al-cfile').click();
    });
    q('#al-cfile').addEventListener('change', importCatalog);
    q('#al-cback').addEventListener('click', () => { if (catPage > 0) { catPage--; loadCatalog(); } });
    q('#al-cmore').addEventListener('click', () => { catPage++; loadCatalog(); });
    bindAn();
  }

  function setMode(m) { st.mode = m; page = 0; save(); syncControls(); refresh(true); }

  function applyPeriod(f, t) {
    if (!f || !t || t < f) return;
    if (t > today()) t = today();
    st.from = f; st.to = t; page = 0; save(); syncControls(); refresh(true);
  }

  // Состояние элементов управления из st
  function syncControls() {
    if (!built) return;
    const hist = st.mode === 'history';
    const isAn = st.mode === 'analytics';
    q('#al-t-active').classList.toggle('active', st.mode === 'active');
    q('#al-t-history').classList.toggle('active', hist);
    q('#al-t-an').classList.toggle('active', isAn);
    q('#al-period').hidden = st.mode === 'active';
    [q('#al-body').closest('.al-card'), q('#al-cat'), q('#al-title'), q('#al-plcs'), q('#al-chip')].forEach(el => { el.hidden = isAn; });
    q('#al-an').hidden = !isAn;
    q('#al-an-filters').hidden = !isAn;
    q('#al-hstate-l').hidden = !hist;
    q('#al-from').value = st.from; q('#al-to').value = st.to;
    q('#al-from').max = q('#al-to').max = today();
    const cur = Object.entries(presets()).find(([, v]) => v[0] === st.from && v[1] === st.to);
    pane.querySelectorAll('[data-p]').forEach(b => b.classList.toggle('active', !!cur && cur[0] === b.dataset.p));
    q('#al-station').value = st.station; q('#al-device').value = st.device; q('#al-cls').value = st.cls;
    q('#al-hstate').value = st.hstate; q('#al-q').value = st.q;
    pane.querySelectorAll('.al-f').forEach(l => {
      const on = { station: st.station, device: st.device, cls: st.cls, hstate: hist && st.hstate !== 'all' ? st.hstate : '', q: st.q }[l.dataset.f];
      l.classList.toggle('al-on', !!on);
    });
  }

  function fillSelect(id, list, all) {
    const el = q(id);
    el.innerHTML = `<option value="">${all}</option>` + (list || []).map(v => `<option value="${esc(v)}">${esc(v)}</option>`).join('');
  }

  async function loadFilters() {
    if (!st.plc || !configured.has(st.plc)) return;
    try { filters = await getJson(buildUrl('/api/alarms/filters', { plc_id: st.plc })); } catch { filters = filters || { stations: [], devices: [], classes: [] }; }
    fillSelect('#al-station', filters.stations, 'Все станции');
    fillSelect('#al-device', filters.devices, 'Все устройства');
    fillSelect('#al-cls', filters.classes, 'Все классы');
  }

  // Все ПЛК из /api/plcs; журнал настроен у тех, у кого в фильтрах есть станции
  function loadPlcs() {
    if (plcsLoading) return plcsLoading;
    plcsLoading = (async () => {
      try {
        const all = await getJson('/api/plcs');
        const list = Array.isArray(all) ? all : (all.plcs || []);
        const checked = await Promise.all(list.map(async p => {
          try { const f = await getJson(buildUrl('/api/alarms/filters', { plc_id: p.id })); return [p, filtersConfigured(f)]; } catch { return [p, false]; }
        }));
        plcs = list;
        configured = new Set(checked.filter(c => c[1]).map(c => String(c[0].id)));
      } catch { /* список останется прежним */ }
      plcsLoading = null;
      const cur = plcs.find(p => String(p.id) === String(st.plc));
      if (cur) st.brand = plcBrand(cur.name);
      else if (st.brand && plcs.some(p => plcBrand(p.name) === st.brand)) st.plc = String(brandPlcs(st.brand).find(p => configured.has(String(p.id)))?.id ?? brandPlcs(st.brand)[0].id);
      else if (!st.brand && plcs.length) {
        const first = plcs.find(p => configured.has(String(p.id))) || plcs[0];
        st.plc = String(first.id); st.brand = plcBrand(first.name);
      } else st.plc = '';
      renderHead();
    })();
    return plcsLoading;
  }

  // кнопки ПЛК идут в личном порядке списка ПЛК (перетаскивание во вкладке «ПЛК», reorder.js)
  const brandPlcs = brand => {
    const list = plcs.filter(p => plcBrand(p.name) === brand);
    return root.reorder ? root.reorder.applyOrder(list, root.reorder.get('order-plc'), p => String(p.id)) : list;
  };
  if (root.reorder) root.reorder.watch('order-plc', () => { if (built) renderHead(); });

  // Название ПЛК и кнопки ПЛК выбранного бренда
  function renderHead() {
    if (!built) return;
    const cur = plcs.find(p => String(p.id) === String(st.plc));
    q('#al-title').textContent = cur ? cur.name : (st.brand || 'Аварии');
    q('#al-plcs').innerHTML = brandPlcs(st.brand).map(p => {
      const id = String(p.id), on = id === String(st.plc), cfg = configured.has(id);
      return `<button type="button" class="al-plc-btn${on ? ' active' : ''}${cfg ? '' : ' al-dim'}" data-plc="${esc(id)}" title="${esc(cfg ? `Открыть журнал событий ПЛК ${p.name}: активные аварии и история за период` : `ПЛК ${p.name}: журнал аварий не настроен, при выборе будет показано пояснение`)}">${esc(p.name)}</button>`;
    }).join('');
  }

  function selectPlc(id) {
    id = String(id);
    const p = plcs.find(x => String(x.id) === id);
    if (p) st.brand = plcBrand(p.name);
    st.plc = id; st.station = st.device = st.cls = st.q = ''; page = 0; filters = null;
    save(); renderHead();
    loadFilters().then(() => { syncControls(); refresh(true); });
  }

  // Выбор бренда в меню вкладки: открывается его ПЛК, у которого журнал настроен (прежний, если он из этого бренда)
  async function selectBrand(brand) {
    await loadPlcs();
    const list = brandPlcs(brand);
    if (st.mode === 'analytics') { st.anBrand = brand; st.anPlc = ''; anResetFilters(); save(); refreshAn(); return; }
    st.brand = brand;
    if (!list.some(p => String(p.id) === String(st.plc))) {
      const pick = list.find(p => configured.has(String(p.id))) || list[0];
      st.plc = pick ? String(pick.id) : '';
    }
    st.station = st.device = st.cls = st.q = ''; page = 0; filters = null;
    save(); renderHead();
    await loadFilters(); syncControls(); refresh(true);
  }

  // ---------- загрузка и отрисовка ----------
  async function refresh(force) {
    if (!built) build();
    if (st.mode === 'analytics') return refreshAn();
    if (!st.plc || !configured.has(String(st.plc))) { data = null; rows = []; failStreak = 0; draw(); return; }
    const my = ++token;
    const url = buildUrl('/api/alarms/events', params({ limit: PAGE, offset: page * PAGE }));
    try {
      const d = await getJson(url);
      if (my !== token) return;
      data = d; fetchedAt = Date.now(); failStreak = 0; rows = d.items || [];
    } catch {
      if (my !== token) return;
      failStreak++;
    }
    loadedOnce = true;
    draw();
  }

  function drawChip() {
    const c = q('#al-chip');
    const ls = data ? data.link_state : '';
    let cls = 'off', text = '● нет данных';
    if (lostNow() || ['no_link', 'stale', 'unknown'].includes(ls)) { cls = 'off al-red'; text = '● нет связи'; }
    else if (ls === 'ok') { cls = 'on'; text = '● данные актуальны'; }
    else if (ls === 'off') text = '● ПЛК выключен';
    c.className = `live-badge ${cls}`;
    c.textContent = text;
  }

  function draw() {
    syncControls();
    drawChip();
    const lost = lostNow();
    q('#al-n-active').textContent = lost || !data ? '—' : data.counts.active;
    q('#al-n-history').textContent = lost || !data ? '—' : data.counts.history;
    q('#al-foot').textContent = `Фоновый сбор работает при закрытом браузере. Время UTC${off() >= 0 ? '+' : ''}${off()} (по смещению сервера). Начало и снятие фиксируются при опросе, короткие импульсы могут быть пропущены.`;
    sel = null;
    const body = q('#al-body');
    const hist = st.mode === 'history';
    const colOn = k => ({ station: st.station, device: st.device, src: st.cls, state: hist && st.hstate !== 'all' ? st.hstate : '' }[k]);
    const head = COLS.map((c, i) => {
      const sorted = c.sort && st.sort && st.sort.sort === c.sort;
      const arrow = sorted ? `<i class="al-arrow">${st.sort.dir === 'asc' ? '▲' : '▼'}</i>` : '';
      const tip = c.sort ? `${c.tip}. Нажмите, чтобы отсортировать по возрастанию, по убыванию и сбросить; Ctrl+нажатие выделяет весь столбец` : `${c.tip}. Ctrl+нажатие выделяет весь столбец`;
      return `<th data-c="${i}" data-k="${c.k}" class="${c.num ? 'al-num' : ''}${colOn(c.k) ? ' al-fon' : ''}${c.sort ? ' al-sortable' : ''}" title="${esc(tip)}">${esc(c.t)}${arrow}</th>`;
    }).join('');
    if (!st.plc || !configured.has(String(st.plc))) {
      body.innerHTML = '<div class="ch-empty">Для этого ПЛК журнал аварий не настроен</div>';
    } else if (!rows.length) {
      body.innerHTML = `<table class="users-table al-table"><thead><tr>${head}</tr></thead></table><div class="ch-empty">${loadedOnce && !data ? 'Не удалось загрузить журнал' : 'Нет аварий по выбранным условиям'}</div>`;
      bindHead(body);
    } else {
      body.innerHTML = `<table class="users-table al-table"><thead><tr>${head}</tr></thead><tbody>${rows.map((r, i) => `<tr data-i="${i}" class="${r.active ? 'al-act' : ''}">${COLS.map((c, ci) => cell(r, c, ci)).join('')}</tr>`).join('')}</tbody></table>`;
      bindHead(body);
      if (window.attachColWidths) window.attachColWidths(body.querySelector('table'), 'alarms-journal');
      body.querySelectorAll('tbody td[data-c]').forEach(td => {
        const pos = () => ({ r: Number(td.parentNode.dataset.i), c: Number(td.dataset.c) });
        td.addEventListener('mousedown', e => { if (e.button) return; const p = pos(); dragging = true; sel = e.shiftKey && sel ? { r1: sel.r1, c1: sel.c1, r2: p.r, c2: p.c } : { r1: p.r, c1: p.c, r2: p.r, c2: p.c }; paintSel(); });
        td.addEventListener('mouseover', () => { if (dragging && sel) { const p = pos(); sel.r2 = p.r; sel.c2 = p.c; paintSel(); } });
      });
    }
    const total = data ? data.total : 0;
    q('#al-pinfo').textContent = lost ? '—' : pageLabel(page + 1, rows.length);
    q('#al-back').disabled = page === 0;
    q('#al-more').disabled = !data || (page * PAGE + rows.length) >= total;
  }

  function cell(r, c, ci) {
    const lost = lostNow();
    let inner;
    const sub = t => `<div class="al-sub">${esc(t)}</div>`;
    switch (c.k) {
      case 'started': inner = esc(toPlant(r.started_at, off())) + (r.active_at_start ? sub('Была активна при обнаружении') : ''); break;
      case 'ended': inner = esc(c.get(r)); break;
      case 'code': inner = `<span class="al-code" title="Код аварии: ссылка-подсветка, перехода нет">${esc(c.get(r))}</span>`; break;
      case 'src': inner = `${esc(r.source || '—')} · ${esc(r.class || '—')}`; break;
      case 'state': inner = `<span class="${r.active ? 'al-st-on' : 'al-st-off'}"${r.acked_at ? ` title="${esc(ackedTitle(r.acked_at, off()))}"` : ''}>${r.active ? 'Активна' : 'Снята'}</span>`; break;
      case 'dur': inner = `<span ${r.active && !lost ? `class="al-live" data-start="${esc(r.started_at)}"` : ''}>${esc(lost ? '—' : fmtDuration(r.active ? liveSeconds(r.started_at, data.now, fetchedAt, Date.now()) : r.duration_sec))}</span>${r.active_at_start ? sub('Наблюдение неполное') : ''}`; break;
      default: inner = esc(c.get(r));
    }
    return `<td data-c="${ci}" class="${c.num ? 'al-num' : ''}${c.k === 'text' ? ' al-text' : ''}">${inner}</td>`;
  }

  function bindHead(body) {
    body.querySelectorAll('th[data-c]').forEach(th => th.addEventListener('click', e => {
      const c = COLS[Number(th.dataset.c)];
      if (e.ctrlKey || e.metaKey) { sel = { r1: 0, c1: Number(th.dataset.c), r2: rows.length - 1, c2: Number(th.dataset.c) }; paintSel(); return; }
      if (!c.sort) return;
      st.sort = nextSort(st.sort, c.sort); page = 0; save(); refresh(true);
    }));
  }

  // Живая длительность активных строк: каждую секунду без запроса к серверу
  setInterval(() => {
    if (!built || !data || lostNow() || !pane.classList.contains('active')) return;
    pane.querySelectorAll('.al-live').forEach(el => { el.textContent = fmtDuration(liveSeconds(el.dataset.start, data.now, fetchedAt, Date.now())); });
  }, 1000);

  // ---------- выделение и копирование ----------
  const norm = s => ({ r1: Math.min(s.r1, s.r2), r2: Math.max(s.r1, s.r2), c1: Math.min(s.c1, s.c2), c2: Math.max(s.c1, s.c2) });
  function paintSel() {
    const s = sel ? norm(sel) : null;
    pane.querySelectorAll('#al-body td[data-c]').forEach(td => {
      const r = Number(td.parentNode.dataset.i), c = Number(td.dataset.c);
      td.classList.toggle('al-sel', !!s && r >= s.r1 && r <= s.r2 && c >= s.c1 && c <= s.c2);
    });
  }
  function copy(range, withHead) {
    const s = norm(range);
    if (!rows.length || s.r2 < 0) return;
    const clean = v => String(v).replace(/[\t\r\n]+/g, ' ');
    const m = [];
    if (withHead) m.push(COLS.slice(s.c1, s.c2 + 1).map(c => clean(c.t)));
    for (let r = s.r1; r <= s.r2; r++) m.push(COLS.slice(s.c1, s.c2 + 1).map(c => clean(c.get(rows[r]))));
    const text = window.xlgrid.toTsv(m);
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
    else if (mod && e.code === 'KeyA') { e.preventDefault(); sel = { r1: 0, c1: 0, r2: rows.length - 1, c2: COLS.length - 1 }; paintSel(); }
    else if (e.key === 'Escape') { sel = null; paintSel(); }
  }

  // ---------- справочник аварий ----------
  const CPAGE = 50;
  let catPage = 0, catTotal = 0;
  async function importCatalog() {
    const inp = q('#al-cfile'), f = inp.files && inp.files[0];
    inp.value = '';
    if (!f || !st.plc) return;
    const btn = q('#al-cimp');
    btn.disabled = true;
    try {
      const r = await fetch(buildUrl('/api/alarms/catalog/import', { plc_id: st.plc }), { method: 'POST', body: f });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) { toast(typeof d.detail === 'string' ? d.detail : 'Не удалось загрузить справочник'); return; }
      toast(`Загружено аварий: ${d.imported} (пустых заготовок: ${d.placeholders})`);
      loadCatalog(true);
    } catch { toast('Не удалось загрузить справочник'); }
    finally { btn.disabled = false; }
  }
  async function loadCatalog(reset) {
    if (reset) catPage = 0;
    const body = q('#al-cbody');
    const stt = window.authState && window.authState();
    q('#al-cimp').hidden = !(stt && stt.user && stt.user.role === 'admin');
    if (!st.plc) { body.innerHTML = '<div class="ch-empty">ПЛК не выбран</div>'; return; }
    try {
      const url = buildUrl('/api/alarms/catalog', { plc_id: st.plc, q: q('#al-cq').value.trim(), limit: CPAGE, offset: catPage * CPAGE });
      const r = await fetch(url, { signal: TIMEOUT() });
      if (r.status === 401 || r.status === 403) { body.innerHTML = '<div class="ch-empty">Справочник доступен после входа в систему</div>'; return; }
      if (!r.ok) throw new Error();
      const d = await r.json();
      const items = d.items || [];
      if (!q('#al-cq').value.trim()) catTotal = d.total || 0;
      body.innerHTML = items.length ? `<table class="users-table al-table"><thead><tr>
        <th class="al-num" title="Номер бита аварии в слове ПЛК">Бит</th><th title="Код аварии">Код</th><th title="Текст аварии из справочника">Текст аварии</th>
        <th title="Класс аварии">Класс</th><th title="Станция линии">Станция</th><th title="Устройство на станции">Устройство</th></tr></thead><tbody>${items.map(i => `<tr>
        <td class="al-num">${esc(i.bit_index)}</td><td><span class="al-code">${esc(i.code || '—')}</span></td><td class="al-text">${esc(i.text || '—')}</td>
        <td>${esc(i.class || '—')}</td><td>${esc(i.station || '—')}</td><td>${esc(i.device || '—')}</td></tr>`).join('')}</tbody></table>` : '<div class="ch-empty">В справочнике ничего не найдено</div>';
      if (window.attachColWidths && items.length) window.attachColWidths(body.querySelector('table'), 'alarms-catalog');
      q('#al-cinfo').textContent = pageLabel(catPage + 1, items.length);
      q('#al-cback').disabled = catPage === 0;
      q('#al-cmore').disabled = (catPage * CPAGE + items.length) >= (d.total || 0);
    } catch { body.innerHTML = '<div class="ch-empty">Не удалось загрузить справочник</div>'; }
  }

  // ---------- аналитика (экран) ----------
  const AN_KEYS = ['frequent', 'longest', 'cases', 'stations', 'days'];
  const AN_TABS = anTables();
  const GS = {};          // состояние таблиц: сортировка, фильтры, выделение
  const GV = {};          // строки, показанные сейчас в таблицах (после фильтров и сортировки)
  let an = null, anFail = 0, anTok = 0, anLoaded = false, anDrag = null, pop = null, rmOpen = false, dlg = null;
  const authUser = () => { const a = window.authState && window.authState(); return a && a.user ? a.user : null; };
  const canExclude = () => anCanExclude(authUser());
  const apiFetch = (url, o) => fetch(url, Object.assign({ signal: TIMEOUT() }, o));
  const EX_ALARM_TIP = 'Убрать эту аварию из статистики аналитики. Журнал событий не меняется. Вернуть можно в списке «Убрано из статистики»';
  const EX_STATION_TIP = 'Убрать станцию целиком из статистики: все её аварии перестанут учитываться в аналитике. Журнал событий не меняется';
  const EX_AC_TIP = 'Авария самого ПЛК (диагностика): по одной из статистики не убирается; убрать можно станцию целиком в таблице «По станциям»';
  const EX_KIND = { frequent: 'alarm', longest: 'alarm', cases: 'alarm', stations: 'station' };
  const gs = key => GS[key] || (GS[key] = { sorts: [], filters: {}, sel: null });
  const anLost = () => anFail >= 2;
  const anCtx = () => ({ off: an ? an.offset_hours : 5 });

  function anSectionsHtml() {
    return AN_KEYS.map(k => `<section class="al-an-sec" id="al-s-${k}">
        <div class="al-an-h"><h3 title="Таблица «${esc(AN_TABS[k].title)}» за выбранный период и линию; как и в Excel-файле">${esc(AN_TABS[k].title)}</h3>
          <button type="button" class="dt-sec" data-copy="${k}" title="Скопировать эту таблицу с заголовками в буфер; её можно вставить в Excel">Копировать таблицу</button></div>
        <div class="al-body" id="al-g-${k}" tabindex="0" title="Выделите ячейки мышью и нажмите Ctrl+C, чтобы скопировать их как текст для Excel; нажатие на заголовок сортирует, Shift добавляет вторую сортировку"></div>
      </section>`).join('');
  }

  function bindAn() {
    q('#al-an-brand').addEventListener('change', e => { st.anBrand = e.target.value; st.anPlc = ''; anResetFilters(); save(); refreshAn(); });
    q('#al-an-area').addEventListener('change', e => { st.anPlc = e.target.value; anResetFilters(); save(); refreshAn(); });
    q('#al-an-all').addEventListener('change', e => { st.anAll = e.target.checked; save(); refreshAn(); });
    q('#al-an-reset').addEventListener('click', () => { AN_KEYS.forEach(k => { GS[k] = { sorts: [], filters: {}, sel: null }; }); drawAn(); });
    q('#al-an-xl').addEventListener('click', async e => {
      const b = e.currentTarget; b.disabled = true;
      try { await window.xlsxDownload(buildUrl('/api/alarms/analytics.xlsx', Object.assign(anQueryOf(st), { metric: st.anMetric, src: 'alarms' }))); } catch (ex) { toast(ex.message); }
      b.disabled = false;
    });
    pane.querySelectorAll('[data-copy]').forEach(b => b.addEventListener('click', () => {
      const k = b.dataset.copy, n = GV[k] ? GV[k].length : 0;
      anCopy(k, { r1: 0, c1: 0, r2: n - 1, c2: AN_TABS[k].cols.length - 1 }, true);
    }));
    q('#al-an-removed-btn').addEventListener('click', () => { rmOpen = !rmOpen; drawRemoved(an && !anLost() ? an : null); });
    q('#al-an-removed-list').addEventListener('click', e => {
      const b = e.target.closest('[data-restore]');
      if (b) restoreExclusion(Number(b.dataset.restore), b);
    });
    document.addEventListener('mouseup', () => { anDrag = null; });
    document.addEventListener('mousedown', e => { if (pop && !pop.contains(e.target) && !e.target.closest('.al-fbtn')) closePop(); });
  }

  function anResetFilters() { AN_KEYS.forEach(k => { gs(k).filters = {}; }); }

  async function refreshAn() {
    if (!built) build();
    const my = ++anTok;
    try {
      const d = await getJson(buildUrl('/api/alarms/analytics', anQueryOf(st)));
      if (my !== anTok) return;
      an = d; anFail = 0;
    } catch {
      if (my !== anTok) return;
      anFail++;
    }
    anLoaded = true;
    drawAn();
  }

  function fillAnSelects() {
    const brands = anBrands(plcs);
    if (plcs.length && st.anBrand && !brands.includes(st.anBrand)) { st.anBrand = ''; st.anPlc = ''; }
    q('#al-an-brand').innerHTML = '<option value="">Все линии</option>' + brands.map(b => `<option value="${esc(b)}">${esc(b)}</option>`).join('');
    q('#al-an-brand').value = st.anBrand;
    const areas = st.anBrand ? anAreas(plcs, st.anBrand) : [];
    if (st.anPlc && !areas.some(a => a.id === st.anPlc)) st.anPlc = '';
    q('#al-an-area-l').hidden = !areas.length;
    q('#al-an-area').innerHTML = '<option value="">Все участки</option>' + areas.map(a => `<option value="${esc(a.id)}">${esc(a.name)}</option>`).join('');
    q('#al-an-area').value = st.anPlc;
    q('#al-an-brand').closest('.al-f').classList.toggle('al-on', !!st.anBrand);
    q('#al-an-area-l').classList.toggle('al-on', !!st.anPlc);
  }

  function drawAn() {
    syncControls();
    fillAnSelects();
    q('#al-an-all').checked = !!st.anAll;
    const d = anLost() ? null : an;
    const empty = q('#al-an-empty'), main = q('#al-an-main');
    const noData = !!d && d.scope.plcs_with_data === 0;
    q('#al-an-note').textContent = d && !noData ? anNote(d.include_all, d.excluded) : '';
    q('#al-an-xl').disabled = !d;
    drawRemoved(d);
    empty.hidden = !!d && !noData;
    main.hidden = !d || noData;
    if (!d) empty.textContent = anLoaded ? 'Не удалось загрузить аналитику' : 'Загрузка…';
    else if (noData) empty.textContent = 'Нет данных: для выбранных ПЛК нет словаря аварий и событий';
    if (!d || noData) return;
    const t = d.tiles;
    q('#al-an-tiles').innerHTML = [[AN_TILES[0], t.events, 'Сколько раз за период появились аварии (учитываются выбранные линия, участок и правило рабочего времени)', ''],
      [AN_TILES[1], fmtDuration(t.total_sec), 'Сколько времени суммарно длились аварии за период (обрезано по рабочему времени линии)', ' al-tile-bad'],
      [AN_TILES[2], t.distinct, 'Сколько разных аварий (разных битов словаря и разных аварий самого ПЛК) появилось за период', '']]
      .map(([n, v, tip, c]) => `<div class="al-tile${c}" title="${esc(tip)}"><span>${esc(n)}</span><b>${esc(v)}</b></div>`).join('');
    const dyn = d.dynamics || [];
    q('#al-an-dcount').innerHTML = daysSvg(dyn, 'count', 760);
    q('#al-an-dmin').innerHTML = daysSvg(dyn, 'time', 760);
    ['count', 'time'].forEach(m => {
      const rows = d.pareto[m] || [], host = q(`#al-an-pareto-${m}`);
      host.innerHTML = rows.length ? paretoSvg(rows, m, 760) : '<div class="ch-empty">Нет аварий за выбранный период</div>';
      host.nextElementSibling.hidden = !rows.length;
    });
    AN_KEYS.forEach(renderGrid);
    q('#al-s-days').hidden = d.from === d.to;
  }

  function renderGrid(key) {
    const def = AN_TABS[key], s = gs(key), host = q(`#al-g-${key}`), ctx = anCtx();
    const src = an && !anLost() ? an[key] : [];
    const view = sortRows(filterRows(src, def.cols, s.filters, ctx), def.cols, s.sorts, ctx);
    GV[key] = view; s.sel = null;
    const kind = canExclude() ? EX_KIND[key] : '';
    const actHead = kind ? '<th data-act-col class="al-act-col" title="Действия со строкой: убрать из статистики аналитики"></th>' : '';
    const head = def.cols.map((c, i) => {
      const si = s.sorts.findIndex(x => x.k === c.k), on = (s.filters[c.k] || []).length > 0;
      const arrow = si >= 0 ? `<i class="al-arrow">${s.sorts[si].dir === 'asc' ? '▲' : '▼'}${s.sorts.length > 1 ? si + 1 : ''}</i>` : '';
      return `<th data-c="${i}" data-k="${c.k}" class="al-sortable${c.num ? ' al-num' : ''}${on ? ' al-fon' : ''}" title="${esc(c.tip)}. Нажмите, чтобы отсортировать по возрастанию, по убыванию и сбросить; Shift+нажатие добавляет вторую сортировку; Ctrl+нажатие выделяет весь столбец"><span class="al-sort">${esc(c.t)}${arrow}</span><button type="button" class="al-fbtn${on ? ' on' : ''}" title="Фильтр по столбцу «${esc(c.t)}»: выбор значений галочками${on ? ' (фильтр включён)' : ''}">▾</button></th>`;
    }).join('') + actHead;
    if (!view.length) {
      host.innerHTML = `<table class="users-table al-table"><thead><tr>${head}</tr></thead></table><div class="ch-empty">${src.length ? 'Нет строк по выбранным фильтрам' : 'Нет аварий за выбранный период'}</div>`;
    } else {
      const actCell = r => !kind ? '' : (kind === 'station' && (!r.station || r.station === '—')
        ? '<td data-act-col class="al-act-col"></td>'
        : kind === 'alarm' && !anCanRemoveAlarm(r)
        ? `<td data-act-col class="al-act-col" title="${esc(EX_AC_TIP)}"></td>`
        : `<td data-act-col class="al-act-col">${kind === 'station'
          ? `<button type="button" class="al-rm-btn" data-rm="station" title="${esc(EX_STATION_TIP)}">Убрать станцию</button>`
          : `<button type="button" class="al-rm-btn" data-rm="alarm" title="${esc(EX_ALARM_TIP)}">Убрать</button>`}</td>`);
      const body = view.map((r, i) => `<tr data-i="${i}">${def.cols.map((c, ci) => `<td data-c="${ci}" class="${c.num ? 'al-num' : ''}${c.k === 'text' ? ' al-text' : ''}">${esc(c.text(r, ctx))}</td>`).join('')}${actCell(r)}</tr>`).join('');
      const foot = def.totals ? `<tfoot><tr class="al-total">${def.totals(view).map((v, ci) => `<td class="${def.cols[ci].num ? 'al-num' : ''}">${esc(v)}</td>`).join('')}${kind ? '<td data-act-col></td>' : ''}</tr></tfoot>` : '';
      host.innerHTML = `<table class="users-table al-table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody>${foot}</table>`;
      if (window.attachColWidths) window.attachColWidths(host.querySelector('table'), `alarms-an-${key}`);
      host.querySelectorAll('tbody td[data-c]').forEach(td => {
        const pos = () => ({ r: Number(td.parentNode.dataset.i), c: Number(td.dataset.c) });
        td.addEventListener('mousedown', e => { if (e.button) return; const p = pos(); anDrag = key; s.sel = e.shiftKey && s.sel ? { r1: s.sel.r1, c1: s.sel.c1, r2: p.r, c2: p.c } : { r1: p.r, c1: p.c, r2: p.r, c2: p.c }; paintGrid(key); });
        td.addEventListener('mouseover', () => { if (anDrag === key && s.sel) { const p = pos(); s.sel.r2 = p.r; s.sel.c2 = p.c; paintGrid(key); } });
      });
    }
    host.querySelectorAll('[data-rm]').forEach(b => b.addEventListener('click', e => {
      e.stopPropagation();
      exclusionDialog(b.dataset.rm, view[Number(b.closest('tr').dataset.i)]);
    }));
    host.querySelectorAll('th[data-c]').forEach(th => th.addEventListener('click', e => {
      const ci = Number(th.dataset.c), c = def.cols[ci];
      if (e.target.closest('.al-fbtn')) { openFilter(key, c.k, e.target.closest('.al-fbtn')); return; }
      if (e.ctrlKey || e.metaKey) { s.sel = { r1: 0, c1: ci, r2: view.length - 1, c2: ci }; paintGrid(key); return; }
      s.sorts = nextSorts(s.sorts, c.k, e.shiftKey);
      renderGrid(key);
    }));
    host.onkeydown = e => {
      const mod = e.ctrlKey || e.metaKey;
      if (mod && e.code === 'KeyC' && s.sel) { e.preventDefault(); anCopy(key, s.sel, false); }
      else if (mod && e.code === 'KeyA') { e.preventDefault(); s.sel = { r1: 0, c1: 0, r2: view.length - 1, c2: def.cols.length - 1 }; paintGrid(key); }
      else if (e.key === 'Escape') { s.sel = null; paintGrid(key); }
    };
  }

  // Строка «Убрано из статистики: N (аварий A, станций S)»; по клику раскрывается список с кнопкой «Вернуть»
  function drawRemoved(d) {
    const box = q('#al-an-removed'), rm = d && d.removed;
    const note = anRemovedNote(rm);
    box.hidden = !note;
    if (!note) return;
    const btn = q('#al-an-removed-btn'), list = q('#al-an-removed-list');
    btn.textContent = `${note} ${rmOpen ? '▴' : '▾'}`;
    btn.setAttribute('aria-expanded', rmOpen ? 'true' : 'false');
    list.hidden = !rmOpen;
    const can = canExclude();
    list.innerHTML = rmOpen ? rm.items.map(it => {
      const x = anRemovedItem(it, d.offset_hours);
      return `<div class="al-rm-row"><span class="al-rm-plc">${esc(it.plc)}</span><span class="al-rm-what">${esc(x.what)}</span>`
        + `<span class="al-muted">${esc(x.author)}</span>${x.reason ? `<span class="al-muted">Причина: ${esc(x.reason)}</span>` : ''}`
        + (can ? `<button type="button" class="dt-sec al-rm-back" data-restore="${esc(it.id)}" title="Вернуть в статистику: аварии снова будут учитываться в аналитике за все периоды; запись об этом остаётся в истории">Вернуть</button>` : '') + '</div>';
    }).join('') : '';
  }

  function closeDlg() { if (dlg) { dlg.remove(); dlg = null; } }

  // Короткое окно подтверждения с необязательной причиной
  function exclusionDialog(kind, row) {
    if (!row) return;
    closeDlg();
    const what = kind === 'station' ? `станцию ${row.station} (${row.plc}) целиком` : `аварию «${row.text}» (${row.plc}${row.station && row.station !== '—' ? ', ' + row.station : ''})`;
    dlg = document.createElement('div');
    dlg.className = 'kpi-modal';
    dlg.innerHTML = `<div class="kpi-modal-box auth-modal al-dlg" role="dialog">
      <h3 class="al-dlg-h">Убрать из статистики?</h3>
      <p class="al-dlg-p">${esc(kind === 'station' ? `Убрать ${what}: все её аварии перестанут учитываться в аналитике за все периоды.` : `Убрать ${what}: она перестанет учитываться в аналитике за все периоды.`)} Журнал событий не меняется.</p>
      <label for="al-dlg-reason">Причина (необязательно)</label>
      <input type="text" id="al-dlg-reason" maxlength="300" placeholder="Например, станция не используется" title="Зачем авария убирается из статистики; видно в списке «Убрано из статистики», можно не заполнять">
      <p class="auth-err al-dlg-err" hidden></p>
      <div class="kpi-modal-actions"><button type="button" data-a="ok" title="Убрать из статистики аналитики; вернуть можно в списке «Убрано из статистики»">Убрать</button><button type="button" class="dt-sec" data-a="cancel" title="Закрыть окно, ничего не меняя">Отмена</button></div></div>`;
    document.body.appendChild(dlg);
    const input = dlg.querySelector('input'), err = dlg.querySelector('.al-dlg-err'), ok = dlg.querySelector('[data-a="ok"]');
    const submit = async () => {
      ok.disabled = true;
      try {
        await exclusionRequest(apiFetch, 'POST', '/api/alarms/exclusions', anExclusionBody(kind, row, input.value));
        closeDlg();
        await refreshAn();
      } catch (ex) { err.textContent = ex.message; err.hidden = false; ok.disabled = false; }
    };
    ok.addEventListener('click', submit);
    dlg.querySelector('[data-a="cancel"]').addEventListener('click', closeDlg);
    dlg.addEventListener('mousedown', e => { if (e.target === dlg) closeDlg(); });
    dlg.addEventListener('keydown', e => { if (e.key === 'Escape') closeDlg(); else if (e.key === 'Enter' && e.target === input) submit(); });
    input.focus();
  }

  async function restoreExclusion(id, btn) {
    btn.disabled = true;
    try {
      await exclusionRequest(apiFetch, 'DELETE', `/api/alarms/exclusions/${id}`);
      await refreshAn();
    } catch (ex) { toast(ex.message); btn.disabled = false; }
  }

  function paintGrid(key) {
    const s = gs(key), r = s.sel ? norm(s.sel) : null;
    q(`#al-g-${key}`).querySelectorAll('td[data-c]').forEach(td => {
      const ri = Number(td.parentNode.dataset.i), c = Number(td.dataset.c);
      td.classList.toggle('al-sel', !!r && ri >= r.r1 && ri <= r.r2 && c >= r.c1 && c <= r.c2);
    });
  }

  function anCopy(key, range, withHead) {
    const def = AN_TABS[key], view = GV[key] || [], s = norm(range), ctx = anCtx();
    if (!view.length || s.r2 < 0) return;
    const clean = v => String(v).replace(/[\t\r\n]+/g, ' ');
    const m = [];
    if (withHead) m.push(def.cols.slice(s.c1, s.c2 + 1).map(c => clean(c.t)));
    for (let r = s.r1; r <= s.r2; r++) m.push(def.cols.slice(s.c1, s.c2 + 1).map(c => clean(c.text(view[r], ctx))));
    const text = window.xlgrid.toTsv(m);
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

  // Фильтр столбца: список значений с поиском и галочками
  function closePop() { if (pop) { pop.remove(); pop = null; } }
  function openFilter(key, k, btn) {
    closePop();
    const def = AN_TABS[key], col = def.cols.find(c => c.k === k), s = gs(key), ctx = anCtx();
    const all = [...new Set((an ? an[key] : []).map(r => col.text(r, ctx)))].sort((a, b) => String(a).localeCompare(String(b), 'ru', { numeric: true }));
    const chosen = new Set(s.filters[k] && s.filters[k].length ? s.filters[k] : all);
    pop = document.createElement('div');
    pop.className = 'al-pop';
    pop.innerHTML = `<input type="search" placeholder="Поиск" title="Найти значение в списке столбца «${esc(col.t)}»"><div class="al-pop-list"></div>
      <div class="al-pop-act"><button type="button" class="dt-sec" data-a="clear" title="Показать все значения столбца и закрыть окно">Сбросить</button><button type="button" data-a="ok" title="Оставить в таблице только отмеченные значения">Применить</button></div>`;
    document.body.appendChild(pop);
    const r = btn.getBoundingClientRect();
    pop.style.top = Math.min(r.bottom + 4, window.innerHeight - 320) + 'px';
    pop.style.left = Math.max(8, Math.min(r.left, window.innerWidth - 270)) + 'px';
    const list = pop.querySelector('.al-pop-list'), search = pop.querySelector('input');
    const paint = () => {
      const term = search.value.trim().toLowerCase();
      list.innerHTML = all.filter(v => String(v).toLowerCase().includes(term)).map(v =>
        `<label title="Показывать строки со значением «${esc(v)}»"><input type="checkbox" title="Отметьте, чтобы показывать строки с этим значением" value="${esc(v)}"${chosen.has(v) ? ' checked' : ''}> ${esc(v)}</label>`).join('') || '<div class="al-muted">Ничего не найдено</div>';
    };
    paint();
    search.addEventListener('input', paint);
    list.addEventListener('change', e => { if (e.target.checked) chosen.add(e.target.value); else chosen.delete(e.target.value); });
    pop.addEventListener('click', e => {
      const a = e.target.closest('[data-a]');
      if (!a) return;
      if (a.dataset.a === 'clear' || chosen.size === all.length) delete s.filters[k]; else s.filters[k] = [...chosen];
      closePop(); renderGrid(key);
    });
    search.focus();
  }

  // ---------- автообновление ----------
  // «Активные» каждые 5 секунд; «История» раз в 30 секунд и только для периода «День» (сегодня)
  setInterval(() => {
    if (!built || !pane.classList.contains('active') || document.hidden) return;
    ticks++;
    if (st.mode === 'active') refresh();
    else if (st.from === today() && st.to === today() && ticks % 6 === 0) refresh();
  }, 5000);

  async function open() {
    if (!built) build();
    const t = presets().day;
    if (!st.from || !st.to) [st.from, st.to] = t;
    if (st.to > today()) st.to = today();
    syncControls();
    if (!plcs.length) { await loadPlcs(); await loadFilters(); syncControls(); }
    renderHead();
    ticks = 0;
    refresh(true);
  }

  // ---------- меню брендов на вкладке (как у «Andon / KPI»: под вкладкой, закрытие по клику вне и по Esc) ----------
  const menu = document.createElement('div');
  menu.id = 'alarms-menu'; menu.className = 'andon-menu'; menu.hidden = true;
  document.body.appendChild(menu);
  let menuTimer = null, menuSuppress = false;

  function renderMenu() {
    const RO = root.reorder;
    const base = orderBrands(plcs.map(p => plcBrand(p.name)));
    const brands = RO ? RO.applyOrder(base, RO.get('order-alarms-brands'), b => b) : base;
    menu.replaceChildren();
    brands.forEach(b => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'am-leaf' + (b === st.brand && tabBtn.classList.contains('active') ? ' active' : '');
      btn.textContent = b;
      btn.dataset.id = b;
      btn.title = `Журнал аварий бренда ${b}: открыть и выбрать ПЛК этого бренда кнопками на экране`;
      btn.addEventListener('click', () => {
        if (!tabBtn.classList.contains('active')) { menuSuppress = true; tabBtn.click(); menuSuppress = false; }
        closeMenu();
        selectBrand(b);
      });
      menu.append(btn);
    });
  }
  if (root.reorder) {
    root.reorder.watch('order-alarms-brands', () => { if (!root.reorder.busy()) renderMenu(); });
    root.reorder.attach(menu, { key: 'order-alarms-brands', itemSelector: ':scope > .am-leaf', idOf: el => el.dataset.id, axis: 'y' });
  }
  function openMenu() {
    clearTimeout(menuTimer);
    renderMenu();
    const r = tabBtn.getBoundingClientRect();
    menu.style.left = r.left + 'px';
    menu.style.top = r.bottom + 'px';
    menu.hidden = false;
    if (!plcs.length) loadPlcs().then(() => { renderMenu(); if (built) renderHead(); });
  }
  function closeMenu(delay = 0) {
    clearTimeout(menuTimer);
    menuTimer = setTimeout(() => { if (root.reorder && root.reorder.busy()) return; menu.hidden = true; }, delay);
  }
  tabBtn.addEventListener('mouseenter', openMenu);
  tabBtn.addEventListener('mouseleave', () => closeMenu(250));
  tabBtn.addEventListener('click', () => { if (!menuSuppress) openMenu(); });
  menu.addEventListener('mouseenter', () => clearTimeout(menuTimer));
  menu.addEventListener('mouseleave', () => closeMenu(250));
  document.addEventListener('click', e => { if (!menu.hidden && !menu.contains(e.target) && !tabBtn.contains(e.target)) closeMenu(); });
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && !menu.hidden) closeMenu(); });

  const prevAuth = window.onAuthChanged;
  window.onAuthChanged = () => { if (prevAuth) prevAuth(); if (built && an && st.mode === 'analytics') drawAn(); };

  tabBtn.addEventListener('click', open);
  if (pane.classList.contains('active')) open();
})(typeof window !== 'undefined' ? window : globalThis);
