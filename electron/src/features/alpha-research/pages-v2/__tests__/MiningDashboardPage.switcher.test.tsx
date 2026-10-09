/**
 * MiningDashboardPage —— 多任务切换面。
 *
 * 钉死的边：
 * - 多任务时顶部出任务胶囊（role=tab，aria-selected 跟随 focusedTaskId），
 *   点哪条就把演化台聚焦到哪条（运行中/已完成都可回看）；
 * - 「停止任务」只停聚焦的那条（任务 id 显式传给 stopMining，不靠缺省）；
 * - 单任务不渲染切换器（保持原版观感）。
 */
import React from 'react';
import { describe, test, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, within } from '@testing-library/react';
import { MiningDashboardPage } from '../MiningDashboardPage';
import { RunQueueProvider } from '../../context-v2/RunQueueContext';
import type { RealtimeMetrics, Task } from '../../types-v2';

const { focusMiningTaskMock, stopMiningMock, refreshMiningFactorsMock } = vi.hoisted(() => ({
  focusMiningTaskMock: vi.fn(),
  stopMiningMock: vi.fn().mockResolvedValue(undefined),
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
    startBacktest: vi.fn(),
    getBacktestStatus: vi.fn().mockResolvedValue({ success: true, data: { task: null } }),
    cancelBacktest: vi.fn(),
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

let tasks: Task[] = [];
let focused = 't1';
const taskById = () => tasks.find((t) => t.taskId === focused) ?? null;

vi.mock('../../context-v2/TaskContext', () => ({
  useTaskContext: () => ({
    miningTask: taskById(),
    miningTasks: tasks,
    focusedTaskId: focused,
    focusMiningTask: focusMiningTaskMock,
    miningEquityCurve: [],
    miningDrawdownCurve: [],
    stopMining: stopMiningMock,
    refreshMiningFactors: refreshMiningFactorsMock,
    attachBacktestTask: vi.fn().mockResolvedValue(undefined),
  }),
}));

// 图表与侧栏不是本测试的被测对象（echarts 在 jsdom 里没有意义）
vi.mock('../../components-v2/LiveCharts', () => ({ LiveCharts: () => null }));
vi.mock('../../components-v2/ProgressSidebar', () => ({ ProgressSidebar: () => null }));

const mkTask = (
  taskId: string,
  userInput: string,
  status: 'running' | 'completed' | 'failed' = 'running',
  progress = 40,
): Task =>
  ({
    taskId,
    status,
    config: { userInput },
    progress: {
      phase: status === 'completed' ? 'completed' : 'evolving',
      currentRound: 1,
      totalRounds: 3,
      progress,
      message: `${userInput} 进度`,
      timestamp: '2026-10-09T00:00:00Z',
    },
    logs: [],
    createdAt: '2026-10-09T00:00:00Z',
    updatedAt: '2026-10-09T00:00:00Z',
    metrics: {
      totalFactors: 0,
      highQualityFactors: 0,
      mediumQualityFactors: 0,
      lowQualityFactors: 0,
      factors: [],
    } as RealtimeMetrics,
  }) as Task;

const renderPage = () =>
  render(
    <RunQueueProvider>
      <MiningDashboardPage onNavigate={vi.fn()} />
    </RunQueueProvider>,
  );

beforeEach(() => {
  focusMiningTaskMock.mockReset();
  stopMiningMock.mockReset();
  stopMiningMock.mockResolvedValue(undefined);
  refreshMiningFactorsMock.mockReset();
  refreshMiningFactorsMock.mockResolvedValue(undefined);
  tasks = [];
  focused = 't1';
});

describe('MiningDashboardPage：多任务切换', () => {
  test('两条任务：胶囊渲染、活动态跟随焦点、点击切换、运行中带百分比', async () => {
    tasks = [mkTask('t1', '动量反转', 'running', 40), mkTask('t2', '量价背离', 'completed', 100)];
    renderPage();

    const tabs = screen.getAllByRole('tab');
    expect(tabs.length).toBe(2);
    expect(tabs[0].getAttribute('aria-selected')).toBe('true'); // t1 是焦点
    expect(tabs[1].getAttribute('aria-selected')).toBe('false');
    // 运行中任务的进度（状态栏也会显示同一数字，故限定在胶囊内找）
    expect(within(tabs[0]).getByText('40%')).toBeTruthy();

    fireEvent.click(tabs[1]);
    expect(focusMiningTaskMock).toHaveBeenCalledWith('t2');
  });

  test('「停止任务」只停聚焦的那条（显式传 id）', async () => {
    tasks = [mkTask('t1', '动量反转', 'running'), mkTask('t2', '量价背离', 'running')];
    renderPage();

    fireEvent.click(screen.getByText('停止任务'));
    expect(stopMiningMock).toHaveBeenCalledWith('t1');
  });

  test('单任务不渲染切换器', async () => {
    tasks = [mkTask('t1', '动量反转', 'running')];
    renderPage();

    expect(screen.queryByRole('tab')).toBeNull();
    expect(screen.getByText('停止任务')).toBeTruthy();
  });
});
