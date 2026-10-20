// Вкладка «Оповещения»: привязка своего Telegram, подписки на события, все чаты (администратор). Сервер: tgnotify.py, /api/telegram
(function (root) {
  'use strict';

  // ================= чистая логика (без DOM, проверяется в node) =================
  const core = (() => {
    const SCOPE_KEYS = ['brands', 'stations', 'plc_ids'];   // что считается «Линиями» (первое из событий)
    const SCOPE_MAX_ITEMS = 50;
    const SCOPE_MAX_LEN = 60;
    const Q_BOTH = 'Тихие часы: заполните оба поля или оставьте оба пустыми';
    const Q_RANGE = 'Тихие часы: целые числа 0..23';

    // поле «Линии» какого события: первое из brands / stations / plc_ids в scope_fields, иначе null
    function scopeKey(fields) {
      const f = Array.isArray(fields) ? fields : [];
      return SCOPE_KEYS.find(k => f.includes(k)) || null;
    }

    // «GWM, Chery;  GWM» -> ['GWM', 'Chery']: запятая или точка с запятой, без пустых и повторов
    function parseList(text) {
      const out = [];
      for (const part of String(text ?? '').split(/[,;]/)) {
        const v = part.trim();
        if (v && !out.includes(v)) out.push(v);
      }
      return out;
    }
    function listError(list) {
      if (list.length > SCOPE_MAX_ITEMS) return `Линии: не больше ${SCOPE_MAX_ITEMS} значений`;
      if (list.some(v => v.length > SCOPE_MAX_LEN)) return `Линии: значение длиннее ${SCOPE_MAX_LEN} символов`;
      return '';
    }
    const listText = list => (Array.isArray(list) ? list.join(', ') : '');

    // тихие часы из полей: оба пустые = нет; оба целые 0..23 = есть; иначе текст ошибки (как на сервере)
    function quietCheck(from, to) {
      const a = String(from ?? '').trim(), b = String(to ?? '').trim();
      if (!a && !b) return { from: null, to: null, error: '' };
      if (!a || !b) return { from: null, to: null, error: Q_BOTH };
      const ok = s => /^\d{1,2}$/.test(s) && Number(s) <= 23;
      if (!ok(a) || !ok(b)) return { from: null, to: null, error: Q_RANGE };
      return { from: Number(a), to: Number(b), error: '' };
    }

    // час попадает в тихие часы (как in_quiet_hours на сервере: через полночь тоже, from == to = нет тишины)
    function inQuiet(from, to, hour) {
      if (from === null || from === undefined || to === null || to === undefined || from === to) return false;
      return from < to ? hour >= from && hour < to : hour >= from || hour < to;
    }

    // строки таблицы -> items для PUT /subscriptions: только включённые; прочие поля области и also_clear берутся из прежней подписки
    // rows: [{event_type, enabled, lines, quiet_from, quiet_to}], catalog: GET /events, prev: subscriptions из /me
    function buildItems(catalog, rows, prev) {
      const cat = new Map((catalog || []).map(e => [e.event_type, e]));
      const old = new Map((prev || []).map(s => [s.event_type, s]));
      const items = [];
      for (const r of rows || []) {
        if (!r.enabled) continue;
        const ev = cat.get(r.event_type);
        if (!ev) continue;
        const title = ev.title || r.event_type;
        const q = quietCheck(r.quiet_from, r.quiet_to);
        if (q.error) return { items: null, error: `${title}: ${q.error}` };
        const was = old.get(r.event_type) || {};
        const fields = ev.scope_fields || [];
        const scope = {};
        for (const [k, v] of Object.entries(was.scope || {})) if (fields.includes(k) && Array.isArray(v) && v.length) scope[k] = v;
        const key = scopeKey(fields);
        if (key) {
          const list = parseList(r.lines);
          const le = listError(list);
          if (le) return { items: null, error: `${title}: ${le}` };
          if (list.length) scope[key] = list; else delete scope[key];
        }
        items.push({ event_type: r.event_type, enabled: true, scope, also_clear: !!was.also_clear && !!ev.supports_clear,
          quiet_from: q.from, quiet_to: q.to });
      }
      return { items, error: '' };
    }

    // подписки + каталог -> строки таблицы (событие без подписки выключено)
    function rowsFrom(catalog, subs) {
      const old = new Map((subs || []).map(s => [s.event_type, s]));
      return (catalog || []).map(ev => {
        const s = old.get(ev.event_type);
        const key = scopeKey(ev.scope_fields);
        return { event_type: ev.event_type, enabled: !!(s && s.enabled), lines: key && s ? listText((s.scope || {})[key]) : '',
          quiet_from: s && s.quiet_from !== null && s.quiet_from !== undefined ? String(s.quiet_from) : '',
          quiet_to: s && s.quiet_to !== null && s.quiet_to !== undefined ? String(s.quiet_to) : '' };
      });
    }

    // текст ответа на «Отправить проверку»
    function testText(res) {
      if (!res || !res.queued) return 'не поставлено в очередь';
      return res.env === 'prod' ? 'поставлено в очередь' : 'поставлено в очередь · среда: разработка, сообщения в разработке не отправляются';
    }

    // detail ответа сервера -> строка (422 от FastAPI бывает списком)
    function detailText(status, data) {
      if (status === 0) return 'Нет связи с сервером';
      const d = data && data.detail;
      if (typeof d === 'string') return d;
      if (Array.isArray(d) && d.length) return d.map(x => (x && x.msg) || '').filter(Boolean).join('; ') || `Ошибка ${status}`;
      return `Ошибка ${status}`;
    }

    return { scopeKey, parseList, listText, listError, quietCheck, inQuiet, buildItems, rowsFrom, testText, detailText };
  })();

  root.tgnotifyCore = core;
  if (typeof module !== 'undefined' && module.exports) module.exports = core;
  if (typeof document === 'undefined') return;

  // ================= экран =================
  const pane = document.querySelector('#notifications');
  const tabBtn = document.querySelector('.tab[data-tab="notifications"]');
  if (!pane || !tabBtn) return;

  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const TIMEOUT = 6000;

  async function api(method, url, body) {
    try {
      const r = await fetch(url, {
        method, credentials: 'same-origin', signal: AbortSignal.timeout(TIMEOUT),
        headers: body ? { 'Content-Type': 'application/json' } : {},
        body: body ? JSON.stringify(body) : undefined,
      });
      let data = null;
      try { data = await r.json(); } catch { /* пустой ответ */ }
      return { ok: r.ok, status: r.status, data };
    } catch {
      return { ok: false, status: 0, data: null };
    }
  }
  const errOf = res => core.detailText(res.status, res.data);
  const mins = sec => Math.round((sec || 0) / 60);

  const st = {
    built: false, loading: false, me: null, events: [], admin: false,
    rows: [], dirty: false, link: null, linkTimer: null, group: null,
  };

  function build() {
    pane.innerHTML = `
      <p class="tg-msg" id="tg-msg" hidden></p>
      <div class="tg-grid" id="tg-grid">
        <section class="tg-card" id="tg-mine">
          <h3 class="tg-h">Мой Telegram</h3>
          <div class="tg-status" id="tg-me-status" title="Привязан ли ваш личный чат Telegram к учётной записи PMM: зелёная точка означает, что оповещения приходят в этот чат"></div>
          <div class="tg-btns">
            <button type="button" id="tg-link" title="Получить ссылку на бота: откройте её в Telegram и нажмите «Старт», чат привяжется к вашей учётной записи">Привязать Telegram</button>
            <button type="button" id="tg-test" class="dt-sec" title="Отправить в ваш чат Telegram пробное сообщение «Проверка связи PMM», чтобы убедиться, что оповещения доходят">Отправить проверку</button>
            <button type="button" id="tg-unlink" class="dt-sec" title="Отвязать ваш чат Telegram: оповещения перестанут приходить, подписки на события будут удалены">Отвязать</button>
            <span class="tg-note" id="tg-test-res"></span>
          </div>
          <div class="tg-link" id="tg-link-box" hidden></div>
        </section>
        <section class="tg-card" id="tg-subs">
          <h3 class="tg-h">Что присылать мне</h3>
          <p class="tg-note" id="tg-subs-off" hidden>Доступно после привязки Telegram</p>
          <div class="tg-tablewrap"><table class="tg-table" id="tg-subs-table"></table></div>
          <div class="tg-btns">
            <button type="button" id="tg-save" title="Сохранить выбранные события, линии и тихие часы: оповещения будут приходить по новым настройкам сразу">Сохранить</button>
            <span class="tg-note" id="tg-save-res"></span>
          </div>
        </section>
        <section class="tg-card tg-wide" id="tg-admin" hidden>
          <h3 class="tg-h">Все чаты</h3>
          <div class="tg-status" id="tg-bot-status" title="Состояние бота оповещений: имя бота, среда (в разработке сообщения не отправляются) и итоги отправки за последние сутки"></div>
          <div class="tg-tablewrap"><table class="tg-table" id="tg-chats-table"></table></div>
          <div class="tg-btns">
            <button type="button" id="tg-group" title="Получить команду привязки общей группы: добавьте бота в группу Telegram и отправьте там эту команду">Привязать группу</button>
            <span class="tg-note" id="tg-chats-res"></span>
          </div>
          <div class="tg-link" id="tg-group-box" hidden></div>
        </section>
      </div>`;
    pane.querySelector('#tg-link').addEventListener('click', doLink);
    pane.querySelector('#tg-test').addEventListener('click', () => doTest(null, pane.querySelector('#tg-test-res')));
    pane.querySelector('#tg-unlink').addEventListener('click', doUnlink);
    pane.querySelector('#tg-save').addEventListener('click', doSave);
    pane.querySelector('#tg-group').addEventListener('click', doGroup);
    const subsT = pane.querySelector('#tg-subs-table');
    subsT.addEventListener('input', onSubsEdit);
    subsT.addEventListener('change', onSubsEdit);
    const chatsT = pane.querySelector('#tg-chats-table');
    chatsT.addEventListener('change', onChatToggle);
    chatsT.addEventListener('click', onChatAction);
    st.built = true;
  }

  const linked = () => !!(st.me && st.me.linked && st.me.chat && st.me.chat.enabled);

  function msg(text) {
    const el = pane.querySelector('#tg-msg');
    el.textContent = text || '';
    el.hidden = !text;
  }

  // ---------- 1. Мой Telegram ----------
  function drawMine() {
    const on = linked();
    pane.querySelector('#tg-me-status').innerHTML = on
      ? `<span class="tg-dot tg-dot-on"></span>привязан: ${esc(st.me.chat.title || 'Telegram')}`
      : '<span class="tg-dot"></span><span class="tg-muted">не привязан</span>';
    pane.querySelector('#tg-link').hidden = on;
    pane.querySelector('#tg-test').hidden = !on;
    pane.querySelector('#tg-unlink').hidden = !on;
    const box = pane.querySelector('#tg-link-box');
    if (on || !st.link) { box.hidden = true; box.innerHTML = ''; return; }
    const l = st.link;
    const left = `код действует ${mins(l.expires_in_sec)} мин`;
    box.innerHTML = l.url
      ? `<a href="${esc(l.url)}" target="_blank" rel="noopener" title="Откройте ссылку в Telegram и нажмите «Старт»: чат привяжется к вашей учётной записи, страница обновится сама">${esc(l.url)}</a>
         <span class="tg-note">код <b class="tg-code">${esc(l.code)}</b> · ${left}</span>`
      : `<span>Напишите боту${l.bot_username ? ' <b>@' + esc(l.bot_username) + '</b>' : ''} команду <b class="tg-code">/start ${esc(l.code)}</b></span>
         <span class="tg-note">${left}${l.message ? ' · ' + esc(l.message) : ''}</span>`;
    box.hidden = false;
  }

  async function doLink() {
    const res = await api('POST', '/api/telegram/link', { kind: 'user' });
    if (!res.ok) { pane.querySelector('#tg-test-res').textContent = errOf(res); return; }
    st.link = res.data;
    drawMine();
    clearInterval(st.linkTimer);
    const until = Date.now() + (res.data.expires_in_sec || 900) * 1000;
    st.linkTimer = setInterval(async () => {
      if (Date.now() > until || !pane.classList.contains('active')) { stopLinkWait(); drawMine(); return; }
      const r = await api('GET', '/api/telegram/me');
      if (r.ok && r.data && r.data.linked && r.data.chat && r.data.chat.enabled) { stopLinkWait(); load(); }
    }, 5000);
  }
  function stopLinkWait() { clearInterval(st.linkTimer); st.linkTimer = null; st.link = null; }

  async function doTest(chatRef, out) {
    out.textContent = '…';
    const res = await api('POST', '/api/telegram/test', chatRef === null ? {} : { chat_ref: chatRef });
    out.textContent = res.ok ? core.testText(res.data) : errOf(res);
  }

  async function doUnlink() {
    if (!confirm('Отвязать Telegram? Оповещения перестанут приходить, подписки будут удалены.')) return;
    const res = await api('DELETE', '/api/telegram/me');
    if (!res.ok) { pane.querySelector('#tg-test-res').textContent = errOf(res); return; }
    pane.querySelector('#tg-test-res').textContent = '';
    st.dirty = false;
    load();
  }

  // ---------- 2. Что присылать мне ----------
  function drawSubs() {
    const on = linked();
    const card = pane.querySelector('#tg-subs');
    card.classList.toggle('tg-off', !on);
    pane.querySelector('#tg-subs-off').hidden = on;
    pane.querySelector('#tg-save').disabled = !on;
    if (st.dirty) return;   // не затирать несохранённые правки при обновлении
    st.rows = core.rowsFrom(st.events, on ? st.me.subscriptions : []);
    const dis = on ? '' : ' disabled';
    const body = st.rows.map((r, i) => {
      const ev = st.events[i];
      const key = core.scopeKey(ev.scope_fields);
      const lines = key
        ? `<input type="text" class="tg-lines" data-f="lines" value="${esc(r.lines)}" placeholder="все"${dis} title="Значения через запятую (например, GWM, Chery), только по ним будут приходить оповещения; пусто = по всем">`
        : '<span class="tg-muted" title="Это событие не делится по линиям: приходит всегда, если включено">—</span>';
      return `<tr data-i="${i}" title="${esc(ev.description || '')}">
        <td>${esc(ev.title || ev.event_type)}</td>
        <td class="tg-c"><input type="checkbox" data-f="enabled"${r.enabled ? ' checked' : ''}${dis} title="Присылать это событие в ваш Telegram: отметьте и нажмите «Сохранить»"></td>
        <td>${lines}</td>
        <td class="tg-c"><input type="number" class="tg-hour" data-f="quiet_from" min="0" max="23" step="1" value="${esc(r.quiet_from)}"${dis} title="Начало тихих часов (0–23, время завода): с этого часа событие не присылается; заполните оба поля или ни одного"></td>
        <td class="tg-c"><input type="number" class="tg-hour" data-f="quiet_to" min="0" max="23" step="1" value="${esc(r.quiet_to)}"${dis} title="Конец тихих часов (0–23, время завода): с этого часа присылать снова; можно через полночь, например с 22 до 6"></td>
      </tr>`;
    }).join('');
    const t = pane.querySelector('#tg-subs-table');
    t.innerHTML = `<thead><tr>
        <th title="Какое событие присылать; наведите на строку, чтобы увидеть подробное описание события">Событие</th>
        <th class="tg-c" title="Включить оповещение об этом событии в ваш Telegram">Вкл</th>
        <th title="Линии, станции или ПЛК через запятую; пусто означает все">Линии</th>
        <th class="tg-c" title="Тихие часы: с какого часа не присылать (0–23)">Тихо с</th>
        <th class="tg-c" title="Тихие часы: до какого часа не присылать (0–23)">до</th>
      </tr></thead><tbody>${body}</tbody>`;
    if (window.attachColWidths) window.attachColWidths(t, 'tg-subs');
  }

  function onSubsEdit(e) {
    const f = e.target.dataset.f;
    const tr = e.target.closest('tr[data-i]');
    if (!f || !tr) return;
    const r = st.rows[Number(tr.dataset.i)];
    r[f] = f === 'enabled' ? e.target.checked : e.target.value;
    st.dirty = true;
    pane.querySelector('#tg-save-res').textContent = 'не сохранено';
  }

  async function doSave() {
    const out = pane.querySelector('#tg-save-res');
    const b = core.buildItems(st.events, st.rows, st.me && st.me.subscriptions);
    if (b.error) { out.textContent = b.error; return; }
    const res = await api('PUT', '/api/telegram/subscriptions', { items: b.items });
    if (!res.ok) { out.textContent = errOf(res); return; }
    out.textContent = 'сохранено';
    st.dirty = false;
    if (st.me) st.me.subscriptions = res.data.items;
    drawSubs();
  }

  // ---------- 3. Все чаты (администратор) ----------
  function drawBot(s) {
    const el = pane.querySelector('#tg-bot-status');
    if (!s) { el.innerHTML = '<span class="tg-muted">состояние бота недоступно</span>'; return; }
    const bot = s.polling && s.polling.bot_username;
    const q = s.queue || {};
    const parts = [];
    if (!s.configured) parts.push('<span class="tg-bad">ключ бота не задан</span>');
    if (bot) parts.push(`бот @${esc(bot)}`);
    parts.push(s.env === 'prod' ? 'среда: рабочая' : 'среда: разработка');
    parts.push(`за сутки: отправлено ${Number(q.sent) || 0}, <span class="${q.failed ? 'tg-bad' : ''}">ошибок ${Number(q.failed) || 0}</span>`);
    el.innerHTML = parts.join(' · ');
  }

  function drawChats(list) {
    const t = pane.querySelector('#tg-chats-table');
    const rows = (list || []).map(c => `<tr data-id="${Number(c.id)}" class="${c.enabled ? '' : 'tg-dim'}">
        <td>${esc(c.title || '—')}</td>
        <td>${c.kind === 'user' ? 'личный' : 'группа'}</td>
        <td>${esc(c.user_name || '—')}</td>
        <td class="tg-n">${Number(c.subscriptions) || 0}</td>
        <td class="tg-c"><input type="checkbox" data-act="enabled"${c.enabled ? ' checked' : ''} title="Включить или выключить отправку оповещений в этот чат, подписки при выключении сохраняются"></td>
        <td class="tg-act">
          <button type="button" class="dt-sec" data-act="test" title="Отправить в этот чат пробное сообщение «Проверка связи PMM», чтобы проверить доставку">Проверка</button>
          <button type="button" class="dt-sec" data-act="del" title="Удалить чат из PMM вместе с его подписками; чтобы вернуть, привяжите его заново">Удалить</button>
        </td>
      </tr>`).join('');
    t.innerHTML = `<thead><tr>
        <th title="Название чата или группы в Telegram">Чат</th>
        <th title="Личный чат пользователя или общая группа">Тип</th>
        <th title="Учётная запись PMM, к которой привязан личный чат">Пользователь</th>
        <th class="tg-n" title="Сколько событий настроено для этого чата">Подписок</th>
        <th class="tg-c" title="Отправляются ли оповещения в этот чат">Включён</th>
        <th title="Проверка доставки и удаление чата">Действия</th>
      </tr></thead><tbody>${rows || '<tr><td colspan="6" class="tg-muted">Чатов пока нет</td></tr>'}</tbody>`;
    if (window.attachColWidths) window.attachColWidths(t, 'tg-chats');
  }

  async function onChatToggle(e) {
    if (e.target.dataset.act !== 'enabled') return;
    const id = Number(e.target.closest('tr').dataset.id);
    const res = await api('PUT', `/api/telegram/chats/${id}`, { enabled: e.target.checked });
    pane.querySelector('#tg-chats-res').textContent = res.ok ? '' : errOf(res);
    loadAdmin();
  }

  async function onChatAction(e) {
    const btn = e.target.closest('button[data-act]');
    if (!btn) return;
    const tr = btn.closest('tr');
    const id = Number(tr.dataset.id);
    const out = pane.querySelector('#tg-chats-res');
    if (btn.dataset.act === 'test') { doTest(id, out); return; }
    if (btn.dataset.act === 'del') {
      const name = tr.cells[0].textContent;
      if (!confirm(`Удалить чат «${name}» вместе с подписками?`)) return;
      const res = await api('DELETE', `/api/telegram/chats/${id}`);
      out.textContent = res.ok ? '' : errOf(res);
      loadAdmin();
    }
  }

  async function doGroup() {
    const box = pane.querySelector('#tg-group-box');
    const res = await api('POST', '/api/telegram/link', { kind: 'group' });
    if (!res.ok) { box.hidden = false; box.textContent = errOf(res); return; }
    const l = res.data;
    box.innerHTML = `<b class="tg-code" title="Команда привязки группы: скопируйте её и отправьте в группе Telegram, куда добавлен бот">${esc(l.command || '/bind ' + l.code)}</b>
      <span class="tg-note">отправьте её в группе, куда добавлен бот · код действует ${mins(l.expires_in_sec)} мин</span>`;
    box.hidden = false;
  }

  // ---------- загрузка ----------
  async function loadAdmin() {
    const [s, c] = await Promise.all([api('GET', '/api/telegram/status'), api('GET', '/api/telegram/chats')]);
    drawBot(s.ok ? s.data : null);
    if (c.ok) drawChats(c.data);
    else pane.querySelector('#tg-chats-res').textContent = errOf(c);
  }

  async function load() {
    if (st.loading) return;
    st.loading = true;
    try {
      if (!st.built) build();
      const [me, ev, au] = await Promise.all([api('GET', '/api/telegram/me'), api('GET', '/api/telegram/events'), api('GET', '/api/auth/me')]);
      const grid = pane.querySelector('#tg-grid');
      if (me.status === 401 || ev.status === 401) { grid.hidden = true; msg('Войдите, чтобы настроить оповещения'); return; }
      if (!me.ok || !ev.ok) { msg(errOf(me.ok ? ev : me)); return; }   // старые данные остаются на экране
      grid.hidden = false;
      msg('');
      st.me = me.data;
      st.events = ev.data || [];
      st.admin = !!(au.ok && au.data && au.data.user && au.data.user.role === 'admin');
      drawMine();
      drawSubs();
      pane.querySelector('#tg-admin').hidden = !st.admin;
      if (st.admin) await loadAdmin();
    } finally {
      st.loading = false;
    }
  }

  tabBtn.addEventListener('click', load);
  setInterval(() => { if (pane.classList.contains('active') && !document.hidden) load(); }, 15000);
  const prevAuth = window.onAuthChanged;
  window.onAuthChanged = () => { if (prevAuth) prevAuth(); st.dirty = false; stopLinkWait(); if (pane.classList.contains('active')) load(); };
})(typeof window !== 'undefined' ? window : globalThis);
