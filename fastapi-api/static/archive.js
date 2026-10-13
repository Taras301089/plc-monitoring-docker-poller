// Экран Andon за прошлый день: навигация по дням (◀ дата ▶), та же таблица из базы и выгрузка дня в Excel
(() => {
  'use strict';

  const box = document.querySelector('#andon-day');
  const input = document.querySelector('#andon-day-date');
  const prev = document.querySelector('#andon-day-prev');
  const next = document.querySelector('#andon-day-next');
  const todayBtn = document.querySelector('#andon-day-today');
  let data = null;
  let loadedKey = '';
  let loadedAt = 0;
  let loading = false;
  const REFRESH_MS = 5000;   // правка с другого устройства (отметка дня, план, простой) видна в архиве не позднее чем через 5 секунд

  window.andonDay = null;   // выбранный прошлый день (ГГГГ-ММ-ДД); null — живой экран за сегодня
  window.dtDay = null;      // тот же день для запросов простоев (downtime.js)

  const today = () => window.plantClock.isoDate();
  const addDays = (iso, n) => { const d = new Date(iso + 'T00:00:00Z'); d.setUTCDate(d.getUTCDate() + n); return d.toISOString().slice(0, 10); };
  const ruDate = iso => iso.split('-').reverse().join('.');

  function setDay(iso) {
    const t = today();
    const past = iso && iso < t ? iso : null;
    window.andonDay = past;
    window.dtDay = past;
    document.body.classList.toggle('andon-archive', !!past);
    input.value = past || t;
    input.max = t;
    next.disabled = !past;
    todayBtn.hidden = !past;
    data = null; loadedKey = ''; loadedAt = 0;
    // заставляем экран перестроиться: живая отрисовка при следующем тике, архивная по своим данным
    andonBuilt = '';
    andonLast = null;
    andonMsg = '';
  }
  window.andonSetDay = setDay;

  prev.addEventListener('click', () => setDay(addDays(window.andonDay || today(), -1)));
  next.addEventListener('click', () => { if (window.andonDay) setDay(addDays(window.andonDay, 1)); });
  todayBtn.addEventListener('click', () => setDay(null));
  input.addEventListener('change', () => setDay(input.value || null));
  input.max = today();
  input.value = today();
  next.disabled = true;

  // Отрисовка выбранного прошлого дня; вызывается из andonTick вместо опроса ПЛК
  window.archivePaint = async sel => {
    const day = window.andonDay;
    const key = `${sel.id}|${day}`;
    if (loadedKey !== key || Date.now() - loadedAt > REFRESH_MS) {
      if (loading) { if (loadedKey !== key) return; }   // фоновое обновление уже идёт: пока показываем прежние данные
      else {
        loading = true;
        try {
          const r = await fetch(`/api/kpi/screens/${sel.id}/hourly?date=${day}`, { signal: AbortSignal.timeout(6000) });
          if (r.ok && window.andonDay === day) { data = await r.json(); loadedKey = key; loadedAt = Date.now(); }
        } catch { /* повторим на следующем тике */ } finally { loading = false; }
        if (loadedKey !== key) return;
      }
    }
    setAndonBadge('off', '● архив');
    const list = data.intervals.filter(x => x.plan > 0 || x.fact > 0);
    const n = list.length;
    if (!n) { showAndonMessage(`За ${ruDate(day)} по линии «${sel.name}» данных нет`); return; }
    const built = `arch|${key}|${n}`;
    if (andonBuilt !== built) {
      if (sel.template === 'fl_counter') buildFlLayout(sel, n); else buildKpiLayout(sel, n);
      andonBuilt = built; andonMsg = '';
    }
    andonBody.style.setProperty('--rows', n);
    const q = k => andonBody.querySelector(`[data-k="${k}"]`);
    const set = (el, text) => { if (el && el.textContent !== text) el.textContent = text; };
    set(q('curTakt'), '—');
    const st = q('state'); if (st) st.hidden = true;
    let planSum = 0, factSum = 0, deltaSum = 0;
    const starveVals = [], repairVals = [], bdcVals = [];
    andonBody.querySelectorAll('tbody tr').forEach((tr, pos) => {
      const x = list[pos];
      if (!x) return;
      tr.dataset.i = String(x.idx - 1);   // номер интервала для сопоставления с простоями
      tr.className = '';
      const c = k => tr.querySelector(`[data-k="${k}"]`);
      set(c('time'), `${hhmm(x.start_min)}-${hhmm(x.end_min ?? x.start_min)}`);
      set(c('min'), String(Math.max(0, (x.end_min ?? x.start_min) - x.start_min)));
      set(c('plan'), String(x.plan));
      set(c('fact'), String(x.fact));
      const d = x.fact - x.plan;
      const dc = c('delta');
      if (data.no_output) { set(dc, ''); dc.className = 'delta'; }   // день без выпуска: «±» не показывается
      else {
        set(dc, d > 0 ? `+${d}` : String(d));
        dc.className = 'delta ' + (d >= 0 ? 'pos' : 'neg');
      }
      const t = c('takt');
      set(t, x.takt_sec > 0 ? mmss(Math.floor(x.takt_sec / 60), x.takt_sec % 60) : '—');
      t.className = x.takt_sec > 0 ? '' : 'dim';
      const sc = c('starve');
      if (sc) {   // «Простой RB262» из базы: последнее значение таймера интервала, миллисекунды
        const ms = Number.isInteger(x.starve_ms) ? x.starve_ms : null;
        starveVals.push(ms);
        set(sc, starveFmt(ms));
        sc.className = ms === null ? 'dim' : '';
      }
      const rcEl = c('repair');
      if (rcEl) {   // «Рем. зона» и «Простой BDC» из базы: последние значения интервала (штуки и миллисекунды)
        const rcv = Number.isInteger(x.repair_cnt) ? x.repair_cnt : null;
        repairVals.push(rcv);
        set(rcEl, rcv === null ? '—' : String(rcv));
        rcEl.className = rcv === null ? 'dim' : '';
        const bdv = Number.isInteger(x.bdc_ms) ? x.bdc_ms : null;
        bdcVals.push(bdv);
        const bdEl = c('bdc');
        set(bdEl, starveFmt(bdv));
        bdEl.className = bdv === null ? 'dim' : '';
      }
      planSum += x.plan; factSum += x.fact; deltaSum += d;
    });
    const sSum = q('starveSum');
    if (sSum) { const got = starveVals.filter(x => x !== null); set(sSum, got.length ? starveFmt(got.reduce((a, x) => a + x, 0)) : '—'); }
    const rSum = q('repairSum');
    if (rSum) {
      const got = repairVals.filter(x => x !== null), gb = bdcVals.filter(x => x !== null);
      set(rSum, got.length ? String(got.reduce((a, x) => a + x, 0)) : '—');
      set(q('bdcSum'), gb.length ? starveFmt(gb.reduce((a, x) => a + x, 0)) : '—');
    }
    set(q('planSum'), String(planSum));
    set(q('tot'), String(factSum));
    const ds = q('deltaSum');
    if (data.no_output) { set(ds, ''); ds.className = 'delta'; }
    else {
      set(ds, deltaSum > 0 ? `+${deltaSum}` : String(deltaSum));
      ds.className = 'delta ' + (deltaSum >= 0 ? 'pos' : 'neg');
    }
    // в строке «Итого» подпись «нет выпуска» (серым): простой дня без выпуска не считается
    const lbl = andonBody.querySelector('tfoot td.lbl');
    if (lbl) {
      let tag = lbl.querySelector('.noout');
      if (data.no_output) {
        if (!tag) { tag = document.createElement('span'); tag.className = 'noout'; lbl.appendChild(tag); }
        set(tag, ` · ${data.no_output_label}`);
        tag.title = data.no_output_hint;
      } else if (tag) tag.remove();
    }
    set(q('shTakt'), '—');
    // остаток кузовов на ФЛ — значение «сейчас», к прошлому дню не относится: строка скрыта, а если в ней есть STR (FL RB294),
    // остаётся только он (STR считает сервер по сохранённым факту и «Рем. зона», та же функция, что в Excel)
    const rr = andonBody.querySelector('tr.rest-row');
    if (rr) {
      const strEl = rr.querySelector('[data-k="str"]');
      rr.hidden = !strEl;
      if (strEl) {
        rr.removeAttribute('title');
        rr.querySelectorAll('td:not(.str-cell)').forEach(td => { if (td.textContent) td.textContent = ''; });
        const p = data.str && typeof data.str.pct === 'number' ? data.str.pct : null;
        set(strEl, p === null ? '—' : strFmt(p));
      }
    }
    if (window.dtApply) window.dtApply(sel, andonBody);
  };
})();
