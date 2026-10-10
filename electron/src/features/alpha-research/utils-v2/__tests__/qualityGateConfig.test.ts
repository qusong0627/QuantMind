/**
 * 入库闸门开关的本地读取（T-MV-05）：语义只表达「关」。
 *
 * - 未存过 / JSON 损坏 → true（默认开；调用方此时不下发 quality_gate_mode，
 *   生效模式由后端 env 兜底——读取层绝不替后端做 soft/hard 的抉择）；
 * - 显式存 false → false（派发时携带 quality_gate_mode=off）；
 * - 非布尔脏值（字符串 "false" 等）→ true（守住布尔契约，不猜）。
 */
import { beforeEach, describe, expect, test } from 'vitest';

import { getStoredQualityGateEnabled } from '../miningDirections';

describe('getStoredQualityGateEnabled', () => {
  beforeEach(() => {
    localStorage.clear();
  });

  test('未存过配置 → 默认开', () => {
    expect(getStoredQualityGateEnabled()).toBe(true);
  });

  test('显式 false → 关；true → 开', () => {
    localStorage.setItem(
      'quantaalpha_config',
      JSON.stringify({ qualityGateEnabled: false }),
    );
    expect(getStoredQualityGateEnabled()).toBe(false);

    localStorage.setItem(
      'quantaalpha_config',
      JSON.stringify({ qualityGateEnabled: true }),
    );
    expect(getStoredQualityGateEnabled()).toBe(true);
  });

  test('配置损坏或字段脏值 → 回默认开（不猜）', () => {
    localStorage.setItem('quantaalpha_config', '{not json');
    expect(getStoredQualityGateEnabled()).toBe(true);

    localStorage.setItem(
      'quantaalpha_config',
      JSON.stringify({ qualityGateEnabled: 'false' }),
    );
    expect(getStoredQualityGateEnabled()).toBe(true);
  });
});
