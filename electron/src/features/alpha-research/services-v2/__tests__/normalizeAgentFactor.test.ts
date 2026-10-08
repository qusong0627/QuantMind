/**
 * api.ts 归一化契约：机构级指标透传 + 缺失保 undefined。
 *
 * 背景（本文件防的回归）：
 * - `rankIcir` 曾被硬编码 0——「没算过」被伪装成「算出来是 0」；
 * - qlib 路径的 PFS/RRE/换手/扣费全写进 metadata_json，旧版
 *   `normalizeAgentFactor` / `getBacktestStatus` 一律丢弃，前端永远看不见；
 * - H5 路径的 ICIR 只在表字段（metadata 里没有），旧版 getBacktestStatus
 *   只读 metadata → H5 因子的 ICIR 永远显示不出来。
 */
import { describe, test, expect, vi, beforeEach } from 'vitest';

const { apiGetMock } = vi.hoisted(() => ({ apiGetMock: vi.fn() }));

vi.mock('../../../../services/aiStrategyClients', () => ({
  apiClient: { get: apiGetMock },
}));

import { getBacktestStatus, normalizeAgentFactor } from '../api';

beforeEach(() => {
  apiGetMock.mockReset();
});

const RAW_FULL = {
  id: 'abc123',
  factor_name: 'f1',
  factor_formulation: 'close/mean(close,5)',
  ic_value: 0.03,
  rank_ic: 0.04,
  rank_icir: 0.9,
  icir: 0.8,
  sharpe_ratio: 1.1,
  annual_return: 0.15,
  max_drawdown: 0.08,
  metadata: {
    market: 'a_share',
    universe: 'csi300',
    icir: 0.8,
    rank_icir: 0.9,
    n_obs: 240,
    rre: 0.9374,
    turnover_daily: 0.25,
    ann_turnover: 63.0,
    ann_return_net: 1.554,
    sharpe_net: 6.2,
    max_drawdown_net: 0.011,
    quality: { pfs: 0.91, pfs_gauss: 0.9, pfs_t: 0.88, n_days: 252 },
  },
};

describe('normalizeAgentFactor', () => {
  test('全量元数据 → 各机构级键逐一透传', () => {
    const factor = normalizeAgentFactor(RAW_FULL);

    expect(factor.rankIcir).toBe(0.9);
    expect(factor.rre).toBe(0.9374);
    expect(factor.pfsQuality).toEqual({
      pfs: 0.91,
      pfsGauss: 0.9,
      pfsT: 0.88,
      nDays: 252,
    });
    expect(factor.turnoverDaily).toBe(0.25);
    expect(factor.annTurnover).toBe(63.0);
    expect(factor.annReturnNet).toBe(1.554);
    expect(factor.sharpeNet).toBe(6.2);
    expect(factor.maxDrawdownNet).toBe(0.011);
    expect(factor.nObs).toBe(240);
  });

  test('没有元数据 → 新键保持 undefined，绝不补 0', () => {
    const factor = normalizeAgentFactor({ id: 'x', factor_name: 'n', ic_value: 0.01 });

    expect(factor.rankIcir).toBeUndefined();
    expect(factor.rre).toBeUndefined();
    expect(factor.pfsQuality).toBeUndefined();
    expect(factor.turnoverDaily).toBeUndefined();
    expect(factor.annReturnNet).toBeUndefined();
    expect(factor.sharpeNet).toBeUndefined();
    expect(factor.maxDrawdownNet).toBeUndefined();
    expect(factor.nObs).toBeUndefined();
  });

  test('H5 路径：rank_icir 只在表字段、PFS 在 metadata.quality', () => {
    const factor = normalizeAgentFactor({
      id: 'h5',
      rank_icir: 0.77,
      icir: 0.5,
      metadata: { quality: { pfs: 0.8, n_days: 120 } },
    });

    expect(factor.rankIcir).toBe(0.77); // 表字段优先
    expect(factor.icir).toBe(0.5); // H5 的 icir 只在表字段
    expect(factor.pfsQuality?.pfs).toBe(0.8);
    expect(factor.pfsQuality?.nDays).toBe(120);
    expect(factor.pfsQuality?.pfsGauss).toBeUndefined();
  });

  test('字符串数值也能归一（后端 JSON 偶有字符串）', () => {
    const factor = normalizeAgentFactor({ id: 's', metadata: { rre: '0.93', n_obs: '240' } });
    expect(factor.rre).toBe(0.93);
    expect(factor.nObs).toBe(240);
  });
});

describe('getBacktestStatus 指标映射', () => {
  test('metadata 的机构级键映射进 metrics；没有的键不写入', async () => {
    apiGetMock.mockResolvedValue({
      data: {
        data: {
          status: 'completed',
          ic_value: 0.02,
          rank_ic: 0.03,
          rank_icir: 0.6,
          metadata: {
            rre: 0.9,
            n_obs: 240,
            turnover_daily: 0.25,
            ann_turnover: 63.0,
            ann_return_net: 1.554,
            sharpe_net: 6.2,
            max_drawdown_net: 0.011,
            quality: { pfs: 0.91, pfs_gauss: 0.9 },
          },
        },
      },
    });

    const res = await getBacktestStatus('fid');
    const metrics = res.data?.task.metrics as Record<string, any>;

    expect(metrics.rre).toBe(0.9);
    expect(metrics.nObs).toBe(240);
    expect(metrics.pfs).toBe(0.91);
    expect(metrics.pfsGauss).toBe(0.9);
    expect(metrics.turnoverDaily).toBe(0.25);
    expect(metrics.annTurnover).toBe(63.0);
    expect(metrics.annReturnNet).toBe(1.554);
    expect(metrics.sharpeNet).toBe(6.2);
    expect(metrics.maxDrawdownNet).toBe(0.011);
    expect(metrics.rankIcir).toBe(0.6);
    // PFS-T 后端没给 → 键不写入（页面显「—」而不是 0）
    expect('pfsT' in metrics).toBe(false);
  });

  test('旧回测（没有任何机构级键）→ metrics 里不出现这些键', async () => {
    apiGetMock.mockResolvedValue({
      data: {
        data: {
          status: 'completed',
          ic_value: 0.02,
          metadata: { data_source: 'qlib_bin', market: 'a_share' },
        },
      },
    });

    const res = await getBacktestStatus('fid');
    const metrics = res.data?.task.metrics as Record<string, any>;

    for (const key of [
      'rre', 'pfs', 'pfsGauss', 'pfsT', 'nObs',
      'turnoverDaily', 'annTurnover', 'annReturnNet', 'sharpeNet', 'maxDrawdownNet',
    ]) {
      expect(key in metrics).toBe(false);
    }
  });
});
