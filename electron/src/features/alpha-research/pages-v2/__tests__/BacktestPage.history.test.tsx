/**
 * BacktestPage —— 回测历史与对比（用户原话「每次单个因子回测的历史数据，后面好对比」）。
 *
 * 钉死的边：
 * - 挂载即拉该因子的历史（listFactorBacktests(taskId)），任务到终态自动重取；
 * - 缺失指标一律「—」，不补 0；失败运行的报错原文可见；
 * - 勾选 ≥2 条出对比表（指标行 × 运行列），每行高亮最优值（收益类取大、回撤类取小）；
 * - 只有一条运行有值的指标行不判「最优」（单值不称最优）；
 * - 最多同时对比 5 条，超出后其余复选框禁用并给提示；
 * - 重取后滑出窗口的旧勾选被收敛清掉（否则对比区消失而复选框全禁用，界面锁死）；
 * - 无任务时不渲染历史卡、不发请求。
 */
import React from 'react';
import { describe, test, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, within, act } from '@testing-library/react';
import { BacktestPage } from '../BacktestPage';

const { listFactorBacktestsMock } = vi.hoisted(() => ({
  listFactorBacktestsMock: vi.fn(),
}));

vi.mock('../../services-v2/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../services-v2/api')>();
  return {
    ...actual,
    listFactorBacktests: listFactorBacktestsMock,
    listFactorLibraries: vi.fn().mockResolvedValue({ success: true, data: { libraries: [] } }),
    getFactors: vi.fn().mockResolvedValue({ success: true, data: { total: 0, factors: [] } }),
    getUniverses: vi.fn().mockResolvedValue({ success: true, data: { universes: [] } }),
  };
});

// recharts 在 jsdom 里没有意义
vi.mock('recharts', () => ({
  ResponsiveContainer: () => null,
  AreaChart: () => null,
  Area: () => null,
  XAxis: () => null,
  YAxis: () => null,
  CartesianGrid: () => null,
  Tooltip: () => null,
}));

const ctxState: { task: any } = { task: null };
vi.mock('../../context-v2/TaskContext', () => ({
  useTaskContext: () => ({
    backendAvailable: true,
    backtestTask: ctxState.task,
    backtestLogs: [],
    startBacktestTask: vi.fn(),
    stopBacktestTask: vi.fn(),
  }),
}));

const mkTask = (status: string) => ({
  taskId: 'f1',
  status,
  progress: { phase: '', progress: 0, message: '', timestamp: '' },
  logs: [],
  metrics: {},
  config: {},
  createdAt: '2026-10-09T02:00:00Z',
  updatedAt: '2026-10-09T02:00:00Z',
});

const run = (over: Record<string, any>) => ({
  runId: 'r0',
  status: 'completed',
  market: 'a_share',
  universe: 'csi300',
  dataSource: 'qlib_bin',
  dateRange: '2024-01-01~2024-12-31',
  startedAt: '2026-10-09T03:00:00Z',
  finishedAt: '2026-10-09T03:02:00Z',
  error: null,
  metrics: {},
  ...over,
});

const THREE_RUNS = [
  run({ runId: 'r2', startedAt: '2026-10-09T03:00:00Z', universe: 'csi500', metrics: { ic: 0.05, icir: 0.6, annualReturn: 0.2, sharpeRatio: 1.5, maxDrawdown: 0.15 } }),
  run({ runId: 'r1', startedAt: '2026-10-08T03:00:00Z', status: 'failed', dataSource: 'h5', dateRange: null, error: 'RuntimeError: 因子炸了' }),
  run({ runId: 'r3', startedAt: '2026-10-07T03:00:00Z', universe: 'csi300', metrics: { ic: 0.02, icir: 0.4, annualReturn: 0.1, sharpeRatio: 1.1, maxDrawdown: 0.3 } }),
];

beforeEach(() => {
  listFactorBacktestsMock.mockReset();
  listFactorBacktestsMock.mockResolvedValue({ success: true, data: { runs: THREE_RUNS } });
  ctxState.task = mkTask('completed');
});

afterEach(() => {
  vi.clearAllMocks();
});

async function renderPage() {
  const utils = render(<BacktestPage />);
  await act(async () => {});
  return utils;
}

describe('回测历史：列表', () => {
  test('挂载即拉历史；运行行渲染状态/配置/指标，缺失显示「—」，失败显示原文', async () => {
    await renderPage();

    expect(listFactorBacktestsMock).toHaveBeenCalledWith('f1', expect.any(Number));
    expect(screen.getByTestId('backtest-history')).toBeTruthy();

    // 完成行：池（UNIVERSE_LABELS 中文）· 数据源 · IC 值
    expect(screen.getByText(/中证500/)).toBeTruthy();
    expect(screen.getByText('0.0500')).toBeTruthy();
    expect(screen.getByText('20.00%')).toBeTruthy();

    // 失败行：原文可见；指标列「—」占位（不补 0）
    expect(screen.getByText('RuntimeError: 因子炸了')).toBeTruthy();
    expect(screen.getAllByText('—').length).toBeGreaterThan(0);
  });

  test('无任务时不渲染历史卡、不发请求', async () => {
    ctxState.task = null;
    await renderPage();

    expect(screen.queryByTestId('backtest-history')).toBeNull();
    expect(listFactorBacktestsMock).not.toHaveBeenCalled();
  });

  test('任务状态变化（运行→完成）触发重取', async () => {
    ctxState.task = mkTask('running');
    const { rerender } = render(<BacktestPage />);
    await act(async () => {});
    expect(listFactorBacktestsMock).toHaveBeenCalledTimes(1);

    ctxState.task = mkTask('completed');
    rerender(<BacktestPage />);
    await act(async () => {});
    expect(listFactorBacktestsMock).toHaveBeenCalledTimes(2);
  });

  test('接口失败显示原文，不静默空表', async () => {
    listFactorBacktestsMock.mockResolvedValue({ success: false, error: '查询回测历史失败' });
    await renderPage();

    expect(screen.getByText(/查询回测历史失败/)).toBeTruthy();
  });

  test('任务状态未变的重渲染不重取（避免无谓请求）', async () => {
    const { rerender } = await renderPage();
    expect(listFactorBacktestsMock).toHaveBeenCalledTimes(1);

    rerender(<BacktestPage />);
    await act(async () => {});

    expect(listFactorBacktestsMock).toHaveBeenCalledTimes(1);
  });
});

describe('回测历史：对比', () => {
  test('勾选两条出对比表；收益类高亮大值、回撤类高亮小值', async () => {
    await renderPage();

    const boxes = screen.getAllByRole('checkbox');
    expect(boxes.length).toBe(3);
    fireEvent.click(boxes[0]); // r2（新）
    fireEvent.click(boxes[2]); // r3（旧）

    const compare = screen.getByTestId('backtest-compare');
    // 两条运行都进表
    expect(within(compare).getByText('0.0500')).toBeTruthy();
    expect(within(compare).getByText('0.0200')).toBeTruthy();

    // 最优高亮：IC 大者（0.05）、回撤小者（15.00%）在 r2 列
    const bestTexts = Array.from(compare.querySelectorAll('td[data-best="1"]')).map(
      (c) => c.textContent,
    );
    expect(bestTexts).toContain('0.0500');
    expect(bestTexts).toContain('15.00%');
    // 回撤小者才是最优：30.00% 不得被标最优
    const dd30 = within(compare).getByText('30.00%');
    expect(dd30.closest('td')?.getAttribute('data-best')).toBeNull();
  });

  test('最多对比 5 条：第 6 条复选框禁用并有提示', async () => {
    listFactorBacktestsMock.mockResolvedValue({
      success: true,
      data: {
        runs: Array.from({ length: 6 }, (_, i) =>
          run({ runId: `r${i}`, startedAt: `2026-10-0${9 - i}T03:00:00Z` }),
        ),
      },
    });
    await renderPage();

    const boxes = screen.getAllByRole('checkbox') as HTMLInputElement[];
    for (let i = 0; i < 5; i += 1) fireEvent.click(boxes[i]);

    expect(boxes[5].disabled).toBe(true);
    expect(screen.getByText(/最多对比 5 次/)).toBeTruthy();
  });

  test('取消勾选后对比列随之减少', async () => {
    await renderPage();

    const boxes = screen.getAllByRole('checkbox');
    fireEvent.click(boxes[0]);
    fireEvent.click(boxes[2]);
    expect(screen.getByTestId('backtest-compare')).toBeTruthy();

    fireEvent.click(boxes[2]); // 取消 r3
    // 只剩一条 → 对比区收起
    expect(screen.queryByTestId('backtest-compare')).toBeNull();
  });

  test('单值不称「最优」：某指标只有一条运行有值时不判 data-best', async () => {
    listFactorBacktestsMock.mockResolvedValue({
      success: true,
      data: {
        runs: [
          run({
            runId: 'ra',
            startedAt: '2026-10-09T03:00:00Z',
            metrics: { ic: 0.05, rre: 0.8 },
          }),
          run({ runId: 'rb', startedAt: '2026-10-08T03:00:00Z', metrics: { ic: 0.02 } }),
        ],
      },
    });
    await renderPage();

    const boxes = screen.getAllByRole('checkbox');
    fireEvent.click(boxes[0]);
    fireEvent.click(boxes[1]);

    const compare = screen.getByTestId('backtest-compare');
    // IC 两条都有值 → 大者高亮
    const ic = within(compare).getByText('0.0500');
    expect(ic.closest('td')?.getAttribute('data-best')).toBe('1');
    // RRE 只有 ra 一条有值 → 不判最优（没得比就不算赢）
    const rre = within(compare).getByText('0.8000');
    expect(rre.closest('td')?.getAttribute('data-best')).toBeNull();
  });

  test('滑出窗口的勾选随重取收敛：界面不锁死（回归）', async () => {
    listFactorBacktestsMock.mockResolvedValue({
      success: true,
      data: {
        runs: Array.from({ length: 5 }, (_, i) =>
          run({ runId: `r${i}`, startedAt: `2026-10-0${9 - i}T03:00:00Z` }),
        ),
      },
    });
    await renderPage();

    const boxes = screen.getAllByRole('checkbox') as HTMLInputElement[];
    for (let i = 0; i < 5; i += 1) fireEvent.click(boxes[i]);
    expect(screen.getByText(/最多对比 5 次/)).toBeTruthy();

    // 重取后只剩 r0 还在 20 条窗口内，其余旧勾选已滑出
    listFactorBacktestsMock.mockResolvedValue({
      success: true,
      data: {
        runs: [
          run({ runId: 'r0', startedAt: '2026-10-09T03:00:00Z' }),
          run({ runId: 'rnew', startedAt: '2026-10-09T09:00:00Z' }),
        ],
      },
    });
    fireEvent.click(screen.getByTitle('刷新回测历史'));
    await act(async () => {});

    const fresh = screen.getAllByRole('checkbox') as HTMLInputElement[];
    // 收敛为 1 条（r0）：未达上限，rnew 可勾选——旧实现会把复选框全禁用（锁死）
    expect(fresh[0].checked).toBe(true);
    expect(fresh[1].checked).toBe(false);
    expect(fresh.some((b) => b.disabled)).toBe(false);
    expect(screen.queryByText(/最多对比 5 次/)).toBeNull();
    // 不足 2 条 → 对比区收起（旧勾选不残留幽灵列）
    expect(screen.queryByTestId('backtest-compare')).toBeNull();
  });
});
