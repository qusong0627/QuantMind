/**
 * 策略控制台「交易记录」行展示模型。
 *
 * 用户原话：「策略管理 交易记录写的很简单啊、名称都没有。」
 * 名称缺失的根因在后端（桥接写入不写 symbol_name，列表接口也不补），
 * 这里负责的是另一半——**一行要能回答机构交易台该回答的问题**：
 * 成交了多少（部分成交没有）、按什么价成交（委托价与成交均价差多少 =
 * 滑点直觉）、成交额与手续费多少、撤单为什么撤。
 */

import { describe, it, expect } from 'vitest';
import { orderRowView } from '../orderRowModel';
import type { Order } from '../../../../../../services/realTradingService';

const base = (patch: Partial<Order> = {}): Order => ({
  id: 1,
  order_id: 'o-1',
  symbol: '600176.SH',
  side: 'buy',
  order_type: 'limit',
  status: 'filled',
  quantity: 500,
  price: 18.21,
  order_value: 9105,
  filled_quantity: 500,
  average_price: 18.18,
  filled_value: 9090,
  created_at: '2026-09-20T14:30:00+08:00',
  filled_at: '2026-09-20T14:31:00+08:00',
  ...patch,
});

describe('orderRowView', () => {
  it('有名称时展示名称并保留代码；无名称时回落代码', () => {
    expect(orderRowView(base({ symbol_name: '中国巨石' })).name).toBe('中国巨石');
    expect(orderRowView(base({ symbol_name: '中国巨石' })).code).toBe('600176.SH');
    expect(orderRowView(base()).name).toBe('');
    expect(orderRowView(base()).code).toBe('600176.SH');
  });

  it('名称与代码相同时不当作名称（后端曾把 symbol 塞进 symbol_name）', () => {
    const view = orderRowView(base({ symbol_name: '600176.SH' }));
    expect(view.name).toBe('');
  });

  it('全成交：数量只写一个数，价格给出委托→成交', () => {
    const view = orderRowView(base());
    expect(view.qtyText).toBe('500 股');
    expect(view.partial).toBe(false);
    expect(view.priceText).toBe('18.21 → 18.18');
  });

  it('部分成交：数量显示「成交/委托」并标记 partial（机构台最关心的一格）', () => {
    const view = orderRowView(
      base({ status: 'partial_filled', quantity: 1000, filled_quantity: 300, average_price: 18.20 }),
    );
    expect(view.qtyText).toBe('300 / 1000 股');
    expect(view.partial).toBe(true);
  });

  it('未成交（已撤）：用委托价与委托数量，金额标为委托额', () => {
    const view = orderRowView(
      base({ status: 'cancelled', filled_quantity: 0, average_price: undefined, filled_value: undefined }),
    );
    expect(view.qtyText).toBe('500 股');
    expect(view.priceText).toBe('18.21');
    expect(view.amountLabel).toBe('委托额');
    expect(view.amountText).toBe('¥9,105.00');
  });

  it('成交后金额用成交额（filled_value），不是委托额', () => {
    const view = orderRowView(base());
    expect(view.amountLabel).toBe('成交额');
    expect(view.amountText).toBe('¥9,090.00');
  });

  it('手续费缺失时留空，不写 ¥0.00（0 与「没有这个数」必须分得开）', () => {
    expect(orderRowView(base()).feeText).toBe('');
    expect(orderRowView(base({ commission: 5.02 })).feeText).toBe('¥5.02');
  });

  it('状态文案分档：已成/部成/已撤/已拒绝/待成交', () => {
    expect(orderRowView(base()).statusLabel).toBe('已成');
    expect(orderRowView(base({ status: 'partial_filled' })).statusLabel).toBe('部成');
    expect(orderRowView(base({ status: 'cancelled' })).statusLabel).toBe('已撤');
    expect(orderRowView(base({ status: 'rejected' })).statusLabel).toBe('已拒绝');
    expect(orderRowView(base({ status: 'submitted' })).statusLabel).toBe('待成交');
  });

  it('撤单原因从 remarks 里取出来（后端把原因追加在备注里）', () => {
    const view = orderRowView(base({ status: 'cancelled', remarks: '[CANCELLED: 用户手动撤销]' }));
    expect(view.note).toBe('用户手动撤销');
  });

  it('普通备注原样保留', () => {
    expect(orderRowView(base({ remarks: '通达信桥委托' })).note).toBe('通达信桥委托');
  });

  it('时间：成交单给成交时间，未成交给委托时间，并标注是哪一种', () => {
    const filled = orderRowView(base());
    expect(filled.timeLabel).toBe('成交');
    expect(filled.timeText).toMatch(/^\d{2}-\d{2} \d{2}:\d{2}$/);

    const cancelled = orderRowView(base({ status: 'cancelled' }));
    expect(cancelled.timeLabel).toBe('委托');
  });

  it('缺字段不炸：空对象也能出一行', () => {
    const view = orderRowView({ id: 0, order_id: 'x', symbol: '000001.SZ', side: 'buy', order_type: 'market', status: 'new', quantity: 0, order_value: 0, filled_quantity: 0, created_at: '' } as Order);
    expect(view.code).toBe('000001.SZ');
    expect(view.amountText).toBe('');
    expect(view.timeText).toBe('—');
  });
});
