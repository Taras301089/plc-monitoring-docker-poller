// Часы экранов Andon: источник — время сервиса на ПК, не часовой пояс браузера (телевизор Xiaomi часто UTC+8).
(() => {
  'use strict';

  const pad = n => String(n).padStart(2, '0');
  let offsetHours = 5;
  let originUnixMs = Date.now();
  let originPerf = performance.now();

  function unixMs() {
    return originUnixMs + (performance.now() - originPerf);
  }

  function parts() {
    const d = new Date(unixMs() + offsetHours * 3600000);
    return {
      y: d.getUTCFullYear(),
      mo: d.getUTCMonth() + 1,
      da: d.getUTCDate(),
      h: d.getUTCHours(),
      mi: d.getUTCMinutes(),
      s: d.getUTCSeconds(),
    };
  }

  window.plantClock = {
    dateText() {
      const p = parts();
      return `${pad(p.da)}.${pad(p.mo)}.${p.y}`;
    },
    timeText() {
      const p = parts();
      return `${pad(p.h)}:${pad(p.mi)}:${pad(p.s)}`;
    },
  };

  async function sync() {
    try {
      const r = await fetch('/api/time');
      if (!r.ok) return;
      const data = await r.json();
      offsetHours = Number(data.offset_hours);
      originUnixMs = Number(data.unix_ms);
      originPerf = performance.now();
    } catch { /* следующий цикл sync повторит запрос */ }
  }

  sync();
  setInterval(sync, 30000);
})();
