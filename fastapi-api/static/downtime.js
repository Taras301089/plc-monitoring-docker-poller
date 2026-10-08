// Простои на экранах Andon: колонка «Простой» в таблице и панель описания (участок / станция, причина, минуты)
(() => {
  'use strict';

  const REFRESH_MS = 15000;
  const cache = {};     // «экран|день» -> { at, data, loading }
  const ck = id => id + '|' + (window.dtDay || '');   // window.dtDay: выбранный прошлый день (архив), пусто — текущий
  let dict = null;
  let dictAt = 0;

  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const fmt = n => String(Math.round(n));
  const fmt1 = n => (Number.isInteger(n) ? String(n) : n.toFixed(1));
  const canEdit = () => {
    const st = window.authState && window.authState();
    return !!(st && st.user && st.user.role !== 'viewer');
  };
  const fmtTs = ts => plantClock.fmt(ts);
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
    const k = ck(screenId);
    const c = cache[k] || (cache[k] = { at: 0, data: null, loading: false });
    if (c.loading || (!force && Date.now() - c.at < REFRESH_MS)) return;
    c.loading = true;
    try {
      // без ответа дольше 10 секунд запрос прерываем, иначе загрузка могла бы зависнуть навсегда
      const r = await fetch(`/api/kpi/screens/${screenId}/downtimes${window.dtDay ? '?date=' + window.dtDay : ''}`, { signal: AbortSignal.timeout(10000) });
      if (r.ok) { c.data = await r.json(); c.at = Date.now(); }
    } catch { /* повторим позже, пока показываем последние известные данные */ } finally { c.loading = false; }
  }

  let dictBusy = null;
  async function loadDict(maxAge = 30000) {
    if (dict && Date.now() - dictAt < maxAge) return dict;
    if (dictBusy) return dictBusy;
    dictBusy = (async () => {
      try {
        const r = await fetch('/api/kpi/dictionary', { signal: AbortSignal.timeout(6000) });
        if (!r.ok) throw new Error('Не удалось загрузить справочники');
        dict = await r.json();
        dictAt = Date.now();
        return dict;
      } finally { dictBusy = null; }
    })();
    return dictBusy;
  }

  // Вызывается после каждой отрисовки таблицы экрана: заполняет колонку «Простой»
  window.dtApply = (screen, root) => {
    load(screen.id);
    const c = cache[ck(screen.id)];
    // пока данные простоев не получены (обрыв связи, перезапуск сервера), ячейки не трогаем
    if (!c || !c.data) return;
    const list = c.data.downtimes || [];
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
          const label = state === 'part' ? `${fmt(d.minutes)}/${fmt(left)}` : fmt(d.minutes);
          // число комментариев в кнопке не показываем: сами комментарии выводятся в колонке «Причина»
          td.innerHTML = `<button type="button" class="dt-btn ${state}" title="${esc(title)}">${label}<span class="dt-go" aria-hidden="true">›</span></button>`;
          td.querySelector('button').addEventListener('click', () => openPanel(screen, d.idx, tr));
          td.dataset.sig = sig;
        }
      } else if (td.dataset.sig) { td.textContent = ''; td.dataset.sig = ''; }
      fillWhy(tr, d && (d.minutes > 0 || d.comments > 0) ? d : null);
      // клик по «не описан» открывает тот же редактор простоя, что и кнопка с минутами в этой строке
      const whyTd = tr.querySelector('[data-k="why"]');
      if (whyTd) whyTd.onclick = e => {
        if (!e.target.closest('.why-none-click') || !d) return;
        openPanel(screen, d.idx, tr);
      };
    });
    // названия станций и причин берём из справочника: когда загрузится, перерисовываем колонку
    // табло открыто сутками: справочник перечитываем не реже раза в 10 секунд, иначе новая причина показывается как «—»
    if (!dict || Date.now() - dictAt >= 10000) loadDict(10000).then(() => window.dtApply(screen, root)).catch(() => {});
  };

  // Подгонка по ширине: показываем столько колонок-причин, сколько помещается; остальные прячем и пишем «+N»
  function fitWhy(td) {
    const box = td.querySelector('.why-cols');
    if (!box) return;
    box.querySelectorAll('.why-more').forEach(e => e.remove());
    const cols = [...box.querySelectorAll('.why-col')];
    cols.forEach(c => { c.hidden = false; });
    const fits = () => cols.every(c => c.hidden || c.offsetLeft + c.offsetWidth <= box.clientWidth + 1);
    if (fits()) return;
    const items = cols.filter(c => !c.classList.contains('why-left'));
    const more = document.createElement('span');
    more.className = 'why-more';
    box.appendChild(more);
    let hiddenN = 0;
    for (let i = cols.length - 1; i >= 0 && !fits(); i--) {
      cols[i].hidden = true;
      if (items.includes(cols[i])) hiddenN++;
      more.textContent = hiddenN ? `+${hiddenN}` : '';
    }
    if (!hiddenN) more.remove();
  }
  window.addEventListener('resize', () => document.querySelectorAll('td.why').forEach(fitWhy));

  // Колонка «Причина»: до трёх строк (причина, описание, следующая причина...), остальное сворачивается в «+N»
  function fillWhy(tr, d) {
    const td = tr.querySelector('[data-k="why"]');
    if (!td) return;
    let html = '', title = '';
    if (d && dict) {
      const its = d.items || [];
      if (!its.length) {
        html = '<span class="why-none why-none-click" title="Простой не описан: нажмите, чтобы открыть редактор и указать станцию и причину этого интервала">не описан</span>';
        title = 'Простой ещё не описан: нажмите на простой, чтобы указать станцию и причину';
      } else {
        const name = it => {
          let st = null;
          for (const a of dict.areas) { const s = a.stations.find(x => x.id === it.station_id); if (s) { st = s; break; } }
          const area = dict.areas.find(a => a.id === it.area_id);
          const reason = (dict.reasons.find(r => r.id === it.reason_id) || {}).name || '—';
          return { place: (st && st.name) || (area && area.name) || '', reason, min: it.minutes };
        };
        // причины по убыванию минут; у каждой строка «станция · причина» (минуты, если причин несколько) и под ней описание
        const all = its.map(it => ({ ...name(it), note: (it.note || '').trim(), comment: it.last_comment || null, who: it.author || '' })).sort((a, b) => b.min - a.min);
        const left = Math.max(0, d.minutes - d.described);
        // при малом числе интервалов строки высокие и помещается четыре строки текста, иначе три
        const rowsN = [...tr.parentElement.children].filter(x => !x.hidden).length;   // скрытые (неактивные) интервалы не считаются
        const MAX_LINES = rowsN <= 8 ? 4 : rowsN <= 11 ? 3 : 2;
        // каждая причина занимает свою колонку (до MAX_LINES строк); колонки идут слева направо, что не поместилось по ширине, скрывается в «+N»
        const cols = [];
        for (const x of all) {
          const lines = [];
          lines.push({ c: 'why-main', h:`${esc(`${x.place ? x.place + ' · ' : ''}${x.reason}${all.length > 1 ? ' · ' + fmt(x.min) + ' мин' : ''}`)}${x.who ? ` <span class="why-by" title="Кто описал причину простоя: фамилия и должность">· ${esc(x.who)}</span>` : ''}` });
          if (x.note && lines.length < MAX_LINES) lines.push({ c: 'why-note', t: x.note });
          if (x.comment && lines.length < MAX_LINES) lines.push({ c: 'why-comment', h: `<span class="why-re" title="Ответ или дополнение к описанию выше">↳</span> ${x.comment.is_oto ? '<span class="cm-badge oto" title="Комментарий оставил сотрудник ОТО">ОТО</span> ' : ''}${esc(x.comment.text)}${x.comment.author ? ` <span class="why-by" title="Кто написал комментарий: фамилия и должность">· ${esc(x.comment.author)}</span>` : ''}` });
          cols.push(`<div class="why-col">${lines.map(l => `<span class="${l.c}">${l.h || esc(l.t)}</span>`).join('')}</div>`);
        }
        if (left > 0.5) cols.push(`<div class="why-col why-left"><span class="why-none">не описано ${fmt(left)} мин</span></div>`);
        html = `<div class="why-cols">${cols.join('')}</div>`;
        title = all.map(x => `${x.place ? x.place + ' · ' : ''}${x.reason}: ${fmt(x.min)} мин${x.who ? ' (' + x.who + ')' : ''}${x.note ? ' — ' + x.note : ''}${x.comment ? `\n   ${x.comment.is_oto ? 'ОТО: ' : ''}${x.comment.text}${x.comment.author ? ' (' + x.comment.author + ')' : ''}` : ''}`).join('\n')
          + (left > 0.5 ? `\nНе описано ${fmt(left)} мин` : '');
      }
    }
    if (td.dataset.sig !== html) { td.innerHTML = html; td.dataset.sig = html; fitWhy(td); }
    td.title = title;
  }

  async function openPanel(screen, idx, tr) {
    await load(screen.id, true);
    const d = ((cache[ck(screen.id)].data || {}).downtimes || []).find(x => x.idx === idx);
    if (!d) return;
    const time = tr.querySelector('[data-k="time"]');
    showPanel(screen, d, time ? time.textContent : '', cache[ck(screen.id)].data.scope);
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
        // удалить строку может только её автор или администратор (удалённое сохраняется в журнале)
        const canDel = editable && (!r.id || (!!meU && (r.created_by === meU.id || meU.role === 'admin')));
        const ro = canRow ? '' : ' disabled';
        row.innerHTML = `
          ${single ? '' : `<select data-f="area"${ro} title="Участок, где произошёл простой. Если участок определить нельзя, оставьте пустым — обязательна только причина">${opt(areas.filter(a => a.is_active || a.id === r.area_id), r.area_id, 'Участок…')}</select>`}
          <select data-f="station"${ro} title="Станция (пост), на которой был простой. Список зависит от выбранного участка">${opt(single ? areas[0].stations.filter(s => s.is_active || s.id === r.station_id) : stations, r.station_id, 'Станция…')}</select>
          <select data-f="reason"${ro} title="Причина простоя — обязательное поле. Если подходящей нет, выберите «Другое» и опишите текстом">${opt(reasons, r.reason_id, 'Причина…')}</select>
          <input data-f="minutes" type="number" min="0.1" max="999" step="0.1" value="${r.minutes ?? ''}"${ro} title="Сколько минут из общего простоя приходится на эту причину">
          ${canDel ? `<button type="button" data-f="del" class="dt-x" title="Удалить эту строку описания простоя: удалить может только автор или администратор, запись остаётся в журнале">Удалить</button>` : '<span></span>'}
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
        const noteEl = row.querySelector('[data-f="note"]');   // @ в описании: подсказка коллег, отмеченные получают уведомление
        if (noteEl && window.dtComments && window.dtComments.attachMentions) { r._men = r._men || new Map(); r._ids = window.dtComments.attachMentions(noteEl, r._men); }
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
          body: JSON.stringify({ items: rows.map(r => ({ id: r.id, area_id: r.area_id, station_id: r.station_id, reason_id: r.reason_id, minutes: Number(r.minutes), note: r.note || '', mentions: r._ids ? r._ids() : [] })) }),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Проверьте заполнение строк');
        // набранные, но не отправленные комментарии уходят вместе с сохранением
        for (const el of threads.values()) {
          if (el._flushDraft && !(await el._flushDraft())) throw new Error('Описание сохранено, но комментарий не отправлен: проверьте текст');
        }
        close();
        toast('Описание простоя сохранено');
        await refreshTable();
      } catch (ex) { err.textContent = ex.message; err.hidden = false; save.disabled = false; }
    });
  }
})();
