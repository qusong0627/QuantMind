/**
 * RunQueueContext —— 回测队列与物化运行的纪律测试。
 *
 * 物化完成判据照抄 admin RdMinedMaterializePanel（这条链踩过坑，别改坏）：
 * - `running` 只由**服务端响应**点亮；start() 自己造 true 会把「运行→结束」的
 *   边提前吃完，整轮运行从界面上消失；
 * - 完成 = 先见服务端 true、再见 false 的那条沿；POST 成功后 30s 启动宽限，
 *   宽限期内看到 false 不算结束（子进程冷启才拿锁）；
 * - 409（锁忙）的 FastAPI detail 必须上屏，并同步一次服务端状态
 *   （可能是管理员在跑，界面要如实点亮）。
 *
 * 回测队列：并发 2（第 3 个排队）；重复入队去重；取消排队项即刻生效、
 * 取消在途项补一枪真取消端点。
 */
import React from 'react';
import { describe, test, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, act } from '@testing-library/react';
import { RunQueueProvider, useBacktestQueue, useMaterializeRun } from '../RunQueueContext';
import type { MaterializeStartResult } from '../../services-v2/materialize';

const {
  startBacktestMock,
  getBacktestStatusMock,
  cancelBacktestMock,
  startMaterializeMock,
  getMaterializeStatusMock,
} = vi.hoisted(() => ({
  startBacktestMock: vi.fn(),
  getBacktestStatusMock: vi.fn(),
  cancelBacktestMock: vi.fn(),
  startMaterializeMock: vi.fn(),
  getMaterializeStatusMock: vi.fn(),
}));

// aiStrategyClients 是 axios 实例的宿主，测试里一律不加载真实实现
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
    ...actual, // extractApiDetail 保持真实——409/400 原文提取正是被测行为
    startMaterialize: startMaterializeMock,
    getMaterializeStatus: getMaterializeStatusMock,
  };
});

type BtHandle = ReturnType<typeof useBacktestQueue>;
type MatHandle = ReturnType<typeof useMaterializeRun>;

const handle: { bt: BtHandle; mat: MatHandle } = { bt: null as any, mat: null as any };

const Probe: React.FC = () => {
  const bt = useBacktestQueue();
  const mat = useMaterializeRun();
  handle.bt = bt;
  handle.mat = mat;
  return (
    <div>
      <span data-testid="bt-active">{bt.activeCount}</span>
      <span data-testid="bt-statuses">
        {JSON.stringify(
          Object.fromEntries(Object.entries(bt.entries).map(([id, e]) => [id, e.status])),
        )}
      </span>
      <span data-testid="bt-error-a">{bt.entries['a']?.error ?? ''}</span>
      <span data-testid="mat-running">{mat.running ? 'yes' : 'no'}</span>
      <span data-testid="mat-ids">{[...(mat.runningIds as Set<string>)].sort().join(',')}</span>
      <span data-testid="mat-warning">{mat.warning ?? ''}</span>
    </div>
  );
};

const matOk = (over: Partial<MaterializeStartResult> = {}): MaterializeStartResult => ({
  started: true,
  running: true,
  confirmed: true,
  requested: 1,
  materializable: ['f1'],
  skipped: {},
  rejected: [],
  message: 'ok',
  ...over,
});

const renderQueue = () => render(<RunQueueProvider><Probe /></RunQueueProvider>);

// 注意：getMaterializeStatus 返回的是**已拆信封**的 MaterializeStatusResult
// （不是 {success,data}——信封在 services-v2/materialize.ts 内部就拆掉了）
const statusOk = (running: boolean) => ({ running, factors: [], lastAt: null });

const flush = async () => {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0);
  });
};

beforeEach(() => {
  vi.useFakeTimers();
  startBacktestMock.mockReset();
  getBacktestStatusMock.mockReset();
  cancelBacktestMock.mockReset();
  startMaterializeMock.mockReset();
  getMaterializeStatusMock.mockReset();
});

afterEach(() => {
  vi.useRealTimers();
});

describe('物化运行：完成判据（服务端 true→false 的那条沿）', () => {
  test('启动宽限内的 false 不算结束；true→false 恰好触发一次 onCompleted', async () => {
    startMaterializeMock.mockResolvedValue(matOk());
    getMaterializeStatusMock
      .mockResolvedValueOnce(statusOk(false)) // POST 后首次探测：子进程还没拿锁
      .mockResolvedValueOnce(statusOk(true)) // 服务端确认运行
      .mockResolvedValueOnce(statusOk(false)); // 结束沿

    const onCompleted = vi.fn();
    renderQueue();

    await act(async () => {
      await handle.mat.start(['f1'], { onCompleted });
      await vi.advanceTimersByTimeAsync(0);
    });

    // 首次探测 false：宽限期内不得触发完成
    expect(onCompleted).not.toHaveBeenCalled();
    expect(screen.getByTestId('mat-running').textContent).toBe('no');

    // 5s 后：服务端 true → 点亮运行中，仍不得触发完成
    await flush();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(screen.getByTestId('mat-running').textContent).toBe('yes');
    expect(screen.getByTestId('mat-ids').textContent).toBe('f1');
    expect(onCompleted).not.toHaveBeenCalled();

    // 再 5s：服务端 false → 完成沿，恰好一次，runningIds 清空
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(onCompleted).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId('mat-running').textContent).toBe('no');
    expect(screen.getByTestId('mat-ids').textContent).toBe('');

    // 结束后轮询停止：不再打状态端点，也不会二次触发
    const callsAtFinish = getMaterializeStatusMock.mock.calls.length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30000);
    });
    expect(getMaterializeStatusMock.mock.calls.length).toBe(callsAtFinish);
    expect(onCompleted).toHaveBeenCalledTimes(1);
  });

  test('409 锁忙：detail 原文上屏，并同步服务端状态如实点亮「运行中」', async () => {
    const detail = '已有物化进程在运行（锁被占用），请稍后再试';
    startMaterializeMock.mockRejectedValue({ response: { status: 409, data: { detail } } });
    getMaterializeStatusMock.mockResolvedValue(statusOk(true)); // 管理员在跑

    renderQueue();

    await act(async () => {
      await handle.mat.start(['f1', 'f2']).catch(() => {});
      await vi.advanceTimersByTimeAsync(0);
    });

    expect(screen.getByTestId('mat-warning').textContent).toBe(detail);
    expect(getMaterializeStatusMock).toHaveBeenCalledWith(['f1', 'f2']);
    expect(screen.getByTestId('mat-running').textContent).toBe('yes');
  });

  test('400 非法 id：detail 原文上屏，不谎报运行中', async () => {
    startMaterializeMock.mockRejectedValue({
      response: { status: 400, data: { detail: '非法因子 ID：--force' } },
    });

    renderQueue();
    await act(async () => {
      await handle.mat.start(['--force']).catch(() => {});
      await vi.advanceTimersByTimeAsync(0);
    });

    expect(screen.getByTestId('mat-warning').textContent).toContain('非法因子 ID');
    expect(screen.getByTestId('mat-running').textContent).toBe('no');
  });
});

describe('回测队列：并发 2、去重、取消', () => {
  const btOk = { success: true as const, data: { taskId: 'x', task: {} as any } };

  test('3 个因子并发 2：前两个真发 POST，第三个行终结后补位', async () => {
    startBacktestMock.mockResolvedValue(btOk);
    getBacktestStatusMock.mockImplementation((id: string) =>
      Promise.resolve({
        success: true as const,
        data: { task: { status: id === 'a' ? 'completed' : 'running' } },
      }),
    );

    renderQueue();
    await act(async () => {
      handle.bt.enqueue(['a', 'b', 'c']);
      await vi.advanceTimersByTimeAsync(0);
    });

    expect(startBacktestMock).toHaveBeenCalledTimes(2);
    expect(JSON.parse(screen.getByTestId('bt-statuses').textContent!)).toEqual({
      a: 'running',
      b: 'running',
      c: 'queued',
    });
    expect(screen.getByTestId('bt-active').textContent).toBe('3');

    // a 终结 → 第三个补位
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2500);
    });
    expect(startBacktestMock).toHaveBeenCalledTimes(3);
    expect(JSON.parse(screen.getByTestId('bt-statuses').textContent!).a).toBe('completed');
  });

  test('行级失败显示 FastAPI detail 原文（不能只吞）', async () => {
    startBacktestMock.mockResolvedValue(btOk);
    getBacktestStatusMock.mockResolvedValue({
      success: true,
      data: { task: { status: 'failed' }, error: '因子不存在或无权访问' },
    });

    renderQueue();
    await act(async () => {
      handle.bt.enqueue(['a']);
      await vi.advanceTimersByTimeAsync(0);
      await vi.advanceTimersByTimeAsync(2500);
    });

    expect(JSON.parse(screen.getByTestId('bt-statuses').textContent!).a).toBe('failed');
    expect(screen.getByTestId('bt-error-a').textContent).toBe('因子不存在或无权访问');
  });

  test('重复入队去重：同一因子在途时不重复 POST', async () => {
    startBacktestMock.mockResolvedValue(btOk);

    renderQueue();
    await act(async () => {
      handle.bt.enqueue(['a']);
      handle.bt.enqueue(['a']);
      await vi.advanceTimersByTimeAsync(0);
    });

    expect(startBacktestMock).toHaveBeenCalledTimes(1);
  });

  test('取消在途项：本地立即 cancelled，并补一枪真取消端点', async () => {
    startBacktestMock.mockResolvedValue(btOk);
    cancelBacktestMock.mockResolvedValue({ success: true, data: {} });

    renderQueue();
    await act(async () => {
      handle.bt.enqueue(['a']);
      await vi.advanceTimersByTimeAsync(0);
    });

    await act(async () => {
      await handle.bt.cancel('a');
      await vi.advanceTimersByTimeAsync(0);
    });

    // 本地已结算——补发的取消请求可能先于后端登记到达，属预期
    expect(cancelBacktestMock).toHaveBeenCalledWith('a');
    expect(screen.getByTestId('bt-active').textContent).toBe('0');
  });

  test('取消排队项：出队即取消，不发取消端点（后端从未启动）', async () => {
    startBacktestMock.mockResolvedValue(btOk);
    getBacktestStatusMock.mockResolvedValue({
      success: true,
      data: { task: { status: 'running' } },
    });

    renderQueue();
    await act(async () => {
      handle.bt.enqueue(['a', 'b', 'c']);
      await vi.advanceTimersByTimeAsync(0);
    });

    await act(async () => {
      await handle.bt.cancel('c');
      await vi.advanceTimersByTimeAsync(0);
    });

    expect(cancelBacktestMock).not.toHaveBeenCalled();
    expect(JSON.parse(screen.getByTestId('bt-statuses').textContent!).c).toBe('cancelled');
  });
});
