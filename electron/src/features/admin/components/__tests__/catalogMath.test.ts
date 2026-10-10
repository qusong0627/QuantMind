/**
 * 训练目录纯计算 + 统计格式化单测（2026-10-10）。
 *
 * 两条铁律值得单测锁死：
 * 1) 启用数口径 `enabled !== false`（与训练侧逐字同口径）——undefined 要算启用；
 * 2) 缺失一律「—」，null/undefined/NaN/Infinity 全都要挡住，绝不放行成 0。
 */
import { describe, test, expect } from 'vitest';

import {
  attachStatsToRows,
  countEnabledFeatures,
  diffEnabledFeatures,
} from '../quantdb/catalogMath';
import {
  MISSING,
  fmtSigned,
  fmtNum,
  fmtPct,
  fmtInt,
  fmtWindowCoverage,
} from '../quantdb/statFormat';

const catalog = (features: Array<Record<string, unknown>>) => ({
  categories: [{ category_id: 'x', features }],
});

describe('countEnabledFeatures', () => {
  test('统计 enabled !== false 的特征（undefined 算启用，与训练侧同口径）', () => {
    // Arrange
    const payload = catalog([
      { key: 'a', enabled: true },
      { key: 'b', enabled: false },
      { key: 'c' }, // 老版本不发 enabled → 训练侧按启用处理
    ]);

    // Act
    const count = countEnabledFeatures(payload);

    // Assert
    expect(count).toBe(2);
  });

  test('空目录/undefined 返回 0', () => {
    expect(countEnabledFeatures(undefined)).toBe(0);
    expect(countEnabledFeatures({ categories: [] })).toBe(0);
  });
});

describe('diffEnabledFeatures', () => {
  test('没有线上版本时返回 null（首次发布不显示增减）', () => {
    expect(diffEnabledFeatures(catalog([{ key: 'a', enabled: true }]), null)).toBeNull();
  });

  test('按逻辑因子 ID 逐键比对启用增减', () => {
    // Arrange：线上 a、b 启用；草稿 a 仍启用、b 被停用、新增 c 启用、d 停用不算
    const published = catalog([
      { key: 'a', enabled: true },
      { key: 'b', enabled: true },
    ]);
    const draft = catalog([
      { key: 'a', enabled: true },
      { key: 'b', enabled: false },
      { key: 'c', enabled: true },
      { key: 'd', enabled: false },
    ]);

    // Act
    const delta = diffEnabledFeatures(draft, published);

    // Assert
    expect(delta).toEqual({ added: 1, removed: 1 });
  });
});

describe('attachStatsToRows（物理列名优先 + 兜底守卫）', () => {
  const stat = (ic: number) => ({ source: 'report' as const, ic_mean: ic });

  test('物理列名直接命中；stats 为 null/undefined 时全部「—」', () => {
    // Arrange
    const rows = [{ source_column: 'VOL20', factor: 'VOL20' }];
    const stats = { VOL20: stat(0.05) };

    // Act
    const attached = attachStatsToRows(rows, stats);
    const none = attachStatsToRows(rows, undefined);

    // Assert
    expect(attached[0].stat?.ic_mean).toBe(0.05);
    expect(none[0].stat).toBeNull();
  });

  test('逻辑因子 ID 二级兜底：列名无统计但 key 命中时挂上', () => {
    // 草稿里逻辑 ID（key）与物理列不同名：stats 以 key 为键的条目归属该行
    const rows = [{ source_column: 'vol_20_raw', factor: 'VOL20' }];
    const stats = { VOL20: stat(0.07) };

    const attached = attachStatsToRows(rows, stats);

    expect(attached[0].stat?.ic_mean).toBe(0.07);
  });

  test('撞列守卫一：key 是另一行的物理列名时，兜底让位给那行的直接命中', () => {
    // Arrange：两行——行 A 列名 vol_20_raw、key VOL20；行 B 列名 VOL20（直接命中）
    const rows = [
      { source_column: 'vol_20_raw', factor: 'VOL20' },
      { source_column: 'VOL20', factor: 'VOL20' },
    ];
    const stats = { VOL20: stat(0.09) };

    // Act
    const attached = attachStatsToRows(rows, stats);

    // Assert：B 直接命中；A 不抢同一条（宁缺毋滥）
    expect(attached[0].stat).toBeNull();
    expect(attached[1].stat?.ic_mean).toBe(0.09);
  });

  test('撞列守卫二：同一 key 只能被兜底认领一次', () => {
    // 两行 key 相同、列名都无统计：只有第一行（排序靠前者）获得兜底
    const rows = [
      { source_column: 'raw_a', factor: 'SAME' },
      { source_column: 'raw_b', factor: 'SAME' },
    ];
    const stats = { SAME: stat(0.11) };

    const attached = attachStatsToRows(rows, stats);

    expect(attached[0].stat?.ic_mean).toBe(0.11);
    expect(attached[1].stat).toBeNull();
  });

  test('直接命中值为 null（后端明确无数据）时不启用兜底', () => {
    // stats[列名] === null 是「该列无统计」的明确信号；key 恰好等于列名，
    // 不应借兜底路径再查一次自己
    const rows = [{ source_column: 'X', factor: 'X' }];
    const stats = { X: null } as Record<string, any>;

    const attached = attachStatsToRows(rows, stats);

    expect(attached[0].stat).toBeNull();
  });
});

describe('statFormat 缺失语义', () => {
  test('null/undefined/NaN/Infinity 一律「—」，绝不显示 0', () => {
    for (const bad of [null, undefined, NaN, Infinity, -Infinity]) {
      expect(fmtSigned(bad)).toBe(MISSING);
      expect(fmtNum(bad)).toBe(MISSING);
      expect(fmtPct(bad)).toBe(MISSING);
      expect(fmtInt(bad)).toBe(MISSING);
      expect(fmtWindowCoverage(bad, 100)).toBe(MISSING);
      expect(fmtWindowCoverage(50, bad)).toBe(MISSING);
    }
  });

  test('真值 0 如实显示 0（与缺失区分）', () => {
    expect(fmtSigned(0, 3)).toBe('0.000');
    expect(fmtNum(0)).toBe('0.00');
    expect(fmtPct(0)).toBe('0.0%');
    expect(fmtInt(0)).toBe('0');
  });

  test('格式：带符号 IC / 百分比 / 千分位整数', () => {
    expect(fmtSigned(-0.08345)).toBe('-0.083');
    expect(fmtSigned(0.0123)).toBe('+0.012');
    expect(fmtPct(0.5576)).toBe('55.8%');
    expect(fmtInt(4204.9)).toBe('4,205');
  });

  test('窗口覆盖 = 有效天数/评估期总天数，分母为 0 时「—」', () => {
    expect(fmtWindowCoverage(2355, 2359)).toBe('99.8%');
    expect(fmtWindowCoverage(34, 2359)).toBe('1.4%');
    expect(fmtWindowCoverage(50, 0)).toBe(MISSING);
  });
});
