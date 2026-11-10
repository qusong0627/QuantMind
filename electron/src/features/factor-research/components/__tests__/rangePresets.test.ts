/**
 * 因子研究时间窗预置 —— 2016–2026 主库上线后，年份档必须覆盖全窗口，
 * 且「时代」快捷档只在有交集时出现（不渲染点了必然空的按钮）。
 */
import { describe, expect, test } from 'vitest';
import { rangePresets } from '../common';

describe('rangePresets：全窗口年份与时代档', () => {
  test('2016–2026 主库：11 个年份 + 三个时代档', () => {
    const presets = rangePresets(['2016-07-29', '2026-11-05']);
    const labels = presets.map((p) => p.label);
    expect(labels[0]).toBe('全部');
    expect(labels).toContain('2016–2019');
    expect(labels).toContain('2020–2024');
    expect(labels).toContain('2025–今');
    expect(labels).toContain('2016');
    expect(labels).toContain('2026');
    expect(labels).not.toContain('2015');
    expect(presets.at(-1)?.label).toBe('自定义');

    const era = presets.find((p) => p.key === 'era2016');
    expect(era).toEqual({ key: 'era2016', label: '2016–2019', start: '2016-01', end: '2019-12' });
  });

  test('窗口只覆盖 2021–2023：不渲染不相交的时代档', () => {
    const presets = rangePresets(['2021-01-01', '2023-12-31']);
    const keys = presets.map((p) => p.key);
    expect(keys).not.toContain('era2016');
    expect(keys).toContain('era2020');
    expect(keys).not.toContain('era2025');
    expect(keys).toContain('y2021');
    expect(keys).toContain('y2023');
    expect(keys).not.toContain('y2020');
  });

  test('没有窗口元信息：至少保留全部/自定义', () => {
    expect(rangePresets(undefined).map((p) => p.key)).toEqual(['all', 'custom']);
  });
});
