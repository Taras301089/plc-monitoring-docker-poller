// Шапка справки повторяет шапку основной страницы: колокольчик и текущий пользователь (только показ, действия — на главной)
(() => {
  'use strict';
  const box = document.querySelector('#ref-user');
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  async function init() {
    let d = null;
    try { const r = await fetch('/api/auth/me', { credentials: 'same-origin' }); if (r.ok) d = await r.json(); } catch { /* нет связи */ }
    const u = d && d.user;
    if (!u) {
      box.innerHTML = `<a class="auth-btn" href="index.html" style="text-decoration:none" title="Перейти на главную страницу, чтобы войти или зарегистрироваться">🔑 Войти или зарегистрироваться</a>`;
      return;
    }
    let unread = 0;
    try { const r = await fetch('/api/kpi/notifications?limit=1', { credentials: 'same-origin' }); if (r.ok) unread = (await r.json()).unread || 0; } catch { /* без счётчика */ }
    box.innerHTML = `
      <div class="notif-wrap"><a class="notif-btn" href="index.html" style="text-decoration:none;color:inherit" title="Уведомления: упоминания, ответы на ваши комментарии и запросы к ОТО (откроются на главной странице)">🔔<b class="notif-badge"${unread ? '' : ' hidden'}>${unread > 99 ? '99+' : unread}</b></a></div>
      <a class="auth-btn" href="index.html" style="text-decoration:none" title="Вы вошли в систему. Меню пользователя, смена пароля и выход — на главной странице"><span>${esc(u.last_name + ' ' + u.first_name.charAt(0) + '.')}</span><small>${esc(u.role_name)}</small></a>`;
    const canSee = ['admin', 'chief', 'area_head'].includes(u.role);
    document.querySelector('#tab-users').hidden = !canSee;
    document.querySelector('#tab-dictionary').hidden = !canSee;
  }
  init();
})();
