/**
 * MatrixHeatmap —— 因子 × 市场适配矩阵（T-FB-13）。
 *
 * 钉死的边：
 * - 缺失一律「—」，绝不显示成 0（completed 但指标键缺席 = 缺失）；
 * - 降级/未跑格子显示状态词（数据不支持 / 待回测 / 待裁决），不是数字；
 * - Best Market 徽标**仅样本外列且 ≥2 个有效值**才判（CN 样本内不参与，
 *   单值不称「最佳」）；
 * - 市场排名模式每列标注 № 名次；
 * - 点格外发完整 DrillTarget（runId/status/metrics），空因子时零请求。
 */
import React from 'react';
import { describe, test, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, act } from '@testing-library/react';
import { MatrixHeatmap } from '../MatrixHeatmap';
import type { MatrixCell, MatrixResult } from '../../../types-v2/backtestCenter';

const mocks = vi.hoisted(() => ({
  fetchMatrix: vi.fn(),
}));

vi.mock('../../../services-v2/factorBacktestApi', async (importOriginal) => {
  const actual =
    await importOriginal<typeof import('../../../services-v2/factorBacktestApi')>();
  return { ...actual, fetchMatrix: mocks.fetchMatrix };
});
// jsdom 无 canvas
vi.mock('../../../../../components/common/EChartsChart', () => ({
  EChartsChart: () => <div data-testid="chart" />,
}));

const mkCell = (over: Partial<MatrixCell> = {}): MatrixCell => ({
  status: 'completed',
  runId: 'run-1',
  compat: 'portable',
  missing: [],
  dynamic: false,
  error: null,
  universe: 'csi300',
  dateRange: '2024-01-01~2024-12-31',
  finishedAt: '2026-10-10T02:00:00Z',
  inSample: false,
  metrics: {},
  ...over,
});

const RESULT: MatrixResult = {
  markets: [
    { market: 'a_share', label: 'A股', inSample: true, experimental: false, benchmark: 'CSI300', costBps: 3 },
    { market: 'us_stock', label: '美股', inSample: false, experimental: false, benchmark: null, costBps: 10 },
    { market: 'hong_kong', label: '港股', inSample: false, experimental: false, benchmark: null, costBps: 12 },
    { market: 'crypto', label: '区块链', inSample: false, experimental: true, benchmark: null, costBps: 20 },
  ],
  factors: [
    {
      factorId: 'fa',
      factorName: '因子A',
      found: true,
      owned: true,
      cnIc: 0.011,
      cells: {
        a_share: mkCell({ inSample: true, metrics: { rank_ic: 0.05 } }),
        us_stock: mkCell({ metrics: { rank_ic: 0.08 } }),
        hong_kong: mkCell({ metrics: { rank_ic: 0.03 } }),
        crypto: mkCell({ status: 'data_unsupported', runId: null }),
      },
    },
    {
      factorId: 'fb',
      factorName: '因子B',
      found: true,
      owned: true,
      cnIc: null,
      cells: {
        a_share: mkCell({ inSample: true, metrics: { rank_ic: 0.02 } }),
        // completed 但指标键缺席 → 必须显「—」，不是 0
        us_stock: mkCell({ metrics: {} }),
        hong_kong: mkCell({ status: 'not_run', runId: null, compat: 'portable' }),
        crypto: mkCell({ status: 'not_run', runId: null, compat: 'unknown' }),
      },
    },
  ],
  counts: { completed: 6, data_unsupported: 1, not_run: 2, failed: 0 },
};

beforeEach(() => {
  mocks.fetchMatrix.mockReset();
  mocks.fetchMatrix.mockResolvedValue({ success: true, data: RESULT });
});

afterEach(() => {
  vi.clearAllMocks();
});

async function renderMatrix(factorIds: string[] = ['fa', 'fb']) {
  const utils = render(
    <MatrixHeatmap factorIds={factorIds} markets={null} refreshToken={0} onOpenCell={() => {}} />,
  );
  await act(async () => {});
  return utils;
}

describe('适配矩阵', () => {
  test('完成格显值；completed 但指标缺失显「—」；降级/未跑显状态词', async () => {
    await renderMatrix();

    expect(screen.getByTestId('matrix-cell-fa-us_stock').textContent).toContain('0.0800');
    // completed 但 metrics 里没有 rank_ic → 缺失「—」，绝不显示成 0
    expect(screen.getByTestId('matrix-cell-fb-us_stock').textContent).toBe('—');
    expect(screen.getByTestId('matrix-cell-fa-crypto').textContent).toBe('数据不支持');
    expect(screen.getByTestId('matrix-cell-fb-hong_kong').textContent).toBe('待回测');
    expect(screen.getByTestId('matrix-cell-fb-crypto').textContent).toBe('待裁决');
    // CN·IC 缺失（fb）显「—」；连同 fb-us 的缺失格共有 ≥2 个「—」占位
    expect(screen.getAllByText('—', { selector: 'td span' }).length).toBeGreaterThanOrEqual(2);
  });

  test('Best 徽标仅样本外且 ≥2 有效值：fa→美股；单值因子 fb 不判', async () => {
    await renderMatrix();

    // fa：样本外有 us 0.08 / hk 0.03 两个值 → us 最佳
    expect(screen.getByTestId('matrix-cell-fa-us_stock').getAttribute('data-best')).toBe('1');
    expect(screen.getByTestId('matrix-cell-fa-hong_kong').getAttribute('data-best')).toBeNull();
    // CN（样本内）即便有值也不参与「最佳市场」
    expect(screen.getByTestId('matrix-cell-fa-a_share').getAttribute('data-best')).toBeNull();
    // fb：样本外只有 us 一个完成值（另两个未跑）→ 单值不称最佳
    expect(screen.getByTestId('matrix-cell-fb-us_stock').getAttribute('data-best')).toBeNull();
  });

  test('市场排名模式：每列标注 № 名次（数值大者 №1）', async () => {
    await renderMatrix();

    fireEvent.click(screen.getByText('市场排名'));
    // us 列：fa 0.08 №1，fb 缺值无名次
    expect(screen.getByTestId('matrix-cell-fa-us_stock').textContent).toContain('№1');
    expect(screen.getByTestId('matrix-cell-fb-us_stock').textContent).not.toContain('№');
  });

  test('点格外发完整 DrillTarget（runId/status/metrics）', async () => {
    const onOpenCell = vi.fn();
    render(
      <MatrixHeatmap factorIds={['fa', 'fb']} markets={null} refreshToken={0} onOpenCell={onOpenCell} />,
    );
    await act(async () => {});

    fireEvent.click(screen.getByTestId('matrix-cell-fa-us_stock'));

    expect(onOpenCell).toHaveBeenCalledWith(
      expect.objectContaining({
        factorId: 'fa',
        factorName: '因子A',
        market: 'us_stock',
        runId: 'run-1',
        status: 'completed',
        metrics: expect.objectContaining({ rank_ic: 0.08 }),
      }),
    );
  });

  test('未选因子：引导空态、零请求', async () => {
    await renderMatrix([]);

    expect(screen.getByText(/先在派发台选择因子/)).toBeTruthy();
    expect(mocks.fetchMatrix).not.toHaveBeenCalled();
  });
});
