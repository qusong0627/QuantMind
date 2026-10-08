/**
 * MiningDashboardPage —— 「一键回测」真的发请求（用户原话：回测点了没有用）。
 *
 * 旧实现 FactorStatsRow 的按钮只调 onBacktest → 仅导航，一个 API 都不发。
 * 现在：一键回测把全部可回测因子（有表达式、非只读）交给 RunQueueContext
 * 真入队（并发 2，第 3 个排队），这里断言 POST 真的打到 startBacktest；
 * 没有可回测因子时按钮禁用（tooltip 说明原因），点击不发任何请求。
 */
import React from 'react';
import { describe, test, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, act } from '@testing-library/react';
import { MiningDashboardPage } from '../MiningDashboardPage';
import { RunQueueProvider } from '../../context-v2/RunQueueContext';
import type { Factor, RealtimeMetrics } from '../../types-v2';

const { startBacktestMock, getBacktestStatusMock, cancelBacktestMock, refreshMiningFactorsMock } =
  vi.hoisted(() => ({
    startBacktestMock: vi.fn(),
    getBacktestStatusMock: vi.fn(),
    cancelBacktestMock: vi.fn(),
    refreshMiningFactorsMock: vi.fn(),
  }));

vi.mock('../../../../services/aiStrategyClients', () => ({
  apiClient: { get: vi.fn(), post: vi.fn() },
  backtestClient: { get: vi.fn(), post: vi.fn() },
}));

vi.mock('../../services-v2/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../services-v2/api')>();
  return {
    ...actual,
    startBacktest: startBacktestMock,
    getBacktestStatus: getBacktestStatusMock,
    cancelBacktest: cancelBacktestMock,
  };
});

vi.mock('../../services-v2/materialize', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../services-v2/materialize')>();
  return {
    ...actual,
    startMaterialize: vi.fn(),
    getMaterializeStatus: vi.fn().mockResolvedValue({ running: false, factors: [], lastAt: null }),
  };
});

vi.mock('../../services/alphaAgentService', () => ({
  alphaAgentService: { promoteByExpression: vi.fn() },
}));

let taskFactors: Factor[] = [];
vi.mock('../../context-v2/TaskContext', () => ({
  useTaskContext: () => ({
    miningTask: {
      taskId: 't1',
      status: 'completed',
      config: { userInput: 'x' },
      progress: { phase: 'completed', currentRound: 3, totalRounds: 3, progress: 100, message: '完成', timestamp: '' },
      logs: [],
      createdAt: '',
      updatedAt: '',
      metrics: { totalFactors: taskFactors.length, highQualityFactors: 0, mediumQualityFactors: 0, lowQualityFactors: 0, factors: taskFactors } as RealtimeMetrics,
    },
    miningEquityCurve: [],
    miningDrawdownCurve: [],
    stopMining: vi.fn(),
    refreshMiningFactors: refreshMiningFactorsMock,
    attachBacktestTask: vi.fn().mockResolvedValue(undefined),
  }),
}));

// 图表与侧栏不是本测试的被测对象（echarts 在 jsdom 里没有意义）
vi.mock('../../components-v2/LiveCharts', () => ({ LiveCharts: () => null }));
vi.mock('../../components-v2/ProgressSidebar', () => ({ ProgressSidebar: () => null }));

function mkFactor(over: Partial<Factor> & { factorId: string }): Factor {
  return {
    factorName: over.factorId,
    factorExpression: 'close/mean(close,5)',
    factorDescription: '',
    quality: 'medium',
    round: 0,
    direction: '',
    createdAt: '2026-10-01T00:00:00Z',
    ...over,
  };
}

const renderPage = () =>
  render(
    <RunQueueProvider>
      <MiningDashboardPage onNavigate={vi.fn()} />
    </RunQueueProvider>,
  );

beforeEach(() => {
  startBacktestMock.mockReset();
  getBacktestStatusMock.mockReset();
  cancelBacktestMock.mockReset();
  refreshMiningFactorsMock.mockReset();
  refreshMiningFactorsMock.mockResolvedValue(undefined);
  startBacktestMock.mockResolvedValue({ success: true, data: { taskId: 'x', task: {} } });
  getBacktestStatusMock.mockResolvedValue({
    success: true,
    data: { task: { status: 'running' } },
  });
  localStorage.clear();
});

describe('MiningDashboardPage：一键回测真入队', () => {
  test('点击一键回测：startBacktest 真的被调，并发 2（第 3 个排队）', async () => {
    taskFactors = [
      mkFactor({ factorId: 'a', factorName: 'A', factorExpression: 'close' }),
      mkFactor({ factorId: 'b', factorName: 'B', factorExpression: 'open' }),
      mkFactor({ factorId: 'c', factorName: 'C', factorExpression: 'high' }),
      mkFactor({ factorId: 'old', factorName: 'Legacy', ownerless: true }), // 不可回测
    ];
    renderPage();

    const btn = await screen.findByText('一键回测（3）');
    await act(async () => {
      fireEvent.click(btn);
    });

    // 并发 2：只发出去两个
    expect(startBacktestMock).toHaveBeenCalledTimes(2);
    expect(startBacktestMock.mock.calls.map((c) => c[0].factorId)).toEqual(['a', 'b']);
    // 三个可回测行都进入队列状态（c 排队中）
    expect(screen.getAllByText('回测中').length).toBe(2);
    expect(screen.getByText('排队中')).toBeTruthy();
  });

  test('没有可回测因子：按钮禁用，点击不发请求', async () => {
    taskFactors = [
      mkFactor({ factorId: 'old', factorName: 'Legacy', ownerless: true }),
      mkFactor({ factorId: 'nocode', factorName: 'NoCode', factorExpression: '' }),
    ];
    renderPage();

    const btn = (await screen.findByText('一键回测（0）')).closest('button')!;
    expect(btn.disabled).toBe(true);
    expect(btn.getAttribute('title')).toContain('暂无可回测因子');

    fireEvent.click(btn);
    expect(startBacktestMock).not.toHaveBeenCalled();
  });
});
