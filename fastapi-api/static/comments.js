// Комментарии к простоям (ответы, правка, пометка ОТО), подсказка по @ и колокольчик уведомлений
(() => {
  'use strict';

  const OTO_ROLES = ['admin', 'chief', 'area_head', 'maintenance'];
  const MODERATORS = ['admin', 'chief', 'area_head'];

  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const fmtTs = ts => new Date(ts).toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
  const toast = t => (typeof showToast === 'function' ? showToast(t) : alert(t));
  const me = () => { const st = window.authState && window.authState(); return st && st.user ? st.user : null; };
  const canWrite = () => { const u = me(); return !!u && u.role !== 'viewer'; };
  const canOto = () => { const u = me(); return !!u && (OTO_ROLES.includes(u.role) || u.department === 'oto'); };

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
    } catch { return { ok: false, data: { detail: 'Нет связи с сервером' } }; }
  }
  const errText = r => (r.data && typeof r.data.detail === 'string') ? r.data.detail : 'Не удалось выполнить действие';

  // ---------- список пользователей для @ ----------
  let people = [], peopleAt = 0;
  async function loadPeople() {
    if (!me()) return [];
    if (Date.now() - peopleAt < 60000) return people;
    const r = await call('GET', '/api/kpi/mentionable');
    if (r.ok) { people = r.data; peopleAt = Date.now(); }
    return people;
  }

  // Подсказка по @ для поля ввода. Возвращает функцию, которая отдаёт id выбранных людей, чьё имя осталось в тексте
  function attachMentions(ta, picked) {
    const pop = document.createElement('div');
    pop.className = 'cm-pop'; pop.hidden = true;
    ta.parentNode.style.position = 'relative';
    ta.after(pop);
    let items = [], sel = 0, range = null;

    const close = () => { pop.hidden = true; items = []; range = null; };
    const choose = p => {
      if (!range) return;
      const ins = `@${p.name} `;
      ta.value = ta.value.slice(0, range.start) + ins + ta.value.slice(range.end);
      const pos = range.start + ins.length;
      ta.setSelectionRange(pos, pos);
      picked.set(p.id, p.name);
      close(); ta.focus();
    };
    const draw = () => {
      pop.innerHTML = items.map((p, i) => `<button type="button" class="cm-opt${i === sel ? ' sel' : ''}" data-i="${i}" title="Отметить ${esc(p.name)}: он получит уведомление"><b>${esc(p.name)}</b><small>${esc([p.department_name, p.role_name].filter(Boolean).join(' · '))}</small></button>`).join('');
      pop.querySelectorAll('.cm-opt').forEach(b => b.addEventListener('mousedown', e => { e.preventDefault(); choose(items[Number(b.dataset.i)]); }));
      pop.hidden = !items.length;
    };
    const update = async () => {
      const caret = ta.selectionStart;
      const m = /@([^\s@]*(?: [^\s@]*)?)$/.exec(ta.value.slice(0, caret));
      if (!m) { close(); return; }
      const q = m[1].toLowerCase();
      const list = await loadPeople();
      items = list.filter(p => p.name.toLowerCase().includes(q)).slice(0, 8);
      sel = 0;
      range = { start: caret - m[1].length - 1, end: caret };
      draw();
    };
    ta.addEventListener('input', update);
    ta.addEventListener('click', update);
    ta.addEventListener('blur', () => setTimeout(close, 150));
    ta.addEventListener('keydown', e => {
      if (pop.hidden) return;
      if (e.key === 'ArrowDown') { e.preventDefault(); sel = (sel + 1) % items.length; draw(); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); sel = (sel - 1 + items.length) % items.length; draw(); }
      else if (e.key === 'Enter' || e.key === 'Tab') { e.preventDefault(); e.stopPropagation(); choose(items[sel]); }
      else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(); }
    });
    return () => [...picked].filter(([, name]) => ta.value.includes(`@${name}`)).map(([id]) => id);
  }

  const renderText = (c) => {
    let t = esc(c.text);
    (c.mention_names || []).forEach(n => { t = t.split(`@${esc(n)}`).join(`<span class="cm-men">@${esc(n)}</span>`); });
    return t.replace(/\n/g, '<br>');
  };

  // ---------- блок комментариев в панели простоя ----------
  function mount(box, itemId, onChange) {
    let comments = [];
    box.innerHTML = '<div class="cm-title">Комментарии</div><div class="cm-list"></div><div class="cm-new"></div>';
    const listEl = box.querySelector('.cm-list'), newEl = box.querySelector('.cm-new'), titleEl = box.querySelector('.cm-title');
    let mainComposer = null, composerOpen = false;

    async function reload() {
      const r = await call('GET', `/api/kpi/items/${itemId}/comments`);
      comments = r.ok ? r.data : [];
      draw();
    }
    async function changed() { await reload(); if (onChange) onChange(comments.filter(c => !c.deleted).length); }

    // Поле ввода: новый комментарий, ответ или правка
    function composer({ parentId = null, edit = null, cancellable = false, onDone }) {
      const wrap = document.createElement('div');
      wrap.className = 'cm-form';
      const user = me();
      const isEdit = !!edit;
      wrap.innerHTML = `
        <textarea rows="2" maxlength="2000" placeholder="${isEdit ? 'Изменить комментарий' : parentId ? 'Ваш ответ…' : 'Напишите комментарий. Чтобы отметить коллегу, введите @ и начало фамилии'}" title="Текст комментария. Введите @ и первые буквы фамилии, чтобы отметить коллегу: он получит уведомление"></textarea>
        <div class="cm-bar">
          ${!isEdit && !(canOto()) ? '<label class="cm-chk" title="Коллеги из ОТО получат уведомление и смогут оставить комментарий по оборудованию"><input type="checkbox" data-f="req"> Запросить ОТО</label>' : ''}
          ${!isEdit && canOto() ? '<label class="cm-chk" title="Отметить комментарий как ответ ОТО: он будет выделен для остальных"><input type="checkbox" data-f="oto"> Комментарий ОТО</label>' : ''}
          <span style="flex:1"></span>
          ${parentId || isEdit || cancellable ? '<button type="button" data-a="cancel" class="dt-sec" title="Отменить и закрыть поле ввода">Отмена</button>' : ''}
          <button type="button" data-a="send" title="${isEdit ? 'Сохранить изменённый комментарий' : 'Отправить комментарий: отмеченные коллеги получат уведомление'}">${isEdit ? 'Сохранить' : parentId ? 'Ответить' : 'Отправить'}</button>
        </div>
        <div class="auth-err" hidden></div>`;
      const ta = wrap.querySelector('textarea');
      const picked = new Map();
      if (edit) {
        ta.value = edit.text;
        (edit.mentions || []).forEach((id, i) => { if (edit.mention_names && edit.mention_names[i]) picked.set(id, edit.mention_names[i]); });
      }
      loadPeople();
      const ids = attachMentions(ta, picked);
      const err = wrap.querySelector('.auth-err');
      const cancel = wrap.querySelector('[data-a="cancel"]');
      if (cancel) cancel.addEventListener('click', () => onDone && onDone(false));
      wrap.querySelector('[data-a="send"]').addEventListener('click', async e => {
        const text = ta.value.trim();
        if (!text) { err.textContent = 'Введите текст комментария'; err.hidden = false; return; }
        e.target.disabled = true; err.hidden = true;
        const r = isEdit
          ? await call('PUT', `/api/kpi/comments/${edit.id}`, { text, mentions: ids() })
          : await call('POST', `/api/kpi/items/${itemId}/comments`, {
            text, parent_id: parentId, mentions: ids(),
            is_oto: !!(wrap.querySelector('[data-f="oto"]') || {}).checked,
            oto_request: !!(wrap.querySelector('[data-f="req"]') || {}).checked,
          });
        if (!r.ok) { err.textContent = errText(r); err.hidden = false; e.target.disabled = false; return; }
        if (onDone) onDone(true);
        ta.value = ''; picked.clear(); e.target.disabled = false;   // основное поле остаётся на месте для следующего комментария
        await changed();
      });
      return wrap;
    }

    function commentEl(c, depth, byParent) {
      const el = document.createElement('div');
      el.className = 'cm-item' + (depth ? ' reply' : '');
      el.style.marginLeft = `${Math.min(depth, 3) * 18}px`;
      if (c.deleted) {
        el.innerHTML = '<div class="cm-del">Комментарий удалён</div>';
      } else {
        const u = me();
        const mine = !!u && c.user_id === u.id;
        const badge = c.is_oto ? '<span class="cm-badge oto" title="Комментарий от ОТО">ОТО</span>'
          : c.oto_request ? '<span class="cm-badge req" title="Автор попросил ОТО посмотреть этот простой">запрос ОТО</span>' : '';
        el.innerHTML = `
          <div class="cm-head"><b>${esc(c.user_name)}</b>${c.department_name ? `<span class="cm-dep">${esc(c.department_name)}</span>` : ''}${badge}
            <span class="cm-ts">${fmtTs(c.created_at)}${c.edited_at ? ' · изменён' : ''}</span></div>
          <div class="cm-text">${renderText(c)}</div>
          <div class="cm-acts">
            ${canWrite() ? '<button type="button" data-a="reply" title="Ответить на этот комментарий: автор получит уведомление">Ответить</button>' : ''}
            ${mine ? '<button type="button" data-a="edit" title="Изменить свой комментарий">Изменить</button>' : ''}
            ${(mine || (u && MODERATORS.includes(u.role))) && canWrite() ? '<button type="button" data-a="del" title="Удалить комментарий (ответы на него сохранятся)">Удалить</button>' : ''}
          </div>
          <div class="cm-slot"></div>`;
        const slot = el.querySelector('.cm-slot');
        const act = (a, fn) => { const b = el.querySelector(`[data-a="${a}"]`); if (b) b.addEventListener('click', fn); };
        act('reply', () => { slot.replaceChildren(composer({ parentId: c.id, onDone: () => slot.replaceChildren() })); slot.querySelector('textarea').focus(); });
        act('edit', () => { slot.replaceChildren(composer({ edit: c, onDone: () => slot.replaceChildren() })); slot.querySelector('textarea').focus(); });
        act('del', async () => {
          if (!confirm('Удалить комментарий?')) return;
          const r = await call('DELETE', `/api/kpi/comments/${c.id}`);
          if (!r.ok) { toast(errText(r)); return; }
          await changed();
        });
      }
      const wrap = document.createElement('div');
      wrap.append(el);
      (byParent.get(c.id) || []).forEach(ch => wrap.append(commentEl(ch, depth + 1, byParent)));
      return wrap;
    }

    function draw() {
      const byParent = new Map();
      comments.forEach(c => { const k = c.parent_id || 0; if (!byParent.has(k)) byParent.set(k, []); byParent.get(k).push(c); });
      listEl.replaceChildren(...((byParent.get(0) || []).map(c => commentEl(c, 0, byParent))));
      const has = comments.length > 0;
      titleEl.hidden = !has;
      if (!canWrite()) { mainComposer = null; newEl.replaceChildren(); return; }
      if (composerOpen) {
        // поле ввода показывается только по кнопке «+ Добавить комментарий»; после отправки или отмены снова прячется
        if (!mainComposer) {
          mainComposer = composer({ cancellable: true, onDone: () => { composerOpen = false; mainComposer = null; draw(); } });
        }
        if (newEl.firstChild !== mainComposer) newEl.replaceChildren(mainComposer);
      } else {
        mainComposer = null;
        const b = document.createElement('button');
        b.type = 'button'; b.className = 'cm-add';
        b.textContent = '+ Добавить комментарий';
        b.title = 'Написать комментарий к этой строке: обсудить причину и время, отметить коллегу через @ или запросить ОТО';
        b.addEventListener('click', () => { composerOpen = true; draw(); const t = newEl.querySelector('textarea'); if (t) t.focus(); });
        newEl.replaceChildren(b);
      }
    }
    // Ответы видны сразу: пока панель открыта, обсуждение обновляется само (не трогаем его, если открыта форма ответа или правки)
    const timer = setInterval(() => {
      if (!box.isConnected) { clearInterval(timer); return; }
      if (document.hidden || box.querySelector('.cm-slot > *')) return;
      reload();
    }, 10000);
    reload();
  }
  window.dtComments = { mount };

  // ---------- колокольчик уведомлений ----------
  const tools = document.querySelector('.tabs-tools');
  const authBox = document.querySelector('#auth-box');
  const bell = document.createElement('div');
  bell.className = 'notif-wrap'; bell.hidden = true;
  bell.innerHTML = `<button type="button" class="notif-btn" title="Уведомления: упоминания, ответы на ваши комментарии и запросы к ОТО">🔔<b class="notif-badge" hidden>0</b></button><div class="notif-menu" hidden></div>`;
  tools.insertBefore(bell, authBox);
  const bellBtn = bell.querySelector('.notif-btn'), badge = bell.querySelector('.notif-badge'), menu = bell.querySelector('.notif-menu');
  const KIND = { mention: 'отметил(а) вас', reply: 'ответил(а) вам', oto: 'просит ОТО посмотреть' };

  async function pollBell() {
    if (!me()) { bell.hidden = true; return; }
    bell.hidden = false;
    const r = await call('GET', '/api/kpi/notifications?limit=30');
    if (!r.ok) return;
    badge.textContent = r.data.unread > 99 ? '99+' : String(r.data.unread);
    badge.hidden = !r.data.unread;
    if (!menu.hidden) drawMenu(r.data.items);
    bell._items = r.data.items;
  }
  function drawMenu(items) {
    menu.innerHTML = `<div class="notif-top"><b>Уведомления</b><button type="button" data-a="all" title="Отметить все уведомления прочитанными">Прочитать все</button></div>`
      + (items.length ? items.map(n => `<button type="button" class="notif-item${n.read_at ? '' : ' unread'}" data-id="${n.id}" data-dt="${n.downtime_id}" data-item="${n.item_id ?? ''}" title="Открыть простой и комментарий">
          <span><b>${esc(n.from_name)}</b> ${KIND[n.kind] || ''}</span><small>${esc(n.snippet)}</small><em>${fmtTs(n.created_at)}</em></button>`).join('')
        : '<div class="notif-empty">Уведомлений пока нет</div>');
    menu.querySelector('[data-a="all"]').addEventListener('click', async () => { await call('POST', '/api/kpi/notifications/read', {}); pollBell(); });
    menu.querySelectorAll('.notif-item').forEach(b => b.addEventListener('click', async () => {
      menu.hidden = true;
      await call('POST', '/api/kpi/notifications/read', { ids: [Number(b.dataset.id)] });
      pollBell();
      if (window.dtOpenById) window.dtOpenById(Number(b.dataset.dt), b.dataset.item ? Number(b.dataset.item) : null);
    }));
  }
  bellBtn.addEventListener('click', e => {
    e.stopPropagation();
    menu.hidden = !menu.hidden;
    if (!menu.hidden) drawMenu(bell._items || []);
    pollBell();
  });
  document.addEventListener('click', e => { if (!menu.hidden && !bell.contains(e.target)) menu.hidden = true; });

  const prev = window.onAuthChanged;
  window.onAuthChanged = () => { if (prev) prev(); peopleAt = 0; pollBell(); };
  setInterval(() => { if (!document.hidden) pollBell(); }, 30000);
  pollBell();
})();
