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
  let loading = false;

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
    data = null; loadedKey = '';
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
    if (loadedKey !== key) {
      if (loading) return;
      loading = true;
      try {
        const r = await fetch(`/api/kpi/screens/${sel.id}/hourly?date=${day}`, { signal: AbortSignal.timeout(10000) });
        if (r.ok && window.andonDay === day) { data = await r.json(); loadedKey = key; }
      } catch { /* повторим на следующем тике */ } finally { loading = false; }
      if (loadedKey !== key) return;
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
      set(dc, d > 0 ? `+${d}` : String(d));
      dc.className = 'delta ' + (d >= 0 ? 'pos' : 'neg');
      const t = c('takt');
      set(t, x.takt_sec > 0 ? mmss(Math.floor(x.takt_sec / 60), x.takt_sec % 60) : '—');
      t.className = x.takt_sec > 0 ? '' : 'dim';
      planSum += x.plan; factSum += x.fact; deltaSum += d;
    });
    set(q('planSum'), String(planSum));
    set(q('tot'), String(factSum));
    const ds = q('deltaSum');
    set(ds, deltaSum > 0 ? `+${deltaSum}` : String(deltaSum));
    ds.className = 'delta ' + (deltaSum >= 0 ? 'pos' : 'neg');
    set(q('shTakt'), '—');
    if (window.dtApply) window.dtApply(sel, andonBody);
  };
})();
