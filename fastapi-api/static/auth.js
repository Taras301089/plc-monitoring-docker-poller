// Вход в систему, страница «Пользователи», смена пароля
(() => {
  'use strict';

  const authBox = document.querySelector('#auth-box');
  const tabUsers = document.querySelector('#tab-users');
  const usersPane = document.querySelector('#users');
  const state = { user: null, setupRequired: false, roles: {}, departments: {}, pending: 0 };
  window.authState = () => state;

  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const shortName = u => `${u.last_name} ${u.first_name.charAt(0)}.`;

  async function api(method, url, body) {
    try {
      const r = await fetch(url, {
        method, credentials: 'same-origin',
        headers: body ? { 'Content-Type': 'application/json' } : {},
        body: body ? JSON.stringify(body) : undefined,
      });
      let data = null;
      try { data = await r.json(); } catch { /* пустой ответ */ }
      return { ok: r.ok, status: r.status, data };
    } catch {
      return { ok: false, status: 0, data: { detail: 'Нет связи с сервером' } };
    }
  }
  const errText = res => {
    const d = res.data && res.data.detail;
    return typeof d === 'string' ? d : 'Проверьте правильность заполнения полей';
  };

  function openModal(html, dismissible = true) {
    const m = document.createElement('div');
    m.className = 'kpi-modal';
    m.innerHTML = `<div class="kpi-modal-box auth-modal" role="dialog">${html}</div>`;
    const close = () => { if (m.parentNode) m.parentNode.removeChild(m); };
    if (dismissible) m.addEventListener('mousedown', e => { if (e.target === m) close(); });
    document.body.appendChild(m);
    const q = s => m.querySelector(s);
    const err = q('.auth-err');
    return {
      close, q,
      showError: text => { if (err) { err.textContent = text; err.hidden = false; } },
      onCancel: fn => { const b = q('[data-act="cancel"]'); if (b) b.addEventListener('click', fn || close); },
    };
  }

  // ---------- последний открытый экран пользователя ----------
  const startedWithHash = !!location.hash;
  let viewApplied = false;
  let forceApplyView = false;
  let viewTimer = null;
  window.saveLastView = () => {
    clearTimeout(viewTimer);
    viewTimer = setTimeout(() => {
      if (!state.user) return;
      const active = document.querySelector('.tab.active[data-tab]');
      if (!active) return;
      api('PUT', '/api/auth/last-view', { tab: active.dataset.tab, screen: typeof andonScreenId === 'number' ? andonScreenId : null });
    }, 700);
  };

  // ---------- состояние ----------
  async function refresh() {
    const res = await api('GET', '/api/auth/me');
    if (res.ok) {
      if (res.data.user && !viewApplied && res.data.last_view && (!startedWithHash || forceApplyView)) {
        viewApplied = true;
        if (window.applyLastView) window.applyLastView(res.data.last_view);
      }
      state.user = res.data.user;
      state.setupRequired = res.data.setup_required;
      state.roles = res.data.roles || {};
      state.departments = res.data.departments || {};
      state.pending = res.data.pending_count || 0;
    }
    renderAuthBox();
    if (window.onAuthChanged) window.onAuthChanged();
    if (state.user && state.user.must_change_password) showChangePassword(true);
  }

  function renderAuthBox() {
    if (!state.user) {
      authBox.innerHTML = `<button type="button" class="auth-btn" id="auth-login" title="${state.setupRequired ? 'Первый запуск: создать учётную запись администратора' : 'Войти под своей учётной записью или отправить заявку на регистрацию, чтобы комментировать и менять настройки'}">🔑 ${state.setupRequired ? 'Создать администратора' : 'Войти или зарегистрироваться'}</button>`;
      authBox.querySelector('#auth-login').addEventListener('click', showLogin);
    } else {
      const u = state.user;
      authBox.innerHTML = `
        <button type="button" class="auth-btn" id="auth-user" title="Меню пользователя: смена пароля и выход"><span>${esc(shortName(u))}</span><small>${esc(u.role_name)}</small></button>
        <div class="auth-menu" hidden>
          <div class="who"><b>${esc(u.full_name)}</b><span>${esc(u.login)} · ${esc(u.role_name)}${u.department_name ? ' · ' + esc(u.department_name) : ''}</span></div>
          <button type="button" data-act="stats" title="Сколько раз вы входили и сколько времени работали в системе по дням">Моя статистика</button>
          <button type="button" data-act="pw" title="Сменить свой пароль">Сменить пароль</button>
          <button type="button" data-act="logout" title="Выйти из системы на этом устройстве">Выйти</button>
        </div>`;
      const menu = authBox.querySelector('.auth-menu');
      authBox.querySelector('#auth-user').addEventListener('click', e => { e.stopPropagation(); menu.hidden = !menu.hidden; });
      menu.querySelector('[data-act="stats"]').addEventListener('click', () => { menu.hidden = true; if (window.openMyStats) window.openMyStats(); });
      menu.querySelector('[data-act="pw"]').addEventListener('click', () => { menu.hidden = true; showChangePassword(false); });
      menu.querySelector('[data-act="logout"]').addEventListener('click', logout);
    }
    const isAdmin = !!state.user && state.user.role === 'admin';
    const canSee = !!state.user && ['admin', 'chief', 'area_head'].includes(state.user.role);
    tabUsers.hidden = !canSee;
    tabUsers.textContent = '👥 Пользователи' + (isAdmin && state.pending ? ` (${state.pending})` : '');
    tabUsers.title = state.pending && isAdmin
      ? `Пользователи системы. Заявок на регистрацию, ожидающих подтверждения: ${state.pending}`
      : (isAdmin
        ? 'Пользователи системы: учётные записи, роли, кто сейчас в сети, входы и время работы (администратор)'
        : 'Пользователи системы: кто сейчас в сети, сколько раз входили и сколько работали (просмотр для начальников)');
    if (!canSee && usersPane.classList.contains('active')) document.querySelector('.tab[data-tab="browser"]').click();
  }
  document.addEventListener('click', e => {
    const menu = authBox.querySelector('.auth-menu');
    if (menu && !authBox.contains(e.target)) menu.hidden = true;
  });

  async function logout() {
    await api('POST', '/api/auth/logout');
    await refresh();
    showToast('Вы вышли из системы');
  }

  // ---------- окна входа ----------
  function showLogin() {
    if (state.setupRequired) { showSetup(); return; }
    const m = openModal(`
      <h3>Вход в систему</h3>
      <form>
        <label>Логин</label><input name="login" autocomplete="username" autofocus title="Ваш логин, выданный администратором">
        <label>Пароль</label><input name="password" type="password" autocomplete="current-password" title="Ваш пароль">
        <div class="auth-err" hidden></div>
        <div class="kpi-modal-actions">
          <button type="button" data-act="register" class="auth-link" title="Нет учётной записи? Отправьте заявку: после подтверждения администратором вы сможете войти">Зарегистрироваться</button>
          <span style="flex:1"></span>
          <button type="button" data-act="cancel" style="background: var(--bg-tertiary); color: var(--fg-primary);" title="Закрыть окно без входа">Отмена</button>
          <button type="submit" title="Войти в систему">Войти</button>
        </div>
      </form>`);
    m.onCancel();
    m.q('[data-act="register"]').addEventListener('click', () => { m.close(); showRegister(); });
    m.q('form').addEventListener('submit', async e => {
      e.preventDefault();
      const f = e.target;
      const res = await api('POST', '/api/auth/login', { login: f.login.value, password: f.password.value });
      if (!res.ok) { m.showError(errText(res)); return; }
      m.close();
      viewApplied = false; forceApplyView = true;
      await refresh();
      showToast(`Добро пожаловать, ${state.user.full_name}`);
    });
  }

  function showRegister() {
    const m = openModal(`
      <h3>Регистрация</h3>
      <p class="hint">Заполните данные и придумайте пароль. После отправки заявку должен подтвердить администратор и назначить вам роль, затем вы сможете войти.</p>
      <form>
        <div class="row2"><div><label>Фамилия</label><input name="last_name" autocomplete="family-name" title="Ваша фамилия: по ней коллеги будут находить вас через @"></div>
        <div><label>Имя</label><input name="first_name" autocomplete="given-name" title="Ваше имя"></div></div>
        <label>Логин</label><input name="login" autocomplete="username" title="Логин для входа: латиница, цифры, точка, дефис или подчёркивание, 3–40 символов">
        <label>Отдел</label><select name="department" title="Ваш отдел: Производство, ОТО или ИТО. Руководителям можно не указывать">${departmentOptions('')}</select>
        <label>Пароль (не короче 8 символов)</label><input name="password" type="password" autocomplete="new-password" title="Придумайте пароль, не короче 8 символов">
        <label>Повторите пароль</label><input name="password2" type="password" autocomplete="new-password" title="Введите пароль ещё раз">
        <div class="auth-err" hidden></div>
        <div class="kpi-modal-actions">
          <button type="button" data-act="cancel" style="background: var(--bg-tertiary); color: var(--fg-primary);" title="Закрыть окно без отправки заявки">Отмена</button>
          <button type="submit" title="Отправить заявку на регистрацию администратору">Отправить заявку</button>
        </div>
      </form>`);
    m.onCancel();
    m.q('form').addEventListener('submit', async e => {
      e.preventDefault();
      const f = e.target;
      if (f.password.value !== f.password2.value) { m.showError('Пароли не совпадают'); return; }
      const res = await api('POST', '/api/auth/register', {
        login: f.login.value, last_name: f.last_name.value, first_name: f.first_name.value, password: f.password.value,
        department: f.department.value || null,
      });
      if (!res.ok) { m.showError(errText(res)); return; }
      m.close();
      const done = openModal(`<h3>Заявка отправлена</h3><p class="hint">Администратор подтвердит её и назначит роль. После этого войдите под своим логином и паролем.</p><div class="kpi-modal-actions"><button type="button" data-act="cancel" title="Закрыть окно">Понятно</button></div>`);
      done.onCancel();
    });
  }

  function showSetup() {
    const m = openModal(`
      <h3>Первый запуск: создайте администратора</h3>
      <p class="hint">Эта учётная запись сможет создавать пользователей. Сделайте это сразу: пока администратора нет, любой посетитель сайта может его создать.</p>
      <form>
        <div class="row2"><div><label>Фамилия</label><input name="last_name" autocomplete="family-name" title="Фамилия администратора"></div>
        <div><label>Имя</label><input name="first_name" autocomplete="given-name" title="Имя администратора"></div></div>
        <label>Логин</label><input name="login" autocomplete="username" title="Логин для входа: латиница, цифры, точка, дефис или подчёркивание, 3–40 символов">
        <label>Пароль (не короче 8 символов)</label><input name="password" type="password" autocomplete="new-password" title="Придумайте надёжный пароль, не короче 8 символов">
        <div class="auth-err" hidden></div>
        <div class="kpi-modal-actions">
          <button type="button" data-act="cancel" style="background: var(--bg-tertiary); color: var(--fg-primary);" title="Закрыть окно">Отмена</button>
          <button type="submit" title="Создать администратора и войти">Создать и войти</button>
        </div>
      </form>`);
    m.onCancel();
    m.q('form').addEventListener('submit', async e => {
      e.preventDefault();
      const f = e.target;
      const res = await api('POST', '/api/auth/setup', {
        login: f.login.value, last_name: f.last_name.value, first_name: f.first_name.value, password: f.password.value,
      });
      if (!res.ok) { m.showError(errText(res)); return; }
      m.close();
      await refresh();
      showToast('Администратор создан');
    });
  }

  function showChangePassword(forced) {
    const m = openModal(`
      <h3>${forced ? 'Смените временный пароль' : 'Смена пароля'}</h3>
      ${forced ? '<p class="hint">Администратор выдал вам временный пароль. Задайте свой: так безопаснее. Можно отложить, но окно появится снова при следующем открытии сайта.</p>' : ''}
      <form>
        <label>Текущий пароль</label><input name="old" type="password" autocomplete="current-password" title="Пароль, которым вы входите сейчас">
        <label>Новый пароль (не короче 8 символов)</label><input name="new1" type="password" autocomplete="new-password" title="Новый пароль, не короче 8 символов">
        <label>Повторите новый пароль</label><input name="new2" type="password" autocomplete="new-password" title="Введите новый пароль ещё раз">
        <div class="auth-err" hidden></div>
        <div class="kpi-modal-actions">
          <button type="button" data-act="cancel" style="background: var(--bg-tertiary); color: var(--fg-primary);" title="${forced ? 'Закрыть окно: пароль можно сменить позже через меню пользователя. Напоминание появится снова при следующем открытии сайта' : 'Закрыть окно без изменений'}">Отмена</button>
          <button type="submit" title="Сохранить новый пароль">Сохранить</button>
        </div>
      </form>`, true);
    m.onCancel();
    m.q('form').addEventListener('submit', async e => {
      e.preventDefault();
      const f = e.target;
      if (f.new1.value !== f.new2.value) { m.showError('Новые пароли не совпадают'); return; }
      const res = await api('POST', '/api/auth/change-password', { old_password: f.old.value, new_password: f.new1.value });
      if (!res.ok) { m.showError(errText(res)); return; }
      m.close();
      await refresh();
      showToast('Пароль изменён');
    });
  }

  // ---------- страница «Пользователи» ----------
  const fmtDate = iso => (iso ? new Date(iso).toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit' }) : '—');
  const genPassword = () => {
    const chars = 'abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789';
    const a = new Uint32Array(10);
    crypto.getRandomValues(a);
    return Array.from(a, n => chars[n % chars.length]).join('');
  };
  const departmentOptions = sel => `<option value="">— не указан</option>` +
    Object.entries(state.departments).map(([k, v]) => `<option value="${k}"${k === sel ? ' selected' : ''}>${esc(v)}</option>`).join('');
  const roleOptions = sel => Object.entries(state.roles).map(([k, v]) => `<option value="${k}"${k === sel ? ' selected' : ''}>${esc(v)}</option>`).join('');

  // ---------- вкладка «Пользователи»: список, статус «в сети», входы и время работы ----------
  let usersPeriod = '7';
  const canSeeUsers = () => !!state.user && ['admin', 'chief', 'area_head'].includes(state.user.role);

  // Статус: отключённая учётная запись или присутствие в системе (и где человек сейчас находится)
  function statusCell(u, on) {
    if (u.is_active === false) return '<span class="badge off">Отключён</span>';
    if (!on) return '<span class="badge off" title="Сейчас не в системе">не в сети</span>';
    const where = window.activityApi ? window.activityApi.where(on.tab, on.screen_id) : '';
    return `<span class="badge ${on.idle ? 'idle' : 'on'}" title="${on.idle ? 'В сети, но больше 5 минут ничего не нажимал' : 'В сети и работает'}">● ${on.idle ? 'неактивен' : 'в сети'}</span><div class="u-where" title="Где пользователь находится сейчас">${esc(where)}</div>`;
  }

  async function loadUsers() {
    if (!canSeeUsers()) return;
    const isAdmin = state.user.role === 'admin';
    const [res, act, onl] = await Promise.all([
      isAdmin ? api('GET', '/api/users') : Promise.resolve(null),
      api('GET', `/api/activity/stats?period=${usersPeriod}`),
      api('GET', '/api/activity/online'),
    ]);
    if (isAdmin && !res.ok) { usersPane.innerHTML = `<div class="empty">${esc(errText(res))}</div>`; return; }
    const A = window.activityApi || { fmtDur: s => String(s), ago: () => '' };
    const stat = new Map((act.ok ? act.data.users : []).map(u => [u.id, u]));
    const online = new Map((onl.ok ? onl.data.users : []).map(u => [u.id, u]));
    const pendingList = isAdmin ? res.data.filter(u => u.status === 'pending') : [];
    const others = isAdmin
      ? res.data.filter(u => u.status !== 'pending')
      : (act.ok ? act.data.users.map(u => ({ id: u.id, full_name: u.name, role_name: u.role_name, department_name: u.department_name, is_active: true })) : []);
    if (isAdmin) { state.pending = pendingList.length; renderAuthBox(); }
    const periods = [['today', 'Сегодня'], ['7', '7 дней'], ['30', '30 дней']];
    usersPane.innerHTML = `
      ${pendingList.length ? `<div class="pending-box"><h3>Заявки на регистрацию (${pendingList.length})</h3>
        <table class="users-table"><thead><tr><th>Фамилия Имя</th><th>Логин</th><th>Отдел</th><th>Подана</th><th>Роль</th><th></th></tr></thead>
        <tbody>${pendingList.map(u => `<tr data-pid="${u.id}"><td><b>${esc(u.full_name)}</b></td><td>${esc(u.login)}</td><td>${esc(u.department_name || '—')}</td><td>${esc(fmtDate(u.created_at))}</td>
          <td><select title="Выберите роль, которую получит сотрудник после подтверждения">${roleOptions('master')}</select></td>
          <td class="acts"><button type="button" data-act="approve" title="Подтвердить заявку и назначить выбранную роль: сотрудник сможет войти">Подтвердить</button>
          <button type="button" data-act="reject" title="Отклонить заявку: она будет удалена">Отклонить</button></td></tr>`).join('')}</tbody></table></div>` : ''}
      <div class="users-top">
        <span class="grow">${isAdmin ? 'Комментировать и менять настройки могут все, кроме роли «Просмотр». Пользователей создаёт и отключает администратор. ' : ''}В сети сейчас: <b>${online.size}</b>. Нажмите на пользователя, чтобы увидеть статистику по дням.</span>
        <span class="act-periods">${periods.map(([k, v]) => `<button type="button" data-p="${k}" class="act-per${k === usersPeriod ? ' active' : ''}" title="Входы и время работы за период: ${v.toLowerCase()}">${v}</button>`).join('')}</span>
        ${isAdmin ? '<button type="button" id="user-add" title="Создать нового пользователя: фамилия, имя, логин, роль и временный пароль">+ Добавить пользователя</button>' : ''}</div>
      <table class="users-table"><thead><tr><th>Фамилия Имя</th>${isAdmin ? '<th>Логин</th>' : ''}<th>Отдел</th><th>Роль</th>
        <th title="Отключена ли учётная запись, а если активна — в сети ли пользователь сейчас и где он находится">Статус</th>
        <th title="Сколько раз пользователь входил в систему за выбранный период">Входов</th>
        <th title="Время, пока пользователь работал: были действия мышью или клавиатурой. Простой дольше 5 минут не считается">Время работы</th>
        <th title="Время, пока у пользователя была открыта вкладка сайта, включая паузы без действий">В сети</th>
        <th>Последний вход</th>${isAdmin ? '<th></th>' : ''}</tr></thead>
      <tbody>${others.map(u => {
        const s = stat.get(u.id) || {};
        return `
        <tr class="${u.is_active === false ? 'off' : ''} u-row" data-id="${u.id}" title="Нажмите, чтобы посмотреть статистику по дням">
          <td><b>${esc(u.full_name)}</b></td>${isAdmin ? `<td>${esc(u.login)}</td>` : ''}<td>${esc(u.department_name || '—')}</td><td><span class="badge">${esc(u.role_name)}</span></td>
          <td>${statusCell(u, online.get(u.id))}${isAdmin && u.must_change_password ? ' <span class="badge off" title="Пользователь ещё не сменил выданный пароль">пароль не сменён</span>' : ''}</td>
          <td>${s.logins ?? 0}</td><td>${esc(A.fmtDur(s.active_sec || 0))}</td><td>${esc(A.fmtDur(s.online_sec || 0))}</td>
          <td>${esc(fmtDate(s.last_login || u.last_login_at))}</td>
          ${isAdmin ? `<td class="acts">
            <button type="button" data-act="edit" title="Изменить фамилию, имя, роль и статус пользователя">Изменить</button>
            <button type="button" data-act="reset" title="Задать новый временный пароль: пользователь сменит его при входе, текущие сессии завершатся">Сбросить пароль</button>
          </td>` : ''}</tr>`;
      }).join('')}</tbody></table>`;
    usersPane.querySelectorAll('.act-per').forEach(b => b.addEventListener('click', () => { usersPeriod = b.dataset.p; loadUsers(); }));
    const add = usersPane.querySelector('#user-add');
    if (add) add.addEventListener('click', () => showUserForm(null, res.data));
    usersPane.querySelectorAll('tr[data-id]').forEach(tr => {
      const id = Number(tr.dataset.id);
      tr.addEventListener('click', e => { if (!e.target.closest('.acts') && window.openActivityDays) window.openActivityDays(id, usersPeriod); });
      if (!isAdmin) return;
      const u = res.data.find(x => x.id === id);
      tr.querySelector('[data-act="edit"]').addEventListener('click', () => showUserForm(u, res.data));
      tr.querySelector('[data-act="reset"]').addEventListener('click', () => showResetPassword(u));
    });
    usersPane.querySelectorAll('tr[data-pid]').forEach(tr => {
      const id = tr.dataset.pid;
      tr.querySelector('[data-act="approve"]').addEventListener('click', async () => {
        const r = await api('POST', `/api/users/${id}/approve`, { role: tr.querySelector('select').value });
        showToast(r.ok ? 'Заявка подтверждена, пользователь может войти' : errText(r));
        loadUsers();
      });
      tr.querySelector('[data-act="reject"]').addEventListener('click', async () => {
        if (!confirm('Отклонить заявку? Она будет удалена.')) return;
        const r = await api('POST', `/api/users/${id}/reject`);
        showToast(r.ok ? 'Заявка отклонена' : errText(r));
        loadUsers();
      });
    });
  }
  window.refreshUsersTab = loadUsers;

  function showUserForm(user) {
    const isNew = !user;
    const m = openModal(`
      <h3>${isNew ? 'Новый пользователь' : 'Пользователь: ' + esc(user.full_name)}</h3>
      <form>
        <div class="row2"><div><label>Фамилия</label><input name="last_name" value="${esc(user ? user.last_name : '')}" title="Фамилия пользователя: по ней коллеги найдут его через @"></div>
        <div><label>Имя</label><input name="first_name" value="${esc(user ? user.first_name : '')}" title="Имя пользователя"></div></div>
        ${isNew ? '<label>Логин</label><input name="login" autocomplete="off" title="Логин для входа: латиница, цифры, точка, дефис или подчёркивание, 3–40 символов">' : `<label>Логин</label><input value="${esc(user.login)}" disabled title="Логин изменить нельзя">`}
        <label>Отдел</label><select name="department" title="Отдел сотрудника: Производство, ОТО или ИТО. Руководителям можно не указывать">${departmentOptions(user ? user.department || '' : '')}</select>
        <label>Роль</label><select name="role" title="Роль нужна для отображения должности пользователя; права на комментарии и настройки одинаковы у всех, кроме «Просмотр»">${roleOptions(user ? user.role : 'master')}</select>
        ${isNew ? `<label>Временный пароль</label><div class="pwrow"><input name="password" value="${genPassword()}" autocomplete="off" title="Временный пароль: передайте его сотруднику, при первом входе он будет обязан его сменить"><button type="button" data-act="gen" title="Сгенерировать случайный пароль">Новый</button></div><p class="hint">Запишите пароль и передайте сотруднику: после сохранения он больше не показывается.</p>` : `<label class="check"><input type="checkbox" name="is_active" ${user.is_active ? 'checked' : ''}> Учётная запись активна</label>`}
        <div class="auth-err" hidden></div>
        <div class="kpi-modal-actions">
          <button type="button" data-act="cancel" style="background: var(--bg-tertiary); color: var(--fg-primary);" title="Закрыть окно без сохранения">Отмена</button>
          <button type="submit" title="Сохранить пользователя">Сохранить</button>
        </div>
      </form>`);
    m.onCancel();
    const gen = m.q('[data-act="gen"]');
    if (gen) gen.addEventListener('click', () => { m.q('[name="password"]').value = genPassword(); });
    m.q('form').addEventListener('submit', async e => {
      e.preventDefault();
      const f = e.target;
      const res = isNew
        ? await api('POST', '/api/users', { login: f.login.value, last_name: f.last_name.value, first_name: f.first_name.value, role: f.role.value, password: f.password.value, department: f.department.value || null })
        : await api('PUT', `/api/users/${user.id}`, { last_name: f.last_name.value, first_name: f.first_name.value, role: f.role.value, is_active: f.is_active.checked, department: f.department.value || null });
      if (!res.ok) { m.showError(errText(res)); return; }
      m.close();
      showToast(isNew ? 'Пользователь создан' : 'Изменения сохранены');
      loadUsers();
    });
  }

  function showResetPassword(user) {
    const m = openModal(`
      <h3>Сброс пароля: ${esc(user.full_name)}</h3>
      <p class="hint">Все текущие сессии пользователя завершатся. При следующем входе он обязан сменить этот пароль.</p>
      <form>
        <label>Временный пароль</label><div class="pwrow"><input name="password" value="${genPassword()}" autocomplete="off" title="Новый временный пароль"><button type="button" data-act="gen" title="Сгенерировать случайный пароль">Новый</button></div>
        <div class="auth-err" hidden></div>
        <div class="kpi-modal-actions">
          <button type="button" data-act="cancel" style="background: var(--bg-tertiary); color: var(--fg-primary);" title="Закрыть окно без изменений">Отмена</button>
          <button type="submit" title="Сохранить новый временный пароль">Сохранить</button>
        </div>
      </form>`);
    m.onCancel();
    m.q('[data-act="gen"]').addEventListener('click', () => { m.q('[name="password"]').value = genPassword(); });
    m.q('form').addEventListener('submit', async e => {
      e.preventDefault();
      const res = await api('POST', `/api/users/${user.id}/reset-password`, { password: e.target.password.value });
      if (!res.ok) { m.showError(errText(res)); return; }
      m.close();
      showToast('Пароль сброшен. Передайте пользователю временный пароль');
      loadUsers();
    });
  }

  tabUsers.addEventListener('click', loadUsers);
  // Администратору раз в минуту обновляем счётчик заявок на вкладке
  setInterval(async () => {
    if (!state.user || state.user.role !== 'admin' || document.hidden) return;
    const res = await api('GET', '/api/auth/me');
    if (res.ok && res.data.pending_count !== state.pending) { state.pending = res.data.pending_count; renderAuthBox(); }
  }, 60000);
  refresh();
})();
