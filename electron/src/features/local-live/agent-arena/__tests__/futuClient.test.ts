/**
 * 港股富途数据层护栏（2026-10-08 恢复；`arena/api/client.ts` 的富途段）。
 *
 * 这一层是生成器产物里**新增**的 QM 本地逻辑（reshape 抽提、closed 双形状、
 * place/cancel/orders 三函数），上游没有对应实现可比对，只有这些金样兜底：
 *   1. reshapeFutuAccount 边界：price=0（拿不到价）/ cost<0（摊薄成本）/
 *      cost=0（字段缺失）/ volume=0 过滤 / market_value 兜底；
 *   2. fetchFutuClosed 双形状：{closed:[...]} 与裸数组都要认（旧栈解包是 bug）；
 *   3. place/cancel/orders 的载荷与解包（REAL 的 env 必须原样上行）。
 *
 * 网络面：不是 mock axios 模块，而是 spy 真实 `api` 实例的 get/post ——
 * 保住 client 模块自身的拦截器/依赖图（authService 等）在测试里按原样加载。
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import type { AxiosResponse } from 'axios';
import {
  api,
  cancelFutuOrder,
  fetchFutuClosed,
  fetchFutuOrders,
  placeFutuOrder,
  reshapeFutuAccount,
} from '../arena/api/client';

const respond = (data: unknown): AxiosResponse => ({ data } as AxiosResponse);

describe('reshapeFutuAccount 金样', () => {
  it('原始行缺失 → 空账户 + 通道标记（不炸面板）', () => {
    expect(reshapeFutuAccount(undefined, 'simulate')).toEqual({
      asset: 0,
      positions: [],
      channel_used: 'futu-simulate',
    });
  });

  it('price=0（拿不到价）：不给盈亏段，市值退回成本口径', () => {
    const out = reshapeFutuAccount(
      {
        total_asset: 100000,
        cash: 50000,
        market_value: 50000,
        positions: {
          '00700.HK': {
            volume: 100,
            available_volume: 100,
            price: 0,
            market_value: 0,
            cost: 380,
            name: '腾讯控股',
            currency: 'HKD',
          },
        },
      },
      'real',
    );
    const p = out.positions[0];
    expect(out.channel_used).toBe('futu-real');
    expect(out.asset).toBe(100000);
    expect(p.pnl).toBe(0);
    expect(p.pnl_pct).toBe(0);
    expect(p.position_value).toBe(380 * 100); // market_value=0 → 成本兜底
  });

  it('cost<0（摊薄成本）：绝对盈亏有效，百分比归 0（符号会颠倒）', () => {
    const out = reshapeFutuAccount(
      {
        total_asset: 1,
        cash: 0,
        market_value: 0,
        positions: {
          '00005.HK': {
            volume: 200,
            available_volume: 200,
            price: 50,
            market_value: 10000,
            cost: -10,
            name: '汇丰控股',
            currency: 'HKD',
          },
        },
      },
      'real',
    );
    const p = out.positions[0];
    expect(p.pnl).toBe((50 - -10) * 200); // 12000：绝对盈亏仍有效
    expect(p.pnl_pct).toBe(0);
    expect(p.position_value).toBe(10000); // market_value 优先
  });

  it('cost=0（字段缺失）：两个都不算，不伪造平盘', () => {
    const out = reshapeFutuAccount(
      {
        total_asset: 1,
        cash: 0,
        market_value: 0,
        positions: {
          '09988.HK': {
            volume: 50,
            available_volume: 50,
            price: 100,
            market_value: 5000,
            cost: 0,
            name: '阿里巴巴-W',
            currency: 'HKD',
          },
        },
      },
      'simulate',
    );
    expect(out.positions[0].pnl).toBe(0);
    expect(out.positions[0].pnl_pct).toBe(0);
  });

  it('volume=0 的行被过滤；正常行四舍五入到两位', () => {
    const out = reshapeFutuAccount(
      {
        total_asset: 1,
        cash: 0,
        market_value: 0,
        positions: {
          '00001.HK': {
            volume: 0, available_volume: 0, price: 60, market_value: 0,
            cost: 50, name: '长和', currency: 'HKD',
          },
          '00700.HK': {
            volume: 100, available_volume: 100, price: 381.234, market_value: 38123.4,
            cost: 350.111, name: '腾讯控股', currency: 'HKD',
          },
        },
      },
      'real',
    );
    expect(out.positions).toHaveLength(1);
    const p = out.positions[0];
    expect(p.stock_code).toBe('00700.HK');
    expect(p.pnl_pct).toBe(+(((381.234 - 350.111) / 350.111) * 100).toFixed(2));
    expect(p.pnl).toBe(+((381.234 - 350.111) * 100).toFixed(2));
    expect(p.available_volume).toBe(100);
  });
});

describe('fetchFutuClosed 双形状容错', () => {
  const row = { code: '00700.HK', name: '腾讯控股', qty: 0, realized_pl: 120 };

  beforeEach(() => {
    vi.spyOn(api, 'get');
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('本仓形状 {closed:[...]}', async () => {
    vi.mocked(api.get).mockResolvedValue(respond({ success: true, data: { closed: [row] } }));
    await expect(fetchFutuClosed('REAL')).resolves.toEqual([row as never]);
    expect(api.get).toHaveBeenCalledWith('/futu/closed', { params: { env: 'REAL' } });
  });

  it('旧栈裸数组形状', async () => {
    vi.mocked(api.get).mockResolvedValue(respond({ success: true, data: [row] }));
    await expect(fetchFutuClosed()).resolves.toEqual([row as never]);
  });

  it('data 缺失 → 空数组（面板空态，不炸）', async () => {
    vi.mocked(api.get).mockResolvedValue(respond({ success: false, error: 'x' }));
    await expect(fetchFutuClosed()).resolves.toEqual([]);
  });
});

describe('place / cancel / orders 载荷与解包', () => {
  beforeEach(() => {
    vi.spyOn(api, 'post');
    vi.spyOn(api, 'get');
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('placeFutuOrder：POST /futu/place {env, market:HK, order}，解包 data', async () => {
    const result = {
      success: true, order_id: 'O1', status: 'SUBMITTED',
      filled_quantity: 0, filled_price: 0, message: '',
    };
    vi.mocked(api.post).mockResolvedValue(respond({ success: true, data: result }));
    const order = {
      code: '00700.HK', price: 380, quantity: 100,
      order_type: 'NORMAL' as const, trd_side: 'BUY' as const,
    };
    await expect(placeFutuOrder('REAL', order)).resolves.toEqual(result);
    expect(api.post).toHaveBeenCalledWith('/futu/place', {
      env: 'REAL', market: 'HK', order,
    });
  });

  it('cancelFutuOrder：POST /futu/cancel {env, market:HK, order_id}', async () => {
    vi.mocked(api.post).mockResolvedValue(
      respond({ success: true, data: { success: true, message: 'CANCELLED' } }),
    );
    await expect(cancelFutuOrder('SIMULATE', 'O1')).resolves.toEqual({
      success: true, message: 'CANCELLED',
    });
    expect(api.post).toHaveBeenCalledWith('/futu/cancel', {
      env: 'SIMULATE', market: 'HK', order_id: 'O1',
    });
  });

  it('fetchFutuOrders：GET /futu/orders?env，取 data.orders', async () => {
    const rows = [{ order_id: 'O1', code: '00700.HK', order_status: 'SUBMITTED' }];
    vi.mocked(api.get).mockResolvedValue(respond({ success: true, data: { orders: rows } }));
    await expect(fetchFutuOrders('REAL')).resolves.toEqual(rows as never);
    expect(api.get).toHaveBeenCalledWith('/futu/orders', { params: { env: 'REAL' } });

    vi.mocked(api.get).mockResolvedValue(respond({ success: false, error: 'opend down' }));
    await expect(fetchFutuOrders()).resolves.toEqual([]);
  });
});
