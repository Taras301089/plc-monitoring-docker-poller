// Изменение ширины столбцов перетаскиванием границы в заголовке (как в Excel); ширины запоминаются
(() => {
  'use strict';
  const MIN = 3;   // минимальная ширина столбца, % от таблицы
  const GRIP_TITLE = 'Потяните, чтобы изменить ширину столбца; двойной щелчок возвращает ширины столбцов по умолчанию';

  const store = {
    get(key) { try { return JSON.parse(localStorage.getItem('colw:' + key)); } catch { return null; } },
    set(key, w) { try { if (w) localStorage.setItem('colw:' + key, JSON.stringify(w)); else localStorage.removeItem('colw:' + key); } catch { /* без сохранения */ } },
  };
  const apply = (cols, w) => cols.forEach((c, i) => { c.style.width = w[i] + '%'; });

  window.attachColWidths = (table, key) => {
    const cols = [...table.querySelectorAll(':scope > colgroup > col')];
    const ths = [...table.querySelectorAll(':scope > thead th')];
    if (!cols.length || ths.length !== cols.length) return;
    const defaults = cols.map(c => parseFloat(c.style.width));
    const saved = store.get(key);
    if (Array.isArray(saved) && saved.length === cols.length && saved.every(v => Number.isFinite(v) && v >= MIN)) apply(cols, saved);

    ths.slice(0, -1).forEach((th, i) => {
      const grip = document.createElement('span');
      grip.className = 'col-grip';
      grip.title = GRIP_TITLE;
      th.appendChild(grip);
      grip.addEventListener('pointerdown', e => {
        e.preventDefault();
        grip.setPointerCapture(e.pointerId);
        grip.classList.add('drag');
        const start = e.clientX;
        const w0 = cols.map(c => parseFloat(c.style.width));
        const total = table.getBoundingClientRect().width;
        const move = ev => {
          const d = Math.max(MIN - w0[i], Math.min(w0[i + 1] - MIN, (ev.clientX - start) / total * 100));
          const w = w0.slice();
          w[i] = +(w0[i] + d).toFixed(2);
          w[i + 1] = +(w0[i + 1] - d).toFixed(2);
          apply(cols, w);
        };
        const up = () => {
          grip.classList.remove('drag');
          grip.removeEventListener('pointermove', move);
          grip.removeEventListener('pointerup', up);
          grip.removeEventListener('pointercancel', up);
          store.set(key, cols.map(c => parseFloat(c.style.width)));
        };
        grip.addEventListener('pointermove', move);
        grip.addEventListener('pointerup', up);
        grip.addEventListener('pointercancel', up);
      });
      grip.addEventListener('dblclick', () => { apply(cols, defaults); store.set(key, null); });
      grip.addEventListener('click', e => e.stopPropagation());
    });
  };
})();
