// Кнопка «Excel» на экране Andon: один день (по умолчанию показанный на экране) или любой период с графиками; доступна всем, вход не нужен
(() => {
  'use strict';

  const btn = document.querySelector('#andon-excel');
  if (!btn) return;
  const toast = t => (typeof showToast === 'function' ? showToast(t) : alert(t));
  const today = () => window.plantClock.isoDate();
  const addDays = (iso, n) => { const d = new Date(iso + 'T00:00:00Z'); d.setUTCDate(d.getUTCDate() + n); return d.toISOString().slice(0, 10); };
  const firstOfMonth = iso => iso.slice(0, 8) + '01';
  const lastOfMonth = iso => { const d = new Date(iso.slice(0, 8) + '01T00:00:00Z'); d.setUTCMonth(d.getUTCMonth() + 1); d.setUTCDate(0); return d.toISOString().slice(0, 10); };

  const presets = () => {
    const t = today();
    const prevMonthLast = addDays(firstOfMonth(t), -1);
    return {
      week: [addDays(t, -6), t],
      month: [firstOfMonth(t), t],
      prev: [firstOfMonth(prevMonthLast), lastOfMonth(prevMonthLast)],
    };
  };

  // Скачивание Excel по адресу API: ошибки (нет данных и т.п.) показываются сообщением, а не открываются как страница
  window.xlsxDownload = async url => {
    const r = await fetch(url);
    if (!r.ok) {
      let msg = `Ошибка ${r.status}`;
      try { const j = await r.json(); if (j.detail) msg = j.detail; } catch { /* оставим код */ }
      throw new Error(msg);
    }
    const blob = await r.blob();
    const m = /filename\*=UTF-8''([^;]+)/.exec(r.headers.get('Content-Disposition') || '');
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = m ? decodeURIComponent(m[1]) : 'andon.xlsx';
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 10000);
  };

  btn.addEventListener('click', () => {
    const id = andonScreenId;
    if (typeof id !== 'number') { toast('Сначала выберите линию на экране Andon'); return; }
    const p = presets();
    const day = window.andonDay || today();
    p.day = [day, day];
    const m = document.createElement('div');
    m.className = 'kpi-modal';
    m.innerHTML = `<div class="kpi-modal-box dt-box per-box" role="dialog" aria-label="Выгрузка в Excel">
      <h3>Выгрузка в Excel</h3>
      <p>Один день: таблица дня (интервалы, план, факт, простои, причины, комментарии) и график план/факт по часам. Период из нескольких дней: сводка с графиками, таблицы по дням, причинам и станциям, список простоев.</p>
      <div class="per-row">
        <label>С <input type="date" data-f="from" value="${day}" max="${today()}" title="Первый день периода; если даты «с» и «по» одинаковые, выгружается один день"></label>
        <label>По <input type="date" data-f="to" value="${day}" max="${today()}" title="Последний день периода (включительно)"></label>
      </div>
      <div class="per-presets">
        <button type="button" class="dt-sec" data-p="day" title="Только день, показанный сейчас на экране Andon">Показанный день</button>
        <button type="button" class="dt-sec" data-p="week" title="Последние 7 дней, включая сегодня">7 дней</button>
        <button type="button" class="dt-sec" data-p="month" title="С первого числа текущего месяца по сегодня">Этот месяц</button>
        <button type="button" class="dt-sec" data-p="prev" title="Весь прошлый календарный месяц">Прошлый месяц</button>
      </div>
      <div class="auth-err" hidden></div>
      <div class="kpi-modal-actions">
        <button type="button" class="dt-sec" data-act="close" title="Закрыть окно">Отмена</button>
        <button type="button" data-act="go" title="Сформировать отчёт и скачать файл Excel (.xlsx) по текущей линии">⬇ Скачать Excel</button>
      </div></div>`;
    document.body.appendChild(m);
    const close = () => m.remove();
    const from = m.querySelector('[data-f="from"]'), to = m.querySelector('[data-f="to"]'), err = m.querySelector('.auth-err');
    m.addEventListener('mousedown', e => { if (e.target === m) close(); });
    m.querySelector('[data-act="close"]').addEventListener('click', close);
    m.querySelectorAll('[data-p]').forEach(b => b.addEventListener('click', () => { [from.value, to.value] = p[b.dataset.p]; }));
    m.querySelector('[data-act="go"]').addEventListener('click', async e => {
      err.hidden = true;
      if (!from.value || !to.value) { err.textContent = 'Укажите обе даты'; err.hidden = false; return; }
      if (to.value < from.value) { err.textContent = 'Дата «по» раньше даты «с»'; err.hidden = false; return; }
      const b = e.currentTarget;
      b.disabled = true;
      const url = from.value === to.value
        ? `/api/kpi/screens/${id}/day.xlsx?date=${from.value}`
        : `/api/kpi/screens/${id}/period.xlsx?from=${from.value}&to=${to.value}`;
      try { await window.xlsxDownload(url); close(); } catch (ex) { err.textContent = ex.message; err.hidden = false; b.disabled = false; }
    });
  });
})();
