/**
 * CSV 导出公共工具。
 *
 * 股票代码列防 Excel 数值化：000/002/300 开头的纯数字代码（如 002661、000001）
 * 会被 Excel/WPS 当数字解析丢前导零。用 ="002661" 公式包装强制按文本解析。
 *
 * 免责段（生成时间 / 数据区间 / 不构成投资建议）由 `exportDisclaimer` 单源提供，
 * 本模块只负责按 CSV 规则渲染它 —— 措辞不在这里写第二遍。
 */

import { disclaimerRows } from './exportDisclaimer';

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
  /** 文件名。本函数不读它（下载名由 `downloadCsvFile` 收）；保留供调用方一站传参。 */
  filename?: string;
  /** 数据覆盖区间（人读文本）。拿不到就别传 —— 免责段会省略该行，不编造。 */
  dataRange?: string;
  /** 覆盖免责段的「生成时间」，仅供测试固定时钟用。 */
  now?: Date;
}

/**
 * 组装 CSV 文本（带 BOM，Excel 直开不乱码）。
 *
 * **免责段追加在尾部**（数据之后、空行隔开），不在头部：
 * 首行是表头是 CSV 的硬约定，任何消费方（含本项目自己的按列取数）都靠它，
 * 把免责段顶到前面会让「第一行是列名」失效。代价是尾部多出几行两列的行 ——
 * 这是「文件离开应用后仍带着免责」必须付的成本，见 `exportDisclaimer` 的模块说明。
 */
export function buildCsvText(header: unknown[], rows: unknown[][], opts?: CsvExportOptions): string {
  const textCols = new Set(opts?.textColumns ?? []);
  const lines = [
    header.map(c => escapeCsvCell(c)).join(','),
    ...rows.map(row => row.map((cell, col) => (textCols.has(col) ? forceTextCsvCell(cell) : escapeCsvCell(cell))).join(',')),
    // 空行分隔数据与免责段：多数解析器会跳过空行，人来读也一眼看得出数据到此为止
    '',
    ...disclaimerRows(opts).map(([label, value]) => `${escapeCsvCell(label)},${escapeCsvCell(value)}`),
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
