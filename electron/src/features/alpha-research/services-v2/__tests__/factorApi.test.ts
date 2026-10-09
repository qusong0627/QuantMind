/**
 * 因子清单 / 回测端点的请求契约（用户「回测点了没有用」「挖到多少显示多少」的根因层）。
 *
 * 钉死的边：
 * - `getFactors` 必须把 `task_id` 透传到查询串（权威全量清单的前提），
 *   limit 客户端先夹到 [1, 500]——后端 Query(le=500)，多要一个就是 422；
 * - `cancelBacktest` 是真 POST（旧实现是空 stub：点「停止回测」后端子进程照跑）；
 * - `startBacktest` 的两种返回（已触发/已在跑）都必须归一为 running
 *   （'backtesting' 直接透传会落进 normalizeTaskStatus 的 default → idle）；
 * - `getBacktestStatus` 的 status 映射：'pending'（从未回测）→ idle「未回测」，
 *   'backtesting' → running，failed/cancelled → failed 且带 metadata.backtest_error 原文。
 */
import { describe, test, expect, vi, beforeEach } from 'vitest';

const { apiGetMock, apiPostMock } = vi.hoisted(() => ({
  apiGetMock: vi.fn(),
  apiPostMock: vi.fn(),
}));

vi.mock('../../../../services/aiStrategyClients', () => ({
  apiClient: { get: apiGetMock, post: apiPostMock },
}));

import {
  FACTOR_LIST_MAX_LIMIT,
  cancelBacktest,
  getBacktestStatus,
  getFactorRecoveryStatus,
  getFactors,
  startBacktest,
  startFactorRecovery,
} from '../api';

beforeEach(() => {
  apiGetMock.mockReset();
  apiPostMock.mockReset();
});

describe('getFactors：task_id 透传与 limit 夹取', () => {
  test('带 taskId 时查询串有 task_id，limit 默认 500', async () => {
    apiGetMock.mockResolvedValue({ data: { data: { factors: [], limit: 500 } } });

    await getFactors({ taskId: 't-1' });

    expect(apiGetMock).toHaveBeenCalledTimes(1);
    const url = apiGetMock.mock.calls[0][0] as string;
    expect(url).toContain('/alpha-agent/factors?');
    expect(url).toContain('task_id=t-1');
    expect(url).toContain('limit=500');
  });

  test('limit 超上限夹到 500、下限夹到 1（后端 le=500，多要会被 422 拒绝）', async () => {
    apiGetMock.mockResolvedValue({ data: { data: { factors: [], limit: 500 } } });

    await getFactors({ limit: 9999 });
    expect(apiGetMock.mock.calls[0][0]).toContain('limit=500');

    await getFactors({ limit: 0 });
    expect(apiGetMock.mock.calls[1][0]).toContain('limit=1');
  });

  test('响应里的服务端 limit 透传给 UI（诚实提示「已达单次上限」）', async () => {
    apiGetMock.mockResolvedValue({
      data: { data: { factors: [{ id: 'f1', factor_name: 'a' }], limit: 500 } },
    });

    const res = await getFactors({ taskId: 't' });

    expect(res.data?.serverLimit).toBe(500);
    expect(res.data?.factors).toHaveLength(1);
  });

  test('FACTOR_LIST_MAX_LIMIT 与服务端 Query(le=500) 对齐', () => {
    expect(FACTOR_LIST_MAX_LIMIT).toBe(500);
  });

  test('offset 透传到查询串（服务端分页）并回显；offset=0 不写查询串', async () => {
    apiGetMock.mockResolvedValue({
      data: { data: { factors: [], limit: 200, offset: 200 } },
    });
    const res = await getFactors({ limit: 200, offset: 200 });
    expect(apiGetMock.mock.calls[0][0]).toContain('offset=200');
    expect(res.data?.offset).toBe(200);

    await getFactors({ limit: 500 });
    expect(apiGetMock.mock.calls[1][0]).not.toContain('offset=');
  });

  test('total 用服务端全量口径、quality_counts 透传（「为啥就显示 200」回归锚）', async () => {
    // 窗口只回 1 行、全量 291——total 必须是 291 而不是窗口长度
    apiGetMock.mockResolvedValue({
      data: {
        data: {
          factors: [{ factor_id: 'f1' }],
          total: 291,
          limit: 200,
          offset: 0,
          quality_counts: { high: 0, medium: 47, low: 199, unknown: 45 },
        },
      },
    });
    const res = await getFactors({ limit: 200 });
    expect(res.data?.total).toBe(291);
    expect(res.data?.qualityCounts).toEqual({
      high: 0,
      medium: 47,
      low: 199,
      unknown: 45,
    });
  });

  test('服务端缺 total（旧后端）退回窗口长度；qualityCounts 为 null，不编造', async () => {
    apiGetMock.mockResolvedValue({
      data: { data: { factors: [{ factor_id: 'f1' }, { factor_id: 'f2' }], limit: 200 } },
    });
    const res = await getFactors({ limit: 200 });
    expect(res.data?.total).toBe(2);
    expect(res.data?.qualityCounts).toBeNull();
  });
});

describe('cancelBacktest：真调取消端点（旧实现是空 stub）', () => {
  test('POST /alpha-agent/factors/{id}/cancel', async () => {
    apiPostMock.mockResolvedValue({ data: { data: { ok: true } } });

    await cancelBacktest('fid-1');

    expect(apiPostMock).toHaveBeenCalledWith('/alpha-agent/factors/fid-1/cancel');
  });
});

describe('startBacktest：POST 真实回测端点，返回归一为 running', () => {
  test('POST /factors/{id}/backtest 且 task.status 是 running', async () => {
    apiPostMock.mockResolvedValue({
      data: { data: { factor_id: 'fid-1', message: '回测已在进行中' } },
    });

    const res = await startBacktest({ factorId: 'fid-1', universe: 'csi300' });

    expect(apiPostMock).toHaveBeenCalledWith(
      '/alpha-agent/factors/fid-1/backtest?universe=csi300',
    );
    expect(res.data?.taskId).toBe('fid-1');
    expect(res.data?.task.status).toBe('running');
  });

  test('无 factorId 直接失败，不发请求', async () => {
    const res = await startBacktest({ factorId: '' });

    expect(res.success).toBe(false);
    expect(apiPostMock).not.toHaveBeenCalled();
  });
});

describe('补码评估（待评估存量因子的批量补码+回测）', () => {
  test('startFactorRecovery POST /factors/recovery?limit=…，data 段映射为 camelCase', async () => {
    apiPostMock.mockResolvedValue({
      data: {
        data: {
          running: true,
          total: 45,
          done: 3,
          failed: 1,
          skipped: 0,
          current_factor_id: 'f-9',
          current_factor_name: 'Overnight_Gap',
          message: null,
          started_at: '2026-10-09T01:00:00+00:00',
          finished_at: null,
          user_id: 'u-should-be-dropped',
        },
      },
    });

    const res = await startFactorRecovery();

    expect(apiPostMock).toHaveBeenCalledWith('/alpha-agent/factors/recovery?limit=200');
    expect(res.success).toBe(true);
    expect(res.data?.running).toBe(true);
    expect(res.data?.total).toBe(45);
    expect(res.data?.currentFactorName).toBe('Overnight_Gap');
    expect(res.data?.finishedAt).toBeNull();
  });

  test('412（未配置 LLM Key）时 detail 原文进 error，不吞成泛化文案', async () => {
    const detail =
      '未配置 LLM API Key：可在个人中心「其他设置 → AI 服务配置」填写，或在服务器 .env 配置。';
    apiPostMock.mockRejectedValue({ response: { status: 412, data: { detail } } });

    const res = await startFactorRecovery();

    expect(res.success).toBe(false);
    expect(res.error).toBe(detail);
  });

  test('getFactorRecoveryStatus GET /factors/recovery/status（3 段路径不与 /factors/{id} 混）', async () => {
    apiGetMock.mockResolvedValue({
      data: { data: { running: false, total: 45, done: 45, failed: 0, skipped: 0 } },
    });

    const res = await getFactorRecoveryStatus();

    expect(apiGetMock).toHaveBeenCalledWith('/alpha-agent/factors/recovery/status');
    expect(res.data?.running).toBe(false);
    expect(res.data?.done).toBe(45);
  });

  test('状态查询失败归为 success:false 带原文（页面把错误显示在评估条上）', async () => {
    apiGetMock.mockRejectedValue({
      response: { status: 500, data: { detail: 'boom' } },
    });
    const res = await getFactorRecoveryStatus();
    expect(res.success).toBe(false);
    expect(res.error).toBe('boom');
  });
});

describe('getBacktestStatus：状态映射（attach 未回测过的因子≠永远回测中）', () => {
  const respond = (status: string, extra: Record<string, unknown> = {}) =>
    apiGetMock.mockResolvedValue({ data: { data: { status, ...extra } } });

  test('pending（从未回测）→ idle，文案「未回测」', async () => {
    respond('pending');
    const res = await getBacktestStatus('f1');
    expect(res.data?.task.status).toBe('idle');
    expect(res.data?.task.progress.message).toBe('未回测');
  });

  test('backtesting → running', async () => {
    respond('backtesting');
    const res = await getBacktestStatus('f1');
    expect(res.data?.task.status).toBe('running');
  });

  test('completed → completed', async () => {
    respond('completed');
    const res = await getBacktestStatus('f1');
    expect(res.data?.task.status).toBe('completed');
  });

  test('failed → failed，且 error 取 metadata.backtest_error 原文', async () => {
    respond('failed', { metadata: { backtest_error: '数据区间超出日历：2010-01-01' } });
    const res = await getBacktestStatus('f1');
    expect(res.data?.task.status).toBe('failed');
    expect(res.data?.error).toBe('数据区间超出日历：2010-01-01');
  });

  test('归属校验 404 → failed 且原文带回（行内三态要显示失败原因，不能只吞）', async () => {
    apiGetMock.mockRejectedValue({
      response: { status: 404, data: { detail: '因子不存在或无权访问' } },
    });
    const res = await getBacktestStatus('someone-elses');
    expect(res.data?.task.status).toBe('failed');
    expect(res.data?.error).toBe('因子不存在或无权访问');
  });
});
