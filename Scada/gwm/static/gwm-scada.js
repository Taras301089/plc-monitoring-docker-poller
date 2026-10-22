/* GWM: read-only live view. The server resolves PLC-local indexes and OPC nodes.
   Missing/bad reads never fall back to demo values or a previous good snapshot. */
(function (root) {
  'use strict';
  const SENSORS = ['OutLet', 'End', 'Slow', 'Start', 'InLet'];
  const MODES = {
    1: { className: 'error', label: 'Error', short: 'ERROR' },
    2: { className: 'manual', label: 'Manual Mode', short: 'MANUAL' },
    3: { className: 'auto', label: 'Auto Mode', short: 'AUTO' },
    4: { className: 'undefined', label: 'Не уточнено', short: 'КОД 4' }
  };
  const TOP = Array.from({ length: 13 }, (_, i) => (i < 9 ? 'UB' : 'MB') + (i + 1) * 10);
  const BOTTOM = Array.from({ length: 14 }, (_, i) => 'MB' + (270 - i * 10));
  const STATIONS = [...TOP, ...BOTTOM];
  function bool(value) {
    if (value === true || value === 1 || value === '1' || value === 'true' || value === 'TRUE') return true;
    if (value === false || value === 0 || value === '0' || value === 'false' || value === 'FALSE') return false;
    return null;
  }
  function mode(value) {
    const code = typeof value === 'number' && Number.isFinite(value) ? value :
      typeof value === 'string' && /^\d+$/.test(value.trim()) ? Number(value) : null;
    return MODES[code] || { className: 'missing', label: code === null ? 'Нет данных' : 'Неизвестный код ' + code, short: code === null ? '—' : 'КОД ' + code };
  }
  function motion(fwd, bwd, reverse = false) {
    fwd = bool(fwd); bwd = bool(bwd);
    if (fwd === null || bwd === null) return { key: 'unknown', label: 'Нет данных', arrow: '—', short: '—' };
    if (fwd && bwd) return { key: 'conflict', label: 'Конфликт направлений', arrow: '↔', short: '1 / 1' };
    if (!fwd && !bwd) return { key: 'stopped', label: 'Останов', arrow: '−', short: 'СТОП' };
    return { key: fwd ? 'forward' : 'backward', label: fwd ? 'Вперёд · RUN_FWD' : 'Назад · RUN_BWD', arrow: fwd !== reverse ? '→' : '←', short: fwd ? 'FWD' : 'BWD' };
  }
  const list = value => Array.isArray(value) ? value.filter(item => item && typeof item === 'object') : [];
  function normalize(raw) {
    raw = raw && typeof raw === 'object' ? raw : {};
    return {
      color: typeof raw.color === 'number' || typeof raw.color === 'string' ? raw.color : null,
      RUN_FWD: bool(raw.RUN_FWD), RUN_BWD: bool(raw.RUN_BWD),
      sensors: Object.fromEntries(SENSORS.map(name => [name, bool((raw.sensors || {})[name])])),
      plc: raw.plc || {}, binding: raw.binding || {},
      carid: typeof raw.carid === 'string' ? raw.carid : null,
      carid_status: raw.carid_status || 'unavailable',
      skid_present: bool(raw.skid_present),
      causes: list(raw.causes), alarms: list(raw.alarms),
      diagnostics_complete: raw.diagnostics_complete === true,
      alarm_mapping_complete: raw.alarm_mapping_complete === true,
      cause_verified: raw.cause_verified === true,
      details_error: typeof raw.details_error === 'string' ? raw.details_error : '',
      details_loaded: raw.details_loaded === true || Object.hasOwn(raw, 'diagnostics_complete')
    };
  }
  function normalizeSnapshot(snapshot) {
    return Object.fromEntries(STATIONS.map(id => [id, normalize(snapshot && snapshot[id])]));
  }
  function alarmSummary(station) {
    if (!station.details_loaded) return 'Чтение аварий…';
    const active = [...station.causes, ...station.alarms];
    if (active.length) return station.cause_verified && Number(station.color) === 1 && active.some(item => item.is_cause === true) ? 'Активные причины Error' : 'Активные сигналы остановки';
    if (!station.diagnostics_complete) return 'Диагностика недоступна';
    if (Number(station.color) === 1) return 'Причина Error не определена';
    return 'Сигналы остановки не активны';
  }
  const esc = value => String(value).replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[ch]);
  const api = { bool, mode, motion, normalize, normalizeSnapshot, alarmSummary, STATIONS, SENSORS };
  if (typeof module === 'object' && module.exports) module.exports = api;
  root.GwmScada = api;
  if (!root.document) return;
  const $ = id => document.getElementById(id);
  let data = normalizeSnapshot(null), selected = 'MB200', busy = false, pending = false, timer;
  const boolText = value => value === null ? 'Нет данных' : value ? '1 · TRUE' : '0 · FALSE';
  const boolClass = value => value === null ? 'is-unknown' : value ? 'is-on' : 'is-off';
  function sensorStrip(station) {
    return SENSORS.map((name, i) => `<span class="sensor" title="${name}: ${boolText(station.sensors[name])}" aria-label="${name}: ${boolText(station.sensors[name])}"><i class="sensor-lamp ${boolClass(station.sensors[name])}"></i><span class="sensor-index">${i + 1}</span></span>`).join('');
  }
  function card(id) {
    const station = data[id], state = mode(station.color), run = motion(station.RUN_FWD, station.RUN_BWD, BOTTOM.includes(id));
    return `<div class="station mode-${state.className}" data-station="${id}">
      <button type="button" class="station-card" aria-pressed="${id === selected}" title="Открыть станцию ${id}: посмотреть текущий CARID кузова, активные аварии и причины состояния Error">
        <span class="station-top"><strong>${id}</strong><span class="station-mark" aria-hidden="true">${state.className === 'error' ? '!' : ''}</span></span>
        <span class="station-mode">${esc(state.short)}</span>
        <span class="station-motion motion-${run.key}" title="RUN_FWD: ${boolText(station.RUN_FWD)}; RUN_BWD: ${boolText(station.RUN_BWD)}"><span class="motion-arrow" aria-hidden="true">${run.arrow}</span><span>${run.short}</span></span>
      </button><div class="sensor-strip">${sensorStrip(station)}</div></div>`;
  }
  function alarmItem(item, cause) {
    return `<article class="alarm-item${cause && item.is_cause ? ' is-cause' : ''}"><h4>${esc(item.text || 'Сигнал без описания')}</h4>${item.source ? `<p class="alarm-meta">${esc(item.source)}</p>` : ''}${item.started_at ? `<p class="alarm-meta">${esc(new Date(item.started_at).toLocaleString('ru-RU', { timeZone: 'Asia/Qyzylorda' }))}</p>` : ''}</article>`;
  }
  function renderInspector() {
    const station = data[selected], state = mode(station.color);
    const source = station.plc.name || (TOP.includes(selected) ? 'GWM_UB' : 'GWM_MB');
    const carText = station.carid_status === 'ok' && station.carid ? station.carid : '—';
    const carNote = station.carid_status === 'empty' ? station.skid_present === false ? 'Транспортёр свободен' : 'CARID в текущей записи отсутствует' : station.carid_status === 'ok' ? 'CARID по данным ПЛК' : 'CARID пока недоступен';
    const verifiedError = station.cause_verified && Number(station.color) === 1;
    const concrete = station.alarms.filter(item => verifiedError && item.is_cause === true);
    const other = station.alarms.filter(item => !concrete.includes(item));
    const causeTitle = alarmSummary(station), hasCauses = station.causes.length > 0 || station.alarms.length > 0;
    $('inspector').innerHTML = `
      <div class="inspector-header"><p class="eyebrow">ВЫБРАННАЯ СТАНЦИЯ</p><h2 class="station-name">${selected}</h2><p class="plc-caption">${esc(source)}${station.plc.ip ? ' · ' + esc(station.plc.ip) : ''}</p>
        <div class="mode-banner mode-${state.className}"><span><i class="swatch"></i>${esc(state.label)}</span><span class="raw-code">Код ${station.color === null || station.color === '' ? '—' : esc(station.color)}</span></div></div>
      <div class="inspector-car"><div class="section-heading"><h3>CARID</h3></div><div class="carid-value">${esc(carText)}</div><p class="inspector-note">${carNote}</p></div>
      <div class="inspector-alarms"><div class="section-heading"><h3>Аварии станции</h3></div>
        <p class="alarm-status ${hasCauses ? 'is-active' : !station.diagnostics_complete ? 'is-unknown' : ''}">${esc(causeTitle)}</p>
        ${concrete.length ? `<div class="alarm-list">${concrete.map(item => alarmItem(item, true)).join('')}</div>` : ''}
        ${station.alarms.length ? '<p class="inspector-note">Аварии ПЛК могут сохраняться до сброса.</p>' : ''}
        ${Number(station.color) === 1 && !concrete.length && station.details_loaded ? '<p class="inspector-note">Точное сообщение причины пока не определено.</p>' : ''}
        ${station.causes.length ? `<div class="section-heading"><h3>Группы ошибок</h3></div><div class="alarm-list">${station.causes.map(item => alarmItem(item, verifiedError)).join('')}</div>` : ''}
        ${other.length ? `<div class="section-heading"><h3>Другие активные сигналы</h3><span class="alarm-count">${other.length}</span></div><div class="alarm-list">${other.map(item => alarmItem(item, false)).join('')}</div>` : ''}
        ${Number(station.color) === 1 && !station.alarm_mapping_complete && station.details_loaded ? '<p class="inspector-note">Расшифровка доступна для части аварийных сигналов.</p>' : ''}
        ${station.details_error ? `<p class="inspector-note">${esc(station.details_error)}</p>` : ''}
        ${hasCauses && !station.diagnostics_complete ? '<p class="inspector-note">Получена только часть сигналов диагностики.</p>' : ''}
      </div>`;
    $('station-picker').value = selected;
  }
  function render() {
    const focused = document.activeElement?.closest?.('.station-card')?.closest('[data-station]')?.dataset.station;
    $('top-row').innerHTML = TOP.map(card).join('');
    $('bottom-row').innerHTML = BOTTOM.map(card).join('');
    $('summary').innerHTML = [['auto', 'Auto'], ['manual', 'Manual'], ['error', 'Error'], ['undefined', 'Код 4'], ['missing', 'Нет данных']]
      .map(([key, label]) => `<div class="summary-item mode-${key}"><i class="swatch"></i><b>${STATIONS.filter(id => mode(data[id].color).className === key).length}</b><span>${label}</span></div>`).join('');
    renderInspector();
    if (focused) document.querySelector(`[data-station="${focused}"] .station-card`)?.focus({ preventScroll: true });
  }
  function select(id, scroll) {
    if (!STATIONS.includes(id)) return;
    selected = id;
    // Details are always fetched for this station; do not display a prior selection's diagnostics.
    data[id] = normalize({ ...data[id], carid: null, carid_status: 'unavailable', causes: [], alarms: [], details_loaded: false, details_error: '' });
    data[id].details_loaded = false;
    render();
    if (scroll) {
      const viewport = $('line-scroll'), target = document.querySelector(`[data-station="${id}"]`).getBoundingClientRect(), box = viewport.getBoundingClientRect();
      if (target.left < box.left || target.right > box.right) viewport.scrollLeft += target.left - box.left - (box.width - target.width) / 2;
    }
    refresh();
  }
  async function refresh() {
    clearTimeout(timer);
    if (busy) { pending = true; return; }
    busy = true; pending = false;
    const requestedStation = selected;
    $('live-refresh').disabled = true;
    try {
      const response = await fetch('/api/scada/gwm?station=' + encodeURIComponent(requestedStation), { signal: AbortSignal.timeout(6000), cache: 'no-store' });
      if (!response.ok) throw new Error('HTTP ' + response.status);
      const payload = await response.json();
      if (!payload.stations || typeof payload.stations !== 'object') throw new Error('Некорректный ответ');
      if (requestedStation !== selected) { pending = true; return; }
      data = normalizeSnapshot(payload.stations);
      const plcs = Array.isArray(payload.plcs) ? payload.plcs : [];
      const connected = plcs.filter(plc => plc.ok && !plc.error).length;
      $('data-source').textContent = connected === 2 ? 'ДАННЫЕ ПЛК' : connected ? 'ЧАСТЬ ДАННЫХ' : 'НЕТ ДАННЫХ ПЛК';
      const stamp = payload.updated_at ? new Date(payload.updated_at) : null;
      const timeText = stamp && !Number.isNaN(stamp.getTime()) ? stamp.toLocaleTimeString('ru-RU', { timeZone: 'Asia/Qyzylorda' }) : '—';
      const issues = plcs.filter(plc => !plc.ok || plc.error).map(plc => plc.name + ': ' + (plc.error || 'нет данных'));
      $('source-note').textContent = 'Обновлено ' + timeText + ' · только чтение' + (issues.length ? ' · ' + issues.join('; ') : ' · опрос каждые 3 с');
      render();
    } catch (error) {
      // Clear even the first failed snapshot; stale CARID or an old green state is misleading.
      data = normalizeSnapshot(null);
      $('data-source').textContent = 'НЕТ ДАННЫХ';
      $('source-note').textContent = 'Не удалось прочитать сигналы. Повторное подключение автоматически.';
      render();
    } finally {
      busy = false;
      $('live-refresh').disabled = false;
      timer = setTimeout(refresh, pending ? 0 : 3000);
    }
  }
  $('station-picker').innerHTML = [...STATIONS].sort((a, b) => Number(a.replace(/\D/g, '')) - Number(b.replace(/\D/g, ''))).map(id => `<option>${id}</option>`).join('');
  document.querySelectorAll('.station-row').forEach(row => row.addEventListener('click', event => {
    const button = event.target.closest('.station-card');
    if (button) {
      select(button.closest('[data-station]').dataset.station, false);
      document.querySelector(`[data-station="${selected}"] .station-card`).focus({ preventScroll: true });
    }
  }));
  $('station-picker').addEventListener('change', event => select(event.target.value, true));
  $('live-refresh').addEventListener('click', refresh);
  $('theme-toggle').addEventListener('click', () => {
    const dark = document.documentElement.dataset.theme !== 'dark';
    document.documentElement.dataset.theme = dark ? 'dark' : 'light';
    $('theme-toggle').textContent = dark ? 'Светлая тема' : 'Тёмная тема';
  });
  api.select = id => select(id, true);
  render();
  refresh();
})(typeof window !== 'undefined' ? window : globalThis);
