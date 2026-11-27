import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { BackgroundTask } from '../types';

const navigateMock = vi.fn();

vi.mock('react-router-dom', () => ({
  useNavigate: () => navigateMock,
}));

const fetchBackgroundTasks = vi.fn();
const fetchServerLoad = vi.fn();
const cancelMiningTask = vi.fn();
const cancelInferenceTask = vi.fn();

vi.mock('../taskSources', () => ({
  fetchBackgroundTasks: (...args: unknown[]) => fetchBackgroundTasks(...args),
  fetchServerLoad: (...args: unknown[]) => fetchServerLoad(...args),
  cancelMiningTask: (...args: unknown[]) => cancelMiningTask(...args),
  cancelInferenceTask: (...args: unknown[]) => cancelInferenceTask(...args),
}));

import { BackgroundTaskDock } from '../BackgroundTaskDock';

const TASKS: BackgroundTask[] = [
  {
    id: 'train_1',
    kind: 'training',
    title: '融合189 · T+10',
    status: 'running',
    progress: 42,
    startedAt: null,
    href: '/admin/models',
  },
  {
    id: 'task_mining',
    kind: 'mining',
    title: '因子演化任务',
    status: 'running',
    progress: null,
    startedAt: null,
    href: '/quantbot',
  },
];

describe('BackgroundTaskDock', () => {
  beforeEach(() => {
    localStorage.clear();
    navigateMock.mockReset();
    cancelMiningTask.mockReset();
    cancelInferenceTask.mockReset();
    fetchBackgroundTasks.mockReset().mockResolvedValue(TASKS);
    fetchServerLoad.mockReset().mockResolvedValue(null);
  });

  it('折叠态按类型汇总进行中任务数', async () => {
    render(<BackgroundTaskDock />);
    await waitFor(() => expect(screen.getByText('2 进行中')).toBeTruthy());
    // 训练/挖掘各一枚小标
    expect(screen.getByTitle('训练')).toBeTruthy();
    expect(screen.getByTitle('挖掘')).toBeTruthy();
  });

  it('点击拖柄展开面板并列出任务，点任务跳转对应页面', async () => {
    const user = userEvent.setup();
    render(<BackgroundTaskDock />);
    await waitFor(() => expect(screen.getByText('2 进行中')).toBeTruthy());

    await user.click(screen.getByRole('button', { name: /后台任务，2 个进行中/ }));
    expect(await screen.findByText('融合189 · T+10')).toBeTruthy();
    // 有确定进度的显示百分比，不确定进度的显示「进行中」
    expect(screen.getByText(/42%/)).toBeTruthy();

    await user.click(screen.getByText('融合189 · T+10'));
    expect(navigateMock).toHaveBeenCalledWith('/admin/models');
  });

  it('空闲时显示「后台任务」且面板给出空态', async () => {
    fetchBackgroundTasks.mockResolvedValue([]);
    const user = userEvent.setup();
    render(<BackgroundTaskDock />);
    await waitFor(() => expect(screen.getByText('后台任务')).toBeTruthy());

    await user.click(screen.getByRole('button', { name: /后台任务，0 个进行中/ }));
    expect(await screen.findByText('当前没有后台任务')).toBeTruthy();
  });

  it('未启用时不发起轮询', async () => {
    render(<BackgroundTaskDock enabled={false} />);
    await new Promise((r) => setTimeout(r, 20));
    expect(fetchBackgroundTasks).not.toHaveBeenCalled();
  });

  it('悬浮条右端 × 关闭整条坞（刷新才恢复）', async () => {
    const user = userEvent.setup();
    render(<BackgroundTaskDock />);
    await waitFor(() => expect(screen.getByText('2 进行中')).toBeTruthy());

    await user.click(screen.getByRole('button', { name: '关闭后台任务坞' }));
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: /后台任务，2 个进行中/ })).toBeNull(),
    );
    expect(cancelMiningTask).not.toHaveBeenCalled();
    expect(cancelInferenceTask).not.toHaveBeenCalled();
  });

  it('训练行没有取消端点：× 只把该行从坞里隐藏', async () => {
    const user = userEvent.setup();
    render(<BackgroundTaskDock />);
    await waitFor(() => expect(screen.getByText('2 进行中')).toBeTruthy());

    await user.click(screen.getByRole('button', { name: /后台任务，2 个进行中/ }));
    expect(await screen.findByText('融合189 · T+10')).toBeTruthy();

    await user.click(screen.getByRole('button', { name: '关闭 融合189 · T+10' }));
    await waitFor(() => expect(screen.queryByText('融合189 · T+10')).toBeNull());
    // 任务本身没被取消（训练没有取消端点）
    expect(cancelMiningTask).not.toHaveBeenCalled();
    expect(cancelInferenceTask).not.toHaveBeenCalled();
    // 另一枚任务仍在
    expect(screen.getByText('因子演化任务')).toBeTruthy();
  });

  it('面板右上角的关闭按钮收起面板（不是取消任务）', async () => {
    const user = userEvent.setup();
    render(<BackgroundTaskDock />);
    await waitFor(() => expect(screen.getByText('2 进行中')).toBeTruthy());

    await user.click(screen.getByRole('button', { name: /后台任务，2 个进行中/ }));
    expect(await screen.findByText('融合189 · T+10')).toBeTruthy();

    await user.click(screen.getByRole('button', { name: '关闭后台任务面板' }));
    await waitFor(() => expect(screen.queryByText('融合189 · T+10')).toBeNull());
    expect(cancelMiningTask).not.toHaveBeenCalled();
    expect(cancelInferenceTask).not.toHaveBeenCalled();
  });
});
