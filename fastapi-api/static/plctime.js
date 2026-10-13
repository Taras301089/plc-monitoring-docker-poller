// Время ПЛК и расхождение с сервером PMM в списке ПЛК («12:41:05 · +5м 28с»). Только показ: PMM время ПЛК не меняет.
// Данные приходят из GET /api/plcs/time (сервер читает узел i=2258 раз в минуту); текст расхождения строит и сервер (plctime.py), оба совпадают.
(function (root) {
  'use strict';

  const LIMIT_SEC = 5;   // расхождение больше этого значения показывается красным
  const TITLE = 'Время ПЛК и расхождение с сервером PMM (плюс — ПЛК впереди). Красным, если расхождение больше 5 секунд. Синхронизация времени настраивается в TIA Portal (NTP) в каждом процессоре';

  // «+5м 28с», «-1м 47с», «+24с», «0с»; нет значения — «—»
  function fmtDrift(sec) {
    if (sec == null || !Number.isFinite(sec)) return '—';
    const s = Math.trunc(sec);
    if (s === 0) return '0с';
    const m = Math.floor(Math.abs(s) / 60), r = Math.abs(s) % 60;
    const sign = s > 0 ? '+' : '-';
    return m ? `${sign}${m}м ${r}с` : `${sign}${r}с`;
  }

  function isDrifted(sec) {
    return sec != null && Number.isFinite(sec) && Math.abs(sec) > LIMIT_SEC;
  }

  // Фрагмент для строки списка. Время ПЛК показывается с поправкой на то, сколько прошло с чтения (часы сервиса через plantClock, не часы браузера).
  // Нет связи или нет значения: «—» без цвета.
  function fragment(entry, fmtTime, nowMs) {
    const fmt = fmtTime || (ms => root.plantClock.fmtTime(ms));
    const now = nowMs || (() => root.plantClock.now());
    const plcMs = entry && entry.ok && entry.plc_time ? Date.parse(entry.plc_time) : NaN;
    if (!Number.isFinite(plcMs) || !Number.isFinite(entry.drift_sec)) {
      return `<span class="plc-time" title="${TITLE}">—</span>`;
    }
    const readMs = Date.parse(entry.read_at);
    const elapsed = Number.isFinite(readMs) ? Math.max(0, now() - readMs) : 0;
    const cls = isDrifted(entry.drift_sec) ? 'plc-time bad' : 'plc-time';
    return `<span class="${cls}" title="${TITLE}">${fmt(plcMs + elapsed)} · ${fmtDrift(entry.drift_sec)}</span>`;
  }

  const api = { fmtDrift, isDrifted, fragment, TITLE, LIMIT_SEC };
  root.plcTime = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
