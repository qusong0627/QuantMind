/**
 * 导出件免责段：单源、必须随文件走、不编造区间。
 *
 * 这些断言的存在理由：屏幕上的免责条（ComplianceChrome）只在应用内可见，
 * 而导出件一旦落盘或转发就脱离了那个上下文 —— 收件人看到的是一张「AI 选出来的
 * 股票表」，却看不到「这不构成投资建议」。所以免责段是**文件的一部分**，
 * 不是可选的装饰，下面的用例把它钉在构建函数里。
 */

import { readFileSync } from 'node:fs';
import path from 'node:path';
import { describe, it, expect } from 'vitest';
import ExcelJS from 'exceljs';

import {
  DISCLAIMER_LABELS,
  DISCLAIMER_SENTENCE,
  dataRangeRow,
  disclaimerRows,
  formatExportTime,
} from '../exportDisclaimer';
import { buildCsvText } from '../csvExport';
import { appendDisclaimerSheet } from '../excelExport';

/**
 * 金样与后端 `backend/tests/test_export_disclaimer.py` 共用同一份 JSON，
 * 存在**后端包内**（`backend/tests/fixtures/exportDisclaimerGolden.json`）：
 * 后端测试跑在容器里，只挂了 `./backend`，金样放在后端侧两边才都读得到
 * （与 `researchScoreGolden.json` 同一考虑）。
 *
 * 直接读文件而不是 `import`：金样在 `electron/` 之外，走模块解析会碰到 vite 的
 * root 限制。用 `__dirname` 定位，与 cwd 无关。
 */
const GOLDEN_PATH = path.resolve(
  __dirname,
  '../../../..', // electron/src/utils/__tests__ → 仓库根
  'backend/tests/fixtures/exportDisclaimerGolden.json',
);
const GOLDEN: {
  sentence: string;
  labels: Record<string, string>;
  requiredPhrases: string[];
} = JSON.parse(readFileSync(GOLDEN_PATH, 'utf-8'));

const FIXED = new Date(2026, 8, 21, 14, 32, 5); // 2026-09-21 14:32:05（本地时区）

describe('金样对拍 —— 前端措辞必须等于后端共读的那一份', () => {
  it('免责语句逐字相同（含标点）', () => {
    expect(DISCLAIMER_SENTENCE).toBe(GOLDEN.sentence);
  });

  it('标签同名同值（否则 CSV/Excel 两侧叫法会漂）', () => {
    expect({ ...DISCLAIMER_LABELS }).toEqual(GOLDEN.labels);
  });

  it.each(GOLDEN.requiredPhrases)('保留合规底线字眼：%s', (phrase) => {
    // 不是重复上一条：上面钉「与金样一致」，这条钉「金样本身没被改成
    // 一句不再免责的话」。两者一起改时，对拍会绿而这条会红。
    expect(DISCLAIMER_SENTENCE).toContain(phrase);
  });
});

describe('disclaimerRows — 生成时间与免责语句是必要成分', () => {
  it('总是带生成时间与不构成投资建议的语句', () => {
    const rows = disclaimerRows({ now: FIXED });
    const labels = rows.map(([k]) => k);
    const values = rows.map(([, v]) => v).join('\n');

    expect(labels).toContain('生成时间');
    expect(values).toContain('2026-09-21 14:32:05');
    expect(values).toContain(DISCLAIMER_SENTENCE);
    expect(values).toContain('不构成任何投资建议');
  });

  it('没有数据区间时**不出现**区间行 —— 绝不编造一个假区间', () => {
    const labels = disclaimerRows({ now: FIXED }).map(([k]) => k);
    expect(labels).not.toContain('数据区间');
  });

  it('给了数据区间才出现区间行', () => {
    const rows = disclaimerRows({ now: FIXED, dataRange: '2026-01-01 ~ 2026-09-18' });
    const range = rows.find(([k]) => k === '数据区间');
    expect(range?.[1]).toBe('2026-01-01 ~ 2026-09-18');
  });

  it('数据区间为空串/空白时同样不出现 —— 空值不得伪装成「有区间」', () => {
    for (const blank of ['', '   ']) {
      const labels = disclaimerRows({ now: FIXED, dataRange: blank }).map(([k]) => k);
      expect(labels).not.toContain('数据区间');
    }
  });
});

describe('formatExportTime — 人读且可排序', () => {
  it('输出 YYYY-MM-DD HH:mm:ss', () => {
    expect(formatExportTime(FIXED)).toBe('2026-09-21 14:32:05');
  });

  it('月/日/时/分/秒补零', () => {
    expect(formatExportTime(new Date(2026, 0, 2, 3, 4, 5))).toBe('2026-01-02 03:04:05');
  });
});

describe('buildCsvText — 免责段追加在尾部，不是头部', () => {
  const header = ['代码', '得分'];
  const rows = [['SH600036', 88], ['SZ000001', 71]];

  it('首行仍是表头（不得因免责段破坏「首行是列名」的约定）', () => {
    const csv = buildCsvText(header, rows);
    const firstLine = csv.replace(/^﻿/, '').split('\n')[0];
    expect(firstLine).toBe('代码,得分');
  });

  it('数据行原样保留且仍在免责段之前', () => {
    const csv = buildCsvText(header, rows);
    const lines = csv.replace(/^﻿/, '').split('\n');
    const dataLineIdx = lines.findIndex((l) => l.includes('SH600036'));
    const timeIdx = lines.findIndex((l) => l.startsWith('生成时间,'));
    expect(dataLineIdx).toBeGreaterThan(-1);
    expect(timeIdx).toBeGreaterThan(dataLineIdx);
    expect(lines.filter((l) => l.includes('SH600036')).length).toBe(1);
  });

  it('BOM 仍在最前（Excel 直开不乱码）', () => {
    expect(buildCsvText(header, rows).startsWith('﻿')).toBe(true);
  });

  it('免责段每个字段都成列 —— 值里有逗号也不破列', () => {
    const csv = buildCsvText(header, rows, {
      dataRange: '2026-01-01, 2026-09-18',
    });
    const lines = csv.replace(/^﻿/, '').split('\n');
    const rangeLine = lines.find((l) => l.startsWith('数据区间,'));
    expect(rangeLine).toBeDefined();
    // 带逗号的区间必须被引号包裹，否则会被解析成三列
    expect(rangeLine).toBe('数据区间,"2026-01-01, 2026-09-18"');
  });

  it('文本列包装（防丢前导零）不受免责段影响', () => {
    const csv = buildCsvText(header, [['000001', 71]], { textColumns: [0] });
    expect(csv).toContain('"=""000001""",71');
  });
});

describe('appendDisclaimerSheet — Excel 独立表，不污染数据表', () => {
  it('新建「免责声明」表并原样保留已有表', () => {
    const wb = new ExcelJS.Workbook();
    wb.addWorksheet('交易明细').addRow(['日期', '价格']);

    appendDisclaimerSheet(wb, { now: FIXED, dataRange: '2026-01-01 ~ 2026-09-18' });

    expect(wb.worksheets.map((s) => s.name)).toEqual(['交易明细', '免责声明']);
    const sheet = wb.getWorksheet('免责声明')!;
    const flat = sheet
      .getSheetValues()
      .flat()
      .filter(Boolean)
      .join('\n');
    expect(flat).toContain('生成时间');
    expect(flat).toContain('2026-09-21 14:32:05');
    expect(flat).toContain('2026-01-01 ~ 2026-09-18');
    expect(flat).toContain('不构成任何投资建议');
  });

  it('数据表一个单元格都不改（免责段只进新表）', () => {
    const wb = new ExcelJS.Workbook();
    const data = wb.addWorksheet('指标摘要');
    data.addRow(['总收益率', '12.00%']);

    appendDisclaimerSheet(wb, { now: FIXED });

    expect(data.getSheetValues().flat().filter(Boolean)).toEqual(['总收益率', '12.00%']);
  });

  it('重复调用不报错（幂等：已有同名表则复用）', () => {
    const wb = new ExcelJS.Workbook();
    appendDisclaimerSheet(wb, { now: FIXED });
    appendDisclaimerSheet(wb, { now: FIXED });
    expect(wb.worksheets.filter((s) => s.name === '免责声明').length).toBe(1);
  });
});

describe('dataRangeRow — 区间文本的构造', () => {
  it('两端都给才成形', () => {
    expect(dataRangeRow('2026-01-01', '2026-09-18')).toBe('2026-01-01 ~ 2026-09-18');
  });

  it('缺一端时退化为单端，不写「~ 」这种半截区间', () => {
    expect(dataRangeRow('2026-01-01', null)).toBe('2026-01-01 起');
    expect(dataRangeRow(null, '2026-09-18')).toBe('截至 2026-09-18');
  });

  it('两端都缺则给 undefined —— 交由调用方走「不出现区间行」', () => {
    expect(dataRangeRow(null, null)).toBeUndefined();
  });
});
