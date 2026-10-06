// Вкладка «Отчёты» (только администратор): автосохранение отчётов дня и месяца по линиям в папку на ПК с Docker
(() => {
  'use strict';

  const tab = document.querySelector('#tab-reports');
  const pane = document.querySelector('#reports');
  if (!tab || !pane) return;

  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const toast = t => (typeof showToast === 'function' ? showToast(t) : alert(t));
  const isAdmin = () => { const st = window.authState && window.authState(); return !!(st && st.user && st.user.role === 'admin'); };

  async function api(url, opts) {
    const r = await fetch(url, opts);
    if (!r.ok) {
      let msg = `Ошибка ${r.status}`;
      try { const j = await r.json(); if (j.detail) msg = j.detail; } catch { /* оставим код */ }
      throw new Error(msg);
    }
    return r.json();
  }

  const status = x => {
    if (x.last_error) return `<span class="rep-err">${esc(x.last_error)}</span>`;
    if (x.last_saved_at) return `Сохранён ${esc(window.plantClock.fmt(x.last_saved_at, true))}`;
    return '<span class="dim">пока не сохранялся</span>';
  };

  async function render() {
    if (!isAdmin()) { pane.innerHTML = '<div class="empty">Настраивать автосохранение отчётов может только администратор</div>'; return; }
    let data;
    try { data = await api('/api/reports/settings'); } catch (ex) { pane.innerHTML = `<div class="empty">${esc(ex.message)}</div>`; return; }

    pane.innerHTML = `<div class="rep-pane">
      <p class="hint">Отчёт дня по каждой линии сохраняется в Excel в папку на ПК, где запущен сервис: <b class="rep-dir">${esc(data.dir)}</b> (внутри папки «год-месяц», затем линия). В первый день месяца в то же время сохраняется месячный отчёт с графиками за прошлый месяц.</p>
      <table class="rep-table"><thead><tr>
        <th title="Включить автоматическое сохранение отчётов для линии">Вкл.</th>
        <th title="Линия (экран Andon)">Линия</th>
        <th title="Во сколько каждый день сохранять отчёт: после окончания работы линии, когда данные за день уже полные">Время сохранения</th>
        <th title="Когда отчёт последний раз сохранялся автоматически или причина, по которой не получилось">Состояние</th>
        <th title="Сохранить отчёт сегодняшнего дня прямо сейчас, не дожидаясь указанного времени"></th>
      </tr></thead><tbody>${data.screens.map(x => `<tr data-id="${x.id}">
        <td><input type="checkbox" data-f="enabled"${x.enabled ? ' checked' : ''} title="Сохранять отчёты линии ${esc(x.name)} каждый день автоматически"></td>
        <td class="rep-name">${esc(x.name)}</td>
        <td><input type="time" data-f="time" value="${esc(x.save_time)}" title="Время сохранения отчёта линии ${esc(x.name)}; рекомендуется ${esc(x.default_time)}: конец последнего интервала плюс 15 минут"></td>
        <td class="rep-st" data-f="st">${status(x)}</td>
        <td><button type="button" class="dt-sec" data-act="now" title="Сохранить отчёт линии ${esc(x.name)} за сегодня сейчас">Сохранить сейчас</button></td></tr>`).join('')}</tbody></table>
      <div class="rep-actions"><button type="button" data-act="save" title="Сохранить включение и время автосохранения по всем линиям">Сохранить настройки</button></div>
    </div>`;

    pane.querySelector('[data-act="save"]').addEventListener('click', async e => {
      const b = e.currentTarget;
      b.disabled = true;
      try {
        for (const tr of pane.querySelectorAll('tbody tr')) {
          const time = tr.querySelector('[data-f="time"]').value;
          if (!time) throw new Error(`Укажите время для линии «${tr.querySelector('.rep-name').textContent}»`);
          await api(`/api/reports/settings/${tr.dataset.id}`, {
            method: 'PUT', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled: tr.querySelector('[data-f="enabled"]').checked, save_time: time }),
          });
        }
        toast('Настройки автосохранения сохранены');
      } catch (ex) { toast(ex.message); }
      b.disabled = false;
    });

    pane.querySelectorAll('[data-act="now"]').forEach(b => b.addEventListener('click', async () => {
      const tr = b.closest('tr');
      b.disabled = true;
      try {
        const r = await api(`/api/reports/save-now/${tr.dataset.id}`, { method: 'POST' });
        tr.querySelector('[data-f="st"]').innerHTML = `Сохранено: <span class="rep-file">${esc(r.file)}</span>`;
      } catch (ex) { tr.querySelector('[data-f="st"]').innerHTML = `<span class="rep-err">${esc(ex.message)}</span>`; }
      b.disabled = false;
    }));
  }

  tab.addEventListener('click', render);
  // вкладка видна только администратору
  const prev = window.onAuthChanged;
  window.onAuthChanged = () => {
    if (prev) prev();
    tab.hidden = !isAdmin();
    if (tab.hidden && pane.classList.contains('active')) document.querySelector('.tab[data-tab="browser"]').click();
  };
  tab.hidden = !isAdmin();
})();
