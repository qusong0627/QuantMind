import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { getChartOption } from '../chartOptions';

describe('chartOptions tradeCount', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-03-21T12:00:00+08:00'));
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('should render the given 7 trading days with integer y-axis steps', () => {
    // 「最近 7 个交易日」的切片由 useIntelligenceCharts 负责（该 Hook 的测试已覆盖
    // data.tradeCount 的长度与末项），getChartOption 只负责把传入的点渲染成图。
    // 因此这里直接喂 Hook 会产出的那 7 个点（03-15..03-21，值 4..10）。
    const points = Array.from({ length: 7 }, (_, index) => ({
      timestamp: new Date(`2026-03-${String(index + 15).padStart(2, '0')}T00:00:00+08:00`).toISOString(),
      value: index + 4,
    }));

    const option = getChartOption('tradeCount', points) as any;

    expect(option.title.text).toBe('近7日交易次数');
    expect(option.xAxis.data).toHaveLength(7);
    expect(option.series[0].data).toEqual([4, 5, 6, 7, 8, 9, 10]);
    expect(option.yAxis.min).toBe(0);
    expect(option.yAxis.max).toBe(10);
    // 纵轴刻意只画一条刻度线（splitNumber: 1 且 interval = max），
    // 避免小柱状图出现密集网格；因此 interval 等于 max，
    // 而不是「约 5 档」的圆整步长（getTradeCountInterval 只用于算 axisMax）。
    expect(option.yAxis.interval).toBe(option.yAxis.max);
  });
});
