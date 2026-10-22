// Выгрузка проекта TIA Portal в XML и Excel (только администратор): кнопка «🧩 Выгрузить проект TIA» в строке выбора ПЛК
// на вкладке «ПЛК» открывает окно выгрузки: «ПЛК» (все ПЛК системы), «Проект (папка)» (файлы .ap17, которые служба нашла в разрешённых папках),
// «Путь сохранения», «Выгрузить», индикатор службы; ниже состояние и последняя выгрузка выбранного ПЛК с кнопкой «⬇ ZIP».
// Пара «ПЛК -> проект» записывается службой в tia-projects.json; на сервер уходит только имя ПЛК и ключ найденного файла, путь не передаётся.
// При открытии предвыбран ПЛК, выбранный вверху вкладки; дальше выбор в окне от верхнего списка не зависит.
// Саму выгрузку делает служба tia-bridge.ps1 на ПК сервера.
// Чистые функции (список источников, тексты состояния) отдаются через module.exports для проверки в node (tests/test_tia_target.py).
(function (root) {
  'use strict';

  const DEFAULT_TARGET = 'C:\\tia-bridge\\out';
  const LOGIC_DIR = 'C:\\tia-bridge\\logic';   // папка файлов для ИИ на ПК сервера (в контейнере это /tia-bridge/logic)
  const PROJECTS_FILE = 'C:\\tia-bridge\\tia-projects.json';
  const PAIR = '@pair';   // выбор «текущий проект ПЛК», путь которого лежит вне найденных папок
  // Точный путь файла для ИИ на сервере: <папка logic>\\<id проекта>.json
  const logicPath = p => (p && p.id ? `${LOGIC_DIR}\\${p.id}.json` : '');
  const ACTIVE = ['queued', 'running'];
  const norm = s => String(s ?? '').trim().toLowerCase();

  // Список ПЛК: все ПЛК системы; у кого уже есть пара «ПЛК -> проект» в tia-projects.json, к названию добавляется «— проект: <папка>»
  function plcOptions(plcs, projects) {
    const prj = Array.isArray(projects) ? projects : [];
    return (Array.isArray(plcs) ? plcs : []).map(plc => {
      const pair = prj.find(p => p.plc && norm(p.plc) === norm(plc.name)) || null;
      return { value: plc.name, label: pair ? `${plc.name} — проект: ${pair.folder || pair.name}` : plc.name, project: pair };
    });
  }

  // Список «Проект (папка)»: найденные службой .ap17 «GWM\UB\UB.ap17 — изменён 20.10 12:03»; если проект ПЛК лежит вне найденных папок, он первый
  function foundOptions(found, project, fmtT) {
    const f = typeof fmtT === 'function' ? fmtT : (x => String(x ?? ''));
    const out = (found && Array.isArray(found.items) ? found.items : []).map(i => ({ value: i.key, label: `${i.rel} — изменён ${f(i.modified)}` }));
    if (project && !project.found_key) out.unshift({ value: PAIR, label: `${project.folder || project.name} — текущий проект ПЛК (вне найденных папок)` });
    return out;
  }

  // Выбранный проект: прежний выбор, если он остался в списке, иначе проект пары ПЛК, иначе ничего
  function pickFound(found, project, wanted) {
    const keys = (found && Array.isArray(found.items) ? found.items : []).map(i => i.key);
    if (wanted && (keys.includes(wanted) || (wanted === PAIR && project && !project.found_key))) return wanted;
    if (project && project.found_key && keys.includes(project.found_key)) return project.found_key;
    return project && !project.found_key ? PAIR : '';
  }

  // Строка перед запуском: какая пара «ПЛК ↔ проект» будет записана
  function confirmLine(plc, found, project, choice) {
    if (!plc || !choice) return '';
    const it = choice === PAIR ? null : (found && found.items || []).find(i => i.key === choice);
    const rel = it ? it.rel : (project && (project.folder || project.name)) || '';
    return rel ? `ПЛК ${plc} ↔ проект ${rel}` : '';
  }

  // Подпись под списком, если проектов нет или список устарел
  function foundHint(found) {
    if (found && Array.isArray(found.items) && found.items.length) {
      return found.stale ? 'Список проектов устарел: служба выгрузки давно его не обновляла, проверьте, что она запущена на ПК сервера.' : '';
    }
    return 'Служба выгрузки не нашла проекты в разрешённых папках. Папки поиска (поле roots) задаются в ' + PROJECTS_FILE
      + ' (правит администратор сервера); по умолчанию C:\\TIA_for_claude_to_XML и C:\\projects.';
  }

  // Путь по умолчанию: последний использованный для этого проекта, иначе папка out службы
  function defaultTarget(project, fallback) {
    if (project && project.target) return project.target;
    if (project && project.last && project.last.target) return project.last.target;
    return fallback || DEFAULT_TARGET;
  }

  const isActive = p => !!(p && p.job && ACTIVE.includes(p.job.state));

  // Кнопка «Выгрузить» доступна, только если служба работает, выбраны ПЛК и проект и выгрузка этого проекта не идёт
  function canExport(alive, plc, choice, busy, project) {
    return !!alive && !!plc && !!choice && !busy && !isActive(project);
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

  const core = { logicPath, LOGIC_DIR, rebuildTitle, zipInfo, logicInfo, canRebuild, canDownloadLogic, DEFAULT_TARGET, PAIR, plcOptions, foundOptions, pickFound, confirmLine, foundHint, defaultTarget, canExport, stateInfo, excelText, lastText, savedPath, fmtMb };
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

  let data = null, plcs = [], plcName = '', choice = '', timer = null, busy = false, modal = null, optsKey = '', shownPrj = '';
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
  const projOf = () => ((data && data.projects) || []).find(p => norm(p.plc) === norm(plcName)) || null;
  const hasData = () => !!data;

  function drawButton() {
    btn.hidden = !isAdmin();
    btn.disabled = !hasData();
    btn.title = hasData() ? BTN_TITLE : NO_ANSWER;
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
    if (modal || !hasData()) return;
    plcName = plcOptions(plcs, data.projects).some(o => o.value === topName()) ? topName() : plcName;   // по умолчанию ПЛК, выбранный вверху
    choice = '';
    modal = document.createElement('div');
    modal.className = 'kpi-modal';
    modal.innerHTML = `
      <div class="kpi-modal-box auth-modal tia-box" role="dialog" aria-label="Выгрузка проекта TIA">
        <h3>Выгрузка проекта TIA · <span data-k="name"></span></h3>
        <p class="tia-intro" title="Что делает окно: служба выгружает проект TIA в XML и Excel, сайт собирает из XML файл JSON для ИИ и хранит его на сервере">Что здесь происходит: 1) служба выгрузки открывает проект TIA Portal и сохраняет его блоки в XML и Excel (архив ZIP, папка C:\\tia-bridge\\out\\); 2) после выгрузки сайт сам собирает из XML файл для ИИ (JSON): короткую запись логики ПЛК, какие условия включают каждый выход. Файл лежит на сервере: C:\\tia-bridge\\logic\\&lt;проект&gt;.json, его можно скачать кнопкой «Скачать JSON».</p>
        <div class="tia-bar">
          <label class="tia-f" title="ПЛК системы, для которого выгружается проект TIA: рядом с названием показано, какая папка проекта уже привязана к этому ПЛК">ПЛК
            <select id="tia-src" title="Выберите ПЛК: у кого уже есть проект, у того в названии указана папка проекта; новому ПЛК проект выбирается справа"></select></label>
          <label class="tia-f tia-prj" title="Проект TIA из папок, где служба выгрузки нашла файлы .ap17; выбранный проект привяжется к выбранному ПЛК">Проект (папка)
            <select id="tia-prj" title="Файл проекта .ap17 из разрешённых папок поиска с датой изменения; при выборе ПЛК с проектом список стоит на его проекте"></select></label>
          <label class="tia-f tia-path" title="Папка, куда служба положит копию ZIP: полный путь с буквой диска (C:\\папка) или сетевой путь (\\\\сервер\\папка)">Путь сохранения
            <input type="text" id="tia-target" spellcheck="false" title="Полный путь к папке для копии архива (C:\\папка или \\\\сервер\\папка); системные папки и корень диска запрещены, папка создаётся сама"></label>
          <button type="button" id="tia-go" disabled title="Выгрузить выбранный проект для выбранного ПЛК в XML и Excel: служба откроет копию проекта в TIA Portal без окна, проект не меняется; несколько минут">Выгрузить</button>
          <span class="tia-svc" data-k="svc"></span>
        </div>
        <div class="tia-line tia-confirm" data-k="confirm" hidden title="Какую пару ПЛК и проект запомнит служба при выгрузке: проверьте её перед нажатием кнопки «Выгрузить»"></div>
        <div class="tia-note tia-nohint" data-k="found" hidden title="Служба выгрузки ищет файлы .ap17 раз в минуту только в разрешённых папках; их список задаёт администратор сервера в tia-projects.json"></div>
        <div class="tia-line" data-k="state"></div>
        <div class="tia-line" data-k="last"></div>
        <table class="tia-tbl" data-k="tbl" title="Все проекты TIA: состояние выгрузки ZIP и файла для ИИ, который сайт собирает из последнего архива">
          <thead><tr><th title="Название проекта TIA из файла tia-projects.json на ПК сервера">Проект</th><th title="Имя ПЛК в системе PMM, к которому относится проект TIA">ПЛК</th>
            <th title="Состояние последней выгрузки проекта из TIA Portal в архив ZIP и кнопка скачивания">Выгрузка (ZIP)</th>
            <th title="Короткая запись логики ПЛК для ИИ: состояние, размер, число блоков и выходов; кнопки скачивания и пересборки">Файл для ИИ</th></tr></thead>
          <tbody data-k="rows"></tbody>
        </table>
        <div class="tia-note" title="Файл для ИИ собирается автоматически после каждой успешной выгрузки, лежит на сервере в папке logic и скачивается кнопкой в таблице">Файл для ИИ нужен затем, чтобы по нему локальная модель объяснила, почему встала станция.</div>
        <div class="kpi-modal-actions">
          <button type="button" data-act="cancel" class="tia-close" title="Закрыть окно; начатая выгрузка продолжится на ПК сервера, состояние видно при следующем открытии окна">Закрыть</button>
        </div>
      </div>`;
    modal.addEventListener('mousedown', e => { if (e.target === modal) closeModal(); });
    modal.querySelector('[data-act="cancel"]').addEventListener('click', closeModal);
    const sel = modal.querySelector('#tia-src');
    const inp = modal.querySelector('#tia-target');
    const go = modal.querySelector('#tia-go');
    sel.addEventListener('change', () => { plcName = sel.value; choice = ''; shownPrj = ''; drawModal(); });
    modal.querySelector('#tia-prj').addEventListener('change', e => { choice = e.target.value; drawModal(); });
    inp.addEventListener('input', () => { if (plcName) typed.set(plcName, inp.value); });
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
    const opts = plcOptions(plcs, data.projects);
    if (!opts.some(o => o.value === plcName)) plcName = opts.length ? opts[0].value : '';
    const p = projOf();
    const found = data.found || { items: [], stale: true };
    choice = pickFound(found, p, choice);
    const sel = q('#tia-src'), psel = q('#tia-prj');
    const fopts = foundOptions(found, p, fmtT);
    const key = JSON.stringify([opts.map(o => [o.value, o.label]), fopts.map(o => [o.value, o.label])]);
    if (key !== optsKey) {
      optsKey = key;
      sel.innerHTML = opts.length ? opts.map(o => `<option value="${esc(o.value)}" title="${esc(o.project ? `ПЛК ${o.value}: привязан проект ${o.project.name}, выбрав другой проект справа, вы замените привязку` : `ПЛК ${o.value}: проект ещё не привязан, выберите его в списке справа`)}">${esc(o.label)}</option>`).join('')
        : '<option value="" disabled>Нет ПЛК в системе</option>';
      psel.innerHTML = '<option value="">Выберите проект…</option>' + fopts.map(o => `<option value="${esc(o.value)}" title="${esc(`Проект ${o.label}: файл проекта .ap17 в разрешённых папках поиска службы выгрузки`)}">${esc(o.label)}</option>`).join('');
    }
    sel.value = plcName;
    psel.value = choice;
    q('[data-k="name"]').textContent = p ? p.name : '';
    const inp = q('#tia-target');
    const pid = plcName;
    if (pid !== shownPrj || document.activeElement !== inp) inp.value = typed.get(plcName) ?? defaultTarget(p, data.default_target);
    shownPrj = pid;
    const cl = confirmLine(plcName, found, p, choice);
    const cbox = q('[data-k="confirm"]');
    cbox.textContent = cl;
    cbox.hidden = !cl;
    const fh = foundHint(found);
    const fbox = q('[data-k="found"]');
    fbox.textContent = fh;
    fbox.hidden = !fh;
    const svc = q('[data-k="svc"]');
    svc.className = `tia-svc ${data.alive ? 'on' : 'off'}`;
    svc.textContent = data.alive ? '● служба работает' : '● служба не запущена';
    svc.title = data.alive ? 'Служба выгрузки tia-bridge работает на ПК сервера и принимает заявки на выгрузку'
      : 'Служба выгрузки не запущена на ПК сервера: задача планировщика «PMM TIA bridge» стартует при входе в Windows под учёткой из группы «Siemens TIA Openness»';
    q('#tia-go').disabled = !canExport(data.alive, plcName, choice, busy, p);
    const st = p ? stateInfo(p.job) : { cls: 'dim', text: 'Для этого ПЛК проект ещё не привязан: выберите проект из списка найденных', title: '' };
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
    const key = JSON.stringify(rows.map(r => [r.p.id, r.p.plc, r.z.text, r.g.text, canRebuild(r.p), canDownloadLogic(r.p), r.p.last && r.p.last.file, r.p.id]));
    if (body.dataset.key === key) return;
    body.dataset.key = key;
    body.innerHTML = rows.map(({ p, z, g }) => `<tr data-id="${esc(p.id)}">`
      + `<td title="Проект TIA: ${esc(p.name)}">${esc(p.name)}</td><td class="tia-plc" title="ПЛК системы PMM для этого проекта">${esc(p.plc || '—')}</td>`
      + `<td><span class="${z.cls}" title="${esc(`${p.plc ? `ПЛК ${p.plc}: ` : ''}${z.title}`)}">${esc(z.text)}</span>`
      + (p.last && !isActive(p) ? ` <button type="button" class="dt-sec" data-act="zip" title="Скачать архив ZIP последней выгрузки проекта: XML блоков, таблицы тегов и тексты проекта">Скачать ZIP</button>` : '') + `</td>`
      + `<td><span class="${g.cls}" title="${esc(`${p.plc ? `ПЛК ${p.plc}: ` : ''}${g.title}`)}">${esc(g.text)}</span>`
      + (canDownloadLogic(p) ? `<div class="tia-fpath" title="Точный путь файла JSON для ИИ на сервере (ПК, где работает служба выгрузки), скачать его можно кнопкой справа">${p.plc ? `ПЛК ${esc(p.plc)} · ` : ''}${esc(logicPath(p))} (на сервере)</div>` : '')
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
    const p = projOf();
    if (!modal || !plcName || !choice) return;
    busy = true; drawModal();
    try {
      const target = modal.querySelector('#tia-target').value.trim() || null;
      // проект из найденных: на сервер уходят только имя ПЛК и ключ найденного файла (пути нет); текущий проект ПЛК вне папок поиска: по его id
      const body = choice === PAIR && p ? { project_ids: [p.id], target_dir: target } : { plc: plcName, found_key: choice, target_dir: target };
      await api('/api/tia/export', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      typed.delete(plcName);
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
