import { describe, expect, it } from 'vitest';
import type { EvalScoreRow } from '../../types/evalCenter';
import {
  averageScore,
  coverageSummary,
  dimensionViews,
  formatNumber,
  formatPercent,
  formatSignedPct,
  gradeColor,
  gradeCounts,
  gradeMeta,
  historySeries,
  radarEntries,
  rowLabels,
} from '../evalCenterModel';

function makeRow(overrides: Partial<EvalScoreRow> = {}): EvalScoreRow {
  return {
    object_type: 'model',
    object_id: 'model_qlib',
    snapshot_date: '2026-09-16',
    score: 72,
    grade: 'B',
    low_confidence: false,
    red_line_failed: [],
    dimensions: {},
    inputs_version: {},
    created_at: '2026-09-16T13:00:00',
    ...overrides,
  };
}

describe('gradeMeta 评级徽章', () => {
  it('A/B/C/D 各自映射标签与样式，低置信 † 单独标注', () => {
    expect(gradeMeta('A').label).toContain('A');
    expect(gradeMeta('A').className).toContain('red');
    expect(gradeMeta('D').className).toContain('slate');
    expect(gradeMeta('C', true).isLowConfidence).toBe(true);
    expect(gradeMeta('C†').isLowConfidence).toBe(true);
    expect(gradeMeta('L').label).toContain('运气');
    expect(gradeMeta('E').label).toContain('证据不足');
  });

  it('空评级回退"未评级"而不抛错', () => {
    expect(gradeMeta(null).label).toBe('未评级');
    expect(gradeMeta(undefined).className).toContain('slate');
  });
});

describe('dimensionViews 维度明细', () => {
  it('区分可评/缺省/红线，缺省携带 note 如实展示', () => {
    const row = makeRow({
      dimensions: {
        predictive: { label: '预测力', score: 73.3, weight: 30 },
        quality_gate: {
          label: '质量闸门',
          score: null,
          weight: 20,
          detail: { insufficient: true, note: 'PFS/DH 未落库' },
        },
        risk: {
          label: '风险',
          score: 20,
          weight: 20,
          red_line_failed: true,
          detail: { red_line: 'MDD ≤ -50%' },
        },
      },
    });
    const views = dimensionViews(row);
    expect(views).toHaveLength(3);
    const byKey = Object.fromEntries(views.map((v) => [v.key, v]));
    expect(byKey.predictive.score).toBe(73.3);
    expect(byKey.quality_gate.score).toBeNull();
    expect(byKey.quality_gate.note).toContain('PFS/DH');
    expect(byKey.risk.redLine).toBe(true);
    expect(byKey.risk.note).toContain('MDD');

    expect(dimensionViews(null)).toEqual([]);
  });
});

describe('radarEntries 雷达数据', () => {
  it('有效维度 <3 → null（不画残缺雷达）', () => {
    const row = makeRow({
      dimensions: {
        a: { label: 'A', score: 80, weight: 50 },
        b: { label: 'B', score: null, weight: 50, detail: { insufficient: true } },
      },
    });
    expect(radarEntries(row)).toBeNull();
  });

  it('≥3 个有效维度 → 值保留一位小数', () => {
    const row = makeRow({
      dimensions: {
        a: { label: 'A', score: 80.123, weight: 30 },
        b: { label: 'B', score: 60, weight: 30 },
        c: { label: 'C', score: 0, weight: 40 },
      },
    });
    const entries = radarEntries(row);
    expect(entries).not.toBeNull();
    expect(entries!.map((e) => e.name)).toEqual(['A', 'B', 'C']);
    expect(entries![0].value).toBe(80.1);
    expect(entries![2].value).toBe(0); // 0 分是有效值（不是缺省）
  });
});

describe('historySeries / coverageSummary', () => {
  it('历史序列按输入顺序取日期/分数/评级', () => {
    const series = historySeries([
      makeRow({ snapshot_date: '2026-09-15', score: 60, grade: 'C' }),
      makeRow({ snapshot_date: '2026-09-16', score: null, grade: 'D' }),
    ]);
    expect(series.dates).toEqual(['2026-09-15', '2026-09-16']);
    expect(series.scores).toEqual([60, null]);
    expect(series.grades).toEqual(['C', 'D']);
    expect(historySeries(null).dates).toEqual([]);
  });

  it('覆盖率摘要：维度计数 + 红线条数', () => {
    const row = makeRow({
      dimensions: {
        a: { score: 80, weight: 50, label: 'A' },
        b: { score: null, weight: 30, label: 'B', detail: { insufficient: true } },
        c: { score: 10, weight: 20, label: 'C', red_line_failed: true },
      },
    });
    expect(coverageSummary(row)).toBe('维度 2/3 · 红线 1');
    expect(coverageSummary(null)).toBe('维度 0/0');
  });
});

describe('gradeColor / gradeCounts / averageScore / rowLabels（2026-09-17 改版）', () => {
  it('gradeColor：A 红 B 蓝 C 橙 D 灰（A股口径），† 归并同档，未知回退', () => {
    expect(gradeColor('A')).toBe('#dc2626');
    expect(gradeColor('b')).toBe('#2563eb');
    expect(gradeColor('C†')).toBe('#ea580c');
    expect(gradeColor('D')).toBe('#64748b');
    expect(gradeColor(null)).toBe('#6366f1');
  });

  it('gradeCounts：按 A/B/C/D/L/E 固定顺序计数，未评级归 ?，零值不出现', () => {
    const rows = [
      makeRow({ grade: 'A' }),
      makeRow({ grade: 'A' }),
      makeRow({ grade: 'B†' }),
      makeRow({ grade: 'L' }),
      makeRow({ grade: null }),
    ];
    expect(gradeCounts(rows)).toEqual([
      { grade: 'A', count: 2 },
      { grade: 'B', count: 1 },
      { grade: 'L', count: 1 },
      { grade: '?', count: 1 },
    ]);
    expect(gradeCounts(null)).toEqual([]);
  });

  it('averageScore：只统计有效分数，一位小数；无 → null', () => {
    expect(averageScore([makeRow({ score: 60 }), makeRow({ score: 80.25 }), makeRow({ score: null })])).toBe(70.1);
    expect(averageScore([])).toBeNull();
    expect(averageScore(null)).toBeNull();
  });

  it('rowLabels：display_name 优先并保留原 id 副标题；无名字按类型兜底', () => {
    const named = rowLabels(makeRow({ object_type: 'factor', object_id: 'a158_LOW0', display_name: '最低相对收盘' }));
    expect(named).toEqual({ primary: '最低相对收盘', secondary: 'a158_LOW0' });

    const strat = rowLabels(makeRow({ object_type: 'strategy', object_id: '93085758a7284ad3ba447a6a9c9ace21', display_name: null }));
    expect(strat.primary).toBe('策略回测 93085758…');
    expect(strat.secondary).toBe('93085758a7284ad3ba447a6a9c9ace21');

    const daily = rowLabels(makeRow({ object_type: 'daily_selection', object_id: '2026-09-15', display_name: null }));
    expect(daily).toEqual({ primary: '2026-09-15', secondary: null });
  });
});

describe('数值格式化（实测量与曲线标注共用口径）', () => {
  it('formatNumber：按位数四舍五入，缺值给「—」而不是 0', () => {
    expect(formatNumber(0.0412, 4)).toBe('0.0412');
    expect(formatNumber(0.04125, 4)).toBe('0.0413');
    expect(formatNumber(-1.12, 2)).toBe('-1.12');
    expect(formatNumber(null)).toBe('—');
    expect(formatNumber(undefined)).toBe('—');
    expect(formatNumber(Number.NaN)).toBe('—');
  });

  it('formatPercent：小数 → 百分数（默认带符号，正数带 +）', () => {
    expect(formatPercent(0.724, 1)).toBe('72.4%');
    expect(formatPercent(0.625, 1)).toBe('62.5%');
    expect(formatPercent(0.66, 0)).toBe('66%');
    expect(formatPercent(null)).toBe('—');
  });

  it('formatSignedPct：收益率带正负号（A 股口径：正=涨/红）', () => {
    expect(formatSignedPct(0.183)).toBe('+18.3%');
    expect(formatSignedPct(-0.081)).toBe('-8.1%');
    expect(formatSignedPct(0)).toBe('0.0%');
    expect(formatSignedPct(null)).toBe('—');
  });
});
