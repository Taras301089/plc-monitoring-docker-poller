// Активность пользователей: сигнал «я здесь» от браузера и окно статистики по дням.
// Список «в сети», входы и время работы показывает вкладка «Пользователи» (auth.js).
(() => {
  'use strict';

  const BEAT_MS = 30000;
  const IDLE_MS = 300000;
  const usersPane = document.querySelector('#users');
  let lastInput = Date.now();

  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const me = () => { const st = window.authState && window.authState(); return st && st.user ? st.user : null; };

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
    } catch { return { ok: false, data: null }; }
  }

  // Любое действие пользователя отмечаем: по нему определяется «активен / неактивен»
  ['mousemove', 'keydown', 'mousedown', 'scroll', 'touchstart'].forEach(ev =>
    window.addEventListener(ev, () => { lastInput = Date.now(); }, { passive: true, capture: true }));

  const fmtDur = sec => {
    const m = Math.round(sec / 60);
    if (m < 1) return sec > 0 ? '< 1 мин' : '—';
    return m < 60 ? `${m} мин` : `${Math.floor(m / 60)} ч ${m % 60} мин`;
  };

  // Где пользователь находится: вкладка и, для Andon, экран
  const where = (tabName, screenId) => {
    const t = document.querySelector(`.tab[data-tab="${tabName}"]`);
    let s = t ? t.textContent.replace(/\s*[▾(].*$/, '').replace(/^\S+\s/, '').trim() : (tabName || '—');
    if (tabName === 'andon' && screenId && typeof andonScreens !== 'undefined' && typeof andonPath === 'function') {
      const sc = andonScreens.find(x => x.id === screenId);
      if (sc) { const p = andonPath(sc); s += ` › ${p.group} › ${p.label}`; }
    }
    return s;
  };
  window.activityApi = { fmtDur, where };

  async function beat() {
    if (!me() || document.hidden) return;
    const active = Date.now() - lastInput < IDLE_MS;
    const cur = document.querySelector('.tab.active[data-tab]');
    const name = cur ? cur.dataset.tab : null;
    await call('POST', '/api/activity/beat', { active, tab: name, screen: name === 'andon' && typeof andonScreenId === 'number' ? andonScreenId : null });
  }

  // Окно «по дням»: своя статистика (userId = null) или выбранного пользователя (администратор и начальники)
  async function openDays(userId, per = '7') {
    const q = `period=${per}${userId ? `&user_id=${userId}` : ''}`;
    const r = await call('GET', `/api/activity/days?${q}`);
    if (!r.ok) return;
    document.querySelectorAll('.act-modal').forEach(x => x.remove());
    const d = r.data;
    const m = document.createElement('div');
    m.className = 'kpi-modal act-modal';
    m.innerHTML = `<div class="kpi-modal-box dt-box" role="dialog" aria-label="Статистика">
      <h3>${userId ? 'Статистика: ' + esc(d.name) : 'Моя статистика'}</h3>
      <div class="act-periods" style="margin-bottom:10px">${[['today', 'Сегодня'], ['7', '7 дней'], ['30', '30 дней']].map(([k, v]) =>
        `<button type="button" data-p="${k}" class="act-per${k === d.period ? ' active' : ''}" title="Показать статистику за период: ${v.toLowerCase()}">${v}</button>`).join('')}</div>
      <table class="man-log"><thead><tr><th>Дата</th><th>Входов</th><th title="Время с действиями мышью или клавиатурой">Время работы</th><th title="Время с открытой вкладкой, включая паузы">В сети</th></tr></thead>
      <tbody>${d.days.map(x => `<tr><td>${esc(new Date(x.day + 'T00:00:00').toLocaleDateString('ru-RU', { weekday: 'short', day: '2-digit', month: '2-digit' }))}</td><td>${x.logins}</td><td>${esc(fmtDur(x.active_sec))}</td><td>${esc(fmtDur(x.online_sec))}</td></tr>`).join('') || '<tr><td colspan="4">За этот период данных пока нет</td></tr>'}</tbody>
      <tfoot><tr><td><b>Итого</b></td><td><b>${d.total.logins}</b></td><td><b>${esc(fmtDur(d.total.active_sec))}</b></td><td><b>${esc(fmtDur(d.total.online_sec))}</b></td></tr></tfoot></table>
      <p class="dt-hint" style="margin:0 0 10px">Время считается по сигналам открытой вкладки. Точность около минуты.</p>
      <div class="kpi-modal-actions"><button type="button" class="dt-sec" data-act="close" title="Закрыть окно">Закрыть</button></div></div>`;
    document.body.appendChild(m);
    const close = () => m.remove();
    m.addEventListener('mousedown', e => { if (e.target === m) close(); });
    m.querySelector('[data-act="close"]').addEventListener('click', close);
    m.querySelectorAll('.act-per').forEach(b => b.addEventListener('click', () => openDays(userId, b.dataset.p)));
  }
  window.openMyStats = () => openDays(null, '7');
  window.openActivityDays = openDays;

  const prev = window.onAuthChanged;
  window.onAuthChanged = () => { if (prev) prev(); beat(); };
  document.addEventListener('visibilitychange', () => { if (!document.hidden) beat(); });
  setInterval(() => {
    beat();
    // пока открыта вкладка «Пользователи», список в сети обновляем сам
    if (usersPane.classList.contains('active') && !document.querySelector('.act-modal') && window.refreshUsersTab) window.refreshUsersTab();
  }, BEAT_MS);
  beat();
})();
