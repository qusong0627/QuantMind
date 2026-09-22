/**
 * 持仓监控渲染层：缺价行不得被画成「亏光」。
 *
 * 事故（2026-09-22 实盘持仓监控浮动盈亏 −84,778.90）：后端补价失败，落库的持仓行
 * 是 `price: 0, market_value: 0`。`positionMetrics` 那一层已把这类行标成
 * `priceMissing` 且不参与加减，但**合计是在本组件里做的** ——
 * `holdings.reduce((s, h) => s + h.profit, 0)` 只要还在用全量 holdings，
 * 十行「市值 0 − 成本」照样能凑出那串数字。utils 侧的绿测拦不住这个：
 * 它测的是 metrics，测不到这个 reduce。
 *
 * 同一条口径在本仓已有明文：研究评分「缺失一律 `—`，绝不显示成 0」。
 */

import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';

import { PositionVisualBoard } from '../PositionVisualBoard';
import type { NormalizedHolding, PositionSummary } from '../../utils/positionMetrics';

vi.mock('echarts-for-react', () => ({
  default: () => <div data-testid="echarts-mock" />,
}));

// 卖出预检面板会拉起 stock-terminal 的整条依赖；本文件只测展示口径
vi.mock('../../../../features/stock-terminal/components/PushConfirmPanel', () => ({
  PushConfirmPanel: () => null,
}));

afterEach(cleanup);

/** 明细行：货架马赛克块与「卖出」按钮的 title 也带股票名，只有明细行的 title
 *  是 `名称（代码） 持仓 N 股 · 市值 …` 这个形状（马赛克块在 `）` 后换行）。 */
const rowOf = (name: string) =>
  screen.getByTitle(new RegExp(`^${name}（[^）]+） 持仓 \\d+ 股 · 市值`));

/** 补价失败的行：有股数、有成本价，现价与市值为 0（后端原样落库） */
const missing = (over: Partial<NormalizedHolding> = {}): NormalizedHolding => ({
  code: 'SZ000028',
  name: '国药一致',
  shares: 200,
  cost: 19.886,
  current: 0,
  profit: 0,
  profitPercent: 0,
  value: 0,
  priceMissing: true,
  ...over,
});

const priced = (over: Partial<NormalizedHolding> = {}): NormalizedHolding => ({
  code: 'SH600036',
  name: '招商银行',
  shares: 200,
  cost: 19.886,
  current: 20.03,
  profit: 28.8,
  profitPercent: 0.724,
  value: 4006,
  priceMissing: false,
  ...over,
});

const summaryOf = (positionValue: number): PositionSummary => ({
  totalAsset: 920498.86,
  cashValue: 834408.86,
  positionValue,
  positionRatio: 9.35,
  cashRatio: 90.65,
});

describe('PositionVisualBoard · 缺价行', () => {
  it('全部缺价时浮动盈亏出 —，既不报 0.00 也不报负数', () => {
    render(<PositionVisualBoard holdings={[missing()]} summary={summaryOf(0)} />);

    const kpi = screen.getByText('浮动盈亏');

    expect(kpi).toHaveTextContent('浮动盈亏 —');
    expect(kpi).not.toHaveTextContent('0.00');
    // 旧口径：市值 0 − 成本 19.886×200 = −3,977.20，十行就是用户看到的那串数字
    expect(kpi).not.toHaveTextContent('-');
  });

  it('缺价行不计入盈利/亏损家数，另立「缺价」计数', () => {
    render(
      <PositionVisualBoard holdings={[missing(), priced()]} summary={summaryOf(4006)} />,
    );

    expect(screen.getByText('盈利')).toHaveTextContent('盈利 1');
    expect(screen.getByText('亏损')).toHaveTextContent('亏损 0');
    expect(screen.getByText('缺价')).toHaveTextContent('缺价 1');
  });

  it('浮动盈亏只合计可定价的行，缺价行不参与', () => {
    render(
      <PositionVisualBoard holdings={[missing(), priced()]} summary={summaryOf(4006)} />,
    );

    const kpi = screen.getByText('浮动盈亏');

    expect(kpi).toHaveTextContent('浮动盈亏 +28.80');
  });

  it('缺价行：现价 / 市值占比 / 盈亏一律出 —，成本照常显示', () => {
    render(<PositionVisualBoard holdings={[missing()]} summary={summaryOf(0)} />);

    const row = rowOf('国药一致');

    expect(row).toHaveTextContent('成本 19.89');
    expect(row).toHaveTextContent('缺现价');
    // 占比 / 现价 / 盈亏金额 三处 `—`
    expect((row.textContent?.match(/—/g) ?? []).length).toBe(3);
    // 旧口径的两个特征：现价画成 ¥0.00、盈亏画成 −3,977.20 与 −100.00%
    expect(row).not.toHaveTextContent('0.00');
    expect(row).not.toHaveTextContent('100.00%');
    expect(row).not.toHaveTextContent('-3,977.20');
  });

  it('可定价的行不受影响，照常出价与盈亏', () => {
    render(<PositionVisualBoard holdings={[priced()]} summary={summaryOf(4006)} />);

    const row = rowOf('招商银行');

    expect(row).toHaveTextContent('20.03');
    expect(row).toHaveTextContent('+28.80');
    expect(row).toHaveTextContent('+0.72%');
    expect(screen.queryByText('缺价')).toBeNull();
  });
});
