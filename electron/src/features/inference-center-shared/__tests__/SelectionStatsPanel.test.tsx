/**
 * 单模型 Top50 历史统计面板：自动统计、换股节奏、快捷区间、缺失值 `—`、
 * 红涨绿跌与合规字眼。
 */

import type React from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import type {
  SelectionStatsResponse,
} from '../../../services/selectionStatsTypes';
import { SelectionStatsPanel } from '../components/crossstats/SelectionStatsPanel';

const { fetchMock } = vi.hoisted(() => ({ fetchMock: vi.fn() }));

vi.mock('../../../services/selectionStatsService', () => ({
  selectionStatsService: { fetch: fetchMock },
  selectionStatsErrorMessage: (err: unknown) =>
    err instanceof Error ? err.message : String(err),
}));

function stats(over: Partial<SelectionStatsResponse> = {}): SelectionStatsResponse {
  const base: SelectionStatsResponse = {
    model_id: 'm1',
    model_name: '测试模型',
    market: 'CN',
    split: 'test',
    requested: {
      start: '2025-02-05',
      end: '2026-10-19',
      top_k: 50,
      hold_days: 10,
      capital: 10_000_000,
    },
    effective: {
      start: '2025-02-05',
      end: '2026-10-19',
      first_signal_day: '2025-02-05',
      last_signal_day: '2026-10-19',
      first_entry_day: '2025-02-06',
      last_exit_day: '2026-10-20',
      trading_days: 414,
      signal_days: 414,
    },
    params: {
      horizon_days: 10,
      execution_lag_days: 1,
      entry_convention: 'close',
      exit_convention: 'close',
      price_adjust: 'qfq',
      fee_convention: 'none',
      tradability: 'not_modeled',
      benchmark: '000300.SH',
      cohort_mode: 'period',
    },
    summary: {
      total_return_pct: 91.2382,
      max_drawdown_pct: -12.8458,
      benchmark_return_pct: 8.0477,
      excess_return_pct: 83.1905,
      cohorts: 42,
      closed_cohorts: 41,
      open_cohorts: 1,
      win_cohorts: 28,
      loss_cohorts: 13,
      flat_cohorts: 0,
      cohort_win_rate_pct: 66.6667,
      avg_cohort_return_pct: 1.52,
      median_cohort_return_pct: 1.21,
      picks: 2100,
      priced_picks: 2093,
      pick_win_rate_pct: 58.2,
      avg_pick_return_pct: 0.71,
      price_coverage_pct: 99.7,
      data_quality: 'partial',
      initial_capital: 10_000_000,
      final_equity: 19_123_820,
      profit_amount: 9_123_820,
    },
    months: [
      {
        month: '2026-09',
        start_date: '2026-09-01',
        end_date: '2026-09-30',
        start_equity: 15_000_000,
        end_equity: 15_481_500,
        return_pct: 3.21,
        profit_amount: 481_500,
        cohorts: 2,
        win_rate_pct: 100,
        benchmark_return_pct: 0.8,
        excess_return_pct: 2.41,
        partial: false,
      },
      {
        month: '2026-10',
        start_date: '2026-10-08',
        end_date: '2026-10-20',
        start_equity: 15_481_500,
        end_equity: 15_249_277.5,
        return_pct: -1.5,
        profit_amount: -232_222.5,
        cohorts: 1,
        win_rate_pct: null,
        benchmark_return_pct: null,
        excess_return_pct: null,
        partial: true,
      },
    ],
    cohorts: [
      {
        index: 1,
        signal_date: '2026-09-01',
        entry_date: '2026-09-02',
        exit_date: '2026-09-16',
        mark_date: '2026-09-16',
        exit_kind: 'closed',
        held: 50,
        priced: 50,
        missing_entry: 0,
        missing_exit: 0,
        stale_exit: 0,
        return_pct: 3.21,
        benchmark_return_pct: 0.8,
        excess_return_pct: 2.41,
        equity: 15_481_500,
        partial: false,
        skipped: false,
      },
      {
        index: 42,
        signal_date: '2026-10-01',
        entry_date: '2026-10-09',
        exit_date: null,
        mark_date: '2026-10-20',
        exit_kind: 'open',
        held: 49,
        priced: 49,
        missing_entry: 1,
        missing_exit: 0,
        stale_exit: 2,
        return_pct: -1.5,
        benchmark_return_pct: null,
        excess_return_pct: null,
        equity: 15_249_277.5,
        partial: false,
        skipped: false,
      },
    ],
    points: [
      { date: '2025-02-06', equity: 10_000_000 },
      { date: '2026-09-16', equity: 15_481_500 },
      { date: '2026-10-20', equity: 15_249_277.5 },
    ],
    warnings: ['stale_exit', 'open_cohort'],
    quality: {
      missing_entry_price: 1,
      missing_exit_price: 0,
      stale_exit: 2,
      underfill_signals: 0,
      partial_cohorts: 0,
      skipped_cohorts: 0,
      open_cohorts: 1,
      signals: 414,
      requested_picks: 2100,
      priced_picks: 2093,
    },
  };
  return { ...base, ...over };
}

const BANNED_WORDS = ['买入', '卖出', '加仓', '减仓', '建仓', '清仓'];

function renderPanel(props: Partial<React.ComponentProps<typeof SelectionStatsPanel>> = {}) {
  return render(
    <SelectionStatsPanel
      modelId="m1"
      horizonDays={10}
      market="CN"
      marketLabel="A股"
      {...props}
    />,
  );
}

beforeEach(() => {
  fetchMock.mockReset();
  fetchMock.mockResolvedValue(stats());
});

describe('SelectionStatsPanel', () => {
  it('进入即自动按默认参数统计一次，并渲染总收益/基准/超额', async () => {
    renderPanel();

    expect(await screen.findByTestId('selection-stats-total-return')).toHaveTextContent(
      '+91.24%',
    );
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledWith('m1', { exclude_blacklist: false });
    // A 股红涨
    expect(screen.getByTestId('selection-stats-total-return').className).toContain(
      'text-red-600',
    );
    expect(screen.getByTestId('selection-stats-summary')).toHaveTextContent('+8.05%');
    expect(screen.getByTestId('selection-stats-summary')).toHaveTextContent('+83.19%');
    expect(screen.getByTestId('selection-stats-quality')).toHaveTextContent('数据不完整');
    expect(screen.getByTestId('selection-stats-chart')).toBeInTheDocument();
    expect(screen.getByText('月度拆解')).toBeInTheDocument();
  });

  it('月度拆解红涨绿跌，缺失胜率/基准显示 — 而不是 0', async () => {
    renderPanel();
    await screen.findByTestId('selection-stats-months');

    const months = screen.getByTestId('selection-stats-months');
    expect(months).toHaveTextContent('3.21%');
    expect(months).toHaveTextContent('-1.50%');
    expect(months.textContent).toContain('—');

    const red = Array.from(months.querySelectorAll('.text-red-600')).map(
      (node) => node.textContent,
    );
    const green = Array.from(months.querySelectorAll('.text-green-600')).map(
      (node) => node.textContent,
    );
    expect(red.join(' ')).toContain('3.21%');
    expect(green.join(' ')).toContain('-1.50%');
  });

  it('告警译成可读文案', async () => {
    renderPanel();
    const warnings = await screen.findByTestId('selection-stats-warnings');
    expect(warnings).toHaveTextContent('最后有效收盘价');
    expect(warnings).toHaveTextContent('未到期');
  });

  it('切「每日滚动」后计算，发送 hold_days=1 与当前资金/入选数', async () => {
    renderPanel();
    await screen.findByTestId('selection-stats-total-return');

    fireEvent.click(screen.getByText('每日滚动 T+1'));
    fireEvent.click(screen.getByTestId('selection-stats-calculate'));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(fetchMock.mock.calls[1][0]).toBe('m1');
    expect(fetchMock.mock.calls[1][1]).toMatchObject({
      start: '2025-02-05',
      end: '2026-10-19',
      top_k: 50,
      hold_days: 1,
      capital: 10_000_000,
    });
  });

  it('默认「按模型周期换股」使用模型训练周期', async () => {
    renderPanel({ horizonDays: 20 });
    await screen.findByTestId('selection-stats-total-return');

    fireEvent.click(screen.getByTestId('selection-stats-calculate'));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(fetchMock.mock.calls[1][1]).toMatchObject({ hold_days: 20 });
  });

  it('快捷区间以测试集全区间为锚点回推', async () => {
    renderPanel();
    await screen.findByTestId('selection-stats-total-return');

    fireEvent.click(screen.getByTestId('selection-stats-quick-3m'));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(fetchMock.mock.calls[1][1]).toMatchObject({
      start: '2026-07-19',
      end: '2026-10-19',
    });
  });

  it('展开各期明细：未到期与缺价备注可见', async () => {
    renderPanel();
    await screen.findByTestId('selection-stats-total-return');

    fireEvent.click(screen.getByText(/各期明细/));
    expect(await screen.findByTestId('selection-stats-cohorts')).toBeInTheDocument();
    expect(screen.getByText(/未到期 · 2 只按窗口内最后价估值/)).toBeInTheDocument();
  });

  it('失败时展示后端 message 并可重试', async () => {
    fetchMock.mockRejectedValueOnce(new Error('模型未找到 pred.parquet'));
    renderPanel();

    const error = await screen.findByTestId('selection-stats-error');
    expect(error).toHaveTextContent('模型未找到 pred.parquet');

    fetchMock.mockResolvedValueOnce(stats());
    fireEvent.click(screen.getByRole('button', { name: /重\s*试/ }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(await screen.findByTestId('selection-stats-total-return')).toBeInTheDocument();
  });

  it('非 A 股模型不请求后端，直接给出不可用说明', () => {
    renderPanel({ market: 'HK', marketLabel: '港股' });
    expect(screen.getByTestId('selection-stats-unsupported')).toHaveTextContent('港股');
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('没有选中模型时不发请求', () => {
    renderPanel({ modelId: null });
    expect(screen.getByText(/先在上方选择模型/)).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('展示面不出现方向性交易字眼', async () => {
    const { container } = renderPanel();
    await screen.findByTestId('selection-stats-total-return');
    for (const word of BANNED_WORDS) {
      expect(container.textContent).not.toContain(word);
    }
    expect(container.textContent).not.toContain('买');
    expect(container.textContent).not.toContain('卖');
  });
});
