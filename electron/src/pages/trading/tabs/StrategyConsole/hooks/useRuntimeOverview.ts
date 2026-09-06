import { useCallback, useEffect, useRef, useState } from 'react';
import type {
    Order,
    RealTradingStatus,
    TradingPrecheckResult,
} from '../../../../../services/realTradingService';
import type {
    LatestInferenceRunInfo,
    UserModelRecord,
} from '../../../../../services/modelTrainingService';
import type { StrategyFile } from '../../../../../types/backtest/strategy';
import { buildInputNodes, deriveRunState } from '../topologyTypes';
import type { RunState, TopologyNode } from '../topologyTypes';

export type ConsoleTradingMode = 'real' | 'simulation';

interface SectionReady {
    status: boolean;
    precheck: boolean;
    model: boolean;
}

export interface RuntimeOverview {
    status: RealTradingStatus | null;
    precheck: TradingPrecheckResult | null;
    defaultModel: UserModelRecord | null;
    latestRun: LatestInferenceRunInfo | null;
    recentOrders: Order[];
    nodes: TopologyNode[];
    runState: RunState;
    ready: SectionReady;
    ordersReady: boolean;
    lastUpdatedAt: string | null;
    error: string | null;
    refresh: () => void;
    strategies: StrategyFile[];
    strategiesLoading: boolean;
    strategiesLoaded: boolean;
    ensureStrategies: () => Promise<StrategyFile[]>;
}

const toDeployMode = (mode: ConsoleTradingMode): 'REAL' | 'SIMULATION' =>
    mode === 'simulation' ? 'SIMULATION' : 'REAL';

/**
 * 拓扑控制台数据聚合（纯前端聚合，零后端改动）：
 * - 首屏只拉 3 路并行：status / trading-precheck / defaultModel(→latestRun 链式)
 * - 不拉 preflight（重探针+写副作用，只保留给启动流程）、策略列表懒加载
 * - 分级轮询：status+precheck 10s（运行中）/ 30s（空闲），模型+批次 60s
 * - 每节独立 try/catch，单节失败不阻塞其它层渲染
 */
export function useRuntimeOverview(
    tenantId: string,
    userId: string,
    tradingMode: ConsoleTradingMode,
    market: string,
    enabled: boolean,
): RuntimeOverview {
    const [status, setStatus] = useState<RealTradingStatus | null>(null);
    const [precheck, setPrecheck] = useState<TradingPrecheckResult | null>(null);
    const [defaultModel, setDefaultModel] = useState<UserModelRecord | null>(null);
    const [latestRun, setLatestRun] = useState<LatestInferenceRunInfo | null>(null);
    const [ready, setReady] = useState<SectionReady>({ status: false, precheck: false, model: false });
    const [lastUpdatedAt, setLastUpdatedAt] = useState<string | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [strategies, setStrategies] = useState<StrategyFile[]>([]);
    const [strategiesLoading, setStrategiesLoading] = useState(false);
    const [strategiesLoaded, setStrategiesLoaded] = useState(false);
    const [recentOrders, setRecentOrders] = useState<Order[]>([]);
    const [ordersReady, setOrdersReady] = useState(false);

    const statusRef = useRef<RealTradingStatus | null>(null);
    const modelIdRef = useRef<string>('');
    const marketRef = useRef(market);
    const fetchingRef = useRef({ status: false, precheck: false, model: false, orders: false });
    const readyRef = useRef<SectionReady>({ status: false, precheck: false, model: false });
    const strategiesFetchingRef = useRef(false);
    statusRef.current = status;
    marketRef.current = market;

    // 项目内 setState 不接受函数式更新，用 ref 镜像做合并（与旧代码值形式一致）
    const markReady = (key: keyof SectionReady) => {
        readyRef.current = { ...readyRef.current, [key]: true };
        setReady(readyRef.current);
    };

    const loadStatus = useCallback(async () => {
        if (fetchingRef.current.status) return;
        fetchingRef.current.status = true;
        try {
            const { realTradingService } = await import('../../../../../services/realTradingService');
            const data = await realTradingService.getStatus(userId, tradingMode, tenantId);
            setStatus(data);
            setLastUpdatedAt(new Date().toISOString());
        } catch (e) {
            console.warn('[TopologyConsole] status failed', e);
        } finally {
            fetchingRef.current.status = false;
            markReady('status');
        }
    }, [tenantId, userId, tradingMode]);

    const loadPrecheck = useCallback(async () => {
        if (fetchingRef.current.precheck) return;
        fetchingRef.current.precheck = true;
        try {
            const { realTradingService } = await import('../../../../../services/realTradingService');
            const data = await realTradingService.getTradingPrecheck(toDeployMode(tradingMode));
            setPrecheck(data);
        } catch (e) {
            console.warn('[TopologyConsole] precheck failed', e);
        } finally {
            fetchingRef.current.precheck = false;
            markReady('precheck');
        }
    }, [tradingMode]);

    const loadModelChain = useCallback(async () => {
        if (fetchingRef.current.model) return;
        fetchingRef.current.model = true;
        try {
            const { modelTrainingService } = await import('../../../../../services/modelTrainingService');
            let model: UserModelRecord | null = null;
            try {
                model = await modelTrainingService.getDefaultModel(marketRef.current);
            } catch (e: unknown) {
                if ((e as { response?: { status?: number } })?.response?.status !== 404) {
                    console.warn('[TopologyConsole] defaultModel failed', e);
                }
            }
            setDefaultModel(model || null);
            const modelId = model?.model_id || '';
            modelIdRef.current = modelId;
            if (modelId) {
                try {
                    const run = await modelTrainingService.getLatestInferenceRun(modelId);
                    setLatestRun(run || null);
                } catch (e) {
                    console.warn('[TopologyConsole] latestRun failed', e);
                    setLatestRun(null);
                }
            } else {
                setLatestRun(null);
            }
        } finally {
            fetchingRef.current.model = false;
            markReady('model');
        }
    }, []);

    // 最近交易记录：只取 10 条，不进首屏关键路径，独立容错
    const loadRecentOrders = useCallback(async () => {
        if (fetchingRef.current.orders) return;
        fetchingRef.current.orders = true;
        try {
            const { realTradingService } = await import('../../../../../services/realTradingService');
            const orders = await realTradingService.getOrders(
                userId,
                undefined,
                tradingMode,
                { limit: 10, offset: 0 },
            );
            setRecentOrders(Array.isArray(orders) ? orders : []);
        } catch (e) {
            console.warn('[TopologyConsole] recentOrders failed', e);
        } finally {
            fetchingRef.current.orders = false;
            setOrdersReady(true);
        }
    }, [userId, tradingMode]);

    const refresh = useCallback(() => {
        void loadStatus();
        void loadPrecheck();
        void loadModelChain();
        void loadRecentOrders();
    }, [loadStatus, loadPrecheck, loadModelChain, loadRecentOrders]);

    // 首屏：3 路并行一次，无串行等待、无全屏遮罩
    useEffect(() => {
        if (!enabled) return;
        readyRef.current = { status: false, precheck: false, model: false };
        setReady(readyRef.current);
        setError(null);
        refresh();
    }, [enabled, tenantId, userId, tradingMode, market, refresh]);

    // 分级轮询：运行中 status+precheck 10s，空闲 30s；模型链 60s
    useEffect(() => {
        if (!enabled) return;
        let fastTimer: number | undefined;
        let slowTimer: number | undefined;
        let modelTimer: number | undefined;
        const armSlow = () => {
            window.clearInterval(fastTimer);
            window.clearInterval(slowTimer);
            fastTimer = undefined;
            slowTimer = window.setInterval(() => {
                void loadStatus();
                void loadPrecheck();
            }, 30000);
        };
        const armFast = () => {
            window.clearInterval(fastTimer);
            window.clearInterval(slowTimer);
            slowTimer = undefined;
            fastTimer = window.setInterval(() => {
                void loadStatus();
                void loadPrecheck();
            }, 10000);
        };
        const syncCadence = () => {
            const s = String(statusRef.current?.status || '').toLowerCase();
            if (s === 'running' || s === 'starting') {
                if (fastTimer === undefined) armFast();
            } else if (slowTimer === undefined) {
                armSlow();
            }
        };
        syncCadence();
        const watcher = window.setInterval(syncCadence, 5000);
        modelTimer = window.setInterval(() => void loadModelChain(), 60000);
        const ordersTimer = window.setInterval(() => void loadRecentOrders(), 30000);
        return () => {
            window.clearInterval(fastTimer);
            window.clearInterval(slowTimer);
            window.clearInterval(watcher);
            window.clearInterval(modelTimer);
            window.clearInterval(ordersTimer);
        };
    }, [enabled, tenantId, userId, tradingMode, loadStatus, loadPrecheck, loadModelChain, loadRecentOrders]);

    // 策略列表懒加载：点下拉框才拉
    const ensureStrategies = useCallback(async (): Promise<StrategyFile[]> => {
        if (strategiesLoaded) return strategies;
        if (strategiesFetchingRef.current) return strategies;
        strategiesFetchingRef.current = true;
        setStrategiesLoading(true);
        try {
            const { strategyManagementService } = await import('../../../../../services/strategyManagementService');
            const list = await strategyManagementService.loadStrategies(userId);
            setStrategies(list);
            setStrategiesLoaded(true);
            return list;
        } catch (e) {
            console.warn('[TopologyConsole] strategies failed', e);
            return [];
        } finally {
            strategiesFetchingRef.current = false;
            setStrategiesLoading(false);
        }
    }, [strategies, strategiesLoaded, userId]);

    const isSim = tradingMode === 'simulation';
    const nodes = buildInputNodes(precheck?.items || [], isSim);

    return {
        status,
        precheck,
        defaultModel,
        latestRun,
        recentOrders,
        nodes,
        runState: deriveRunState(status),
        ready,
        ordersReady,
        lastUpdatedAt,
        error,
        refresh,
        strategies,
        strategiesLoading,
        strategiesLoaded,
        ensureStrategies,
    };
}
