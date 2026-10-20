// Словарь названий интерфейса: подписи и подсказки, которые администратор меняет в окне «Названия» (вкладка «Справочники»).
// Стандартные названия записаны в коде и в разметке; пока в словаре нет своего названия (или сервер не отвечает), показываются они.
(() => {
  'use strict';

  const REFRESH_MS = 10000;     // правка администратора видна на всех экранах не позднее чем через 10 секунд (и табло, и архив)
  // тайм-аут запроса 6 секунд; при ошибке остаются прежние или стандартные названия
  const hasDom = typeof document !== 'undefined';

  let labels = {};              // key -> { text, title }: только изменённые
  let signature = '{}';

  // название по ключу: своё или стандартное def
  const uiLabel = (key, def) => (labels[key] && labels[key].text) || def;
  const uiTitle = (key, def) => (labels[key] && labels[key].title) || def;

  // Применяет словарь к разметке: data-ui-label="ключ" меняет текст, data-ui-title="ключ" подсказку; data-ui-prefix="📉 " сохраняет значок перед текстом.
  // Стандартные текст и подсказка запоминаются в data-ui-def / data-ui-title-def при первом проходе, чтобы сброс возвращал их.
  function applyLabels(root) {
    if (!hasDom) return;
    (root || document).querySelectorAll('[data-ui-label]').forEach(el => {
      const prefix = el.dataset.uiPrefix || '';
      if (el.dataset.uiDef === undefined) el.dataset.uiDef = prefix && el.textContent.startsWith(prefix) ? el.textContent.slice(prefix.length) : el.textContent;
      const want = prefix + uiLabel(el.dataset.uiLabel, el.dataset.uiDef);
      if (el.textContent !== want) el.textContent = want;
    });
    (root || document).querySelectorAll('[data-ui-title]').forEach(el => {
      if (el.dataset.uiTitleDef === undefined) el.dataset.uiTitleDef = el.getAttribute('title') || '';
      const want = uiTitle(el.dataset.uiTitle, el.dataset.uiTitleDef);
      if (el.getAttribute('title') !== want) el.setAttribute('title', want);
    });
  }

  // Перечитывает словарь; при изменении обновляет разметку и сообщает экранам, которые рисуют подписи сами (событие ui-labels-changed)
  async function loadLabels() {
    try {
      const r = await fetch('/api/ui-labels', { signal: AbortSignal.timeout(6000), cache: 'no-store' });
      if (!r.ok) return;
      const next = (await r.json()).labels || {};
      const sig = JSON.stringify(next);
      if (sig === signature) return;
      labels = next; signature = sig;
      applyLabels(document);
      document.dispatchEvent(new CustomEvent('ui-labels-changed'));
    } catch { /* нет связи или тайм-аут: остаются прежние или стандартные названия */ }
  }

  window.uiLabel = uiLabel;
  window.uiTitle = uiTitle;
  window.applyLabels = applyLabels;
  window.loadLabels = loadLabels;

  if (!hasDom) { if (typeof module !== 'undefined' && module.exports) module.exports = { uiLabel, uiTitle }; return; }
  loadLabels();
  setInterval(() => { if (!document.hidden) loadLabels(); }, REFRESH_MS);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) loadLabels(); });
  // при открытии вкладки словарь перечитывается сразу (клик по вкладке), а разметка, нарисованная позже, получает названия
  document.addEventListener('click', e => { if (e.target.closest && e.target.closest('.tab[data-tab]')) { loadLabels(); setTimeout(() => applyLabels(document), 0); } }, true);
  document.addEventListener('DOMContentLoaded', () => applyLabels(document));
  // разметка, которую экраны рисуют сами (таблица Andon, окна), получает названия сразу после появления
  let timer = 0;
  new MutationObserver(() => { clearTimeout(timer); timer = setTimeout(() => applyLabels(document), 150); })
    .observe(document.documentElement, { childList: true, subtree: true });
})();
