/**
 * BatchDispatchPanel —— 批量派发台（T-FB-12）。
 *
 * 钉死的边：
 * - 单批上限 200 为**真闸门**：超限禁用派发 + 可操作提示，不静默截断；
 * - 派发成功回调 onBatchDispatched（页级 driver），跳过清单原文可见；
 * - 批次终态恰好回调一次 onBatchSettled（驱动矩阵/台账重取），轮询不重复触发；
 * - 运行中提供取消（POST /batch/cancel）。
 */
import React, { useState } from 'react';
import { describe, test, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, act } from '@testing-library/react';
import { BatchDispatchPanel } from '../BatchDispatchPanel';

const mocks = vi.hoisted(() => ({
  getFactors: vi.fn(),
  listBacktestMarkets: vi.fn(),
  launchBatch: vi.fn(),
  getBatchStatus: vi.fn(),
  cancelBatch: vi.fn(),
}));

vi.mock('../../../services-v2/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../../services-v2/api')>();
  return { ...actual, getFactors: mocks.getFactors };
});
vi.mock('../../../services-v2/factorBacktestApi', () => ({
  getFactors: undefined,
  listBacktestMarkets: mocks.listBacktestMarkets,
  launchBatch: mocks.launchBatch,
  getBatchStatus: mocks.getBatchStatus,
  cancelBatch: mocks.cancelBatch,
}));

const mkFactor = (id: string, name: string) =>
  ({ factorId: id, factorName: name, ic: 0.01 }) as any;

const mkMarket = (market: string, label: string, ready = true) =>
  ({
    market,
    qlibMarket: market,
    label,
    inSample: market === 'a_share',
    experimental: market === 'futures',
    note: null,
    ready,
    calendarStart: '2018-01-01',
    calendarEnd: '2026-10-01',
    instruments: 300,
    columns: [],
    universeMode: '',
    defaultUniverse: null,
    universeTopN: null,
    windowYears: 5,
    costBps: 10,
    benchmark: null,
    minDays: 250,
  }) as any;

const mkBatchStatus = (status: string, progressOver: Record<string, unknown> = {}) => ({
  batch: {
    batchId: 'bb-1',
    userId: '10000001',
    status,
    error: null,
    createdAt: '2026-10-10T01:00:00Z',
    finishedAt: status === 'running' ? null : '2026-10-10T01:10:00Z',
  },
  spec: { factorIds: ['f0'], markets: ['us_stock'], start: null, end: null, costBps: null, skipped: [] },
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
    ...progressOver,
  },
  units: [],
  failures: [],
});

/** 复刻页级 wiring：activeBatchId 受控（派发回填），其余选择项与页同构 */
const Harness: React.FC<{
  onSettled?: () => void;
  onDispatched?: (id: string) => void;
}> = ({ onSettled, onDispatched }) => {
  const [ids, setIds] = useState<string[]>([]);
  const [markets, setMarkets] = useState<string[] | null>(null);
  const [activeId, setActiveId] = useState<string | null>(null);
  return (
    <BatchDispatchPanel
      factorIds={ids}
      onFactorIdsChange={setIds}
      markets={markets}
      onMarketsChange={setMarkets}
      activeBatchId={activeId}
      onBatchDispatched={(id) => {
        setActiveId(id);
        onDispatched?.(id);
      }}
      onBatchSettled={onSettled ?? (() => {})}
    />
  );
};

beforeEach(() => {
  for (const m of Object.values(mocks)) m.mockReset();
  mocks.getFactors.mockResolvedValue({
    success: true,
    data: { total: 2, factors: [mkFactor('f0', '因子零'), mkFactor('f1', '因子一')] },
  });
  mocks.listBacktestMarkets.mockResolvedValue({
    success: true,
    data: [
      mkMarket('a_share', 'A股'),
      mkMarket('us_stock', '美股'),
      mkMarket('futures', '期货', false),
    ],
  });
});

afterEach(() => {
  vi.clearAllMocks();
});

async function renderHarness(props: React.ComponentProps<typeof Harness> = {}) {
  const utils = render(<Harness {...props} />);
  await act(async () => {});
  return utils;
}

describe('批量派发台', () => {
  test('超过单批上限 200：禁用派发并给可操作提示，不静默截断（201 个仍显示 201）', async () => {
    mocks.getFactors.mockResolvedValue({
      success: true,
      data: {
        total: 201,
        factors: Array.from({ length: 201 }, (_, i) => mkFactor(`f${i}`, `因子${i}`)),
      },
    });
    await renderHarness();

    fireEvent.click(screen.getByText('全选全部'));

    expect(screen.getByText('因子（201 已选）')).toBeTruthy();
    expect((screen.getByTestId('launch-batch') as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText(/单批上限 200 个因子/)).toBeTruthy();
  });

  test('派发成功：回调 batchId、渲染排队结论与跳过清单原文', async () => {
    const onDispatched = vi.fn();
    const onSettled = vi.fn();
    mocks.launchBatch.mockResolvedValue({
      success: true,
      data: {
        batchId: 'bb-1',
        total: 2,
        queued: 1,
        skipped: [{ factorId: 'f1', market: 'futures', reason: '市场未就绪' }],
        status: 'running',
        message: '已排队 1 个单元，引擎侧排水',
      },
    });
    mocks.getBatchStatus.mockResolvedValue({ success: true, data: mkBatchStatus('completed') });
    await renderHarness({ onDispatched, onSettled });

    fireEvent.click(screen.getAllByRole('checkbox')[0]);
    await act(async () => {
      fireEvent.click(screen.getByTestId('launch-batch'));
    });
    await act(async () => {}); // 首轮 poll 即终态

    expect(mocks.launchBatch).toHaveBeenCalledWith(
      expect.objectContaining({ factorIds: ['f0'] }),
    );
    expect(onDispatched).toHaveBeenCalledWith('bb-1');
    expect(screen.getByText('已排队 1 个单元，引擎侧排水')).toBeTruthy();
    expect(screen.getByText(/跳过 1 个/)).toBeTruthy();
    expect(screen.getByText(/市场未就绪/)).toBeTruthy();
    expect(screen.getByTestId('batch-progress')).toBeTruthy();
    // 终态恰一次（首拉即 completed 不再有定时器）
    expect(onSettled).toHaveBeenCalledTimes(1);
  });

  test('轮询 running→completed：onBatchSettled 恰好一次，终态后不再轮询', async () => {
    vi.useFakeTimers();
    try {
      const onSettled = vi.fn();
      mocks.launchBatch.mockResolvedValue({
        success: true,
        data: { batchId: 'bb-1', total: 1, queued: 1, skipped: [], status: 'running', message: 'ok' },
      });
      mocks.getBatchStatus
        .mockResolvedValueOnce({
          success: true,
          data: mkBatchStatus('running', { done: 0, completed: 0, running: 1 }),
        })
        .mockResolvedValueOnce({ success: true, data: mkBatchStatus('completed') });

      render(<Harness onSettled={onSettled} />);
      await act(async () => {});
      fireEvent.click(screen.getAllByRole('checkbox')[0]);
      await act(async () => {
        fireEvent.click(screen.getByTestId('launch-batch'));
      });
      await act(async () => {}); // poll#1 → running

      expect(screen.getByText('排水运行中')).toBeTruthy();
      expect(onSettled).not.toHaveBeenCalled();

      await act(async () => {
        await vi.advanceTimersByTimeAsync(4100); // poll#2 → completed
      });
      expect(onSettled).toHaveBeenCalledTimes(1);
      expect(screen.getByText('已完成')).toBeTruthy();

      await act(async () => {
        await vi.advanceTimersByTimeAsync(30000);
      });
      expect(onSettled).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });

  test('运行中可取消：POST /batch/cancel 收到 batchId 并进入已取消态', async () => {
    mocks.launchBatch.mockResolvedValue({
      success: true,
      data: { batchId: 'bb-1', total: 1, queued: 1, skipped: [], status: 'running', message: 'ok' },
    });
    mocks.getBatchStatus
      .mockResolvedValueOnce({
        success: true,
        data: mkBatchStatus('running', { done: 0, completed: 0, running: 1 }),
      })
      .mockResolvedValue({ success: true, data: mkBatchStatus('cancelled') });
    mocks.cancelBatch.mockResolvedValue({ success: true, data: { killed: 1, closed: true } });
    await renderHarness();

    fireEvent.click(screen.getAllByRole('checkbox')[0]);
    await act(async () => {
      fireEvent.click(screen.getByTestId('launch-batch'));
    });
    await act(async () => {}); // poll#1 → running

    await act(async () => {
      fireEvent.click(screen.getByText('取消批次'));
    });
    expect(mocks.cancelBatch).toHaveBeenCalledWith('bb-1');
    expect(screen.getByText('已取消')).toBeTruthy();
  });
});
