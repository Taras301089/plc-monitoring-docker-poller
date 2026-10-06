// Вставить в mcp__Claude_Browser__javascript_tool целиком. Возвращает JSON: размеры окна, пересечения блоков (больше 6px), обрезанный текст (у заголовков допуск 6px на ручки ширины), горизонтальная прокрутка.
(() => {
  const pane = document.querySelector('.tab-content.active') || document.body;
  const shown = e => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e); return r.width > 1 && r.height > 1 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const name = e => (e.id ? '#' + e.id : '') + (e.className && typeof e.className === 'string' ? '.' + e.className.trim().split(/\s+/).slice(0, 2).join('.') : '') || e.tagName.toLowerCase();
  // ключевые блоки: шапка Andon, плитки, названия, часы, кнопки, фильтры, карточки графиков
  const sel = '.andon-top, .andon-lines, .andon-day, .andon-side .tile, .andon-h, .andon-clockbox, .andon-state, .kpi-wrap, ' +
    '.ch-bar, .ch-tiles, .ch-card, .rep-pane, nav.tabs, #auth-box';
  const blocks = [...document.querySelectorAll(sel)].filter(e => shown(e) && (pane.contains(e) || e.matches('nav.tabs, #auth-box')));
  const overlaps = [];
  for (let i = 0; i < blocks.length; i++) for (let j = i + 1; j < blocks.length; j++) {
    const a = blocks[i], b = blocks[j];
    if (a.contains(b) || b.contains(a)) continue;
    const r = a.getBoundingClientRect(), q = b.getBoundingClientRect();
    const w = Math.min(r.right, q.right) - Math.max(r.left, q.left), h = Math.min(r.bottom, q.bottom) - Math.max(r.top, q.top);
    if (w > 6 && h > 6) overlaps.push([name(a), name(b), Math.round(w) + 'x' + Math.round(h)]);
  }
  // текст, который не помещается (кроме ручек изменения ширины и областей с многоточием)
  const clipped = [...pane.querySelectorAll('th, td, button, .tile b, .andon-h, label')]
    .filter(e => shown(e) && !e.closest('.why') && e.scrollWidth > e.clientWidth + (e.tagName === "TH" ? 6 : 2) && getComputedStyle(e).overflow !== 'auto')
    .slice(0, 12).map(e => name(e) + ' «' + e.textContent.trim().slice(0, 24) + '» ' + e.scrollWidth + '>' + e.clientWidth);
  return JSON.stringify({ w: innerWidth, h: innerHeight, overlaps, clipped, hscroll: document.documentElement.scrollWidth > innerWidth + 1 });
})()
