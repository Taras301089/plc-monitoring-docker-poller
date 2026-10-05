// Справочники простоев: причины, участки и станции (правят администратор и начальники)
(() => {
  'use strict';

  const tab = document.querySelector('#tab-dictionary');
  const pane = document.querySelector('#dictionary');
  const EDITORS = ['admin', 'chief', 'area_head'];
  let data = null;
  let scope = 'Chery';
  let showArchived = false;

  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const canEdit = () => {
    const st = window.authState && window.authState();
    return !!(st && st.user && EDITORS.includes(st.user.role));
  };

  async function call(method, url, body) {
    try {
      const r = await fetch(url, {
        method, credentials: 'same-origin',
        headers: body ? { 'Content-Type': 'application/json' } : {},
        body: body ? JSON.stringify(body) : undefined,
      });
      let d = null;
      try { d = await r.json(); } catch { /* пустой ответ */ }
      return { ok: r.ok, data: d };
    } catch {
      return { ok: false, data: { detail: 'Нет связи с сервером' } };
    }
  }
  const errText = r => (r.data && typeof r.data.detail === 'string') ? r.data.detail : 'Проверьте правильность заполнения';
  const toast = text => (typeof showToast === 'function' ? showToast(text) : alert(text));

  async function load() {
    const r = await call('GET', '/api/kpi/dictionary');
    if (!r.ok) { pane.innerHTML = `<div class="empty">${esc(errText(r))}</div>`; return; }
    data = r.data;
    render();
  }

  // Действие + перерисовка. Ошибку показываем всплывающим сообщением.
  async function act(method, url, body, okText) {
    const r = await call(method, url, body);
    toast(r.ok ? okText : errText(r));
    if (r.ok) await load();
    return r.ok;
  }

  // Обмен порядком двух соседних записей
  async function swap(kind, list, i, j, extra) {
    const a = list[i], b = list[j];
    if (!a || !b) return;
    const put = (x, order) => call('PUT', `/api/kpi/${kind}/${x.id}`, { ...extra(x), is_active: x.is_active, sort_order: order });
    const ao = a.sort_order, bo = b.sort_order;
    // при равных значениях порядка разводим их
    const [na, nb] = ao === bo ? [bo + (i < j ? 1 : -1), bo] : [bo, ao];
    await put(a, na); await put(b, nb);
    await load();
  }

  const row = (item, i, list, kind, extra, label) => `
    <tr class="${item.is_active ? '' : 'off'}" data-kind="${kind}" data-id="${item.id}" data-i="${i}">
      <td class="num">${i + 1}</td>
      <td><b>${esc(item.name)}</b>${item.hint ? ` <span class="dict-hint">${esc(item.hint)}</span>` : ''}${item.is_active ? '' : ' <span class="badge off">в архиве</span>'}</td>
      <td class="acts">
        <button type="button" data-a="up" ${i === 0 ? 'disabled' : ''} title="Поднять ${label} выше в списке выбора">▲</button>
        <button type="button" data-a="down" ${i === list.length - 1 ? 'disabled' : ''} title="Опустить ${label} ниже в списке выбора">▼</button>
        <button type="button" data-a="rename" title="Переименовать ${label}: старые записи простоев сохранят привязку к нему">Изменить</button>
        <button type="button" data-a="toggle" title="${item.is_active ? 'Убрать из списков выбора (архив). Старые записи простоев сохранятся' : 'Вернуть из архива в списки выбора'}">${item.is_active ? 'В архив' : 'Вернуть'}</button>
      </td>
    </tr>`;

  function render() {
    const edit = canEdit();
    const visible = list => (showArchived ? list : list.filter(x => x.is_active));
    const reasons = visible(data.reasons);
    const areas = data.areas.filter(a => a.scope === scope && (showArchived || a.is_active));
    const scopeTabs = Object.entries(data.scopes).map(([k, v]) =>
      `<button type="button" class="dict-scope ${k === scope ? 'active' : ''}" data-scope="${esc(k)}" title="Показать участки и станции направления: ${esc(v)}">${esc(v)}</button>`).join('');

    pane.innerHTML = `
      <div class="users-top"><h2>📚 Справочники простоев</h2>
        <span class="grow">Списки для описания простоев. Удалять нельзя — записи уходят в архив, чтобы история простоев не потеряла названия.</span>
        <label class="check" title="Показывать записи из архива, чтобы вернуть их в списки выбора"><input type="checkbox" id="dict-arch" ${showArchived ? 'checked' : ''}> Показать архив</label></div>

      <div class="dict-grid">
        <section class="dict-card">
          <div class="dict-head"><h3>Причины простоев</h3>
            ${edit ? '<button type="button" id="reason-add" title="Добавить новую причину простоя в общий список (для всех брендов и Finish Line)">+ Причина</button>' : ''}</div>
          <table class="users-table dict-table"><tbody id="dict-reasons">
            ${reasons.map((r, i) => row(r, i, reasons, 'reasons', x => ({ name: x.name, hint: x.hint }), 'причину')).join('')}
          </tbody></table>
        </section>

        <section class="dict-card">
          <div class="dict-head"><h3>Участки и станции</h3>
            <div class="dict-scopes">${scopeTabs}</div>
            ${edit ? '<button type="button" id="area-add" title="Добавить новый участок в выбранное направление">+ Участок</button>' : ''}</div>
          ${areas.map(a => {
            const sts = showArchived ? a.stations : a.stations.filter(s => s.is_active);
            return `<div class="dict-area ${a.is_active ? '' : 'off'}" data-area="${a.id}">
              <div class="dict-area-head"><b>${esc(a.name)}</b><span class="dict-hint">${sts.length} станций</span>${a.is_active ? '' : '<span class="badge off">в архиве</span>'}
                ${edit ? `<span class="acts">
                  <button type="button" data-a="area-up" title="Поднять участок выше">▲</button>
                  <button type="button" data-a="area-down" title="Опустить участок ниже">▼</button>
                  <button type="button" data-a="area-rename" title="Переименовать участок">Изменить</button>
                  <button type="button" data-a="area-toggle" title="${a.is_active ? 'Убрать участок из списков выбора (архив)' : 'Вернуть участок из архива'}">${a.is_active ? 'В архив' : 'Вернуть'}</button>
                  <button type="button" data-a="st-add" title="Добавить станцию (пост) в этот участок">+ Станция</button></span>` : ''}</div>
              <table class="users-table dict-table"><tbody>
                ${sts.map((s, i) => row(s, i, sts, 'stations', x => ({ name: x.name }), 'станцию')).join('')}
              </tbody></table></div>`;
          }).join('') || '<div class="empty">В этом направлении нет участков</div>'}
        </section>
      </div>`;
    bind(edit, areas);
  }

  function bind(edit, areas) {
    pane.querySelector('#dict-arch').addEventListener('change', e => { showArchived = e.target.checked; render(); });
    pane.querySelectorAll('.dict-scope').forEach(b => b.addEventListener('click', () => { scope = b.dataset.scope; render(); }));
    if (!edit) return;

    const reasons = showArchived ? data.reasons : data.reasons.filter(r => r.is_active);
    pane.querySelector('#reason-add').addEventListener('click', () => {
      const name = (prompt('Название новой причины простоя') || '').trim();
      if (!name) return;
      const hint = (prompt('Пояснение к причине (можно оставить пустым)') || '').trim();
      act('POST', '/api/kpi/reasons', { name, hint }, 'Причина добавлена');
    });
    pane.querySelector('#area-add').addEventListener('click', () => {
      const name = (prompt('Название нового участка') || '').trim();
      if (name) act('POST', '/api/kpi/areas', { scope, name }, 'Участок добавлен');
    });

    // причины
    pane.querySelectorAll('#dict-reasons tr').forEach(tr => {
      const i = Number(tr.dataset.i), item = reasons[i];
      const body = o => ({ name: item.name, hint: item.hint, is_active: item.is_active, ...o });
      tr.querySelector('[data-a="up"]').addEventListener('click', () => swap('reasons', reasons, i, i - 1, x => ({ name: x.name, hint: x.hint })));
      tr.querySelector('[data-a="down"]').addEventListener('click', () => swap('reasons', reasons, i, i + 1, x => ({ name: x.name, hint: x.hint })));
      tr.querySelector('[data-a="rename"]').addEventListener('click', () => {
        const name = (prompt('Название причины', item.name) || '').trim();
        if (!name) return;
        const hint = prompt('Пояснение к причине', item.hint);
        act('PUT', `/api/kpi/reasons/${item.id}`, body({ name, hint: hint === null ? item.hint : hint.trim() }), 'Причина изменена');
      });
      tr.querySelector('[data-a="toggle"]').addEventListener('click', () =>
        act('PUT', `/api/kpi/reasons/${item.id}`, body({ is_active: !item.is_active }), item.is_active ? 'Причина в архиве' : 'Причина возвращена'));
    });

    // участки и станции
    pane.querySelectorAll('.dict-area').forEach(box => {
      const ai = areas.findIndex(a => a.id === Number(box.dataset.area)), area = areas[ai];
      const abody = o => ({ name: area.name, is_active: area.is_active, ...o });
      const aextra = x => ({ name: x.name });
      box.querySelector('[data-a="area-up"]').addEventListener('click', () => swap('areas', areas, ai, ai - 1, aextra));
      box.querySelector('[data-a="area-down"]').addEventListener('click', () => swap('areas', areas, ai, ai + 1, aextra));
      box.querySelector('[data-a="area-rename"]').addEventListener('click', () => {
        const name = (prompt('Название участка', area.name) || '').trim();
        if (name) act('PUT', `/api/kpi/areas/${area.id}`, abody({ name }), 'Участок изменён');
      });
      box.querySelector('[data-a="area-toggle"]').addEventListener('click', () =>
        act('PUT', `/api/kpi/areas/${area.id}`, abody({ is_active: !area.is_active }), area.is_active ? 'Участок в архиве' : 'Участок возвращён'));
      box.querySelector('[data-a="st-add"]').addEventListener('click', () => {
        const name = (prompt(`Название новой станции в участке «${area.name}»`) || '').trim();
        if (name) act('POST', `/api/kpi/areas/${area.id}/stations`, { name }, 'Станция добавлена');
      });
      const sts = showArchived ? area.stations : area.stations.filter(s => s.is_active);
      box.querySelectorAll('tr[data-kind="stations"]').forEach(tr => {
        const i = Number(tr.dataset.i), item = sts[i];
        const sbody = o => ({ name: item.name, is_active: item.is_active, ...o });
        tr.querySelector('[data-a="up"]').addEventListener('click', () => swap('stations', sts, i, i - 1, x => ({ name: x.name })));
        tr.querySelector('[data-a="down"]').addEventListener('click', () => swap('stations', sts, i, i + 1, x => ({ name: x.name })));
        tr.querySelector('[data-a="rename"]').addEventListener('click', () => {
          const name = (prompt('Название станции', item.name) || '').trim();
          if (name) act('PUT', `/api/kpi/stations/${item.id}`, sbody({ name }), 'Станция изменена');
        });
        tr.querySelector('[data-a="toggle"]').addEventListener('click', () =>
          act('PUT', `/api/kpi/stations/${item.id}`, sbody({ is_active: !item.is_active }), item.is_active ? 'Станция в архиве' : 'Станция возвращена'));
      });
    });
  }

  // Вкладка видна только администратору и начальникам
  function updateTab() {
    const ok = canEdit();
    tab.hidden = !ok;
    if (!ok && pane.classList.contains('active')) document.querySelector('.tab[data-tab="browser"]').click();
  }
  const prev = window.onAuthChanged;
  window.onAuthChanged = () => { if (prev) prev(); updateTab(); };
  updateTab();

  tab.addEventListener('click', load);
})();
