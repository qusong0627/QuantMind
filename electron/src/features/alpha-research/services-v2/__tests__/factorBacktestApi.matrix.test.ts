/**
 * fetchMatrix 映射契约 —— 重点是 T-FB-18 显著性段（significance）。
 *
 * 组件测试全部 mock 了服务层，这里是「原始 snake_case → camelCase」的
 * 唯一直接钉子：
 * - 显著性段的 nw_t / p_value / q_value_bhy / family_n / family_note 逐键映射；
 * - significance 整体缺失或 null（失败/未跑格）→ null，绝不伪造对象或 0；
 * - factor_id / cn_ic 等既有键的映射顺带钉住（回归护栏）。
 */
import { describe, test, expect, vi, beforeEach } from 'vitest';

const { apiPostMock } = vi.hoisted(() => ({
  apiPostMock: vi.fn(),
}));

vi.mock('../../../../services/aiStrategyClients', () => ({
  apiClient: { get: vi.fn(), post: apiPostMock },
}));

import { fetchMatrix } from '../factorBacktestApi';

beforeEach(() => {
  apiPostMock.mockReset();
});

const FAMILY_NOTE = '族 = 同一批次全部完成单元（NW t → 正态双侧 p → BY 校正）';

function _rawResult() {
  return {
    data: {
      data: {
        markets: [
          { market: 'crypto', label: '区块链', in_sample: false, experimental: true, benchmark: null, cost_bps: 20 },
        ],
        counts: { completed: 1, failed: 1 },
        factors: [
          {
            factor_id: 'f-1',
            factor_name: '因子A',
            found: true,
            owned: true,
            cn_ic: 0.011,
            cells: {
              crypto: {
                status: 'completed',
                run_id: 'fb-r1',
                compat: 'portable',
                missing: [],
                dynamic: false,
                error: null,
                universe: 'all',
                date_range: '2023~2026',
                finished_at: '2026-10-10T02:00:00Z',
                in_sample: false,
                metrics: { rank_ic: 0.05, ic_nw_t: 5.6 },
                significance: {
                  nw_t: 5.6,
                  p_value: 2.033161038274203e-8,
                  q_value_bhy: 4.420673000361909e-7,
                  family_n: 8,
                  family_note: FAMILY_NOTE,
                },
              },
              us_stock: {
                status: 'failed',
                run_id: 'fb-r2',
                compat: 'unknown',
                missing: [],
                dynamic: false,
                error: 'boom',
                universe: null,
                date_range: null,
                finished_at: null,
                in_sample: false,
                metrics: {},
                // 失败格没有显著性段（后端给 null）
                significance: null,
              },
            },
          },
        ],
      },
    },
  };
}

describe('fetchMatrix：显著性段映射（T-FB-18）', () => {
  test('POST /factor-backtest/matrix，significance 逐键映射；null 保持 null', async () => {
    apiPostMock.mockResolvedValue(_rawResult());

    const res = await fetchMatrix({ factorIds: ['f-1'], markets: ['crypto', 'us_stock'] });

    expect(apiPostMock).toHaveBeenCalledTimes(1);
    expect(apiPostMock.mock.calls[0][0]).toBe('/factor-backtest/matrix');
    expect(apiPostMock.mock.calls[0][1]).toEqual({
      factor_ids: ['f-1'],
      markets: ['crypto', 'us_stock'],
    });

    expect(res.success).toBe(true);
    const row = res.data!.factors[0];
    expect(row.factorId).toBe('f-1');
    expect(row.cnIc).toBe(0.011);

    expect(row.cells.crypto.significance).toEqual({
      nw_t: 5.6,
      p_value: 2.033161038274203e-8,
      q_value_bhy: 4.420673000361909e-7,
      family_n: 8,
      family_note: FAMILY_NOTE,
    });
    // 失败格：null 就是 null —— 格子显「—」，绝不显示成 0
    expect(row.cells.us_stock.significance).toBeNull();
  });

  test('significance 段字段缺失 → 该键为 null（不伪造数值）', async () => {
    const raw = _rawResult();
    raw.data.data.factors[0].cells.crypto.significance = { nw_t: 1.23 };
    apiPostMock.mockResolvedValue(raw);

    const res = await fetchMatrix({ factorIds: ['f-1'] });

    expect(res.data!.factors[0].cells.crypto.significance).toEqual({
      nw_t: 1.23,
      p_value: null,
      q_value_bhy: null,
      family_n: null,
      family_note: null,
    });
  });
});
