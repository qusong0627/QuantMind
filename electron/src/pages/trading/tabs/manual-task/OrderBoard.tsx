/**
 * 手动任务第 4 步左侧的两块台账：委托单行列表 + 风控拦截分组。
 *
 * 改版前每笔委托是一张 ~150px 的卡片、每条拦截也是一张卡片（实测 653 条），
 * 一屏放不下 4 笔委托，风控那一块更是滚不到底。这里只做「减法」：
 * 同样这些字段，委托压成单行、拦截按原因归组，数据一个不少。
 *
 * 配色遵循 A 股口径：买红卖绿（见 memory: feedback-astock-red-green）。
 */

import React, { useState } from 'react';
import { ChevronDown, ShieldAlert, ShieldCheck } from 'lucide-react';
import type {
    ManualExecutionPreviewOrder,
    ManualExecutionPreviewSkippedItem,
} from '../../../../services/realTradingService';
import { normalizeStockCode } from '../../../../utils/portfolioUtils';
import { commonReason, formatMoney, orderRowView, summarizeSkipped, type TradeSide } from './manualTaskModel';

/** 展开一类拦截时最多铺多少个标的 chip，其余折叠成 +N */
const SYMBOLS_PREVIEW_LIMIT = 60;

const SIDE_TONE: Record<TradeSide, { chip: string; accent: string; amount: string }> = {
    buy: {
        chip: 'bg-rose-50 text-rose-600 border border-rose-100',
        accent: 'bg-rose-500',
        amount: 'text-rose-600',
    },
    sell: {
        chip: 'bg-emerald-50 text-emerald-600 border border-emerald-100',
        accent: 'bg-emerald-500',
        amount: 'text-emerald-600',
    },
};

const actionLabel = (action: string): string => {
    if (action === 'SELL') return '卖';
    if (action === 'BUY') return '买';
    return '过滤';
};

const actionTone = (action: string): string => {
    if (action === 'SELL') return 'bg-emerald-50 text-emerald-600';
    if (action === 'BUY') return 'bg-rose-50 text-rose-600';
    return 'bg-gray-100 text-gray-500';
};

const OrderRow: React.FC<{ order: ManualExecutionPreviewOrder; showReason: boolean }> = ({ order, showReason }) => {
    const row = orderRowView(order);
    const tone = SIDE_TONE[row.side];

    return (
        <div className="group flex items-center gap-3 px-3 py-2 transition-colors hover:bg-gray-50/70">
            <span className={`flex h-6 w-6 shrink-0 items-center justify-center rounded-md text-[10px] font-black ${tone.chip}`}>
                {row.sideLabel}
            </span>
            <div className="min-w-0 flex-1">
                <div className="flex items-center gap-1.5">
                    <span className="shrink-0 font-mono text-[11px] font-bold text-gray-900">{normalizeStockCode(row.symbol)}</span>
                    {row.name && <span className="truncate text-[10px] text-gray-400">{row.name}</span>}
                    {showReason && row.reason && (
                        <span className="ml-auto max-w-[140px] shrink-0 truncate text-[9px] text-gray-300" title={row.reason}>
                            {row.reason}
                        </span>
                    )}
                </div>
                <div className="mt-0.5 font-mono text-[10px] text-gray-400">
                    {row.quantityText} 股 · {row.orderTypeText} {row.priceText} · {row.positionText}
                </div>
            </div>
            <div className="shrink-0 text-right">
                <div className={`font-mono text-[11px] font-bold ${row.hasPrice ? 'text-gray-900' : 'text-gray-300'}`}>
                    {row.amountText}
                </div>
                <div className="font-mono text-[9px] text-gray-300">{row.referenceText}</div>
            </div>
        </div>
    );
};

interface OrderColumnProps {
    title: string;
    side: TradeSide;
    orders: ManualExecutionPreviewOrder[];
}

export const OrderColumn: React.FC<OrderColumnProps> = ({ title, side, orders }) => {
    const tone = SIDE_TONE[side];
    const totalNotional = orders.reduce((sum, order) => {
        const notional = Number(order?.estimated_notional);
        return sum + (Number.isFinite(notional) ? notional : 0);
    }, 0);
    const sharedReason = commonReason(orders);

    return (
        <section className="flex flex-col overflow-hidden rounded-xl border border-gray-100 bg-white">
            <header className="flex items-center justify-between border-b border-gray-50 px-3 py-2">
                <div className="flex items-center gap-2">
                    <span className={`h-3.5 w-1 rounded-full ${tone.accent}`} />
                    <h4 className="text-[11px] font-bold uppercase tracking-wider text-gray-900">{title}</h4>
                    <span className={`rounded-md px-1.5 py-px font-mono text-[10px] font-bold ${tone.chip}`}>{orders.length}</span>
                </div>
                <span className={`font-mono text-[10px] font-bold ${orders.length > 0 ? tone.amount : 'text-gray-300'}`}>
                    {formatMoney(totalNotional)}
                </span>
            </header>
            {sharedReason && (
                <div className="truncate border-b border-gray-50 bg-gray-50/50 px-3 py-1 text-[9px] text-gray-400" title={sharedReason}>
                    本列统一说明：{sharedReason}
                </div>
            )}
            <div className="max-h-[420px] divide-y divide-gray-50 overflow-y-auto custom-scrollbar">
                {orders.length > 0 ? (
                    orders.map((order) => (
                        <OrderRow
                            key={`${order.side}-${order.symbol}-${order.quantity}-${order.price ?? 0}`}
                            order={order}
                            showReason={!sharedReason}
                        />
                    ))
                ) : (
                    <div className="py-10 text-center text-[10px] font-bold uppercase tracking-widest text-gray-300">
                        本次无委托
                    </div>
                )}
            </div>
        </section>
    );
};

interface RiskGroupListProps {
    items?: ManualExecutionPreviewSkippedItem[] | null;
}

export const RiskGroupList: React.FC<RiskGroupListProps> = ({ items }) => {
    const [expanded, setExpanded] = useState<Set<string>>(new Set());
    const risk = summarizeSkipped(items);

    const toggle = (key: string) => {
        const next = new Set(expanded);
        if (next.has(key)) {
            next.delete(key);
        } else {
            next.add(key);
        }
        setExpanded(next);
    };

    const hasRisk = risk.total > 0;

    return (
        <section
            className={`overflow-hidden rounded-xl border ${hasRisk ? 'border-rose-100 bg-rose-50/30' : 'border-emerald-100 bg-emerald-50/30'}`}
        >
            <header className="flex items-center justify-between px-3 py-2">
                <div className={`flex items-center gap-2 text-[11px] font-bold ${hasRisk ? 'text-rose-700' : 'text-emerald-700'}`}>
                    {hasRisk ? <ShieldAlert size={13} /> : <ShieldCheck size={13} />}
                    <span>风控 / 过滤</span>
                    <span className="font-mono text-[12px]">{risk.total}</span>
                    <span className="text-[10px] font-medium text-gray-400">笔</span>
                </div>
                <span className="text-[10px] font-medium text-gray-400">
                    {hasRisk ? `${risk.groups.length} 类原因 · 点击展开标的` : '未触发拦截规则'}
                </span>
            </header>

            {hasRisk && (
                <ul className="bg-white/70">
                    {risk.groups.map((group) => {
                        const key = `${group.reason}|${group.action}`;
                        const open = expanded.has(key);
                        return (
                            <li key={key} className="border-t border-gray-50">
                                <button
                                    type="button"
                                    onClick={() => toggle(key)}
                                    aria-expanded={open}
                                    className="flex w-full items-center gap-3 px-3 py-2 text-left transition-colors hover:bg-gray-50/70"
                                >
                                    <span className={`shrink-0 rounded px-1.5 py-px text-[9px] font-black ${actionTone(group.action)}`}>
                                        {actionLabel(group.action)}
                                    </span>
                                    <span className="min-w-0 flex-1 truncate text-[11px] text-gray-700" title={group.reason}>
                                        {group.reason}
                                    </span>
                                    <span className="shrink-0 font-mono text-[11px] font-bold text-gray-900">{group.count}</span>
                                    <ChevronDown
                                        size={13}
                                        className={`shrink-0 text-gray-300 transition-transform ${open ? 'rotate-180' : ''}`}
                                    />
                                </button>
                                {open && (
                                    <div className="flex flex-wrap gap-1 px-3 pb-3">
                                        {group.symbols.slice(0, SYMBOLS_PREVIEW_LIMIT).map((symbol) => (
                                            <span
                                                key={symbol}
                                                className="rounded-md border border-gray-100 bg-gray-50 px-1.5 py-0.5 font-mono text-[10px] text-gray-500"
                                            >
                                                {normalizeStockCode(symbol)}
                                            </span>
                                        ))}
                                        {group.symbols.length > SYMBOLS_PREVIEW_LIMIT && (
                                            <span className="px-1.5 py-0.5 text-[10px] text-gray-400">
                                                +{group.symbols.length - SYMBOLS_PREVIEW_LIMIT}
                                            </span>
                                        )}
                                        {group.symbols.length === 0 && (
                                            <span className="px-1.5 py-0.5 text-[10px] text-gray-300">后端未返回标的明细</span>
                                        )}
                                    </div>
                                )}
                            </li>
                        );
                    })}
                </ul>
            )}
        </section>
    );
};
