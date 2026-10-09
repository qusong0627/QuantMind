/**
 * BacktestPage（回测中心）—— 三区装配与页级 wiring。
 *
 * 钉死的边：
 * - 三区俱全（派发台 / 适配矩阵 / 运行台账），未选因子时矩阵零请求 + 引导空态；
 * - 页级因子选择（派发台「全选」）驱动矩阵按同一列表取数；
 * - 矩阵格点击 → 报告抽屉打开（页级 drill 状态装配，含序列拉取）。
 */
import React from 'react';
import { describe, test, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, act, within } from '@testing-library/react';
import { BacktestPage } from '../BacktestPage';
import type { MatrixResult } from '../../types-v2/backtestCenter';

const mocks = vi.hoisted(() => ({
  getFactors: vi.fn(),
  fetchMatrix: vi.fn(),
  listBacktestMarkets: vi.fn(),
  launchBatch: vi.fn(),
  getBatchStatus: vi.fn(),
  cancelBatch: vi.fn(),
  listBatches: vi.fn(),
  listRuns: vi.fn(),
  getRunSeries: vi.fn(),
  getRunReport: vi.fn(),
}));

vi.mock('../../services-v2/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../services-v2/api')>();
  return { ...actual, getFactors: mocks.getFactors };
});
vi.mock('../../services-v2/factorBacktestApi', () => ({
  fetchMatrix: mocks.fetchMatrix,
  listBacktestMarkets: mocks.listBacktestMarkets,
  launchBatch: mocks.launchBatch,
  getBatchStatus: mocks.getBatchStatus,
  cancelBatch: mocks.cancelBatch,
  listBatches: mocks.listBatches,
  listRuns: mocks.listRuns,
  getRunSeries: mocks.getRunSeries,
  getRunReport: mocks.getRunReport,
}));
// jsdom 无 canvas
vi.mock('../../../../components/common/EChartsChart', () => ({
  EChartsChart: () => <div data-testid="chart" />,
}));

const mkMarket = (market: string, label: string) =>
  ({
    market,
    qlibMarket: market,
    label,
    inSample: market === 'a_share',
    experimental: false,
    note: null,
    ready: true,
    calendarStart: null,
    calendarEnd: null,
    instruments: null,
    columns: [],
    universeMode: '',
    defaultUniverse: null,
    universeTopN: null,
    windowYears: 5,
    costBps: 10,
    benchmark: null,
    minDays: 250,
  }) as any;

const MATRIX: MatrixResult = {
  markets: [
    { market: 'a_share', label: 'A股', inSample: true, experimental: false, benchmark: 'CSI300', costBps: 3 },
    { market: 'us_stock', label: '美股', inSample: false, experimental: false, benchmark: null, costBps: 10 },
  ],
  factors: [
    {
      factorId: 'fa',
      factorName: '因子A',
      found: true,
      owned: true,
      cnIc: 0.01,
      cells: {
        a_share: {
          status: 'completed',
          runId: 'run-cn',
          compat: 'portable',
          missing: [],
          dynamic: false,
          error: null,
          universe: 'csi300',
          dateRange: '2024-01-01~2024-12-31',
          finishedAt: null,
          inSample: true,
          metrics: { rank_ic: 0.05 },
        },
        us_stock: {
          status: 'completed',
          runId: 'run-1',
          compat: 'portable',
          missing: [],
          dynamic: false,
          error: null,
          universe: 'top300',
          dateRange: '2024-01-01~2024-12-31',
          finishedAt: null,
          inSample: false,
          metrics: { rank_ic: 0.08 },
        },
      },
    },
  ],
  counts: { completed: 2, not_run: 0, failed: 0 },
};

beforeEach(() => {
  for (const m of Object.values(mocks)) m.mockReset();
  mocks.getFactors.mockResolvedValue({
    success: true,
    data: { total: 1, factors: [{ factorId: 'fa', factorName: '因子A', ic: 0.01 }] },
  });
  mocks.listBacktestMarkets.mockResolvedValue({
    success: true,
    data: [mkMarket('a_share', 'A股'), mkMarket('us_stock', '美股')],
  });
  mocks.fetchMatrix.mockResolvedValue({ success: true, data: MATRIX });
  mocks.listBatches.mockResolvedValue({ success: true, data: [] });
  mocks.listRuns.mockResolvedValue({ success: true, data: [] });
  mocks.getRunSeries.mockResolvedValue({
    success: true,
    data: {
      run: {
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
        metrics: { rank_ic: 0.08 },
        hasSeries: true,
        createdAt: '2026-10-10T01:00:00Z',
        finishedAt: '2026-10-10T01:05:00Z',
      },
      series: {
        dates: ['2024-01-01', '2024-01-02'],
        ic: [0.1, 0.2],
        icCum: [0.1, 0.3],
        navLong: [1, 1.01],
        navLs: [1, 1.015],
        navBench: [1, 1.005],
        qCurves: {},
        turnover: [0.1, 0.1],
        coverage: [300, 300],
        bench: 'equal_weight',
        meta: { costBps: 10, topPct: 0.1, nBuckets: 2, turnoverConvention: '双边' },
      },
    },
  });
  mocks.getRunReport.mockResolvedValue({
    success: true,
    data: {
      run: { runId: 'run-1', factorId: 'fa', status: 'completed', metrics: {} },
      report: { available: true, status: 'completed' },
    },
  });
});

afterEach(() => {
  vi.clearAllMocks();
});

describe('回测中心（页面装配）', () => {
  test('三区俱全；未选因子时矩阵引导空态、零取数', async () => {
    render(<BacktestPage />);
    await act(async () => {});

    expect(screen.getByText('回测中心')).toBeTruthy();
    expect(screen.getByTestId('batch-dispatch')).toBeTruthy();
    expect(screen.getByTestId('matrix-heatmap')).toBeTruthy();
    expect(screen.getByTestId('run-ledger-batches')).toBeTruthy();
    expect(screen.getByTestId('run-ledger-runs')).toBeTruthy();

    // 矩阵与台账各自的引导空态
    expect(screen.getByText(/先在派发台选择因子——矩阵将并排展示/)).toBeTruthy();
    expect(screen.getByText(/先在派发台选择因子——这里会列出/)).toBeTruthy();
    expect(mocks.fetchMatrix).not.toHaveBeenCalled();
    expect(mocks.listRuns).not.toHaveBeenCalled();
  });

  test('派发台全选 → 矩阵按同一因子列表取数；点格 → 报告抽屉打开并拉序列', async () => {
    render(<BacktestPage />);
    await act(async () => {});

    fireEvent.click(screen.getByText('全选全部'));
    await act(async () => {});

    expect(mocks.fetchMatrix).toHaveBeenCalledWith({ factorIds: ['fa'], markets: null });
    expect(screen.getByTestId('matrix-cell-fa-us_stock')).toBeTruthy();
    // 页级选择同样驱动台账明细
    expect(mocks.listRuns).toHaveBeenCalledWith(expect.objectContaining({ factorId: 'fa' }));

    fireEvent.click(screen.getByTestId('matrix-cell-fa-us_stock'));
    await act(async () => {});

    expect(mocks.getRunSeries).toHaveBeenCalledWith('run-1');
    expect(mocks.getRunReport).toHaveBeenCalledWith('run-1');
    const drawer = screen.getByTestId('factor-report');
    expect(within(drawer).getByText('因子A')).toBeTruthy();
    expect(within(drawer).getByText('美股（样本外）')).toBeTruthy();
  });
});
