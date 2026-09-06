/**
 * CSV 导出公共工具。
 *
 * 股票代码列防 Excel 数值化：000/002/300 开头的纯数字代码（如 002661、000001）
 * 会被 Excel/WPS 当数字解析丢前导零。用 ="002661" 公式包装强制按文本解析。
 */

/** CSV 单元格转义：含逗号/引号/换行时加引号包裹。 */
export function escapeCsvCell(value: unknown): string {
  const raw = value === null || value === undefined ? '' : String(value);
  if (!/[",\n\r]/.test(raw)) return raw;
  return `"${raw.replace(/"/g, '""')}"`;
}

/** 强制文本单元格（防 Excel 丢前导零），用于代码/单号等标识列。 */
export function forceTextCsvCell(value: unknown): string {
  const raw = value === null || value === undefined ? '' : String(value);
  return escapeCsvCell(`="${raw}"`);
}

export interface CsvExportOptions {
  /** 强制按文本导出的列下标集合（0-based），如股票代码列。 */
  textColumns?: number[];
  /** 文件名（不含扩展名部分由调用方传入完整名）。 */
  filename: string;
}

/** 组装 CSV 文本（带 BOM，Excel 直开不乱码）。 */
export function buildCsvText(header: unknown[], rows: unknown[][], opts?: CsvExportOptions): string {
  const textCols = new Set(opts?.textColumns ?? []);
  const lines = [
    header.map(c => escapeCsvCell(c)).join(','),
    ...rows.map(row => row.map((cell, col) => (textCols.has(col) ? forceTextCsvCell(cell) : escapeCsvCell(cell))).join(',')),
  ];
  return '﻿' + lines.join('\n');
}

/** 下载 CSV 并返回是否成功。 */
export function downloadCsvFile(csvText: string, filename: string): boolean {
  try {
    const blob = new Blob([csvText], { type: 'text/csv;charset=utf-8;' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    a.style.display = 'none';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    return true;
  } catch {
    return false;
  }
}
