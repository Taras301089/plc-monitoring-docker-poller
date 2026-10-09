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

  const api = { parseNum, fmt, parseTsv, toTsv, fillDown, pasteCells, clearCells, sameValue, validateRow };
  root.xlgrid = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
