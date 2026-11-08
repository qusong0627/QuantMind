/**
 * 热力图切换动画契约：数据/日期/颜色变化时复用同一 ECharts 实例，
 * 只做 setOption 合并更新，不能 dispose+init 整图刷新。
 */
import { render } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import React from 'react';

const chart = {
  setOption: vi.fn(),
  on: vi.fn(),
  off: vi.fn(),
  resize: vi.fn(),
  dispose: vi.fn(),
  getModel: vi.fn(() => undefined),
};
vi.mock('echarts', () => ({ init: vi.fn(() => chart) }));

import * as echarts from 'echarts';
import { SectorHeatmapChart } from '../../market-analysis-shared/SectorHeatmapChart';

class RO {
  observe() {}
  disconnect() {}
}

const dataA = [
  { name: '半导体', value: 100, pct_change: 1.2 },
  { name: '银行', value: 80, pct_change: -0.3 },
];

describe('SectorHeatmapChart · 切换动画', () => {
  afterEach(() => {
    vi.clearAllMocks();
    // @ts-expect-error test polyfill
    delete global.ResizeObserver;
  });

  it('首屏空数据后到达：迟到初始化也要挂上 click 处理器', () => {
    // @ts-expect-error test polyfill
    global.ResizeObserver = RO;
    const { rerender } = render(<SectorHeatmapChart data={[]} renderKey="empty" />);
    expect(chart.on).not.toHaveBeenCalled();
    rerender(<SectorHeatmapChart data={dataA} renderKey="loaded" />);
    expect((echarts.init as unknown as ReturnType<typeof vi.fn>).mock.calls.length).toBe(1);
    const clickCalls = chart.on.mock.calls.filter((c) => c[0] === 'click');
    expect(clickCalls.length).toBe(1);
  });

  it('数据更新复用实例，使用 notMerge:false 合并补间', () => {
    // @ts-expect-error test polyfill
    global.ResizeObserver = RO;
    const { rerender } = render(<SectorHeatmapChart data={dataA} renderKey="a" />);
    expect(chart.dispose).not.toHaveBeenCalled();
    expect(chart.setOption).toHaveBeenCalled();
    const initCalls = (echarts.init as unknown as ReturnType<typeof vi.fn>).mock.calls.length;
    expect(initCalls).toBe(1);

    rerender(
      <SectorHeatmapChart
        data={[{ name: '半导体', value: 100, pct_change: -2.1 }, { name: '银行', value: 80, pct_change: 0.8 }]}
        renderKey="b"
      />,
    );
    expect((echarts.init as unknown as ReturnType<typeof vi.fn>).mock.calls.length).toBe(1);
    expect(chart.dispose).not.toHaveBeenCalled();
    const mergeCalls = chart.setOption.mock.calls.filter(
      (c) => c[1] && (c[1] as { notMerge?: boolean }).notMerge === false,
    );
    expect(mergeCalls.length).toBeGreaterThan(0);
    expect((mergeCalls[0][1] as { replaceMerge?: string[] }).replaceMerge).toEqual(['series']);
  });

  it('标签字色随底色：浅色块切深字、深色块保持白字', () => {
    // @ts-expect-error test polyfill
    global.ResizeObserver = RO;
    const data = [
      { name: '电池', value: 100, pct_change: 0.5 },
      { name: '电力', value: 80, pct_change: 3 },
    ];
    render(
      <SectorHeatmapChart
        data={data}
        renderKey="contrast"
        colorOf={(item) => (item.name === '电池' ? '#cbd5e1' : '#dc2626')}
      />,
    );

    const dataItems = chart.setOption.mock.calls
      .flatMap((c) => {
        const series = (c[0] as { series?: Array<{ data?: Array<Record<string, unknown>> }> }).series;
        return Array.isArray(series) ? series.flatMap((s) => s.data ?? []) : [];
      })
      .filter((d) => d.name === '电池' || d.name === '电力') as Array<{
      name: string;
      label: { color: string; rich: Record<string, { color: string }> };
    }>;

    const light = dataItems.find((d) => d.name === '电池')!;
    const dark = dataItems.find((d) => d.name === '电力')!;
    expect(light.label.color).toBe('#0f172a');
    expect(light.label.rich.name.color).toBe('#0f172a');
    expect(dark.label.color).toBe('#ffffff');
    expect(dark.label.rich.pct.color).toBe('#ffffff');
  });
});
