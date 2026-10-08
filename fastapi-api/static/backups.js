// Справка, раздел «Бэкапы и место на диске»: виден только администратору (данные из GET /api/backups)
(() => {
  'use strict';
  const sec = document.querySelector('#backups-section');
  if (!sec) return;
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  function size(n) {
    if (n == null) return '—';
    const u = ['Б', 'КБ', 'МБ', 'ГБ', 'ТБ'];
    let i = 0, v = Number(n);
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return v.toLocaleString('ru-RU', { maximumFractionDigits: i < 2 ? 0 : 1 }) + '\u00a0' + u[i];
  }
  function age(h) {
    if (h == null) return 'нет бэкапов';
    if (h < 1) return 'меньше часа назад';
    if (h < 48) return Math.round(h) + '\u00a0ч назад';
    return Math.round(h / 24) + '\u00a0дн назад';
  }
  function days(n) {
    if (n >= 730) return 'более 2 лет';
    if (n >= 60) return '~' + Math.round(n / 30) + '\u00a0мес';
    return n + '\u00a0дн';
  }
  const tile = (cls, label, value, title) =>
    `<div class="bk-tile ${cls}" title="${esc(title)}"><small>${esc(label)}</small><b>${esc(value)}</b></div>`;

  function render(d) {
    const fresh = d.last_backup_age_hours != null && d.last_backup_age_hours <= 36;
    const okRun = d.status_ok !== false;
    const tiles = [];
    tiles.push(tile(fresh && okRun ? 'ok' : 'bad', 'Последний бэкап', age(d.last_backup_age_hours),
      'Когда сняты последние дампы. Красный: старше 36 часов, нет файлов или последний запуск скрипта закончился ошибкой'));
    tiles.push(tile('', 'Размер базы', size(d.db_size), 'Размер основной базы данных на диске (сама база, не дамп)'));
    tiles.push(tile('', 'Все дампы', size(d.backups_total), 'Суммарный размер файлов в папке бэкапов (хранятся последние ' + (d.keep_days || 30) + ' дней)'));
    if (d.disk_free != null) {
      tiles.push(tile('', 'Свободно на C:', size(d.disk_free) + ' из ' + size(d.disk_total),
        'Свободное место на диске C: на момент последнего запуска backup.ps1 (обновляется при каждом бэкапе)'));
    }
    const f = d.forecast || {};
    let fText, fCls = '', fTitle = 'Свободное место делится на средний рост базы по размерам ежедневных дампов';
    if (f.status === 'ok') {
      fText = days(f.days_left) + ' (рост ' + size(f.growth_per_day) + '/день)';
      fCls = f.days_left < 90 ? 'bad' : 'ok';
    } else if (f.status === 'no_growth') {
      fText = 'рост не обнаружен';
    } else {
      fText = 'мало данных (нужно 7+ дней бэкапов)';
    }
    tiles.push(tile(fCls, 'Места хватит на', fText, fTitle));
    document.querySelector('#bk-tiles').innerHTML = tiles.join('');

    let line = 'Папка: <code>' + esc(d.folder) + '</code>.';
    if (!d.folder_available) line += ' <strong>Папка недоступна для сайта</strong> (проверьте подключение <code>C:\plc-backups</code> в <code>docker-compose.yml</code>).';
    else if (d.status_ok === false) line += ' <strong>Последний запуск скрипта завершился ошибкой:</strong> ' + esc(d.status_error || 'причина не указана');
    else if (d.status_ok == null) line += ' Файл <code>status.json</code> не найден: запустите <code>backup.ps1</code>, чтобы появились данные о диске.';
    document.querySelector('#bk-summary').innerHTML = line;

    document.querySelector('#bk-files tbody').innerHTML = d.files.length
      ? d.files.map(x => `<tr><td>${esc(x.name)}</td><td>${esc(new Date(x.mtime * 1000).toLocaleString('ru-RU'))}</td><td class="bk-num">${size(x.size)}</td></tr>`).join('')
      : '<tr><td colspan="3">Дампов нет</td></tr>';
    document.querySelector('#bk-tables tbody').innerHTML = d.tables
      .map(x => `<tr><td>${esc(x.name)}</td><td class="bk-num">${size(x.size)}</td></tr>`).join('');
    sec.hidden = false;
  }

  async function load() {
    try {
      const r = await fetch('/api/backups', { credentials: 'same-origin', signal: AbortSignal.timeout(6000) });
      if (!r.ok) { sec.hidden = true; return; }   // не администратор или не вошёл: раздел не показывается
      render(await r.json());
    } catch { /* нет связи: раздел остаётся скрытым или с прежними данными */ }
  }
  load();
  setInterval(load, 60000);
})();
