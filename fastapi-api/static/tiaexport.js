// Выгрузка проекта TIA Portal в XML и Excel (только администратор): кнопка «🧩 Выгрузить проект TIA» в строке выбора ПЛК
// на вкладке «ПЛК» открывает окно выгрузки: «Источник» (все ПЛК системы; ПЛК без проекта в tia-projects.json серые и не выбираются),
// «Путь сохранения», «Выгрузить», индикатор службы; ниже состояние и последняя выгрузка выбранного источника с кнопкой «⬇ ZIP».
// При открытии предвыбран ПЛК, выбранный вверху вкладки (если у него есть проект); дальше выбор в окне от верхнего списка не зависит.
// Саму выгрузку делает служба tia-bridge.ps1 на ПК сервера.
// Чистые функции (список источников, тексты состояния) отдаются через module.exports для проверки в node (tests/test_tia_target.py).
(function (root) {
  'use strict';

  const DEFAULT_TARGET = 'C:\\tia-bridge\\out';
  const ACTIVE = ['queued', 'running'];
  const NO_PROJECTS = 'Ни для одного ПЛК не задан проект TIA (файл tia-projects.json на ПК сервера)';
  const norm = s => String(s ?? '').trim().toLowerCase();

  // Список «Источник»: каждый ПЛК системы; у кого есть проект (поле plc в tia-projects.json), тот выбирается,
  // остальные серые «<имя> — проект не задан». Проекты без поля plc (старые файлы) или с незнакомым ПЛК идут в конце по своему имени.
  function buildSources(plcs, projects) {
    const prj = Array.isArray(projects) ? projects : [];
    const used = new Set();
    const out = [];
    (Array.isArray(plcs) ? plcs : []).forEach(plc => {
      const mine = prj.filter(p => p.plc && norm(p.plc) === norm(plc.name));
      if (!mine.length) {
        out.push({ value: `plc:${plc.id}`, label: `${plc.name} — проект не задан`, plc: plc.name, project: null, disabled: true });
        return;
      }
      mine.forEach(p => {
        used.add(p.id);
        out.push({ value: p.id, label: mine.length > 1 ? `${plc.name} · ${p.name}` : plc.name, plc: plc.name, project: p, disabled: false });
      });
    });
    prj.filter(p => !used.has(p.id)).forEach(p => out.push({ value: p.id, label: p.name, plc: p.plc || '', project: p, disabled: false }));
    return out;
  }

  // Источник ПЛК по его имени (ПЛК, выбранный вверху вкладки): первый выбираемый с этим ПЛК, иначе ''
  function sourceForPlc(sources, plcName) {
    const n = norm(plcName);
    const s = n ? (sources || []).find(x => !x.disabled && norm(x.plc) === n) : null;
    return s ? s.value : '';
  }

  // Выбранный источник: заданный, если он выбирается, иначе первый доступный
  function pickSource(sources, wanted) {
    const ok = sources.filter(s => !s.disabled);
    if (ok.some(s => s.value === wanted)) return wanted;
    return ok.length ? ok[0].value : (sources[0] ? sources[0].value : '');
  }

  // Путь по умолчанию: последний использованный для этого проекта, иначе папка out службы
  function defaultTarget(project, fallback) {
    if (project && project.target) return project.target;
    if (project && project.last && project.last.target) return project.last.target;
    return fallback || DEFAULT_TARGET;
  }

  const isActive = p => !!(p && p.job && ACTIVE.includes(p.job.state));

  // Кнопка «Выгрузить» доступна, только если служба работает, у источника есть проект и выгрузка этого проекта не идёт
  function canExport(alive, src, busy) {
    return !!alive && !!src && !src.disabled && !!src.project && !busy && !isActive(src.project);
  }

  function stateInfo(job) {
    if (!job) return { cls: 'dim', text: 'не выгружался', title: 'Этот проект ещё ни разу не выгружался' };
    if (job.state === 'queued') return { cls: 'tia-run', text: 'в очереди', title: 'Заявка принята, служба начнёт выгрузку, когда освободится' };
    if (job.state === 'running') return { cls: 'tia-run', text: 'выгружается…', title: 'Идёт выгрузка: открытие проекта в TIA Portal без окна и экспорт блоков, обычно несколько минут' };
    if (job.state === 'error') return { cls: 'tia-err', text: `ошибка: ${job.error || 'неизвестная'}`, title: `Выгрузка не удалась: ${job.error || 'причина не записана'}` };
    return { cls: 'tia-ok', text: 'готово', title: 'Последняя выгрузка закончилась успешно, архив можно скачать кнопкой «⬇ ZIP»' };
  }

  function fmtMb(n) { return `${(Number(n || 0) / 1048576).toFixed(1).replace('.', ',')} МБ`; }

  // Какие файлы Excel есть в архиве (по строкам manifest.csv с kind=texts/prodiag, их считает служба)
  function excelText(last) {
    const parts = [];
    if (last && Number(last.texts) > 0) parts.push('тексты проекта');
    if (last && last.prodiag) parts.push('ProDiag');
    return parts.length ? parts.join(', ') : 'нет';
  }

  function lastText(last, fmtT) {
    if (!last) return '';
    const f = typeof fmtT === 'function' ? fmtT : (x => String(x ?? ''));
    return `Последняя выгрузка: ${f(last.finished_at)} · ${fmtMb(last.size)} · блоков ${last.exported ?? 0}, пропущено ${last.skipped ?? 0}, `
      + `не выгружено ${last.failed ?? 0} · файлов Excel: ${excelText(last)}`;
  }

  // Куда положен архив: копия в «Путь сохранения», для старых выгрузок без этого поля — папка out службы
  function savedPath(last, fallback) {
    if (!last) return '';
    if (last.saved_to) return String(last.saved_to);
    return `${fallback || DEFAULT_TARGET}\\${last.file || ''}`;
  }

  const nf = n => Number(n || 0).toLocaleString('ru-RU');

  // Колонка «Выгрузка (ZIP)» таблицы проектов: что с последней выгрузкой проекта из TIA
  function zipInfo(p, fmtT) {
    const f = typeof fmtT === 'function' ? fmtT : (x => String(x ?? ''));
    const job = p && p.job;
    if (job && job.state === 'queued') return { cls: 'tia-run', text: '⏳ в очереди', title: 'Заявка принята, служба начнёт выгрузку проекта, когда освободится' };
    if (job && job.state === 'running') return { cls: 'tia-run', text: '⏳ выгружается…', title: 'Идёт выгрузка проекта из TIA Portal, обычно несколько минут' };
    if (job && job.state === 'error') return { cls: 'tia-err', text: `✗ ошибка: ${job.error || 'неизвестная'}`, title: `Выгрузка не удалась: ${job.error || 'причина не записана'}` };
    if (p && p.last) return { cls: 'tia-ok', text: `✓ ${f(p.last.finished_at)}, ${fmtMb(p.last.size)}`, title: 'Последняя успешная выгрузка проекта: когда закончилась и сколько весит архив ZIP' };
    return { cls: 'dim', text: '— не выгружался', title: 'Этот проект ещё ни разу не выгружался из TIA Portal' };
  }

  // Колонка «Файл для ИИ»: готовность короткой записи логики ПЛК, которую сайт собирает из последнего ZIP
  function logicInfo(p, fmtT) {
    const f = typeof fmtT === 'function' ? fmtT : (x => String(x ?? ''));
    const lg = (p && p.logic) || { state: 'none' };
    if (lg.state === 'building') return { cls: 'tia-run', text: '⏳ создаётся…', title: 'Сайт собирает файл логики ПЛК из последнего ZIP, обычно несколько секунд; окно само обновится' };
    if (lg.state === 'readonly') return { cls: 'dim', text: '🔒 недоступно в тестовой копии (только чтение)', title: 'Тестовая копия сайта работает только на чтение: файл для ИИ собирается на рабочем сервере, здесь его создать нельзя' };
    if (lg.state === 'ready') return { cls: 'tia-ok', text: `✓ готов ${f(lg.finished_at)}, ${fmtMb(lg.size)}; ${nf(lg.blocks)} блоков, ${nf(lg.writes)} выходов`,
      title: 'Файл логики ПЛК готов: время сборки, размер, число разобранных блоков и выходов с цепочками условий' };
    if (lg.state === 'error') return { cls: 'tia-err', text: `✗ ошибка: ${lg.error || 'неизвестная'}`, title: `Файл для ИИ собрать не удалось: ${lg.error || 'причина не записана'}; прежний файл, если был, остался` };
    return { cls: 'dim', text: '— ждёт выгрузки', title: 'Файл для ИИ появится сам после первой успешной выгрузки проекта из TIA Portal' };
  }

  const isRo = p => !!(p && p.logic && (p.logic.state === 'readonly' || p.logic.readonly));
  const canRebuild = p => !!(p && p.last && !isRo(p) && !(p.logic && p.logic.state === 'building') && !isActive(p));
  const rebuildTitle = p => (isRo(p)
    ? 'Пересборка недоступна в тестовой копии сайта: каталог только для чтения, файл для ИИ собирается на рабочем сервере'
    : 'Собрать файл для ИИ заново из последнего ZIP без новой выгрузки из TIA Portal, занимает несколько секунд');
  const canDownloadLogic = p => !!(p && p.logic && p.logic.state === 'ready');

  const core = { rebuildTitle, zipInfo, logicInfo, canRebuild, canDownloadLogic, DEFAULT_TARGET, NO_PROJECTS, buildSources, sourceForPlc, pickSource, defaultTarget, canExport, stateInfo, excelText, lastText, savedPath, fmtMb };
  root.tiaCore = core;
  if (typeof module !== 'undefined' && module.exports) module.exports = core;
  if (typeof document === 'undefined') return;

  const btn = document.querySelector('#tia-open');
  const topPlc = document.querySelector('#plc');
  const pane = document.querySelector('#browser');
  if (!btn || !topPlc || !pane) return;

  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const toast = t => (typeof root.showToast === 'function' ? root.showToast(t) : alert(t));
  const isAdmin = () => {
    if (typeof root.isAdminUser === 'function') return root.isAdminUser();
    const st = root.authState && root.authState();
    return !!(st && st.user && st.user.role === 'admin');
  };
  const fmtT = iso => (iso && root.plantClock ? root.plantClock.fmt(iso, true) : '—');
  const FAST_MS = 4000, SLOW_MS = 15000;
  const BTN_TITLE = 'Выгрузить проект TIA Portal в XML и Excel: откроется окно с выбором ПЛК-источника, путём сохранения, состоянием службы и ZIP';
  const NO_ANSWER = 'Нет ответа сервера о проектах TIA: проверьте связь с сервером, список перечитается сам';

  let data = null, plcs = [], sources = [], current = '', timer = null, busy = false, modal = null, optsKey = '', shownPrj = '';
  const typed = new Map();   // проект -> путь, введённый вручную (не затирается опросом)

  async function api(url, opts) {
    const r = await fetch(url, Object.assign({ credentials: 'same-origin', signal: AbortSignal.timeout(6000) }, opts));
    if (!r.ok) {
      let msg = `Ошибка ${r.status}`;
      try { const j = await r.json(); if (typeof j.detail === 'string') msg = j.detail; } catch { /* оставим код */ }
      throw new Error(msg);
    }
    return r.json();
  }

  const topName = () => (topPlc.selectedOptions[0] ? topPlc.selectedOptions[0].textContent : '');
  const srcOf = () => sources.find(s => s.value === current) || null;
  const hasProjects = () => !!(data && Array.isArray(data.projects) && data.projects.length);

  function drawButton() {
    btn.hidden = !isAdmin();
    btn.disabled = !hasProjects();
    btn.title = hasProjects() ? BTN_TITLE : (data ? NO_PROJECTS : NO_ANSWER);
  }

  function onKey(e) { if (e.key === 'Escape') closeModal(); }
  function closeModal() {
    if (!modal) return;
    modal.remove();
    modal = null;
    optsKey = ''; shownPrj = '';
    document.removeEventListener('keydown', onKey);
    schedule();
  }

  function openModal() {
    if (modal || !hasProjects()) return;
    sources = buildSources(plcs, data.projects);
    current = pickSource(sources, sourceForPlc(sources, topName()) || current);   // по умолчанию ПЛК, выбранный вверху
    modal = document.createElement('div');
    modal.className = 'kpi-modal';
    modal.innerHTML = `
      <div class="kpi-modal-box auth-modal tia-box" role="dialog" aria-label="Выгрузка проекта TIA">
        <h3>Выгрузка проекта TIA · <span data-k="name"></span></h3>
        <div class="tia-bar">
          <label class="tia-f" title="Источник выгрузки: ПЛК системы, для которого в файле tia-projects.json на ПК сервера задан проект TIA. Серые ПЛК без проекта выбрать нельзя">Источник
            <select id="tia-src" title="Выберите ПЛК, проект TIA которого нужно выгрузить; серые строки без проекта: добавьте проект с полем plc в tia-projects.json"></select></label>
          <label class="tia-f tia-path" title="Папка, куда служба положит копию ZIP: полный путь с буквой диска (C:\\папка) или сетевой путь (\\\\сервер\\папка)">Путь сохранения
            <input type="text" id="tia-target" spellcheck="false" title="Полный путь к папке для копии архива (C:\\папка или \\\\сервер\\папка); системные папки и корень диска запрещены, папка создаётся сама"></label>
          <button type="button" id="tia-go" disabled title="Выгрузить проект выбранного источника в XML и Excel: служба откроет копию проекта в TIA Portal без окна, проект не меняется; несколько минут">Выгрузить</button>
          <span class="tia-svc" data-k="svc"></span>
        </div>
        <div class="tia-line" data-k="state"></div>
        <div class="tia-line" data-k="last"></div>
        <table class="tia-tbl" data-k="tbl" title="Все проекты TIA: состояние выгрузки ZIP и файла для ИИ, который сайт собирает из последнего архива">
          <thead><tr><th title="Название проекта TIA из файла tia-projects.json на ПК сервера">Проект</th><th title="Имя ПЛК в системе PMM, к которому относится проект TIA">ПЛК</th>
            <th title="Состояние последней выгрузки проекта из TIA Portal в архив ZIP и кнопка скачивания">Выгрузка (ZIP)</th>
            <th title="Короткая запись логики ПЛК для ИИ: состояние, размер, число блоков и выходов; кнопки скачивания и пересборки">Файл для ИИ</th></tr></thead>
          <tbody data-k="rows"></tbody>
        </table>
        <div class="tia-note" title="Файл для ИИ собирается автоматически после каждой успешной выгрузки, лежит на сервере в папке logic и скачивается кнопкой в таблице">Файл для ИИ: короткая запись логики ПЛК (какие условия включают каждый выход); по нему локальная модель объяснит, почему встала станция. Лежит на сервере: C:\\tia-bridge\\logic\\</div>
        <div class="kpi-modal-actions">
          <button type="button" data-act="cancel" class="tia-close" title="Закрыть окно; начатая выгрузка продолжится на ПК сервера, состояние видно при следующем открытии окна">Закрыть</button>
        </div>
      </div>`;
    modal.addEventListener('mousedown', e => { if (e.target === modal) closeModal(); });
    modal.querySelector('[data-act="cancel"]').addEventListener('click', closeModal);
    const sel = modal.querySelector('#tia-src');
    const inp = modal.querySelector('#tia-target');
    const go = modal.querySelector('#tia-go');
    sel.addEventListener('change', () => { current = sel.value; drawModal(); });
    inp.addEventListener('input', () => { const p = srcOf() && srcOf().project; if (p) typed.set(p.id, inp.value); });
    inp.addEventListener('keydown', e => { if (e.key === 'Enter' && !go.disabled) go.click(); });
    go.addEventListener('click', exportNow);
    modal.querySelector('[data-k="tbl"]').addEventListener('click', tableClick);
    document.addEventListener('keydown', onKey);
    document.body.appendChild(modal);
    drawModal();
    sel.focus();
    load();
  }

  function drawModal() {
    if (!modal) return;
    if (!data) { closeModal(); toast(NO_ANSWER); return; }
    const q = s => modal.querySelector(s);
    sources = buildSources(plcs, data.projects);
    current = pickSource(sources, current);
    const sel = q('#tia-src');
    const key = JSON.stringify(sources.map(s => [s.value, s.label, s.disabled]));
    if (key !== optsKey) {
      optsKey = key;
      sel.innerHTML = sources.length
        ? sources.map(s => `<option value="${esc(s.value)}"${s.disabled ? ' disabled class="tia-none"' : ''} title="${esc(s.project ? `Проект TIA: ${s.project.name}` : 'Для этого ПЛК нет проекта в tia-projects.json на ПК сервера')}">${esc(s.label)}</option>`).join('')
        : '<option value="" disabled>Нет ПЛК и проектов</option>';
    }
    sel.value = current;
    const src = srcOf();
    const p = src && src.project;
    q('[data-k="name"]').textContent = p ? p.name : '';
    const inp = q('#tia-target');
    const pid = p ? p.id : '';
    if (pid !== shownPrj || document.activeElement !== inp) inp.value = p ? (typed.get(p.id) ?? defaultTarget(p, data.default_target)) : '';
    shownPrj = pid;
    inp.disabled = !p;
    const svc = q('[data-k="svc"]');
    svc.className = `tia-svc ${data.alive ? 'on' : 'off'}`;
    svc.textContent = data.alive ? '● служба работает' : '● служба не запущена';
    svc.title = data.alive ? 'Служба выгрузки tia-bridge работает на ПК сервера и принимает заявки на выгрузку'
      : 'Служба выгрузки не запущена на ПК сервера: задача планировщика «PMM TIA bridge» стартует при входе в Windows под учёткой из группы «Siemens TIA Openness»';
    q('#tia-go').disabled = !canExport(data.alive, src, busy);
    const st = p ? stateInfo(p.job) : { cls: 'dim', text: 'Для этого ПЛК не задан проект: добавьте его с полем plc в tia-projects.json на ПК сервера', title: '' };
    q('[data-k="state"]').innerHTML = `<span class="${st.cls}"${st.title ? ` title="${esc(st.title)}"` : ''}>${esc(st.text)}</span>`;
    drawTable();
    const l = p && p.last;
    const box = q('[data-k="last"]');
    const lkey = l ? JSON.stringify([pid, l.file, l.finished_at, l.saved_to, l.copy_error]) : `-${pid}`;
    if (box.dataset.key === lkey) return;   // без изменений не перерисовывать (кнопка ZIP не мигает)
    box.dataset.key = lkey;
    box.innerHTML = l
      ? `<span class="tia-last" title="Когда закончилась последняя успешная выгрузка, размер архива, число блоков и какие файлы Excel есть в архиве">${esc(lastText(l, fmtT))}</span>`
        + `<button type="button" class="dt-sec" id="tia-dl" title="Скачать ZIP последней выгрузки: XML блоков, типов и таблиц тегов, manifest.csv, тексты проекта ProjectTexts_*.xlsx и ProDiag_Supervisions.xlsx">⬇ ZIP</button>`
        + `<span class="tia-saved" title="Куда служба положила копию архива (папка «Путь сохранения» на ПК сервера)">${esc(savedPath(l, data.default_target))}</span>`
        + (l.copy_error ? `<span class="tia-err" title="${esc(l.copy_error)}">${esc(l.copy_error)}</span>` : '')
      : '';
    box.hidden = !l;
    box.querySelector('#tia-dl')?.addEventListener('click', async e => {
      const b = e.currentTarget;
      b.disabled = true;
      try { await root.xlsxDownload(`/api/tia/download/${encodeURIComponent(l.file)}`); } catch (ex) { toast(ex.message); }
      b.disabled = false;
    });
  }

  // Таблица всех проектов: перерисовывается только при изменении данных, чтобы кнопки не мигали при опросе
  function drawTable() {
    const body = modal.querySelector('[data-k="rows"]');
    const rows = (data.projects || []).map(p => ({ p, z: zipInfo(p, fmtT), g: logicInfo(p, fmtT) }));
    const key = JSON.stringify(rows.map(r => [r.p.id, r.p.plc, r.z.text, r.g.text, canRebuild(r.p), canDownloadLogic(r.p), r.p.last && r.p.last.file]));
    if (body.dataset.key === key) return;
    body.dataset.key = key;
    body.innerHTML = rows.map(({ p, z, g }) => `<tr data-id="${esc(p.id)}">`
      + `<td title="Проект TIA: ${esc(p.name)}">${esc(p.name)}</td><td title="ПЛК системы PMM для этого проекта">${esc(p.plc || '—')}</td>`
      + `<td><span class="${z.cls}" title="${esc(z.title)}">${esc(z.text)}</span>`
      + (p.last && !isActive(p) ? ` <button type="button" class="dt-sec" data-act="zip" title="Скачать архив ZIP последней выгрузки проекта: XML блоков, таблицы тегов и тексты проекта">Скачать ZIP</button>` : '') + `</td>`
      + `<td><span class="${g.cls}" title="${esc(g.title)}">${esc(g.text)}</span>`
      + (canDownloadLogic(p) ? ` <button type="button" class="dt-sec" data-act="json" title="Скачать файл JSON с логикой ПЛК для ИИ: для каждого выхода цепочка условий, которые его включают">Скачать JSON</button>` : '')
      + ((p.last || isRo(p)) ? ` <button type="button" class="dt-sec" data-act="rebuild"${canRebuild(p) ? '' : ' disabled'} title="${esc(rebuildTitle(p))}">Пересобрать</button>` : '')
      + `</td></tr>`).join('') || '<tr><td colspan="4" class="dim">Нет проектов</td></tr>';
  }

  async function tableClick(e) {
    const b = e.target.closest('button[data-act]');
    const tr = b && b.closest('tr[data-id]');
    if (!b || !tr || b.disabled) return;
    const id = tr.dataset.id;
    const p = (data.projects || []).find(x => x.id === id);
    b.disabled = true;
    try {
      if (b.dataset.act === 'rebuild') {
        await api(`/api/tia/logic/${encodeURIComponent(id)}/rebuild`, { method: 'POST' });
        await load();
        return;
      }
      if (b.dataset.act === 'json') await root.xlsxDownload(`/api/tia/logic/${encodeURIComponent(id)}`);
      if (b.dataset.act === 'zip' && p && p.last) await root.xlsxDownload(`/api/tia/download/${encodeURIComponent(p.last.file)}`);
    } catch (ex) { toast(ex.message); }
    b.disabled = false;
  }

  async function exportNow() {
    const src = srcOf();
    if (!src || !src.project || !modal) return;
    busy = true; drawModal();
    try {
      await api('/api/tia/export', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project_ids: [src.project.id], target_dir: modal.querySelector('#tia-target').value.trim() || null }) });
      typed.delete(src.project.id);
    } catch (ex) { toast(ex.message); }
    busy = false;
    await load();
  }

  async function load() {
    if (!isAdmin()) { data = null; closeModal(); drawButton(); clearTimeout(timer); return; }
    const [prj, pl] = await Promise.allSettled([api('/api/tia/projects'), api('/api/plcs')]);
    data = prj.status === 'fulfilled' ? prj.value : null;
    if (pl.status === 'fulfilled' && Array.isArray(pl.value)) {
      const R = root.reorder;
      plcs = R ? R.applyOrder(pl.value, R.get('order-plc'), i => String(i.id)) : pl.value;   // личный порядок списка ПЛК, как вверху
    }
    drawButton();
    drawModal();
    schedule();
  }

  // каждые 4 с, пока открыто окно или идёт выгрузка; раз в 15 с, пока открыта вкладка «ПЛК»; иначе не опрашиваем
  const anyActive = () => !!(data && Array.isArray(data.projects) && data.projects.some(p => isActive(p) || (p.logic && p.logic.state === 'building')));
  function schedule() {
    clearTimeout(timer);
    const fast = !!modal || anyActive();
    timer = setTimeout(() => { if (modal || pane.classList.contains('active')) load(); else schedule(); }, fast ? FAST_MS : SLOW_MS);
  }

  btn.addEventListener('click', openModal);
  document.querySelector('.tab[data-tab="browser"]')?.addEventListener('click', () => setTimeout(load, 50));
  const prevAuth = root.onAuthChanged;
  root.onAuthChanged = () => { if (prevAuth) prevAuth(); load(); };
  load();
})(typeof window !== 'undefined' ? window : globalThis);
