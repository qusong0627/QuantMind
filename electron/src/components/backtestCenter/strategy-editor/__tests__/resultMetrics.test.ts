import { describe, expect, it } from 'vitest';
import type { StrategyLabRunResult } from '../../../../features/strategy-lab/types';
import type { SavedBacktestRun } from '../../../../utils/strategyEditorSavedRuns';
import {
  alignBuyHoldBenchmark,
  computeEquitySummary,
  computeMonthlyReturns,
  computeStrategyScore,
  extractSavedRunMetrics,
  formatMoney,
  formatNumber,
  formatPct,
  formatSignedPct,
  normalizeDrawdown,
  signedToneClass,
  validateBacktestParams,
} from '../resultMetrics';

const makeResult = (metrics: Partial<StrategyLabRunResult['metrics']> = {}): StrategyLabRunResult => ({
  run_id: 'run-test',
  status: 'success',
  metrics: {
    cum_return: 0.2,
    annual_return: 0.18,
    sharpe: 1.2,
    max_drawdown: -0.12,
    win_rate: 0.55,
    n_trades: 40,
    avg_position: 0.8,
    ...metrics,
  },
  equity: [],
  trades: [],
  positions: [],
  overlays: {},
  logs: [],
  warnings: [],
  error: null,
  error_traceback: null,
  config: {},
  script_sha: 'sha',
  data_snapshot_at: null,
  elapsed_sec: 1,
  started_at: 0,
  finished_at: 1,
});

describe('resultMetrics 格式化', () => {
  it('缺失值一律显示 —，不冒充 0', () => {
    expect(formatPct(null)).toBe('—');
    expect(formatPct(undefined)).toBe('—');
    expect(formatPct(Number.NaN)).toBe('—');
    expect(formatSignedPct(null)).toBe('—');
    expect(formatMoney(null)).toBe('—');
  });

  it('百分比与带符号百分比：正数补 +，负数保留 -', () => {
    expect(formatPct(0.1234)).toBe('12.34%');
    expect(formatSignedPct(0.1234)).toBe('+12.34%');
    expect(formatSignedPct(-0.05)).toBe('-5.00%');
  });

  it('负零显示为 0.00，不出现 -0.00', () => {
    expect(formatNumber(-0.001, 2)).toBe('0.00');
    expect(formatNumber(-0.02, 2)).toBe('-0.02');
  });

  it('回撤正负两种口径都归一化为负数', () => {
    expect(normalizeDrawdown(0.3105)).toBeCloseTo(-0.3105);
    expect(normalizeDrawdown(-0.3105)).toBeCloseTo(-0.3105);
    expect(normalizeDrawdown(null)).toBeNull();
  });

  it('A 股红涨绿跌：正收益红、负收益绿、0 中性', () => {
    expect(signedToneClass(0.1)).toBe('text-red-500');
    expect(signedToneClass(-0.1)).toBe('text-emerald-600');
    expect(signedToneClass(0)).toBe('text-slate-800');
    expect(signedToneClass(null)).toBe('text-slate-800');
  });
});

describe('computeStrategyScore', () => {
  it('无结果返回 null', () => {
    expect(computeStrategyScore(null)).toBeNull();
  });

  it('高夏普/高年化/低回撤/大样本 → 优', () => {
    const score = computeStrategyScore(
      makeResult({ sharpe: 1.8, annual_return: 0.4, max_drawdown: -0.05, n_trades: 80 }),
    );
    expect(score?.score).toBe(100);
    expect(score?.grade).toBe('优');
  });

  it('minibt 的正数回撤（31.05 表示 -31.05%）不会被当成「回撤 < 10%」', () => {
    const score = computeStrategyScore(
      makeResult({ sharpe: -0.1, annual_return: -0.05, max_drawdown: 0.45, n_trades: 80 }),
    );
    expect(score?.score).toBe(20);
    expect(score?.notes).toContain('回撤 > 30%：高风险');
  });

  it('负夏普/负年化/大回撤/小样本 → 高风险并给出提示', () => {
    const score = computeStrategyScore(
      makeResult({ sharpe: -0.2, annual_return: -0.1, max_drawdown: -0.45, n_trades: 2 }),
    );
    expect(score?.score).toBe(0);
    expect(score?.grade).toBe('高风险');
    expect(score?.notes).toEqual(
      expect.arrayContaining(['夏普 ≤ 0：警告', '年化收益为负', '回撤 > 30%：高风险', '交易 < 5 次，统计不显著']),
    );
  });
});

const equity = [
  { date: '2024-01-02', value: 1, benchmark: 1 },
  { date: '2024-01-31', value: 1.1, benchmark: 1.02 },
  { date: '2024-02-29', value: 0.99, benchmark: 1.03 },
  { date: '2024-03-29', value: 1.21, benchmark: 1.05 },
  { date: '2025-01-02', value: 1.15, benchmark: 1.06 },
];

describe('computeEquitySummary', () => {
  it('区间/基准/超额/月度胜率/最长水下', () => {
    const summary = computeEquitySummary(equity);
    expect(summary).not.toBeNull();
    expect(summary!.startDate).toBe('2024-01-02');
    expect(summary!.endDate).toBe('2025-01-02');
    expect(summary!.tradingDays).toBe(5);
    expect(summary!.finalNetValue).toBeCloseTo(1.15);
    expect(summary!.finalEquity).toBeCloseTo(1.15);
    expect(summary!.benchmarkReturn).toBeCloseTo(0.06);
    expect(summary!.alpha).toBeCloseTo(0.09);
    // 2024-02 负、2024-03 正、2025-01 负、2024-01 正 → 2/4
    expect(summary!.monthlyWinRate).toBeCloseTo(0.5);
    // 峰值 1.1 之后 0.99 水下 1 天；1.21 之后 1.15 水下 1 天（不连续）
    expect(summary!.maxDrawdownDays).toBe(1);
  });

  it('空序列返回 null', () => {
    expect(computeEquitySummary([])).toBeNull();
  });

  it('无基准时 benchmarkReturn/alpha 为 null，而非 0', () => {
    const summary = computeEquitySummary([
      { date: '2024-01-02', value: 1, benchmark: null },
      { date: '2024-02-01', value: 1.1, benchmark: null },
    ]);
    expect(summary!.benchmarkReturn).toBeNull();
    expect(summary!.alpha).toBeNull();
  });
});

describe('computeMonthlyReturns', () => {
  it('按年成行，首月以当月首点为基准，年度收益按年内首末点', () => {
    const rows = computeMonthlyReturns(equity);
    expect(rows.map((row) => row.year)).toEqual([2024, 2025]);
    const jan = rows[0].months[0];
    expect(jan.ret).toBeCloseTo(0.1);
    const feb = rows[0].months[1];
    expect(feb.ret).toBeCloseTo(0.99 / 1.1 - 1);
    expect(rows[0].yearReturn).toBeCloseTo(0.21);
    expect(rows[1].yearReturn).toBeCloseTo(0);
    // 没有数据的小月保持 null（不插 0）
    expect(rows[0].months[4].ret).toBeNull();
  });
});

describe('alignBuyHoldBenchmark（minibt 买入持有基准）', () => {
  const eq = (rows: Array<[string, number]>) =>
    rows.map(([date, value]) => ({ date, value, benchmark: null }));

  it('全额对齐：持有收益与超额按同一窗口计算', () => {
    const result = alignBuyHoldBenchmark(
      eq([
        ['2024-01-02', 1],
        ['2024-01-03', 1.1],
        ['2024-01-04', 0.99],
        ['2024-01-05', 1.21],
      ]),
      [
        { date: '2024-01-02', close: 10 },
        { date: '2024-01-03', close: 11 },
        { date: '2024-01-04', close: 9 },
        { date: '2024-01-05', close: 12 },
      ],
    );
    expect(result).not.toBeNull();
    expect(result!.strategyReturn).toBeCloseTo(0.21);
    expect(result!.benchmarkReturn).toBeCloseTo(0.2);
    expect(result!.alpha).toBeCloseTo(0.01);
    expect(result!.series.map((point) => Number(point.value.toFixed(4)))).toEqual([1, 1.1, 0.9, 1.2]);
  });

  it('基准起点晚于策略：窗口自动收窄到共同日期', () => {
    const result = alignBuyHoldBenchmark(
      eq([
        ['2024-01-02', 1],
        ['2024-01-03', 1.1],
        ['2024-01-04', 0.99],
        ['2024-01-05', 1.21],
      ]),
      [
        { date: '2024-01-03', close: 11 },
        { date: '2024-01-04', close: 9 },
        { date: '2024-01-05', close: 12 },
      ],
    );
    expect(result).not.toBeNull();
    expect(result!.strategyReturn).toBeCloseTo(1.21 / 1.1 - 1);
    expect(result!.benchmarkReturn).toBeCloseTo(12 / 11 - 1);
    expect(result!.series).toHaveLength(3);
  });

  it('停牌缺日按前值填充，而不是丢掉策略日期', () => {
    const result = alignBuyHoldBenchmark(
      eq([
        ['2024-01-02', 1],
        ['2024-01-03', 1.1],
        ['2024-01-04', 1.2],
      ]),
      [
        { date: '2024-01-02', close: 10 },
        { date: '2024-01-04', close: 12 },
      ],
    );
    expect(result).not.toBeNull();
    expect(result!.benchmarkReturn).toBeCloseTo(0.2);
    expect(result!.series.map((point) => Number(point.value.toFixed(4)))).toEqual([1, 1, 1.2]);
  });

  it('数据不足返回 null（空序列 / 单点 / 基准无交集）', () => {
    expect(alignBuyHoldBenchmark([], [])).toBeNull();
    expect(alignBuyHoldBenchmark(eq([['2024-01-02', 1]]), [{ date: '2024-01-02', close: 10 }])).toBeNull();
    expect(
      alignBuyHoldBenchmark(
        eq([
          ['2024-01-02', 1],
          ['2024-01-03', 1.1],
        ]),
        [{ date: '2024-02-01', close: 10 }],
      ),
    ).toBeNull();
  });
});

describe('validateBacktestParams', () => {
  const base = { symbol: '600036', startDate: '2024-01-01', endDate: '2025-01-01', fast: 10, slow: 20 };

  it('合法参数可应用', () => {
    const result = validateBacktestParams({ ...base, isMinibt: true });
    expect(result.canApply).toBe(true);
    expect(result.messages).toEqual([]);
  });

  it('日期倒置 / fast>=slow / 空标的都会被拦下', () => {
    const date = validateBacktestParams({ ...base, endDate: '2023-12-31', isMinibt: false });
    expect(date.dateInvalid).toBe(true);
    expect(date.canApply).toBe(false);

    const fastSlow = validateBacktestParams({ ...base, fast: 20, slow: 20, isMinibt: true });
    expect(fastSlow.fastSlowInvalid).toBe(true);

    const empty = validateBacktestParams({ ...base, symbol: '  ', isMinibt: true });
    expect(empty.symbolMissing).toBe(true);
  });

  it('非 minibt 不校验快慢线，也不要求单标的代码', () => {
    const result = validateBacktestParams({ ...base, symbol: '', fast: 50, slow: 10, isMinibt: false });
    expect(result.fastSlowInvalid).toBe(false);
    expect(result.symbolMissing).toBe(false);
    expect(result.canApply).toBe(true);
  });
});

describe('extractSavedRunMetrics', () => {
  const makeRun = (result: unknown): SavedBacktestRun => ({
    id: 'x',
    name: 'x',
    savedAt: '2026-01-01T00:00:00Z',
    kind: 'minibt',
    result,
  });

  it('StrategyLab 形状读 metrics', () => {
    expect(extractSavedRunMetrics(makeRun(makeResult({ cum_return: 0.3, sharpe: 1.5, max_drawdown: -0.1 })))).toEqual({
      cumReturn: 0.3,
      sharpe: 1.5,
      maxDrawdown: -0.1,
    });
  });

  it('qlib 形状读顶层字段', () => {
    expect(
      extractSavedRunMetrics(makeRun({ total_return: 0.12, sharpe_ratio: 0.9, max_drawdown: -0.05 })),
    ).toEqual({ cumReturn: 0.12, sharpe: 0.9, maxDrawdown: -0.05 });
  });

  it('缺失指标保持 null', () => {
    expect(extractSavedRunMetrics(makeRun({}))).toEqual({
      cumReturn: null,
      sharpe: null,
      maxDrawdown: null,
    });
  });
});
