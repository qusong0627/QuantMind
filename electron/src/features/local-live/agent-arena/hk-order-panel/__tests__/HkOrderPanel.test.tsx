/**
 * HkOrderPanel 护栏（2026-10-08 港股富途恢复）。
 *
 * 本组件是「生成物外的本地组件」——不被 port-from-arena.mjs 重建，
 * 因此契约变更只能靠这些测试兜住。覆盖：
 *   1. 默认态：模拟环境 + 提交按钮随 env/方向换文案；
 *   2. 客户端校验先于网络：数量/价格不合法时**不许**发起 place 请求；
 *   3. 载荷逐字段：placeFutuOrder(env, {code,price,quantity,order_type,trd_side})；
 *   4. 实盘必须过 window.confirm（拒绝确认 = 不发请求）；
 *   5. 错误面文案：409 futu_unlock_required → 「实盘下单未解锁」；
 *   6. 「当前委托」：仅可撤状态给撤单按钮，confirm 后走 cancelFutuOrder；
 *   7. 纯函数 describeFutuError / validateOrder 的金样。
 *
 * 网络面全部 mock（arena/api/client 整模块）：组件里没有别的 IO。
 * usePolling 首轮有 2.5s 错峰相位 → 用假定时器推进（advanceTimersByTimeAsync
 * 会连同微任务一起 flush，axios 桩的 promise 能落到 state 上）。
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, cleanup, fireEvent, act, screen } from '@testing-library/react';
import type { FutuOrderRaw } from '../../arena/api/client';
import HkOrderPanel, { describeFutuError, validateOrder } from '../HkOrderPanel';

const placeFutuOrder = vi.fn();
const cancelFutuOrder = vi.fn();
const fetchFutuOrders = vi.fn();

vi.mock('../../arena/api/client', () => ({
  placeFutuOrder: (...args: unknown[]) => placeFutuOrder(...args),
  cancelFutuOrder: (...args: unknown[]) => cancelFutuOrder(...args),
  fetchFutuOrders: (...args: unknown[]) => fetchFutuOrders(...args),
}));

const order = (over: Partial<FutuOrderRaw> = {}): FutuOrderRaw => ({
  order_id: 'O1',
  code: '00700.HK',
  name: '腾讯控股',
  trd_side: 'BUY',
  order_type: 'NORMAL',
  order_status: 'SUBMITTED',
  qty: 100,
  price: 380,
  dealt_qty: 0,
  dealt_avg_price: 0,
  create_time: '2026-10-08 10:00:00',
  last_err_msg: '',
  ...over,
});

/** 推进 usePolling 首轮（相位 2.5s）+ flush 微任务。 */
const flushPoll = async () => {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(2600);
  });
};

/** 冲刷已 resolve 的 promise 链（假定时器下 findBy / waitFor 会死锁，一律用它 + getBy）。 */
const settle = async () => {
  await act(async () => {});
};

const fillOrder = (price = '380', qty = '100') => {
  fireEvent.change(screen.getByLabelText('委托价格'), { target: { value: price } });
  fireEvent.change(screen.getByLabelText('委托数量'), { target: { value: qty } });
};

describe('HkOrderPanel', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    placeFutuOrder.mockReset();
    cancelFutuOrder.mockReset();
    fetchFutuOrders.mockReset();
    fetchFutuOrders.mockResolvedValue([]);
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
  });

  it('默认模拟环境，提交按钮文案随环境/方向变化', async () => {
    render(<HkOrderPanel />);
    await flushPoll();
    expect(screen.getByRole('button', { name: '提交模拟买入' })).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('买卖方向'), { target: { value: 'SELL' } });
    fireEvent.change(screen.getByLabelText('下单环境'), { target: { value: 'REAL' } });
    expect(screen.getByRole('button', { name: '提交实盘卖出' })).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('实盘环境');
  });

  it('数量非法时不发请求，给出中文校验文案', async () => {
    render(<HkOrderPanel />);
    await flushPoll();
    fireEvent.change(screen.getByLabelText('委托价格'), { target: { value: '380' } });
    fireEvent.change(screen.getByLabelText('委托数量'), { target: { value: '0' } });
    fireEvent.click(screen.getByRole('button', { name: /提交/ }));
    await settle();
    expect(screen.getByRole('status')).toHaveTextContent('数量必须是正整数');
    expect(placeFutuOrder).not.toHaveBeenCalled();
  });

  it('限价单缺价格时不发请求', async () => {
    render(<HkOrderPanel />);
    await flushPoll();
    fireEvent.change(screen.getByLabelText('委托数量'), { target: { value: '100' } });
    fireEvent.click(screen.getByRole('button', { name: /提交/ }));
    await settle();
    expect(screen.getByRole('status')).toHaveTextContent('限价单必须填写大于 0 的价格');
    expect(placeFutuOrder).not.toHaveBeenCalled();
  });

  it('模拟限价买入：载荷逐字段 + 受理回执', async () => {
    placeFutuOrder.mockResolvedValue({
      success: true,
      order_id: 'O9',
      status: 'SUBMITTED',
      filled_quantity: 0,
      filled_price: 0,
      message: '',
    });
    render(<HkOrderPanel />);
    await flushPoll();
    fillOrder();
    fireEvent.click(screen.getByRole('button', { name: '提交模拟买入' }));
    await settle();

    expect(placeFutuOrder).toHaveBeenCalledWith('SIMULATE', {
      code: '00700.HK',
      price: 380,
      quantity: 100,
      order_type: 'NORMAL',
      trd_side: 'BUY',
    });
    expect(screen.getByRole('status')).toHaveTextContent('已受理：委托号 O9');
  });

  it('市价单价格字段禁用且载荷 price=0', async () => {
    placeFutuOrder.mockResolvedValue({
      success: true, order_id: 'M1', status: 'SUBMITTED',
      filled_quantity: 0, filled_price: 0, message: '',
    });
    render(<HkOrderPanel />);
    await flushPoll();
    fireEvent.change(screen.getByLabelText('委托类型'), { target: { value: 'MARKET' } });
    expect(screen.getByLabelText('委托价格')).toBeDisabled();
    fireEvent.change(screen.getByLabelText('委托数量'), { target: { value: '100' } });
    fireEvent.click(screen.getByRole('button', { name: '提交模拟买入' }));
    await settle();
    expect(placeFutuOrder).toHaveBeenCalledWith(
      'SIMULATE',
      expect.objectContaining({ order_type: 'MARKET', price: 0 }),
    );
  });

  it('实盘必须先过 window.confirm：取消确认则不发请求', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false);
    render(<HkOrderPanel />);
    await flushPoll();
    fireEvent.change(screen.getByLabelText('下单环境'), { target: { value: 'REAL' } });
    fillOrder();
    fireEvent.click(screen.getByRole('button', { name: '提交实盘买入' }));
    expect(confirmSpy).toHaveBeenCalledOnce();
    expect(placeFutuOrder).not.toHaveBeenCalled();
    confirmSpy.mockRestore();
  });

  it('实盘确认后按 REAL 提交', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    placeFutuOrder.mockResolvedValue({
      success: true, order_id: 'R1', status: 'SUBMITTED',
      filled_quantity: 0, filled_price: 0, message: '',
    });
    render(<HkOrderPanel />);
    await flushPoll();
    fireEvent.change(screen.getByLabelText('下单环境'), { target: { value: 'REAL' } });
    fillOrder();
    fireEvent.click(screen.getByRole('button', { name: '提交实盘买入' }));
    await settle();
    expect(placeFutuOrder).toHaveBeenCalledWith('REAL', expect.objectContaining({ code: '00700.HK' }));
    confirmSpy.mockRestore();
  });

  it('409 解锁缺失 → 「实盘下单未解锁」文案', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    placeFutuOrder.mockRejectedValue({ response: { status: 409, data: { detail: 'futu_unlock_required' } } });
    render(<HkOrderPanel />);
    await flushPoll();
    fireEvent.change(screen.getByLabelText('下单环境'), { target: { value: 'REAL' } });
    fillOrder();
    fireEvent.click(screen.getByRole('button', { name: '提交实盘买入' }));
    await settle();
    expect(screen.getByRole('status')).toHaveTextContent('实盘下单未解锁（需配置交易密码 MD5）');
    confirmSpy.mockRestore();
  });

  it('业务拒单（200 + success:false）原样展示拒因', async () => {
    placeFutuOrder.mockResolvedValue({
      success: false, order_id: '', status: '', filled_quantity: 0,
      filled_price: 0, message: '现金不足',
    });
    render(<HkOrderPanel />);
    await flushPoll();
    fillOrder();
    fireEvent.click(screen.getByRole('button', { name: '提交模拟买入' }));
    await settle();
    expect(screen.getByRole('status')).toHaveTextContent('拒单：现金不足');
  });

  it('当前委托：可撤状态给撤单按钮，confirm 后调用 cancelFutuOrder', async () => {
    fetchFutuOrders.mockResolvedValue([order()]);
    cancelFutuOrder.mockResolvedValue({ success: true, message: 'CANCELLED' });
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    render(<HkOrderPanel />);
    await flushPoll();

    expect(screen.getByText('腾讯控股')).toBeInTheDocument();
    expect(screen.getByText('已提交')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '撤单' }));
    await settle();
    expect(cancelFutuOrder).toHaveBeenCalledWith('SIMULATE', 'O1');
    expect(screen.getByRole('status')).toHaveTextContent('撤单已受理：O1');
    confirmSpy.mockRestore();
  });

  it('终态委托（全部成交）不给撤单按钮', async () => {
    fetchFutuOrders.mockResolvedValue([
      order({ order_id: 'O2', order_status: 'FILLED_ALL', dealt_qty: 100, dealt_avg_price: 379.5 }),
    ]);
    render(<HkOrderPanel />);
    await flushPoll();
    expect(screen.getByText('全部成交')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '撤单' })).not.toBeInTheDocument();
  });

  it('委托读取失败显式降级（不假装空列表）', async () => {
    fetchFutuOrders.mockRejectedValue(new Error('OpenD down'));
    render(<HkOrderPanel />);
    await flushPoll();
    await settle();
    expect(screen.getByText(/委托读取失败/)).toBeInTheDocument();
  });
});

describe('describeFutuError / validateOrder', () => {
  it.each([
    [403, undefined, '实盘交易未开启：后端闸门已关闭（real_trading_disabled）'],
    [409, 'futu_unlock_required', '实盘下单未解锁（需配置交易密码 MD5）'],
    [502, 'futu_bridge_error: x', '富途通道不可达（OpenD 未连接 / 未登录）'],
    [503, 'futu_unreachable: x', '富途整体不可达（OpenD 未连接）'],
    [400, 'order.quantity 必须大于 0', 'order.quantity 必须大于 0'],
  ])('HTTP %i → 中文文案', (status, detail, expected) => {
    expect(describeFutuError({ response: { status, data: { detail } } })).toBe(expected);
  });

  it('无响应体时退回 Error.message', () => {
    expect(describeFutuError(new Error('Network Error'))).toBe('Network Error');
    expect(describeFutuError(undefined)).toBe('请求失败（未知错误）');
  });

  it('validateOrder 边界', () => {
    expect(validateOrder({ code: '', quantity: 100, price: 1, orderType: 'NORMAL' })).toMatch(/代码/);
    expect(validateOrder({ code: '700', quantity: 0, price: 1, orderType: 'NORMAL' })).toMatch(/正整数/);
    expect(validateOrder({ code: '700', quantity: 1.5, price: 1, orderType: 'NORMAL' })).toMatch(/正整数/);
    expect(validateOrder({ code: '700', quantity: 100, price: 0, orderType: 'NORMAL' })).toMatch(/价格/);
    // 市价单不校验价格
    expect(validateOrder({ code: '700', quantity: 100, price: 0, orderType: 'MARKET' })).toBeNull();
    expect(validateOrder({ code: '700', quantity: 100, price: 380, orderType: 'NORMAL' })).toBeNull();
  });
});
