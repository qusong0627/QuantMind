import { useState, useEffect, useCallback, useRef } from 'react';
import { portfolioService, ChartDataPoint, PositionDistribution } from '../services/portfolioService';
import { tradingService } from '../services/tradingService';
import { realTradingService } from '../services/realTradingService';
import { modelTrainingService } from '../services/modelTrainingService';
import { useWebSocket } from '../contexts/WebSocketContext';
import { shouldUpdateByFingerprint, calcFingerprint } from '../utils/dataChange';
import { refreshOrchestrator } from '../services/refreshOrchestrator';
import { authService } from '../features/auth/services/authService';
import { useAppSelector } from '../store';

export interface ChartData {
    dailyReturn: ChartDataPoint[];
    tradeCount: ChartDataPoint[];
    positionRatio: PositionDistribution[];
}

const CHART_CALENDAR_MARKET = 'SSE';
const tradingDayWindowCache = new Map<string, Promise<string[]>>();

const chartsAutoFetchEnabled = (): boolean => {
    const env = (import.meta as any)?.env || {};
    const raw = env.VITE_INTELLIGENCE_CHARTS_AUTO_FETCH;
    if (raw === undefined || raw === null || String(raw).trim() === '') {
        return true;
    }
    return String(raw).trim().toLowerCase() !== 'false';
};

const asRecord = (value: unknown): Record<string, unknown> | null =>
    value && typeof value === 'object' ? (value as Record<string, unknown>) : null;

const unwrapDataObject = (value: unknown): Record<string, unknown> | null => {
    let current = asRecord(value);
    let depth = 0;
    while (current && depth < 4) {
        const nested = asRecord(current.data);
        if (!nested) break;
        current = nested;
        depth += 1;
    }
    return current;
};

const normalizeChartPoints = (value: unknown): ChartDataPoint[] => {
    if (Array.isArray(value)) {
        return value
            .map((item) => {
                const row = asRecord(item);
                if (!row) return null;
                const timestamp = typeof row.timestamp === 'string' ? row.timestamp : new Date().toISOString();
                const rawValue = row.value;
                const num = typeof rawValue === 'number' ? rawValue : Number(rawValue);
                if (!Number.isFinite(num)) return null;
                return {
                    timestamp,
                    value: num,
                    label: typeof row.label === 'string' ? row.label : undefined,
                } as ChartDataPoint;
            })
            .filter((item): item is ChartDataPoint => item !== null);
    }

    const obj = asRecord(value);
    if (!obj) return [];

    const nestedData = asRecord(obj.data);
    if (!nestedData) return [];

    const rawDailyReturns = nestedData.daily_returns;
    const rawDates = nestedData.dates;
    if (Array.isArray(rawDailyReturns)) {
        return rawDailyReturns
            .map((v, idx) => {
                const num = typeof v === 'number' ? v : Number(v);
                if (!Number.isFinite(num)) return null;
                
                let timestamp: string;
                if (Array.isArray(rawDates) && typeof rawDates[idx] === 'string') {
                    timestamp = rawDates[idx];
                } else {
                    const d = new Date();
                    d.setDate(d.getDate() - (rawDailyReturns.length - 1 - idx));
                    timestamp = d.toISOString();
                }

                return {
                    timestamp,
                    value: num,
                    label: 'daily_return',
                } as ChartDataPoint;
            })
            .filter((item): item is ChartDataPoint => item !== null);
    }

    return [];
};

const normalizePositionDistribution = (value: unknown): PositionDistribution[] => {
    const toAggregatedHoldingCash = (list: PositionDistribution[]): PositionDistribution[] => {
        if (!Array.isArray(list) || list.length === 0) return [];
        const total = list.reduce((sum, item) => sum + (Number(item.value) || 0), 0);
        if (!Number.isFinite(total) || total <= 0) return [];

        const cashKeywords = ['cash', '现金', '可用资金', '空仓', '余额', 'money', 'funding', 'margin'];
        const isCash = (item: PositionDistribution): boolean => {
            const text = `${item.name || ''} ${item.code || ''}`.toLowerCase();
            return cashKeywords.some((key) => text.includes(key));
        };

        const cashValue = list
            .filter(isCash)
            .reduce((sum, item) => sum + (Number(item.value) || 0), 0);
        const holdingValue = Math.max(0, total - cashValue);

        return [
            {
                name: '持仓市值',
                code: 'HOLDING',
                value: holdingValue,
                ratio: holdingValue / total,
            },
            {
                name: '可用资金',
                code: 'CASH',
                value: cashValue,
                ratio: cashValue / total,
            },
        ];
    };

    if (Array.isArray(value)) {
        const parsed = value
            .map((item) => {
                const row = asRecord(item);
                if (!row || typeof row.name !== 'string') return null;
                const rawVal = row.value ?? row.ratio ?? 0;
                const num = typeof rawVal === 'number' ? rawVal : Number(rawVal);
                if (!Number.isFinite(num)) return null;
                return {
                    name: row.name,
                    code: typeof row.code === 'string' ? row.code : row.name,
                    value: num,
                    ratio: typeof row.ratio === 'number' ? row.ratio : num,
                } as PositionDistribution;
            })
            .filter((item): item is PositionDistribution => item !== null);
        return toAggregatedHoldingCash(parsed);
    }

    const obj = unwrapDataObject(value);
    if (!obj) return [];

    const toDistributionListFromMap = (mapObj: Record<string, unknown>): PositionDistribution[] =>
        Object.entries(mapObj)
            .map(([name, ratio]) => {
                const num = typeof ratio === 'number' ? ratio : Number(ratio);
                if (!Number.isFinite(num)) return null;
                return {
                    name,
                    code: name,
                    value: num,
                    ratio: num,
                } as PositionDistribution;
            })
            .filter((item): item is PositionDistribution => item !== null);

    const sectors = asRecord(obj.sectors);
    if (sectors && Object.keys(sectors).length > 0) {
        return toAggregatedHoldingCash(toDistributionListFromMap(sectors));
    }

    const assets = asRecord(obj.assets);
    if (assets && Object.keys(assets).length > 0) {
        return toAggregatedHoldingCash(toDistributionListFromMap(assets));
    }

    const positions = Array.isArray(obj.positions) ? obj.positions : null;
    if (positions && positions.length > 0) {
        const parsed = positions
            .map((item) => {
                const row = asRecord(item);
                if (!row) return null;
                const rawValue = row.market_value ?? row.value ?? row.amount ?? row.weight ?? row.ratio;
                const num = typeof rawValue === 'number' ? rawValue : Number(rawValue);
                if (!Number.isFinite(num) || num <= 0) return null;
                const name = String(row.symbol_name ?? row.name ?? row.symbol ?? row.code ?? '未知持仓');
                const code = String(row.symbol ?? row.code ?? name);
                return {
                    name,
                    code,
                    value: num,
                    ratio: num,
                } as PositionDistribution;
            })
            .filter((item): item is PositionDistribution => item !== null);
        return toAggregatedHoldingCash(parsed);
    }

    const parsed = Object.entries(obj)
        .map(([name, ratio]) => {
            const num = typeof ratio === 'number' ? ratio : Number(ratio);
            if (!Number.isFinite(num)) return null;
            return {
                name,
                code: name,
                value: num,
                ratio: num,
            } as PositionDistribution;
        })
        .filter((item): item is PositionDistribution => item !== null);
    return toAggregatedHoldingCash(parsed);
};

const buildHoldingCashFromAccount = (account: unknown): PositionDistribution[] => {
    const row = asRecord(account);
    if (!row) return [];

    const toNumber = (v: unknown): number => {
        const n = Number(v);
        return Number.isFinite(n) ? n : 0;
    };

    // 数据库中 portfolios 表使用 total_value 字段，而不是 total_asset
    const marketValue = Math.max(0, toNumber(row.market_value));
    const cash = Math.max(
        0,
        toNumber(row.available_cash ?? row.cash ?? row.available_balance),
    );
    const totalAssetRaw = toNumber(row.total_value ?? row.total_asset);
    
    // 如果没有任何资产信息，返回空
    if (totalAssetRaw <= 0 && marketValue <= 0 && cash <= 0) return [];
    
    // 优先采用：市值 = marketValue，现金 = total_value - marketValue
    const holdingValue = Math.max(0, marketValue);
    const cashValue = Math.max(0, totalAssetRaw > 0 ? totalAssetRaw - holdingValue : cash);
    const finalTotal = holdingValue + cashValue;

    if (finalTotal <= 0) return [];

    return [
        {
            name: '持仓市值',
            code: 'HOLDING',
            value: holdingValue,
            ratio: holdingValue / finalTotal,
        },
        {
            name: '可用资金',
            code: 'CASH',
            value: cashValue,
            ratio: cashValue / finalTotal,
        },
    ];
};

const resolveChartUserId = (userId: string): string => {
    const normalized = String(userId || '').trim();
    if (normalized && normalized !== 'current') return normalized;

    const storedUser = authService.getStoredUser() as { user_id?: string; id?: string } | null;
    const fromStored = String(storedUser?.user_id || storedUser?.id || '').trim();
    return fromStored || normalized;
};

const parseIsoDateFromTimestamp = (timestamp: string): string | null => {
    const normalized = String(timestamp || '').trim();
    if (!normalized) return null;
    if (/^\d{4}-\d{2}-\d{2}$/.test(normalized)) {
        return normalized;
    }
    const parsed = new Date(normalized);
    if (Number.isNaN(parsed.getTime())) return null;
    return parsed.toISOString().slice(0, 10);
};

const toUtcTradingTimestamp = (isoDate: string): string => `${isoDate}T00:00:00Z`;

const resolveAccountDailyReturnPct = (account: unknown): number | null => {
    const row = asRecord(account);
    if (!row) return null;

    const ratio = Number(row.daily_return_ratio);
    if (Number.isFinite(ratio)) {
        return ratio * 100;
    }

    const pct = Number(row.daily_return_pct);
    if (Number.isFinite(pct)) {
        return pct;
    }

    const legacy = Number(row.daily_return);
    if (Number.isFinite(legacy)) {
        return legacy;
    }

    return null;
};

const buildTradingCalendarSeries = (
    tradingDates: string[],
    sourcePoints: ChartDataPoint[],
    labelResolver?: (existing: ChartDataPoint | null, isoDate: string) => string | undefined,
): ChartDataPoint[] => {
    const valueMap = new Map<string, ChartDataPoint>();
    sourcePoints.forEach((point) => {
        const isoDate = parseIsoDateFromTimestamp(point.timestamp);
        if (!isoDate) return;
        valueMap.set(isoDate, {
            timestamp: toUtcTradingTimestamp(isoDate),
            value: Number.isFinite(point.value) ? point.value : 0,
            label: point.label,
        });
    });

    return tradingDates.map((isoDate) => {
        const existing = valueMap.get(isoDate) ?? null;
        return {
            timestamp: toUtcTradingTimestamp(isoDate),
            value: existing?.value ?? 0,
            label: labelResolver ? labelResolver(existing, isoDate) : existing?.label,
        } as ChartDataPoint;
    });
};

const getTradingDayWindow = async (anchorDate: string, count: number): Promise<string[]> => {
    const cacheKey = `${CHART_CALENDAR_MARKET}:${anchorDate}:${count}`;
    const cached = tradingDayWindowCache.get(cacheKey);
    if (cached) return cached;

    const promise = (async () => {
        const dates = [anchorDate];
        let cursor = anchorDate;
        while (dates.length < count) {
            const prev = await modelTrainingService.prevTradingDay(CHART_CALENDAR_MARKET, cursor);
            if (!prev || prev === cursor) break;
            dates.push(prev);
            cursor = prev;
        }
        return dates.reverse();
    })();

    tradingDayWindowCache.set(cacheKey, promise);
    return promise;
};

export const useIntelligenceCharts = (userId: string = 'current', options?: { autoRefresh?: boolean }) => {
    const autoFetchEnabled = options?.autoRefresh ?? chartsAutoFetchEnabled();
    const resolvedUserId = resolveChartUserId(userId);
    const tradingMode = useAppSelector((state) => state.ui.tradingMode);
    const [data, setData] = useState<ChartData>({
        dailyReturn: [],
        tradeCount: [],
        positionRatio: []
    });
    const [loading, setLoading] = useState(autoFetchEnabled);
    const [error, setError] = useState<string | null>(null);
    const [isStale, setIsStale] = useState<boolean>(false);
    const [lastUpdatedAt, setLastUpdatedAt] = useState<string | null>(null);
    const initializedRef = useRef<boolean>(false);
    const fingerprintRef = useRef<string | null>(null);
    const dataRef = useRef<ChartData>({
        dailyReturn: [],
        tradeCount: [],
        positionRatio: []
    });

    // WebSocket connection
    const { onMessage } = useWebSocket();

    const fetchData = useCallback(async (params?: { silent?: boolean }) => {
        if (!autoFetchEnabled) {
            setLoading(false);
            setError(null);
            return;
        }

        if (!resolvedUserId) {
            const emptyData: ChartData = { dailyReturn: [], tradeCount: [], positionRatio: [] };
            const { changed, fingerprint } = shouldUpdateByFingerprint(fingerprintRef.current, emptyData);
            if (changed) {
                setData(emptyData);
                dataRef.current = emptyData;
                fingerprintRef.current = fingerprint;
            }
            setIsStale(false);
            setLastUpdatedAt(null);
            setError(null);
            setLoading(false);
            initializedRef.current = true;
            return;
        }
        const silent = params?.silent ?? true;

        try {
            const [dailyReturn, tradeCount, positionRatio, account, ledgerDaily] = await Promise.all([
                portfolioService.getDailyReturns(resolvedUserId),
                tradingService.getTradeStats(resolvedUserId, '1w'),
                portfolioService.getPositionDistribution(resolvedUserId),
                realTradingService.getAccount(resolvedUserId).catch(() => null),
                realTradingService.getAccountLedgerDaily(30, resolvedUserId).catch(() => []),
            ]);

            let normalizedPositionRatio: PositionDistribution[] = [];
            
            // 优先从账户余额中计算比例，确保持仓市值和可用资金精准展示
            if (account) {
                normalizedPositionRatio = buildHoldingCashFromAccount(account);
            } 
            
            // 如果账户数据不可用，或者比例都是0，则尝试解析持仓分布
            if (normalizedPositionRatio.length === 0 || normalizedPositionRatio.every(v => v.value <= 0)) {
                normalizedPositionRatio = normalizePositionDistribution(positionRatio);
            }

            const ledgerPoints = Array.isArray(ledgerDaily)
                ? ledgerDaily
                    .map((row: any) => {
                        if (!row || typeof row.snapshot_date !== 'string') return null;
                        const pctValue = Number(row.daily_return_pct);
                        const ratioValue = Number(row.daily_return_ratio);
                        const legacyPctValue = Number(row.daily_return);
                        const returnValue = Number.isFinite(pctValue)
                            ? pctValue
                            : Number.isFinite(ratioValue)
                                ? ratioValue * 100
                                : Number.isFinite(legacyPctValue)
                                    ? legacyPctValue
                                    : Number.NaN;
                        if (!Number.isFinite(returnValue)) return null;
                        return {
                            timestamp: `${row.snapshot_date}T00:00:00Z`,
                            value: returnValue,
                            label: row.snapshot_kind === 'daily_ledger' ? '日账本收益率' : '账本日收益率',
                        } as ChartDataPoint;
                    })
                    .filter((item): item is ChartDataPoint => item !== null)
                : [];

            const normalizedReturnPoints = ledgerPoints.length > 0 ? ledgerPoints : normalizeChartPoints(dailyReturn);
            const fallbackAnchorDate = parseIsoDateFromTimestamp(new Date().toISOString()) || new Date().toISOString().slice(0, 10);
            const ledgerAnchorDate = parseIsoDateFromTimestamp(ledgerPoints[ledgerPoints.length - 1]?.timestamp || '');
            const resolvedAnchor = await modelTrainingService.resolveInferenceDateByCalendar(
                CHART_CALENDAR_MARKET,
                ledgerAnchorDate || fallbackAnchorDate,
            );
            const anchorTradingDate = resolvedAnchor.date || ledgerAnchorDate || fallbackAnchorDate;
            const [recentDailyTradingDates, recentTradeTradingDates] = await Promise.all([
                getTradingDayWindow(anchorTradingDate, 30),
                getTradingDayWindow(anchorTradingDate, 7),
            ]);

            const todayReturnPct = resolveAccountDailyReturnPct(account);
            const returnSourcePoints = normalizedReturnPoints.slice();
            if (Number.isFinite(Number(todayReturnPct))) {
                returnSourcePoints.push({
                    timestamp: toUtcTradingTimestamp(anchorTradingDate),
                    value: Number(todayReturnPct),
                    label: '今日实时',
                });
            }

            const returnPoints = buildTradingCalendarSeries(
                recentDailyTradingDates,
                returnSourcePoints,
                (existing, isoDate) => (isoDate === anchorTradingDate ? (existing?.label || '今日实时') : existing?.label),
            );
            const tradeCountPoints = buildTradingCalendarSeries(
                recentTradeTradingDates,
                Array.isArray(tradeCount) ? tradeCount : [],
            );

            const nextData = {
                dailyReturn: returnPoints,
                tradeCount: tradeCountPoints,
                positionRatio: normalizedPositionRatio,
            };
            const { changed, fingerprint } = shouldUpdateByFingerprint(fingerprintRef.current, nextData);
            if (changed) {
                setData(nextData);
                dataRef.current = nextData;
                fingerprintRef.current = fingerprint;
            }
            setIsStale(false);
            setLastUpdatedAt(new Date().toISOString());
            setError(null);
        } catch (err) {
            setError(err instanceof Error ? err.message : 'Failed to fetch chart data');
            setIsStale(
                dataRef.current.dailyReturn.length > 0 ||
                dataRef.current.tradeCount.length > 0 ||
                dataRef.current.positionRatio.length > 0,
            );
        } finally {
            initializedRef.current = true;
            setLoading(false);
        }
    }, [autoFetchEnabled, resolvedUserId, tradingMode]);

    useEffect(() => {
        if (!autoFetchEnabled) {
            setLoading(false);
            return;
        }
        fetchData({ silent: false });
    }, [autoFetchEnabled, fetchData]);

    useEffect(() => {
        setLoading(true);
        setData({ dailyReturn: [], tradeCount: [], positionRatio: [] });
        fingerprintRef.current = null;
    }, [tradingMode]);

    useEffect(() => {
        const unsubscribe = onMessage((type, payload) => {
            // 监听 chart_update 类型消息
            if (String(type) === 'chart_update' || payload?.type === 'chart_update') {
                const updateData = payload?.data || payload;
                const { chartType, value } = updateData;

                if (!chartType || value === undefined) return;

                const prev = dataRef.current;
                const updateState = (() => {
                    if (chartType === 'dailyReturn' || chartType === 'tradeCount') {
                        const currentList = prev[chartType];
                        const newPoint: ChartDataPoint = {
                            timestamp: new Date().toISOString(),
                            value: value,
                            label: '实时数据'
                        };
                        // 保持最近30个数据点
                        const nextData = {
                            ...prev,
                            [chartType]: [...currentList.slice(Math.max(0, currentList.length - 29)), newPoint]
                        };
                        return nextData;
                    } else if (chartType === 'positionRatio') {
                        // 不处理持仓占比的单个数值更新，通常它是全量更新
                        // 如果 payload 包含 fullData
                        if (Array.isArray(updateData.fullData)) {
                            const nextData = {
                                ...prev,
                                positionRatio: updateData.fullData
                            };
                            return nextData;
                        }
                    }
                    return null;
                })();

                if (updateState) {
                    dataRef.current = updateState;
                    fingerprintRef.current = calcFingerprint(updateState);
                    setData(updateState);
                }
            }
        });
        return unsubscribe;
    }, [onMessage]);

    useEffect(() => {
        if (!autoFetchEnabled) {
            return;
        }
        const unregister = refreshOrchestrator.register(
            'intelligence-charts',
            async () => {
                await fetchData({ silent: true });
            },
            { minIntervalMs: 1200 },
        );

        return unregister;
    }, [autoFetchEnabled, fetchData]);

    const hasDailyReturn = data.dailyReturn.length > 0;
    const hasTradeCount = data.tradeCount.length > 0;
    const hasPositionRatio = data.positionRatio.length > 0;

    return {
        data,
        loading,
        error,
        isStale,
        lastUpdatedAt,
        hasDailyReturn,
        hasTradeCount,
        hasPositionRatio,
        refresh: fetchData,
    };
};
