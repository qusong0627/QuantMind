/**
 * 挖掘任务监视器 —— 用户的原话是「因子挖掘时候，刷新界面就没有了」。
 *
 * 所以本文件的第一条用例就是那句话本身：**重新挂载 = 刷新页面**之后，运行中的
 * 任务必须立刻重新出现（状态来自后端 `GET /alpha-agent/tasks`，不是内存）。
 *
 * 另外两条是这个组件最容易被顺手改坏的边：
 * - **轮询不能变成请求风暴**（`SnapshotPanel` 刚踩过：effect 依赖每次都换新的回调，
 *   5 秒轮询退化成自转循环）。这条是常驻壳上的组件，失控会一直打引擎。
 * - **轮询失败不能把已有列表清空**：后端重启的那几秒里，面板应该冻在最后一次
 *   成功的数据上并挂出提示，而不是变成「什么都没有」——那正是用户抱怨的现象。
 */
import React from 'react';
import { describe, test, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';
import MiningTaskMonitor from '../MiningTaskMonitor';
import type { Task } from '../../types-v2';

const { listTasksMock, cancelMiningMock } = vi.hoisted(() => ({
  listTasksMock: vi.fn(),
  cancelMiningMock: vi.fn(),
}));

vi.mock('../../services-v2/api', () => ({
  listTasks: listTasksMock,
  cancelMining: cancelMiningMock,
}));

const ok = <T,>(data: T) => ({ success: true, data });

function mkTask(over: Partial<Task> & { taskId: string; status: Task['status'] }): Task {
  return {
    config: { userInput: '' },
    logs: [],
    createdAt: '2026-10-07T09:38:53Z',
    updatedAt: '2026-10-07T09:44:32Z',
    progress: {
      phase: 'running',
      currentRound: 2,
      totalRounds: 3,
      progress: 47,
      message: 'Loop 2/3 写作代码',
      timestamp: '2026-10-07T09:44:32Z',
    },
    ...over,
  } as Task;
}

const RUNNING = mkTask({ taskId: '46c374ae27e44306', status: 'running' });
const FAILED = mkTask({
  taskId: 'e0586ac078614578',
  status: 'failed',
  createdAt: '2026-10-07T08:55:39Z',
  progress: {
    phase: 'parsing',
    currentRound: 0,
    totalRounds: 3,
    progress: 0,
    message: 'Server restarted while task was running',
    timestamp: '2026-10-07T09:44:32Z',
  },
});
const DONE = mkTask({
  taskId: '3d1f0000aaaa1111',
  status: 'completed',
  createdAt: '2026-10-07T07:10:00Z',
});

/** 只在 fake timers 的小节里用：把挂载那一轮的 `listTasks()` promise 推落地。 */
const settle = async () => {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0);
  });
};

beforeEach(() => {
  listTasksMock.mockReset();
  cancelMiningMock.mockReset();
  listTasksMock.mockResolvedValue(ok({ tasks: [RUNNING] }));
  cancelMiningMock.mockResolvedValue(ok({}));
});

afterEach(() => {
  vi.useRealTimers();
});

describe('MiningTaskMonitor：后台进度在刷新之后仍然存在', () => {
  test('重新挂载（= 刷新页面）后立刻显示运行中的任务与进度', async () => {
    render(<MiningTaskMonitor />);

    expect(await screen.findByText(/因子挖掘 47%/)).toBeTruthy();
    expect(screen.getByText(/Loop 2\/3/)).toBeTruthy();
  });

  test('一个任务都没有时什么都不渲染（不给每页添一块常驻浮层）', async () => {
    listTasksMock.mockResolvedValue(ok({ tasks: [] }));

    const { container } = render(<MiningTaskMonitor />);

    await waitFor(() => expect(listTasksMock).toHaveBeenCalled());
    await waitFor(() => expect(container.firstChild).toBeNull());
  });

  test('每条任务带方向摘要（后端 direction 落在 config.userInput 上）', async () => {
    listTasksMock.mockResolvedValue(
      ok({
        tasks: [
          { ...RUNNING, config: { userInput: '尾盘主力资金净流入 × 换手率背离' } },
          DONE,
        ],
      }),
    );

    render(<MiningTaskMonitor />);
    fireEvent.click(await screen.findByText(/因子挖掘 47%/));

    expect(screen.getByText('尾盘主力资金净流入 × 换手率背离')).toBeTruthy();
  });

  test('展开面板后运行中/失败/完成都在，且失败行带后端给的原因', async () => {
    listTasksMock.mockResolvedValue(ok({ tasks: [RUNNING, FAILED, DONE] }));

    render(<MiningTaskMonitor />);
    fireEvent.click(await screen.findByText(/因子挖掘 47%/));

    expect(screen.getByText('运行中 47% · Loop 2/3')).toBeTruthy();
    expect(screen.getByText('失败')).toBeTruthy();
    expect(screen.getByText('已完成')).toBeTruthy();
    // 原因原样来自后端 error_message，不做改写
    expect(screen.getByText(/Server restarted while task was running/)).toBeTruthy();
    // 短 id 便于对上日志目录名
    expect(screen.getByText('e0586ac0')).toBeTruthy();
  });

  test('点「取消」真的请求取消，并立刻回读一次任务列表', async () => {
    render(<MiningTaskMonitor />);
    fireEvent.click(await screen.findByText(/因子挖掘 47%/));

    fireEvent.click(screen.getByText('取消'));

    await waitFor(() => expect(cancelMiningMock).toHaveBeenCalledWith('46c374ae27e44306'));
    // 首屏 1 次 + 取消后回读 1 次
    await waitFor(() => expect(listTasksMock.mock.calls.length).toBeGreaterThanOrEqual(2));
  });
});

describe('MiningTaskMonitor：未登录不轮询（壳在公开路由/登录页也会挂载）', () => {
  test('enabled=false 时不发请求、不渲染；登录后（enabled→true）立刻开始拉取', async () => {
    vi.useFakeTimers();
    const { container, rerender } = render(<MiningTaskMonitor enabled={false} />);
    await settle();

    // 静置 10 秒（跨过两个轮询周期）：未登录一个请求都不许发
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10000);
    });
    expect(listTasksMock).not.toHaveBeenCalled();
    expect(container.firstChild).toBeNull();

    // 登录态出现：立刻拉取并渲染，不等下一个 5 秒节拍
    rerender(<MiningTaskMonitor enabled={true} />);
    await settle();
    expect(listTasksMock).toHaveBeenCalledTimes(1);
    expect(screen.getByText(/因子挖掘 47%/)).toBeTruthy();
  });
});

describe('MiningTaskMonitor：轮询节流、失败降级与收起', () => {
  test('5 秒轮询一次，不是每次渲染都发请求', async () => {
    vi.useFakeTimers();
    render(<MiningTaskMonitor />);
    await settle();
    const afterMount = listTasksMock.mock.calls.length;
    expect(afterMount).toBe(1);

    // 静置 4 秒：不该有新请求
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4000);
    });
    expect(listTasksMock.mock.calls.length).toBe(afterMount);

    // 跨过 5 秒：恰好 +1
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1500);
    });
    expect(listTasksMock.mock.calls.length).toBe(afterMount + 1);
  });

  test('轮询失败时冻结在最后一次成功的数据上，并说明正在显示旧数据', async () => {
    vi.useFakeTimers();
    render(<MiningTaskMonitor />);
    await settle();
    expect(screen.getByText(/因子挖掘 47%/)).toBeTruthy();

    listTasksMock.mockRejectedValue(new Error('Failed to fetch'));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });

    // 列表没被清空
    expect(screen.getByText(/因子挖掘 47%/)).toBeTruthy();
    // 展开能看到降级提示
    fireEvent.click(screen.getByText(/因子挖掘 47%/));
    expect(screen.getByText(/任务列表刷新失败/)).toBeTruthy();
  });

  test('收起后消失；任务状态一变就重新出现（失败不该被静默吞掉）', async () => {
    vi.useFakeTimers();
    render(<MiningTaskMonitor />);
    await settle();

    fireEvent.click(screen.getByLabelText('隐藏任务监视器'));
    expect(screen.queryByText(/因子挖掘 47%/)).toBeNull();

    // 任务 running → failed：指纹变了，应该重新冒头
    listTasksMock.mockResolvedValue(ok({ tasks: [{ ...RUNNING, status: 'failed' }] }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });

    expect(screen.getByText('无运行中的挖掘任务')).toBeTruthy();
  });
});
