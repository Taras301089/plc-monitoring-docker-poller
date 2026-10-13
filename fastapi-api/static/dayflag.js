// Дни без выпуска и отметка «Простой весь день (поломка линии)»: чистые функции для «Отчётов» и архива Andon (проверяются в node).
// Само правило считает сервер (dayflags.py); здесь только то, что показать: подпись «нет выпуска», кнопка отметки, строка «По дням».
(function (root) {
  'use strict';

  const LABEL = 'Простой весь день (поломка линии)';
  const NO_OUTPUT = 'нет выпуска';
  const FLAG_TITLE = 'Для дня без выпуска: отметьте, если линия стояла из-за поломки. Тогда время простоя за этот день пойдёт в статистику. Снять отметку может автор и администратор';
  const NO_OUTPUT_TITLE = 'За день не произведено ни одного кузова: выходной или нет плана, простой не считается. Если линия простаивала из-за поломки, отметьте это кнопкой «Простой весь день (поломка линии)» в «Отчётах»';

  const canEdit = user => !!user && user.role !== 'viewer';

  // Что показать в «Отчётах» над плитками за один день. info: { state: null | 'no_output' | 'full_downtime', flag }, user: вошедший пользователь или null.
  // Кнопка скрыта, если действие недоступно: день с выпуском, текущий день, нет входа или роль «Просмотр».
  function flagView(info, user) {
    if (!info || !info.state) return { show: false };
    const on = info.state === 'full_downtime';
    const edit = canEdit(user);
    if (!on && !edit) return { show: false };
    const flag = info.flag || {};
    const mayClear = edit && (user.role === 'admin' || user.id === flag.set_by);
    return {
      show: true, on, enabled: on ? mayClear : edit, label: LABEL, title: FLAG_TITLE,
      // содержание, затем автор: «отмечено: Фамилия, должность · ДД.ММ ЧЧ:ММ»
      note: on ? `отмечено: ${flag.set_by_name || '—'} · ${flag.set_at_text || ''}`.replace(/ · $/, '') : '',
    };
  }

  // Значение плитки «Простой, мин» за день: у дня без выпуска подпись «нет выпуска» (если людьми не описано ни минуты)
  function downValue(info, down, fmtNum) {
    return info && info.no_output && !down ? NO_OUTPUT : fmtNum(down);
  }

  // Строка таблицы «По дням» за период; fmt: { fmtD, fmtNum, pct1 }. У дня без выпуска «±» пустая, простой подписан.
  function periodDayRow(x, fmt) {
    const quiet = !!x.no_output && !x.downtime;
    return [fmt.fmtD(x.date), x.plan, x.fact, x.no_output ? '' : x.fact - x.plan, fmt.pct1(x.pct),
      quiet ? NO_OUTPUT : fmt.fmtNum(x.downtime), quiet ? '—' : fmt.fmtNum(x.described), quiet ? '—' : fmt.fmtNum(Math.max(0, x.downtime - x.described))];
  }

  const api = { LABEL, NO_OUTPUT, FLAG_TITLE, NO_OUTPUT_TITLE, flagView, downValue, periodDayRow };
  root.dayFlag = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
