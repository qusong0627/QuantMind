/**
 * 策略服务
 *
 * 封装策略相关 API 调用
 *
 * @author QuantMind Team
 * @date 2025-02-13
 */

import { apiClient } from './api-client';
import { API_ENDPOINTS } from './config';

export interface ApiResponse<T> {
    code: number;
    message: string;
    data: T;
}

// 策略状态类型
export type StrategyStatus = 'running' | 'starting' | 'paused' | 'error' | 'stopped';
export type RiskLevel = 'low' | 'medium' | 'high';

// 策略接口定义
export interface Strategy {
    id: string;
    name: string;
    description?: string;
    status: StrategyStatus;
    base_status?: 'draft' | 'repository' | 'live_trading';
    runtime_state?: 'running' | 'starting' | 'stopped' | 'error' | null;
    effective_status?: 'running' | 'starting' | 'stopped' | 'error';
    total_return: number;
    today_return: number;
    today_pnl?: number;
    risk_level: RiskLevel;
    created_at: string;
    updated_at: string;
    last_update?: string;
    error_code?: string;
    error_message?: string;
    last_failed_at?: string;
    last_signal_at?: string;
    execution_latency_ms?: number;
    parameters?: Record<string, unknown>;
}

// 启动/停止响应
export interface StrategyActionResponse {
    success: boolean;
    message: string;
    status: StrategyStatus;
}

// 回测配置
export interface BacktestConfig {
    strategy_id: string;
    start_date: string;
    end_date: string;
    initial_capital: number;
    frequency: '1d' | '1m' | '5m' | '15m' | '30m' | '60m';
}

// 回测结果
export interface BacktestResult {
    backtest_id: string;
    status: 'pending' | 'running' | 'completed' | 'failed';
    total_return: number;
    annual_return: number;
    max_drawdown: number;
    sharpe_ratio: number;
    alpha: number;
    beta: number;
    volatility: number;
    information_ratio: number;
}

// 回测记录
export interface BacktestRecord {
    id: string;
    strategy_id: string;
    start_date: string;
    end_date: string;
    status: string;
    result?: BacktestResult;
    created_at: string;
}

class StrategyService {
    private isObject(value: unknown): value is Record<string, unknown> {
        return typeof value === 'object' && value !== null;
    }

    private parseStatus(value: unknown): StrategyStatus {
        const text = String(value || '').toLowerCase();
        if (text === 'starting') return 'starting';
        if (text === 'running' || text === 'active' || text === 'live_trading') return 'running';
        if (text === 'paused' || text === 'inactive') return 'stopped';
        if (text === 'error' || text === 'failed') return 'error';
        if (text === 'stopped' || text === 'archived' || text === 'draft') return 'stopped';
        return 'stopped';
    }

    private parseRiskLevel(value: unknown): RiskLevel {
        const text = String(value || '').toLowerCase();
        if (text === 'low' || text === 'medium' || text === 'high') {
            return text as RiskLevel;
        }
        return 'medium';
    }

    private normalizeStrategy(input: unknown, index: number): Strategy {
        const raw = this.isObject(input) ? input : {};
        const now = new Date().toISOString();
        const id = String(raw['id'] ?? raw['strategy_id'] ?? `strategy-${index + 1}`);
        const name = String(raw['name'] ?? raw['strategy_name'] ?? `策略 ${index + 1}`);
        const description = raw['description'] ? String(raw['description']) : undefined;
        const baseStatusText = String(raw['base_status'] ?? raw['status'] ?? '').toLowerCase();
        const runtimeStateText = String(raw['runtime_state'] ?? '').toLowerCase();
        const effectiveStatusText = String(raw['effective_status'] ?? raw['status'] ?? '').toLowerCase();
        const status = this.parseStatus(effectiveStatusText);
        const totalReturn = Number(raw['total_return'] ?? raw['totalReturn'] ?? 0);
        const todayReturn = Number(raw['today_return'] ?? raw['todayReturn'] ?? 0);
        const todayPnl = Number(raw['today_pnl'] ?? raw['todayPnl'] ?? 0);
        const riskLevel = this.parseRiskLevel(raw['risk_level'] ?? raw['riskLevel']);
        const createdAt = String(raw['created_at'] ?? raw['createdAt'] ?? now);
        const updatedAt = String(raw['updated_at'] ?? raw['updatedAt'] ?? now);

        return {
            id,
            name,
            description,
            status,
            base_status: (baseStatusText === 'draft' || baseStatusText === 'repository' || baseStatusText === 'live_trading')
                ? (baseStatusText as Strategy['base_status'])
                : undefined,
            runtime_state: (runtimeStateText === 'running' || runtimeStateText === 'starting' || runtimeStateText === 'stopped' || runtimeStateText === 'error')
                ? (runtimeStateText as NonNullable<Strategy['runtime_state']>)
                : null,
            effective_status: (effectiveStatusText === 'running' || effectiveStatusText === 'starting' || effectiveStatusText === 'stopped' || effectiveStatusText === 'error')
                ? (effectiveStatusText as NonNullable<Strategy['effective_status']>)
                : undefined,
            total_return: Number.isFinite(totalReturn) ? totalReturn : 0,
            today_return: Number.isFinite(todayReturn) ? todayReturn : 0,
            today_pnl: Number.isFinite(todayPnl) ? todayPnl : 0,
            risk_level: riskLevel,
            created_at: createdAt,
            updated_at: updatedAt,
            last_update: raw['last_update'] ? String(raw['last_update']) : undefined,
            error_code: raw['error_code'] ? String(raw['error_code']) : undefined,
            error_message: raw['error_message'] ? String(raw['error_message']) : undefined,
            last_failed_at: raw['last_failed_at'] ? String(raw['last_failed_at']) : undefined,
            last_signal_at: raw['last_signal_at'] ? String(raw['last_signal_at']) : undefined,
            execution_latency_ms: Number(raw['execution_latency_ms'] ?? raw['executionLatencyMs'] ?? 0) || undefined,
            parameters: this.isObject(raw['parameters']) ? (raw['parameters'] as Record<string, unknown>) : undefined,
        };
    }

    private extractList(data: unknown): unknown[] {
        if (Array.isArray(data)) {
            return data;
        }
        if (!this.isObject(data)) {
            return [];
        }

        // 后端 StrategyListResponse 格式: { total, strategies: [...] }
        const candidates = ['strategies', 'items', 'list', 'records', 'data'];
        for (const key of candidates) {
            const value = data[key];
            if (Array.isArray(value)) {
                return value;
            }
        }
        return [];
    }

    private hasListShape(data: unknown): boolean {
        if (Array.isArray(data)) {
            return true;
        }
        if (!this.isObject(data)) {
            return false;
        }
        return ['strategies', 'items', 'list', 'records', 'data'].some((key) => key in data);
    }

    /**
     * 获取策略列表
     * @param options.market 市场过滤（CN/HK/US/FUTURES/CRYPTO）；后端按 parameters.market 过滤，
     *                       历史无 market 字段的策略按 A 股计；不传 = 全部市场
     */
    async getStrategies(options: { market?: string } = {}): Promise<ApiResponse<Strategy[]>> {
        const market = String(options.market || '').toUpperCase().trim();
        const endpoint = market
            ? `${API_ENDPOINTS.STRATEGIES}?market=${encodeURIComponent(market)}`
            : API_ENDPOINTS.STRATEGIES;
        const response = await apiClient.get<unknown>(endpoint);
        const normalizedResponse = this.isObject(response) ? response : {};
        const rawData = this.isObject(normalizedResponse) && 'data' in normalizedResponse
            ? normalizedResponse['data']
            : response;
        const hasStrategiesPayload = this.hasListShape(rawData) || this.hasListShape(response);

        const rawCode = normalizedResponse['code'];
        const rawSuccess = normalizedResponse['success'];
        const code = typeof rawCode === 'number'
            ? rawCode
            : rawSuccess === true
                ? 200
                : hasStrategiesPayload
                    ? 200
                    : 500;
        const message = String(
            normalizedResponse['message'] ??
            (code >= 200 && code < 300 ? 'Success' : 'Failed'),
        );

        const strategies = this.extractList(rawData).map((item, index) => this.normalizeStrategy(item, index));

        return {
            code,
            message,
            data: strategies,
        };
    }

    /**
     * 获取策略详情
     */
    async getStrategy(id: string): Promise<ApiResponse<Strategy>> {
        return await apiClient.get<ApiResponse<Strategy>>(API_ENDPOINTS.STRATEGY_DETAIL(id));
    }

    /**
     * 创建策略
     */
    async createStrategy(strategy: Omit<Strategy, 'id' | 'created_at' | 'updated_at'>): Promise<ApiResponse<Strategy>> {
        return await apiClient.post<ApiResponse<Strategy>>(API_ENDPOINTS.STRATEGIES, strategy);
    }

    /**
     * 删除策略
     */
    async deleteStrategy(id: string): Promise<ApiResponse<void>> {
        return await apiClient.delete<ApiResponse<void>>(API_ENDPOINTS.STRATEGY_DETAIL(id));
    }

    /**
     * 更新策略
     */
    async updateStrategy(id: string, strategy: Partial<Strategy>): Promise<ApiResponse<Strategy>> {
        return await apiClient.put<ApiResponse<Strategy>>(API_ENDPOINTS.STRATEGY_DETAIL(id), strategy);
    }

    /**
     * 启动策略
     */
    async startStrategy(id: string): Promise<ApiResponse<StrategyActionResponse>> {
        try {
            return await apiClient.post<ApiResponse<StrategyActionResponse>>(API_ENDPOINTS.STRATEGY_START(id));
        } catch (error) {
            throw error;
        }
    }

    /**
     * 停止策略
     */
    async stopStrategy(id: string): Promise<ApiResponse<StrategyActionResponse>> {
        try {
            return await apiClient.post<ApiResponse<StrategyActionResponse>>(API_ENDPOINTS.STRATEGY_STOP(id));
        } catch (error) {
            throw error;
        }
    }

    /**
     * 获取回测历史
     */
    async getBacktestHistory(id: string): Promise<ApiResponse<BacktestRecord[]>> {
        return await apiClient.get<ApiResponse<BacktestRecord[]>>(API_ENDPOINTS.STRATEGY_BACKTEST(id));
    }

    /**
     * 运行回测
     */
    async runBacktest(id: string, config: BacktestConfig): Promise<ApiResponse<BacktestResult>> {
        return await apiClient.post<ApiResponse<BacktestResult>>(
            API_ENDPOINTS.STRATEGY_BACKTEST(id),
            config as unknown as Record<string, unknown>,
        );
    }

    /**
     * 获取Mock数据 (降级用)
     */
    private getMockStrategies(): Strategy[] {
        return [
            {
                id: '1',
                name: 'AI量化策略A (模拟)',
                status: 'running',
                total_return: 15.67,
                today_return: 2.45,
                risk_level: 'medium',
                created_at: new Date().toISOString(),
                updated_at: new Date().toISOString()
            },
            {
                id: '2',
                name: '市场中性策略 (模拟)',
                status: 'running',
                total_return: 8.92,
                today_return: 1.23,
                risk_level: 'low',
                created_at: new Date().toISOString(),
                updated_at: new Date().toISOString()
            },
            {
                id: '3',
                name: '趋势跟踪策略 (模拟)',
                status: 'paused',
                total_return: -2.34,
                today_return: -0.56,
                risk_level: 'high',
                created_at: new Date().toISOString(),
                updated_at: new Date().toISOString()
            },
            {
                id: '4',
                name: '套利策略B (模拟)',
                status: 'error',
                total_return: 5.67,
                today_return: 0.00,
                risk_level: 'medium',
                created_at: new Date().toISOString(),
                updated_at: new Date().toISOString()
            }
        ];
    }
}

export const strategyService = new StrategyService();
