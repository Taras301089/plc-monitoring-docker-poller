// Единый источник времени: часы ПК с сервисом + KPI_UTC_OFFSET_HOURS, не часы и не часовой пояс браузера (телевизор Xiaomi часто UTC+8).
// Все даты и время на экранах выводить через plantClock.
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
    // текущее время сервиса в мс (для «сколько прошло»: часы устройства не используются)
    now: unixMs,
    // метка времени с сервера (ISO) -> «дд.мм чч:мм» (с годом при withYear) по времени завода
    fmt(ts, withYear) {
      const t = new Date(ts).getTime();
      if (!ts || Number.isNaN(t)) return '—';
      const d = new Date(t + offsetHours * 3600000);
      return `${pad(d.getUTCDate())}.${pad(d.getUTCMonth() + 1)}${withYear ? '.' + d.getUTCFullYear() : ''} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
    },
    dateText() {
      const p = parts();
      return `${pad(p.da)}.${pad(p.mo)}.${p.y}`;
    },
    // сегодняшняя дата по времени завода в виде ГГГГ-ММ-ДД (для выбора дня и запросов к серверу)
    isoDate() {
      const p = parts();
      return `${p.y}-${pad(p.mo)}-${pad(p.da)}`;
    },
    // минуты от начала суток по времени завода (для сравнения с интервалами смены)
    minutes() {
      const p = parts();
      return p.h * 60 + p.mi + p.s / 60;
    },
    timeText() {
      const p = parts();
      return `${pad(p.h)}:${pad(p.mi)}`;
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
