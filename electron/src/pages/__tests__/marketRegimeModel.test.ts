/**
 * 大盘分析指数叠加口径：窗口首日归一为 100，缺失日 null（connectNulls）。
 */
import { describe, it, expect } from 'vitest';
import {
  rebaseIndexPoints,
  regimeIndexOptions,
  resolveScoreRows,
  type RegimeIndexSeries,
} from '../marketRegimeModel';

describe('regimeIndexOptions（按市场取候选，绝不跨市场拿 A 股指数）', () => {
  it('CN 默认沪深300', () => {
    expect(regimeIndexOptions('CN')[0]).toEqual({ code: '000300.SH', name: '沪深300' });
  });

  it('HK 只给恒生系，不含 000300.SH', () => {
    const codes = regimeIndexOptions('HK').map((x) => x.code);
    expect(codes).toContain('HSI.HK');
    expect(codes).toContain('HSTECH.HK');
    expect(codes).not.toContain('000300.SH');
  });

  it('US 只给美股指数，FUTURES 只给中证商品指数', () => {
    expect(regimeIndexOptions('US').map((x) => x.code)).not.toContain('000300.SH');
    expect(regimeIndexOptions('FUTURES')[0].code).toBe('100001.CCI');
  });

  it('未接入市场 → 空表（不回落 CN）', () => {
    expect(regimeIndexOptions('CRYPTO')).toEqual([]);
    expect(regimeIndexOptions('UNKNOWN')).toEqual([]);
  });
});

describe('rebaseIndexPoints', () => {
  it('首个有效日 = 100，后续按比例', () => {
    const out = rebaseIndexPoints(
      [
        { trade_date: '2026-09-01', close: 4000 },
        { trade_date: '2026-09-02', close: 4100 },
        { trade_date: '2026-09-03', close: 3800 },
      ],
      ['2026-09-01', '2026-09-02', '2026-09-03'],
    );
    expect(out).toEqual([100, 102.5, 95]);
  });

  it('日期缺失保持 null，不填充成 0', () => {
    const out = rebaseIndexPoints(
      [{ trade_date: '2026-09-02', close: 200 }],
      ['2026-09-01', '2026-09-02', '2026-09-03'],
    );
    expect(out).toEqual([null, 100, null]);
  });

  it('首个日期无数据时，以第一个有数据的日期为基准', () => {
    const out = rebaseIndexPoints(
      [
        { trade_date: '2026-09-02', close: 50 },
        { trade_date: '2026-09-03', close: 60 },
      ],
      ['2026-09-01', '2026-09-02', '2026-09-03'],
    );
    expect(out).toEqual([null, 100, 120]);
  });

  it('全无数据 → 全 null', () => {
    expect(rebaseIndexPoints([], ['2026-09-01', '2026-09-02'])).toEqual([null, null]);
  });

  it('非法收盘价（0/负数/NaN）视为缺失', () => {
    const out = rebaseIndexPoints(
      [
        { trade_date: '2026-09-01', close: 0 },
        { trade_date: '2026-09-02', close: 100 },
        { trade_date: '2026-09-03', close: Number.NaN },
      ],
      ['2026-09-01', '2026-09-02', '2026-09-03'],
    );
    expect(out).toEqual([null, 100, null]);
  });
});

describe('resolveScoreRows（单选指数用对应范围的均分）', () => {
  const allMarket = [
    { trade_date: '2026-09-01', avg_score: -0.01, median_score: -0.005, count: 5000 },
  ];
  const dates = ['2026-09-01', '2026-09-02'];
  const csi300: RegimeIndexSeries = {
    code: '000300.SH',
    name: '沪深300',
    scope: 'constituents',
    constituent_count: 300,
    points: [
      { trade_date: '2026-09-01', close: 100, avg_score: -0.02, median_score: -0.01, count: 300 },
      { trade_date: '2026-09-02', close: 101, avg_score: -0.03, median_score: -0.02, count: 300 },
    ],
  };

  it('单选有范围的指数 → 用该指数范围的均分', () => {
    const out = resolveScoreRows(allMarket, csi300, dates);
    expect(out.scoped).toBe(true);
    expect(out.rows[0].avg_score).toBe(-0.02);
    expect(out.rows[1].count).toBe(300);
  });

  it('scope=all_market → 回退全市场', () => {
    const out = resolveScoreRows(allMarket, { ...csi300, scope: 'all_market' }, dates);
    expect(out.scoped).toBe(false);
    expect(out.rows).toBe(allMarket);
  });

  it('单指数没有逐日均分 → 回退全市场（不画空线）', () => {
    const noStats: RegimeIndexSeries = {
      ...csi300,
      points: csi300.points.map((p) => ({ ...p, avg_score: null })),
    };
    expect(resolveScoreRows(allMarket, noStats, dates).scoped).toBe(false);
  });

  it('未单选（null）→ 全市场', () => {
    expect(resolveScoreRows(allMarket, null, dates).rows).toBe(allMarket);
  });
});
