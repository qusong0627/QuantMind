import { describe, expect, it } from 'vitest';
import {
  beijingPartsOf,
  fmtBeijingClock,
  fmtBeijingDateTime,
  isAwareTimestamp,
  isCnTradingHours,
  naivePartsOf,
} from '../timeBeijing';

describe('timeBeijing', () => {
  it('isAwareTimestamp：Z / ±HH:MM / ±HHMM / PG 文本 ±HH 为 aware；naive 与坏值不是', () => {
    expect(isAwareTimestamp('2026-10-10T01:30:05Z')).toBe(true);
    expect(isAwareTimestamp('2026-10-10T09:30:05+08:00')).toBe(true);
    expect(isAwareTimestamp('2026-10-10T09:30:05+0800')).toBe(true);
    expect(isAwareTimestamp('2026-10-10 09:30:05.123+08')).toBe(true); // PG timestamptz::text 形态
    expect(isAwareTimestamp('2026-10-10 09:30:05')).toBe(false);
    expect(isAwareTimestamp('2026-10-10')).toBe(false);
    expect(isAwareTimestamp('garbage')).toBe(false);
  });

  it('beijingPartsOf：aware 一律换算北京墙钟（设备时区无关）', () => {
    // 01:30:05 UTC = 北京 09:30:05
    expect(beijingPartsOf('2026-10-10T01:30:05Z')).toEqual({ y: 2026, m: 10, d: 10, hh: 9, mm: 30, ss: 5 });
    // +08 形态本身就是北京钟面
    expect(beijingPartsOf('2026-10-10 09:30:05+08')).toEqual({ y: 2026, m: 10, d: 10, hh: 9, mm: 30, ss: 5 });
    // epoch 秒 / 毫秒自适应：1789002005 = 2026-09-10T...（秒）
    expect(beijingPartsOf(1789002005)?.ss).toBe(5);
    expect(beijingPartsOf(1789002005000)).toEqual(beijingPartsOf(1789002005));
    // naive / 坏值 → null（调用方走原样分支）
    expect(beijingPartsOf('2026-10-10 09:30:05')).toBeNull();
    expect(beijingPartsOf('garbage')).toBeNull();
    expect(beijingPartsOf(null)).toBeNull();
    expect(beijingPartsOf('')).toBeNull();
  });

  it('naivePartsOf：取墙钟字段，无秒补 0；非 naive 形态 → null', () => {
    expect(naivePartsOf('2026-10-10 09:30:05')).toEqual({ y: 2026, m: 10, d: 10, hh: 9, mm: 30, ss: 5 });
    expect(naivePartsOf('2026-10-10T09:30')).toEqual({ y: 2026, m: 10, d: 10, hh: 9, mm: 30, ss: 0 });
    expect(naivePartsOf('garbage')).toBeNull();
    expect(naivePartsOf(undefined)).toBeNull();
  });

  it('fmtBeijingDateTime：aware 换算北京；naive 原样；日期串/坏值原样；空值 —', () => {
    expect(fmtBeijingDateTime('2026-10-10T01:30:05Z')).toBe('2026-10-10 09:30:05');
    expect(fmtBeijingDateTime('2026-10-10T01:30:05Z', { withSeconds: false })).toBe('2026-10-10 09:30');
    // PG 文本形态（旧裸截断在非 +08 会话下会把 UTC 钟面当北京）
    expect(fmtBeijingDateTime('2026-10-10 09:30:05.123+08')).toBe('2026-10-10 09:30:05');
    // naive 原样（分钟口径截到分、秒口径补 :00）
    expect(fmtBeijingDateTime('2026-10-10 09:30:05', { withSeconds: false })).toBe('2026-10-10 09:30');
    expect(fmtBeijingDateTime('2026-10-10 09:30', { withSeconds: true })).toBe('2026-10-10 09:30:00');
    expect(fmtBeijingDateTime('2026-10-10')).toBe('2026-10-10');
    expect(fmtBeijingDateTime('garbage')).toBe('garbage');
    expect(fmtBeijingDateTime(null)).toBe('—');
    expect(fmtBeijingDateTime('')).toBe('—');
  });

  it('fmtBeijingClock：只出时钟段（aware 换算 / naive 原样）', () => {
    expect(fmtBeijingClock('2026-10-10T01:30:05Z')).toBe('09:30:05');
    expect(fmtBeijingClock('2026-10-10 09:30:05')).toBe('09:30:05');
    expect(fmtBeijingClock('2026-10-10 09:30:05', { withSeconds: false })).toBe('09:30');
  });

  it('isCnTradingHours：按北京墙钟判时段（周末 / 午休 / 边界都不放行）', () => {
    // 2026-10-12 是周一；02:00Z = 北京 10:00 盘中
    expect(isCnTradingHours(new Date('2026-10-12T02:00:00Z'))).toBe(true);
    expect(isCnTradingHours(new Date('2026-10-12T04:00:00Z'))).toBe(false); // 北京 12:00 午休
    expect(isCnTradingHours(new Date('2026-10-12T05:00:00Z'))).toBe(true); // 北京 13:00 午后
    expect(isCnTradingHours(new Date('2026-10-12T01:25:00Z'))).toBe(true); // 北京 09:25 含集合竞价尾段
    expect(isCnTradingHours(new Date('2026-10-12T01:24:00Z'))).toBe(false);
    expect(isCnTradingHours(new Date('2026-10-12T03:35:00Z'))).toBe(true); // 北京 11:35 上午收
    expect(isCnTradingHours(new Date('2026-10-12T03:36:00Z'))).toBe(false);
    expect(isCnTradingHours(new Date('2026-10-12T07:05:00Z'))).toBe(true); // 北京 15:05 全天收
    expect(isCnTradingHours(new Date('2026-10-12T07:06:00Z'))).toBe(false);
    // 2026-10-11 是周日：北京盘中时刻也不放行
    expect(isCnTradingHours(new Date('2026-10-11T02:00:00Z'))).toBe(false);
  });
});
