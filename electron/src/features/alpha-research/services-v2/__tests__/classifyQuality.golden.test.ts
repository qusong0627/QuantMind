/**
 * 因子质量分档（前端侧）——与后端 `classify_quality` 共用金样。
 *
 * 金样只有一份（`backend/tests/fixtures/factorQualityGolden.json`），后端
 * `backend/tests/test_factor_quality_golden.py` 反向读同一文件（与
 * `researchScoreGolden.json` 同一套纪律）：任一侧改了阈值，两侧测试立刻红。
 * 瓦片全量计数（后端口径）与列表徽章（前端口径）一旦漂移，用户会看到
 * 「中等 47」却只筛出 24 条——正是本轮「越挖、中等因子越少」修复要消灭的
 * 那类假象。
 */
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { describe, expect, it, vi } from 'vitest';

vi.mock('../../../../services/aiStrategyClients', () => ({
  apiClient: { get: vi.fn(), post: vi.fn() },
}));

import { classifyQuality } from '../api';

/**
 * 直接读文件而不是 `import`：金样在 `electron/` 之外，走模块解析会碰到 vite
 * 的 root 限制。用 `__dirname` 定位（与 `researchScore.test.ts` 同款）。
 */
const GOLDEN_PATH = path.resolve(
  __dirname,
  '../../../../../../backend/tests/fixtures/factorQualityGolden.json',
);
const golden: {
  highMinAbsIc: number;
  mediumMinAbsIc: number;
  cases: Array<{ ic: number | null; quality: string }>;
} = JSON.parse(readFileSync(GOLDEN_PATH, 'utf-8'));

describe('classifyQuality 金样（与后端同口径）', () => {
  it.each(golden.cases)('ic=$ic → $quality', ({ ic, quality }) => {
    expect(classifyQuality(ic)).toBe(quality);
  });

  it('阈值边界与金样逐位一致', () => {
    expect(classifyQuality(golden.highMinAbsIc)).toBe('high');
    expect(classifyQuality(golden.highMinAbsIc - 0.001)).toBe('medium');
    expect(classifyQuality(golden.mediumMinAbsIc)).toBe('medium');
    expect(classifyQuality(golden.mediumMinAbsIc - 0.001)).toBe('low');
  });

  it.each([
    ['undefined', undefined],
    ['NaN', Number.NaN],
    ['Infinity', Number.POSITIVE_INFINITY],
    ['-Infinity', Number.NEGATIVE_INFINITY],
  ])('%s → unknown（缺失不落 low）', (_label, input) => {
    expect(classifyQuality(input as number | null | undefined)).toBe('unknown');
  });
});
