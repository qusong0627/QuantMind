import { describe, it, expect } from 'vitest';
import {
  formatVolume,
  formatAmount,
  formatPrice,
  formatPercent,
  formatTime,
  formatDate,
  getRelativeTime,
  formatBackendTime,
  parseBackendTimestamp,
} from '../format';

describe('formatVolume', () => {
  it('应该格式化成交量（亿）', () => {
    expect(formatVolume(150000000)).toBe('1.50亿');
    expect(formatVolume(100000000)).toBe('1.00亿');
  });

  it('应该格式化成交量（万）', () => {
    expect(formatVolume(50000)).toBe('5.00万');
    expect(formatVolume(10000)).toBe('1.00万');
  });

  it('应该格式化成交量（个）', () => {
    expect(formatVolume(9999)).toBe('9999');
    expect(formatVolume(100)).toBe('100');
  });
});

describe('formatAmount', () => {
  it('应该格式化金额（亿）', () => {
    expect(formatAmount(150000000)).toBe('1.50亿');
  });

  it('应该格式化金额（万）', () => {
    expect(formatAmount(50000)).toBe('5.00万');
  });

  it('应该格式化金额（元）', () => {
    expect(formatAmount(9999)).toBe('9999.00');
    expect(formatAmount(100.5)).toBe('100.50');
  });
});

describe('formatPrice', () => {
  it('应该格式化价格（默认2位小数）', () => {
    expect(formatPrice(123.456)).toBe('123.46');
    expect(formatPrice(100)).toBe('100.00');
  });

  it('应该格式化价格（自定义小数位）', () => {
    expect(formatPrice(123.456, 3)).toBe('123.456');
    expect(formatPrice(100, 0)).toBe('100');
  });
});

describe('formatPercent', () => {
  it('应该格式化正百分比（带符号）', () => {
    expect(formatPercent(5.5)).toBe('+5.50%');
    expect(formatPercent(10)).toBe('+10.00%');
  });

  it('应该格式化负百分比', () => {
    expect(formatPercent(-3.5)).toBe('-3.50%');
  });

  it('应该格式化百分比（不带符号）', () => {
    expect(formatPercent(5.5, false)).toBe('5.50%');
    expect(formatPercent(-3.5, false)).toBe('-3.50%');
  });
});

describe('formatTime', () => {
  it('应该格式化时间', () => {
    const timestamp = new Date('2024-01-01T10:30:45').getTime();
    expect(formatTime(timestamp)).toBe('10:30:45');
  });

  it('应该补齐零', () => {
    const timestamp = new Date('2024-01-01T09:05:03').getTime();
    expect(formatTime(timestamp)).toBe('09:05:03');
  });
});

describe('formatDate', () => {
  it('应该格式化日期', () => {
    const timestamp = new Date('2024-01-15').getTime();
    expect(formatDate(timestamp)).toBe('2024-01-15');
  });

  it('应该补齐零', () => {
    const timestamp = new Date('2024-01-05').getTime();
    expect(formatDate(timestamp)).toBe('2024-01-05');
  });
});

describe('getRelativeTime', () => {
  it('应该返回秒前', () => {
    const timestamp = Date.now() - 30000; // 30秒前
    expect(getRelativeTime(timestamp)).toBe('30秒前');
  });

  it('应该返回分钟前', () => {
    const timestamp = Date.now() - 180000; // 3分钟前
    expect(getRelativeTime(timestamp)).toBe('3分钟前');
  });

  it('应该返回小时前', () => {
    const timestamp = Date.now() - 7200000; // 2小时前
    expect(getRelativeTime(timestamp)).toBe('2小时前');
  });

  it('应该返回天前', () => {
    const timestamp = Date.now() - 86400000 * 2; // 2天前
    expect(getRelativeTime(timestamp)).toBe('2天前');
  });
});

describe('backend time formatting', () => {
  it('should treat naive ISO timestamps as UTC and format them in Shanghai time', () => {
    expect(parseBackendTimestamp('2026-04-09T05:12:00')?.toISOString()).toBe('2026-04-09T05:12:00.000Z');
    expect(formatBackendTime('2026-04-09T05:12:00', { withSeconds: true })).toBe('13:12:00');
  });

  it('should treat Python microsecond naive ISO as UTC, not local time', () => {
    expect(parseBackendTimestamp('2026-09-14T07:54:25.123456')?.toISOString()).toBe(
      '2026-09-14T07:54:25.123Z',
    );
    expect(formatBackendTime('2026-09-14T07:54:25.123456', { withSeconds: true })).toBe(
      '15:54:25',
    );
  });

  it('should keep timezone-aware timestamps stable', () => {
    expect(parseBackendTimestamp('2026-04-09T05:12:00Z')?.toISOString()).toBe('2026-04-09T05:12:00.000Z');
    expect(formatBackendTime('2026-04-09T05:12:00Z', { withSeconds: true })).toBe('13:12:00');
  });
});
