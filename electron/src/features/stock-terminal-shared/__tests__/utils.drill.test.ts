import { describe, expect, it } from 'vitest';
import { signalPointDrillEntries, tradeMarkerDrillEntries } from '../utils';

describe('tradeMarkerDrillEntries（T-FE-08 买卖点下钻）', () => {
  it('完整字段 → 条目齐备（方向/价/量/金额/费用/理由/订单号 + 来源）', () => {
    const entries = tradeMarkerDrillEntries({
      date: '2026-09-10',
      side: 'buy',
      price: 41.1,
      shares: 100,
      reason: '调仓买入: 当前0 → 目标100',
      order_id: 'a1b2c3',
      amount: 4110,
      fee: 5,
    });
    const byLabel = Object.fromEntries(entries.map((e) => [e.label, e]));
    expect(byLabel['方向'].value).toBe('买入');
    expect(byLabel['成交价'].value).toBe('41.10');
    expect(byLabel['成交金额'].value).toBe('4110.00');
    expect(byLabel['费用'].source).toContain('total_fee');
    expect(byLabel['理由（下单备注）'].value).toContain('调仓买入');
    expect(byLabel['订单号'].value).toBe('a1b2c3');
  });

  it('缺失字段如实「—」并给提示（不推断）', () => {
    const entries = tradeMarkerDrillEntries({ date: '2026-09-10', side: 'sell', price: 10, shares: 200 });
    const byLabel = Object.fromEntries(entries.map((e) => [e.label, e]));
    expect(byLabel['方向'].value).toBe('卖出');
    expect(byLabel['成交金额'].value).toBe('—');
    expect(byLabel['理由（下单备注）'].value).toBe('—');
    expect(byLabel['理由（下单备注）'].hint).toContain('未带备注');
    expect(byLabel['订单号'].value).toBe('—');
  });
});

describe('signalPointDrillEntries（信号点下钻）', () => {
  it('位置中性化 + 分数来源标注（同源副图口径）', () => {
    const buy = signalPointDrillEntries({ date: '2026-09-15', side: 'BUY', fusion: 0.011234 });
    const byLabel = Object.fromEntries(buy.map((e) => [e.label, e]));
    // 展示面只说位置，不说「买入信号」——那是替用户做决定
    expect(byLabel['相对位置'].value).toBe('靠前');
    expect(byLabel['相对位置'].hint).toBeTruthy();
    expect(byLabel['推理分数'].value).toBe('0.011234');
    expect(byLabel['推理分数'].source).toContain('engine_signal_scores');

    const sell = signalPointDrillEntries({ date: '2026-09-15', side: 'SELL', fusion: null });
    const sellByLabel = Object.fromEntries(sell.map((e) => [e.label, e]));
    expect(sellByLabel['相对位置'].value).toBe('靠后');
    expect(sellByLabel['推理分数'].value).toBe('—');
  });

  it('认不出的判定显示 —，不硬套成某个位置', () => {
    const odd = signalPointDrillEntries({ date: '2026-09-15', side: 'STRONG_BUY', fusion: 1 });
    expect(Object.fromEntries(odd.map((e) => [e.label, e]))['相对位置'].value).toBe('—');
  });
});
