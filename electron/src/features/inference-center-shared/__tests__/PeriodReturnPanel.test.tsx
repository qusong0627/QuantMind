/**
 * 周期收益面板：区间参数、月度拆解、缺失值 `—`、红涨绿跌与合规字眼。
 */

import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import type {
  BlendPeriodResponse,
  BlendRecord,
} from '../../../services/blendTypes';
import { PeriodReturnPanel } from '../components/combo/PeriodReturnPanel';

function blend(over: Partial<BlendRecord> = {}): BlendRecord {
  return {
    blend_id: 'blend-1',
    tenant_id: 'default',
    user_id: 'u1',
    name: '全13·T+10 等权',
    market: 'CN',
    purpose: 'rebalance',
    source: 'system',
    template_id: 'tpl-base',
    horizon_days: 10,
    weighting: 'equal',
    method: 'mean',
    trim_count: 2,
    min_coverage: 2,
    consensus_min_count: 0,
    entry_per_day: 5,
    rolling_window: 10,
    target_positions: 50,
    feature_signature: 'sig',
    data_source: 'quantdb_factors',
    status: 'active',
    created_at: null,
    updated_at: null,
    ...over,
  };
}

function period(over: Partial<BlendPeriodResponse> = {}): BlendPeriodResponse {
  const base: BlendPeriodResponse = {
    blend_id: 'blend-1',
    source: 'pred',
    bounds: {
      source: 'pred',
      pred: {
        start: '2025-01-02', end: '2026-10-19', trading_days: 430,
        members_ready: 13, members_total: 13,
      },
      runs: { start: '2026-08-27', end: '2026-10-19', trading_days: 33 },
      effective: { start: '2025-01-02', end: '2026-10-19', trading_days: 430 },
      warnings: [],
    },
    benchmark: { symbol: 'SH000300', name: '沪深300', available: true, days: 430 },
    requested: { start: '2026-04-22', end: '2026-10-19', capital: 10000000, positions: 50 },
    effective: {
      start: '2026-08-27',
      end: '2026-10-19',
      first_scoring_day: '2026-08-27',
      last_scoring_day: '2026-10-19',
    },
    template: { entry_per_day: 5, hold_days: 10, target_positions: 50, method: 'mean' },
    summary: {
      initial_capital: 10000000,
      final_equity: 9935061.85,
      total_return_pct: -0.65,
      profit_amount: -64938.15,
      realized_pnl_amount: 12000,
      realized_return_pct: 0.12,
      unrealized_pnl_amount: -76938.15,
      unrealized_return_pct: -0.77,
      max_drawdown_pct: -6.04,
      drawdown_peak_date: '2026-09-01',
      drawdown_trough_date: '2026-10-17',
      drawdown_recovery_date: null,
      drawdown_recovery_days: null,
      benchmark_return_pct: -2.1,
      benchmark_coverage_pct: 100,
      excess_return_pct: 1.45,
      annualized_return_pct: -7.2,
      volatility_pct: 18.5,
      sharpe_ratio: -0.35,
      sortino_ratio: -0.5,
      calmar_ratio: -1.1,
      tracking_error_pct: 6.2,
      information_ratio: 0.23,
      profit_factor: 0.82,
      payoff_ratio: 1.1,
      avg_win_pct: 5.2,
      avg_loss_pct: -4.7,
      gross_profit_amount: 220000,
      gross_loss_amount: -268000,
      avg_holding_days: 10,
      turnover_x: 3.2,
      avg_exposure_pct: 84.5,
      final_cash_pct: 30.2,
      trading_days: 32,
      scoring_days: 32,
      entries: 160,
      exits: 110,
      closed_trades: 110,
      open_positions: 50,
      wins: 50,
      losses: 60,
      flats: 0,
      win_rate_pct: 45.45,
      avg_trade_return_pct: 0.88,
      avg_positions: 42.97,
      entry_convention: 'close',
      fee_convention: 'none',
      data_quality: 'complete',
      scoring_coverage_pct: 100,
    },
    months: [
      {
        month: '2026-08',
        start_date: '2026-08-27',
        end_date: '2026-08-31',
        start_equity: 10000000,
        end_equity: 10031333.15,
        return_pct: 0.31,
        profit_amount: 31333.15,
        realized_pnl_amount: 0,
        unrealized_pnl_amount: 31333.15,
        benchmark_return_pct: -0.2,
        excess_return_pct: 0.51,
        entries: 15,
        exits: 0,
        closed_trades: 0,
        month_end_positions: 15,
        win_rate_pct: null,
        partial: true,
      },
      {
        month: '2026-10',
        start_date: '2026-10-08',
        end_date: '2026-10-19',
        start_equity: 10374000,
        end_equity: 9935061.85,
        return_pct: -4.21,
        profit_amount: -438938.15,
        realized_pnl_amount: -320000,
        unrealized_pnl_amount: -118938.15,
        benchmark_return_pct: -1.8,
        excess_return_pct: -2.41,
        entries: 40,
        exits: 40,
        closed_trades: 40,
        month_end_positions: 50,
        win_rate_pct: 37.5,
        partial: true,
      },
    ],
    daily: [
      {
        date: '2026-08-27', equity: 10000000, cash: 9400000, market_value: 600000,
        exposure_pct: 6, positions: 5, entries: 5, exits: 0,
        realized_pnl_amount: 0, unrealized_pnl_amount: 0,
        return_pct: null, benchmark_return_pct: null, excess_return_pct: null,
      },
      {
        date: '2026-09-30', equity: 10374000, cash: 4000000, market_value: 6374000,
        exposure_pct: 61.4, positions: 50, entries: 5, exits: 5,
        realized_pnl_amount: 80000, unrealized_pnl_amount: 294000,
        return_pct: 1.1, benchmark_return_pct: -0.2, excess_return_pct: 1.3,
      },
      {
        date: '2026-10-19', equity: 9935061.85, cash: 3000000, market_value: 6935061.85,
        exposure_pct: 69.8, positions: 50, entries: 5, exits: 5,
        realized_pnl_amount: 12000, unrealized_pnl_amount: -76938.15,
        return_pct: -0.4, benchmark_return_pct: -1.8, excess_return_pct: 1.4,
      },
    ],
    trades: [
      {
        symbol: 'SZ002790', code: 'SZ002790', name: '瑞尔特',
        entry_date: '2026-08-27', exit_date: '2026-09-10',
        entry_price: 5.99, exit_price: 8.46, shares: 33389.99, notional: 200000,
        return_pct: 41.24, pnl_amount: 82470.78, held_days: 10,
        exit_reason: '到期', deferred: false,
      },
      {
        symbol: 'SH600000', code: 'SH600000', name: '浦发银行',
        entry_date: '2026-09-01', exit_date: '2026-09-15',
        entry_price: 7.9, exit_price: 7.5, shares: 25316.45, notional: 200000,
        return_pct: -5.06, pnl_amount: -10126.58, held_days: 10,
        exit_reason: '到期', deferred: true,
      },
    ],
    open: [
      {
        symbol: 'SZ002864', code: 'SZ002864', name: '盘龙药业',
        entry_date: '2026-09-29', mark_date: '2026-10-19',
        entry_price: 35.2, mark_price: 34.04, shares: 5681.82, notional: 200000,
        return_pct: -3.3, pnl_amount: -6592.87, held_days: 9, stale: true,
      },
    ],
    warnings: ['effective_start_clamped', 'positions_cap'],
    members: [],
  };
  return { ...base, ...over };
}

const baseProps = {
  loading: false,
  error: null,
  dates: ['2026-10-19', '2026-08-27'],
  onRetry: vi.fn(),
};

describe('PeriodReturnPanel', () => {
  it('渲染总收益、月度拆解与口径脚注，缺失值显示 —', () => {
    const { container } = render(
      <PeriodReturnPanel
        {...baseProps}
        blend={blend()}
        period={period()}
        onCalculate={vi.fn()}
      />,
    );

    expect(screen.getByTestId('combo-period-total-return')).toHaveTextContent('-0.65%');
    expect(screen.getByTestId('combo-period-total-return').className).toContain('text-green-600');
    expect(screen.getByTestId('combo-period-profit')).toHaveTextContent('-6.49 万');
    expect(screen.getByTestId('combo-period-quality')).toHaveTextContent('数据完整');
    // 已实现 / 浮动分开；基准 / 超额来自统一数据端口
    expect(screen.getByTestId('combo-period-realized')).toHaveTextContent('+1.20 万');
    expect(screen.getByTestId('combo-period-realized')).toHaveTextContent('0.12%');
    expect(screen.getByTestId('combo-period-unrealized')).toHaveTextContent('-7.69 万');
    expect(screen.getByTestId('combo-period-benchmark')).toHaveTextContent('-2.10%');
    expect(screen.getByTestId('combo-period-excess')).toHaveTextContent('+1.45%');
    expect(screen.getByTestId('combo-period-source')).toHaveTextContent('pred 测试集');
    expect(screen.getByTestId('combo-period-source')).toHaveTextContent(
      '2025-01-02 ~ 2026-10-19',
    );
    expect(screen.getByTestId('combo-period-source')).toHaveTextContent('沪深300 · 430 日');
    // 基准按初始资金归一后与组合同轴（橙色虚线）
    expect(screen.getByTestId('combo-period-chart').innerHTML).toContain('#f59e0b');
    const months = screen.getByTestId('combo-period-months').textContent ?? '';
    expect(months).toContain('基准');
    expect(months).toContain('超额');
    expect(screen.getByTestId('combo-period-chart')).toBeInTheDocument();
    expect(screen.getByText('月度拆解')).toBeInTheDocument();
    // 2026-08 无已了结交易 → 胜率必须是 —，不是 0
    expect(screen.getAllByText('—').length).toBeGreaterThan(0);
    expect(screen.getByTestId('combo-period-warnings')).toHaveTextContent(
      '建仓日早于最早可用评分日',
    );
    expect(screen.getByTestId('combo-period-warnings')).toHaveTextContent('持仓上限');
    expect(container.textContent).not.toMatch(/买入|卖出/);
  });

  it('点计算：按表单发送资金（万→元）与持仓上限', async () => {
    const onCalculate = vi.fn();
    render(
      <PeriodReturnPanel
        {...baseProps}
        blend={blend()}
        period={period()}
        onCalculate={onCalculate}
      />,
    );
    const [capitalInput] = screen.getAllByRole('spinbutton');
    fireEvent.change(capitalInput, { target: { value: '3000' } });
    fireEvent.click(screen.getByTestId('combo-period-calculate'));
    await waitFor(() => expect(onCalculate).toHaveBeenCalledTimes(1));
    expect(onCalculate.mock.calls[0][0]).toMatchObject({
      start: '2026-04-22',
      end: '2026-10-19',
      capital: 30000000,
      positions: 50,
    });
  });

  it('快捷区间「近 1 月」按可用评分日回推并带上当前资金/持仓', async () => {
    const onCalculate = vi.fn();
    render(
      <PeriodReturnPanel
        {...baseProps}
        blend={blend()}
        period={period()}
        onCalculate={onCalculate}
      />,
    );
    fireEvent.click(screen.getByTestId('combo-period-quick-1m'));
    await waitFor(() => expect(onCalculate).toHaveBeenCalledTimes(1));
    expect(onCalculate.mock.calls[0][0]).toMatchObject({
      start: '2026-09-19',
      end: '2026-10-19',
      capital: 10000000,
      positions: 50,
    });
  });

  it('快捷区间「全部」按 pred 可用区间回看到 2025，而不是只到推理台账', async () => {
    const onCalculate = vi.fn();
    render(
      <PeriodReturnPanel
        {...baseProps}
        blend={blend()}
        period={period()}
        onCalculate={onCalculate}
      />,
    );
    fireEvent.click(screen.getByTestId('combo-period-quick-all'));
    await waitFor(() => expect(onCalculate).toHaveBeenCalledTimes(1));
    expect(onCalculate.mock.calls[0][0]).toMatchObject({
      start: '2025-01-02',
      end: '2026-10-19',
      capital: 10000000,
      positions: 50,
    });
  });

  it('基准覆盖率不足时，基准与超额显示 —（不写 0）', () => {
    const data = period();
    data.summary.benchmark_coverage_pct = 40;
    data.summary.benchmark_return_pct = null;
    data.summary.excess_return_pct = null;
    render(
      <PeriodReturnPanel {...baseProps} blend={blend()} period={data} onCalculate={vi.fn()} />,
    );
    expect(screen.getByTestId('combo-period-benchmark').textContent).toBe('—');
    expect(screen.getByTestId('combo-period-excess').textContent).toBe('—');
  });

  it('展开明细：已了结与在场表格都渲染，顺延/沿用标记可见', async () => {
    render(
      <PeriodReturnPanel
        {...baseProps}
        blend={blend()}
        period={period()}
        onCalculate={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByText('已了结明细（2 笔）'));
    expect(await screen.findByText('瑞尔特')).toBeInTheDocument();
    expect(screen.getByText('顺延')).toBeInTheDocument();

    fireEvent.click(screen.getByText('期末在场（1 只）'));
    expect(await screen.findByText('盘龙药业')).toBeInTheDocument();
    expect(screen.getByText('沿用')).toBeInTheDocument();
  });

  it('错误态：展示原因并可重试', () => {
    const onRetry = vi.fn();
    render(
      <PeriodReturnPanel
        {...baseProps}
        error="评分日不足"
        blend={blend()}
        period={null}
        onCalculate={vi.fn()}
        onRetry={onRetry}
      />,
    );
    expect(screen.getByTestId('combo-period-error')).toHaveTextContent('评分日不足');
    fireEvent.click(screen.getByRole('button', { name: /重\s*试/ }));
    expect(onRetry).toHaveBeenCalled();
  });

  it('无结果时给出空态提示，不伪造 0 收益', () => {
    render(
      <PeriodReturnPanel
        {...baseProps}
        blend={blend()}
        period={null}
        onCalculate={vi.fn()}
      />,
    );
    expect(screen.getByText(/按模板滚动评估组合综合排名收益/)).toBeInTheDocument();
    expect(screen.queryByTestId('combo-period-total-return')).toBeNull();
  });
});
