// Экраны подсборок (Floor subassembly, Body side): план и факт по часам вводятся вручную прямо в ячейках таблицы
(() => {
  'use strict';

  const REFRESH_MS = 5000;
  const cache = {};     // screenId -> { at, data, loading }

  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const pad2 = n => String(n).padStart(2, '0');
  const hhmm = m => `${pad2(Math.floor(m / 60) % 24)}:${pad2(m % 60)}`;
  const toast = t => (typeof showToast === 'function' ? showToast(t) : alert(t));
  const st = () => (window.authState && window.authState()) || {};
  const canEdit = () => !!(st().user && st().user.role !== 'viewer');
  const fmtTs = ts => plantClock.fmt(ts);

  async function load(screenId, force = false) {
    const c = cache[screenId] || (cache[screenId] = { at: 0, data: null, loading: false });
    if (c.loading || (!force && Date.now() - c.at < REFRESH_MS)) return;
    c.loading = true;
    try {
      const r = await fetch(`/api/kpi/screens/${screenId}/manual`);
      if (r.ok) { c.data = await r.json(); c.at = Date.now(); }
    } catch { /* повторим позже */ } finally { c.loading = false; }
  }

  function build(screen, root, n) {
    root.innerHTML = `
      <div class="andon-layout">
        <aside class="andon-side">
          <div class="tile" title="Данные подсборки вводятся вручную мастерами: нажмите на ячейку плана или факта, введите число и нажмите Enter">
            <label>Ввод данных</label><b style="font-size:.5em">вручную</b><small>нажмите на ячейку «План» или «Факт»</small>
          </div>
          <div class="andon-clockbox" title="Дата и время сервиса на ПК (не часы телевизора или браузера)"><div class="andon-date" data-k="date"></div><div class="andon-time" data-k="time"></div></div>
        </aside>
        <div class="andon-main">
          <h2 class="andon-h">${esc(screen.bindings.brand)}: ${esc(screen.bindings.area)}
            <button type="button" class="man-log-btn" data-act="log" title="Журнал ввода: кто и когда менял план и факт по этому участку">📜 Журнал</button></h2>
          <div class="kpi-wrap"><table class="kpi-table">
            <colgroup><col style="width:4%"><col style="width:13%"><col style="width:6%"><col style="width:8%"><col style="width:10%"><col style="width:7%"><col style="width:12%"><col style="width:40%"></colgroup>
            <thead><tr>
              <th title="Номер часового интервала смены">№</th>
              <th title="Время интервала: такое же, как на Main Line этого бренда">Время</th>
              <th title="Длительность интервала в минутах">Мин.</th>
              <th title="Плановое количество подсборок за интервал: вводится вручную">План</th>
              <th title="Фактическое количество подсборок за интервал: вводится вручную. Пока не введено, простой не считается">Факт</th>
              <th title="Отклонение факта от плана для завершённых интервалов: красный — отставание">±</th>
              <th title="Простой по интервалу в минутах (отставание от плана). Нажмите на число, чтобы указать станцию и причину">Простой</th>
              <th title="Причины простоя: станция, причина и описание, до трёх строк (сначала самые долгие). «+N» — сколько причин не поместилось, все видны по наведению и при нажатии на простой">Причина</th>
            </tr></thead>
            <tbody>${Array.from({ length: n }, (_, i) => `<tr data-i="${i}"><td class="n">${i + 1}</td><td class="time" data-k="time"></td><td data-k="min"></td><td class="man" data-k="plan"></td><td class="man fact" data-k="fact"></td><td class="delta" data-k="delta"></td><td class="dt" data-k="dt"></td><td class="why" data-k="why"></td></tr>`).join('')}</tbody>
            <tfoot><tr><td></td><td class="lbl" colspan="2">Итого за смену</td><td data-k="planSum"></td><td data-k="factSum"></td><td class="delta" data-k="deltaSum"></td><td></td><td></td></tr></tfoot>
          </table></div>
        </div>
      </div>`;
    root.querySelector('[data-act="log"]').addEventListener('click', () => openLog(screen));
    root.querySelector('tbody').addEventListener('click', e => {
      const td = e.target.closest('td.man');
      if (td) startEdit(screen, td);
    });
  }

  function startEdit(screen, td) {
    if (td.querySelector('input')) return;
    if (!st().user) { toast('Войдите в систему, чтобы вводить план и факт'); return; }
    if (!canEdit()) { toast('У вашей роли нет права вводить данные'); return; }
    const tr = td.closest('tr'), field = td.dataset.k, idx = Number(tr.dataset.i) + 1;
    const iv = ((cache[screen.id] || {}).data || { intervals: [] }).intervals.find(x => x.idx === idx);
    const old = iv ? iv[field] : null;
    const input = document.createElement('input');
    input.type = 'number'; input.min = '0'; input.max = '999'; input.step = '1'; input.className = 'man-input';
    input.value = old === null || old === undefined ? '' : String(old);
    input.title = field === 'plan' ? 'План на интервал: целое число, Enter — сохранить, Esc — отмена' : 'Факт за интервал: целое число, Enter — сохранить, Esc — отмена. Пустое поле очищает факт';
    td.textContent = ''; td.appendChild(input);
    input.focus(); input.select();
    let done = false;
    const finish = async save => {
      if (done) return; done = true;
      const raw = input.value.trim();
      td.dataset.sig = '';
      if (!save) { td.textContent = ''; return; }
      const value = raw === '' ? null : Number(raw);
      if (value !== null && (!Number.isInteger(value) || value < 0 || value > 999)) { toast('Введите целое число от 0 до 999'); td.textContent = ''; return; }
      try {
        const r = await fetch(`/api/kpi/screens/${screen.id}/manual/${idx}`, {
          method: 'PUT', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ field, value }),
        });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(typeof d.detail === 'string' ? d.detail : 'Не удалось сохранить');
      } catch (ex) { toast(ex.message); }
      await load(screen.id, true);
      td.textContent = '';
    };
    input.addEventListener('keydown', e => {
      if (e.key === 'Enter') { e.preventDefault(); finish(true); }
      else if (e.key === 'Escape') { e.preventDefault(); finish(false); }
    });
    input.addEventListener('blur', () => finish(true));
  }

  // Вызывается раз в секунду из вкладки Andon; rebuild = экран только что выбран
  window.manualPaint = (screen, root, rebuild) => {
    load(screen.id);
    const data = (cache[screen.id] || {}).data;
    if (!data || !data.date) {
      if (rebuild || !root.querySelector('.empty')) root.innerHTML = '<div class="empty">Сетка интервалов появится, как только сборщик KPI запишет данные Main Line этого бренда.</div>';
      return;
    }
    const n = data.intervals.length;
    if (rebuild || root.dataset.manual !== `${screen.id}|${n}`) { build(screen, root, n); root.dataset.manual = `${screen.id}|${n}`; }
    root.style.setProperty('--rows', n);
    const set = (el, text) => { if (el && el.textContent !== text) el.textContent = text; };
    const clock = window.plantClock;
    if (clock) {
      set(root.querySelector('[data-k="date"]'), clock.dateText());
      set(root.querySelector('[data-k="time"]'), clock.timeText());
    }
    const editable = canEdit();
    let planSum = 0, factSum = 0, deltaSum = 0, hasDelta = false;
    root.querySelectorAll('tbody tr[data-i]').forEach(tr => {
      const iv = data.intervals[Number(tr.dataset.i)];
      if (!iv) return;
      const past = data.now_min >= iv.end_min, cur = data.now_min >= iv.start_min && data.now_min < iv.end_min;
      tr.className = cur ? 'now' : past ? '' : 'future';
      const c = k => tr.querySelector(`[data-k="${k}"]`);
      set(c('time'), `${hhmm(iv.start_min)}-${hhmm(iv.end_min)}`);
      set(c('min'), String(Math.max(0, iv.end_min - iv.start_min)));
      for (const f of ['plan', 'fact']) {
        const td = c(f);
        td.classList.toggle('man-edit', editable);
        td.classList.toggle('man-missing', f === 'fact' && past && iv.plan > 0 && iv.fact === null);
        td.title = (editable ? 'Нажмите, чтобы ввести значение. ' : 'Чтобы вводить данные, войдите в систему (роль кроме «Просмотр»). ')
          + (iv.updated_by ? `Последнее изменение: ${iv.updated_by}, ${fmtTs(iv.updated_at)}` : 'Значение ещё не вводилось');
        if (!td.querySelector('input')) set(td, f === 'plan' ? String(iv.plan) : (iv.fact === null ? '' : String(iv.fact)));
      }
      planSum += iv.plan;
      factSum += iv.fact || 0;
      const dc = c('delta');
      if (past && iv.fact !== null && (iv.plan || iv.fact)) {
        const d = iv.fact - iv.plan;
        deltaSum += d; hasDelta = true;
        set(dc, d > 0 ? `+${d}` : String(d));
        dc.className = 'delta ' + (d >= 0 ? 'pos' : 'neg');
      } else { set(dc, ''); dc.className = 'delta'; }
    });
    set(root.querySelector('[data-k="planSum"]'), String(planSum));
    set(root.querySelector('[data-k="factSum"]'), String(factSum));
    const ds = root.querySelector('[data-k="deltaSum"]');
    if (hasDelta) { set(ds, deltaSum > 0 ? `+${deltaSum}` : String(deltaSum)); ds.className = 'delta ' + (deltaSum >= 0 ? 'pos' : 'neg'); } else set(ds, '');
    if (window.dtApply) window.dtApply(screen, root);
  };

  async function openLog(screen) {
    let list = [];
    try {
      const r = await fetch(`/api/kpi/screens/${screen.id}/manual-log?limit=100`);
      if (r.ok) list = await r.json();
    } catch { /* покажем пустой журнал */ }
    const m = document.createElement('div');
    m.className = 'kpi-modal';
    m.innerHTML = `<div class="kpi-modal-box dt-box" role="dialog" aria-label="Журнал ввода">
      <h3>Журнал ввода — ${esc(screen.bindings.brand)}: ${esc(screen.bindings.area)}</h3>
      <p>Последние 100 изменений плана и факта: кто, когда и что ввёл.</p>
      <table class="man-log"><thead><tr><th>Когда</th><th>Кто</th><th>Интервал</th><th>Поле</th><th>Было → стало</th></tr></thead>
      <tbody>${list.map(x => `<tr><td>${esc(fmtTs(x.ts))}</td><td>${esc(x.user_name)}</td><td>${x.idx}</td><td>${x.field === 'plan' ? 'План' : 'Факт'}</td><td>${x.old_value ?? '—'} → ${x.new_value ?? '—'}</td></tr>`).join('') || '<tr><td colspan="5">Записей пока нет</td></tr>'}</tbody></table>
      <div class="kpi-modal-actions"><button type="button" class="dt-sec" title="Закрыть журнал">Закрыть</button></div></div>`;
    document.body.appendChild(m);
    const close = () => m.remove();
    m.addEventListener('mousedown', e => { if (e.target === m) close(); });
    m.querySelector('button').addEventListener('click', close);
  }
})();
