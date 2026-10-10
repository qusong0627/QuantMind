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

  it('should derive floating pnl from dict positions (simulation account shape)', () => {
    const view = buildTradingTopBarAccountInfo(
      {
        total_asset: 1_010_000,
        cash: 900_000,
        market_value: 110_000,
        daily_pnl: 5_000,
        total_pnl: 10_000,
        // 无显式 floating_pnl：走派生分支
        positions: {
          '600036.SH': { volume: 10000, available_volume: 10000, cost: 10.0, price: 11.0, market_value: 110000 },
          '000001.SZ': { volume: 0, available_volume: 0, cost: 0, price: 0, market_value: 0 },
        },
      } as any,
      null,
    );

    // (11 - 10) * 10000 = 10000，空仓不计入
    expect(view.floating_pnl).toBe(10000);
    expect(view.floating_pnl_percent).toBeCloseTo(10000 / 110000);
  });
});
