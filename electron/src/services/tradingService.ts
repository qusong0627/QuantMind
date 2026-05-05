/**
 * 交易服务
 *
 * 封装 Trading Service 订单/成交相关 API 调用
 * 通过 API Gateway (端口8000) 统一代理
 *
 * @author QuantMind Team
 * @date 2025-02-13
 */

import { APIClient, createAPIClient, DEFAULT_API_CONFIG, APIClientConfig } from './api-client';
import { API_ENDPOINTS } from './config';
import { SERVICE_URLS } from '../config/services';
import { ChartDataPoint } from './portfolioService';

// ==================== 接口定义 ====================

/** 订单方向 */
export type OrderSide = 'buy' | 'sell';

/** 订单类型 */
export type OrderType = 'market' | 'limit' | 'stop';

/** 订单状态 */
export type OrderStatus =
    | 'pending'
    | 'submitted'
    | 'partial'
    | 'partially_filled'
    | 'filled'
    | 'cancelled'
    | 'rejected'
    | 'failed'
    | 'expired';

/** 交易模式 */
export type TradingMode = 'real' | 'simulation';

/** 订单记录 */
export interface Order {
    id: string | number;
    order_id?: string;
    user_id: string | number;
    portfolio_id?: string | number;
    strategy_id?: string | number;
    tenant_id?: string;
    symbol: string;
    symbol_name?: string;
    side: OrderSide;
    order_type: OrderType;
    status: OrderStatus;
    trading_mode: TradingMode;
    quantity: number;
    price?: number;
    filled_quantity?: number;
    filled_price?: number;
    average_price?: number;
    stop_price?: number;
    total_amount?: number;
    order_value?: number;
    filled_value?: number;
    commission?: number;
    created_at: string;
    updated_at: string;
}

/** 订单列表查询参数 */
export interface ListOrdersParams {
    user_id?: string;
    portfolio_id?: string;
    status?: OrderStatus;
    trading_mode?: TradingMode;
    limit?: number;
    offset?: number;
}

/** 创建订单参数 */
export interface CreateOrderParams {
    symbol: string;
    symbol_name?: string;
    side: OrderSide;
    order_type: OrderType;
    quantity: number;
    price?: number;
    stop_price?: number;
    portfolio_id?: string;
    strategy_id?: string;
    trading_mode?: TradingMode;
}

/** 订单列表响应（含分页） */
export interface OrderListResponse {
    orders: Order[];
    total: number;
    limit: number;
    offset: number;
}

type ListOrdersResult = Order[] | OrderListResponse;

export interface Trade {
    id: string | number;
    trade_id?: string;
    order_id?: string;
    user_id: string | number;
    portfolio_id?: string | number;
    tenant_id?: string;
    symbol: string;
    symbol_name?: string;
    side: OrderSide | string;
    trading_mode: TradingMode | string;
    quantity: number;
    price: number;
    trade_value?: number;
    commission?: number;
    executed_at?: string;
    created_at?: string;
}

export interface ListTradesParams {
    user_id?: string;
    portfolio_id?: string;
    trading_mode?: TradingMode;
    limit?: number;
    offset?: number;
}

/** 前端展示用的交易记录（兼容 TradeRecordsCard 现有接口） */
export interface TradeRecord {
    id: string;
    symbol: string;
    name: string;
    type: '买入' | '卖出';
    price: number;
    amount: number;
    total: number;
    time: string;
    status: '已成交' | '待成交' | '已撤销' | '部分成交' | '未知状态';
}

interface TradeStatsDailyPoint {
    timestamp: string;
    value: number;
    label?: string;
}

interface TradeStatsSummaryResponse {
    daily_counts?: TradeStatsDailyPoint[];
    data?: {
        daily_counts?: TradeStatsDailyPoint[];
    };
}

// ==================== 服务类 ====================

class TradingService {
    private client: APIClient;
    private ordersEndpointUnavailableUntil = 0;
    private tradesEndpointUnavailableUntil = 0;

    constructor() {
        this.client = createAPIClient({
            ...DEFAULT_API_CONFIG,
            baseURL: SERVICE_URLS.API_GATEWAY,
            onUnauthorized: () => {
                console.warn('Trading Service: 未授权，请重新登录');
            },
        } as APIClientConfig);
    }

    /**
     * 设置认证 Token
     */
    setToken(token: string): void {
        this.client.setToken(token);
    }

    /**
     * 清除认证 Token
     */
    clearToken(): void {
        this.client.clearToken();
    }

    // ==================== Orders API ====================

    /**
     * 获取订单列表
     */
    async listOrders(params: ListOrdersParams = {}): Promise<ListOrdersResult> {
        if (Date.now() < this.ordersEndpointUnavailableUntil) {
            return [];
        }

        const queryParams: Record<string, unknown> = {};
        if (params.user_id) queryParams['user_id'] = params.user_id;
        if (params.portfolio_id) queryParams['portfolio_id'] = params.portfolio_id;
        if (params.status) queryParams['status'] = params.status;
        if (params.trading_mode) queryParams['trading_mode'] = params.trading_mode;
        queryParams['limit'] = params.limit ?? 50;
        queryParams['offset'] = params.offset ?? 0;

        try {
            return await this.client.get<ListOrdersResult>(API_ENDPOINTS.ORDERS, queryParams);
        } catch (error: any) {
            if (this.isNotFoundError(error)) {
                // 网关未接入 orders 路由时，短时熔断避免控制台反复 404
                this.ordersEndpointUnavailableUntil = Date.now() + 5 * 60 * 1000;
                return [];
            }
            throw error;
        }
    }

    /**
     * 获取成交列表
     */
    async listTrades(params: ListTradesParams = {}): Promise<Trade[]> {
        if (Date.now() < this.tradesEndpointUnavailableUntil) {
            throw new Error('trades endpoint unavailable');
        }
        const queryParams: Record<string, unknown> = {};
        if (params.user_id) queryParams['user_id'] = params.user_id;
        if (params.portfolio_id) queryParams['portfolio_id'] = params.portfolio_id;
        if (params.trading_mode) queryParams['trading_mode'] = this.normalizeTradingMode(params.trading_mode);
        queryParams['limit'] = params.limit ?? 50;
        queryParams['offset'] = params.offset ?? 0;

        try {
            return await this.client.get<Trade[]>(API_ENDPOINTS.TRADES, queryParams);
        } catch (error: any) {
            if (this.isNotFoundError(error)) {
                // 网关未接入 trades 路由时，短时熔断避免控制台反复 404
                this.tradesEndpointUnavailableUntil = Date.now() + 5 * 60 * 1000;
            }
            throw error;
        }
    }

    /**
     * 获取订单详情
     */
    async getOrder(orderId: string): Promise<Order> {
        return this.client.get<Order>(API_ENDPOINTS.ORDER_DETAIL(orderId));
    }

    /**
     * 创建订单
     */
    async createOrder(data: CreateOrderParams): Promise<Order> {
        return this.client.post<Order>(
            API_ENDPOINTS.ORDERS,
            data as unknown as Record<string, unknown>,
        );
    }

    /**
     * 取消订单
     */
    async cancelOrder(orderId: string): Promise<Order> {
        return this.client.post<Order>(
            API_ENDPOINTS.ORDER_CANCEL(orderId),
            { order_id: orderId } as unknown as Record<string, unknown>,
        );
    }

    /**
     * 获取交易统计数据 (用于图表)
     * @param userId 用户ID
     * @param range 时间范围
     * @param tradingMode 交易模式
     */
    async getTradeStats(userId: string, range = '1m', tradingMode?: TradingMode): Promise<ChartDataPoint[]> {
        try {
            const params: Record<string, unknown> = {
                user_id: userId,
                range
            };
            if (tradingMode) {
                params['trading_mode'] = this.normalizeTradingMode(tradingMode);
            }
            const response = await this.client.get<TradeStatsSummaryResponse | ChartDataPoint[]>(API_ENDPOINTS.TRADING_STATS, params);
            if (Array.isArray(response)) {
                return response;
            }
            const points = response?.daily_counts ?? response?.data?.daily_counts ?? [];
            if (!Array.isArray(points)) {
                return [];
            }
            return points
                .map((point) => ({
                    timestamp: String(point?.timestamp || ''),
                    value: Number(point?.value ?? 0),
                    label: point?.label || 'trade_count',
                }))
                .filter((point) => point.timestamp && Number.isFinite(point.value));
        } catch (error) {
            console.warn('获取交易统计失败:', error);
            return []; // Remove mock data
        }
    }

    private getMockTradeStats(count = 30): ChartDataPoint[] {
        return Array.from({ length: count }, (_, i) => {
            return {
                timestamp: new Date(Date.now() - (count - 1 - i) * 24 * 60 * 60 * 1000).toISOString(),
                value: Math.floor(Math.random() * 20), // 0-20 trades per day
                label: `Day ${i + 1}`
            };
        });
    }

    // ==================== 聚合/转换方法 ====================

    /**
     * 获取最近交易记录（前端展示用）
     *
     * 从后端获取订单列表，转换为 TradeRecord 格式
     * 后端不可用时返回空列表
     */
    async getRecentTrades(
        limit = 10,
        tradingMode?: TradingMode,
    ): Promise<{ records: TradeRecord[]; isOffline: boolean; isFallbackToOrders: boolean }> {
        const normalizedTradingMode = this.normalizeTradingMode(tradingMode);
        try {
            const trades = await this.listTrades({
                limit,
                trading_mode: normalizedTradingMode,
            });
            const records = trades.map((trade) => this.mapTradeToTradeRecord(trade));
            return { records, isOffline: false, isFallbackToOrders: false };
        } catch (tradeError) {
            // 成交接口失败时降级回订单接口，避免卡片直接空白
            try {
                const response = await this.listOrders({
                    limit,
                    trading_mode: normalizedTradingMode,
                });
                const orders = this.extractOrders(response);
                const records = orders.map((order) => this.mapOrderToTradeRecord(order));
                return { records, isOffline: false, isFallbackToOrders: true };
            } catch (orderError) {
                console.warn('交易服务不可用，返回空列表:', orderError || tradeError);
                return { records: [], isOffline: true, isFallbackToOrders: false };
            }
        }
    }

    private mapTradeToTradeRecord(trade: Trade): TradeRecord {
        const id = String(trade.trade_id ?? trade.id);
        const quantity = Number(trade.quantity ?? 0);
        const price = Number(trade.price ?? 0);
        const total = Number(trade.trade_value ?? (quantity * price));
        const side = String(trade.side || '').toLowerCase();

        return {
            id,
            symbol: trade.symbol,
            name: trade.symbol_name || trade.symbol,
            type: side === 'sell' ? '卖出' : '买入',
            price,
            amount: quantity,
            total,
            time: trade.executed_at || trade.created_at || new Date().toISOString(),
            status: '已成交',
        };
    }

    /**
     * 将后端 Order 转换为前端 TradeRecord 格式
     */
    private mapOrderToTradeRecord(order: Order): TradeRecord {
        const statusMap: Record<string, TradeRecord['status']> = {
            pending: '待成交',
            open: '待成交',
            submitted: '待成交',
            partial: '部分成交',
            partially_filled: '部分成交',
            filled: '已成交',
            cancelled: '已撤销',
            rejected: '已撤销',
            failed: '已撤销',
            expired: '已撤销',
        };

        const id = String(order.order_id ?? order.id);
        const amount = order.filled_quantity ?? order.quantity;
        const price = order.filled_price ?? order.average_price ?? order.price ?? 0;
        const total = order.filled_value ?? order.total_amount ?? order.order_value ?? (price * amount);
        const statusKey = String(order.status || '').toLowerCase();
        const side = String(order.side || '').toLowerCase();

        return {
            id,
            symbol: order.symbol,
            name: order.symbol_name || order.symbol, // 优先使用后端返回的代码简称
            type: side === 'sell' ? '卖出' : '买入',
            price,
            amount: amount,
            total,
            time: order.created_at,
            status: statusMap[statusKey] || '未知状态',
        };
    }

    private normalizeTradingMode(mode?: TradingMode): TradingMode | undefined {
        if (!mode) return undefined;
        const normalized = String(mode).trim().toLowerCase();
        if (normalized === 'real') return 'real';
        if (normalized === 'simulation') return 'simulation';
        return undefined;
    }

    private extractOrders(result: ListOrdersResult): Order[] {
        if (Array.isArray(result)) {
            return result;
        }
        return result.orders || [];
    }

    private isNotFoundError(error: unknown): boolean {
        if (!error || typeof error !== 'object') return false;
        const maybe = error as { status?: number; code?: string };
        return maybe.status === 404 || maybe.code === 'HTTP_404';
    }
}

export const tradingService = new TradingService();
