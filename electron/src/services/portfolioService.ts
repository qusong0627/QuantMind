/**
 * 投资组合服务
 *
 * 封装 Portfolio Service 和 Simulation 相关 API 调用
 * 通过 API Gateway (端口8000) 统一代理
 *
 * @author QuantMind Team
 * @date 2025-02-13
 */

import { APIClient, createAPIClient, DEFAULT_API_CONFIG, APIClientConfig } from './api-client';
import { API_ENDPOINTS } from './config';
import { SERVICE_URLS } from '../config/services';
import { FundData } from './userService';
import { calculatePositionsWinRate } from '../utils/portfolioUtils';

// ==================== 接口定义 ====================

/** 投资组合 */
export interface Portfolio {
    id: string;
    user_id: string;
    name: string;
    description?: string;
    total_value: number;
    cash_balance: number;
    created_at: string;
    updated_at: string;
}

/** 持仓 */
export interface Position {
    id: string;
    portfolio_id: string;
    symbol: string;
    quantity: number;
    avg_cost: number;
    current_price: number;
    market_value: number;
    unrealized_pnl: number;
    weight: number;
}

/** 绩效指标 */
export interface PerformanceMetrics {
    total_return: number;
    annual_return: number;
    sharpe_ratio: number;
    max_drawdown: number;
    volatility: number;
    sortino_ratio: number;
    calmar_ratio: number;
    win_rate: number;
}

/** 模拟盘账户 */
export interface SimulationAccount {
    tenant_id: string;
    user_id: string;
    total_asset: number;
    available_balance: number;
    frozen_balance: number;
    total_pnl: number;
    today_pnl: number;
    positions: Position[];
}

/** 模拟盘设置 */
export interface SimulationSettings {
    initial_capital: number;
}

/** 默认模拟账户初始资金 (100万) */
const DEFAULT_INITIAL_CAPITAL = 1_000_000;

/** 图表数据点 */
export interface ChartDataPoint {
    timestamp: string;
    value: number;
    label?: string;
}

/** 持仓分布数据 */
export interface PositionDistribution {
    name: string;
    value: number;
    code: string;
    ratio: number;
}

// ==================== 服务类 ====================

class PortfolioService {
    private client: APIClient;

    constructor() {
        this.client = createAPIClient({
            ...DEFAULT_API_CONFIG,
            baseURL: SERVICE_URLS.API_GATEWAY,
            onUnauthorized: () => {
                console.warn('Portfolio Service: 未授权，请重新登录');
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

    private toNumber(value: unknown, fallback = 0): number {
        const parsed = Number(value);
        return Number.isFinite(parsed) ? parsed : fallback;
    }

    private pickFirstNumber(
        values: unknown[],
        fallback = 0,
    ): number {
        for (const value of values) {
            if (value === null || value === undefined) continue;
            const parsed = Number(value);
            if (Number.isFinite(parsed)) return parsed;
        }
        return fallback;
    }

    private pickPreferredRatio(
        candidates: unknown[],
        derivedRatio: number,
    ): number {
        for (const value of candidates) {
            if (value === null || value === undefined) continue;
            const parsed = Number(value);
            if (!Number.isFinite(parsed)) continue;
            if (Math.abs(parsed) > 1e-8) {
                return parsed;
            }
            if (Math.abs(derivedRatio) <= 1e-8) {
                return 0;
            }
        }
        return derivedRatio;
    }

    private pickConsistentRatio(
        candidates: unknown[],
        derivedRatio: number,
        options?: {
            requireDerived?: boolean;
            maxDiff?: number;
        },
    ): number {
        const requireDerived = options?.requireDerived ?? false;
        const maxDiff = options?.maxDiff ?? 0.0005;
        const preferred = this.pickPreferredRatio(candidates, derivedRatio);
        if (!Number.isFinite(derivedRatio)) {
            return preferred;
        }
        if (requireDerived) {
            return derivedRatio;
        }
        if (!Number.isFinite(preferred)) {
            return derivedRatio;
        }
        return Math.abs(preferred - derivedRatio) > maxDiff ? derivedRatio : preferred;
    }

    private hasValidRealAccount(account: any): boolean {
        if (!account) return false;
        if (account.is_online === true) return true;
        const totalAsset = this.pickFirstNumber([account.total_asset], 0);
        const cash = this.pickFirstNumber(
            [account.cash, account.available_cash, account.available_balance],
            0,
        );
        const positions = Array.isArray(account.positions)
            ? account.positions.length
            : Object.keys(account.positions || {}).length;
        return totalAsset > 0 || cash > 0 || positions > 0;
    }

    private boolOr(value: unknown, fallback: boolean): boolean {
        if (typeof value === 'boolean') return value;
        return fallback;
    }

    // ==================== Portfolio API ====================

    /**
     * 获取用户组合列表
     */
    async listPortfolios(userId: string): Promise<Portfolio[]> {
        return this.client.get<Portfolio[]>(API_ENDPOINTS.PORTFOLIOS, { user_id: userId });
    }

    /**
     * 获取组合详情
     */
    async getPortfolio(portfolioId: string): Promise<Portfolio> {
        return this.client.get<Portfolio>(API_ENDPOINTS.PORTFOLIO_DETAIL(portfolioId));
    }

    /**
     * 获取组合持仓
     */
    async getPositions(portfolioId: string): Promise<Position[]> {
        return this.client.get<Position[]>(API_ENDPOINTS.PORTFOLIO_POSITIONS(portfolioId));
    }

    /**
     * 获取组合绩效指标
     */
    async getPerformance(portfolioId: string): Promise<PerformanceMetrics> {
        return this.client.get<PerformanceMetrics>(API_ENDPOINTS.PORTFOLIO_PERFORMANCE(portfolioId));
    }

    /**
     * 获取日收益率曲线
     * @param userId 用户ID
     * @param range 时间范围 (1w, 1m, 3m, 6m, 1y, all)
     */
    async getDailyReturns(userId: string, range = '1m'): Promise<ChartDataPoint[]> {
        try {
            return await this.client.get<ChartDataPoint[]>(API_ENDPOINTS.PORTFOLIOS_PERFORMANCE, {
                user_id: userId,
                range,
                type: 'daily_pnl'
            });
        } catch (error) {
            console.warn('获取日收益数据失败:', error);
            return []; // Remove mock data
        }
    }

    /**
     * 获取持仓分布
     * @param userId 用户ID
     */
    async getPositionDistribution(userId: string): Promise<PositionDistribution[]> {
        try {
            return await this.client.get<PositionDistribution[]>(API_ENDPOINTS.PORTFOLIO_DISTRIBUTION, {
                user_id: userId
            });
        } catch (error) {
            console.warn('获取持仓分布失败:', error);
            return []; // Remove mock data
        }
    }

    // ==================== Mock Data Generators ====================

    private getMockDailyReturns(count = 30): ChartDataPoint[] {
        return Array.from({ length: count }, (_, i) => {
            const isPositive = Math.random() > 0.45;
            const value = isPositive
                ? Math.random() * 5000 + 500
                : -(Math.random() * 4000 + 300);

            return {
                timestamp: new Date(Date.now() - (count - 1 - i) * 24 * 60 * 60 * 1000).toISOString(),
                value: Math.round(value * 100) / 100,
                label: `Day ${i + 1}`
            };
        });
    }

    private getMockPositionDistribution(): PositionDistribution[] {
        return [
            { name: '贵州茅台', code: '600519', value: 500000, ratio: 45 },
            { name: '宁德时代', code: '300750', value: 300000, ratio: 27 },
            { name: '招商银行', code: '600036', value: 150000, ratio: 13.5 },
            { name: '五粮液', code: '000858', value: 100000, ratio: 9 },
            { name: '现金', code: 'CASH', value: 60000, ratio: 5.5 }
        ];
    }

    // ==================== Simulation API ====================

    /**
     * 获取模拟盘账户
     */
    async getSimulationAccount(userId: string, tenantId = 'default'): Promise<SimulationAccount> {
        return this.client.get<SimulationAccount>(API_ENDPOINTS.SIMULATION_ACCOUNT, {
            tenant_id: tenantId,
            user_id: userId,
        });
    }

    /**
     * 获取模拟盘设置
     */
    async getSimulationSettings(): Promise<SimulationSettings> {
        return this.client.get<SimulationSettings>(API_ENDPOINTS.SIMULATION_SETTINGS);
    }

    /**
     * 更新模拟盘设置
     */
    async updateSimulationSettings(settings: SimulationSettings): Promise<SimulationSettings> {
        return this.client.put<SimulationSettings>(
            API_ENDPOINTS.SIMULATION_SETTINGS,
            settings as unknown as Record<string, unknown>,
        );
    }

    /**
     * 重置模拟盘账户
     */
    async resetSimulationAccount(userId: string, tenantId = 'default'): Promise<unknown> {
        return this.client.post(API_ENDPOINTS.SIMULATION_RESET, {
            user_id: userId,
            tenant_id: tenantId,
        });
    }

    // ==================== 聚合方法 ====================

    /**
     * 获取资金概览数据
     *
     * 根据 mode 参数分流：
     * - 'real': 调用 realTradingService.getAccount
     * - 'simulation': 调用 realTradingService.getSimulationAccount
     */
    async getFundOverview(
        userId: string,
        mode: 'real' | 'simulation' = 'simulation',
        tenantId = 'default',
    ): Promise<{ data: FundData; isSimulated: boolean }> {
        try {
            const { realTradingService } = await import('./realTradingService');
            let account: any = null;
            let useSimulation = mode === 'simulation';

            if (mode === 'real') {
                const realAccount = await realTradingService.getAccount(userId, tenantId).catch((error: any) => {
                    const statusCode = Number(error?.response?.status ?? 0);
                    if (statusCode === 401 || statusCode === 403) {
                        throw error;
                    }
                    return null;
                });
                if (realAccount) {
                    account = realAccount;
                    useSimulation = false;
                } else {
                    account = await realTradingService.getSimulationAccount(userId, tenantId);
                    useSimulation = true;
                }
            } else {
                account = await realTradingService.getSimulationAccount(userId, tenantId);
                useSimulation = true;
            }

            if (!account) {
                throw new Error('No account data received');
            }

            let totalAsset = this.pickFirstNumber(
                [account.total_asset],
                0,
            );

            // 增强逻辑：如果实盘资产为 0 且非模拟盘，尝试从账本历史中拉取最后一次有效资产
            if (!useSimulation && totalAsset <= 0) {
                try {
                    const history = await realTradingService.getAccountLedgerDaily(1, userId, tenantId);
                    if (history && history.length > 0) {
                        totalAsset = this.pickFirstNumber([history[0].total_asset], 0);
                        console.info('Using fallback asset from ledger:', totalAsset);
                    }
                } catch (ledgerErr) {
                    console.warn('Failed to fetch fallback asset from ledger:', ledgerErr);
                }
            }

            if (totalAsset <= 0 && useSimulation) {
                totalAsset = DEFAULT_INITIAL_CAPITAL;
            }

            const metrics = (account && typeof account.metrics === 'object' && account.metrics)
                ? account.metrics
                : {};
            const metricsMeta = (account && typeof account.metrics_meta === 'object' && account.metrics_meta)
                ? account.metrics_meta
                : {};

            let initialCapital = Number.isFinite(totalAsset)
                ? Math.max(0, totalAsset - this.pickFirstNumber([account.total_pnl, account.total_pnl_raw, metrics.total_pnl], 0))
                : 0;
            let initialCapitalEstimated = false;
            
            // 兜底：如果算出来的初始权益为 0 或与总资产相等，且数据库里有明确的 initial_equity 字段，则优先使用
            const dbInitialEquity = this.pickFirstNumber([account.initial_equity], 0);
            if (dbInitialEquity > 0 && (initialCapital <= 0 || initialCapital === totalAsset)) {
                initialCapital = dbInitialEquity;
            }

            if (useSimulation) {
                try {
                    const settings = await realTradingService.getSimulationSettings();
                    const configuredInitialCash = this.pickFirstNumber(
                        [settings?.initial_cash],
                        0,
                    );
                    if (configuredInitialCash > 0) {
                        initialCapital = configuredInitialCash;
                    }
                } catch {
                    // ignore settings read failure and fallback to account/default values
                }
            }
            if (initialCapital <= 0) {
                if (useSimulation) {
                    initialCapital = DEFAULT_INITIAL_CAPITAL;
                } else {
                    // 实盘未知初始权益时，不再用当前总资产硬回退，避免总收益率长期假 0。
                    initialCapital = totalAsset;
                    initialCapitalEstimated = true;
                }
            }

            const totalPnL = this.pickFirstNumber([account.total_pnl, account.total_pnl_raw, metrics.total_pnl], 0);
            const todayPnL = this.pickFirstNumber([account.daily_pnl, account.today_pnl, account.today_pnl_raw, metrics.daily_pnl, metrics.today_pnl], 0);
            const monthlyPnLValue = this.pickFirstNumber([account.monthly_pnl, account.monthly_pnl_raw, metrics.monthly_pnl], Number.NaN);
            const dayOpenEquity = this.pickFirstNumber(
                [
                    account?.baseline?.day_open_equity,
                    account.day_open_equity,
                    account.initial_equity, // 增加 initial_equity 作为日初权益的最后保底
                    metricsMeta?.baseline?.day_open_equity,
                ],
                0,
            );
            const derivedDailyReturn = dayOpenEquity > 0 ? (todayPnL / dayOpenEquity) : (totalAsset > 0 ? (todayPnL / totalAsset) : 0);
            const dailyReturn = this.pickConsistentRatio(
                [
                    account.daily_return_ratio,
                    Number.isFinite(Number(account.daily_return_pct)) ? Number(account.daily_return_pct) / 100 : Number.NaN,
                    Number.isFinite(Number(account.daily_return)) ? Number(account.daily_return) / 100 : Number.NaN,
                    Number.isFinite(Number(metrics.daily_return)) ? Number(metrics.daily_return) / 100 : Number.NaN,
                ],
                derivedDailyReturn,
                {
                    requireDerived: dayOpenEquity > 0 && Number.isFinite(todayPnL),
                },
            );
            const derivedTotalReturn = initialCapital > 0 ? (totalPnL / initialCapital) : 0;
            const totalReturn = this.pickConsistentRatio(
                [
                    account.total_return_ratio,
                    Number.isFinite(Number(account.total_return_pct)) ? Number(account.total_return_pct) / 100 : Number.NaN,
                    Number.isFinite(Number(account.total_return)) ? Number(account.total_return) / 100 : Number.NaN,
                    Number.isFinite(Number(metrics.total_return)) ? Number(metrics.total_return) / 100 : Number.NaN,
                ],
                derivedTotalReturn,
                {
                    requireDerived: initialCapital > 0 && Number.isFinite(totalPnL),
                },
            );
            const availableBalance = this.pickFirstNumber(
                [account.cash, account.available_cash, account.available_balance],
                totalAsset,
            );
            const frozenBalance = this.pickFirstNumber(
                [account.frozen, account.frozen_balance],
                0,
            );
            
            // 胜率计算优化：优先使用持仓实时计算的胜率
            const positionsWinRate = calculatePositionsWinRate(account);
            const winRate = positionsWinRate.total > 0 
                ? positionsWinRate.winRate 
                : this.pickFirstNumber([metrics.win_rate, account.win_rate], 0);

            const maxDrawdown = this.pickFirstNumber([account.max_drawdown], 0);
            const sharpeRatio = this.pickFirstNumber([account.sharpe_ratio], 0);
            const monthlyPnL = this.toNumber(monthlyPnLValue, NaN);
            const accountOnline = useSimulation
                ? undefined
                : Boolean(account.is_online === true);
            const todayPnLAvailable = this.boolOr(
                metricsMeta.today_pnl_available,
                Number.isFinite(Number(metrics.daily_pnl ?? metrics.today_pnl ?? account.daily_pnl ?? account.today_pnl)),
            );
            const dailyReturnAvailable = this.boolOr(
                (metricsMeta as any).daily_return_available,
                Number.isFinite(dailyReturn),
            );
            const totalPnLAvailable = this.boolOr(
                metricsMeta.total_pnl_available,
                Number.isFinite(Number(account.total_pnl ?? metrics.total_pnl)),
            );
            const totalReturnAvailable = this.boolOr(
                metricsMeta.total_return_available,
                initialCapital > 0 && Number.isFinite(totalReturn),
            );
            const monthlyPnLAvailable = this.boolOr(
                metricsMeta.monthly_pnl_available,
                Number.isFinite(monthlyPnL),
            );
            const metricsSource = String(
                metricsMeta.total_pnl_source ||
                metricsMeta.today_pnl_source ||
                (useSimulation ? 'simulation_account' : 'account_snapshot')
            );

            return {
                data: {
                    totalAsset,
                    availableBalance,
                    frozenBalance,
                    todayPnL,
                    dailyReturn: Math.round(dailyReturn * 10000) / 100,
                    totalPnL,
                    totalReturn: Math.round(totalReturn * 10000) / 100,
                    initialCapital,
                    initialCapitalEstimated,
                    winRate,
                    maxDrawdown,
                    sharpeRatio,
                    monthlyPnL: Number.isFinite(monthlyPnL) ? monthlyPnL : undefined,
                    todayPnLAvailable,
                    dailyReturnAvailable,
                    totalPnLAvailable,
                    totalReturnAvailable,
                    monthlyPnLAvailable,
                    metricsSource,
                    metricsMeta,
                    accountOnline,
                    lastUpdate: new Date().toISOString(),
                },
                isSimulated: useSimulation,
            };
        } catch (error) {
            console.warn(`获取${mode === 'real' ? '实盘' : '模拟'}账户数据失败:`, error);
            // 降级：如果是模拟盘失败，返回默认数据；如果是实盘失败，抛出错误由上层处理或返回空数据
            if (mode === 'simulation') {
                return {
                    data: this.getDefaultFundData(),
                    isSimulated: true,
                };
            }
            throw error;
        }
    }

    /**
     * 获取首次登录 / 后端不可用时的默认资金数据
     */
    getDefaultFundData(): FundData {
        return {
            totalAsset: DEFAULT_INITIAL_CAPITAL,
            availableBalance: DEFAULT_INITIAL_CAPITAL,
            frozenBalance: 0,
            todayPnL: 0,
            dailyReturn: 0,
            totalPnL: 0,
            totalReturn: 0,
            initialCapital: DEFAULT_INITIAL_CAPITAL,
            initialCapitalEstimated: false,
            winRate: 0,
            maxDrawdown: 0,
            sharpeRatio: 0,
            todayPnLAvailable: true,
            dailyReturnAvailable: true,
            totalPnLAvailable: true,
            totalReturnAvailable: true,
            monthlyPnLAvailable: false,
            metricsSource: 'default_fallback',
            lastUpdate: new Date().toISOString(),
        };
    }
}

export const portfolioService = new PortfolioService();
