// Ответ отдела ОТО / ИТО на ответственность за отставание (шаг 2 T-15): блок ответа в едином окне «Ответ отдела и комментарии» (клик по «ОТО…/ИТО…» в «Отв.» или по описанию в «Причине»; во «Простоях» так же).
// Права и числа проверяет и считает сервер (dtresponse.py, responsibility.py); здесь подписи, проверка ввода и само окно. Чистые функции проверяются в node.
(function (root) {
  'use strict';

  const SHORT = { production: 'ПР', oto: 'ОТО', ito: 'ИТО', other: 'ДР' };
  const NAME = { production: 'Производство', oto: 'ОТО', ito: 'ИТО', other: 'Другое (внешние причины)' };
  const STATUS = { pending: 'ждёт подтверждения', confirmed: 'принято полностью', partial: 'принято частично', disputed: 'спорно' };
  const ANSWERING = ['oto', 'ito'];
  const ANY_DEPARTMENT = ['admin', 'chief'];   // отвечают за любой отдел (как на сервере)

  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const round1 = n => Math.round((Number(n) || 0) * 10) / 10;
  const num = n => String(round1(n)).replace('.', ',');
  const fmtTs = ts => {
    if (root.plantClock && typeof root.plantClock.fmt === 'function') return root.plantClock.fmt(ts);
    const m = String(ts || '').match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/);   // без часов завода (node): время как пришло с сервера
    return m ? `${m[3]}.${m[2]} ${m[4]}:${m[5]}` : '';
  };

  // Право ответить за отдел (как dtresponse.can_respond): вошедший не «Просмотр»; администратор и начальник цеха за любой отдел, остальные за своё подразделение
  function canRespond(user, owner) {
    if (!user || user.role === 'viewer' || !ANSWERING.includes(owner)) return false;
    return ANY_DEPARTMENT.includes(user.role) || user.department === owner;
  }
  // Отозвать ответ: только его автор и администратор
  const canWithdraw = (user, resp) => !!(user && resp && (user.role === 'admin' || resp.user_id === user.id));
  const dialogTitle = owner => `Ответ отдела ${SHORT[owner] || ''}`;

  // Подсказка под полем «наши минуты»: сколько уйдёт на ПР
  function restHint(total, k) {
    const t = round1(total), v = round1(k);
    if (!(v > 0 && v < t)) return `Укажите от 0,1 до ${num(t - 0.1)} мин`;
    return `Остаток ${num(t - v)} мин уйдёт на ПР`;
  }

  // Проверка перед отправкой (сервер проверяет то же): пустая строка = можно сохранять
  function validate(answer, total, k, comment) {
    if (!answer) return 'Выберите ответ';
    if (answer === 'partial') {
      const v = round1(k);
      if (k === null || k === undefined || k === '' || !(v > 0 && v < round1(total))) return `Частично: укажите наши минуты от 0,1 до ${num(round1(total) - 0.1)}`;
    }
    if (answer === 'disagree' && !String(comment || '').trim()) return 'Не согласен: напишите комментарий, почему';
    if (answer === 'disagree' && String(comment).trim().length > 500) return 'Не согласен: комментарий не длиннее 500 знаков';
    return '';
  }

  // Что ответил отдел, словами без текста комментария (блок окна: текст «Не согласен» лежит в переписке); total: минуты строки
  function answerLabel(resp, total) {
    if (!resp) return '';
    if (resp.answer === 'agree') return 'Согласен';
    if (resp.answer === 'partial') {
      const rest = total ? ` из ${num(total)}, ${num(round1(total) - round1(resp.minutes))} мин на ПР` : '';
      return `Частично, ${num(resp.minutes)} мин${rest}`;
    }
    return 'Не согласен';
  }
  // То же с текстом комментария «Не согласен» (подсказка знака в «Отв.»)
  const answerText = (resp, total) => answerLabel(resp, total) + (resp && resp.answer === 'disagree' && resp.comment ? `: «${resp.comment}»` : '');

  // Подсказка знака в колонке «Отв.»: отдел, статус, причина, ответ отдела с автором (содержание, затем автор в скобках)
  function ownTip(owner, status, resp, reason, total) {
    let s = `Ответственный: ${NAME[owner] || owner}, ${STATUS[status] || status}${reason ? `. Причина: ${reason}` : ''}`;
    if (resp) {
      const c = resp.comment && resp.answer !== 'disagree' ? ` — ${resp.comment}` : '';
      const at = answerText(resp, total);
      s += `. Ответ отдела: ${at.charAt(0).toLowerCase() + at.slice(1)}${c} (${resp.author || resp.user_name || ''}${resp.updated_at ? ', ' + fmtTs(resp.updated_at) : ''})`;
    }
    return s;
  }
  // «Ответил(а) Фамилия, должность, дата время»: строка автора под ответом
  const authorLine = resp => (resp ? `Ответил(а) ${resp.author || resp.user_name || '—'}, ${fmtTs(resp.updated_at)}` : '');

  // ---------- блок ответа отдела внутри единого окна «Ответ отдела и комментарии» (окно строит downtime.js: dtItemWindow) ----------
  // Строка «Простой 5,5 мин · за ОТО 5,5 мин» под заголовком окна
  const headLine = (dtMinutes, total, owner) => [dtMinutes ? `Простой ${num(dtMinutes)} мин` : '', `за ${SHORT[owner] || ''} ${num(total)} мин`].filter(Boolean).join(' · ');

  // Что делает «Сохранить» окна: ответ отдела (только если выбран и изменён) и/или новый комментарий; пустая error = можно сохранять.
  // o: { can, changed, answer, total, k, text }; «Не согласен» берёт текст из поля комментария окна и без него не сохраняется
  function savePlan(o) {
    const text = String(o.text || '').trim();
    if (o.can && o.changed) {
      const msg = validate(o.answer, o.total, o.k, text);
      if (msg) return { error: msg, response: false, comment: false };
    } else if (!text) {
      return { error: o.can ? 'Выберите ответ отдела или напишите комментарий' : 'Введите текст комментария', response: false, comment: false };
    }
    return { error: '', response: !!(o.can && o.changed), comment: !!text };
  }

  async function send(itemId, method, body) {
    let r;
    try {
      r = await fetch(`/api/downtime-items/${itemId}/response`, {
        method, credentials: 'same-origin', signal: AbortSignal.timeout(6000),
        headers: body ? { 'Content-Type': 'application/json' } : {}, body: body ? JSON.stringify(body) : undefined,
      });
    } catch (ex) { throw new Error(ex && ex.name === 'TimeoutError' ? 'Сервер не ответил за 6 секунд: попробуйте ещё раз' : 'Нет связи с сервером'); }
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Ответ не сохранён: проверьте поля и связь');
    return data;
  }

  // Рисует блок ответа в box. ctx: { item: {id, owner, minutes, response}, user, showErr(text), onChange() }
  // Блок виден тому, кто может отвечать за отдел, и всем остальным только для чтения, если ответ уже есть; иначе скрыт.
  // Возвращает управление окном: visible, can, changed(), answer(), minutes(), save(text), setThread(comments)
  function mount(box, ctx) {
    const it = ctx.item, owner = it.owner, total = Number(it.minutes) || 0;
    const can = canRespond(ctx.user, owner);
    let resp = it.response || null;
    let thread = [];   // комментарии переписки: текст «Не согласен» уже лежит там, повторять его в блоке не нужно
    const ctl = { visible: can || !!resp, can, newBox: null, changed: () => false, answer: () => '', minutes: () => null, save: async () => null, setThread() {}, focus() {} };
    box.hidden = !ctl.visible;
    if (!ctl.visible) return ctl;
    const short = SHORT[owner];
    box.innerHTML = `<div class="cm-title" title="Ответ отдела на ответственность за этот простой: согласен, частично или не согласен">Ответ отдела ${esc(short)}</div>
      ${can ? `<div class="dr-opts">
        <label class="dr-opt" title="Отдел согласен: все минуты этой строки остаются за отделом, статус станет «принято полностью» (●)"><input type="radio" name="dr-a" value="agree"> Согласен</label>
        <label class="dr-opt" title="Отдел принимает только часть минут: впишите свои минуты, остаток строки перейдёт на производство (ПР), статус «принято частично» (◐)"><input type="radio" name="dr-a" value="partial"> Частично: наши минуты
          <input type="number" class="dr-min" min="0.1" max="${round1(total - 0.1)}" step="0.1" title="Сколько минут из этой строки принимает отдел: больше 0 и меньше минут строки; остаток уйдёт на ПР"></label>
        <span class="dr-rest" title="Сколько минут строки перейдёт на производство (ПР) при частичном согласии"></span>
        <label class="dr-opt" title="Отдел не согласен: минуты остаются за отделом со статусом «спорно» (!), причину напишите в поле комментария отдела ниже, её увидят все"><input type="radio" name="dr-a" value="disagree"> Не согласен (комментарий обязателен)</label>
      </div>
      <div class="dr-newlbl" title="Комментарий отдела к ответу: для «Не согласен» обязателен, для остальных по желанию; текст попадёт в переписку выше">Комментарий отдела (для «Не согласен» обязателен)</div>
      <div class="dr-newbox"></div>` : ''}
      <div class="dr-view"></div>`;
    const q = s => box.querySelector(s);
    const view = q('.dr-view');
    ctl.newBox = q('.dr-newbox');
    const minEl = q('.dr-min'), restEl = q('.dr-rest');
    const answer = () => (q('input[name="dr-a"]:checked') || {}).value || '';
    const minutes = () => (!minEl || minEl.value === '' ? null : Number(minEl.value));
    const setAnswer = r => {
      box.querySelectorAll('input[name="dr-a"]').forEach(x => { x.checked = !!r && x.value === r.answer; });
      if (minEl) minEl.value = r && r.answer === 'partial' ? r.minutes : '';
    };
    const upd = () => {
      if (!restEl) return;
      const a = answer();
      restEl.textContent = a === 'partial' ? restHint(total, minEl.value === '' ? 0 : Number(minEl.value)) : '';
      restEl.hidden = a !== 'partial';
    };
    const inThread = t => thread.some(c => !c.deleted && String(c.text || '').trim() === t);
    const drawView = () => {
      const dcm = resp && resp.comment && !inThread(resp.comment.trim()) ? resp.comment : '';
      view.innerHTML = `${can ? '' : `<p class="dr-ans" title="Что ответил отдел: согласен, частично (сколько минут принял) или не согласен">${esc(answerLabel(resp, total))}</p>`}
        ${dcm ? `<p class="dr-dcm" title="Комментарий отдела, записанный вместе с ответом (в переписке ниже его нет)">Комментарий отдела: «${esc(dcm)}»</p>` : ''}
        ${resp ? `<p class="dr-who" title="Кто дал текущий ответ отдела и когда (фамилия, должность, дата и время)">${esc(authorLine(resp))}${can && canWithdraw(ctx.user, resp)
          ? ' <button type="button" data-act="withdraw" class="dt-sec" title="Отозвать ответ отдела: строка снова будет ждать подтверждения; прежний ответ сохранится в журнале">Отозвать</button>' : ''}</p>` : ''}`;
      const wd = view.querySelector('[data-act="withdraw"]');
      if (wd) wd.addEventListener('click', async () => {
        wd.disabled = true;
        try { await send(it.id, 'DELETE'); resp = null; setAnswer(null); upd(); drawView(); if (ctx.onChange) await ctx.onChange(); }
        catch (ex) { ctx.showErr(ex.message); wd.disabled = false; }
      });
    };
    if (can) {
      setAnswer(resp);
      box.querySelectorAll('input[name="dr-a"]').forEach(r => r.addEventListener('change', () => { upd(); if (answer() === 'partial') minEl.focus(); }));
      minEl.addEventListener('input', () => { const p = q('input[value="partial"]'); if (p) p.checked = true; upd(); });
      minEl.addEventListener('focus', () => { const p = q('input[value="partial"]'); if (p && !p.checked) { p.checked = true; upd(); } });
      upd();
    }
    drawView();
    Object.assign(ctl, {
      answer, minutes,
      // выбранный ответ отличается от сохранённого (пока ответ не выбран или не менялся, сохранять его не нужно)
      changed: () => { const a = answer(); return !!a && (!resp || resp.answer !== a || (a === 'partial' && round1(minutes()) !== round1(resp.minutes))); },
      // PUT ответа; «Не согласен»: тот же текст уходит и в ответ (сервер требует comment), и в переписку окном
      save: async text => {
        const a = answer(), k = minutes();
        const data = await send(it.id, 'PUT', { answer: a, minutes: a === 'partial' ? round1(k) : null, comment: a === 'disagree' ? text : '' });
        resp = data.response;
        drawView();
        return resp;
      },
      setThread: list => { thread = list || []; drawView(); },
      focus: () => { const c = q('input[name="dr-a"]'); if (c) c.focus(); },
    });
    return ctl;
  }

  const api = { canRespond, canWithdraw, dialogTitle, restHint, validate, answerText, answerLabel, ownTip, authorLine, headLine, savePlan, mount };
  root.dtResponse = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
