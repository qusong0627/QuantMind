/**
 * TradingService 单元测试
 *
 * @author QuantMind Team
 * @date 2025-02-13
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';

// 使用 vi.hoisted 确保 mocks 变量被提升
const mocks = vi.hoisted(() => ({
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    delete: vi.fn(),
    patch: vi.fn(),
    request: vi.fn(),
}));

vi.mock('axios', () => {
    return {
        default: {
            create: () => ({
                get: mocks.get,
                post: mocks.post,
                put: mocks.put,
                delete: mocks.delete,
                patch: mocks.patch,
                request: mocks.request,
                interceptors: {
                    request: { use: vi.fn() },
                    response: { use: vi.fn() },
                },
            }),
            isAxiosError: () => false,
        },
        // 支持 Named import
        create: () => ({
            get: mocks.get,
            post: mocks.post,
            put: mocks.put,
            delete: mocks.delete,
            patch: mocks.patch,
            request: mocks.request,
            interceptors: {
                request: { use: vi.fn() },
                response: { use: vi.fn() },
            },
        }),
        isAxiosError: () => false,
        __esModule: true,
    };
});

import { tradingService } from '../tradingService';

describe('TradingService', () => {
    beforeEach(() => {
        vi.clearAllMocks();
    });

    describe('getRecentTrades', () => {
        it('后端不可用时应该返回空列表并标记 isOffline', async () => {
            mocks.get.mockRejectedValue(new Error('Network Error'));

            const result = await tradingService.getRecentTrades(10);

            expect(result.isOffline).toBe(true);
            expect(result.isFallbackToOrders).toBe(false);
            expect(result.records).toEqual([]);
        });

        it('成交接口返回数据时应该正确映射为 TradeRecord 格式', async () => {
            const mockTrades = [
                {
                    id: 'trade-1',
                    trade_id: 'trade-1',
                    user_id: 'user-1',
                    symbol: '600519.SH',
                    side: 'BUY',
                    trading_mode: 'REAL',
                    quantity: 100,
                    price: 1799.5,
                    trade_value: 179950.0,
                    executed_at: '2025-02-13T13:30:00Z',
                    created_at: '2025-02-13T13:30:01Z',
                },
                {
                    id: 'trade-2',
                    trade_id: 'trade-2',
                    user_id: 'user-1',
                    symbol: '000001.SZ',
                    side: 'sell',
                    trading_mode: 'simulation',
                    quantity: 500,
                    price: 12.5,
                    trade_value: 6250.0,
                    executed_at: '2025-02-13T13:35:00Z',
                    created_at: '2025-02-13T13:35:01Z',
                },
            ];
            mocks.get.mockResolvedValueOnce({ data: mockTrades });

            const result = await tradingService.getRecentTrades(10);

            expect(result.isOffline).toBe(false);
            expect(result.isFallbackToOrders).toBe(false);
            expect(result.records).toHaveLength(2);
            expect(result.records[0].id).toBe('trade-1');
            expect(result.records[0].type).toBe('买入');
            expect(result.records[0].status).toBe('已成交');
            expect(result.records[1].type).toBe('卖出');
            expect(result.records[1].status).toBe('已成交');
        });

        it('成交接口失败时应降级到订单接口', async () => {
            const mockOrders = {
                orders: [
                    {
                        id: 'order-1',
                        user_id: 'user-1',
                        symbol: '600519.SH',
                        side: 'buy',
                        order_type: 'limit',
                        status: 'filled',
                        trading_mode: 'simulation',
                        quantity: 100,
                        price: 1800.00,
                        filled_quantity: 100,
                        filled_price: 1799.50,
                        total_amount: 179950.00,
                        created_at: '2025-02-13T13:30:00Z',
                        updated_at: '2025-02-13T13:30:01Z',
                    },
                    {
                        id: 'order-2',
                        user_id: 'user-1',
                        symbol: '000001.SZ',
                        side: 'sell',
                        order_type: 'market',
                        status: 'pending',
                        trading_mode: 'simulation',
                        quantity: 500,
                        price: 12.50,
                        total_amount: 6250.00,
                        created_at: '2025-02-13T13:35:00Z',
                        updated_at: '2025-02-13T13:35:00Z',
                    },
                ],
                total: 2,
                limit: 10,
                offset: 0,
            };
            mocks.get.mockRejectedValueOnce(new Error('trade endpoint down'));
            mocks.get.mockResolvedValueOnce({ data: mockOrders });

            const result = await tradingService.getRecentTrades(10);

            expect(result.isOffline).toBe(false);
            expect(result.isFallbackToOrders).toBe(true);
            expect(result.records).toHaveLength(2);
            expect(result.records[0].id).toBe('order-1');
            expect(result.records[0].type).toBe('买入'); // BUY/SELL mixed-case 兼容
            expect(result.records[0].status).toBe('已成交');
            expect(result.records[1].type).toBe('卖出');
            expect(result.records[1].status).toBe('待成交');
        });

        it('成交接口返回空列表时应该返回空记录', async () => {
            mocks.get.mockResolvedValue({
                data: [],
            });

            const result = await tradingService.getRecentTrades(10);

            expect(result.isOffline).toBe(false);
            expect(result.isFallbackToOrders).toBe(false);
            expect(result.records).toEqual([]);
        });

        it('订单降级时应兼容状态大小写并支持未知状态', async () => {
            const mockOrders = [
                {
                    id: 1,
                    order_id: 'order-1',
                    user_id: 1,
                    symbol: '600519.SH',
                    side: 'BUY',
                    order_type: 'limit',
                    status: 'PARTIALLY_FILLED',
                    trading_mode: 'simulation',
                    quantity: 100,
                    filled_quantity: 30,
                    price: 1800,
                    average_price: 1798.2,
                    order_value: 180000,
                    filled_value: 53946,
                    created_at: '2025-02-13T13:30:00Z',
                    updated_at: '2025-02-13T13:30:01Z',
                },
                {
                    id: 2,
                    order_id: 'order-2',
                    user_id: 1,
                    symbol: '000001.SZ',
                    side: 'SELL',
                    order_type: 'limit',
                    status: 'X_UNKNOWN',
                    trading_mode: 'simulation',
                    quantity: 10,
                    price: 10,
                    order_value: 100,
                    created_at: '2025-02-13T13:30:00Z',
                    updated_at: '2025-02-13T13:30:01Z',
                }
            ];

            mocks.get.mockRejectedValueOnce(new Error('trade endpoint down'));
            mocks.get.mockResolvedValueOnce({ data: mockOrders });

            const result = await tradingService.getRecentTrades(10);

            expect(result.isOffline).toBe(false);
            expect(result.isFallbackToOrders).toBe(true);
            expect(result.records).toHaveLength(2);
            expect(result.records[0].id).toBe('order-1');
            expect(result.records[0].status).toBe('部分成交');
            expect(result.records[0].price).toBe(1798.2);
            expect(result.records[0].total).toBe(53946);
            expect(result.records[1].status).toBe('未知状态');
        });
    });

    describe('Order 状态映射', () => {
        it('应该正确映射所有订单状态', async () => {
            const statuses = [
                'pending',
                'submitted',
                'partial',
                'partially_filled',
                'filled',
                'cancelled',
                'rejected',
                'failed',
                'expired',
            ];
            const expected = ['待成交', '待成交', '部分成交', '部分成交', '已成交', '已撤销', '已撤销', '已撤销', '已撤销'];

            const orders = statuses.map((status, i) => ({
                id: `order-${i}`,
                user_id: 'user-1',
                symbol: `${i}.SH`,
                side: 'buy',
                order_type: 'limit',
                status,
                trading_mode: 'simulation',
                quantity: 100,
                price: 10,
                total_amount: 1000,
                created_at: '2025-02-13T13:30:00Z',
                updated_at: '2025-02-13T13:30:00Z',
            }));

            mocks.get.mockRejectedValueOnce(new Error('trade endpoint down'));
            mocks.get.mockResolvedValueOnce({
                data: { orders, total: orders.length, limit: 50, offset: 0 },
            });

            const result = await tradingService.getRecentTrades(50);

            result.records.forEach((record, i) => {
                expect(record.status).toBe(expected[i]);
            });
        });
    });

    describe('cancelOrder', () => {
        it('应携带 order_id 请求体调用取消接口', async () => {
            mocks.post.mockResolvedValue({
                data: {
                    id: 'order-1',
                    user_id: 'user-1',
                    symbol: '600519.SH',
                    side: 'buy',
                    order_type: 'limit',
                    status: 'cancelled',
                    trading_mode: 'simulation',
                    quantity: 100,
                    price: 1800,
                    total_amount: 180000,
                    created_at: '2025-02-13T13:30:00Z',
                    updated_at: '2025-02-13T13:31:00Z',
                },
            });

            await tradingService.cancelOrder('order-1');

            expect(mocks.post).toHaveBeenCalledWith(
                '/api/v1/orders/order-1/cancel',
                { order_id: 'order-1' },
                undefined,
            );
        });
    });

    describe('getTradeStats', () => {
        it('应优先解析 daily_counts 时序字段', async () => {
            mocks.get.mockResolvedValue({
                data: {
                    daily_counts: [
                        { timestamp: '2026-03-01T00:00:00Z', value: 2, label: 'trade_count' },
                        { timestamp: '2026-03-02T00:00:00Z', value: 5, label: 'trade_count' },
                    ],
                    total_trades: 7,
                },
            });

            const result = await tradingService.getTradeStats('current');

            expect(result).toEqual([
                { timestamp: '2026-03-01T00:00:00Z', value: 2, label: 'trade_count' },
                { timestamp: '2026-03-02T00:00:00Z', value: 5, label: 'trade_count' },
            ]);
        });

        it('后端仅返回旧汇总结构时应降级为空数组', async () => {
            mocks.get.mockResolvedValue({
                data: {
                    total_trades: 10,
                    total_value: 12345.67,
                },
            });

            const result = await tradingService.getTradeStats('current');

            expect(result).toEqual([]);
        });
    });
});

/**
 * 实盘闸门 403（`ENABLE_REAL_TRADING=false` 的部署）与既有 404 熔断的分工。
 *
 * 走动态 import 拿**新的单例**：熔断位是实例状态，复用文件顶部的单例会让
 * 用例之间互相污染（前一个用例熔断，后一个用例连 mock 都不再被调到）。
 */
describe('TradingService 实盘闸门 403', () => {
    const DISABLED = { status: 403, data: { detail: 'real_trading_disabled' } };

    // 本块是文件级 describe 的兄弟节点，拿不到上面那个 `beforeEach`，
    // 必须自己清调用记录——否则 `toHaveBeenCalledTimes` 数的是整个文件累计的调用数
    beforeEach(() => {
        vi.clearAllMocks();
    });

    const loadFresh = async () => {
        vi.resetModules();
        return (await import('../tradingService')).tradingService;
    };

    it('listOrders 遇闸门 403 时抛出而非返回空数组', async () => {
        // Arrange：返回空数组会渲染成「没有委托」，而事实是「这一路不可用」
        const service = await loadFresh();
        mocks.get.mockRejectedValue(DISABLED);

        // Act / Assert
        await expect(service.listOrders({ limit: 10 })).rejects.toMatchObject({ status: 403 });
    });

    it('闸门 403 之后熔断，不再反复打后端', async () => {
        // Arrange
        const service = await loadFresh();
        mocks.get.mockRejectedValue(DISABLED);
        await expect(service.listOrders({ limit: 10 })).rejects.toBeDefined();

        // Act
        const second = await service.listOrders({ limit: 10 });

        // Assert：熔断生效 → 空数组且没有再发请求
        expect(second).toEqual([]);
        expect(mocks.get).toHaveBeenCalledTimes(1);
    });

    it('普通 403（权限不足）不熔断，原样抛给用户看', async () => {
        // Arrange：只看状态码分不出「本部署没开实盘」与「你没权限」，
        // 后者被当成前者静默吞掉，用户就永远看不到真正的原因
        const service = await loadFresh();
        mocks.get.mockRejectedValue({ status: 403, data: { detail: 'forbidden' } });

        // Act / Assert
        await expect(service.listOrders({ limit: 10 })).rejects.toMatchObject({ status: 403 });
        await expect(service.listOrders({ limit: 10 })).rejects.toMatchObject({ status: 403 });
        expect(mocks.get).toHaveBeenCalledTimes(2);
    });

    it('listTrades 遇闸门 403 也熔断', async () => {
        // Arrange
        const service = await loadFresh();
        mocks.get.mockRejectedValue(DISABLED);
        await expect(service.listTrades({ limit: 10 })).rejects.toBeDefined();

        // Act / Assert：熔断后直接抛「unavailable」，不再发请求
        await expect(service.listTrades({ limit: 10 })).rejects.toThrow('trades endpoint unavailable');
        expect(mocks.get).toHaveBeenCalledTimes(1);
    });
});
