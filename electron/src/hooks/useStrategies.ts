/**
 * 策略监控 Hook
 *
 * 管理策略列表状态、启停操作及统计数据
 * 支持 WebSocket 实时推送策略状态变化
 *
 * @author QuantMind Team
 * @date 2025-02-13
 */

import { useState, useEffect, useCallback, useRef } from 'react';
import { strategyService, Strategy, StrategyStatus, ApiResponse, StrategyActionResponse } from '../services/strategyService';
import type { RealTradingStatus } from '../services/realTradingService';
import { shouldUpdateByFingerprint } from '../utils/dataChange';
import { refreshOrchestrator } from '../services/refreshOrchestrator';
import {
  MessageType,
  websocketService,
  WebSocketStatus,
} from '../services/websocketService';

export interface StrategyStats {
    totalStrategies: number;
    activeStrategies: number;
    stoppedStrategies: number;
    errorStrategies: number;
    totalReturn: number;
    todayReturn: number;
    todayPnL: number;
}

export interface UseStrategiesOptions {
    autoRefresh?: boolean;
    refreshInterval?: number;
    enableRealtime?: boolean;
}

export interface UseStrategiesReturn {
    strategies: Strategy[];
    stats: StrategyStats;
    loading: boolean;
    error: string | null;
    isSimulated: boolean;
    isStale: boolean;
    lastUpdatedAt: string | null;
    realtimeStatus: 'connected' | 'fallback' | 'disabled';
    refresh: () => Promise<void>;
    startStrategy: (id: string) => Promise<boolean>;
    stopStrategy: (id: string) => Promise<boolean>;
}

const DEFAULT_POLLING_INTERVAL = 30000;
const MIN_POLLING_INTERVAL = 800;
const MAX_POLLING_INTERVAL = 10000;
const RECONNECT_REFRESH_DELAY = 500;

export const useStrategies = (options: UseStrategiesOptions = {}): UseStrategiesReturn => {
    const {
        autoRefresh = true,
        refreshInterval = DEFAULT_POLLING_INTERVAL,
        enableRealtime = true,
    } = options;

    const [strategies, setStrategies] = useState<Strategy[]>([]);
    const [stats, setStats] = useState<StrategyStats>({
        totalStrategies: 0,
        activeStrategies: 0,
        stoppedStrategies: 0,
        errorStrategies: 0,
        totalReturn: 0,
        todayReturn: 0,
        todayPnL: 0
    });
    const [loading, setLoading] = useState<boolean>(true);
    const [error, setError] = useState<string | null>(null);
    const [isSimulated, setIsSimulated] = useState<boolean>(false);
    const [isStale, setIsStale] = useState<boolean>(false);
    const [lastUpdatedAt, setLastUpdatedAt] = useState<string | null>(null);
    const [realtimeStatus, setRealtimeStatus] = useState<'connected' | 'fallback' | 'disabled'>('disabled');
    
    const fingerprintRef = useRef<string | null>(null);
    const initializedRef = useRef<boolean>(false);
    const strategiesRef = useRef<Strategy[]>([]);
    const statsRef = useRef<StrategyStats>(stats);
    const subscribedTopicRef = useRef<string | null>(null);
    const prevRealtimeStatusRef = useRef<'connected' | 'fallback' | 'disabled'>('disabled');

    const getCurrentUserId = useCallback(() => {
        try {
            const raw = localStorage.getItem('user');
            if (!raw) return '';
            const parsed = JSON.parse(raw);
            return String(parsed?.id || parsed?.user_id || '');
        } catch {
            return '';
        }
    }, []);

    const reconcileRuntimeStatus = useCallback(
        async (list: Strategy[]): Promise<Strategy[]> => {
            if (!Array.isArray(list) || list.length === 0) return list;
            const hasRunning = list.some(s => s.status === 'running' || s.status === 'starting');
            if (hasRunning) return list;

            let rtStatus: RealTradingStatus | null = null;
            try {
                const { realTradingService } = await import('../services/realTradingService');
                rtStatus = await realTradingService.getStatus('');
            } catch {
                return list;
            }

            const runtime = rtStatus?.status;
            if (runtime !== 'running' && runtime !== 'starting') {
                return list;
            }

            const activeStrategy = rtStatus?.strategy;
            const activeId = String(activeStrategy?.id || '').trim();
            const activeName = String(activeStrategy?.name || '').trim().toLowerCase();
            const activeTemplateId = activeId.startsWith('sys_') ? activeId.replace('sys_', '').toLowerCase() : '';
            const normalizedRuntime: StrategyStatus = runtime === 'starting' ? 'starting' : 'running';

            let matched = false;
            const next = list.map((item) => {
                const itemId = String(item.id || '').trim();
                const itemName = String(item.name || '').trim().toLowerCase();
                const itemTemplateId = String((item.parameters as Record<string, unknown> | undefined)?.strategy_type || '')
                    .trim()
                    .toLowerCase();

                const isActive = (activeId && itemId === activeId)
                    || (activeTemplateId && itemTemplateId === activeTemplateId)
                    || (activeName && itemName === activeName);

                if (!isActive) return item;
                matched = true;
                return {
                    ...item,
                    status: normalizedRuntime,
                    runtime_state: normalizedRuntime,
                    effective_status: normalizedRuntime,
                };
            });

            return matched ? next : list;
        },
        [],
    );

    const isSuccessResponse = <T,>(response: ApiResponse<T> | undefined): boolean => {
        if (!response) return false;
        if (typeof (response as unknown as { code?: number }).code === 'number') {
            const code = (response as unknown as { code: number }).code;
            return code >= 200 && code < 300;
        }
        return Boolean((response as unknown as { success?: boolean }).success);
    };

    const calculateStats = (list: Strategy[]): StrategyStats => {
        const totalStrategies = list.length;
        const activeStrategies = list.filter(s => s.status === 'running' || s.status === 'starting').length;
        const stoppedStrategies = list.filter(s => s.status === 'stopped').length;
        const errorStrategies = list.filter(s => s.status === 'error').length;

        const totalReturn = list.reduce((sum, s) => sum + s.total_return, 0);
        const todayReturn = list.reduce((sum, s) => sum + s.today_return, 0);
        const todayPnL = list.reduce((sum, s) => sum + (s.today_pnl || 0), 0);

        return {
            totalStrategies,
            activeStrategies,
            stoppedStrategies,
            errorStrategies,
            totalReturn: Number(totalReturn.toFixed(2)),
            todayReturn: Number(todayReturn.toFixed(2)),
            todayPnL: Number(todayPnL.toFixed(2)),
        };
    };

    const applyStrategiesSnapshot = useCallback((
        nextStrategies: Strategy[],
        nextIsSimulated: boolean,
    ) => {
        const nextStats = calculateStats(nextStrategies);
        const snapshot = {
            strategies: nextStrategies,
            stats: nextStats,
            isSimulated: nextIsSimulated,
        };
        const { changed, fingerprint } = shouldUpdateByFingerprint(fingerprintRef.current, snapshot);

        if (changed) {
            setStrategies(nextStrategies);
            strategiesRef.current = nextStrategies;
            setStats(nextStats);
            statsRef.current = nextStats;
            setIsSimulated(nextIsSimulated);
            fingerprintRef.current = fingerprint;
        }
    }, []);

    const fetchData = useCallback(async (params?: { silent?: boolean }) => {
        const silent = params?.silent ?? true;

        try {
            if (!silent && !initializedRef.current) {
                setLoading(true);
            }

            const response = await strategyService.getStrategies();

            if (isSuccessResponse(response) && Array.isArray(response.data)) {
                const reconciledStrategies = await reconcileRuntimeStatus(response.data);
                const nextIsSimulated = reconciledStrategies.some(s => s.name.includes('(模拟)'));
                applyStrategiesSnapshot(reconciledStrategies, nextIsSimulated);
                setIsStale(false);
                setLastUpdatedAt(new Date().toISOString());
                setError(null);
            } else {
                setError(response?.message || '获取策略列表失败');
                setIsStale(strategies.length > 0);
            }
        } catch (err) {
            const errorMessage = err instanceof Error ? err.message : '未知错误';
            setError(errorMessage);
            setIsStale(strategies.length > 0);
        } finally {
            initializedRef.current = true;
            setLoading(false);
        }
    }, [strategies.length, reconcileRuntimeStatus, applyStrategiesSnapshot]);

    const refresh = useCallback(async () => {
        await fetchData({ silent: true });
    }, [fetchData]);

    const startStrategy = useCallback(async (id: string): Promise<boolean> => {
        const previousStrategies = strategies;
        const previousStats = stats;
        try {
            const nextStrategies = strategies.map(s => s.id === id ? { ...s, status: 'starting' as StrategyStatus } : s);
            applyStrategiesSnapshot(nextStrategies, isSimulated);

            const response = await strategyService.startStrategy(id);
            if (!isSuccessResponse<StrategyActionResponse>(response)) {
                throw new Error(response.message || '启动策略失败');
            }

            await fetchData({ silent: true });
            await refreshOrchestrator.requestRefresh('strategies', 'module-action', true);
            return true;
        } catch (err) {
            console.error('启动策略失败:', err);
            setStrategies(previousStrategies);
            setStats(previousStats);
            setError(err instanceof Error ? err.message : '启动策略失败');
            setIsStale(previousStrategies.length > 0);
            return false;
        }
    }, [strategies, stats, isSimulated, fetchData, applyStrategiesSnapshot]);

    const stopStrategy = useCallback(async (id: string): Promise<boolean> => {
        const previousStrategies = strategies;
        const previousStats = stats;
        try {
            const nextStrategies = strategies.map(s => s.id === id ? { ...s, status: 'stopped' as StrategyStatus } : s);
            applyStrategiesSnapshot(nextStrategies, isSimulated);

            const response = await strategyService.stopStrategy(id);
            if (!isSuccessResponse<StrategyActionResponse>(response)) {
                throw new Error(response.message || '停止策略失败');
            }

            await fetchData({ silent: true });
            await refreshOrchestrator.requestRefresh('strategies', 'module-action', true);
            return true;
        } catch (err) {
            console.error('停止策略失败:', err);
            setStrategies(previousStrategies);
            setStats(previousStats);
            setError(err instanceof Error ? err.message : '停止策略失败');
            setIsStale(previousStrategies.length > 0);
            return false;
        }
    }, [strategies, stats, isSimulated, fetchData, applyStrategiesSnapshot]);

    const connectRealtime = useCallback(async () => {
        if (!enableRealtime) {
            setRealtimeStatus('disabled');
            return;
        }

        const userId = getCurrentUserId();
        if (!userId) {
            setRealtimeStatus('disabled');
            return;
        }

        const topic = `strategy.${userId}`;
        const status = websocketService.getStatus();
        if (status === WebSocketStatus.DISCONNECTED || status === WebSocketStatus.ERROR) {
            try {
                await websocketService.connect();
            } catch (error) {
                console.warn('策略实时连接失败，回退轮询', error);
                setRealtimeStatus('fallback');
                return;
            }
        }

        websocketService.subscribe({ channels: [topic] });
        subscribedTopicRef.current = topic;
        setRealtimeStatus(websocketService.getStatus() === WebSocketStatus.CONNECTED ? 'connected' : 'fallback');
    }, [enableRealtime, getCurrentUserId]);

    const disconnectRealtime = useCallback(() => {
        if (subscribedTopicRef.current) {
            websocketService.unsubscribe([subscribedTopicRef.current]);
            subscribedTopicRef.current = null;
        }
        setRealtimeStatus('disabled');
    }, []);

    useEffect(() => {
        fetchData({ silent: false });
    }, []);

    useEffect(() => {
        if (!autoRefresh) {
            return;
        }

        const effectiveInterval = realtimeStatus === 'connected'
            ? Infinity
            : Math.min(Math.max(refreshInterval, MIN_POLLING_INTERVAL), MAX_POLLING_INTERVAL);

        if (effectiveInterval === Infinity) {
            return;
        }

        const unregister = refreshOrchestrator.register(
            'strategies',
            async () => {
                await fetchData({ silent: true });
            },
            { minIntervalMs: effectiveInterval },
        );

        return unregister;
    }, [autoRefresh, refreshInterval, fetchData, realtimeStatus]);

    useEffect(() => {
        if (
            prevRealtimeStatusRef.current !== 'connected' &&
            realtimeStatus === 'connected' &&
            initializedRef.current
        ) {
            setTimeout(() => {
                fetchData({ silent: true });
            }, RECONNECT_REFRESH_DELAY);
        }
        prevRealtimeStatusRef.current = realtimeStatus;
    }, [realtimeStatus, fetchData]);

    useEffect(() => {
        if (!enableRealtime) {
            return;
        }

        const handleStrategyMessage = (data: unknown) => {
            const raw = data as {
                strategy_id?: string;
                status?: string;
                name?: string;
                error_message?: string;
            };

            if (!raw?.strategy_id) return;

            const strategyId = String(raw.strategy_id);
            const newStatus = raw.status as StrategyStatus | undefined;

            if (newStatus && ['running', 'starting', 'stopped', 'error', 'paused'].includes(newStatus)) {
                const updatedStrategies = strategiesRef.current.map(s => {
                    if (s.id === strategyId) {
                        return {
                            ...s,
                            status: newStatus,
                            error_message: raw.error_message,
                        };
                    }
                    return s;
                });
                applyStrategiesSnapshot(updatedStrategies, isSimulated);
            }
        };

        const handleStatusChange = (status: WebSocketStatus) => {
            if (status === WebSocketStatus.CONNECTED && subscribedTopicRef.current) {
                websocketService.subscribe({ channels: [subscribedTopicRef.current] });
                setRealtimeStatus('connected');
            } else if (
                status === WebSocketStatus.DISCONNECTED ||
                status === WebSocketStatus.ERROR ||
                status === WebSocketStatus.RECONNECTING
            ) {
                setRealtimeStatus('fallback');
            }
        };

        websocketService.addMessageHandler(MessageType.TRADE_SIGNAL, handleStrategyMessage);
        websocketService.addStatusHandler(handleStatusChange);
        connectRealtime().catch(() => setRealtimeStatus('fallback'));

        return () => {
            websocketService.removeMessageHandler(MessageType.TRADE_SIGNAL, handleStrategyMessage);
            websocketService.removeStatusHandler(handleStatusChange);
            disconnectRealtime();
        };
    }, [enableRealtime, applyStrategiesSnapshot, isSimulated, connectRealtime, disconnectRealtime]);

    return {
        strategies,
        stats,
        loading,
        error,
        isSimulated,
        isStale,
        lastUpdatedAt,
        realtimeStatus,
        refresh,
        startStrategy,
        stopStrategy,
    };
};
