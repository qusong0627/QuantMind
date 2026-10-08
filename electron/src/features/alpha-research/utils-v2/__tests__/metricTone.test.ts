/**
 * metricTone / metricToneClass —— A 股口径红涨绿跌的唯一实现。
 *
 * 用户实测反馈「数据指标回测的也要显示正确」：有方向的量（IC/收益族）
 * 必须按 A 股习惯 **正=红、负=绿**（不是美股绿涨）。
 * `formatMetricValue`（缺失→`—`、真 0 显示 0）的边界另由
 * services-v2/__tests__/metricRegistry.test.ts 钉死，这里只测着色。
 */
import { describe, test, expect } from 'vitest';
import { metricTone, metricToneClass } from '../index';

describe('metricTone：正 up / 负 down / 零与缺失 flat', () => {
  test('正数 → up（红），负数 → down（绿）', () => {
    expect(metricTone(0.0312)).toBe('up');
    expect(metricTone(-0.004)).toBe('down');
    expect(metricTone(1.554)).toBe('up');
  });

  test('真实的 0 是 flat——不是 up 也不是 down', () => {
    expect(metricTone(0)).toBe('flat');
    expect(metricTone(-0)).toBe('flat');
  });

  test('null / undefined / NaN / Infinity 一律 flat（缺失不着色，显「—」的地方不加色）', () => {
    expect(metricTone(null)).toBe('flat');
    expect(metricTone(undefined)).toBe('flat');
    expect(metricTone(NaN)).toBe('flat');
    expect(metricTone(Infinity)).toBe('flat');
    expect(metricTone(-Infinity)).toBe('flat');
  });
});

describe('metricToneClass：A股红涨绿跌的文本色类', () => {
  test('up → text-rose-500（红=涨/做多方向）', () => {
    expect(metricToneClass(0.02)).toBe('text-rose-500');
  });

  test('down → text-emerald-500（绿=跌）', () => {
    expect(metricToneClass(-0.02)).toBe('text-emerald-500');
  });

  test('flat（0 / 缺失）→ 空串，不加任何颜色', () => {
    expect(metricToneClass(0)).toBe('');
    expect(metricToneClass(undefined)).toBe('');
    expect(metricToneClass(null)).toBe('');
    expect(metricToneClass(NaN)).toBe('');
  });
});
