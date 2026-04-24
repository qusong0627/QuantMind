import axios, { AxiosHeaders } from 'axios';
import { SERVICE_ENDPOINTS } from '../config/services';
import { authService } from '../features/auth/services/authService';
import type { ExecutionConfig, LiveTradeConfig } from '../types/liveTrading';

function getTenantId(): string {
    const fromEnv = String((import.meta as any).env?.VITE_TENANT_ID || '').trim();
    return fromEnv || 'default';
}

const configuredRealTradingApiUrl = String((import.meta as any).env?.VITE_REAL_TRADING_API_URL || '').trim();
const apiBase =
    configuredRealTradingApiUrl ||
    `${SERVICE_ENDPOINTS.API_GATEWAY}/real-trading`;
const configuredRealTradingDirectUrl = String((import.meta as any).env?.VITE_REAL_TRADING_DIRECT_URL || '').trim();
// OSS 版本使用相对路径，通过 Nginx 代理
const directApiBase = configuredRealTradingDirectUrl || '/api/v1/real-trading';
const normalizedApiBase = apiBase.replace(/\/+$/, '');
const normalizedDirectApiBase = directApiBase.replace(/\/+$/, '');
const hasDistinctDirectFallback = normalizedApiBase !== normalizedDirectApiBase;

function createHttpClient(baseURL: string) {
    const client = axios.create({
        baseURL: baseURL.replace(/\/+$/, ''),
        timeout: 30000,
    });

    client.interceptors.request.use((config) => {
        const token = authService.getAccessToken();
        if (token) {
            if (!config.headers) {
                config.headers = new AxiosHeaders();
            }
            config.headers.set('Authorization', `Bearer ${token}`);
        }
        return config;
    });

    client.interceptors.response.use(
        (response) => response,
        async (error) => {
            if (error.response?.status === 401) {
                return authService.handle401Error(error, client);
            }
            return Promise.reject(error);
        }
    );

    return client;
}

const http = createHttpClient(apiBase);
const directHttp = createHttpClient(directApiBase);

function shouldUseDirectFallback(error: any): boolean {
    if (!hasDistinctDirectFallback) {
        return false;
    }
    const status = Number(error?.response?.status ?? 0);
    if (!status) {
        return true;
    }
    return status === 502 || status === 503 || status === 504;
}

type RealTradingRequestConfig = {
    method: 'get' | 'post' | 'put' | 'delete' | 'patch';
    url: string;
    params?: Record<string, unknown>;
    data?: unknown;
    headers?: Record<string, string>;
};

async function requestRealTradingWithFallback<T>(
    config: RealTradingRequestConfig,
    allowFallback: boolean = true,
): Promise<T> {
    try {
        const response = await http.request<T>(config as any);
        return response.data;
    } catch (error: any) {
        if (!allowFallback || !shouldUseDirectFallback(error)) {
            throw error;
        }
        const response = await directHttp.request<T>(config as any);
        return response.data;
    }
}

export interface RealTradingStatus {
    status: 'running' | 'stopped' | 'not_running' | 'starting' | 'error';
    user_id: string;
    mode?: 'REAL' | 'SHADOW' | 'SIMULATION';
    orchestration_mode?: 'docker' | 'k8s';
    message?: string;
    daily_pnl?: number | null;
    daily_return?: number | null;
    portfolio?: {
        portfolio_id?: number;
        daily_pnl?: number | null;
        daily_return?: number | null;
        total_pnl?: number | null;
        total_return?: number | null;
        total_value?: number | null;
        initial_capital?: number | null;
        run_status?: string | null;
        updated_at?: string | null;
        position_count?: number | null;
    } | null;
    k8s_status?: {
        name: string;
        replicas: number;
        ready_replicas: number;
        available_replicas: number;
        unavailable_replicas: number;
    };
    strategy?: {
        id: string;
        name: string;
        description: string;
    };
    execution_config?: ExecutionConfig | null;
    live_trade_config?: LiveTradeConfig | null;
    latest_hosted_task?: ManualExecutionTaskRecord | null;
    latest_signal_run_id?: string | null;
    signal_source_status?: {
        available: boolean;
        source?: 'inference' | 'fallback' | 'missing' | 'window_pending' | 'expired' | 'mismatch' | string;
        message?: string;
        execution_window_start?: string;
        execution_window_end?: string;
    } | null;
}

export interface RealTradingLogs {
    tenant_id?: string;
    user_id: string;
    logs: string;
}

export interface ManualExecutionTaskRecord {
    task_id: string;
    tenant_id: string;
    user_id: string;
    strategy_id: string;
    strategy_name: string;
    run_id: string;
    model_id: string;
    prediction_trade_date: string;
    trading_mode: 'REAL' | 'SHADOW' | 'SIMULATION' | string;
    status: 'queued' | 'validating' | 'dispatching' | 'running' | 'completed' | 'failed' | string;
    stage?: string;
    error_stage?: string | null;
    error_message?: string | null;
    signal_count?: number;
    order_count?: number;
    success_count?: number;
    failed_count?: number;
    progress?: number;
    task_type?: 'manual' | 'hosted' | string;
    task_source?: string;
    trigger_mode?: 'manual' | 'schedule' | string;
    trigger_context_json?: Record<string, unknown> | null;
    strategy_snapshot_json?: Record<string, unknown> | null;
    parent_runtime_id?: string | null;
    request_json?: Record<string, unknown> | null;
    result_json?: Record<string, unknown> | null;
    created_at?: string;
    updated_at?: string;
}

export interface ManualExecutionLogEntry {
    id: string;
    task_id: string;
    tenant_id: string;
    user_id: string;
    line: string;
    ts: string;
    level: string;
    stage?: string;
    status?: string;
    progress?: number;
    signal_index?: number;
    order_index?: number;
    summary?: Record<string, unknown> | string;
}

export interface ManualExecutionLogSnapshot {
    task_id: string;
    status?: string;
    stage?: string;
    progress?: number;
    signal_count?: number;
    order_count?: number;
    success_count?: number;
    failed_count?: number;
    error_stage?: string;
    error_message?: string;
    updated_at?: string;
    last_line?: string;
    summary?: Record<string, unknown>;
    logs_tail?: string;
}

export interface ManualExecutionLogsResponse {
    entries: ManualExecutionLogEntry[];
    next_id: string;
    snapshot: ManualExecutionLogSnapshot | null;
    task?: ManualExecutionTaskRecord;
}

export interface ManualExecutionPreviewOrder {
    symbol: string;
    name?: string;
    side: 'BUY' | 'SELL' | string;
    trade_action?: string;
    quantity: number;
    order_type: 'LIMIT' | 'MARKET' | string;
    price?: number;
    reference_price?: number;
    estimated_notional?: number;
    current_volume?: number;
    current_market_value?: number;
    reason?: string;
    fusion_score?: number;
}

export interface ManualExecutionPreviewSkippedItem {
    symbol: string;
    action: 'BUY' | 'SELL' | string;
    reason: string;
    source?: string;
}

export interface ManualExecutionPreview {
    preview_hash: string;
    account_snapshot: {
        account_id?: string;
        snapshot_at?: string;
        total_asset: number;
        available_cash: number;
        market_value: number;
        position_count: number;
    };
    strategy_context: {
        model_id: string;
        run_id: string;
        prediction_trade_date: string;
        strategy_id: string;
        strategy_name: string;
        trading_mode: 'REAL' | string;
        strategy_params?: Record<string, unknown>;
        note?: string | null;
    };
    sell_orders: ManualExecutionPreviewOrder[];
    buy_orders: ManualExecutionPreviewOrder[];
    skipped_items: ManualExecutionPreviewSkippedItem[];
    summary: {
        signal_count?: number;
        buy_candidate_count?: number;
        sell_candidate_count?: number;
        sell_order_count?: number;
        buy_order_count?: number;
        skipped_count?: number;
        estimated_sell_proceeds?: number;
        estimated_buy_amount?: number;
        estimated_remaining_cash?: number;
        available_cash?: number;
        inferred_signal_plan?: boolean;
        strategy_type?: string;
        topk?: number;
        n_drop?: number;
    };
}


export interface Order {
    id: number;
    order_id: string;
    symbol: string;
    symbol_name?: string;
    side: 'buy' | 'sell';
    order_type: string;
    status: string;
    quantity: number;
    price?: number;
    order_value: number;
    filled_quantity: number;
    average_price?: number;
    filled_value?: number;
    trade_action?: string;
    submitted_at?: string;
    created_at: string;
    filled_at?: string;
    client_order_id?: string;
    exchange_order_id?: string;
}

export interface OrdersQueryOptions {
    portfolioId?: number;
    symbol?: string;
    startDate?: string;
    endDate?: string;
    limit?: number;
    offset?: number;
}

export interface Trade {
    id: number;
    trade_id: string;
    symbol: string;
    symbol_name?: string;
    side: 'buy' | 'sell';
    quantity: number;
    price: number;
    trade_value: number;
    commission: number;
    executed_at: string;
}

export interface VerifiedStrategy {
    id: string;
    name: string;
    description: string;
    status: string;
}

export interface SimulationSettings {
    initial_cash: number;
    last_modified_at?: string | null;
    next_allowed_modified_at?: string | null;
    can_modify: boolean;
    cooldown_days: number;
    amount_step: number;
}

export interface SimulationFundSnapshot {
    snapshot_date: string;
    total_asset: string;
    available_balance: string;
    frozen_balance: string;
    market_value: string;
    initial_capital: string;
    total_pnl: string;
    today_pnl: string;
    source: string;
}

export interface RealAccountLedgerDailySnapshot {
    account_id?: string;
    snapshot_date: string;
    last_snapshot_at?: string;
    snapshot_kind?: 'daily_ledger';
    total_asset: number;
    cash: number;
    market_value: number;
    initial_equity: number;
    day_open_equity: number;
    month_open_equity: number;
    broker_today_pnl_raw?: number;
    today_pnl_raw: number;
    monthly_pnl_raw: number;
    total_pnl_raw: number;
    floating_pnl_raw: number;
    daily_pnl?: number;
    monthly_pnl?: number;
    total_pnl?: number;
    floating_pnl?: number;
    daily_return_pct: number;
    total_return_pct: number;
    daily_return_ratio?: number;
    total_return_ratio?: number;
    baseline?: {
        initial_equity: number;
        day_open_equity: number;
        month_open_equity: number;
    };
    position_count: number;
    source: string;
}

export interface StartTradingResponse {
    status: string;
    message?: string;
    effective_execution_config?: ExecutionConfig;
    effective_live_trade_config?: LiveTradeConfig;
    k8s_result?: any;
}

export interface PreflightCheckItem {
    key: string;
    label: string;
    ok: boolean;
    required: boolean;
    message: string;
    details?: Record<string, any>;
}

export interface PreflightCheckResponse {
    ready: boolean;
    mode: 'REAL' | 'SHADOW' | 'SIMULATION';
    user_id: string;
    tenant_id: string;
    checked_at?: string;
    checks: PreflightCheckItem[];
}

export interface TradingPrecheckItem {
    key: string;
    label: string;
    passed: boolean;
    detail: string;
}

export interface TradingPrecheckResult {
    passed: boolean;
    checked_at: string;
    items: TradingPrecheckItem[];
}

export interface TradingPrecheckFailure {
    message: string;
    checked_at?: string;
    items: TradingPrecheckItem[];
    first_failed_reason?: string;
}

function resolveErrorMessage(error: any): string {
    const status = error?.response?.status;
    const detail = error?.response?.data?.detail;
    const messageFromBody = error?.response?.data?.message;
    if (detail && typeof detail === 'object' && typeof detail.message === 'string') {
        return detail.message;
    }
    const msg = typeof detail === 'string'
        ? detail
        : (typeof messageFromBody === 'string' ? messageFromBody : '');

    if (msg) return msg;
    if (status === 401) return '登录已过期，请重新登录';
    if (status === 403) return '无权限访问交易服务';
    if (status === 404) return '交易资源不存在';
    if (status === 429) return '请求过于频繁，请稍后重试';
    if (status === 502 || status === 503 || status === 504) {
        return '交易服务暂不可用，请检查网关、交易后端与本机直连地址';
    }
    if (!status) {
        return '交易服务暂时不可达，请检查网关、交易容器、本机直连地址与网络连通性';
    }
    return error?.message || '交易请求失败';
}

function buildConfigWarning(): string | null {
    if (!configuredRealTradingApiUrl) return null;
    const normalized = configuredRealTradingApiUrl.replace(/\/+$/, '');
    const validPattern = /\/api\/v1\/real-trading$/;
    if (!validPattern.test(normalized)) {
        return 'VITE_REAL_TRADING_API_URL 建议指向 /api/v1/real-trading；若无直连需求，可留空并走网关默认地址';
    }
    return null;
}

const bindingStatusCache = new Map<string, { expiresAt: number; promise: Promise<QmtBindingStatus | null> }>();
const BINDING_STATUS_TTL_MS = 15000;

function buildUnavailableRealAccount(
    reason: 'unbound' | 'not_reported' | 'not_found',
    message: string,
    bindingStatus?: QmtBindingStatus | null,
): AccountInfo {
    return {
        account_id: bindingStatus?.account_id || undefined,
        total_asset: 0,
        cash: 0,
        available_cash: 0,
        frozen_cash: 0,
        market_value: 0,
        today_pnl: 0,
        daily_pnl: 0,
        monthly_pnl: 0,
        total_pnl: 0,
        total_return: 0,
        total_return_pct: 0,
        daily_return_pct: 0,
        daily_return_ratio: 0,
        total_return_ratio: 0,
        floating_pnl: 0,
        is_online: false,
        positions: [],
        position_count: 0,
        message,
        account_unavailable_reason: reason,
        baseline: {
            initial_equity: 0,
            day_open_equity: 0,
            month_open_equity: 0,
        },
    };
}

export const realTradingService = {
    getFriendlyError: (error: any): string => resolveErrorMessage(error),
    getConfigWarning: (): string | null => buildConfigWarning(),
    getQmtBindingStatus: async (
        userId: string,
        tenantId: string = getTenantId(),
        options?: { force?: boolean },
    ): Promise<QmtBindingStatus | null> => {
        const disableCache = String((import.meta as any).env?.MODE || '').toLowerCase() === 'test';
        const cacheKey = `${tenantId}:${String(userId || '').trim() || 'current'}`;
        const now = Date.now();
        const cached = bindingStatusCache.get(cacheKey);
        if (!disableCache && !options?.force && cached && cached.expiresAt > now) {
            return cached.promise;
        }

        const token = authService.getAccessToken();
        const promise = axios.get(
            `${SERVICE_ENDPOINTS.API_GATEWAY}/internal/strategy/bridge/binding/status`,
            {
                params: { user_id: userId },
                headers: token ? new AxiosHeaders({ Authorization: `Bearer ${token}` }) : undefined,
                timeout: 15000,
            },
        )
            .then((response) => response.data as QmtBindingStatus)
            .catch((error) => {
                const status = Number(error?.response?.status ?? 0);
                if (status === 401 || status === 403) {
                    throw error;
                }
                return null;
            });

        if (!disableCache) {
            bindingStatusCache.set(cacheKey, {
                expiresAt: now + BINDING_STATUS_TTL_MS,
                promise,
            });
        }
        return promise;
    },
    extractTradingPrecheckFailure: (error: any): TradingPrecheckFailure | null => {
        const detail = error?.response?.data?.detail;
        if (!detail || typeof detail !== 'object' || detail.precheck_failed !== true) {
            return null;
        }
        return {
            message: typeof detail.message === 'string' ? detail.message : '次日预测排名准备度未通过',
            checked_at: typeof detail.checked_at === 'string' ? detail.checked_at : undefined,
            items: Array.isArray(detail.items) ? detail.items : [],
            first_failed_reason: typeof detail.first_failed_reason === 'string' ? detail.first_failed_reason : undefined,
        };
    },

    preflight: async (
        tradingMode: 'REAL' | 'SHADOW' | 'SIMULATION',
        _userId: string,
        _tenantId: string = getTenantId()
    ): Promise<PreflightCheckResponse> => {
        return await requestRealTradingWithFallback<PreflightCheckResponse>({
            method: 'get',
            url: '/preflight',
            params: {
                trading_mode: tradingMode,
            },
        });
    },

    getTradingPrecheck: async (
        tradingMode: 'REAL' | 'SHADOW' | 'SIMULATION'
    ): Promise<TradingPrecheckResult> => {
        return await requestRealTradingWithFallback<TradingPrecheckResult>({
            method: 'get',
            url: '/trading-precheck',
            params: {
                trading_mode: tradingMode,
            },
        });
    },

    // Start Real Trading using strategy ID and mode
    start: async (
        _userId: string,
        strategyId: string,
        tradingMode: string = 'REAL',
        _tenantId: string = getTenantId(),
        executionConfig?: ExecutionConfig,
        liveTradeConfig?: LiveTradeConfig
    ): Promise<StartTradingResponse> => {
        const formData = new FormData();
        formData.append('strategy_id', strategyId);
        formData.append('trading_mode', tradingMode);
        if (executionConfig) {
            formData.append('execution_config', JSON.stringify(executionConfig));
        }
        if (liveTradeConfig) {
            formData.append('live_trade_config', JSON.stringify(liveTradeConfig));
        }

        return await requestRealTradingWithFallback<StartTradingResponse>({
            method: 'post',
            url: '/start',
            data: formData,
            headers: {
                'Content-Type': 'multipart/form-data',
            },
        });
    },

    // Stop Real Trading
    stop: async (_userId: string, _tenantId: string = getTenantId()) => {
        return await requestRealTradingWithFallback({
            method: 'post',
            url: '/stop',
            data: {},
        });
    },

    // Get Status
    getStatus: async (userId?: string, tradingMode?: string, tenantId: string = getTenantId()): Promise<RealTradingStatus> => {
        const actualUserId = userId || (authService.getStoredUser() as any)?.user_id || (authService.getStoredUser() as any)?.sub || '';
        return await requestRealTradingWithFallback<RealTradingStatus>({
            method: 'get',
            url: '/status',
            params: {
                user_id: actualUserId,
                tenant_id: tenantId,
                trading_mode: tradingMode?.toUpperCase(),
            },
        });
    },

    // Get Logs
    getLogs: async (_userId: string, tail: number = 100, _tenantId: string = getTenantId()): Promise<RealTradingLogs> => {
        return await requestRealTradingWithFallback<RealTradingLogs>({
            method: 'get',
            url: '/logs',
            params: { tail },
        });
    },

    previewManualExecution: async (payload: {
        model_id: string;
        run_id: string;
        strategy_id: string;
        trading_mode?: 'REAL';
        note?: string;
    }): Promise<ManualExecutionPreview> => {
        return await requestRealTradingWithFallback<ManualExecutionPreview>({
            method: 'post',
            url: '/manual-executions/preview',
            data: payload,
        });
    },

    createManualExecution: async (payload: {
        model_id: string;
        run_id: string;
        strategy_id: string;
        trading_mode?: 'REAL' | 'SHADOW' | 'SIMULATION';
        preview_hash?: string;
        note?: string;
    }): Promise<{ status: string; task_id: string; task?: ManualExecutionTaskRecord; preview_summary?: Record<string, unknown> }> => {
        return await requestRealTradingWithFallback<{
            status: string;
            task_id: string;
            task?: ManualExecutionTaskRecord;
            preview_summary?: Record<string, unknown>;
        }>({
            method: 'post',
            url: '/manual-executions',
            data: payload,
        });
    },

    listManualExecutions: async (
        limit: number = 10,
        filters?: {
            task_type?: 'manual' | 'hosted' | string;
            task_source?: string;
            active_runtime_id?: string;
        },
    ): Promise<{ items: ManualExecutionTaskRecord[]; total: number; limit: number }> => {
        return await requestRealTradingWithFallback<{
            items: ManualExecutionTaskRecord[];
            total: number;
            limit: number;
        }>({
            method: 'get',
            url: '/manual-executions',
            params: {
                limit,
                task_type: filters?.task_type,
                task_source: filters?.task_source,
                active_runtime_id: filters?.active_runtime_id,
            },
        });
    },

    clearManualExecutions: async (): Promise<{ cleared_count: number }> => {
        return await requestRealTradingWithFallback<{ cleared_count: number }>({
            method: 'delete',
            url: '/manual-executions',
        });
    },

    getManualExecution: async (taskId: string): Promise<ManualExecutionTaskRecord> => {
        return await requestRealTradingWithFallback<ManualExecutionTaskRecord>({
            method: 'get',
            url: `/manual-executions/${taskId}`,
        });
    },

    getManualExecutionLogs: async (
        taskId: string,
        afterId: string = '0-0',
        limit: number = 200,
    ): Promise<ManualExecutionLogsResponse> => {
        return await requestRealTradingWithFallback<ManualExecutionLogsResponse>({
            method: 'get',
            url: `/manual-executions/${taskId}/logs`,
            params: {
                after_id: afterId,
                limit,
            },
        });
    },

    // Get Orders
    getOrders: async (
        userId: string,
        status?: string,
        tradingMode?: 'real' | 'simulation' | 'REAL' | 'SIMULATION' | 'SHADOW',
        options?: OrdersQueryOptions,
    ): Promise<Order[]> => {
        const token = authService.getAccessToken();
        const normalizedStatus = status ? status.toUpperCase() : undefined;
        const normalizedTradingMode = tradingMode ? tradingMode.toUpperCase() : undefined;
        const normalizedRoute = normalizedTradingMode === 'SIMULATION'
            ? `${SERVICE_ENDPOINTS.API_GATEWAY}/simulation/orders`
            : `${SERVICE_ENDPOINTS.API_GATEWAY}/orders`;
        const response = await axios.get(normalizedRoute, {
            params: {
                user_id: userId,
                status: normalizedStatus,
                trading_mode: normalizedTradingMode,
                portfolio_id: options?.portfolioId,
                symbol: options?.symbol,
                start_date: options?.startDate,
                end_date: options?.endDate,
                limit: options?.limit,
                offset: options?.offset,
            },
            headers: token ? new AxiosHeaders({ Authorization: `Bearer ${token}` }) : undefined,
            timeout: 30000,
        });
        return response.data;
    },

    // Get Account Info
    getAccount: async (userId: string, tenantId: string = getTenantId()): Promise<AccountInfo> => {
        const bindingStatus = await realTradingService.getQmtBindingStatus(userId, tenantId).catch((error) => {
            const status = Number(error?.response?.status ?? 0);
            if (status === 401 || status === 403) {
                throw error;
            }
            return null;
        });

        if (bindingStatus && !bindingStatus.account_id) {
            return buildUnavailableRealAccount('unbound', '当前账户未绑定实盘交易账号', bindingStatus);
        }
        if (bindingStatus?.account_id && !bindingStatus.account_reported_at) {
            return buildUnavailableRealAccount('not_reported', '实盘账号尚未上报账户快照，请启动 QMT Agent', bindingStatus);
        }

        try {
            return await requestRealTradingWithFallback<AccountInfo>({
                method: 'get',
                url: '/account',
            });
        } catch (error: any) {
            const status = Number(error?.response?.status ?? 0);
            if (status === 401 || status === 403) {
                throw error;
            }
            if (status === 404) {
                const message = bindingStatus?.account_id
                    ? '实盘账号尚未上报账户快照，请启动 QMT Agent'
                    : '当前账户未绑定实盘交易账号';
                return buildUnavailableRealAccount(
                    bindingStatus?.account_id ? 'not_reported' : 'unbound',
                    message,
                    bindingStatus,
                );
            }
            throw error;
        }
    },

    // Get Account Info by Runtime Mode
    getRuntimeAccount: async (
        userId: string,
        tenantId: string = getTenantId(),
        runtimeMode?: string | null,
    ): Promise<AccountInfo | null> => {
        const normalizedMode = String(runtimeMode || '').trim().toUpperCase();
        if (normalizedMode === 'SIMULATION') {
            return await realTradingService.getSimulationAccount(userId, tenantId).catch(() => null);
        }
        return await realTradingService.getAccount(userId, tenantId).catch(() => null);
    },

    // Get Simulation Account Info
    getSimulationAccount: async (_userId: string, _tenantId: string = getTenantId()): Promise<AccountInfo | null> => {
        const token = authService.getAccessToken();
        const response = await axios.get(`${SERVICE_ENDPOINTS.API_GATEWAY}/simulation/account`, {
            headers: token ? new AxiosHeaders({ Authorization: `Bearer ${token}` }) : undefined,
            timeout: 30000,
        });
        return response.data?.data || null;
    },

    getAccountLedgerDaily: async (
        days: number = 30,
        _userId: string = 'current',
        _tenantId: string = getTenantId(),
        accountId?: string,
    ): Promise<RealAccountLedgerDailySnapshot[]> => {
        const params: Record<string, string | number> = { days };
        if (accountId) {
            params.account_id = accountId;
        }
        return await requestRealTradingWithFallback<RealAccountLedgerDailySnapshot[]>({
            method: 'get',
            url: '/account/ledger/daily',
            params,
        }).then((data) => Array.isArray(data) ? data : []);
    },

    // Reset Simulation Account
    resetSimulationAccount: async (
        _userId: string,
        initialCash: number,
        _tenantId: string = getTenantId()
    ): Promise<AccountInfo | null> => {
        const token = authService.getAccessToken();
        const response = await axios.post(
            `${SERVICE_ENDPOINTS.API_GATEWAY}/simulation/reset`,
            { initial_cash: initialCash },
            {
                headers: token ? new AxiosHeaders({ Authorization: `Bearer ${token}` }) : undefined,
                timeout: 30000,
            }
        );
        return response.data?.data || null;
    },

    // Get Simulation Settings
    getSimulationSettings: async (): Promise<SimulationSettings | null> => {
        const token = authService.getAccessToken();
        const response = await axios.get(`${SERVICE_ENDPOINTS.API_GATEWAY}/simulation/settings`, {
            headers: token ? new AxiosHeaders({ Authorization: `Bearer ${token}` }) : undefined,
            timeout: 30000,
        });
        return response.data?.data || null;
    },

    // Update Simulation Settings
    updateSimulationSettings: async (initialCash: number): Promise<SimulationSettings | null> => {
        const token = authService.getAccessToken();
        const response = await axios.put(
            `${SERVICE_ENDPOINTS.API_GATEWAY}/simulation/settings`,
            { initial_cash: initialCash },
            {
                headers: token ? new AxiosHeaders({ Authorization: `Bearer ${token}` }) : undefined,
                timeout: 30000,
            }
        );
        return response.data?.data || null;
    },

    // Get Simulation Fund Snapshots (from DB table simulation_fund_snapshots)
    getSimulationDailySnapshots: async (days: number = 1): Promise<SimulationFundSnapshot[]> => {
        const token = authService.getAccessToken();
        const response = await axios.get(`${SERVICE_ENDPOINTS.API_GATEWAY}/simulation/snapshots/daily`, {
            params: { days },
            headers: token ? new AxiosHeaders({ Authorization: `Bearer ${token}` }) : undefined,
            timeout: 30000,
        });
        return Array.isArray(response.data) ? response.data : [];
    },

    // Get Real Account Settings
    getRealAccountSettings: async (): Promise<{ initial_equity: number; last_modified_at?: string; can_modify: boolean } | null> => {
        try {
            return await requestRealTradingWithFallback<{ initial_equity: number; last_modified_at?: string; can_modify: boolean }>({
                method: 'get',
                url: '/account/settings',
            });
        } catch (err) {
            console.error('Failed to get real account settings', err);
            return null;
        }
    },

    // Update Real Account Settings
    updateRealAccountSettings: async (initialEquity: number): Promise<boolean> => {
        try {
            const response = await requestRealTradingWithFallback<{ success?: boolean }>({
                method: 'put',
                url: '/account/settings',
                data: { initial_equity: initialEquity },
            });
            return response?.success === true;
        } catch (err) {
            console.error('Failed to update real account settings', err);
            return false;
        }
    }
};

export interface AccountInfo {
    account_id?: string;
    snapshot_kind?: 'account_snapshot';
    timestamp?: string | number;
    total_asset: number;
    /** 可用资金（后端归一化：QMT available_cash 为 0 时 fallback 到 cash） */
    cash: number;
    available_cash?: number;
    /** QMT 上报的冻结资金（委托冻结 + 资产缺口冻结） */
    frozen_cash?: number;
    frozen?: number;
    market_value: number;
    today_pnl?: number;
    daily_pnl?: number;
    daily_return?: number;
    monthly_pnl?: number;
    total_pnl?: number;
    total_return?: number;
    total_return_pct?: number;
    daily_return_pct?: number;
    daily_return_ratio?: number;
    total_return_ratio?: number;
    floating_pnl?: number;
    broker_today_pnl_raw?: number;
    initial_equity?: number;
    day_open_equity?: number;
    month_open_equity?: number;
    baseline?: {
        initial_equity: number;
        day_open_equity: number;
        month_open_equity: number;
    };
    is_online?: boolean;
    message?: string;
    account_unavailable_reason?: 'unbound' | 'not_reported' | 'not_found';
    position_count?: number;
    positions: Array<{
        symbol?: string;
        volume?: number;
        available_volume?: number;
        market_value?: number;
        price?: number;
        last_price?: number;
        cost_price?: number;
    }> | Record<string, {
        volume?: number;
        market_value?: number;
        price?: number;
        last_price?: number;
        cost_price?: number;
    }>;
}

export interface QmtBindingStatus {
    online: boolean;
    user_id: string;
    tenant_id: string;
    account_id?: string | null;
    hostname?: string | null;
    client_version?: string | null;
    last_seen_at?: string | null;
    heartbeat_at?: string | null;
    account_reported_at?: string | null;
    stale_reason?: string | null;
}
