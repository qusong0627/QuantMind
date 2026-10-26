import { describe, expect, it } from 'vitest';
import { qlibResultToStrategyLabResult } from '../qlibToStrategyLab';
import type { BacktestResult } from '../../services/backtestService';

describe('qlibResultToStrategyLabResult', () => {
  it('把 qlib 结果映射成统一的 StrategyLab 形状', () => {
    const result = {
      backtest_id: 'bt_1',
      status: 'completed',
      total_return: 0.12,
      annual_return: 0.2,
      sharpe_ratio: 1.5,
      max_drawdown: -0.08,
      win_rate: 0.55,
      total_trades: 2,
      execution_time: 3.2,
      equity_curve: [
        { date: '2026-01-01', value: 100 },
        { date: '2026-01-02', value: 112 },
      ],
      trades: [
        { date: '2026-01-01', symbol: '600036.SH', action: 'buy', price: 10, quantity: 100 },
        { date: '2026-01-02', symbol: '600036.SH', action: 'sell', price: 11, quantity: 100, pnl: 100 },
      ],
    } as unknown as BacktestResult;

    const out = qlibResultToStrategyLabResult(result);
    expect(out.run_id).toBe('bt_1');
    expect(out.status).toBe('success');
    expect(out.metrics.cum_return).toBeCloseTo(0.12);
    expect(out.metrics.max_drawdown).toBeCloseTo(0.08);
    expect(out.equity).toHaveLength(2);
    expect(out.trades[0].direction).toBe('BUY');
    expect(out.trades[1].direction).toBe('SELL');
    expect(out.metrics.n_trades).toBe(2);
  });

  it('失败结果映射为 failed，并保留错误信息', () => {
    const out = qlibResultToStrategyLabResult({
      backtest_id: 'bt_2',
      status: 'failed',
      error_message: 'no pred',
    } as unknown as BacktestResult);
    expect(out.status).toBe('failed');
    expect(out.error).toBe('no pred');
  });
});
