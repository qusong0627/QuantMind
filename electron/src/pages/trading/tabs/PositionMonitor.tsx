import { useAppSelector } from '../../../store';
import { selectCurrentMarket } from '../../../store/slices/uiSlice';
import React, { useEffect, useRef, useState } from 'react';

import { AccountInfo } from '../../../services/realTradingService';
import { marketDataService } from '../../../services/marketDataService';
import { websocketService, MessageType } from '../../../services/websocketService';
import { buildNormalizedHoldings, extractPositionCodes, getPositionSummary, NormalizedHolding } from '../utils/positionMetrics';
import PositionOverview from '../components/PositionOverview';
import { normalizeSymbol } from '../../../utils/portfolioUtils';

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
        /** 昨收：与 Now 同源的 Redis 快照 PreClose/Close（老推送可能缺失） */
        pre_close?: number | null;
        close_price?: number | null;
        open?: number | null;
        high?: number | null;
        low?: number | null;
        is_stale?: boolean;
        timestamp?: string | number;
    };
}

/** 持仓明细叠加实时价：现价/市值/盈亏全部按 live price 重算，当日收益按昨收重算 */
const mergeLivePrices = (
    holdings: NormalizedHolding[],
    live: Record<string, number>,
    prevCloseMap: Record<string, number> = {},
): NormalizedHolding[] => {
    return holdings.map(h => {
        const prev = prevCloseMap[h.code];
        const base = prev != null && Number.isFinite(prev) && prev > 0
            ? { ...h, prevClose: prev }
            : h;
        const price = live[h.code];
        if (price == null || !Number.isFinite(price) || price <= 0) {
            // 无实时价时，若刚拿到昨收也把当日收益补算一次
            if (base !== h && base.prevClose > 0 && base.current > 0) {
                const dp = (base.current - base.prevClose) * base.shares;
                const dv = base.prevClose * base.shares;
                return { ...base, dailyProfit: dp, dailyProfitPercent: dv > 0 ? (dp / dv) * 100 : 0 };
            }
            return base;
        }
        const value = base.shares * price;
        const profit = base.cost > 0 ? (price - base.cost) * base.shares : 0;
        const costValue = base.shares * base.cost;
        const dailyProfit = base.prevClose > 0
            ? (price - base.prevClose) * base.shares
            : null;
        const dailyBase = base.prevClose > 0 ? base.prevClose * base.shares : 0;
        return {
            ...base,
            current: price,
            value,
            profit,
            profitPercent: costValue > 0 ? (profit / costValue) * 100 : 0,
            dailyProfit,
            dailyProfitPercent: dailyProfit == null ? null : dailyBase > 0 ? (dailyProfit / dailyBase) * 100 : 0,
        };
    });
};

const PositionMonitor: React.FC<PositionMonitorProps> = ({ userId: _userId, isActive, accountInfo }) => {
    const currentMarket = useAppSelector(selectCurrentMarket);
    const [stockNames, setStockNames] = useState<Record<string, string>>({});
    const [livePrices, setLivePrices] = useState<Record<string, number>>({});
    const [livePrevClose, setLivePrevClose] = useState<Record<string, number>>({});
    const livePricesRef = useRef<Record<string, number>>({});
    const livePrevCloseRef = useRef<Record<string, number>>({});
    const subscribedRef = useRef<string[]>([]);

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
            const code = normalizeSymbol(String(msg?.stock_code || ''));
            if (!code) return;
            const price = Number(msg?.data?.price);
            const prevRaw = msg?.data?.pre_close ?? msg?.data?.close_price;
            const prev = Number(prevRaw);
            let priceChanged = false;
            let prevChanged = false;
            if (Number.isFinite(price) && price > 0) {
                const next = { ...livePricesRef.current, [code]: price };
                livePricesRef.current = next;
                setLivePrices(next);
                priceChanged = true;
            }
            if (Number.isFinite(prev) && prev > 0 && livePrevCloseRef.current[code] !== prev) {
                const nextPrev = { ...livePrevCloseRef.current, [code]: prev };
                livePrevCloseRef.current = nextPrev;
                setLivePrevClose(nextPrev);
                prevChanged = true;
            }
            if (!priceChanged && !prevChanged) return;
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

    const holdings = React.useMemo(() => {
        return mergeLivePrices(buildNormalizedHoldings(accountInfo, stockNames, livePrevClose), livePrices, livePrevClose);
    }, [accountInfo, stockNames, livePrices, livePrevClose]);

    const summary = React.useMemo(
        () => getPositionSummary(accountInfo, holdings),
        [accountInfo, holdings],
    );

    if (!isActive) return null;

    return (
        <div className="h-full p-2.5 pb-[50px] flex flex-col gap-2">
            {/* 行情来源：全市场远程 Redis（通达信内网桥已下线） */}
            <div className={`flex items-center gap-2 px-3 py-1.5 rounded-xl border bg-white/70 text-[11px] shrink-0 ${currentMarket !== 'CN' ? 'hidden' : ''}`}>
                <span className="font-black text-slate-500">行情来源</span>
                <span className="inline-flex items-center gap-1.5 font-bold text-emerald-600">
                    <span className="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse" />
                    远程全市场行情 Redis（延时约 1–2 分钟）
                </span>
                <span className="ml-auto text-slate-300 font-mono text-[10px]">
                    监控 {extractPositionCodes(accountInfo).length} 只持仓
                </span>
            </div>
            <div className="flex-1 min-h-0">
                <PositionOverview holdings={holdings} summary={summary} variant="full" />
            </div>
        </div>
    );
};

export default PositionMonitor;
