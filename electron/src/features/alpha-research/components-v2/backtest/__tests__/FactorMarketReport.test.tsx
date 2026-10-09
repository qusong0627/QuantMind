/**
 * FactorMarketReport —— 单因子 × 市场报告抽屉（T-FB-14）。
 *
 * 钉死的边：
 * - 降级终态（data_unsupported 等）：给出状态 + 原因原文，不拉序列、图表页签禁用
 *   ——诚实降级，不画空图；
 * - 完成：拉 /runs/{id}/series，概览出标量卡；基准是等权兜底时必须显著标注
 *   （不能冒充指数超额）；
 * - Esc / 背板关闭。
 */
import React from 'react';
import { describe, test, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, act } from '@testing-library/react';
import { FactorMarketReport } from '../FactorMarketReport';
import type { DrillTarget, RunSeries } from '../../../types-v2/backtestCenter';

const mocks = vi.hoisted(() => ({
  getRunSeries: vi.fn(),
  fetchMatrix: vi.fn(),
}));

vi.mock('../../../services-v2/factorBacktestApi', () => ({
  getRunSeries: mocks.getRunSeries,
  fetchMatrix: mocks.fetchMatrix,
}));
// jsdom 无 canvas
vi.mock('../../../../../components/common/EChartsChart', () => ({
  EChartsChart: () => <div data-testid="chart" />,
}));

const mkSeries = (): RunSeries => ({
  dates: ['2024-01-01', '2024-01-02', '2024-01-03'],
  ic: [0.1, 0.2, -0.05],
  icCum: [0.1, 0.3, 0.25],
  navLong: [1, 1.01, 1.02],
  navLs: [1, 1.015, 1.01],
  navBench: [1, 1.005, 1.01],
  qCurves: { q1: [1, 0.99, 0.98], q2: [1, 1.02, 1.03] },
  turnover: [0.1, 0.1, 0.12],
  coverage: [300, 300, 298],
  bench: 'equal_weight',
  meta: { costBps: 10, topPct: 0.1, nBuckets: 2, turnoverConvention: '双边' },
});

const mkRun = () => ({
  runId: 'run-1',
  factorId: 'fa',
  factorName: '因子A',
  status: 'completed',
  kind: null,
  market: 'us_stock',
  universe: 'top300',
  dataSource: 'quantdb_factors',
  dateRange: '2024-01-01~2024-12-31',
  error: null,
  metrics: { rank_ic: 0.03, n_days: 250 },
  hasSeries: true,
  createdAt: '2026-10-10T01:00:00Z',
  finishedAt: '2026-10-10T01:05:00Z',
});

const TARGET_COMPLETED: DrillTarget = {
  factorId: 'fa',
  factorName: '因子A',
  market: 'us_stock',
  marketLabel: '美股（样本外）',
  runId: 'run-1',
  status: 'completed',
  metrics: { rank_ic: 0.03, n_days: 250 },
};

const TARGET_DEGRADED: DrillTarget = {
  factorId: 'fa',
  factorName: '因子A',
  market: 'crypto',
  marketLabel: '区块链（样本外）',
  runId: 'run-x',
  status: 'data_unsupported',
  error: '列 vol_persistence_20 在本市场不存在',
  dateRange: '2024-01-01~2024-12-31',
};

beforeEach(() => {
  mocks.getRunSeries.mockReset();
  mocks.getRunSeries.mockResolvedValue({
    success: true,
    data: { run: mkRun(), series: mkSeries() },
  });
});

afterEach(() => {
  vi.clearAllMocks();
});

describe('因子报告抽屉', () => {
  test('降级终态：显示原因原文与诚实提示，图表页签禁用、不拉序列', async () => {
    render(<FactorMarketReport target={TARGET_DEGRADED} onClose={vi.fn()} />);
    await act(async () => {});

    expect(screen.getByText(/降级终态（数据不支持）/)).toBeTruthy();
    expect(screen.getByText(/该因子的代码依赖本市场不存在的列/)).toBeTruthy();
    expect(screen.getByText(/列 vol_persistence_20 在本市场不存在/)).toBeTruthy();
    expect((screen.getByRole('button', { name: 'IC' }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole('button', { name: '分组' }) as HTMLButtonElement).disabled).toBe(true);
    expect(mocks.getRunSeries).not.toHaveBeenCalled();
  });

  test('完成：拉序列、概览出标量卡，等权兜底显著标注', async () => {
    render(<FactorMarketReport target={TARGET_COMPLETED} onClose={vi.fn()} />);
    await act(async () => {});

    expect(mocks.getRunSeries).toHaveBeenCalledWith('run-1');
    // 标量卡（后端原始键 → 显示标签）
    expect(screen.getByText('Rank IC')).toBeTruthy();
    expect(screen.getByText('0.0300')).toBeTruthy();
    expect(screen.getByText('有效天数')).toBeTruthy();
    expect(screen.getByText('250')).toBeTruthy();
    // 等权兜底不是指数超额——必须标注
    expect(screen.getByText(/等权兜底/)).toBeTruthy();
    expect(screen.getByTestId('chart')).toBeTruthy();
    // 曲线页签可用
    expect((screen.getByRole('button', { name: 'IC' }) as HTMLButtonElement).disabled).toBe(false);
  });

  test('Esc 关闭', async () => {
    const onClose = vi.fn();
    render(<FactorMarketReport target={TARGET_COMPLETED} onClose={onClose} />);
    await act(async () => {});

    fireEvent.keyDown(window, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  test('target 为 null：不渲染抽屉', () => {
    render(<FactorMarketReport target={null} onClose={vi.fn()} />);
    expect(screen.queryByTestId('factor-report')).toBeNull();
  });
});
