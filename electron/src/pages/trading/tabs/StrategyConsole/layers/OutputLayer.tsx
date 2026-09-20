import React from 'react';
import { Skeleton } from 'antd';
import { TerminalSquare } from 'lucide-react';
import type { Order } from '../../../../../services/realTradingService';
import { orderRowView, type OrderStatusTone } from './orderRowModel';

interface OutputLayerProps {
    recentOrders: Order[];
    ordersLoading: boolean;
    onOpenHistory?: () => void;
    onOpenManualTask?: () => void;
    logsOpen: boolean;
    onToggleLogs: () => void;
}

/** 状态色：已成=绿、部成=琥珀、拒=红、待成交=蓝、撤=灰（灰=终态但无损失） */
const STATUS_CLS: Record<OrderStatusTone, string> = {
    ok: 'bg-emerald-50 text-emerald-700 border-emerald-200',
    warn: 'bg-amber-50 text-amber-700 border-amber-200',
    bad: 'bg-rose-50 text-rose-700 border-rose-200',
    pending: 'bg-blue-50 text-blue-700 border-blue-200',
    muted: 'bg-slate-100 text-slate-500 border-slate-200',
};

/**
 * L3 交易记录：券商式流水的**两行一行单**。
 *
 * 此前是六列等宽表格（方向/股票/数量/价格/状态/时间），在半栏宽里塞不下
 * 成交额与手续费，用户看到的也就只有"很简单"的六格。改成两行：
 * 第一行是身份与结论（方向 · 名称(代码) · 状态 · 时间），第二行是钱与量
 * （成交/委托数量 · 委托价→成交均价 · 成交额 · 手续费），备注（含撤单原因）
 * 另起一行。名称由后端 `/orders` 补齐（桥接写入不写 symbol_name），
 * 查不到时回落显示代码——不编造、不留空。
 */
const OutputLayer: React.FC<OutputLayerProps> = ({
    recentOrders,
    ordersLoading,
    onOpenHistory,
    onOpenManualTask,
    logsOpen,
    onToggleLogs,
}) => {
    const rows = recentOrders.map((order) => ({ order, view: orderRowView(order) }));
    // 合计只统计真有成交的行（未成交/被拒的单没有成交额，混进来会虚增）
    const totals = rows.reduce(
        (acc, { order }) => {
            const qty = Number(order.filled_quantity) || 0;
            if (qty <= 0) return acc;
            acc.amount += Number(order.filled_value) || 0;
            acc.fee += Number(order.commission) || 0;
            acc.count += 1;
            return acc;
        },
        { amount: 0, fee: 0, count: 0 },
    );

    return (
        <section className="bg-white rounded-2xl border border-slate-200/80 shadow-xs p-4">
            <div className="flex items-center gap-2 mb-3">
                <span className="text-xs font-bold px-1.5 py-0.5 rounded bg-indigo-50 text-indigo-500">RECORDS</span>
                <h3 className="font-bold text-slate-800 text-sm">交易记录</h3>
                {!ordersLoading && recentOrders.length > 0 && (
                    <span className="text-xs text-slate-400">最近 {recentOrders.length} 条</span>
                )}
                <div className="ml-auto flex items-center gap-2">
                    <button
                        type="button"
                        onClick={onToggleLogs}
                        className="px-3 py-1.5 rounded-xl bg-slate-50 text-slate-700 text-xs font-bold hover:bg-slate-100 transition-all flex items-center gap-1.5 border border-slate-200"
                    >
                        <TerminalSquare size={13} />
                        {logsOpen ? '收起任务日志' : '查看任务日志'}
                    </button>
                    {onOpenHistory && (
                        <button
                            type="button"
                            onClick={onOpenHistory}
                            className="text-xs font-bold text-blue-600 hover:text-blue-700"
                        >
                            查看全部 →
                        </button>
                    )}
                </div>
            </div>
            {ordersLoading ? (
                <Skeleton active paragraph={{ rows: 4 }} />
            ) : recentOrders.length === 0 ? (
                <div className="flex items-center justify-center border border-dashed border-slate-100 rounded-xl text-xs text-slate-400 py-10">
                    暂无委托记录
                </div>
            ) : (
                <>
                    {/* 近 N 条合计：不是"今日"——这里是最近 10 条的口径，如实标注 */}
                    {totals.count > 0 && (
                        <div className="mb-2 flex flex-wrap items-center gap-x-3 gap-y-1 rounded-lg bg-slate-50/80 border border-slate-100 px-2 py-1 text-[11px] text-slate-500">
                            <span>
                                近 {recentOrders.length} 条中已成交 <span className="font-bold text-slate-700">{totals.count}</span> 笔
                            </span>
                            <span>
                                成交额 <span className="font-mono font-bold text-slate-700">¥{totals.amount.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</span>
                            </span>
                            <span>
                                手续费 <span className="font-mono text-slate-600">¥{totals.fee.toFixed(2)}</span>
                            </span>
                            <span className="text-slate-400">（手续费直接从策略净收益里扣）</span>
                        </div>
                    )}
                    <div className="overflow-y-auto custom-scrollbar rounded-xl border border-slate-100 divide-y divide-slate-100 max-h-96">
                        {rows.map(({ order, view }) => (
                            <div
                                key={order.order_id || order.id}
                                className="px-3 py-2 hover:bg-slate-50/60 transition-colors"
                            >
                                {/* 第一行：身份与结论 */}
                                <div className="flex items-center gap-2 min-w-0">
                                    <span
                                        className={`w-6 h-6 shrink-0 rounded-md text-[11px] font-bold flex items-center justify-center ${
                                            view.isBuy ? 'bg-red-50 text-red-600' : 'bg-emerald-50 text-emerald-600'
                                        }`}
                                    >
                                        {view.isBuy ? '买' : '卖'}
                                    </span>
                                    <span className="min-w-0 flex items-baseline gap-1.5">
                                        <span className="font-bold text-slate-800 text-xs truncate">
                                            {view.name || view.code}
                                        </span>
                                        {view.name && (
                                            <span className="font-mono text-[10px] text-slate-400">{view.code}</span>
                                        )}
                                    </span>
                                    <span
                                        className={`shrink-0 rounded border px-1.5 py-px text-[10px] font-bold ${STATUS_CLS[view.statusTone]}`}
                                    >
                                        {view.statusLabel}
                                    </span>
                                    <span className="ml-auto shrink-0 font-mono text-[10px] text-slate-400">
                                        <span className="mr-1 text-slate-300">{view.timeLabel}</span>
                                        {view.timeText}
                                    </span>
                                </div>
                                {/* 第二行：量与钱 */}
                                <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-0.5 pl-8 text-[11px] text-slate-500">
                                    <span className="font-mono">
                                        <span className={view.partial ? 'text-amber-600 font-bold' : 'text-slate-700'}>
                                            {view.qtyText}
                                        </span>
                                        {view.partial && <span className="ml-1 text-[10px] text-amber-600">未全部成交</span>}
                                    </span>
                                    {view.priceText && (
                                        <span className="font-mono">
                                            <span className="mr-1 text-slate-300">价</span>
                                            {view.priceText}
                                        </span>
                                    )}
                                    {view.amountText && (
                                        <span className="font-mono">
                                            <span className="mr-1 text-slate-300">{view.amountLabel}</span>
                                            <span className="text-slate-700">{view.amountText}</span>
                                        </span>
                                    )}
                                    {view.feeText && (
                                        <span className="font-mono">
                                            <span className="mr-1 text-slate-300">费</span>
                                            {view.feeText}
                                        </span>
                                    )}
                                    {order.order_type && (
                                        <span className="text-[10px] text-slate-400">
                                            {String(order.order_type).toLowerCase() === 'market' ? '市价' : '限价'}
                                        </span>
                                    )}
                                </div>
                                {/* 第三行：备注/撤单原因（后端把原因追加在 remarks 里） */}
                                {view.note && (
                                    <div
                                        className={`mt-0.5 pl-8 truncate text-[10px] ${
                                            view.statusTone === 'bad' || view.statusTone === 'muted' ? 'text-amber-600' : 'text-slate-400'
                                        }`}
                                        title={view.note}
                                    >
                                        {view.note}
                                    </div>
                                )}
                            </div>
                        ))}
                    </div>
                </>
            )}
        </section>
    );
};

export default OutputLayer;
