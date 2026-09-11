import { useAppSelector } from '../../../store';
import { selectCurrentMarket } from '../../../store/slices/uiSlice';
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { AccountInfo } from '../../../services/realTradingService';
import { marketDataService } from '../../../services/marketDataService';
import { websocketService, MessageType } from '../../../services/websocketService';
import { buildNormalizedHoldings, extractPositionCodes, getPositionSummary, NormalizedHolding } from '../utils/positionMetrics';
import PositionOverview from '../components/PositionOverview';
import { SERVICE_URLS } from '../../../config/services';

interface PositionMonitorProps {
    userId: string;
    isActive: boolean;
    accountInfo: AccountInfo | null;
}

/** stream 服务推送的实时行情消息（topic stock.{code}） */
interface LiveQuote {
    stock_code: string;
    data?: {
        price?: number | null;
        open?: number | null;
        high?: number | null;
        low?: number | null;
        is_stale?: boolean;
        timestamp?: string | number;
    };
}

interface QuoteFeedStatus {
    running?: boolean;
    bridge_ok?: boolean;
    last_feed_at?: string | null;
    last_feed_age_sec?: number | null;
    symbols?: string[];
    is_trading_time?: boolean;
    last_error?: string | null;
}

const authHeader = () => ({
    'Content-Type': 'application/json',
    Authorization: `Bearer ${localStorage.getItem('access_token') || ''}`,
});

/** 持仓明细叠加实时价：现价/市值/盈亏全部按 live price 重算 */
const mergeLivePrices = (holdings: NormalizedHolding[], live: Record<string, number>): NormalizedHolding[] => {
    return holdings.map(h => {
        const price = live[h.code];
        if (price == null || !Number.isFinite(price) || price <= 0) return h;
        const value = h.shares * price;
        const profit = h.cost > 0 ? (price - h.cost) * h.shares : 0;
        const costValue = h.shares * h.cost;
        return {
            ...h,
            current: price,
            value,
            profit,
            profitPercent: costValue > 0 ? (profit / costValue) * 100 : 0,
        };
    });
};

const PositionMonitor: React.FC<PositionMonitorProps> = ({ userId: _userId, isActive, accountInfo }) => {
    const currentMarket = useAppSelector(selectCurrentMarket);
    const [stockNames, setStockNames] = useState<Record<string, string>>({});
    const [livePrices, setLivePrices] = useState<Record<string, number>>({});
    const livePricesRef = useRef<Record<string, number>>({});
    const [feedStatus, setFeedStatus] = useState<QuoteFeedStatus | null>(null);
    const subscribedRef = useRef<string[]>([]);
    const apiGatewayBase = SERVICE_URLS.API_GATEWAY.replace(/\/+$/, '');

    React.useEffect(() => {
        if (!accountInfo || !accountInfo.positions) return;

        const codes = extractPositionCodes(accountInfo).filter(code => !stockNames[code]);
        if (codes.length === 0) return;

        const fetchNames = async () => {
            try {
                const results = await marketDataService.getStockDetailsBatch(codes, 10, 50);
                const newNames: Record<string, string> = {};
                results.forEach(({ code, result }) => {
                    if (result.success && result.data?.name) {
                        newNames[code] = result.data.name;
                    }
                });
                if (Object.keys(newNames).length > 0) {
                    (setStockNames as any)(prev => ({ ...prev, ...newNames }));
                }
            } catch (err) {
                console.error('Failed to fetch stock names in batch:', err);
            }
        };
        fetchNames();
    }, [accountInfo, stockNames]);

    // 订阅持仓股实时行情（topic stock.{code}，stream 服务 2s 推一次）
    useEffect(() => {
        if (!isActive) return;
        const codes = extractPositionCodes(accountInfo);
        if (codes.length === 0) return;
        const toSubscribe = codes.filter(c => !subscribedRef.current.includes(c));
        if (toSubscribe.length === 0) return;
        subscribedRef.current = [...subscribedRef.current, ...toSubscribe];
        websocketService.subscribe({ symbols: toSubscribe });
    }, [isActive, accountInfo]);

    useEffect(() => {
        if (!isActive) return;
        const handler = (data: unknown) => {
            const msg = data as LiveQuote;
            const code = String(msg?.stock_code || '').toUpperCase();
            const price = Number(msg?.data?.price);
            if (!code || !Number.isFinite(price) || price <= 0) return;
            const next = { ...livePricesRef.current, [code]: price };
            livePricesRef.current = next;
            setLivePrices(next);
        };
        websocketService.addMessageHandler('quote' as MessageType, handler);
        return () => {
            websocketService.removeMessageHandler('quote' as MessageType, handler);
        };
    }, [isActive]);

    // 退页时退订持仓行情
    useEffect(() => {
        if (isActive || subscribedRef.current.length === 0) return;
        websocketService.unsubscribe(subscribedRef.current);
        subscribedRef.current = [];
    }, [isActive]);

    // 行情 Feed 状态轮询（Data Feed 检查口径：last_feed_age < 300s）
    const fetchFeedStatus = useCallback(async () => {
        try {
            const res = await fetch(`${apiGatewayBase}/api/v1/tdx/quote-feed/status`, {
                headers: authHeader(),
            });
            if (res.status === 403) {
                setFeedStatus(null);
                return;
            }
            if (res.ok) {
                setFeedStatus(await res.json());
            }
        } catch (e) {
            console.error('Failed to fetch quote feed status', e);
        }
    }, [apiGatewayBase]);

    useEffect(() => {
        if (!isActive) return;
        fetchFeedStatus();
        const timer = setInterval(fetchFeedStatus, 20000);
        return () => clearInterval(timer);
    }, [isActive, fetchFeedStatus]);

    const holdings = React.useMemo(() => {
        return mergeLivePrices(buildNormalizedHoldings(accountInfo, stockNames), livePrices);
    }, [accountInfo, stockNames, livePrices]);

    const summary = React.useMemo(
        () => getPositionSummary(accountInfo, holdings),
        [accountInfo, holdings],
    );

    if (!isActive) return null;

    const feedLive = !!feedStatus?.bridge_ok && (feedStatus?.last_feed_age_sec ?? 999) < 300;

    return (
        <div className="h-full p-2.5 pb-[50px] flex flex-col gap-2">
            {/* 实时行情来源指示：TDX 桥实时 vs QuantDB 日线兜底（仅 CN；其余市场为日线数据） */}
            <div className={`flex items-center gap-2 px-3 py-1.5 rounded-xl border bg-white/70 text-[11px] shrink-0 ${currentMarket !== 'CN' ? 'hidden' : ''}`}>
                <span className="font-black text-slate-500">行情来源</span>
                {feedLive ? (
                    <span className="inline-flex items-center gap-1.5 font-bold text-emerald-600">
                        <span className="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse" />
                        通达信实时（{feedStatus!.last_feed_age_sec}s 前）
                    </span>
                ) : (
                    <span className="inline-flex items-center gap-1.5 font-bold text-amber-600">
                        <span className="w-1.5 h-1.5 rounded-full bg-amber-400" />
                        QuantDB 日线兜底{feedStatus?.last_error ? ` · ${feedStatus.last_error}` : ''}
                    </span>
                )}
                {feedStatus?.is_trading_time === false && (
                    <span className="text-slate-400 font-medium">（非交易时段）</span>
                )}
                <span className="ml-auto text-slate-300 font-mono text-[10px]">
                    监控 {feedStatus?.symbols?.length ?? 0} 只持仓 · 实时提醒仅限持仓股
                </span>
            </div>
            <div className="flex-1 min-h-0">
                <PositionOverview holdings={holdings} summary={summary} variant="full" />
            </div>
        </div>
    );
};

export default PositionMonitor;
