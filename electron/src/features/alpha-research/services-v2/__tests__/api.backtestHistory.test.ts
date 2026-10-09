/**
 * 回测历史端点（`GET /alpha-agent/factors/{id}/backtests`）请求与映射契约。
 *
 * 钉死的边：
 * - URL 与 limit 透传（后端 Query 夹 [1,100]，客户端先夹再发，多要一个就是 422）；
 * - 行映射：run_id/status/池/源/区间/起止时间 → camelCase；指标复用与
 *   getBacktestStatus 同一套映射（ICIR 列与 metadata 双源、缺失一律不写键，
 *   界面显「—」，禁止补 0）；
 * - 接口失败 → success:false 带原文（历史面板要显示错误，不能静默空表）。
 */
import { describe, test, expect, vi, beforeEach } from 'vitest';

const { apiGetMock, apiPostMock } = vi.hoisted(() => ({
  apiGetMock: vi.fn(),
  apiPostMock: vi.fn(),
}));

vi.mock('../../../../services/aiStrategyClients', () => ({
  apiClient: { get: apiGetMock, post: apiPostMock },
}));

import { listFactorBacktests } from '../api';

const FULL_ROW = {
  run_id: 'r1',
  factor_id: 'f1',
  status: 'completed',
  market: 'a_share',
  universe: 'csi500',
  data_source: 'qlib_bin',
  date_range: '2024-01-01~2024-12-31',
  ic_value: 0.021,
  rank_ic: 0.03,
  icir: null, // 列缺失 → metadata 兜底
  rank_icir: 0.72,
  sharpe_ratio: 1.13,
  annual_return: 0.152,
  max_drawdown: 0.2,
  error: null,
  created_at: '2026-10-09T02:00:00Z',
  finished_at: '2026-10-09T02:03:00Z',
  metadata: { icir: 0.51, n_obs: 240, quality: { pfs: 0.9 } },
};

beforeEach(() => {
  apiGetMock.mockReset();
  apiPostMock.mockReset();
});

describe('listFactorBacktests：端点与 limit', () => {
  test('GET /alpha-agent/factors/{id}/backtests，默认 limit=20', async () => {
    apiGetMock.mockResolvedValue({ data: { data: { runs: [] } } });

    await listFactorBacktests('f1');

    expect(apiGetMock).toHaveBeenCalledTimes(1);
    const url = apiGetMock.mock.calls[0][0] as string;
    expect(url).toContain('/alpha-agent/factors/f1/backtests');
    expect(url).toContain('limit=20');
  });

  test('limit 越界夹到 [1,100]（后端 Query le=100，多要会被 422）', async () => {
    apiGetMock.mockResolvedValue({ data: { data: { runs: [] } } });

    await listFactorBacktests('f1', 9999);
    expect(apiGetMock.mock.calls[0][0]).toContain('limit=100');

    await listFactorBacktests('f1', 0);
    expect(apiGetMock.mock.calls[1][0]).toContain('limit=1');
  });

  test('factorId 进 URL 前编码（含空格/斜杠的 id 不能拆路径）', async () => {
    apiGetMock.mockResolvedValue({ data: { data: { runs: [] } } });

    await listFactorBacktests('f 1/x');

    expect(apiGetMock.mock.calls[0][0]).toContain('/factors/f%201%2Fx/backtests');
  });
});

describe('listFactorBacktests：行映射', () => {
  test('运行行 → camelCase；ICIR 列与 metadata 双源；缺失指标不写键', async () => {
    apiGetMock.mockResolvedValue({ data: { data: { runs: [FULL_ROW] } } });

    const res = await listFactorBacktests('f1');
    expect(res.success).toBe(true);
    const run = res.data!.runs[0];

    expect(run.runId).toBe('r1');
    expect(run.status).toBe('completed');
    expect(run.universe).toBe('csi500');
    expect(run.dataSource).toBe('qlib_bin');
    expect(run.dateRange).toBe('2024-01-01~2024-12-31');
    expect(run.startedAt).toBe('2026-10-09T02:00:00Z');
    expect(run.finishedAt).toBe('2026-10-09T02:03:00Z');
    expect(run.error).toBeNull();

    expect(run.metrics.ic).toBeCloseTo(0.021);
    expect(run.metrics.icir).toBeCloseTo(0.51); // 列 NULL → metadata 兜底
    expect(run.metrics.rankIcir).toBeCloseTo(0.72);
    expect(run.metrics.pfs).toBeCloseTo(0.9);
    expect(run.metrics.nObs).toBe(240);
    // 缺失指标绝不补 0（「换手 0」与「没算过」是两回事）
    expect('annTurnover' in run.metrics).toBe(false);
    expect('sharpeNet' in run.metrics).toBe(false);
  });

  test('ICIR 列有值时压过 metadata（列 0.6 胜 metadata 0.2，优先级不翻转）', async () => {
    apiGetMock.mockResolvedValue({
      data: {
        data: {
          runs: [{ ...FULL_ROW, icir: 0.6, metadata: { icir: 0.2 } }],
        },
      },
    });

    const res = await listFactorBacktests('f1');
    expect(res.data!.runs[0].metrics.icir).toBeCloseTo(0.6);
  });

  test('字符串数字归一为数字，NaN/空串视为缺失（DB numeric 可能以字符串下发）', async () => {
    apiGetMock.mockResolvedValue({
      data: {
        data: {
          runs: [
            { ...FULL_ROW, ic_value: '0.05', rank_ic: Number.NaN, metadata: {} },
          ],
        },
      },
    });

    const res = await listFactorBacktests('f1');
    const metrics = res.data!.runs[0].metrics;
    expect(metrics.ic).toBeCloseTo(0.05);
    expect('rankIc' in metrics).toBe(false);
  });

  test('失败行带 error 原文与空指标', async () => {
    apiGetMock.mockResolvedValue({
      data: {
        data: {
          runs: [
            {
              run_id: 'r2',
              factor_id: 'f1',
              status: 'failed',
              error: 'RuntimeError: 因子炸了',
              created_at: '2026-10-09T03:00:00Z',
              finished_at: '2026-10-09T03:01:00Z',
              metadata: {},
            },
          ],
        },
      },
    });

    const res = await listFactorBacktests('f1');
    const run = res.data!.runs[0];
    expect(run.status).toBe('failed');
    expect(run.error).toBe('RuntimeError: 因子炸了');
    expect(Object.keys(run.metrics)).toHaveLength(0);
  });

  test('接口失败 → success:false 且带 HTTP 原文（不静默空表）', async () => {
    apiGetMock.mockRejectedValue({
      response: { data: { detail: 'Factor f1 not found' } },
    });

    const res = await listFactorBacktests('f1');
    expect(res.success).toBe(false);
    expect(res.error).toContain('Factor f1 not found');
  });
});
