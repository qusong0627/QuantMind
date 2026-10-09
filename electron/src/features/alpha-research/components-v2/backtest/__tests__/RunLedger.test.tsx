/**
 * RunLedger —— 运行台账（T-FB-15）。
 *
 * 钉死的边：
 * - 批次行展开拉 /batch/status 出单元表，单元「看报告」外发完整 onOpenRun；
 * - 因子明细勾选 2–5 条出对比表：收益类取大、回撤类取小，单值不判最优；
 * - 重取后滑出窗口的旧勾选被收敛清掉（否则界面锁死——回归守卫）；
 * - 市场/状态筛选只作用于可见集，对比列随之收敛。
 */
import React from 'react';
import { describe, test, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, act, within } from '@testing-library/react';
import { RunLedger } from '../RunLedger';
import type { LedgerRun } from '../../../types-v2/backtestCenter';

const mocks = vi.hoisted(() => ({
  listBatches: vi.fn(),
  getBatchStatus: vi.fn(),
  listRuns: vi.fn(),
  listBacktestMarkets: vi.fn(),
  cancelBatch: vi.fn(),
}));

vi.mock('../../../services-v2/factorBacktestApi', () => ({
  listBatches: mocks.listBatches,
  getBatchStatus: mocks.getBatchStatus,
  listRuns: mocks.listRuns,
  listBacktestMarkets: mocks.listBacktestMarkets,
  cancelBatch: mocks.cancelBatch,
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

const mkBatchListItem = () => ({
  batchId: 'bb-1',
  userId: '10000001',
  status: 'completed',
  error: null,
  createdAt: '2026-10-10T01:00:00Z',
  finishedAt: '2026-10-10T01:10:00Z',
  spec: { factorIds: ['fa'], markets: ['us_stock'], start: null, end: null, costBps: null, skipped: [] },
});

const mkBatchStatus = () => ({
  batch: {
    batchId: 'bb-1',
    userId: '10000001',
    status: 'completed',
    error: null,
    createdAt: '2026-10-10T01:00:00Z',
    finishedAt: '2026-10-10T01:10:00Z',
  },
  spec: { factorIds: ['fa'], markets: ['us_stock'], start: null, end: null, costBps: null, skipped: [] },
  progress: {
    pending: 0,
    running: 0,
    completed: 1,
    failed: 0,
    cancelled: 0,
    dataUnsupported: 0,
    insufficient: 0,
    unavailable: 0,
    total: 1,
    done: 1,
    consecFails: 0,
    maxConsecFails: 5,
    draining: true,
    current: [],
  },
  units: [
    {
      factorId: 'fa',
      market: 'us_stock',
      status: 'completed',
      runId: 'run-9',
      attempts: 1,
      error: null,
      finishedAt: '2026-10-10T01:10:00Z',
      ic: 0.02,
      rankIc: 0.03,
      icir: 0.5,
      sharpe: 1.1,
      maxDrawdown: 0.2,
      nDays: 250,
    },
  ],
  failures: [],
});

const mkRun = (over: Partial<LedgerRun>): LedgerRun => ({
  runId: 'ra',
  factorId: 'fa',
  factorName: '因子A',
  status: 'completed',
  kind: null,
  market: 'us_stock',
  universe: 'csi300',
  dataSource: 'quantdb_factors',
  dateRange: '2024-01-01~2024-12-31',
  error: null,
  metrics: {},
  hasSeries: true,
  createdAt: '2026-10-09T03:00:00Z',
  finishedAt: '2026-10-09T03:05:00Z',
  ...over,
});

beforeEach(() => {
  for (const m of Object.values(mocks)) m.mockReset();
  mocks.listBatches.mockResolvedValue({ success: true, data: [] });
  mocks.getBatchStatus.mockResolvedValue({ success: true, data: mkBatchStatus() });
  mocks.listRuns.mockResolvedValue({ success: true, data: [] });
  mocks.listBacktestMarkets.mockResolvedValue({
    success: true,
    data: [mkMarket('us_stock', '美股'), mkMarket('a_share', 'A股')],
  });
});

afterEach(() => {
  vi.clearAllMocks();
});

async function renderLedger(refreshToken = 0, onOpenRun = vi.fn()) {
  const utils = render(
    <RunLedger factorIds={['fa']} activeBatchId={null} refreshToken={refreshToken} onOpenRun={onOpenRun} />,
  );
  await act(async () => {});
  return { ...utils, onOpenRun };
}

describe('运行台账：批次历史', () => {
  test('展开批次拉单元表；单元「看报告」外发完整 onOpenRun（含市场标签）', async () => {
    mocks.listBatches.mockResolvedValue({ success: true, data: [mkBatchListItem()] });
    const { onOpenRun } = await renderLedger();

    fireEvent.click(screen.getByTestId('batch-row-bb-1'));
    await act(async () => {});

    expect(mocks.getBatchStatus).toHaveBeenCalledWith('bb-1');
    expect(screen.getByText('0.0300')).toBeTruthy(); // rankIc 4 位
    expect(screen.getByText('美股')).toBeTruthy();

    fireEvent.click(screen.getByText(/看报告/));
    expect(onOpenRun).toHaveBeenCalledWith(
      expect.objectContaining({
        factorId: 'fa',
        market: 'us_stock',
        marketLabel: '美股',
        runId: 'run-9',
        status: 'completed',
      }),
    );
  });
});

describe('运行台账：明细对比', () => {
  test('勾选两条出对比表：收益类高亮大值、回撤类高亮小值；单值不判最优', async () => {
    mocks.listRuns.mockResolvedValue({
      success: true,
      data: [
        mkRun({
          runId: 'ra',
          finishedAt: '2026-10-09T03:00:00Z',
          metrics: { ic: 0.04, rank_ic: 0.051, max_drawdown: 0.15, n_days: 250 },
        }),
        mkRun({
          runId: 'rb',
          market: 'a_share',
          finishedAt: '2026-10-08T03:00:00Z',
          metrics: { ic: 0.02, rank_ic: 0.03, max_drawdown: 0.3 },
        }),
      ],
    });
    await renderLedger();

    const boxes = screen.getAllByRole('checkbox');
    fireEvent.click(boxes[0]);
    fireEvent.click(boxes[1]);

    const compare = screen.getByTestId('ledger-compare');
    // IC 0.04 / Rank IC 0.051 / 回撤 15%（小者）都应高亮在 ra 列
    const bestTexts = Array.from(compare.querySelectorAll('td[data-best="1"]')).map(
      (c) => c.textContent,
    );
    expect(bestTexts).toContain('0.0400');
    expect(bestTexts).toContain('0.0510');
    expect(bestTexts).toContain('15.00%');
    // 回撤 30.00% 不是最优
    expect(within(compare).getByText('30.00%').closest('td')?.getAttribute('data-best')).toBeNull();
    // n_days 只有 ra 有值 → 单值不称最优
    expect(within(compare).getByText('250').closest('td')?.getAttribute('data-best')).toBeNull();

    // 市场筛选收敛可见集：只剩 us_stock 一条 → 对比区（需 ≥2）收起，rb 行消失
    fireEvent.change(screen.getByLabelText('按市场筛选'), { target: { value: 'us_stock' } });
    expect(screen.queryByText('0.0200')).toBeNull();
    expect(screen.queryByTestId('ledger-compare')).toBeNull();
  });

  test('重取后滑出窗口的勾选随可见集收敛：界面不锁死（回归）', async () => {
    mocks.listRuns.mockResolvedValue({
      success: true,
      data: Array.from({ length: 5 }, (_, i) =>
        mkRun({ runId: `r${i}`, metrics: { ic: 0.01 * (i + 1) } }),
      ),
    });
    const { rerender } = await renderLedger(0);

    const boxes = screen.getAllByRole('checkbox') as HTMLInputElement[];
    for (let i = 0; i < 5; i += 1) fireEvent.click(boxes[i]);
    expect(screen.getByText(/最多对比 5 次/)).toBeTruthy();

    // 重取后只剩 r0 还在窗口内，其余旧勾选已滑出
    mocks.listRuns.mockResolvedValue({
      success: true,
      data: [mkRun({ runId: 'r0' }), mkRun({ runId: 'rnew', metrics: { ic: 0.09 } })],
    });
    rerender(
      <RunLedger factorIds={['fa']} activeBatchId={null} refreshToken={1} onOpenRun={vi.fn()} />,
    );
    await act(async () => {});

    const fresh = screen.getAllByRole('checkbox') as HTMLInputElement[];
    expect(fresh[0].checked).toBe(true); // r0 勾选保留
    expect(fresh[1].checked).toBe(false);
    expect(fresh.some((b) => b.disabled)).toBe(false); // 未达上限，不锁死
    expect(screen.queryByText(/最多对比 5 次/)).toBeNull();
    expect(screen.queryByTestId('ledger-compare')).toBeNull(); // 不足 2 条 → 收起
  });
});
