/**
 * 持仓监控 · 来源条：**账户源**（看哪个券商账户）+ **行情源**（谁在喂这些价）。
 *
 * 为什么单起一条：多券商并行上报时，QMT（50 只 / 2388 万）与通达信（8 只 / 92 万）是
 * 两个**互不相交的真实账户**，两条流每 30s 交错写库。此前页面读「最新一行」，
 * 现价/市值/持仓数就在两个账户之间来回跳；页面上那条「行情来源」写的又是另一条链路的
 * 心跳（TDX 持仓馈送 bridge_ok），与真正写 key 的席位无关 —— 于是文案也在两个源之间跳。
 *
 * 现在两个问题各有各的答案：
 * - 账户源：chips 给出每个源的总资产/持仓数/新鲜度，点一下 = 只看它（改 view 偏好）；
 *   「设为交易券商」是独立按钮 + 二次确认（改下单路由，两件事不混）。
 * - 行情源：读后端按持仓标的**实际采样** `market:snapshot:*` 的 source 字段聚合结果，
 *   如实标出主供数方与「同池还有谁在写」；多写席并存是要暴露的问题，不是要藏起来的。
 */

import React, { useCallback, useEffect, useState } from 'react';
import { AlertTriangle, ArrowLeftRight, Radio, TrendingUp, Wallet } from 'lucide-react';
import { authService } from '../../../features/auth/services/authService';
import { SERVICE_URLS } from '../../../config/services';
import { realTradingService } from '../../../services/realTradingService';
import type { AccountSourceItem, AccountSourcesPayload } from '../../../services/realTradingService';
import { ConfirmDialog } from '../../../components/ui/ConfirmDialog';
import {
    getPreferredAccountSource,
    setPreferredAccountSource,
} from '../utils/accountSourcePreference';
import {
    extraQuoteWriters,
    formatAge,
    formatMoney,
    freshnessStyle,
    nextPreferredSource,
    quoteSampleSymbols,
    resolveViewedSource,
    type QuoteSourcesPayload,
} from '../utils/positionSourceModel';

const apiBase = `${SERVICE_URLS.API_GATEWAY}/api/v1`;
const POLL_MS = 15000;

interface QuoteFeedStatusPayload {
    quote_sources?: QuoteSourcesPayload;
    is_trading_time?: boolean;
    symbols?: string[];
}

interface PositionSourceBarProps {
    market: string;
    isActive: boolean;
    /** 本轮账户快照实际来自哪个源（/account 的 account_source） */
    accountSource?: string | null;
    /** 请求源不可用、已退回全源最新（数字不是你要的那个账户） */
    sourceDowngraded?: boolean;
    sourceDowngradedReason?: string | null;
    /** 模拟盘没有「券商账户源」概念，只显示行情源那一行 */
    accountMode?: 'simulation' | 'real';
    /** 当前账户持仓代码（采样「谁在喂这些价」的标的集，缺省才用馈送自己的监控名单） */
    positionCodes?: string[];
    /** 切换交易券商成功后 → 让宿主重取账户 */
    onBrokerChange?: () => void;
}

export const PositionSourceBar: React.FC<PositionSourceBarProps> = ({
    market,
    isActive,
    accountSource,
    sourceDowngraded,
    sourceDowngradedReason,
    accountMode = 'simulation',
    positionCodes,
    onBrokerChange,
}) => {
    const isCn = market === 'CN';
    const isReal = accountMode === 'real';
    const [sources, setSources] = useState<AccountSourcesPayload | null>(null);
    const [feed, setFeed] = useState<QuoteFeedStatusPayload | null>(null);
    const [preferred, setPreferredSource] = useState<string | null>(() => getPreferredAccountSource(market));
    const [pendingBroker, setPendingBroker] = useState<AccountSourceItem | null>(null);
    const [switching, setSwitching] = useState(false);
    const [notice, setNotice] = useState<string | null>(null);

    const authHeaders = useCallback(() => {
        const token = authService.getAccessToken();
        return {
            'Content-Type': 'application/json',
            ...(token ? { Authorization: `Bearer ${token}` } : {}),
        };
    }, []);

    // 采样集随「当前在看哪个账户」变：点 chip 换源后重新取该账户持仓的行情源
    const sampleSymbols = quoteSampleSymbols(positionCodes);

    const load = useCallback(async () => {
        const statusUrl = sampleSymbols
            ? `${apiBase}/tdx/quote-feed/status?symbols=${encodeURIComponent(sampleSymbols)}`
            : `${apiBase}/tdx/quote-feed/status`;
        const [sourcePayload, feedStatus] = await Promise.all([
            isReal ? realTradingService.getAccountSources().catch(() => null) : Promise.resolve(null),
            fetch(statusUrl, { headers: authHeaders() })
                .then(res => (res.ok ? res.json() : null))
                .catch(() => null),
        ]);
        setSources(sourcePayload);
        setFeed(feedStatus);
    }, [isReal, authHeaders, sampleSymbols]);

    useEffect(() => {
        if (!isActive || !isCn) return;
        void load();
        const timer = setInterval(() => void load(), POLL_MS);
        return () => clearInterval(timer);
    }, [isActive, isCn, load]);

    useEffect(() => {
        setPreferredSource(getPreferredAccountSource(market));
    }, [market]);

    /** 只改「看哪个源」：先写偏好（广播给宿主页面立即重取），再同步本地选中态 */
    const applyPreferred = useCallback(
        (source: string | null) => {
            setPreferredAccountSource(source, market);
            setPreferredSource(source);
        },
        [market],
    );

    // 交易券商切换是**下单路由**变更，必须二次确认（不是「看一眼另一个账户」那种点击）
    const confirmBrokerSwitch = useCallback(async () => {
        const item = pendingBroker;
        setPendingBroker(null);
        if (!item?.broker) return;
        setSwitching(true);
        setNotice(null);
        try {
            const resp = await fetch(`${apiBase}/broker-config/selected/CN`, {
                method: 'PUT',
                headers: authHeaders(),
                body: JSON.stringify({ broker: item.broker }),
            });
            if (!resp.ok) {
                const detail = await resp.json().catch(() => null);
                throw new Error(detail?.detail || `HTTP ${resp.status}`);
            }
            setNotice(`已把「${item.label}」设为 A 股交易券商`);
            setPreferredAccountSource(null, market);
            setPreferredSource(getPreferredAccountSource(market));
            onBrokerChange?.();
            await load();
        } catch (e: unknown) {
            setNotice(`切换失败：${e instanceof Error ? e.message : String(e)}`);
        } finally {
            setSwitching(false);
        }
    }, [pendingBroker, authHeaders, market, onBrokerChange, load]);

    if (!isCn) return null;

    const viewed = resolveViewedSource(preferred, sources?.selected_source);
    const quote = feed?.quote_sources;
    const quoteStyle = freshnessStyle(quote?.level);
    const others = extraQuoteWriters(quote);

    return (
        <div className="shrink-0 rounded-xl border border-slate-200 bg-white/80 divide-y divide-slate-100">
            {isReal && (
                <div className="flex items-center gap-2 px-3 py-1.5 text-[11px] flex-wrap">
                    <span className="inline-flex items-center gap-1 font-black text-slate-500 shrink-0">
                        <Wallet className="w-3 h-3" /> 持仓账户
                    </span>
                    {(sources?.sources || []).length === 0 ? (
                        <span className="text-slate-400">暂无券商快照上报</span>
                    ) : (
                        (sources?.sources || []).map(item => {
                            const style = freshnessStyle(item.freshness);
                            const isViewing = viewed === item.source;
                            const isOrderRoute = !!item.selected;
                            const chipClass = isViewing
                                ? 'border-slate-800 bg-slate-900 text-white'
                                : 'border-slate-200 bg-white text-slate-600 hover:border-slate-400';
                            return (
                                <span
                                    key={item.source}
                                    className={`inline-flex items-center gap-1.5 rounded-lg border px-2 py-1 transition-colors ${chipClass}`}
                                >
                                    <button
                                        type="button"
                                        onClick={() => applyPreferred(nextPreferredSource(preferred, item.source))}
                                        title={`只看「${item.label}」的账户快照（不改下单路由）`}
                                        className="inline-flex items-center gap-1.5 font-bold"
                                    >
                                        <span className={`w-1.5 h-1.5 rounded-full ${style.dot}`} />
                                        {item.label}
                                        <span className={`font-mono tabular-nums ${isViewing ? 'text-white/70' : 'text-slate-400'}`}>
                                            {item.position_count ?? 0} 只 · {formatMoney(item.total_asset)} · {formatAge(item.age_sec)}
                                        </span>
                                    </button>
                                    {isOrderRoute && (
                                        <span
                                            title="下单走这个账户"
                                            className={`text-[9px] font-black px-1 rounded ${isViewing ? 'bg-white/20 text-white' : 'bg-slate-900 text-white'}`}
                                        >
                                            交易
                                        </span>
                                    )}
                                    {item.selectable && !isOrderRoute && (
                                        <button
                                            type="button"
                                            disabled={switching}
                                            onClick={() => setPendingBroker(item)}
                                            title={`把「${item.label}」设为 A 股交易券商（改下单路由）`}
                                            className={`inline-flex items-center gap-0.5 text-[9px] font-bold px-1 rounded border disabled:opacity-50 ${
                                                isViewing
                                                    ? 'border-white/30 text-white/80 hover:bg-white/10'
                                                    : 'border-slate-200 text-slate-500 hover:border-slate-400'
                                            }`}
                                        >
                                            <ArrowLeftRight className="w-2.5 h-2.5" />
                                            设为交易券商
                                        </button>
                                    )}
                                </span>
                            );
                        })
                    )}
                    {preferred && (
                        <button
                            type="button"
                            onClick={() => applyPreferred(null)}
                            className="text-[10px] font-bold text-blue-600 hover:underline shrink-0"
                        >
                            跟随交易券商
                        </button>
                    )}
                    {sources?.selected_source && sources.selected_source_explicit === false && (
                        <span className="text-[10px] text-slate-400 shrink-0">
                            （交易券商未在页面选定，当前按环境配置 = {sources.selected_source_label}）
                        </span>
                    )}
                </div>
            )}

            <div className="flex items-center gap-2 px-3 py-1.5 text-[11px] flex-wrap">
                <span className="inline-flex items-center gap-1 font-black text-slate-500 shrink-0">
                    <Radio className="w-3 h-3" /> 行情来源
                </span>
                {quote?.error ? (
                    <span className="inline-flex items-center gap-1.5 font-bold text-rose-600">
                        <AlertTriangle className="w-3 h-3" />
                        {quote.error}
                    </span>
                ) : quote?.dominant ? (
                    <span className={`inline-flex items-center gap-1.5 font-bold ${quoteStyle.text}`}>
                        <span className={`w-1.5 h-1.5 rounded-full ${quoteStyle.dot} ${quote.level === 'fresh' ? 'animate-pulse' : ''}`} />
                        {quote.dominant_label || quote.dominant}
                        <span className="font-mono tabular-nums font-medium">{formatAge(quote.newest_age_sec)}</span>
                        <span className={`text-[10px] font-black px-1 rounded bg-slate-100 ${quoteStyle.text}`}>{quoteStyle.label}</span>
                    </span>
                ) : (
                    <span className="inline-flex items-center gap-1.5 font-bold text-amber-600">
                        <span className="w-1.5 h-1.5 rounded-full bg-amber-400" />
                        {quote
                            ? `持仓标的本轮无实时快照（采样 ${quote.requested ?? 0} 只）· 显示 QuantDB 日线兜底`
                            : '行情源未采样'}
                    </span>
                )}
                {others.length > 0 && (
                    <span
                        className="inline-flex items-center gap-1 text-[10px] font-bold text-amber-700 bg-amber-50 border border-amber-200 rounded px-1.5 py-0.5"
                        title="同一批键被多个写席轮流写：现价可能在两值间跳，直到席位阈值收敛"
                    >
                        <AlertTriangle className="w-2.5 h-2.5" />
                        同池另有写源：{others.map(s => `${s.label} ${s.count} 只`).join('、')}
                    </span>
                )}
                {feed?.is_trading_time === false && (
                    <span className="text-slate-400 font-medium">（非交易时段）</span>
                )}
                {sourceDowngraded && (
                    <span
                        className="inline-flex items-center gap-1 font-bold text-rose-600"
                        title={sourceDowngradedReason || ''}
                    >
                        <AlertTriangle className="w-3 h-3" />
                        请求的账户源不可用 · 显示的是全源最新
                        {accountSource ? `（${accountSource}）` : ''}
                    </span>
                )}
                {notice && <span className="ml-auto text-[10px] text-slate-500">{notice}</span>}
                <span
                    className="ml-auto text-slate-300 font-mono text-[10px] inline-flex items-center gap-1"
                    title="行情源按「当前显示的持仓」采样；馈送监控名单是 TDX 馈送自己的口径，两者不同"
                >
                    <TrendingUp className="w-2.5 h-2.5" />
                    采样 {quote?.requested ?? 0} 只 · 馈送 {feed?.symbols?.length ?? 0} 只
                </span>
            </div>

            <ConfirmDialog
                isOpen={!!pendingBroker}
                isDanger
                title="切换 A 股交易券商？"
                message={
                    pendingBroker
                        ? `把「${pendingBroker.label}」设为 A 股实盘交易券商。\n\n`
                          + '这会改变下单走的账户（新委托、预检、风控都按它算），不只是切换显示。'
                        : ''
                }
                confirmText="切换"
                onConfirm={() => void confirmBrokerSwitch()}
                onCancel={() => setPendingBroker(null)}
            />
        </div>
    );
};

export default PositionSourceBar;
