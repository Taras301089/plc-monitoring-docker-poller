/* One theme preference for PMM, help and standalone/embedded SCADA screens. */
(function (window, document) {
  'use strict';
  if (window.PMMTheme) return;
  const valid = value => value === 'dark' || value === 'light';
  const root = document.documentElement;
  const media = typeof window.matchMedia === 'function'
    ? window.matchMedia('(prefers-color-scheme: dark)') : null;
  const systemTheme = () => media && media.matches ? 'dark' : 'light';
  let preference = null;
  try {
    const saved = window.localStorage.getItem('theme');
    if (valid(saved)) preference = saved;
  } catch (_) { /* Private/locked browsers still get a working switch. */ }

  let parent = null;
  try {
    if (window.parent !== window && window.parent.document) parent = window.parent;
  } catch (_) { /* Never access a cross-origin frame host. */ }

  function updateButton() {
    const button = document.getElementById('theme-toggle');
    if (!button) return;
    const dark = root.getAttribute('data-theme') === 'dark';
    const label = dark ? 'Светлая тема' : 'Тёмная тема';
    button.textContent = button.getAttribute('data-theme-label') === 'text'
      ? label : dark ? '☀️' : '🌙';
    button.setAttribute('aria-label', label);
    button.title = 'Переключить светлую и тёмную тему одновременно на экранах PMM и SCADA';
  }

  function apply(theme) {
    if (!valid(theme)) return;
    root.setAttribute('data-theme', theme);
    root.style.colorScheme = theme;
    updateButton();
  }

  function set(theme) {
    if (!valid(theme)) return;
    preference = theme;
    apply(theme);
    // A same-origin iframe shares the host even when storage is unavailable.
    if (parent && parent.PMMTheme) parent.PMMTheme.set(theme);
    else {
      try { window.localStorage.setItem('theme', theme); } catch (_) {}
    }
  }

  window.PMMTheme = {
    get: () => root.getAttribute('data-theme'),
    set,
    toggle: () => set(root.getAttribute('data-theme') === 'dark' ? 'light' : 'dark')
  };
  const hostTheme = parent && parent.document.documentElement.getAttribute('data-theme');
  apply(valid(hostTheme) ? hostTheme : preference || systemTheme());

  window.addEventListener('storage', event => {
    if (event.key !== 'theme' && event.key !== null) return;
    try {
      if (event.storageArea && event.storageArea !== window.localStorage) return;
    } catch (_) {}
    preference = valid(event.newValue) ? event.newValue : null;
    apply(preference || systemTheme());
  });

  if (parent && typeof window.MutationObserver === 'function') {
    const observer = new window.MutationObserver(() => {
      const theme = parent.document.documentElement.getAttribute('data-theme');
      if (valid(theme)) apply(theme);
    });
    observer.observe(parent.document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
    window.addEventListener('pagehide', () => observer.disconnect());
    window.addEventListener('pageshow', () => {
      apply(parent.document.documentElement.getAttribute('data-theme'));
      observer.observe(parent.document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
    });
  }

  function followSystem() {
    if (preference === null) {
      const theme = parent && parent.document.documentElement.getAttribute('data-theme');
      apply(valid(theme) ? theme : systemTheme());
    }
  }
  if (media && media.addEventListener) media.addEventListener('change', followSystem);
  else if (media && media.addListener) media.addListener(followSystem);

  function bind() {
    updateButton();
    const button = document.getElementById('theme-toggle');
    if (button) button.addEventListener('click', window.PMMTheme.toggle);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bind, { once: true });
  else bind();
})(window, document);
