import { describe, expect, it } from 'vitest';
import { computeDrawdownSeries, computeRangeStats, formatPercent } from '../equityStats';

describe('computeDrawdownSeries（回撤水下曲线）', () => {
  it('解析解：峰值 120 → 谷值 90 的回撤 = -25%', () => {
    const dd = computeDrawdownSeries([100, 120, 90, 110]);
    expect(dd[0]).toBeCloseTo(0, 10);
    expect(dd[1]).toBeCloseTo(0, 10);
    expect(dd[2]).toBeCloseTo(90 / 120 - 1, 10);
    expect(dd[3]).toBeCloseTo(110 / 120 - 1, 10);
    expect(Math.min(...dd)).toBeCloseTo(-0.25, 10);
  });

  it('单调上涨 → 全 0；非法值不污染', () => {
    expect(computeDrawdownSeries([1, 2, 3]).every((v) => v === 0)).toBe(true);
    const dd = computeDrawdownSeries([100, NaN, 50]);
    expect(dd[1]).toBe(0);
    expect(dd[2]).toBeCloseTo(-0.5, 10);
  });
});

describe('computeRangeStats（区间统计）', () => {
  it('恒定日收益 +1%：年化 = 1.01^n 拉伸；波动≈0 → 夏普 null（不硬算）', () => {
    const values = Array.from({ length: 253 }, (_, i) => Math.pow(1.01, i));
    const stats = computeRangeStats(values);
    expect(stats.points).toBe(253);
    expect(stats.totalReturn).toBeCloseTo(Math.pow(1.01, 252) - 1, 6);
    expect(stats.annualized).toBeCloseTo(Math.pow(1.01, 252) - 1, 6); // 253 点 ≈ 252 个收益 = 1 年
    expect(stats.volatility).toBeCloseTo(0, 8);
    expect(stats.sharpe).toBeNull(); // std=0 → 不硬算
    expect(stats.maxDrawdown).toBeCloseTo(0, 10);
  });

  it('基准对比与超额：策略 +10% vs 基准 +4% → 超额 +6pp', () => {
    const stats = computeRangeStats([100, 110], [100, 104]);
    expect(stats.totalReturn).toBeCloseTo(0.1, 10);
    expect(stats.benchmarkReturn).toBeCloseTo(0.04, 10);
    expect(stats.excess).toBeCloseTo(0.06, 10);
  });

  it('样本 <2 或首值非正 → 全 null（不画假线）', () => {
    const one = computeRangeStats([100]);
    expect(one.totalReturn).toBeNull();
    expect(one.maxDrawdown).toBeNull();
    const bad = computeRangeStats([0, 10]);
    expect(bad.totalReturn).toBeNull();
    expect(computeRangeStats([]).sharpe).toBeNull();
  });

  it('最大回撤取全区间最小值（含深回撤场景）', () => {
    const stats = computeRangeStats([100, 150, 75, 120]);
    expect(stats.maxDrawdown).toBeCloseTo(75 / 150 - 1, 10);
  });
});

describe('formatPercent', () => {
  it('容错：null/NaN → —；正常保留两位', () => {
    expect(formatPercent(null)).toBe('—');
    expect(formatPercent(NaN)).toBe('—');
    expect(formatPercent(0.1234)).toBe('12.34%');
    expect(formatPercent(-0.05)).toBe('-5.00%');
  });
});
