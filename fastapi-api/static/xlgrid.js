// Чистая логика Excel-подобной таблицы (без DOM): разбор и сборка TSV, заполнение, вставка, сравнение, проверка.
(function (root) {
  'use strict';

  // Число из текста: пусто -> null, запятая или точка как десятичный разделитель, мусор -> NaN
  function parseNum(txt) {
    if (txt === null || txt === undefined) return null;
    const t = String(txt).trim().replace(/\s/g, '').replace(',', '.');
    if (t === '') return null;
    if (!/^[+-]?(\d+\.?\d*|\.\d+)(e[+-]?\d+)?$/i.test(t)) return NaN;
    return Number(t);
  }

  // Значение в текст ячейки (null -> пусто)
  function fmt(v) { return v === null || v === undefined ? '' : String(v); }

  // Текст буфера обмена -> матрица строк (Excel даёт хвостовой перевод строки)
  function parseTsv(text) {
    let s = String(text === null || text === undefined ? '' : text).replace(/\r\n/g, '\n').replace(/\r/g, '\n');
    if (s.endsWith('\n')) s = s.slice(0, -1);
    return s.split('\n').map(line => line.split('\t'));
  }

  // Матрица значений -> текст с табуляциями и переводами строк
  function toTsv(rows) { return rows.map(r => r.map(fmt).join('\t')).join('\n'); }

  // Заполнение вниз: исходные строки повторяются по кругу на count строк
  function fillDown(srcRows, count) {
    const out = [];
    if (!srcRows.length) return out;
    for (let i = 0; i < count; i++) out.push(srcRows[i % srcRows.length].slice());
    return out;
  }

  // Вставка матрицы text в таблицу rows x cols начиная с (r, c); возвращает [{r, c, v}], выход за границу обрезается
  function pasteCells(matrix, r, c, rows, cols) {
    const out = [];
    matrix.forEach((line, i) => line.forEach((v, j) => {
      if (r + i < rows && c + j < cols) out.push({ r: r + i, c: c + j, v: String(v).trim() });
    }));
    return out;
  }

  // Delete: список ячеек диапазона с пустым значением
  function clearCells(r0, r1, c0, c1) {
    const out = [];
    for (let r = r0; r <= r1; r++) for (let c = c0; c <= c1; c++) out.push({ r, c, v: '' });
    return out;
  }

  // Совпадают ли два значения (текст черновика и сохранённое число или null)
  function sameValue(draft, saved) {
    const d = parseNum(draft);
    const s = saved === undefined ? null : saved;
    if (Number.isNaN(d)) return false;
    return d === s;
  }

  // Проверка строки теми же правилами, что на сервере; возвращает {поле: причина}
  function validateRow(row) {
    const err = {};
    const labels = { deadband: 'Мёртвая зона', limit_low: 'Нижний порог', limit_high: 'Верхний порог' };
    const v = {};
    Object.keys(labels).forEach(k => {
      v[k] = parseNum(row[k]);
      if (Number.isNaN(v[k])) err[k] = `${labels[k]}: нужно число (пусто: не задано)`;
    });
    if (!err.deadband && v.deadband !== null && v.deadband < 0) err.deadband = 'Мёртвая зона не может быть отрицательной';
    if (!err.limit_low && !err.limit_high && v.limit_low !== null && v.limit_high !== null && v.limit_low >= v.limit_high) {
      err.limit_low = 'Нижний порог должен быть меньше верхнего';
      err.limit_high = 'Верхний порог должен быть больше нижнего';
    }
    return err;
  }

  // ===== Флажки переменных: массовые операции (состояние строки {sel, archive, grafana, alarm}) =====
  const FLAG_COLS = ['archive', 'grafana', 'alarm'];

  // Новое состояние строки после установки столбца col в val: флаг включает выбор, снятие выбора гасит флаги
  function setFlag(state, col, val) {
    const s = Object.assign({ sel: false, archive: false, grafana: false, alarm: false }, state);
    val = !!val;
    s[col] = val;
    if (col === 'sel' && !val) FLAG_COLS.forEach(c => { s[c] = false; });
    if (col !== 'sel' && val) s.sel = true;
    return s;
  }

  // Диапазон между якорем и целью по порядку видимых строк: {from, to} (по возрастанию, в границах count) или null
  function rangeBetween(anchor, target, count) {
    if (!Number.isInteger(anchor) || !Number.isInteger(target) || anchor < 0 || target < 0 || anchor >= count || target >= count) return null;
    return { from: Math.min(anchor, target), to: Math.max(anchor, target) };
  }

  // Всем строкам from..to (включительно) в столбце col задаётся val; возвращает {states, changed: [индексы изменившихся]}
  function fillRange(states, from, to, col, val) {
    const out = states.slice();
    const changed = [];
    const lo = Math.max(0, from), hi = Math.min(states.length - 1, to);
    for (let i = lo; i <= hi; i++) {
      const next = setFlag(states[i], col, val);
      if (FLAG_COLS.concat('sel').some(c => next[c] !== !!states[i][c])) { out[i] = next; changed.push(i); }
    }
    return { states: out, changed };
  }

  // Протяжка: состояние столбца исходной строки src переносится на все строки между src и target
  function dragFill(states, src, target, col) {
    const r = rangeBetween(src, target, states.length);
    if (!r) return { states: states.slice(), changed: [] };
    return fillRange(states, r.from, r.to, col, !!states[src][col]);
  }

  // Shift+клик: строкам между якорем и целью задаётся val (новое состояние последнего клика)
  function shiftRange(states, anchor, target, col, val) {
    const r = rangeBetween(anchor, target, states.length);
    if (!r) return { states: states.slice(), changed: [] };
    return fillRange(states, r.from, r.to, col, val);
  }

  // «Выбрать все»: если уже все включены, снимает, иначе ставит всем
  function selectAll(states, col) {
    const val = !(states.length > 0 && states.every(s => !!s[col]));
    return Object.assign({ value: val }, fillRange(states, 0, states.length - 1, col, val));
  }

  // Сводка столбца для шапки: {total, checked, all, none}
  function columnSummary(states, col) {
    const checked = states.filter(s => !!s[col]).length;
    return { total: states.length, checked, all: states.length > 0 && checked === states.length, none: checked === 0 };
  }

  // Число с правильным окончанием для уведомления: «1 строка», «2 строки», «5 строк»
  function rowsWord(n) {
    const m10 = n % 10, m100 = n % 100;
    const w = m10 === 1 && m100 !== 11 ? 'строка' : (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14) ? 'строки' : 'строк');
    return `${n} ${w}`;
  }

  const api = { parseNum, fmt, parseTsv, toTsv, fillDown, pasteCells, clearCells, sameValue, validateRow,
    setFlag, rangeBetween, fillRange, dragFill, shiftRange, selectAll, columnSummary, rowsWord };
  root.xlgrid = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
