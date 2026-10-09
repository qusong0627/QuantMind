/**
 * HomePage —— 多任务面（用户原话：「多任务进行挖掘？相互独立」）。
 *
 * 钉死的边：
 * - 每个运行中任务一行，逐行可停：「停止」只把该任务 id 交给 stopMining，互不连带；
 * - 「查看演化台」先聚焦该任务再跳转；
 * - 多任务运行时输入框保持可提交（isSubmitting 只跟提交在途走，运行数只进提示文案）；
 * - 提交失败（429/建缓存失败）原文上屏，可「知道了」关闭；不产生假任务行。
 */
import React from 'react';
import { describe, test, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, act } from '@testing-library/react';
import { HomePage } from '../HomePage';
import type { Task } from '../../types-v2';

const {
  getDataSummaryMock,
  stopMiningMock,
  startMiningMock,
  focusMiningTaskMock,
  dismissMiningStartErrorMock,
} = vi.hoisted(() => ({
  getDataSummaryMock: vi.fn(),
  stopMiningMock: vi.fn(),
  startMiningMock: vi.fn(),
  focusMiningTaskMock: vi.fn(),
  dismissMiningStartErrorMock: vi.fn(),
}));

vi.mock('../../services-v2/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../services-v2/api')>();
  return { ...actual, getDataSummary: getDataSummaryMock };
});

// 输入框与文档面板不是本测试的被测对象：捕获 HomePage 沉下去的 props
let chatProps: any = null;
vi.mock('../../components-v2/ChatInput', () => ({
  ChatInput: (props: any) => {
    chatProps = props;
    return <div data-testid="chat-input" />;
  },
}));
vi.mock('../../components-v2/DocMiningPanel', () => ({ DocMiningPanel: () => null }));

const ctxState: { tasks: Task[]; starting: boolean; error: string | null } = {
  tasks: [],
  starting: false,
  error: null,
};
vi.mock('../../context-v2/TaskContext', () => ({
  useTaskContext: () => ({
    backendAvailable: true,
    miningTasks: ctxState.tasks,
    miningStarting: ctxState.starting,
    miningStartError: ctxState.error,
    dismissMiningStartError: dismissMiningStartErrorMock,
    startMining: startMiningMock,
    stopMining: stopMiningMock,
    focusMiningTask: focusMiningTaskMock,
  }),
}));

const mkTask = (
  taskId: string,
  userInput: string,
  status: 'running' | 'completed' = 'running',
  progress = 40,
): Task =>
  ({
    taskId,
    status,
    config: { userInput },
    progress: {
      phase: 'evolving',
      currentRound: 1,
      totalRounds: 3,
      progress,
      message: `${userInput} 进度`,
      timestamp: '2026-10-09T00:00:00Z',
    },
    logs: [],
    createdAt: '2026-10-09T00:00:00Z',
    updatedAt: '2026-10-09T00:00:00Z',
  }) as Task;

async function renderPage(onNavigate = vi.fn()) {
  render(<HomePage onNavigate={onNavigate} />);
  await act(async () => {}); // getDataSummary 落地
  return onNavigate;
}

beforeEach(() => {
  getDataSummaryMock.mockReset();
  stopMiningMock.mockReset();
  startMiningMock.mockReset();
  focusMiningTaskMock.mockReset();
  dismissMiningStartErrorMock.mockReset();
  getDataSummaryMock.mockResolvedValue({ success: true, data: null });
  ctxState.tasks = [];
  ctxState.starting = false;
  ctxState.error = null;
  chatProps = null;
  localStorage.clear();
});

describe('HomePage：多任务行', () => {
  test('每个运行中任务一行，逐行停止互不影响', async () => {
    ctxState.tasks = [mkTask('t1', '动量反转'), mkTask('t2', '量价背离')];
    await renderPage();

    const stops = screen.getAllByText('停止');
    expect(stops.length).toBe(2);
    // 行上有方向原文可辨
    expect(screen.getByText('动量反转')).toBeTruthy();
    expect(screen.getByText('量价背离')).toBeTruthy();

    fireEvent.click(stops[0]);
    expect(stopMiningMock).toHaveBeenCalledWith('t1');
    fireEvent.click(stops[1]);
    expect(stopMiningMock).toHaveBeenLastCalledWith('t2');
  });

  test('「查看演化台」先聚焦该任务再跳转', async () => {
    ctxState.tasks = [mkTask('t1', '动量反转'), mkTask('t2', '量价背离')];
    const nav = await renderPage();

    fireEvent.click(screen.getAllByText('查看演化台')[0]);
    expect(focusMiningTaskMock).toHaveBeenCalledWith('t1');
    expect(nav).toHaveBeenCalledWith('mining_dashboard');
  });

  test('已完成任务不占运行行；输入框在多任务运行时保持可提交', async () => {
    ctxState.tasks = [
      mkTask('t1', '动量反转'),
      mkTask('t2', '量价背离'),
      mkTask('t0', '旧任务', 'completed', 100),
    ];
    await renderPage();

    expect(screen.getAllByText('停止').length).toBe(2); // completed 不在运行行
    expect(chatProps.isSubmitting).toBe(false); // 运行中不锁提交
    expect(chatProps.runningCount).toBe(2); // 运行数只进提示
  });

  test('提交在途：提交中行出现、输入框 isSubmitting=true', async () => {
    ctxState.starting = true;
    await renderPage();

    expect(screen.getByText('任务提交中...')).toBeTruthy();
    expect(chatProps.isSubmitting).toBe(true);
  });

  test('提交失败原文上屏，可「知道了」关闭', async () => {
    ctxState.error = '启动失败: 您已有 2 个挖掘任务在运行（上限 2），请等待完成或先取消任务。';
    await renderPage();

    expect(screen.getByText(/上限 2/)).toBeTruthy();
    fireEvent.click(screen.getByText('知道了'));
    expect(dismissMiningStartErrorMock).toHaveBeenCalledTimes(1);
  });
});
