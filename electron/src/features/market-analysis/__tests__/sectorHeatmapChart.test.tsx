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
});
