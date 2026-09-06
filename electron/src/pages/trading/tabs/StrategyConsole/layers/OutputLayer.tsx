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
 * L3 交易记录：全宽单行列表（最近委托），行高放开、每条一行展示
 * 代码 / 方向 / 数量 / 价格 / 状态 / 时间，不再挤窄列换行。
 */
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
                    {onOpenManualTask && (
                        <button
                            type="button"
                            onClick={onOpenManualTask}
                            className="px-3 py-1.5 rounded-xl bg-blue-50 text-blue-700 text-xs font-bold hover:bg-blue-100 transition-all flex items-center gap-1.5 border border-blue-100"
                        >
                            <Activity size={13} /> 查看详情
                        </button>
                    )}
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
                <div className="overflow-y-auto custom-scrollbar divide-y divide-slate-100 max-h-96">
                    {recentOrders.map((order) => {
                        const isBuy = String(order.side || '').toLowerCase() === 'buy';
                        const price = order.average_price ?? order.price;
                        return (
                            <div
                                key={order.order_id || order.id}
                                className="grid grid-cols-[28px_minmax(0,1fr)_auto_auto_auto] items-center gap-3 py-2"
                            >
                                <span className={`w-7 h-7 rounded-lg text-xs font-bold flex items-center justify-center ${isBuy ? 'bg-red-50 text-red-600' : 'bg-emerald-50 text-emerald-600'}`}>
                                    {isBuy ? '买' : '卖'}
                                </span>
                                <div className="min-w-0 flex items-baseline gap-2 truncate">
                                    <span className="text-xs font-bold text-slate-800 truncate" title={`${order.symbol} ${order.symbol_name || ''}`}>
                                        {order.symbol_name || order.symbol}
                                    </span>
                                    <span className="font-mono text-xs text-slate-400 shrink-0">{order.symbol}</span>
                                </div>
                                <span className="text-xs text-slate-600 font-mono whitespace-nowrap">
                                    {order.filled_quantity ?? order.quantity} 股
                                    {typeof price === 'number' ? ` @ ${price.toFixed(2)}` : ''}
                                </span>
                                <span className="text-xs font-bold text-slate-500 whitespace-nowrap">{orderStatusLabel(order.status)}</span>
                                <span className="text-xs text-slate-400 font-mono whitespace-nowrap">{formatOrderTime(order.created_at)}</span>
                            </div>
                        );
                    })}
                </div>
            )}
        </section>
    );
};

export default OutputLayer;
