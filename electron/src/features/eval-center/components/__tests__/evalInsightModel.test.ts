import { describe, expect, it } from 'vitest';
import type { EvalScoreRow } from '../../types/evalCenter';
import {
  costCompare,
  dimensionCoverage,
  evidenceFootnote,
  insightFor,
  overviewStats,
} from '../evalInsightModel';

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

describe('dimensionCoverage 维度覆盖格', () => {
  it('按评分卡口径的固定顺序列格（jsonb 回来是字母序，不能照抄）', () => {
    // Arrange：键序故意打乱成 PG jsonb 的字母序
    const row = makeRow({
      object_type: 'factor',
      dimensions: {
        coverage: { label: '覆盖', score: 60, weight: 15 },
        independence: { label: '独立性', score: 50, weight: 15 },
        predictive: { label: '预测力', score: 80, weight: 30 },
        quality_gate: {
          label: '质量闸门',
          score: null,
          weight: 20,
          detail: { insufficient: true, note: 'PFS 未落库，本期不可评' },
        },
        stability: { label: '稳定性', score: 70, weight: 20 },
      },
    });

    // Act
    const cov = dimensionCoverage(row);

    // Assert
    expect(cov.cells.map((c) => c.key)).toEqual([
      'predictive',
      'stability',
      'independence',
      'quality_gate',
      'coverage',
    ]);
    expect(cov.scored).toBe(4);
    expect(cov.total).toBe(5);
    expect(cov.rate).toBeCloseTo(0.8);
    expect(cov.cells[3].scored).toBe(false);
    expect(cov.cells[3].note).toBe('PFS 未落库，本期不可评');
  });

  it('未知类型按权重降序（同权重按 key）排，缺省格仍带 note', () => {
    const row = makeRow({
      object_type: 'mystery_card',
      dimensions: {
        c: { label: 'C', score: 2, weight: 10 },
        a: { label: 'A', score: null, weight: 50, detail: { note: '数据源缺失' } },
        b: { label: 'B', score: 1, weight: 10 },
      },
    });

    const cov = dimensionCoverage(row);

    expect(cov.cells.map((c) => c.key)).toEqual(['a', 'b', 'c']);
    expect(cov.cells[0].note).toBe('数据源缺失');
  });

  it('红线格单独标出；空行不抛错', () => {
    const row = makeRow({
      object_type: 'model',
      dimensions: {
        oos_predictive: {
          label: '样本外预测力',
          score: 20,
          weight: 30,
          red_line_failed: true,
          detail: { red_line: '实测 RankIC ≤ 0' },
        },
      },
    });

    const cov = dimensionCoverage(row);
    expect(cov.cells[0].redLine).toBe(true);
    expect(cov.cells[0].note).toContain('RankIC');

    expect(dimensionCoverage(null)).toEqual({ cells: [], scored: 0, total: 0, rate: 0 });
  });
});

describe('insightFor 关键实测量（缺证据必带原因，不许留白）', () => {
  it('模型 → 实测 RankIC（带上 ICIR 口径）', () => {
    const row = makeRow({
      object_type: 'model',
      dimensions: {
        oos_predictive: {
          label: '样本外预测力',
          score: 80,
          weight: 30,
          detail: { test_rank_ic: 0.0412, test_rank_icir: 0.63 },
        },
      },
    });

    const insight = insightFor(row)!;

    expect(insight.label).toBe('实测 RankIC');
    expect(insight.value).toBe('0.0412');
    expect(insight.tone).toBe('pos');
    expect(insight.missing).toBe(false);
    expect(insight.hint).toContain('0.63');
  });

  it('因子 → ICIR；负 IC 的强因子标负色并说明「反转即可用」', () => {
    const row = makeRow({
      object_type: 'factor',
      dimensions: {
        predictive: {
          label: '预测力',
          score: 74,
          weight: 30,
          detail: { mean_ic: -0.052, icir: -1.12, direction: 'inverted' },
        },
      },
    });

    const insight = insightFor(row)!;

    expect(insight.label).toBe('ICIR');
    expect(insight.value).toBe('-1.12');
    expect(insight.tone).toBe('neg');
    expect(insight.hint).toContain('反转');
  });

  it('策略 → 年化 / 最大回撤（带回超额口径）', () => {
    const row = makeRow({
      object_type: 'strategy',
      dimensions: {
        return: {
          label: '收益',
          score: 78,
          weight: 20,
          detail: { annual_return: 0.183, benchmark_annual: 0.05, excess_annual: 0.133 },
        },
        risk: { label: '风险', score: 70, weight: 20, detail: { max_drawdown: -0.081 } },
      },
    });

    const insight = insightFor(row)!;

    expect(insight.label).toBe('年化 / 最大回撤');
    expect(insight.value).toBe('+18.3% / -8.1%');
    expect(insight.tone).toBe('pos');
    expect(insight.hint).toContain('超额');
  });

  it('策略浮亏 → 负色（A 股口径：绿=跌）', () => {
    const row = makeRow({
      object_type: 'strategy',
      dimensions: {
        return: { label: '收益', score: 30, weight: 20, detail: { annual_return: -0.12 } },
      },
    });

    expect(insightFor(row)!.tone).toBe('neg');
    expect(insightFor(row)!.value).toContain('-12.0%');
  });

  it('账户 → 资金利用率', () => {
    const row = makeRow({
      object_type: 'account',
      dimensions: {
        capital_efficiency: {
          label: '资金效率',
          score: 66,
          weight: 25,
          detail: { utilization: 0.724, cash_ratio: 0.276 },
        },
      },
    });

    const insight = insightFor(row)!;

    expect(insight.label).toBe('资金利用率');
    expect(insight.value).toBe('72.4%');
  });

  it('每日选股待回填 → 独立 pending 态，原因原样带出', () => {
    const note = 'T+1..T+5 前向数据未齐，待回填（不假填）';
    const row = makeRow({
      object_type: 'daily_selection',
      dimensions: {
        realized: {
          label: '事后验证',
          score: null,
          weight: 35,
          detail: { horizon: 5, pending: true, note },
        },
      },
    });

    const insight = insightFor(row)!;

    expect(insight.label).toBe('T+5 超额');
    expect(insight.value).toBe('待回填');
    expect(insight.missing).toBe(true);
    expect(insight.tone).toBe('pending');
    expect(insight.hint).toBe(note);
  });

  it('每日选股已回填 → 报平均超额与命中率', () => {
    const row = makeRow({
      object_type: 'daily_selection',
      dimensions: {
        realized: {
          label: '事后验证',
          score: 71,
          weight: 35,
          detail: { horizon: 5, n: 8, mean_excess: 0.0124, hit_rate: 0.625 },
        },
      },
    });

    const insight = insightFor(row)!;

    expect(insight.label).toBe('T+5 超额');
    expect(insight.value).toBe('+1.24%');
    expect(insight.hint).toContain('62.5%');
    expect(insight.missing).toBe(false);
  });

  it('维度在但 detail 缺该量 → 仍给位置 + 原因（不消失）', () => {
    const row = makeRow({
      object_type: 'factor',
      dimensions: {
        predictive: {
          label: '预测力',
          score: null,
          weight: 30,
          detail: { insufficient: true, note: 'IC 序列 < 20' },
        },
      },
    });

    const insight = insightFor(row)!;

    expect(insight.missing).toBe(true);
    expect(insight.value).toBe('缺省');
    expect(insight.hint).toBe('IC 序列 < 20');
  });

  it('体检卡与未知类型没有「一个实测量」这一列 → null', () => {
    expect(insightFor(makeRow({ object_type: 'strategy_health' }))).toBeNull();
    expect(insightFor(makeRow({ object_type: 'unknown_thing' }))).toBeNull();
    expect(insightFor(null)).toBeNull();
  });
});

describe('overviewStats 总览带', () => {
  const rows = [
    makeRow({
      object_id: 'a',
      score: 88,
      grade: 'A',
      dimensions: {
        oos_predictive: { label: 'P', score: 80, weight: 30 },
        stratification: { label: 'S', score: 70, weight: 25 },
        robustness: { label: 'R', score: 60, weight: 15 },
      },
    }),
    makeRow({
      object_id: 'b',
      score: 64.5,
      grade: 'C',
      red_line_failed: ['滚动健康'],
      dimensions: {
        oos_predictive: { label: 'P', score: 50, weight: 30, red_line_failed: true },
        stratification: { label: 'S', score: null, weight: 25, detail: { note: '不足' } },
      },
    }),
    makeRow({
      object_id: 'c',
      score: null,
      grade: null,
      dimensions: {
        oos_predictive: { label: 'P', score: null, weight: 30, detail: { note: '无' } },
      },
    }),
  ];

  it('计数 / 均分 / 红线对象数 / 实证对象数（≥3 维有分）一起给', () => {
    const stats = overviewStats(rows);

    expect(stats.total).toBe(3);
    expect(stats.avg).toBe(76.3);
    expect(stats.redLineCount).toBe(1);
    expect(stats.evidenceObjects).toBe(1);
    expect(stats.coverageRate).toBeCloseTo(1 / 3);
    expect(stats.note).toBe('本类 3 个对象中 1 个有 ≥3 维实证');
  });

  it('空列表不抛错，note 说明本类无对象', () => {
    const stats = overviewStats([]);

    expect(stats.total).toBe(0);
    expect(stats.avg).toBeNull();
    expect(stats.coverageRate).toBe(0);
    expect(stats.note).toContain('无对象');
    expect(overviewStats(null).total).toBe(0);
  });
});

describe('costCompare 换手 × 成本对比条', () => {
  it('毛利 / 成本拖累 / 净利三条，宽度按最大绝对值归一，成本拖累固定风险色', () => {
    const row = makeRow({
      object_type: 'model',
      dimensions: {
        turnover_cost: {
          label: '换手与成本',
          score: 60,
          weight: 15,
          detail: {
            gross_annual: 0.4,
            cost_drag_annual: -0.1,
            net_annual: 0.3,
            turnover_mean: 0.25,
            round_trip_cost: 0.002,
            top_k: 50,
          },
        },
      },
    });

    const { bars, note } = costCompare(row);

    expect(bars.map((b) => b.label)).toEqual(['毛利', '成本拖累', '净利']);
    expect(bars[0].width).toBe(1);
    expect(bars[1].width).toBeCloseTo(0.25);
    expect(bars[0].tone).toBe('pos');
    expect(bars[1].tone).toBe('risk');
    expect(note).toContain('25.0%');
    expect(note).toContain('Top50');
  });

  it('成本后为负 → 净利标负色（不是「成本拖累」那种风险色）', () => {
    const row = makeRow({
      object_type: 'model',
      dimensions: {
        turnover_cost: {
          label: '换手与成本',
          score: 0,
          weight: 15,
          detail: { gross_annual: 0.05, cost_drag_annual: -0.12, net_annual: -0.07 },
        },
      },
    });

    const { bars } = costCompare(row);
    expect(bars[2].tone).toBe('neg');
    expect(bars[2].width).toBeCloseTo(0.07 / 0.12);
  });

  it('该维缺省 → 条为空 + note 说明原因（不画三条 0 长条假装算过）', () => {
    const row = makeRow({
      object_type: 'model',
      dimensions: {
        turnover_cost: {
          label: '换手与成本',
          score: null,
          weight: 15,
          detail: { insufficient: true, note: '无换手证据或毛利不可得：成本后收益无从计算' },
        },
      },
    });

    const { bars, note } = costCompare(row);

    expect(bars).toEqual([]);
    expect(note).toContain('无换手证据');
  });
});

describe('evidenceFootnote 页脚（来源 / 口径 / 时间戳）', () => {
  it('因子卡：带出数据集、缺省维清单与快照时间戳', () => {
    const row = makeRow({
      object_type: 'factor',
      inputs_version: { dataset: 'alpha_library', weights: { predictive: 30 } },
      dimensions: {
        predictive: { label: '预测力', score: 80, weight: 30 },
        independence: {
          label: '独立性',
          score: null,
          weight: 15,
          detail: { insufficient: true, note: '相关性矩阵缺该因子' },
        },
      },
    });

    const foot = evidenceFootnote(row);

    expect(foot.source).toBe('alpha_library');
    expect(foot.caliber).toContain('1/2');
    expect(foot.caliber).toContain('权重已归一');
    expect(foot.asOf).toContain('2026-09-16');
    expect(foot.missing).toContain('独立性');
  });

  it('无 inputs_version 时回退到表名，无缺省维时说「无」', () => {
    const row = makeRow({
      inputs_version: {},
      dimensions: { oos_predictive: { label: 'P', score: 80, weight: 30 } },
    });

    const foot = evidenceFootnote(row);

    expect(foot.source).toBe('eval_scores');
    expect(foot.missing).toBe('无');
  });
});
