import React from 'react';
import { Skeleton } from 'antd';
import { Activity, TerminalSquare } from 'lucide-react';
import type { Order } from '../../../../../services/realTradingService';

interface OutputLayerProps {
    recentOrders: Order[];
    ordersLoading: boolean;
    onOpenHistory?: () => void;
    onOpenManualTask?: () => void;
    logsOpen: boolean;
    onToggleLogs: () => void;
}

const orderStatusLabel = (value?: string | null): string => {
    const s = String(value || '').toLowerCase();
    if (s === 'filled') return '已成';
    if (s === 'partial_filled' || s === 'partially_filled') return '部成';
    if (s === 'cancelled' || s === 'canceled') return '已撤';
    if (s === 'rejected') return '已拒绝';
    if (s === 'submitted' || s === 'pending' || s === 'new') return '待成交';
    return value || '-';
};

const formatOrderTime = (value?: string | null): string => {
    if (!value) return '-';
    const d = new Date(value);
    if (Number.isNaN(d.getTime())) return String(value).slice(11, 16) || '-';
    return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
};

/**
 * L3 交易记录：等宽居中表格（方向 / 股票 / 数量 / 价格 / 状态 / 时间）
 * 股票列固定 180px，不再 1fr 抢占；右侧数量/价格/状态/时间均分 1fr，间距松开，全部居中。
 */
const GRID_COLS = 'grid-cols-[56px_180px_1fr_1fr_1fr_1fr]';

const OutputLayer: React.FC<OutputLayerProps> = ({
    recentOrders,
    ordersLoading,
    onOpenHistory,
    onOpenManualTask,
    logsOpen,
    onToggleLogs,
}) => {
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
                <div className="overflow-hidden rounded-xl border border-slate-100">
                    <div className={`grid ${GRID_COLS} items-center gap-2 bg-slate-50/80 px-3 py-2 text-xs font-bold text-slate-500`}>
                        <span className="text-center">方向</span>
                        <span className="text-center">股票</span>
                        <span className="text-center">数量</span>
                        <span className="text-center">价格</span>
                        <span className="text-center">状态</span>
                        <span className="text-center">时间</span>
                    </div>
                    <div className="overflow-y-auto custom-scrollbar divide-y divide-slate-100 max-h-96">
                        {recentOrders.map((order) => {
                            const isBuy = String(order.side || '').toLowerCase() === 'buy';
                            const price = order.average_price ?? order.price;
                            const qty = order.filled_quantity ?? order.quantity;
                            // 已成交/部分成交显示成交时间，其余显示委托时间
                            const st = String(order.status || '').toLowerCase();
                            const showFilledTime = st === 'filled' || st.includes('partial');
                            const timeValue = (showFilledTime ? (order as any).filled_at : undefined)
                                ?? order.created_at;
                            return (
                                <div
                                    key={order.order_id || order.id}
                                    className={`grid ${GRID_COLS} items-center gap-2 px-3 py-2.5 text-xs`}
                                >
                                    <span className="flex justify-center">
                                        <span className={`w-7 h-7 rounded-lg text-xs font-bold flex items-center justify-center ${isBuy ? 'bg-red-50 text-red-600' : 'bg-emerald-50 text-emerald-600'}`}>
                                            {isBuy ? '买' : '卖'}
                                        </span>
                                    </span>
                                    <span className="flex flex-col items-center justify-center min-w-0 text-center" title={`${order.symbol} ${order.symbol_name || ''}`}>
                                        {(() => {
                                            const hasName = !!(order.symbol_name && order.symbol_name !== order.symbol && order.symbol_name !== '—');
                                            return (
                                                <>
                                                    <span className="font-bold text-slate-800 truncate max-w-full">
                                                        {hasName ? order.symbol_name : order.symbol}
                                                    </span>
                                                    {hasName && <span className="font-mono text-[11px] text-slate-400">{order.symbol}</span>}
                                                </>
                                            );
                                        })()}
                                    </span>
                                    <span className="text-center font-mono text-slate-700 whitespace-nowrap">{qty != null ? `${qty} 股` : '—'}</span>
                                    <span className="text-center font-mono text-slate-700 whitespace-nowrap">{typeof price === 'number' ? price.toFixed(2) : '—'}</span>
                                    <span className="text-center font-bold text-slate-600 whitespace-nowrap">{orderStatusLabel(order.status)}</span>
                                    <span className="text-center font-mono text-slate-400 whitespace-nowrap">{formatOrderTime(timeValue)}</span>
                                </div>
                            );
                        })}
                    </div>
                </div>
            )}
        </section>
    );
};

export default OutputLayer;
