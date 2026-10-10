/**
 * 挖掘方向两组（T-MV-06）：训练特征目录 + 因子值库目录。
 *
 * 钉住三条纪律：
 * 1. **方向 label 不带磁盘数字**——label 会存 localStorage 并在派发时作为
 *    direction 下发；塞列数/日期后盘面一变（219→221）已存 label 与列表失配
 *    被静默丢弃（日切白名单旧坑）。事实只走副行展示。
 * 2. **事实摘要诚实**：CN > HK > US 优先级取第一个有数据的市场；全无 →
 *    「本机无数据目录」，绝不假报 0 列。
 * 3. **分组与回落**：categories 空 → 内置参考且 source='reference'（组标题
 *    据此改文案，不冒充 feature catalog）；接口抛错 → 整组回落但形状完整。
 */
import { beforeEach, describe, expect, test, vi } from 'vitest';

import type { FactorLibrary } from '../../types-v2';
import {
  REFERENCE_MINING_DIRECTIONS,
  fetchMiningDirectionGroups,
  libraryDirectionLabel,
  libraryFactSummary,
} from '../miningDirections';

vi.mock('../../services-v2/api', () => ({ getFactorCategories: vi.fn() }));

import { getFactorCategories } from '../../services-v2/api';

const lib = (over: Partial<FactorLibrary> = {}): FactorLibrary => ({
  id: 'l2_factors',
  name: 'L2 因子',
  kind: 'microstructure',
  description: 'L2 微观结构因子',
  excluded: false,
  markets: { CN: { columns: 219, start: '2018-01-02', end: '2026-10-09' } },
  ...over,
});

describe('libraryDirectionLabel / libraryFactSummary', () => {
  test('label 只有策展文本；磁盘事实变化不改变 label', () => {
    const a = libraryDirectionLabel(lib());
    const b = libraryDirectionLabel(
      lib({ markets: { CN: { columns: 999, start: null, end: null } } }),
    );
    expect(a).toBe('L2 因子 · 因子值库');
    expect(a).toBe(b);
  });

  test('事实摘要按 CN > HK > US 取第一个有数据的市场', () => {
    const item = lib({
      markets: {
        US: { columns: 182, start: '2001-01-02', end: '2026-10-08' },
        HK: { columns: 191, start: '2010-01-04', end: '2026-10-08' },
        CN: { columns: 219, start: '2018-01-02', end: '2026-10-09' },
      },
    });
    expect(libraryFactSummary(item)).toBe('CN · 219 列 · 2018-01-02 ~ 2026-10-09');

    const hkOnly = lib({
      markets: { US: null, HK: { columns: 24, start: '2024-11-25', end: '2026-10-08' } },
    });
    expect(libraryFactSummary(hkOnly)).toBe('HK · 24 列 · 2024-11-25 ~ 2026-10-08');
  });

  test('列数读不出 → 只报日期；只有起点 → 「起」；全无 → 本机无数据目录（不假报 0）', () => {
    expect(
      libraryFactSummary(
        lib({ markets: { CN: { columns: null, start: '2020-01-02', end: '2026-10-09' } } }),
      ),
    ).toBe('CN · 2020-01-02 ~ 2026-10-09');

    expect(
      libraryFactSummary(
        lib({ markets: { CN: { columns: 5, start: '2020-01-02', end: null } } }),
      ),
    ).toBe('CN · 5 列 · 2020-01-02 起');

    expect(libraryFactSummary(lib({ markets: { CN: null, HK: null } }))).toBe(
      '本机无数据目录',
    );
    expect(libraryFactSummary(lib({ markets: {} }))).toBe('本机无数据目录');
  });
});

describe('fetchMiningDirectionGroups', () => {
  beforeEach(() => {
    vi.mocked(getFactorCategories).mockReset();
  });

  test('categories→目录组；libraries 按 excluded 分组；checkedAt 透传', async () => {
    vi.mocked(getFactorCategories).mockResolvedValue({
      success: true,
      data: {
        categories: [{ id: 'c1', name: '动量', featureCount: 12, sampleFeatures: ['mom_5'] }],
        libraries: [
          lib(),
          lib({ id: 'features_daily', name: '每日特征（技术+估值）', excluded: true }),
        ],
        librariesCheckedAt: '2026-10-10',
      },
    });
    const groups = await fetchMiningDirectionGroups();
    expect(groups.catalogSource).toBe('feature_catalog');
    expect(groups.catalog.map((d) => d.label)).toEqual(['动量类因子 (12)']);
    expect(groups.libraries.map((d) => d.label)).toEqual(['L2 因子 · 因子值库']);
    expect(groups.libraries[0].library.id).toBe('l2_factors');
    expect(groups.excludedLibraries.map((l) => l.id)).toEqual(['features_daily']);
    expect(groups.checkedAt).toBe('2026-10-10');
  });

  test('categories 为空 → 内置参考（source=reference），库组不受牵连', async () => {
    vi.mocked(getFactorCategories).mockResolvedValue({
      success: true,
      data: { categories: [], libraries: [lib()], librariesCheckedAt: '' },
    });
    const groups = await fetchMiningDirectionGroups();
    expect(groups.catalog).toBe(REFERENCE_MINING_DIRECTIONS);
    expect(groups.catalogSource).toBe('reference');
    expect(groups.libraries).toHaveLength(1);
  });

  test('接口抛错 → 整体回落且形状完整（库组空）', async () => {
    vi.mocked(getFactorCategories).mockRejectedValue(new Error('offline'));
    const groups = await fetchMiningDirectionGroups();
    expect(groups.catalog).toBe(REFERENCE_MINING_DIRECTIONS);
    expect(groups.catalogSource).toBe('reference');
    expect(groups.libraries).toEqual([]);
    expect(groups.excludedLibraries).toEqual([]);
    expect(groups.checkedAt).toBe('');
  });
});
