// Итоги отставания по отделам: подписи для Andon, вкладки «Простои» и «Отчётов» (проверяются в node).
// Числа считает сервер (responsibility.py, ответ r: total, owners, undescribed, pending(_by), disputed(_by), share); здесь только то, как их показать.
(function (root) {
  'use strict';

  const ORDER = ['production', 'oto', 'ito', 'other'];
  const SHORT = { production: 'ПР', oto: 'ОТО', ito: 'ИТО', other: 'ДР' };
  const NAME = { production: 'Производство', oto: 'ОТО', ito: 'ИТО', other: 'Другое (внешние причины)' };
  // те же названия столбцов, что в Excel (responsibility.HEADS на сервере)
  const HEADS = ['', 'Всего', 'ПР', 'ОТО', 'ИТО', 'ДР', 'Не назначено', 'Не описано', 'Ждёт подтверждения', 'Спорно'];
  const TIPS = [
    'Название строки: минуты или доля от всего отставания',
    'Всё отставание за период в минутах: то же число, что «Простой, мин»; равно сумме ПР, ОТО, ИТО, ДР, «Не назначено» и «Не описано»',
    'Минуты отставания по причинам, за которые отвечает производство',
    'Минуты отставания по причинам, за которые отвечает отдел технического обслуживания (ОТО)',
    'Минуты отставания по причинам, за которые отвечает инженерно-технический отдел (ИТО)',
    'Минуты отставания по внешним причинам (другое): поставщик, электричество и т.п.',
    'Минуты отставания, которые описаны, но отдел ещё не выбран: ответственного назначает человек в окне описания простоя',
    'Минуты отставания, для которых ещё не указана причина и ответственный',
    'Минуты ОТО и ИТО, которые отдел ещё не подтвердил; это часть минут отделов, а не отдельное слагаемое',
    'Минуты, с которыми отдел не согласен; это часть минут отделов, а не отдельное слагаемое',
  ];
  const TITLE = 'По ответственным';
  const TAB_TITLE = 'Минуты по ответственным';
  const ROW_MINUTES = 'Минут';
  const ROW_SHARE = 'Доля, %';

  const num = n => String(Math.round((Number(n) || 0) * 10) / 10).replace('.', ',');
  const pct = n => `${Math.round(Number(n) || 0)}%`;
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const by = (r, f, o) => Number(((r || {})[f] || {})[o]) || 0;

  // Строка в «ИТОГО» на Andon: «Отставание за день N мин: ПР a · ОТО b … · не описано c».
  // Только отделы с минутами; «…» после отдела, если часть его минут ждёт подтверждения; «!» красным после «спорно»; без отставания строки нет.
  function andonLine(r) {
    if (!r || !(Number(r.total) > 0)) return { text: '', html: '', title: '' };
    const parts = [], htmls = [], tip = [`Отставание за день: ${num(r.total)} мин`];
    for (const o of ORDER) {
      const m = Number((r.owners || {})[o]) || 0;
      if (!(m > 0)) continue;
      const pend = by(r, 'pending_by', o) > 0, disp = by(r, 'disputed_by', o) > 0;
      parts.push(`${SHORT[o]} ${num(m)}${pend ? ' …' : ''}${disp ? ' !' : ''}`);
      htmls.push(`${SHORT[o]} ${num(m)}${pend ? ' <span class="resp-pend">…</span>' : ''}${disp ? ' <b class="resp-disp">!</b>' : ''}`);
      const extra = [];
      if (pend) extra.push(`ждёт подтверждения ${num(by(r, 'pending_by', o))} мин`);
      if (disp) extra.push(`спорно ${num(by(r, 'disputed_by', o))} мин`);
      tip.push(`${NAME[o]}: ${num(m)} мин (${pct((r.share || {})[o])})${extra.length ? ', ' + extra.join(', ') : ''}`);
    }
    const unas = Number((r.owners || {}).none) || 0;
    if (unas > 0) { parts.push(`не назначено ${num(unas)}`); htmls.push(`не назначено ${num(unas)}`); tip.push(`Не назначено (отдел ещё не выбран): ${num(unas)} мин (${pct((r.share || {}).none)})`); }
    const und = Number(r.undescribed) || 0;
    if (und > 0) { parts.push(`не описано ${num(und)}`); htmls.push(`не описано ${num(und)}`); }
    tip.push(`Не описано: ${num(und)} мин (${pct((r.share || {}).undescribed)})`);
    tip.push(`Ждёт подтверждения: ${num(r.pending)} мин; спорно: ${num(r.disputed)} мин (часть минут отделов, не отдельное слагаемое)`);
    const head = `Отставание за день ${num(r.total)} мин`;
    const sep = parts.length ? ': ' : '';
    return { text: head + sep + parts.join(' · '), html: esc(head) + sep + htmls.join(' · '), title: tip.join('\n') };
  }

  // Две строки таблицы (минуты и доли) в порядке HEADS; те же числа в Excel
  function tableRows(r) {
    const o = (r && r.owners) || {}, s = (r && r.share) || {};
    return [
      [ROW_MINUTES, num(r.total), num(o.production), num(o.oto), num(o.ito), num(o.other), num(o.none), num(r.undescribed), num(r.pending), num(r.disputed)],
      [ROW_SHARE, r.total > 0 ? '100%' : '—', pct(s.production), pct(s.oto), pct(s.ito), pct(s.other), pct(s.none), pct(s.undescribed), pct(s.pending), pct(s.disputed)],
    ];
  }

  // Заполняет ячейку «ИТОГО» (колонка «Причина») таблицы Andon; resp приходит от сервера вместе со списком простоев дня
  function fill(rootEl, r) {
    const td = rootEl.querySelector('tfoot [data-k="resp"]');
    if (!td) return;
    const v = andonLine(r);
    if (td.dataset.sig !== v.html) { td.innerHTML = v.html; td.dataset.sig = v.html; }
    td.title = v.title;
  }

  const api = { fill, ORDER, SHORT, NAME, HEADS, TIPS, TITLE, TAB_TITLE, ROW_MINUTES, ROW_SHARE, andonLine, tableRows };
  root.respKit = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
