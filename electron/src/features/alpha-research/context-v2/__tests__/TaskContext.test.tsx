/**
 * TaskContext —— 「挖到多少显示多少」与「指标不造假」的数据层契约。
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
 * - **任务完成沿自动拉一次全量清单**。
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
} = vi.hoisted(() => ({
  startMiningMock: vi.fn(),
  listTasksMock: vi.fn(),
  healthCheckMock: vi.fn(),
  getFactorsMock: vi.fn(),
  connectMiningWsMock: vi.fn(),
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
  };
});

/** 捕获 WS 消息处理器（bindMiningTransport 把 handleMiningWsMessage 传进来） */
let wsOnMessage: ((msg: any) => void) | null = null;

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
  wsOnMessage = null;

  healthCheckMock.mockResolvedValue(true);
  listTasksMock.mockResolvedValue({ success: true, data: { tasks: [] } });
  connectMiningWsMock.mockImplementation((_taskId: string, onMessage: (msg: any) => void) => {
    wsOnMessage = onMessage;
    return { close: vi.fn(), _pollingTimeoutId: undefined };
  });
});

const mkTask = (): Task =>
  ({
    taskId: 't1',
    status: 'running',
    config: { userInput: 'momentum' },
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

async function mountAndStart(): Promise<void> {
  render(
    <TaskProvider>
      <Probe />
    </TaskProvider>,
  );
  await flush(); // healthCheck / listTasks 落地
  startMiningMock.mockResolvedValue({ success: true, data: { taskId: 't1', task: mkTask() } });
  await act(async () => {
    handle.ctx.startMining({ userInput: 'momentum' } as any);
  });
  expect(screen.getByTestId('status').textContent).toBe('running');
  expect(wsOnMessage).toBeTruthy();
}

describe('TaskContext：全量因子清单（挖到多少显示多少）', () => {
  test('refreshMiningFactors 带 taskId + limit=500，25 行去重合并成 24 个因子', async () => {
    getFactorsMock.mockResolvedValue({
      success: true,
      data: { factors: rawFactors(), total: 25, limit: 500, offset: 0, serverLimit: 500 },
    });
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
    getFactorsMock.mockResolvedValue({
      success: true,
      data: { factors: rawFactors(), total: 25, limit: 500, offset: 0, serverLimit: 500 },
    });
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

  test('不带参数的 refreshMiningFactors 用当前任务 id', async () => {
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
    getFactorsMock.mockResolvedValue({
      success: true,
      data: { factors: rawFactors(), total: 25, limit: 500, offset: 0, serverLimit: 500 },
    });
    await mountAndStart();
    await act(async () => {
      await handle.ctx.refreshMiningFactors('t1');
    });
    expect(screen.getByTestId('count').textContent).toBe('24');

    await act(async () => {
      wsOnMessage!({
        type: 'log',
        data: {
          id: 'l1',
          timestamp: '2026-10-08T00:01:00Z',
          level: 'info',
          message: 'Added new factor: momentum_reversal_5d',
        },
      });
    });

    expect(screen.getByTestId('count').textContent).toBe('24'); // 没有第 25 个假行
    expect(screen.getByTestId('logs').textContent).toBe('1');
  });
});

describe('TaskContext：任务完成沿自动拉全量清单', () => {
  test('WS result 置 completed 后自动再拉一次 /factors?task_id', async () => {
    getFactorsMock.mockResolvedValue({
      success: true,
      data: { factors: rawFactors(), total: 25, limit: 500, offset: 0, serverLimit: 500 },
    });
    await mountAndStart();
    const callsBefore = getFactorsMock.mock.calls.length;

    await act(async () => {
      wsOnMessage!({ type: 'result', data: { status: 'completed' } });
    });
    await flush();

    expect(screen.getByTestId('status').textContent).toBe('completed');
    expect(getFactorsMock.mock.calls.length).toBe(callsBefore + 1);
    expect(getFactorsMock).toHaveBeenLastCalledWith({ taskId: 't1', limit: 500 });
  });
});
