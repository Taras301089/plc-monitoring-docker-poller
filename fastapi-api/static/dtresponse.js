// Ответ отдела ОТО / ИТО на ответственность за отставание (шаг 2 T-15): окно «Ответ отдела ОТО» из колонки «Отв.» на Andon и во «Простоях».
// Права и числа проверяет и считает сервер (dtresponse.py, responsibility.py); здесь подписи, проверка ввода и само окно. Чистые функции проверяются в node.
(function (root) {
  'use strict';

  const SHORT = { production: 'ПР', oto: 'ОТО', ito: 'ИТО', other: 'ДР' };
  const NAME = { production: 'Производство', oto: 'ОТО', ito: 'ИТО', other: 'Другое (внешние причины)' };
  const STATUS = { pending: 'ждёт подтверждения', confirmed: 'подтверждено', disputed: 'спорно' };
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
    return '';
  }

  // Что ответил отдел, словами (для подсказки и окна); total: минуты строки (для остатка на ПР при «Частично»)
  function answerText(resp, total) {
    if (!resp) return '';
    if (resp.answer === 'agree') return 'Согласен';
    if (resp.answer === 'partial') {
      const rest = total ? `, ${num(round1(total) - round1(resp.minutes))} мин на ПР` : '';
      return `Частично: отдел принял ${num(resp.minutes)} мин${rest}`;
    }
    return `Не согласен${resp.comment ? `: «${resp.comment}»` : ''}`;
  }

  // Подсказка знака в колонке «Отв.»: отдел, статус, причина, ответ отдела с автором (содержание, затем автор в скобках)
  function ownTip(owner, status, resp, reason, total) {
    let s = `Ответственный: ${NAME[owner] || owner}, ${STATUS[status] || status}${reason ? `. Причина: ${reason}` : ''}`;
    if (resp) {
      const c = resp.comment && resp.answer !== 'disagree' ? ` — ${resp.comment}` : '';
      s += `. Ответ отдела: ${answerText(resp, total)}${c} (${resp.author || resp.user_name || ''}${resp.updated_at ? ', ' + fmtTs(resp.updated_at) : ''})`;
    }
    return s;
  }
  // «Ответил(а) Фамилия, должность, дата время»: строка автора под ответом
  const authorLine = resp => (resp ? `Ответил(а) ${resp.author || resp.user_name || '—'}, ${fmtTs(resp.updated_at)}` : '');

  // ---------- окно ----------
  // ctx: { item: {id, owner, minutes, response}, time, dtMinutes, reason, note, onDone }
  function open(ctx) {
    const doc = root.document;
    const st = root.authState && root.authState();
    const user = st && st.user ? st.user : null;
    const it = ctx.item, owner = it.owner, total = Number(it.minutes) || 0, resp = it.response || null;
    if (!canRespond(user, owner) || doc.querySelector('.dr-box')) return;
    const short = SHORT[owner];
    const m = doc.createElement('div');
    m.className = 'kpi-modal';
    const head = [ctx.time ? `Интервал ${ctx.time}` : '', ctx.dtMinutes ? `простой ${num(ctx.dtMinutes)} мин` : '', `за ${short} ${num(total)} мин`,
      ctx.reason || '', ctx.note || ''].filter(Boolean).join(' · ');
    const k0 = resp && resp.answer === 'partial' ? resp.minutes : '';
    m.innerHTML = `<div class="kpi-modal-box dt-box dr-box" role="dialog" aria-label="${esc(dialogTitle(owner))}">
      <h3>${esc(dialogTitle(owner))}</h3>
      <p class="dr-head" title="Строка описания простоя, за которую отвечает отдел: интервал, весь простой интервала, минуты строки, причина и описание">${esc(head)}</p>
      <div class="dr-opts">
        <label class="dr-opt" title="Отдел согласен: все минуты этой строки остаются за отделом, статус станет «подтверждено» (✓)"><input type="radio" name="dr-a" value="agree"${resp && resp.answer === 'agree' ? ' checked' : ''}> Согласен</label>
        <label class="dr-opt" title="Отдел принимает только часть минут: впишите свои минуты, остаток строки перейдёт на производство (ПР), статус «подтверждено»"><input type="radio" name="dr-a" value="partial"${resp && resp.answer === 'partial' ? ' checked' : ''}> Частично: наши минуты
          <input type="number" class="dr-min" min="0.1" max="${round1(total - 0.1)}" step="0.1" value="${esc(k0)}" title="Сколько минут из этой строки принимает отдел: больше 0 и меньше минут строки; остаток уйдёт на ПР"></label>
        <span class="dr-rest" title="Сколько минут строки перейдёт на производство (ПР) при частичном согласии"></span>
        <label class="dr-opt" title="Отдел не согласен: минуты остаются за отделом со статусом «спорно» (!), комментарий обязателен, его увидят все"><input type="radio" name="dr-a" value="disagree"${resp && resp.answer === 'disagree' ? ' checked' : ''}> Не согласен: комментарий</label>
        <textarea class="dr-cm" maxlength="500" rows="2" placeholder="Комментарий отдела (для «Не согласен» обязательно)" title="Комментарий отдела к ответу: для «Не согласен» обязательно напишите, почему; для остальных ответов по желанию">${esc(resp ? resp.comment : '')}</textarea>
      </div>
      ${resp ? `<p class="dr-who" title="Кто дал текущий ответ отдела и когда (фамилия, должность, дата и время)">${esc(authorLine(resp))}</p>` : ''}
      <div class="auth-err" hidden></div>
      <div class="kpi-modal-actions">
        ${resp && canWithdraw(user, resp) ? '<button type="button" data-act="withdraw" class="dt-sec" title="Отозвать ответ отдела: строка снова будет ждать подтверждения; прежний ответ сохранится в журнале">Отозвать ответ</button>' : ''}
        <span style="flex:1"></span>
        <button type="button" data-act="cancel" class="dt-sec" title="Закрыть окно без сохранения ответа отдела (клавиша Esc или щелчок вне окна)">Отмена</button>
        <button type="button" data-act="save" title="Сохранить ответ отдела: статус и минуты по ответственным пересчитаются на всех экранах и в Excel">Сохранить</button>
      </div>
    </div>`;
    doc.body.appendChild(m);
    const q = s => m.querySelector(s);
    const err = q('.auth-err'), minEl = q('.dr-min'), restEl = q('.dr-rest'), cmEl = q('.dr-cm');
    const answer = () => (q('input[name="dr-a"]:checked') || {}).value || '';
    const showErr = t => { err.textContent = t; err.hidden = !t; };
    const upd = () => {
      const a = answer();
      restEl.textContent = a === 'partial' ? restHint(total, minEl.value === '' ? 0 : Number(minEl.value)) : '';
      restEl.hidden = a !== 'partial';
      cmEl.classList.toggle('need', a === 'disagree' && !cmEl.value.trim());
    };
    m.querySelectorAll('input[name="dr-a"]').forEach(r => r.addEventListener('change', () => { upd(); if (answer() === 'partial') minEl.focus(); else if (answer() === 'disagree') cmEl.focus(); }));
    minEl.addEventListener('input', () => { const p = q('input[value="partial"]'); if (p) p.checked = true; upd(); });
    minEl.addEventListener('focus', () => { const p = q('input[value="partial"]'); if (p && !p.checked) { p.checked = true; upd(); } });
    cmEl.addEventListener('input', upd);
    upd();
    const onKey = e => { if (e.key === 'Escape') { e.preventDefault(); close(); } };
    const close = () => { doc.removeEventListener('keydown', onKey); m.remove(); };
    doc.addEventListener('keydown', onKey);
    m.addEventListener('mousedown', e => { if (e.target === m) close(); });
    q('[data-act="cancel"]').addEventListener('click', close);
    const send = async (method, body) => {
      const r = await fetch(`/api/downtime-items/${it.id}/response`, {
        method, credentials: 'same-origin', signal: AbortSignal.timeout(6000),
        headers: body ? { 'Content-Type': 'application/json' } : {}, body: body ? JSON.stringify(body) : undefined,
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Ответ не сохранён: проверьте поля и связь');
      return data;
    };
    const done = async text => {
      close();
      if (typeof root.showToast === 'function') root.showToast(text);
      if (ctx.onDone) await ctx.onDone();
    };
    const save = q('[data-act="save"]');
    save.addEventListener('click', async () => {
      const a = answer(), k = minEl.value === '' ? null : Number(minEl.value), cm = cmEl.value.trim();
      const msg = validate(a, total, k, cm);
      if (msg) { showErr(msg); return; }
      showErr('');
      save.disabled = true;
      try { await send('PUT', { answer: a, minutes: a === 'partial' ? round1(k) : null, comment: cm }); await done('Ответ отдела сохранён'); }
      catch (ex) { showErr(ex.name === 'TimeoutError' ? 'Сервер не ответил за 6 секунд: попробуйте ещё раз' : ex.message); save.disabled = false; }
    });
    const wd = q('[data-act="withdraw"]');
    if (wd) wd.addEventListener('click', async () => {
      wd.disabled = true;
      try { await send('DELETE'); await done('Ответ отдела отозван'); }
      catch (ex) { showErr(ex.name === 'TimeoutError' ? 'Сервер не ответил за 6 секунд: попробуйте ещё раз' : ex.message); wd.disabled = false; }
    });
    setTimeout(() => { const c = q('input[name="dr-a"]:checked') || q('input[name="dr-a"]'); if (c) c.focus(); }, 50);
  }

  const api = { canRespond, canWithdraw, dialogTitle, restHint, validate, answerText, ownTip, authorLine, open };
  root.dtResponse = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
