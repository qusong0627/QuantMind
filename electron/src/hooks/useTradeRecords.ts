/**
 * 交易记录 Hook
 *
 * 从后端获取最近交易记录，后端不可用时降级为空列表
 *
 * @author QuantMind Team
 * @date 2025-02-13
 */

import { useState, useEffect, useCallback, useRef } from 'react';
import { useSelector } from 'react-redux';
import { tradingService, TradeRecord, TradingMode } from '../services/tradingService';
import { shouldUpdateByFingerprint } from '../utils/dataChange';
import { refreshOrchestrator } from '../services/refreshOrchestrator';
import { useTradeWebSocket } from './useTradeWebSocket';
import { marketDataService } from '../services/marketDataService';

export interface UseTradeRecordsOptions {
    limit?: number;
    tradingMode?: TradingMode;
    autoRefresh?: boolean;
    refreshInterval?: number;
    /** 市场过滤（CN/HK/US/FUTURES/CRYPTO）；不传 = 全部市场 */
    market?: string;
}

export interface UseTradeRecordsReturn {
    records: TradeRecord[];
    loading: boolean;
    error: string | null;
    isOffline: boolean;
    isFallbackToOrders: boolean;
    isStale: boolean;
    lastUpdatedAt: string | null;
    refresh: () => Promise<void>;
}

export const useTradeRecords = (options: UseTradeRecordsOptions = {}): UseTradeRecordsReturn => {
    const {
        limit = 10,
        tradingMode,
        autoRefresh = false,
        refreshInterval = 60000, // 1分钟
        market,
    } = options;

    const [records, setRecords] = useState<TradeRecord[]>([]);
    const [loading, setLoading] = useState<boolean>(true);
    const [error, setError] = useState<string | null>(null);
    const [isOffline, setIsOffline] = useState<boolean>(false);
    const [isFallbackToOrders, setIsFallbackToOrders] = useState<boolean>(false);
    const [isStale, setIsStale] = useState<boolean>(false);
    const [lastUpdatedAt, setLastUpdatedAt] = useState<string | null>(null);
    const fingerprintRef = useRef<string | null>(null);
    const initializedRef = useRef<boolean>(false);
    const backoffMsRef = useRef<number>(3000);
    const retryTimerRef = useRef<number | null>(null);
    const failedSymbolsRef = useRef<Set<string>>(new Set());

    const userId = useSelector((state: any) => state.auth?.user?.id);


    const normalizeRecords = useCallback((input: TradeRecord[]): TradeRecord[] => {
        const byId = new Map<string, TradeRecord>();
        for (const item of input) {
            byId.set(item.id, item);
        }

        return Array.from(byId.values())
            .sort((a, b) => {
                const ta = new Date(a.time).getTime();
                const tb = new Date(b.time).getTime();
                if (Number.isNaN(ta) && Number.isNaN(tb)) return 0;
                if (Number.isNaN(ta)) return 1;
                if (Number.isNaN(tb)) return -1;
                return tb - ta;
            })
            .slice(0, limit);
    }, [limit]);

    const clearRetryTimer = useCallback(() => {
        if (retryTimerRef.current !== null) {
            window.clearTimeout(retryTimerRef.current);
            retryTimerRef.current = null;
        }
    }, []);

    const scheduleRetry = useCallback(() => {
        clearRetryTimer();
        const delay = backoffMsRef.current;
        retryTimerRef.current = window.setTimeout(() => {
            fetchData({ silent: true }).catch(() => undefined);
        }, delay);
        backoffMsRef.current = Math.min(Math.floor(delay * 1.8), 30000);
    // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [clearRetryTimer]);

    // 获取交易记录
    const fetchData = useCallback(async (params?: { silent?: boolean }) => {
        const silent = params?.silent ?? true;

        try {
            if (!silent && !initializedRef.current) {
                setLoading(true);
            }
            setError(null);

            const result = await tradingService.getRecentTrades(limit, tradingMode, market);
            const normalizedRecords = normalizeRecords(result.records);

            // 批量获取股票名称并补全
            const uniqueSymbols = Array.from(new Set(normalizedRecords.map(r => r.symbol)))
                .filter(symbol => !failedSymbolsRef.current.has(symbol));
            
            if (uniqueSymbols.length > 0) {
                try {
                    // 使用分批获取以防超时和并发过载
                    const batchResults = await marketDataService.getStockDetailsBatch(uniqueSymbols, 10, 50);
                    const nameMap: Record<string, string> = {};
                    
                    batchResults.forEach(({ code, result }) => {
                        if (result.success && result.data?.name) {
                            nameMap[code] = result.data.name;
                        } else {
                            failedSymbolsRef.current.add(code);
                        }
                    });

                    // 更新记录中的名称
                    normalizedRecords.forEach(record => {
                        if (nameMap[record.symbol]) {
                            record.name = nameMap[record.symbol];
                        }
                    });
                } catch (nameError) {
                    console.warn('Failed to resolve stock names batch in useTradeRecords:', nameError);
                }
            }

            const nextSnapshot = {
                records: normalizedRecords,
                isOffline: result.isOffline,
                isFallbackToOrders: result.isFallbackToOrders,
            };

            const { changed, fingerprint } = shouldUpdateByFingerprint(fingerprintRef.current, nextSnapshot);
            if (changed) {
                if (result.isOffline && normalizedRecords.length === 0 && records.length > 0) {
                    setIsOffline(true);
                    setIsStale(true);
                    fingerprintRef.current = fingerprint;
                } else {
                    setRecords(normalizedRecords);
                    setIsOffline(result.isOffline);
                    setIsFallbackToOrders(result.isFallbackToOrders);
                    setIsStale((result.isOffline || result.isFallbackToOrders) && normalizedRecords.length > 0);
                    if (!result.isOffline) {
                        setLastUpdatedAt(new Date().toISOString());
                    }
                    fingerprintRef.current = fingerprint;
                }
            }

            if (result.isOffline) {
                scheduleRetry();
            } else {
                clearRetryTimer();
                backoffMsRef.current = 3000;
            }
        } catch (err) {
            const errorMessage = err instanceof Error ? err.message : '未知错误';
            setError(errorMessage);
            console.error('获取交易记录失败:', errorMessage);

            const nextSnapshot = {
                records,
                isOffline: true,
                isFallbackToOrders: false,
            };

            const { changed, fingerprint } = shouldUpdateByFingerprint(fingerprintRef.current, nextSnapshot);
            if (changed) {
                setIsOffline(true);
                setIsFallbackToOrders(false);
                setIsStale(records.length > 0);
                fingerprintRef.current = fingerprint;
            }

            scheduleRetry();
        } finally {
            initializedRef.current = true;
            setLoading(false);
        }
    }, [limit, tradingMode, market, normalizeRecords, records, scheduleRetry, clearRetryTimer]);

    // 监听实时成交更新事件，收到后立即触发数据刷新
    useTradeWebSocket({
        userId: userId ? String(userId) : '',
        onTradeEvent: useCallback((event) => {
            console.log('收到实时交易事件，正在刷新记录...', event);
            fetchData({ silent: true }).catch(() => undefined);
        }, [fetchData]),
        // 仅在已登录且 autoRefresh 为 true 时启用
        enabled: autoRefresh && !!userId,
    });

    // 手动刷新
    const refresh = useCallback(async () => {
        clearRetryTimer();
        await fetchData({ silent: true });
    }, [fetchData, clearRetryTimer]);

    // 初始化
    useEffect(() => {
        fetchData({ silent: false });
    }, [fetchData]);

    // 统一由协调器触发刷新
    useEffect(() => {
        if (!autoRefresh) {
            return;
        }

        const unregister = refreshOrchestrator.register(
            'trade-records',
            async () => {
                await fetchData({ silent: true });
            },
            { minIntervalMs: Math.min(Math.max(refreshInterval, 800), 10000) },
        );

        return unregister;
    }, [autoRefresh, refreshInterval, fetchData]);

    useEffect(() => {
        return () => {
            clearRetryTimer();
        };
    }, [clearRetryTimer]);

    return {
        records,
        loading,
        error,
        isOffline,
        isFallbackToOrders,
        isStale,
        lastUpdatedAt,
        refresh,
    };
};
