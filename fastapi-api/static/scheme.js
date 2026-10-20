// Справка, раздел «Схема проекта»: рисует схемы Mermaid из кода рядом (блок с id из data-scheme; тот же текст копируется кнопкой).
// Библиотека лежит рядом (static/vendor/mermaid.min.js, лицензия MIT), интернет не нужен.
(function () {
  const boxes = Array.from(document.querySelectorAll('.scheme-box[data-scheme]'));
  if (!boxes.length) return;
  let n = 0;
  function fail(box, msg) { box.textContent = msg + ' Код схемы ниже можно вставить на сайте mermaid.live.'; }
  async function drawAll() {
    if (!window.mermaid) { boxes.forEach(b => fail(b, 'Схему не удалось нарисовать: библиотека не загрузилась.')); return; }
    const dark = document.documentElement.getAttribute('data-theme') === 'dark';
    window.mermaid.initialize({ startOnLoad: false, securityLevel: 'strict', theme: dark ? 'dark' : 'default', flowchart: { htmlLabels: true, useMaxWidth: false, curve: 'basis' } });
    for (const box of boxes) {
      const src = document.getElementById(box.dataset.scheme);
      if (!src) continue;
      try {
        const out = await window.mermaid.render('scheme-svg-' + (++n), src.textContent.trim());
        box.innerHTML = out.svg;
      } catch (e) {
        fail(box, 'Схему не удалось нарисовать (' + String((e && e.message) || e).slice(0, 80) + ').');
      }
    }
  }
  window.addEventListener('load', drawAll);
  // при смене темы кнопкой 🌙/☀️ схемы перерисовываются в цветах новой темы
  new MutationObserver(() => { if (window.mermaid) drawAll(); }).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
})();
