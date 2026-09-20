import axios, { AxiosHeaders } from 'axios';
import { SERVICE_ENDPOINTS, SERVICE_URLS } from '../config/services';
import { authService } from '../features/auth/services/authService';
import type { ExecutionConfig, LiveTradeConfig } from '../types/liveTrading';

function getTenantId(): string {
    const fromEnv = String((import.meta as any).env?.VITE_TENANT_ID || '').trim();
    return fromEnv || 'default';
}

const configuredRealTradingApiUrl = String((import.meta as any).env?.VITE_REAL_TRADING_API_URL || '').trim();
const configuredRealTradingDirectUrl = String((import.meta as any).env?.VITE_REAL_TRADING_DIRECT_URL || '').trim();

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

function getRuntimeRealTradingApiBase(): string {
    return (
        configuredRealTradingApiUrl ||
        `${SERVICE_ENDPOINTS.API_GATEWAY}/real-trading`
    ).replace(/\/+$/, '');
}

// 仅在显式配置直连地址时启用 fallback，避免开发环境误打到本地 Vite 地址。
function getRuntimeRealTradingDirectApiBase(): string | null {
    const normalized = configuredRealTradingDirectUrl.replace(/\/+$/, '');
    return normalized || null;
}

function hasDistinctDirectFallback(): boolean {
    const direct = getRuntimeRealTradingDirectApiBase();
    if (!direct) return false;
    return direct !== getRuntimeRealTradingApiBase();
}

function shouldUseDirectFallback(error: any): boolean {
    if (!hasDistinctDirectFallback()) {
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
    // 重接口（手动任务预览/创建）可显式放宽超时，默认沿用 client 的 30s。
    timeout?: number;
};

async function requestRealTradingWithFallback<T>(
    config: RealTradingRequestConfig,
    allowFallback: boolean = true,
): Promise<T> {
    const http = createHttpClient(getRuntimeRealTradingApiBase());
    try {
        const response = await http.request<T>(config as any);
        return response.data;
    } catch (error: any) {
        if (!allowFallback || !shouldUseDirectFallback(error)) {
            throw error;
        }
        const directApiBase = getRuntimeRealTradingDirectApiBase();
        if (!directApiBase) {
            throw error;
        }
        const directHttp = createHttpClient(directApiBase);
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
    trading_permission?: TradingPermission;
    signal_readiness?: SignalReadiness | null;
    // ── T-RC-15 市场闸门与配置版本（后端新增字段，全部可选以兼容旧后端）──
    /** 当前运行策略的归属市场（服务端判定），`null` 表示载荷未声明市场 */
    market?: string | null;
    /** 策略自身声明的市场（`parameters.market`） */
    strategy_market?: string | null;
    /** 市场判定依据：active/live_trade_config/strategy_params/undeclared… */
    market_source?: string | null;
    /** 页签市场与运行市场不一致时的话术；一致时为 null */
    market_gate?: string | null;
    /** 配置版本号；>0 说明发生过热更新（含 /start 的初值 1） */
    config_version?: number;
    config_updated_at?: string | null;
    /** 最近一个调仓周期的执行摘要（来自运行维度状态键） */
    latest_cycle?: {
        status?: string;
        stage?: string;
        at?: string;
        last_line?: string;
    } | null;
    /**
     * 风控口径分裂体检（D9）：`diverged=true` 说明「界面上配的止损」与
     * 「策略实际读到的止损」不一致——止损看着配了却不触发，最隐蔽的一类事故。
     * `null` 表示无从比对（未声明/无策略），**不是一致**。
     */
    execution_config_divergence?: {
        diverged: boolean;
        fields?: Record<string, { snapshot?: unknown; strategy?: unknown }>;
        message?: string;
    } | null;
    /**
     * 守护条心跳（T-RC-20）：托管循环是否还活着。
     * `state` 口径与体检 C07 一致：ok / stale / off / missing。
     * 空数组表示后端未提供该块（旧版本），**不等于「没有调度在跑」**。
     */
    schedulers?: Array<{
        key: string;
        name: string;
        enabled: boolean;
        state: 'ok' | 'stale' | 'off' | 'missing' | string;
        age: number | null;
        ttl: number | null;
    }>;
}

/** 单字段变更（`{from,to}`）；热更新响应里的 diff 用这个结构。 */
export interface RuntimeConfigFieldDiff {
    from?: unknown;
    to?: unknown;
}

/**
 * 热更新响应（`POST /runtime-config`）。
 *
 * `effective_at: 'next_cycle'` 是**事实陈述**：写入只落 Redis 快照，托管调度器
 * 下一周期重读才生效——界面必须照此措辞，不能显示成「已立即生效」。
 */
export interface RuntimeConfigUpdateResult {
    /** `'success'`（已写入）或 `'dry_run'`（预演，未写任何东西） */
    status: 'success' | 'dry_run' | string;
    message: string;
    config_version: number;
    previous_config_version?: number;
    effective_at: 'next_cycle' | string;
    effective_execution_config?: ExecutionConfig | null;
    effective_live_trade_config?: LiveTradeConfig | null;
    diff?: {
        execution_config?: Record<string, RuntimeConfigFieldDiff>;
        live_trade_config?: Record<string, RuntimeConfigFieldDiff>;
    };
    /** 改动是否触及调仓节奏（节奏变更会触发同日重复执行守卫） */
    rhythm_changed?: boolean;
    /** 今日已触发的阶段；非空且未 force 时后端回 409 */
    already_fired_phases?: string[];
    forced?: boolean;
    execution_config_sync?: { synced: boolean; reason?: string } | null;
    /** 恒为 true：热更新不触碰持仓（后端断言回传，供界面明示） */
    position_untouched?: boolean;
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
    /**
     * 来源（`hosted_sim` / `hosted_runner` / `manual` / `bootstrap` / `system`）。
     * 运行流（`/runtime-logs`）每条都有，由后端按 task_id 前缀推断；
     * 旧的单任务流（`/{task_id}/logs`）没有该字段，故可选。
     */
    source?: string;
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
    /** 申万行业（后端 _enrich_preview_display_fields 补，缺失为空串） */
    industry?: string;
    /** A 股上市板：科创板 / 创业板 / 中小板 / 深主板 / 沪主板 / 北交所 / 其他 */
    board?: string;
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
    name?: string;
    industry?: string;
    board?: string;
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
    /** 手续费（后端 OrderResponse 已返回；缺失表示该行没有这个数，不是 0） */
    commission?: number;
    /** 备注；撤单原因由后端追加为 `[CANCELLED: 原因]` */
    remarks?: string;
    trading_mode?: string;
    strategy_id?: number | null;
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
    trading_permission?: TradingPermission;
    signal_readiness?: SignalReadiness | null;
    k8s_result?: any;
}

export type TradingPermission = 'trade_enabled' | 'observe_only' | 'blocked' | string;

export interface SignalReadiness {
    available?: boolean;
    status?: string;
    message?: string;
    latest_run_id?: string | null;
    data_trade_date?: string | null;
    prediction_trade_date?: string | null;
    execution_window_start?: string | null;
    execution_window_end?: string | null;
    signal_count?: number;
    redis_latest_run_id?: string | null;
    trading_permission?: TradingPermission;
    blocking?: boolean;
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
    trading_permission?: TradingPermission;
    signal_readiness?: SignalReadiness | null;
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
    trading_permission?: TradingPermission;
    signal_readiness?: SignalReadiness | null;
}

export interface TradingPrecheckFailure {
    message: string;
    checked_at?: string;
    items: TradingPrecheckItem[];
    first_failed_reason?: string;
    trading_permission?: TradingPermission;
    signal_readiness?: SignalReadiness | null;
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

function buildUnavailableRealAccount(
    reason: 'unbound' | 'not_reported' | 'not_found',
    message: string,
): AccountInfo {
    return {
        account_id: undefined,
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
            trading_permission: typeof detail.trading_permission === 'string' ? detail.trading_permission : undefined,
            signal_readiness: detail.signal_readiness && typeof detail.signal_readiness === 'object' ? detail.signal_readiness : null,
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
    /** 停止策略。`reason` 落服务端审计与运行日志（T-RC-19 停止留痕）。 */
    stop: async (
        _userId: string,
        _tenantId: string = getTenantId(),
        reason?: string,
    ) => {
        const form = new URLSearchParams();
        if (reason) form.set('reason', reason);
        return await requestRealTradingWithFallback({
            method: 'post',
            url: '/stop',
            data: form.toString(),
            headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
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

    /**
     * 运行维度日志（T-RC-14，游标增量读）。
     *
     * 键为 tenant+user，**纯模拟托管链路没有任务行也能读**（这正是旧 `LogPanel`
     * 认 `latest_hosted_task.task_id` 时模拟盘永远空白的原因）；响应结构与
     * 单任务流对齐，故前端复用同一套游标轮询逻辑。
     *
     * 端点就是 `GET /logs`——它此前是「暂不支持远程查看」的空壳，现已改为读运行流。
     * 不复用旧名 `getLogs` 是因为返回体不同（那边只给一段拼好的文本，这边给结构化
     * entries + 游标），同名两种形状必然有人接错。
     */
    getRuntimeLogs: async (params?: {
        afterId?: string;
        limit?: number;
        level?: string;
        stage?: string;
        source?: string;
    }): Promise<ManualExecutionLogsResponse> => {
        return await requestRealTradingWithFallback<ManualExecutionLogsResponse>({
            method: 'get',
            url: '/logs',
            params: {
                after_id: params?.afterId ?? '0-0',
                limit: params?.limit ?? 200,
                level: params?.level,
                stage: params?.stage,
                source: params?.source,
            },
        });
    },

    /**
     * 风控一屏（T-RC-22）：生效止损/大跌拦截 + 当日风险锁 + 口径分裂诊断。
     *
     * 与下单侧同源（`risk_lock` 模块），因此「界面上看到的锁」就是「下单时判的锁」。
     * `locks.available=false` 表示**读不到**，不是「没有锁」。
     */
    getRiskStatus: async (tradeDate?: string): Promise<{
        status: string;
        user_id: string;
        effective_execution_config?: Record<string, unknown> | null;
        source?: string;
        strategy_id?: string | null;
        strategy_execution_config?: Record<string, unknown> | null;
        execution_config_divergence?: { diverged: boolean; fields?: Record<string, unknown>; message?: string } | null;
        locks?: {
            available: boolean;
            trade_date?: string;
            account_frozen?: boolean;
            symbols?: string[];
            reason?: string | null;
        } | null;
        running?: boolean;
    }> => {
        return await requestRealTradingWithFallback({
            method: 'get',
            url: '/risk-status',
            params: { trade_date: tradeDate },
        });
    },

    /**
     * 盘中热更新配置（T-RC-16）。只改配置、不动持仓、不重启进程；
     * `expectedConfigVersion` 不符时后端回 409（乐观并发，不静默覆盖）。
     *
     * REAL/SHADOW 会回 409 且响应头带 `X-Requires-Restart`——容器 runner 的配置
     * 是 env，改配置必须重建容器，前端据此提示而不是谎称已生效。
     */
    updateRuntimeConfig: async (payload: {
        executionConfig?: Partial<ExecutionConfig>;
        liveTradeConfig?: Partial<LiveTradeConfig>;
        expectedConfigVersion?: number;
        dryRun?: boolean;
        force?: boolean;
    }): Promise<RuntimeConfigUpdateResult> => {
        const form = new URLSearchParams();
        if (payload.executionConfig) {
            form.set('execution_config_json', JSON.stringify(payload.executionConfig));
        }
        if (payload.liveTradeConfig) {
            form.set('live_trade_config_json', JSON.stringify(payload.liveTradeConfig));
        }
        if (payload.expectedConfigVersion !== undefined) {
            form.set('expected_config_version', String(payload.expectedConfigVersion));
        }
        if (payload.dryRun) form.set('dry_run', 'true');
        if (payload.force) form.set('force', 'true');
        return await requestRealTradingWithFallback<RuntimeConfigUpdateResult>({
            method: 'post',
            url: '/runtime-config',
            data: form.toString(),
            headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        });
    },

    previewManualExecution: async (payload: {
        model_id: string;
        run_id: string;
        strategy_id: string;
        trading_mode?: 'REAL' | 'SHADOW' | 'SIMULATION';
        note?: string;
    }): Promise<ManualExecutionPreview> => {
        return await requestRealTradingWithFallback<ManualExecutionPreview>({
            method: 'post',
            url: '/manual-executions/preview',
            data: payload,
            // 后端需拉账户快照 + 构建全市场信号计划（必要时回退读 pred.parquet），
            // 首次冷启动可能超过 30s，放宽到 120s，与网关代理超时对齐。
            timeout: 120000,
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
            // 与预览同口径的重接口，放宽超时避免冷启动误报失败。
            timeout: 120000,
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
    // source：按源看（qmt_exec / tdx_bridge）。留空 = 当前实盘券商对应的源（后端仲裁）。
    // 多券商并行上报时两个源是两个真实账户，不指定就会在两边的最新行之间抖动。
    getAccount: async (
        userId: string,
        tenantId: string = getTenantId(),
        source?: string | null,
    ): Promise<AccountInfo> => {
        try {
            return await requestRealTradingWithFallback<AccountInfo>({
                method: 'get',
                url: '/account',
                params: source ? { source } : undefined,
            });
        } catch (error: any) {
            const status = Number(error?.response?.status ?? 0);
            if (status === 401 || status === 403) {
                throw error;
            }
            if (status === 404) {
                return buildUnavailableRealAccount(
                    'unbound',
                    '当前账户未绑定模拟交易账号',
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
        market?: string,
        source?: string | null,
    ): Promise<AccountInfo | null> => {
        const normalizedMode = String(runtimeMode || '').trim().toUpperCase();
        if (normalizedMode === 'SIMULATION') {
            return await realTradingService.getSimulationAccount(userId, tenantId, market).catch(() => null);
        }
        return await realTradingService.getAccount(userId, tenantId, source).catch(() => null);
    },

    // 实盘账户「按源看」概览（各券商源最新快照 + 新鲜度 + 当前选定源）
    getAccountSources: async (): Promise<AccountSourcesPayload | null> => {
        try {
            const resp = await requestRealTradingWithFallback<AccountSourcesPayload>({
                method: 'get',
                url: '/account/sources',
            });
            return resp;
        } catch (error: any) {
            const status = Number(error?.response?.status ?? 0);
            if (status === 401 || status === 403) throw error;
            return null;
        }
    },

    // Get Simulation Account Info
    getSimulationAccount: async (_userId: string, _tenantId: string = getTenantId(), market?: string): Promise<AccountInfo | null> => {
        const token = authService.getAccessToken();
        const response = await axios.get(`${SERVICE_ENDPOINTS.API_GATEWAY}/simulation/account`, {
            params: market ? { market } : undefined,
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
        _tenantId: string = getTenantId(),
        market?: string
    ): Promise<AccountInfo | null> => {
        const token = authService.getAccessToken();
        const response = await axios.post(
            `${SERVICE_ENDPOINTS.API_GATEWAY}/simulation/reset`,
            { initial_cash: initialCash, market },
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
    // T-P1-07：market 传具体市场取该市场序列（默认 ALL=跨市场合并行，兼容旧调用）
    getSimulationDailySnapshots: async (days: number = 1, market?: string): Promise<SimulationFundSnapshot[]> => {
        const token = authService.getAccessToken();
        const response = await axios.get(`${SERVICE_ENDPOINTS.API_GATEWAY}/simulation/snapshots/daily`, {
            params: { days, ...(market ? { market } : {}) },
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
    },

    analyzeHoldingImages: async (
        formData: FormData
    ): Promise<{ success: boolean; message?: string; data?: any[]; available_cash?: number }> => {
        const token = authService.getAccessToken();
        const response = await axios.post(`${SERVICE_URLS.API_GATEWAY}/simulation/sync/ocr`, formData, {
            headers: {
                'Content-Type': 'multipart/form-data',
                'Authorization': `Bearer ${token}`
            },
            timeout: 60000,
        });
        return response.data;
    },

    syncSimulationHoldings: async (holdings: any[], availableCash?: number): Promise<boolean> => {
        const token = authService.getAccessToken();
        const payload: Record<string, any> = { holdings };
        if (typeof availableCash === 'number' && Number.isFinite(availableCash)) {
            payload.available_cash = availableCash;
        }
        const response = await axios.post(`${SERVICE_URLS.API_GATEWAY}/simulation/sync/confirm`, payload, {
            headers: token ? new AxiosHeaders({ Authorization: `Bearer ${token}` }) : undefined,
            timeout: 30000,
        });
        return response.data?.success === true;
    },
};


export interface AccountInfo {
    account_id?: string;
    snapshot_kind?: 'account_snapshot';
    timestamp?: string | number;
    total_asset: number;
    /** 可用资金（available_cash 为 0 时 fallback 到 cash） */
    cash: number;
    available_cash?: number;
    /** 冻结资金（委托冻结 + 资产缺口冻结） */
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
    // ── 来源仲裁（多券商并行上报时；见 /account?source=） ──
    /** 本条快照实际来自哪个源（account_source） */
    account_source?: string;
    account_source_label?: string;
    /** 本次请求指定的源（留空时=当前实盘券商源） */
    requested_source?: string;
    /** true=请求源不可用、本条是全源最新可用（数字不是你要的那个账户） */
    source_downgraded?: boolean;
    source_downgraded_reason?: 'requested_source_no_snapshot' | 'requested_source_no_usable_snapshot';
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

/** GET /account/sources：各券商源的快照概览（「按源看」chips 的数据源） */
export interface AccountSourceItem {
    source: string;
    label: string;
    account_id?: string | null;
    snapshot_at?: string | null;
    age_sec?: number | null;
    freshness?: 'fresh' | 'stale' | 'unavailable';
    is_usable?: boolean;
    /** 当前实盘券商对应的源（= 下单走的那个账户） */
    selected?: boolean;
    /** 回查得到的 CN 券商键；null = 无可选券商（如手工录入），页面只能看不能切 */
    broker?: string | null;
    selectable?: boolean;
    total_asset?: number;
    cash?: number;
    market_value?: number;
    position_count?: number;
}

export interface AccountSourcesPayload {
    selected_source?: string | null;
    selected_source_label?: string | null;
    /** false = 当前券商来自 REAL_BROKER_TYPE 兜底，不是用户在页面上选的 */
    selected_source_explicit?: boolean;
    sources: AccountSourceItem[];
    policy?: { fresh_within_s: number; stale_within_s: number };
    server_time?: string;
}
