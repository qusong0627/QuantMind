/**
 * 指标注册表（metricRegistry.ts）契约测试。
 *
 * 三条命脉：
 * 1. **金样 dual-read**：本地默认描述符表必须与后端金样
 *    `backend/tests/fixtures/miningMetricsGolden.json` 的 registry 段逐字段一致
 *    （后端 `test_mining_plugins_registry.py` 读的是同一份文件——两端同源）。
 * 2. **缺失显「—」、绝不补 0**：`turnover=0`（名单没换）与「没算过」是两回事。
 * 3. **后端失败可离线**：拉取不到注册表时回落到本地默认表，页面照常渲染。
 */
import { describe, test, expect, vi, beforeEach } from 'vitest';
import path from 'node:path';
import fs from 'node:fs';
import { fileURLToPath } from 'node:url';

const { apiGetMock } = vi.hoisted(() => ({ apiGetMock: vi.fn() }));

vi.mock('../../../../services/aiStrategyClients', () => ({
  apiClient: { get: apiGetMock },
}));

import {
  DEFAULT_METRIC_DESCRIPTORS,
  MISSING_METRIC_TEXT,
  __resetMetricRegistryForTests,
  fetchMetricRegistry,
  formatMetricValue,
  getMetricDescriptor,
  mergeMetricDescriptors,
} from '../metricRegistry';

const golden = JSON.parse(
  fs.readFileSync(
    path.resolve(
      path.dirname(fileURLToPath(import.meta.url)),
      '../../../../../../backend/tests/fixtures/miningMetricsGolden.json',
    ),
    'utf-8',
  ),
);

beforeEach(() => {
  apiGetMock.mockReset();
  __resetMetricRegistryForTests();
});

describe('DEFAULT_METRIC_DESCRIPTORS 与后端金样', () => {
  test('与金样 registry 段逐字段一致（含顺序）', () => {
    expect(DEFAULT_METRIC_DESCRIPTORS).toEqual(golden.registry);
  });

  test('本地表成本口径与金样 meta 一致（0.2% 双边，研究口径）', () => {
    const netReturn = getMetricDescriptor('ann_return_net');
    expect(netReturn?.description).toContain('0.2%');
  });
});

describe('formatMetricValue', () => {
  test('null / undefined / NaN 一律「—」', () => {
    expect(formatMetricValue('rre', null)).toBe(MISSING_METRIC_TEXT);
    expect(formatMetricValue('rre', undefined)).toBe(MISSING_METRIC_TEXT);
    expect(formatMetricValue('rre', NaN)).toBe(MISSING_METRIC_TEXT);
    expect(formatMetricValue('rre', Infinity)).toBe(MISSING_METRIC_TEXT);
  });

  test('真实的 0 要显示成 0，不许被当成缺失', () => {
    expect(formatMetricValue('turnover_daily', 0)).toBe('0.0000');
    expect(formatMetricValue('ann_turnover', 0)).toBe('0.00');
  });

  test('精度按描述符：rre 4 位、n_obs 0 位', () => {
    expect(formatMetricValue('rre', 0.9374384380505406)).toBe('0.9374');
    expect(formatMetricValue('n_obs', 252)).toBe('252');
  });

  test('pct 单位的小数值 ×100 加 %（annual_return 是分数不是百分数）', () => {
    expect(formatMetricValue('annual_return', 1.554)).toBe('155.40%');
    expect(formatMetricValue('ann_return_net', -0.011)).toBe('-1.10%');
  });

  test('未知 key 走兜底格式（4 位小数），不抛', () => {
    expect(formatMetricValue('brand_new_metric', 0.5)).toBe('0.5000');
  });

  test('接受描述符对象同样可格式', () => {
    const descriptor = getMetricDescriptor('quality.pfs')!;
    expect(formatMetricValue(descriptor, 0.9)).toBe('0.9000');
  });
});

describe('mergeMetricDescriptors', () => {
  test('按 key 覆盖、新键追加、本地顺序保留', () => {
    const merged = mergeMetricDescriptors(DEFAULT_METRIC_DESCRIPTORS, [
      { ...DEFAULT_METRIC_DESCRIPTORS[0], label: 'IC（后端新标签）' },
      {
        key: 'brand_new',
        label: '新指标',
        group: 'pool',
        unit: 'ratio',
        better: 'none',
        precision: 1,
        description: '',
      },
    ]);
    expect(merged[0].label).toBe('IC（后端新标签）');
    expect(merged).toHaveLength(DEFAULT_METRIC_DESCRIPTORS.length + 1);
    expect(merged[merged.length - 1].key).toBe('brand_new');
  });
});

describe('fetchMetricRegistry', () => {
  test('后端成功：按 key 合并进注册表', async () => {
    apiGetMock.mockResolvedValue({
      data: {
        data: {
          version: 1,
          metrics: [
            {
              key: 'rre',
              label: 'RRE（后端）',
              group: 'robustness',
              unit: 'score',
              better: 'higher',
              precision: 4,
              description: 'x',
            },
          ],
        },
      },
    });

    const registry = await fetchMetricRegistry();

    expect(apiGetMock).toHaveBeenCalledWith('/alpha-agent/metrics/registry');
    expect(registry.find((d) => d.key === 'rre')?.label).toBe('RRE（后端）');
    expect(registry).toHaveLength(DEFAULT_METRIC_DESCRIPTORS.length);
  });

  test('后端离线 / 结构不对：回落本地默认表，不抛', async () => {
    apiGetMock.mockRejectedValue(new Error('offline'));
    expect(await fetchMetricRegistry()).toEqual(DEFAULT_METRIC_DESCRIPTORS);

    apiGetMock.mockResolvedValue({ data: { data: {} } });
    expect(await fetchMetricRegistry()).toEqual(DEFAULT_METRIC_DESCRIPTORS);

    apiGetMock.mockResolvedValue({ data: { data: { metrics: [] } } });
    expect(await fetchMetricRegistry()).toEqual(DEFAULT_METRIC_DESCRIPTORS);
  });

  test('拉取后 getMetricDescriptor 能看到合并结果', async () => {
    apiGetMock.mockResolvedValue({
      data: {
        data: {
          metrics: [
            {
              key: 'rre',
              label: 'RRE-X',
              group: 'robustness',
              unit: 'score',
              better: 'higher',
              precision: 2,
              description: '',
            },
          ],
        },
      },
    });
    await fetchMetricRegistry();
    expect(getMetricDescriptor('rre')?.precision).toBe(2);
    expect(formatMetricValue('rre', 0.9374)).toBe('0.94');
  });
});
