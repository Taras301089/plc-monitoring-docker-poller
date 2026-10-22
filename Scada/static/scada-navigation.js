/* SCADA is a PMM tab; a live layout exists only while its tab is visible. */
(function (root) {
  'use strict';
  const SCREENS = [
    { id: 'gwm', label: 'GWM', url: '/scada/gwm/gwm-scada.html?embedded=1',
      title: 'Открыть схему GWM: состояния станций, движение, датчики, CARID и причины активных аварий' },
    { id: 'changan', label: 'Changan',
      title: 'Открыть экран Changan: схема станций и привязки сигналов пока не добавлены' },
    { id: 'chery', label: 'Chery',
      title: 'Открыть экран Chery: схема станций и привязки сигналов пока не добавлены' },
    { id: 'main', label: 'Main Layout',
      title: 'Открыть общий план производства: расположение линий и связи между ними пока не добавлены' },
  ];
  const normalizeScreen = id => SCREENS.some(s => s.id === id) ? id : 'gwm';
  if (typeof module !== 'undefined' && module.exports) module.exports = { SCREENS, normalizeScreen };
  if (!root || !root.document) return;

  const doc = root.document;
  const tab = doc.getElementById('tab-scada');
  const panel = doc.getElementById('scada');
  const sidebar = doc.getElementById('scada-screens');
  const content = doc.getElementById('scada-content');
  if (!tab || !panel || !sidebar || !content) return;

  let selected = 'gwm';
  try { selected = normalizeScreen(root.localStorage.getItem('scada-screen')); } catch { /* storage is optional */ }
  let mounted = null;
  let closeTimer;
  let suppressMenu = false;
  const menu = doc.createElement('div');
  menu.id = 'scada-menu';
  menu.className = 'andon-menu';
  menu.setAttribute('role', 'menu');
  menu.setAttribute('aria-label', 'Схемы производства');
  menu.hidden = true;
  doc.body.appendChild(menu);

  function syncContent() {
    const active = panel.classList.contains('active');
    if (!active) {
      // Unloading the document stops its timers and pending browser requests.
      if (mounted !== null) content.replaceChildren();
      mounted = null;
      return;
    }
    if (mounted === selected) return;
    content.replaceChildren();
    mounted = selected;
    const screen = SCREENS.find(s => s.id === selected);
    if (screen.url) {
      const frame = doc.createElement('iframe');
      frame.className = 'scada-frame';
      frame.title = 'GWM — схема станций и текущие данные ПЛК';
      frame.src = screen.url;
      content.appendChild(frame);
    } else {
      const state = doc.createElement('section');
      state.className = 'scada-pending';
      const heading = doc.createElement('h2');
      heading.textContent = screen.label;
      const hint = doc.createElement('p');
      hint.textContent = selected === 'main'
        ? 'Общий план пока не добавлен. Нужны расположение линий GWM, Changan и Chery и связи между ними.'
        : 'Схема пока не добавлена. Нужны расположение станций и привязки сигналов ПЛК для этой линии.';
      state.append(heading, hint);
      content.appendChild(state);
    }
  }

  function markSelected() {
    [menu, sidebar].forEach(container => container.querySelectorAll('[data-screen]').forEach(button => {
      const current = button.dataset.screen === selected;
      button.classList.toggle('active', current);
      if (current) button.setAttribute('aria-current', 'page');
      else button.removeAttribute('aria-current');
    }));
  }

  function closeMenu(delay = 0) {
    clearTimeout(closeTimer);
    const close = () => {
      if (root.reorder && root.reorder.busy()) return;
      menu.hidden = true;
      tab.setAttribute('aria-expanded', 'false');
    };
    if (delay) closeTimer = setTimeout(close, delay); else close();
  }

  function openMenu(focus = false) {
    clearTimeout(closeTimer);
    menu.hidden = false;
    const rect = tab.getBoundingClientRect();
    const width = menu.getBoundingClientRect().width;
    menu.style.left = Math.max(8, Math.min(rect.left, root.innerWidth - width - 8)) + 'px';
    menu.style.top = rect.bottom + 'px';
    tab.setAttribute('aria-expanded', 'true');
    if (focus) (menu.querySelector('.active') || menu.firstElementChild).focus();
  }

  function selectScreen(id) {
    selected = normalizeScreen(id);
    try { root.localStorage.setItem('scada-screen', selected); } catch { /* storage is optional */ }
    markSelected();
    if (!panel.classList.contains('active')) {
      suppressMenu = true;
      tab.click();
      suppressMenu = false;
    }
    syncContent();
    closeMenu();
  }

  SCREENS.forEach(screen => {
    [menu, sidebar].forEach(container => {
      const button = doc.createElement('button');
      button.type = 'button';
      button.className = 'scada-screen';
      button.dataset.screen = screen.id;
      button.textContent = screen.label;
      button.title = screen.title;
      if (container === menu) button.setAttribute('role', 'menuitem');
      button.addEventListener('click', () => {
        selectScreen(screen.id);
        if (container === menu) sidebar.querySelector(`[data-screen="${selected}"]`).focus();
      });
      container.appendChild(button);
    });
  });

  const RO = root.reorder;
  if (RO) {
    const key = 'order-scada-screens';
    const apply = order => {
      if (RO.busy()) return;
      [menu, sidebar].forEach(container => RO.applyToDom(container, ':scope > [data-screen]', order, el => el.dataset.screen));
    };
    apply(RO.get(key));
    RO.watch(key, apply);
    [menu, sidebar].forEach(container => RO.attach(container, {
      key, itemSelector: ':scope > [data-screen]', idOf: el => el.dataset.screen,
      onChange: ids => apply(ids),
    }));
  }

  tab.addEventListener('mouseenter', () => openMenu());
  tab.addEventListener('mouseleave', () => closeMenu(250));
  tab.addEventListener('click', () => { syncContent(); if (!suppressMenu) openMenu(); });
  tab.addEventListener('keydown', event => {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      openMenu(true);
    }
  });
  menu.addEventListener('mouseenter', () => clearTimeout(closeTimer));
  menu.addEventListener('mouseleave', () => closeMenu(250));
  menu.addEventListener('keydown', event => {
    const buttons = [...menu.querySelectorAll('[role="menuitem"]')];
    const index = buttons.indexOf(doc.activeElement);
    let target;
    if (event.key === 'ArrowDown') target = (index + 1) % buttons.length;
    else if (event.key === 'ArrowUp') target = (index - 1 + buttons.length) % buttons.length;
    else if (event.key === 'Home') target = 0;
    else if (event.key === 'End') target = buttons.length - 1;
    if (target !== undefined) { event.preventDefault(); buttons[target].focus(); }
    if (event.key === 'Tab') closeMenu();
  });
  doc.addEventListener('keydown', event => {
    if (event.key === 'Escape' && !menu.hidden) {
      closeMenu();
      tab.focus();
    }
  });
  doc.addEventListener('click', event => {
    if (!menu.contains(event.target) && !tab.contains(event.target)) closeMenu();
  });
  root.addEventListener('resize', () => { if (!menu.hidden) openMenu(); });
  // PMM's existing tab handler also handles hash/last-view restores and role changes.
  doc.querySelectorAll('.tab[data-tab]').forEach(button => button.addEventListener('click', syncContent));
  new MutationObserver(syncContent).observe(panel, { attributes: true, attributeFilter: ['class'] });
  markSelected();
  syncContent();
})(typeof window === 'undefined' ? null : window);
