// Панель «Проекты TIA» во вкладке «Переменные ПЛК» (только администратор): выгрузка проектов TIA Portal в XML.
// Общая таблица: строка на проект, галочки, «выбрать все» и одна кнопка «Выгрузить выбранные». Саму выгрузку делает служба tia-bridge.ps1 на ПК сервера.
(() => {
  'use strict';

  const host = document.querySelector('#browser');
  const anchor = host && host.querySelector('.area-plc');
  if (!host || !anchor) return;

  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const toast = t => (typeof showToast === 'function' ? showToast(t) : alert(t));
  const isAdmin = () => { const st = window.authState && window.authState(); return !!(st && st.user && st.user.role === 'admin'); };
  const fmtT = iso => (iso && window.plantClock ? window.plantClock.fmt(iso, true) : '');
  const fmtMb = n => `${(Number(n || 0) / 1048576).toFixed(1).replace('.', ',')} МБ`;
  const ACTIVE = ['queued', 'running'];
  const FAST_MS = 3000, SLOW_MS = 15000;

  const sec = document.createElement('section');
  sec.className = 'area area-tia';
  sec.id = 'tia-export';
  sec.hidden = true;
  anchor.after(sec);

  let data = null, timer = null, busy = false;
  const checked = new Set();

  async function api(url, opts) {
    const r = await fetch(url, Object.assign({ signal: AbortSignal.timeout(6000) }, opts));
    if (!r.ok) {
      let msg = `Ошибка ${r.status}`;
      try { const j = await r.json(); if (j.detail) msg = j.detail; } catch { /* оставим код */ }
      throw new Error(msg);
    }
    return r.json();
  }

  // Что написать в столбце «Состояние»
  function stateOf(p) {
    const j = p.job;
    if (!j) return '<span class="dim">не выгружался</span>';
    if (j.state === 'queued') return `<span class="tia-run" title="Заявка принята, служба начнёт выгрузку, когда освободится">в очереди</span>`;
    if (j.state === 'running') return `<span class="tia-run" title="Идёт выгрузка: открытие проекта в TIA Portal без окна и экспорт блоков, обычно несколько минут">выгружается…</span>`;
    if (j.state === 'error') return `<span class="tia-err" title="${esc(j.error || '')}">ошибка: ${esc((j.error || '').slice(0, 90))}</span>`;
    return '<span class="tia-ok">готово</span>';
  }

  function lastOf(p) {
    const l = p.last;
    if (!l) return '<span class="dim">—</span>';
    const extra = `блоков ${l.exported ?? 0}${l.skipped ? `, пропущено ${l.skipped}` : ''}${l.failed ? `, не выгружено ${l.failed}` : ''}`;
    return `${esc(fmtT(l.finished_at))} · ${fmtMb(l.size)} · ${esc(extra)}`;
  }

  function draw() {
    sec.hidden = !isAdmin() || !data;
    if (sec.hidden) return;
    const rows = data.projects;
    const active = rows.some(p => p.job && ACTIVE.includes(p.job.state));
    const all = rows.length && rows.every(p => checked.has(p.id));
    sec.innerHTML = `
      <div class="panel-header">🧩 Проекты TIA <span class="area-hint">выгрузка в XML</span>
        <span class="tia-svc ${data.alive ? 'on' : 'off'}" title="${data.alive ? 'Служба выгрузки tia-bridge работает на ПК сервера' : 'Служба выгрузки не запущена на ПК сервера: задача планировщика «PMM TIA bridge» стартует при входе в Windows под учёткой из группы «Siemens TIA Openness»'}">${data.alive ? '● служба работает' : '● служба не запущена'}</span>
      </div>
      <div class="tia-body">
        ${rows.length ? `<table class="users-table tia-table"><thead><tr>
          <th class="tia-chk"><input type="checkbox" id="tia-all" ${all ? 'checked' : ''} title="Выбрать все проекты или снять выбор"></th>
          <th title="Проект из списка разрешённых (файл tia-projects.json на ПК сервера)">Проект</th>
          <th title="Состояние последней выгрузки этого проекта">Состояние</th>
          <th title="Когда закончилась последняя успешная выгрузка, размер архива и число блоков">Последняя выгрузка</th>
          <th title="Скачать ZIP с XML последней успешной выгрузки"></th></tr></thead><tbody>${rows.map(p => `<tr>
          <td class="tia-chk"><input type="checkbox" data-id="${esc(p.id)}" ${checked.has(p.id) ? 'checked' : ''} ${p.job && ACTIVE.includes(p.job.state) ? 'disabled' : ''} title="Включить проект ${esc(p.name)} в выгрузку"></td>
          <td>${esc(p.name)}</td><td>${stateOf(p)}</td><td>${lastOf(p)}</td>
          <td>${p.last ? `<button type="button" class="dt-sec" data-dl="${esc(p.last.file)}" title="Скачать ZIP с XML проекта ${esc(p.name)}: блоки, типы данных, таблицы тегов и manifest.csv">⬇ ZIP</button>` : ''}</td></tr>`).join('')}</tbody></table>
        <div class="tia-actions"><button type="button" id="tia-go" title="Выгрузить в XML все отмеченные проекты: служба откроет копию проекта в TIA Portal без окна, проект не меняется; выгрузка идёт несколько минут" ${busy || !checked.size || !data.alive ? 'disabled' : ''}>Выгрузить выбранные</button>
          <span class="dim">${active ? 'Идёт выгрузка, страницу можно не закрывать' : ''}</span></div>`
        : '<div class="empty">Список проектов пуст: добавьте проекты в tia-projects.json на ПК сервера</div>'}
      </div>`;
    sec.querySelector('#tia-all')?.addEventListener('change', e => { rows.forEach(p => { if (!(p.job && ACTIVE.includes(p.job.state))) (e.target.checked ? checked.add(p.id) : checked.delete(p.id)); }); draw(); });
    sec.querySelectorAll('input[data-id]').forEach(c => c.addEventListener('change', () => { (c.checked ? checked.add(c.dataset.id) : checked.delete(c.dataset.id)); draw(); }));
    sec.querySelectorAll('[data-dl]').forEach(b => b.addEventListener('click', async () => {
      b.disabled = true;
      try { await window.xlsxDownload(`/api/tia/download/${encodeURIComponent(b.dataset.dl)}`); } catch (ex) { toast(ex.message); }
      b.disabled = false;
    }));
    sec.querySelector('#tia-go')?.addEventListener('click', async () => {
      busy = true; draw();
      try {
        const r = await api('/api/tia/export', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ project_ids: [...checked] }) });
        toast(`Выгрузка поставлена в очередь: ${r.started.length}`);
        checked.clear();
      } catch (ex) { toast(ex.message); }
      busy = false;
      await load();
    });
    schedule(active);
  }

  async function load() {
    if (!isAdmin()) { data = null; sec.hidden = true; return; }
    try { data = await api('/api/tia/projects'); } catch { data = null; }
    draw();
  }

  // быстро, пока идёт выгрузка; редко в остальное время; пока вкладка «Переменные ПЛК» не открыта, ничего не опрашиваем
  function schedule(active) {
    clearTimeout(timer);
    timer = setTimeout(() => { if (host.classList.contains('active')) load(); else schedule(false); }, active ? FAST_MS : SLOW_MS);
  }

  document.querySelector('.tab[data-tab="browser"]')?.addEventListener('click', () => setTimeout(load, 50));
  const prevAuth = window.onAuthChanged;
  window.onAuthChanged = () => { if (prevAuth) prevAuth(); load(); };
  load();
})();
