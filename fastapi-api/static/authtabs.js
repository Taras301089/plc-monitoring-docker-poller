// Вкладки и вход:
// 1) «Переменные ПЛК», «Переменные БД» и «Пользователи» видны только администратору; если открытая вкладка стала недоступной
//    (нет входа, другая роль), открывается Andon;
// 2) после обновления страницы вкладки, зависящие от пользователя (Пользователи, Справочники, Отчёты), открываются раньше,
//    чем сервер сообщил, кто вошёл, и остаются пустыми, поэтому, когда это стало известно, открытая вкладка загружается заново.
(() => {
  'use strict';
  const ADMIN_ONLY = ['browser', 'saved', 'users'];
  const NEEDS_USER = ['users', 'dictionary', 'reports'];   // «Отчёты»: после входа админу добавляется блок автосохранения
  let known;   // последний известный пользователь (id или null)
  const prev = window.onAuthChanged;
  window.onAuthChanged = () => {
    if (prev) prev();
    const st = window.authState && window.authState();
    const admin = !!(st && st.user && st.user.role === 'admin');
    ADMIN_ONLY.forEach(t => { const b = document.querySelector(`.tab[data-tab="${t}"]`); if (b) b.hidden = !admin; });
    let tab = document.querySelector('.tab.active[data-tab]');
    if (tab && tab.hidden) {
      const home = document.querySelector('.tab[data-tab="andon"]');
      if (home) home.click();
      tab = document.querySelector('.tab.active[data-tab]');
    }
    const uid = st && st.user ? st.user.id : null;
    if (uid === known) return;
    known = uid;
    if (tab && NEEDS_USER.includes(tab.dataset.tab) && !tab.hidden) tab.click();
  };
})();
