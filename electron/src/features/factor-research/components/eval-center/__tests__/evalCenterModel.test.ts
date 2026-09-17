import { describe, expect, it } from 'vitest';
import type { EvalScoreRow } from '../../../types/evalCenter';
import {
  coverageSummary,
  dimensionViews,
  gradeMeta,
  historySeries,
  radarEntries,
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
