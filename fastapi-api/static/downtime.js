// Простои на экранах Andon: колонка «Простой» в таблице и панель описания (участок / станция, причина, минуты)
(() => {
  'use strict';

  const REFRESH_MS = 15000;
  const cache = {};     // screenId -> { at, data, loading }
  let dict = null;
  let dictAt = 0;

  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const fmt = n => String(Math.round(n));
  const fmt1 = n => (Number.isInteger(n) ? String(n) : n.toFixed(1));
  const canEdit = () => {
    const st = window.authState && window.authState();
    return !!(st && st.user && st.user.role !== 'viewer');
  };
  const fmtTs = ts => new Date(ts).toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
  // Две отдельные строки: кто внёс строку описания и кто последним её менял (если менял)
  const authorLine = r => {
    let s = `<div title="Кто первым создал эту строку описания и когда">Внёс: <b>${esc(r.created_by_name || '—')}</b>, ${fmtTs(r.created_at)}</div>`;
    if (r.updated_by_name && (r.updated_by_name !== r.created_by_name || Math.abs(new Date(r.updated_at) - new Date(r.created_at)) > 60000)) {
      s += `<div title="Кто последним менял эту строку (станцию, причину, минуты или описание) и когда">Изменил: <b>${esc(r.updated_by_name)}</b>, ${fmtTs(r.updated_at)}</div>`;
    }
    return s;
  };
  const toast = t => (typeof showToast === 'function' ? showToast(t) : alert(t));

  async function load(screenId, force = false) {
    const c = cache[screenId] || (cache[screenId] = { at: 0, data: null, loading: false });
    if (c.loading || (!force && Date.now() - c.at < REFRESH_MS)) return;
    c.loading = true;
    try {
      const r = await fetch(`/api/kpi/screens/${screenId}/downtimes`);
      if (r.ok) { c.data = await r.json(); c.at = Date.now(); }
    } catch { /* повторим позже */ } finally { c.loading = false; }
  }

  async function loadDict() {
    if (dict && Date.now() - dictAt < 30000) return dict;
    const r = await fetch('/api/kpi/dictionary');
    if (!r.ok) throw new Error('Не удалось загрузить справочники');
    dict = await r.json();
    dictAt = Date.now();
    return dict;
  }

  // Вызывается после каждой отрисовки таблицы экрана: заполняет колонку «Простой»
  window.dtApply = (screen, root) => {
    load(screen.id);
    const c = cache[screen.id];
    const list = (c && c.data && c.data.downtimes) || [];
    root.querySelectorAll('tbody tr[data-i]').forEach(tr => {
      const td = tr.querySelector('[data-k="dt"]');
      if (!td) return;
      const d = list.find(x => x.idx === Number(tr.dataset.i) + 1);
      let sig = '';
      if (d && (d.minutes > 0 || d.comments > 0)) {
        const state = d.described >= d.minutes - 0.5 ? 'ok' : d.described > 0 ? 'part' : 'bad';
        sig = `${state}|${fmt(d.minutes)}|${fmt(d.described)}|${d.comments || 0}`;
        if (td.dataset.sig !== sig) {
          const left = Math.max(0, d.minutes - d.described);
          const title = (state === 'ok'
            ? `Простой ${fmt(d.minutes)} мин полностью описан. Нажмите, чтобы посмотреть или изменить`
            : `Простой ${fmt(d.minutes)} мин: факт ${d.fact} против плана ${d.plan}. Не описано ${fmt(left)} мин${state === 'part' ? ` (на кнопке: всего/не описано)` : ''}. Нажмите, чтобы указать станцию и причину`)
            + (d.comments ? `. Комментариев: ${d.comments}` : '');
          // частично описанный простой: «всего/не описано», например 50/25; иначе просто минуты
          const label = state === 'part' ? `${fmt(d.minutes)}/${fmt(left)}` : `${fmt(d.minutes)} мин`;
          td.innerHTML = `<button type="button" class="dt-btn ${state}" title="${esc(title)}">${label}${d.comments ? ` <span class="dt-cm">💬${d.comments}</span>` : ''}<span class="dt-go" aria-hidden="true">›</span></button>`;
          td.querySelector('button').addEventListener('click', () => openPanel(screen, d.idx, tr));
          td.dataset.sig = sig;
        }
      } else if (td.dataset.sig) { td.textContent = ''; td.dataset.sig = ''; }
    });
  };

  async function openPanel(screen, idx, tr) {
    await load(screen.id, true);
    const d = ((cache[screen.id].data || {}).downtimes || []).find(x => x.idx === idx);
    if (!d) return;
    const time = tr.querySelector('[data-k="time"]');
    showPanel(screen, d, time ? time.textContent : '', cache[screen.id].data.scope);
  }

  // Открыть простой из уведомления: выбираем его экран и показываем панель
  window.dtOpenById = async (id, itemId = null, stay = false) => {
    let data;
    try {
      const r = await fetch(`/api/kpi/downtimes/${id}`);
      if (!r.ok) throw new Error();
      data = await r.json();
    } catch { toast('Простой не найден: возможно, он уже пересчитан'); return; }
    const screen = (typeof andonScreens !== 'undefined' ? andonScreens : []).find(s => s.id === data.screen_id);
    if (!screen) { toast('Экран этого простоя недоступен'); return; }
    // stay: открыть панель поверх текущей вкладки (например, из списка «Простои»), не переключая экран Andon
    if (!stay) {
      const tab = document.querySelector('.tab[data-tab="andon"]');
      if (andonScreenId !== screen.id) {
        const leaf = document.querySelector(`#andon-menu [data-screen="${screen.id}"]`);
        if (leaf) leaf.click();
      } else if (!tab.classList.contains('active')) tab.click();
      const menu = document.querySelector('#andon-menu');
      if (menu) menu.hidden = true;
    }
    const [y, mo, da] = String(data.date || '').split('-');
    const hm = m => `${String(Math.floor(m / 60) % 24).padStart(2, '0')}:${String(m % 60).padStart(2, '0')}`;
    const when = data.start_min !== null ? `${da}.${mo} ${hm(data.start_min)}-${hm(data.end_min)}` : `${da}.${mo}`;
    showPanel(screen, data.downtime, when, data.scope, itemId);
  };

  async function showPanel(screen, d, timeText, scope, openItemId = null) {
    let dd;
    try { dd = await loadDict(); } catch (e) { toast(e.message); return; }
    // Экран бренда — это Main Line (подсборки получат свои экраны); у Finish Line один участок. Участок не спрашиваем
    let areas = dd.areas.filter(a => !scope || a.scope === scope);
    if (screen.template === 'body_counter') areas = areas.filter(a => a.name === 'Main Line');
    if (screen.template === 'manual_counter') areas = areas.filter(a => a.name === screen.bindings.area);
    const single = areas.length === 1;
    const editable = canEdit();
    let rows = d.items.map(i => ({ ...i }));
    if (!rows.length && editable) rows.push({ id: null, area_id: single ? areas[0].id : null, station_id: null, reason_id: null, minutes: Math.round(d.minutes * 10) / 10, note: '' });

    const m = document.createElement('div');
    m.className = 'kpi-modal';
    m.innerHTML = `<div class="kpi-modal-box dt-box" role="dialog" aria-label="Простой">
      <h3>Простой ${fmt(d.minutes)} мин — ${esc(screen.name)}</h3>
      <p>Интервал ${esc(timeText)}: план ${d.plan}, факт ${d.fact}. Добавьте причины, из-за которых линия отстала. Если причин несколько, нажмите «+ причина».</p>
      <div class="dt-rows"></div>
      <div class="dt-sum"></div>
      <div class="auth-err" hidden></div>
      <div class="kpi-modal-actions">
        ${editable ? '<button type="button" data-act="add" class="dt-sec" title="Добавить ещё одну строку: другая станция или причина, на которую пришлась часть простоя">+ причина</button>' : ''}
        <span style="flex:1"></span>
        <button type="button" data-act="cancel" class="dt-sec" title="Закрыть окно без сохранения изменений">${editable ? 'Отмена' : 'Закрыть'}</button>
        ${editable ? '<button type="button" data-act="save" title="Сохранить описание простоя: станции, причины и минуты. Сохранённое увидят все">Сохранить</button>' : ''}
      </div>
      ${editable ? '' : '<p class="dt-hint">Чтобы описать простой и комментировать, войдите в систему (роль кроме «Просмотр»).</p>'}
    </div>`;
    document.body.appendChild(m);
    const refreshTable = async () => {
      await load(screen.id, true);
      if (window.dtApply && andonScreenId === screen.id) window.dtApply(screen, document.querySelector('#andon'));
      if (window.dtListRefresh) window.dtListRefresh();     // список «Простои» обновляется после правок
    };
    // Комментарии привязаны к строкам; контейнер обсуждения (id строки -> элемент) сохраняется при перерисовке строк,
    // чтобы не терять набранный, но не отправленный текст
    const threads = new Map();
    const close = () => m.remove();
    m.addEventListener('mousedown', e => { if (e.target === m) close(); });
    const box = m.querySelector('.dt-rows'), sum = m.querySelector('.dt-sum'), err = m.querySelector('.auth-err');
    const reasonName = id => (dd.reasons.find(r => r.id === id) || {}).name;
    const stationOf = id => { for (const a of dd.areas) { const s = a.stations.find(x => x.id === id); if (s) return s; } return null; };

    function updateSum() {
      const s = rows.reduce((t, r) => t + (Number(r.minutes) || 0), 0);
      const left = d.minutes - s;
      // всё время уже описано: новую строку добавлять не нужно
      const addBtn = m.querySelector('[data-act="add"]');
      if (addBtn) {
        addBtn.disabled = left <= 0.5;
        addBtn.title = left <= 0.5
          ? 'Всё время простоя уже описано. Чтобы добавить ещё причину, уменьшите минуты в одной из строк'
          : 'Добавить ещё одну строку: другая станция или причина, на которую пришлась часть простоя';
      }
      sum.className = 'dt-sum' + (left < -1 ? ' over' : '');
      sum.textContent = left < -1
        ? `Описано ${fmt1(s)} из ${fmt1(d.minutes)} мин — больше времени простоя`
        : `Описано ${fmt1(s)} из ${fmt1(d.minutes)} мин${left > 0.5 ? `, не описано ${fmt1(left)} мин` : ''}`;
    }

    function render() {
      box.innerHTML = '';
      rows.forEach((r, n) => {
        const row = document.createElement('div');
        row.className = 'dt-row';
        const opt = (list, sel, ph) => `<option value="">${ph}</option>` + list.map(o => `<option value="${o.id}"${o.id === sel ? ' selected' : ''}>${esc(o.name)}</option>`).join('');
        const area = areas.find(a => a.id === r.area_id);
        const stations = (area ? area.stations : []).filter(s => s.is_active || s.id === r.station_id);
        const reasons = dd.reasons.filter(x => x.is_active || x.id === r.reason_id);
        const needNote = reasonName(r.reason_id) === 'Другое' || (stationOf(r.station_id) || {}).name === 'Другое';
        // строку описания меняет только её автор (и администратор с начальниками); остальным она доступна только для комментариев
        const meU = (window.authState && window.authState().user) || null;
        const mine = !r.id || (!!meU && (r.created_by === meU.id || ['admin', 'chief', 'area_head'].includes(meU.role)));
        const canRow = editable && mine;
        const ro = canRow ? '' : ' disabled';
        row.innerHTML = `
          ${single ? '' : `<select data-f="area"${ro} title="Участок, где произошёл простой. Если участок определить нельзя, оставьте пустым — обязательна только причина">${opt(areas.filter(a => a.is_active || a.id === r.area_id), r.area_id, 'Участок…')}</select>`}
          <select data-f="station"${ro} title="Станция (пост), на которой был простой. Список зависит от выбранного участка">${opt(single ? areas[0].stations.filter(s => s.is_active || s.id === r.station_id) : stations, r.station_id, 'Станция…')}</select>
          <select data-f="reason"${ro} title="Причина простоя — обязательное поле. Если подходящей нет, выберите «Другое» и опишите текстом">${opt(reasons, r.reason_id, 'Причина…')}</select>
          <input data-f="minutes" type="number" min="0.1" max="999" step="0.1" value="${r.minutes ?? ''}"${ro} title="Сколько минут из общего простоя приходится на эту причину">
          ${canRow ? `<button type="button" data-f="del" class="dt-x" title="Убрать эту строку из описания простоя">✕</button>` : '<span></span>'}
          ${canRow || r.note ? `<input data-f="note" class="dt-note${needNote && !(r.note || '').trim() ? ' need' : ''}" maxlength="500" value="${esc(r.note)}" placeholder="${needNote ? 'Опишите, что произошло (обязательно для «Другое»)' : 'Описание: что произошло (необязательно)'}"${ro} title="Свободное описание: что именно произошло. Для причины или станции «Другое» заполнять обязательно">` : ''}
          ${r.id
            ? `<div class="dt-meta"><div class="dt-who">${authorLine(r)}</div></div>${editable && !mine ? `<div class="dt-lock" title="Строку описания меняет только тот, кто её внёс (и администратор с начальниками)">🔒 Строку описал(а) ${esc(r.created_by_name)}: изменить её может только автор, остальные могут комментировать</div>` : ''}`
            : (editable ? '<div class="dt-meta"><span title="Комментировать можно только сохранённую строку">Сохраните строку, чтобы её можно было комментировать</span></div>' : '')}`;
        const on = (f, ev, fn) => { const el = row.querySelector(`[data-f="${f}"]`); if (el) el.addEventListener(ev, fn); };
        on('area', 'change', e => { r.area_id = e.target.value ? Number(e.target.value) : null; r.station_id = null; render(); });
        on('station', 'change', e => {
          r.station_id = e.target.value ? Number(e.target.value) : null;
          if (single && r.station_id) r.area_id = areas[0].id;
          render();
        });
        on('reason', 'change', e => { r.reason_id = e.target.value ? Number(e.target.value) : null; render(); });
        on('minutes', 'input', e => { r.minutes = e.target.value === '' ? null : Number(e.target.value); updateSum(); });
        on('note', 'input', e => { r.note = e.target.value; });
        on('del', 'click', () => { threads.delete(r.id); rows.splice(n, 1); render(); });
        if (r.id) {
          // комментарии строки всегда раскрыты: ответ сразу виден
          row.dataset.item = String(r.id);
          let el = threads.get(r.id);
          if (!el && window.dtComments) {
            el = document.createElement('div');
            el.className = 'dt-comments';
            threads.set(r.id, el);
            window.dtComments.mount(el, r.id, () => refreshTable());
          }
          if (el) row.appendChild(el);
        }
        box.appendChild(row);
      });
      updateSum();
    }
    render();
    // открыли из уведомления: показываем строку, где написали
    if (openItemId) setTimeout(() => { const t = threads.get(openItemId); if (t) t.scrollIntoView({ block: 'center' }); }, 400);

    m.querySelector('[data-act="cancel"]').addEventListener('click', close);
    const add = m.querySelector('[data-act="add"]');
    if (add) add.addEventListener('click', () => {
      const left = Math.max(0, Math.round((d.minutes - rows.reduce((t, r) => t + (Number(r.minutes) || 0), 0)) * 10) / 10);
      if (left <= 0.5) { toast('Всё время простоя уже описано'); return; }
      rows.push({ id: null, area_id: single ? areas[0].id : null, station_id: null, reason_id: null, minutes: left || null, note: '' });
      render();
    });
    const save = m.querySelector('[data-act="save"]');
    if (save) save.addEventListener('click', async () => {
      err.hidden = true;
      for (const [n, r] of rows.entries()) {
        const msg = !r.reason_id ? 'выберите причину' : !(r.minutes > 0) ? 'укажите минуты' : '';
        if (msg) { err.textContent = `Строка ${n + 1}: ${msg}`; err.hidden = false; return; }
      }
      save.disabled = true;
      try {
        const resp = await fetch(`/api/kpi/downtimes/${d.id}/items`, {
          method: 'PUT', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ items: rows.map(r => ({ id: r.id, area_id: r.area_id, station_id: r.station_id, reason_id: r.reason_id, minutes: Number(r.minutes), note: r.note || '' })) }),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Проверьте заполнение строк');
        close();
        toast('Описание простоя сохранено');
        await refreshTable();
      } catch (ex) { err.textContent = ex.message; err.hidden = false; save.disabled = false; }
    });
  }
})();
