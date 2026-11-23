import { describe, expect, it } from 'vitest';
import {
  buildTradingTopBarAccountInfo,
  resolveTradingAccountMode,
  selectTradingAccount,
} from '../accountAdapter';

describe('accountAdapter', () => {
  it('should prefer real runtime mode for real trading account selection', () => {
    const selection = selectTradingAccount({
      runtimeMode: 'REAL',
      realAccount: { total_asset: 10, cash: 5, market_value: 5, positions: [] } as any,
      simulationAccount: { total_asset: 99, cash: 99, market_value: 0, positions: [] } as any,
    });

    expect(selection.mode).toBe('real');
    expect(selection.source).toBe('postgresql');
    expect(selection.account?.total_asset).toBe(10);
  });

  it('should respect preferred mode for simulation account selection', () => {
    const selection = selectTradingAccount({
      runtimeMode: 'SIMULATION',
      preferredMode: 'simulation',
      realAccount: { total_asset: 10, cash: 5, market_value: 5, positions: [] } as any,
      simulationAccount: { total_asset: 99, cash: 99, market_value: 0, positions: [] } as any,
    });

    expect(selection.mode).toBe('simulation');
    expect(selection.source).toBe('simulation');
    expect(selection.account?.total_asset).toBe(99);
  });

  it('should normalize top bar metrics from account snapshot', () => {
    const view = buildTradingTopBarAccountInfo(
      {
        total_asset: 100,
        initial_equity: 90,
        cash: 60,
        market_value: 40,
        today_pnl: 5,
        total_pnl: 10,
        floating_pnl: 4,
        frozen_cash: 20,
        positions: [{ volume: 10, last_price: 5, cost_price: 4 }],
      } as any,
      {
        status: 'running',
        mode: 'REAL',
        portfolio: {
          total_return: 10,
          total_pnl: 10,
          daily_pnl: 5,
          daily_return: 5,
          position_count: 1,
        },
      } as any,
    );

    expect(view.total_asset).toBe(100);
    expect(view.initial_equity).toBe(90);
    expect(view.cash).toBe(60);
    expect(view.market_value).toBe(40);
    expect(view.daily_pnl).toBe(5);
    expect(view.daily_pnl_percent).toBeCloseTo(0.05);
    expect(view.total_pnl).toBe(10);
    expect(view.total_pnl_percent).toBeCloseTo(10 / 90);
    expect(view.position_count).toBe(1);
    expect(resolveTradingAccountMode('SIMULATION', 'real')).toBe('real');
  });

  it('should prefer normalized baseline and return fields when account snapshot provides them', () => {
    const view = buildTradingTopBarAccountInfo(
      {
        total_asset: 120,
        cash: 60,
        market_value: 60,
        daily_pnl: 6,
        total_pnl: 20,
        floating_pnl: 8,
        daily_return_ratio: 0.06,
        total_return_ratio: 0.2,
        baseline: {
          initial_equity: 100,
          day_open_equity: 100,
          month_open_equity: 105,
        },
        positions: [] as any,
      } as any,
      null,
    );

    expect(view.initial_equity).toBe(100);
    expect(view.day_open_equity).toBe(100);
    expect(view.month_open_equity).toBe(105);
    expect(view.daily_pnl_percent).toBeCloseTo(0.06);
    expect(view.total_pnl_percent).toBeCloseTo(0.2);
  });

  it('should prefer pnl-derived ratios when explicit return fields conflict with baseline', () => {
    const view = buildTradingTopBarAccountInfo(
      {
        total_asset: 21_652_375.54,
        cash: 5_000_000,
        market_value: 16_652_375.54,
        daily_pnl: 7_999,
        total_pnl: 644_376.54,
        daily_return_ratio: 0.0004,
        total_return_ratio: 0.0004,
        baseline: {
          initial_equity: 21_000_000,
          day_open_equity: 21_644_376.54,
          month_open_equity: 21_644_376.54,
        },
        positions: [] as any,
      } as any,
      null,
    );

    expect(view.initial_equity).toBeCloseTo(21_007_999);
    expect(view.daily_pnl_percent).toBeCloseTo(7_999 / 21_644_376.54);
    expect(view.total_pnl_percent).toBeCloseTo(644_376.54 / 21_007_999);
  });

  it('should prefer direct position_count and daily_pnl fallbacks when positions are absent', () => {
    const view = buildTradingTopBarAccountInfo(
      {
        total_asset: 100,
        cash: 60,
        market_value: 40,
        daily_pnl: 7,
        total_pnl: 10,
        frozen_cash: 20,
        position_count: 76,
        positions: [] as any,
      } as any,
      null,
    );

    expect(view.daily_pnl).toBe(7);
    expect(view.position_count).toBe(76);
  });

  it('should derive initial equity from total asset and pnl when snapshot omits it', () => {
    const view = buildTradingTopBarAccountInfo(
      {
        total_asset: 100,
        cash: 60,
        market_value: 40,
        daily_pnl: 7,
        total_pnl: 25,
        frozen_cash: 20,
        position_count: 76,
        positions: [] as any,
      } as any,
      null,
    );

    expect(view.initial_equity).toBe(75);
  });

  it('should derive floating pnl from dict-shaped positions (simulation account)', () => {
    // 事故（2026-10-20 模拟交易顶栏）：/simulation/account 的 positions 是
    // dict[symbol, position]，且不返回顶层 floating_pnl —— 派生逻辑只认数组，
    // 78 只持仓的浮动盈亏恒显示 0.00 / 0.00%。
    const view = buildTradingTopBarAccountInfo(
      {
        total_asset: 1_032_736.63,
        cash: 25_058.63,
        market_value: 1_007_678,
        position_count: 2,
        positions: {
          '002552.SZ': {
            symbol: '002552.SZ',
            volume: 500,
            last_price: 41.66,
            cost_price: 54.61,
            market_value: 20_830,
            side: 'long',
          },
          '300489.SZ': {
            symbol: '300489.SZ',
            volume: 100,
            last_price: 231.53,
            cost_price: 222,
            market_value: 23_153,
            side: 'long',
          },
        },
      } as any,
      null,
    );

    const expected = (41.66 - 54.61) * 500 + (231.53 - 222) * 100;
    expect(view.floating_pnl).toBeCloseTo(expected);
    expect(view.floating_pnl_percent).toBeCloseTo(expected / 1_007_678);
    expect(view.position_count).toBe(2);
  });

  it('should invert floating pnl for short positions in dict shape', () => {
    const view = buildTradingTopBarAccountInfo(
      {
        total_asset: 100_000,
        cash: 60_000,
        market_value: 40_000,
        positions: {
          'AAPL': {
            symbol: 'AAPL',
            volume: 100,
            last_price: 90,
            cost_price: 100,
            side: 'short',
          },
        },
      } as any,
      null,
    );

    expect(view.floating_pnl).toBeCloseTo((100 - 90) * 100);
  });

  it('should fall back to market_value when a dict position has no last price', () => {
    const view = buildTradingTopBarAccountInfo(
      {
        total_asset: 12_500,
        cash: 2_500,
        market_value: 10_000,
        positions: {
          '600000.SH': {
            symbol: '600000.SH',
            volume: 1_000,
            cost_price: 9,
            market_value: 10_000,
            side: 'long',
          },
        },
      } as any,
      null,
    );

    expect(view.floating_pnl).toBeCloseTo((10_000 / 1_000 - 9) * 1_000);
  });

  it('should still derive floating pnl from array positions', () => {
    const view = buildTradingTopBarAccountInfo(
      {
        total_asset: 12_500,
        cash: 2_500,
        market_value: 10_000,
        positions: [
          { volume: 1_000, last_price: 10, cost_price: 9 },
          { volume: 500, last_price: 8, cost_price: 9 },
        ],
      } as any,
      null,
    );

    expect(view.floating_pnl).toBeCloseTo(1_000 - 500);
  });

  it('should derive initial equity from total asset minus total pnl even when baseline exists', () => {
    const view = buildTradingTopBarAccountInfo(
      {
        total_asset: 21_652_375.54,
        total_pnl: 644_376.54,
        baseline: {
          initial_equity: 21_644_376.54,
        },
        positions: [] as any,
      } as any,
      null,
    );

    expect(view.initial_equity).toBeCloseTo(21_007_999);
  });

  it('should normalize equity to cash + market value when the client fields are out of sync', () => {
    // 2026-11-23 实况：客户端 total_asset 按 9.65 刷新、market_value 按 9.63 刷新，
    // 两者差 1,886（≈ 601825 两分钱 × 94,300 股）。卡片必须单一口径。
    const view = buildTradingTopBarAccountInfo(
      {
        total_asset: 930_295.74,
        cash: 1_484.74,
        market_value: 926_925.0,
        day_open_equity: 928_699.28,
        initial_equity: 918_934.73,
        today_pnl: 1_596.46,
        total_pnl: 11_361.01,
        positions: [] as any,
      } as any,
      null,
    );

    expect(view.total_asset).toBeCloseTo(928_409.74, 2);
    expect(view.daily_pnl).toBeCloseTo(928_409.74 - 928_699.28, 2);
    expect(view.total_pnl).toBeCloseTo(928_409.74 - 918_934.73, 2);
  });

  it('should keep the reported equity when it already equals cash + market value', () => {
    const view = buildTradingTopBarAccountInfo(
      {
        total_asset: 928_377.74,
        cash: 1_484.74,
        market_value: 926_893.0,
        day_open_equity: 928_699.28,
        initial_equity: 918_934.73,
        today_pnl: -321.54,
        total_pnl: 9_443.01,
        positions: [] as any,
      } as any,
      null,
    );

    expect(view.total_asset).toBeCloseTo(928_377.74, 2);
    expect(view.daily_pnl).toBeCloseTo(-321.54, 2);
    expect(view.total_pnl).toBeCloseTo(9_443.01, 2);
  });
});
