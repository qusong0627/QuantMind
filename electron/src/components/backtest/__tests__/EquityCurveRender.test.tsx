/**
 * T-FE-09 组件级回归：EquityCurveChart 必须走 lightweight-charts **v5** API。
 *
 * 历史事故：组件按 v4 的 `chart.addLineSeries` 编写，库升级 v5 后方法移除，
 * `as any` 强转吞错 → 曲线长期静默空白。本测试以 mock 断言 v5 调用路径
 * （addSeries(LineSeries/AreaSeries, …, paneIndex)），v4 写法到此即红。
 */

import { render, screen } from '@testing-library/react';
import { Provider } from 'react-redux';
import store from '../../../store';
import { describe, expect, it, vi, beforeEach } from 'vitest';

const addSeries = vi.fn();
const setHeight = vi.fn();
const fitContent = vi.fn();
const remove = vi.fn();
const applyOptions = vi.fn();

vi.mock('lightweight-charts', () => ({
  createChart: vi.fn(() => ({
    addSeries,
    panes: () => [{ setHeight }, {}],
    remove,
    applyOptions,
    timeScale: () => ({ fitContent }),
    priceScale: () => ({ applyOptions }),
  })),
  LineSeries: 'LineSeries',
  AreaSeries: 'AreaSeries',
}));

import { EquityCurveChart } from '../EquityCurve';

const seriesStub = () => ({ setData: vi.fn() });

describe('EquityCurveChart（v5 API + 水下 + 统计）', () => {
  beforeEach(() => {
    addSeries.mockReset();
    addSeries.mockImplementation(() => seriesStub());
  });

  it('使用 v5 addSeries(LineSeries/AreaSeries)；回撤面积进 pane 1', () => {
    const values = [100, 110, 105, 120];
    render(
      <Provider store={store}>
        <EquityCurveChart
          equityCurve={{
            timestamps: values.map((_, i) => 1_700_000_000_000 + i * 86_400_000),
            values,
            drawdowns: [],
            returns: [],
          }}
          initialCapital={100}
          benchmarkData={[{ timestamp: 1_700_000_000_000, value: 100 }, { timestamp: 1_700_086_400_000, value: 104 }]}
        />
      </Provider>
    );

    const seriesTypes = addSeries.mock.calls.map((c) => c[0]);
    expect(seriesTypes).toContain('LineSeries');
    expect(seriesTypes).toContain('AreaSeries');
    // 水下图必须落在第二 pane（v5 三方签名：definition, options, paneIndex）
    const ddCall = addSeries.mock.calls.find((c) => c[0] === 'AreaSeries');
    expect(ddCall?.[2]).toBe(1);
    // 图表侧零 v4 幽灵方法：mock 对象上不存在 addLineSeries，若组件仍调用会直接抛错（测试红）
  });

  it('统计 chips 渲染：区间收益/最大回撤解析值 + 缺失不硬算', () => {
    const values = [100, 120, 90, 110]; // 最大回撤 = 90/120-1 = -25%
    render(
      <Provider store={store}>
        <EquityCurveChart
          equityCurve={{
            timestamps: values.map((_, i) => 1_700_000_000_000 + i * 86_400_000),
            values,
            drawdowns: [],
            returns: [],
          }}
          initialCapital={100}
        />
      </Provider>
    );
    expect(screen.getByText('最大回撤')).toBeTruthy();
    expect(screen.getByText('-25.00%')).toBeTruthy();
    expect(screen.getByText('10.00%')).toBeTruthy(); // 区间收益 (110/100-1)
    // 无基准 → 不渲染超额 chip
    expect(screen.queryByText('超额（vs 基准）')).toBeNull();
  });
});
