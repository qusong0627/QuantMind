/**
 * TaskContext —— 数据层契约（全量清单口径 + 多任务注册表）。
 *
 * 钉死的边：
 * - **权威全量清单**：`refreshMiningFactors` 走 `GET /factors?task_id=…&limit=500`，
 *   把 /tasks 载荷 20 条上限之外的全部因子合并进任务（旧实现前端还 slice(0,10)，
 *   双层截断）；
 * - **按 factorId 去重**：后到覆盖（同一因子的物化/回测状态会更新），不产生重复行；
 * - **缺失不补 0**：没算过 IC 的因子 ic 保持 undefined；头条指标 = 全清单 RankIC
 *   最优（绝不把 undefined 显成 0.0000）；
 * - **日志行不再是假因子**：旧实现把 "Added new factor:" 正则解析成 generateId()
 *   造的假行（无法回测/物化），现在日志只进 logs；
 * - **任务完成沿自动拉一次全量清单**；
 * - **多任务相互独立**：运行中任务可多条并存（后端上限 2/人，429 兜底），
 *   WS 消息按 taskId 路由、停止只断指定任务的传输、提交失败不产生假任务行。
 */
import React from 'react';
import { describe, test, expect, vi, beforeEach } from 'vitest';
import { render, screen, act } from '@testing-library/react';
import { TaskProvider, useTaskContext } from '../TaskContext';
import type { Task } from '../../types-v2';

const {
  startMiningMock,
  listTasksMock,
  healthCheckMock,
  getFactorsMock,
  connectMiningWsMock,
  cancelMiningMock,
  getMiningStatusMock,
} = vi.hoisted(() => ({
  startMiningMock: vi.fn(),
  listTasksMock: vi.fn(),
  healthCheckMock: vi.fn(),
  getFactorsMock: vi.fn(),
  connectMiningWsMock: vi.fn(),
  cancelMiningMock: vi.fn(),
  getMiningStatusMock: vi.fn(),
}));

// axios 宿主模块：测试一律不加载真实实现（authService 等一串依赖）
vi.mock('../../../../services/aiStrategyClients', () => ({
  apiClient: { get: vi.fn(), post: vi.fn() },
  backtestClient: { get: vi.fn(), post: vi.fn() },
}));

vi.mock('../../services-v2/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../services-v2/api')>();
  return {
    ...actual, // normalizeAgentFactor / emptyMetrics / FACTOR_LIST_MAX_LIMIT 保持真实
    healthCheck: healthCheckMock,
    listTasks: listTasksMock,
    startMining: startMiningMock,
    getFactors: getFactorsMock,
    connectMiningWs: connectMiningWsMock,
    cancelMining: cancelMiningMock,
    getMiningStatus: getMiningStatusMock,
  };
});

/** 按 taskId 捕获各任务独立的 WS 消息处理器与 close（多任务传输互不覆盖） */
const wsHandlers = new Map<string, (msg: any) => void>();
const wsClosed = new Map<string, ReturnType<typeof vi.fn>>();

type Ctx = ReturnType<typeof useTaskContext>;
const handle: { ctx: Ctx } = { ctx: null as any };

const Probe: React.FC = () => {
  const ctx = useTaskContext();
  handle.ctx = ctx;
  const t = ctx.miningTask;
  const factors = t?.metrics?.factors ?? [];
  const byId = (id: string) => factors.find((f) => f.factorId === id);
  return (
    <div>
      <span data-testid="count">{factors.length}</span>
      <span data-testid="rank-ic">{String(t?.metrics?.rankIc ?? 'undef')}</span>
      <span data-testid="factor-name">{t?.metrics?.factorName ?? 'undef'}</span>
      <span data-testid="f0-ic">{String(byId('f0')?.ic ?? 'undef')}</span>
      <span data-testid="f5-rankic">{String(byId('f5')?.rankIc ?? 'undef')}</span>
      <span data-testid="quality">
        {[t?.metrics?.highQualityFactors, t?.metrics?.mediumQualityFactors, t?.metrics?.lowQualityFactors].join('/')}
      </span>
      <span data-testid="logs">{t?.logs?.length ?? -1}</span>
      <span data-testid="status">{t?.status ?? 'none'}</span>
      {/* 多任务视图：注册表全量（顺序=启动先后）与焦点 */}
      <span data-testid="tasks">
        {ctx.miningTasks.map((x) => `${x.taskId}:${x.status}:${x.progress.progress}`).join(',')}
      </span>
      <span data-testid="focused">{ctx.focusedTaskId ?? 'none'}</span>
      <span data-testid="start-error">{ctx.miningStartError ?? 'none'}</span>
    </div>
  );
};

const flush = () => act(async () => {});

beforeEach(() => {
  startMiningMock.mockReset();
  listTasksMock.mockReset();
  healthCheckMock.mockReset();
  getFactorsMock.mockReset();
  connectMiningWsMock.mockReset();
  cancelMiningMock.mockReset();
  getMiningStatusMock.mockReset();
  wsHandlers.clear();
  wsClosed.clear();

  healthCheckMock.mockResolvedValue(true);
  listTasksMock.mockResolvedValue({ success: true, data: { tasks: [] } });
  cancelMiningMock.mockResolvedValue({ success: true, data: {} });
  getMiningStatusMock.mockResolvedValue({ success: true, data: { task: null } });
  connectMiningWsMock.mockImplementation((taskId: string, onMessage: (msg: any) => void) => {
    wsHandlers.set(taskId, onMessage);
    const close = vi.fn();
    wsClosed.set(taskId, close);
    return { close, _pollingTimeoutId: undefined };
  });
});

const mkTask = (taskId = 't1', over: Partial<Task> = {}): Task =>
  ({
    taskId,
    status: 'running',
    config: { userInput: `dir-${taskId}` },
    progress: {
      phase: 'evolving',
      currentRound: 1,
      totalRounds: 3,
      progress: 40,
      message: 'Loop 1/3',
      timestamp: '2026-10-08T00:00:00Z',
    },
    logs: [],
    createdAt: '2026-10-08T00:00:00Z',
    updatedAt: '2026-10-08T00:00:00Z',
    ...over,
  }) as Task;

/** 24 个唯一因子 + f5 重复一次（后到覆盖）；f0 有 ic、其余 IC 无关字段留空 */
function rawFactors(): any[] {
  const rows = Array.from({ length: 24 }, (_, i) => ({
    id: `f${i}`,
    factor_name: `Alpha_${i}`,
    factor_formulation: 'close/mean(close,5)',
    ic_value: i === 0 ? 0.0321 : undefined,
    metadata: { rank_ic: i === 3 ? 0.0812 : 0.01 + i * 0.001, market: 'a_share' },
    user_id: 1,
    created_at: `2026-10-07T0${i % 10}:00:00Z`,
  }));
  // 重复行：f5 后到、rank_ic 更新（去重后覆盖）
  rows.push({
    id: 'f5',
    factor_name: 'Alpha_5',
    factor_formulation: 'close/mean(close,5)',
    ic_value: 0.06,
    metadata: { rank_ic: 0.0555, market: 'a_share' },
    user_id: 1,
    created_at: '2026-10-07T09:00:00Z',
  });
  return rows;
}

const factorsPayload = () => ({
  success: true,
  data: { factors: rawFactors(), total: 25, limit: 500, offset: 0, serverLimit: 500 },
});

async function mountAndStart(): Promise<void> {
  render(
    <TaskProvider>
      <Probe />
    </TaskProvider>,
  );
  await flush(); // healthCheck / listTasks 落地
  await startTask('t1');
  expect(screen.getByTestId('status').textContent).toBe('running');
  expect(wsHandlers.has('t1')).toBe(true);
}

/** 起一个任务：mock 返回 mkTask(id) 并等待 POST 落地 */
async function startTask(id: string, over: Partial<Task> = {}): Promise<void> {
  startMiningMock.mockResolvedValue({ success: true, data: { taskId: id, task: mkTask(id, over) } });
  await act(async () => {
    handle.ctx.startMining({ userInput: `dir-${id}` } as any);
  });
}

/** 把消息派发给指定任务的传输（模拟该任务的 WS 轮询回调） */
async function sendWs(taskId: string, msg: any): Promise<void> {
  await act(async () => {
    wsHandlers.get(taskId)!(msg);
  });
}

describe('TaskContext：全量因子清单（挖到多少显示多少）', () => {
  test('refreshMiningFactors 带 taskId + limit=500，25 行去重合并成 24 个因子', async () => {
    getFactorsMock.mockResolvedValue(factorsPayload());
    await mountAndStart();

    await act(async () => {
      await handle.ctx.refreshMiningFactors('t1');
    });

    // 权威请求：taskId 收口 + 500 上限（>20 条不会再被截）
    expect(getFactorsMock).toHaveBeenCalledWith({ taskId: 't1', limit: 500 });
    // 去重：25 行 → 24 个唯一因子
    expect(screen.getByTestId('count').textContent).toBe('24');
    // 后到覆盖：f5 第二次的 rank_ic 生效
    expect(screen.getByTestId('f5-rankic').textContent).toBe('0.0555');
  });

  test('缺失不补 0：ic 缺席显示 undef；头条指标=全清单 RankIC 最优', async () => {
    getFactorsMock.mockResolvedValue(factorsPayload());
    await mountAndStart();

    await act(async () => {
      await handle.ctx.refreshMiningFactors('t1');
    });

    expect(screen.getByTestId('f0-ic').textContent).toBe('0.0321');
    // f1..f23（除 dup 覆盖的 f5）的 ic_value 都是 undefined → 不许变 0
    expect(screen.getByTestId('count').textContent).toBe('24');
    // 头条 = 全清单 rank_ic 最优的 f3（0.0812），不是 0
    expect(screen.getByTestId('rank-ic').textContent).toBe('0.0812');
    expect(screen.getByTestId('factor-name').textContent).toBe('Alpha_3');
    // 质量计数按 ic_value 现算：f0 0.0321→medium，f5 覆盖后 0.06→high，其余 unknown
    expect(screen.getByTestId('quality').textContent).toBe('1/1/0');
  });

  test('不带参数的 refreshMiningFactors 用当前聚焦任务 id', async () => {
    getFactorsMock.mockResolvedValue({
      success: true,
      data: { factors: [], total: 0, limit: 500, offset: 0, serverLimit: 500 },
    });
    await mountAndStart();

    await act(async () => {
      await handle.ctx.refreshMiningFactors();
    });

    expect(getFactorsMock).toHaveBeenCalledWith({ taskId: 't1', limit: 500 });
  });
});

describe('TaskContext：日志行不再是假因子', () => {
  test('WS 日志 "Added new factor:" 只进 logs，不产生 factor 行', async () => {
    getFactorsMock.mockResolvedValue(factorsPayload());
    await mountAndStart();
    await act(async () => {
      await handle.ctx.refreshMiningFactors('t1');
    });
    expect(screen.getByTestId('count').textContent).toBe('24');

    await sendWs('t1', {
      type: 'log',
      data: {
        id: 'l1',
        timestamp: '2026-10-08T00:01:00Z',
        level: 'info',
        message: 'Added new factor: momentum_reversal_5d',
      },
    });

    expect(screen.getByTestId('count').textContent).toBe('24'); // 没有第 25 个假行
    expect(screen.getByTestId('logs').textContent).toBe('1');
  });
});

describe('TaskContext：任务完成沿自动拉全量清单', () => {
  test('WS result 置 completed 后自动再拉一次 /factors?task_id', async () => {
    getFactorsMock.mockResolvedValue(factorsPayload());
    await mountAndStart();
    const callsBefore = getFactorsMock.mock.calls.length;

    await sendWs('t1', { type: 'result', data: { status: 'completed' } });
    await flush();

    expect(screen.getByTestId('status').textContent).toBe('completed');
    expect(getFactorsMock.mock.calls.length).toBe(callsBefore + 1);
    expect(getFactorsMock).toHaveBeenLastCalledWith({ taskId: 't1', limit: 500 });
  });
});

describe('TaskContext：多任务相互独立', () => {
  test('已有任务运行中仍可提交第二个；注册表两条、焦点切到新任务', async () => {
    await mountAndStart(); // t1
    await startTask('t2');

    expect(startMiningMock).toHaveBeenCalledTimes(2);
    expect(screen.getByTestId('tasks').textContent).toBe('t1:running:40,t2:running:40');
    expect(screen.getByTestId('focused').textContent).toBe('t2');
    // 两个任务各自绑定了独立的传输
    expect(wsHandlers.has('t1')).toBe(true);
    expect(wsHandlers.has('t2')).toBe(true);
  });

  test('WS 消息按 taskId 路由：t2 的进度不污染 t1', async () => {
    await mountAndStart();
    await startTask('t2');

    await sendWs('t2', {
      type: 'progress',
      data: {
        phase: 'planning',
        currentRound: 2,
        totalRounds: 9,
        progress: 77,
        message: 't2 进度',
        timestamp: '2026-10-08T02:00:00Z',
      },
    });

    expect(screen.getByTestId('tasks').textContent).toBe('t1:running:40,t2:running:77');
  });

  test('停止 t1 只断 t1：取消请求带 t1、t1 终态、t2 传输与消息仍然活着', async () => {
    await mountAndStart();
    await startTask('t2');

    await act(async () => {
      await handle.ctx.stopMining('t1');
    });

    expect(cancelMiningMock).toHaveBeenCalledWith('t1');
    expect(screen.getByTestId('tasks').textContent).toBe('t1:failed:40,t2:running:40');
    expect(wsClosed.get('t1')).toHaveBeenCalled();
    expect(wsClosed.get('t2')).not.toHaveBeenCalled();

    // t2 仍然接收消息（传输没有被 t1 的停止连带拆掉）
    await sendWs('t2', {
      type: 'log',
      data: { id: 'x', timestamp: '2026-10-08T02:01:00Z', level: 'info', message: 't2 alive' },
    });
    expect(screen.getByTestId('tasks').textContent).toContain('t2:running');
  });

  test('恢复：listTasks 里全部运行中任务都绑定传输，焦点取最新一条', async () => {
    listTasksMock.mockResolvedValue({
      success: true,
      data: {
        tasks: [
          mkTask('tA', { createdAt: '2026-10-08T00:00:00Z' }),
          mkTask('tB', { createdAt: '2026-10-08T01:00:00Z' }),
          mkTask('tC', { status: 'completed', createdAt: '2026-10-07T00:00:00Z' }),
        ],
      },
    });
    render(
      <TaskProvider>
        <Probe />
      </TaskProvider>,
    );
    await flush();

    expect(screen.getByTestId('tasks').textContent).toBe('tA:running:40,tB:running:40');
    expect([...wsHandlers.keys()].sort()).toEqual(['tA', 'tB']);
    expect(screen.getByTestId('focused').textContent).toBe('tB');
  });

  test('提交失败（429）不产生假任务行，错误原文进 miningStartError', async () => {
    render(
      <TaskProvider>
        <Probe />
      </TaskProvider>,
    );
    await flush();

    startMiningMock.mockRejectedValue({
      response: { data: { detail: '您已有 2 个挖掘任务在运行（上限 2），请等待完成或先取消任务。' } },
    });
    await act(async () => {
      handle.ctx.startMining({ userInput: 'x' } as any);
    });

    expect(screen.getByTestId('tasks').textContent).toBe('');
    expect(screen.getByTestId('focused').textContent).toBe('none');
    expect(screen.getByTestId('start-error').textContent).toContain('上限 2');
    // 下次成功提交后错误清场
    await startTask('t1');
    expect(screen.getByTestId('start-error').textContent).toBe('none');
  });

  test('提交在途锁：POST 未返回时重复提交被忽略，返回后解锁', async () => {
    render(
      <TaskProvider>
        <Probe />
      </TaskProvider>,
    );
    await flush();

    let resolveStart: (v: any) => void = () => {};
    startMiningMock.mockImplementation(
      () => new Promise((res) => { resolveStart = res; }),
    );
    act(() => {
      handle.ctx.startMining({ userInput: 'a' } as any);
    });
    await flush();
    expect(handle.ctx.miningStarting).toBe(true);

    act(() => {
      handle.ctx.startMining({ userInput: 'b' } as any);
    });
    expect(startMiningMock).toHaveBeenCalledTimes(1);

    await act(async () => {
      resolveStart({ success: true, data: { taskId: 't1', task: mkTask('t1') } });
    });
    expect(handle.ctx.miningStarting).toBe(false);
    expect(screen.getByTestId('tasks').textContent).toBe('t1:running:40');
  });

  test('focusMiningTask 切换聚焦任务，miningTask 派生跟随', async () => {
    await mountAndStart();
    await startTask('t2');
    expect(handle.ctx.miningTask?.taskId).toBe('t2');

    act(() => {
      handle.ctx.focusMiningTask('t1');
    });

    expect(screen.getByTestId('focused').textContent).toBe('t1');
    expect(handle.ctx.miningTask?.taskId).toBe('t1');
    // 认不出的任务 id 不改焦点（防止悬空焦点）
    act(() => {
      handle.ctx.focusMiningTask('nope');
    });
    expect(screen.getByTestId('focused').textContent).toBe('t1');
  });

  test('resetMiningTask 只移除指定任务并改焦到剩余最新，传输同步拆除', async () => {
    await mountAndStart();
    await startTask('t2');

    await act(async () => {
      handle.ctx.resetMiningTask('t2');
    });

    expect(screen.getByTestId('tasks').textContent).toBe('t1:running:40');
    expect(screen.getByTestId('focused').textContent).toBe('t1');
    expect(wsClosed.get('t2')).toHaveBeenCalled();
    expect(wsClosed.get('t1')).not.toHaveBeenCalled();
  });
});

describe('TaskContext：并行方向数（T-MV-04）', () => {
  test('N>1 回执逐条接纳：全进注册表、各绑传输、焦点给第一条、seq 触发进演化台', async () => {
    render(
      <TaskProvider>
        <Probe />
      </TaskProvider>,
    );
    await flush();
    const seq0 = handle.ctx.miningStartSeq;

    startMiningMock.mockResolvedValue({
      success: true,
      data: {
        taskId: 'm1',
        task: mkTask('m1'),
        tasks: [mkTask('m1'), mkTask('m2', { status: 'queued' }), mkTask('m3')],
        failures: [],
        message: 'A股 已派发 3 条方向任务（启动 2 / 排队 1 / 失败 0）',
      },
    });
    await act(async () => {
      handle.ctx.startMining({ userInput: '' } as any);
    });

    expect(screen.getByTestId('tasks').textContent).toBe(
      'm1:running:40,m2:queued:40,m3:running:40',
    );
    expect(screen.getByTestId('focused').textContent).toBe('m1');
    expect([...wsHandlers.keys()].sort()).toEqual(['m1', 'm2', 'm3']);
    expect(handle.ctx.miningStartSeq).toBe(seq0 + 1);
    expect(screen.getByTestId('start-error').textContent).toBe('none');
  });

  test('部分失败：成功条目照常入库，失败摘要进 miningStartError（不装全成功）', async () => {
    render(
      <TaskProvider>
        <Probe />
      </TaskProvider>,
    );
    await flush();

    startMiningMock.mockResolvedValue({
      success: true,
      data: {
        taskId: 'm1',
        tasks: [mkTask('m1')],
        failures: [{ direction: '方向二', error: '队列已满（上限 8）' }],
      },
    });
    await act(async () => {
      handle.ctx.startMining({ userInput: '' } as any);
    });

    expect(screen.getByTestId('tasks').textContent).toBe('m1:running:40');
    expect(screen.getByTestId('focused').textContent).toBe('m1');
    expect(screen.getByTestId('start-error').textContent).toContain('方向二');
    expect(screen.getByTestId('start-error').textContent).toContain('队列已满');
  });

  test('全部失败：不留假任务行、焦点不动、不触发自动进演化台，错误逐条上屏', async () => {
    render(
      <TaskProvider>
        <Probe />
      </TaskProvider>,
    );
    await flush();
    const seq0 = handle.ctx.miningStartSeq;

    startMiningMock.mockResolvedValue({
      success: true,
      data: {
        taskId: '',
        tasks: [],
        failures: [
          { direction: '方向一', error: '队列已满' },
          { direction: '方向二', error: '硬件锁被占用' },
        ],
      },
    });
    await act(async () => {
      handle.ctx.startMining({ userInput: '' } as any);
    });

    expect(screen.getByTestId('tasks').textContent).toBe('');
    expect(screen.getByTestId('focused').textContent).toBe('none');
    expect(handle.ctx.miningStartSeq).toBe(seq0);
    expect(screen.getByTestId('start-error').textContent).toContain('启动失败');
    expect(screen.getByTestId('start-error').textContent).toContain('硬件锁被占用');
  });
});
