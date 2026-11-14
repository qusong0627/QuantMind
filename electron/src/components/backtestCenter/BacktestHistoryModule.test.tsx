import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import { BacktestDetailModal } from './BacktestHistoryModule';
import { backtestService, type BacktestResult } from '../../services/backtestService';

vi.mock('../../services/backtestService', () => ({ backtestService: { getResult: vi.fn() } }));
vi.mock('../backtest/BacktestHistory', () => ({ BacktestHistory: () => null, resolveStrategyName: () => '动量策略' }));
vi.mock('echarts-for-react', () => ({ default: ({ option }: any) => <div data-testid="chart">{option.series[0].data.length} points</div> }));
vi.mock('framer-motion', () => ({ AnimatePresence: ({ children }: any) => children, motion: { div: ({ children, initial, animate, exit, ...props }: any) => <div {...props}>{children}</div> } }));
const summary = { backtest_id: 'run-1', total_return: .37 } as BacktestResult;
const full = {
  ...summary,
  equity_curve: [{ date: '2025-02-05', value: 1000000 }],
  drawdown_curve: [{ date: '2025-02-05', drawdown: 0 }],
  trades: Array.from({ length: 21 }, (_, i) => ({ date: '2025-02-05', symbol: i === 20 ? 'sz000001' : 'sh600000', action: i === 20 ? 'sell' : 'buy', price: 10, quantity: 100, amount: 1000, commission: 5 })),
} as BacktestResult;
beforeEach(() => vi.clearAllMocks());
it('loads full data from a summary and paginates and filters actual trades', async () => {
  vi.mocked(backtestService.getResult).mockResolvedValue(full);
  render(<BacktestDetailModal backtest={summary} onClose={() => {}} />);
  expect(screen.getByRole('status')).toBeInTheDocument();
  await screen.findByText('成交记录 · 21 笔');
  expect(backtestService.getResult).toHaveBeenCalledWith('run-1', false);
  expect(screen.getAllByTestId('chart')).toHaveLength(2);
  expect(screen.getAllByText('600000.SH')).toHaveLength(20);
  fireEvent.click(screen.getByText('下一页'));
  expect(screen.getByText('000001.SZ')).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText('成交方向'), { target: { value: 'sell' } });
  expect(screen.getByText('共 1 笔 · 第 1 / 1 页')).toBeInTheDocument();
});
it('shows a retryable error instead of claiming there are no trades', async () => {
  vi.mocked(backtestService.getResult).mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce(full);
  render(<BacktestDetailModal backtest={summary} onClose={() => {}} />);
  await screen.findByRole('alert');
  expect(screen.queryByText(/此记录没有已保存/)).not.toBeInTheDocument();
  fireEvent.click(screen.getByText('重新加载'));
  await screen.findByText('成交记录 · 21 笔');
});
it('ignores a stale response after switching records', async () => {
  let resolveFirst!: (value: BacktestResult) => void;
  vi.mocked(backtestService.getResult).mockImplementationOnce(() => new Promise((resolve) => { resolveFirst = resolve; })).mockResolvedValueOnce({ ...full, backtest_id: 'run-2', trades: [] });
  const { rerender } = render(<BacktestDetailModal backtest={summary} onClose={() => {}} />);
  rerender(<BacktestDetailModal backtest={{ ...summary, backtest_id: 'run-2' }} onClose={() => {}} />);
  await screen.findByText('成交记录 · 0 笔');
  resolveFirst(full);
  await waitFor(() => expect(screen.queryByText('成交记录 · 21 笔')).not.toBeInTheDocument());
});
