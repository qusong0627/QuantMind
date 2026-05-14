import React, { useCallback, useMemo, useRef, useState } from 'react';
import { FileText, Search, Download, TrendingUp, TrendingDown, Clock, CheckCircle, XCircle, ChevronLeft, ChevronRight, AlertTriangle, Activity, RotateCcw, ChevronDown } from 'lucide-react';
import { Dropdown, message } from 'antd';
import type { Order } from '../../../services/realTradingService';
import { marketDataService } from '../../../services/marketDataService';
import { csvExporter } from '../../../services/export';
import { exportTradeRecordsToExcel } from '../../../utils/excelExport';
import { formatBackendDateTime, formatBackendTime } from '../../../utils/format';
import type { TradeRecordExportRow } from '../../../utils/excelExport';

interface TradingHistoryProps {
    userId: string;
    isActive: boolean;
    tradingMode?: 'real' | 'simulation';
}

interface TradeRow {
    id: string;
    createdAt: string;
    time: string;
    direction: string;
    code: string;
    name: string;
    quantity: number;         // 委托数量
    filledQty: number;        // 成交数量
    price: number;            // 委托价
    avgPrice: number;         // 成交均价
    amount: number;           // 委托金额
    filledValue: number;      // 成交金额
    status: string;
    tradeAction: string;      // buy_to_open / sell_to_close 等
    exchangeOrderId: string;  // 交易所委托号
    commission: number;       // 手续费
}

type ExportFormat = 'csv' | 'xlsx';
type ExportScope = 'current' | 'all';

interface OrdersRange {
    startDate?: string;
    endDate?: string;
}

const PAGE_SIZE = 500;

const buildDateRange = (timeRange: 'today' | 'week' | 'month' | 'all'): OrdersRange => {
    if (timeRange === 'all') return {};

    const now = new Date();
    const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate());

    if (timeRange === 'today') {
        return {
            startDate: startOfToday.toISOString(),
        };
    }

    const start = new Date(startOfToday);
    if (timeRange === 'week') {
        start.setDate(start.getDate() - 7);
    } else {
        start.setDate(start.getDate() - 30);
    }

    return {
        startDate: start.toISOString(),
    };
};

const TradingHistory: React.FC<TradingHistoryProps> = ({ userId, isActive, tradingMode }) => {
    const [timeRange, setTimeRange] = useState<'today' | 'week' | 'month' | 'all'>('today');
    const [searchTerm, setSearchTerm] = useState('');
    const [statusFilter, setStatusFilter] = useState<string>('all');
    const [directionFilter, setDirectionFilter] = useState<string>('all');
    const [currentPage, setCurrentPage] = useState(1);
    const [trades, setTrades] = useState<TradeRow[]>([]);
    const [stockNames, setStockNames] = React.useState<Record<string, string>>({});
    const [exporting, setExporting] = useState(false);
    const stockNamesRef = useRef<Record<string, string>>({});
    const failedCodesRef = useRef<Set<string>>(new Set());
    const loadingRef = useRef(false);
    const itemsPerPage = 50;

    React.useEffect(() => {
        stockNamesRef.current = stockNames;
    }, [stockNames]);

    // Load data
    const loadOrders = useCallback(async () => {
        if (!userId || loadingRef.current) return;
        loadingRef.current = true;

        try {
            const { realTradingService } = await import('../../../services/realTradingService');
            const rangeQuery = buildDateRange(timeRange);
            const allOrders: Order[] = [];
            let offset = 0;

            while (true) {
                const batch = await realTradingService.getOrders(userId, undefined, tradingMode, {
                    ...rangeQuery,
                    limit: PAGE_SIZE,
                    offset,
                });
                allOrders.push(...batch);

                if (batch.length < PAGE_SIZE) {
                    break;
                }
                offset += PAGE_SIZE;
            }

            const uniqueCodes = Array.from(new Set(
                allOrders
                    .filter(o => !o.symbol_name)
                    .map(o => o.symbol)
            ));
            const codesToFetch = uniqueCodes.filter(code => !stockNamesRef.current[code] && !failedCodesRef.current.has(code));

            if (codesToFetch.length > 0) {
                try {
                    // 使用分批请求机制 (每次5个，延迟100ms)
                    const results = await marketDataService.getStockDetailsBatch(codesToFetch, 5, 100);
                    
                    const batchNames: Record<string, string> = {};
                    results.forEach(({ code, result }) => {
                        if (result.success && result.data) {
                            batchNames[code] = result.data.name;
                        } else {
                            // 标记为失败，避免后续重复尝试请求
                            failedCodesRef.current.add(code);
                        }
                    });
                    
                    if (Object.keys(batchNames).length > 0) {
                        const mergedNames = { ...stockNamesRef.current, ...batchNames };
                        stockNamesRef.current = mergedNames;
                        setStockNames(mergedNames);
                    }
                } catch (error) {
                    console.error('Failed to fetch stock names batch:', error);
                }
            }

            const mapped: TradeRow[] = allOrders.map(order => {
                const rawStatus = String(order.status || '').toLowerCase();
                const filledQty = order.filled_quantity ?? 0;
                const totalQty = order.quantity ?? 0;
                const avgPrice = order.average_price ?? order.price ?? 0;
                const filledValue = order.filled_value ?? (filledQty * avgPrice);

                const isFilled = ['filled', 'partially_filled', 'partial'].includes(rawStatus);
                
                // 部分成交判断：有成交数量但未全部成交
                const isPartial = filledQty > 0 && filledQty < totalQty;
                
                // 状态修正：部分成交时优先显示"部分成交"
                let displayStatus = rawStatus;
                if (isPartial && rawStatus !== 'filled') {
                    displayStatus = 'partial';
                }

                // trade_action 映射为中文
                const actionMap: Record<string, string> = {
                    buy_to_open: '买入开仓',
                    sell_to_close: '卖出平仓',
                    sell_to_open: '卖出开仓',
                    buy_to_close: '买入平仓',
                    open: '开仓',
                    close: '平仓',
                };
                const rawAction = String(order.trade_action || '').toLowerCase();

                return {
                    id: order.id.toString(),
                    createdAt: order.submitted_at || order.created_at,
                    time: formatBackendTime(order.submitted_at || order.created_at, { withSeconds: true }),
                    direction: String(order.side || '').toLowerCase(),
                    code: order.symbol,
                    name: order.symbol_name || stockNamesRef.current[order.symbol] || order.symbol,
                    quantity: order.quantity,
                    filledQty,
                    price: order.price || 0,
                    avgPrice: isFilled || isPartial ? avgPrice : (order.price || 0),
                    amount: order.order_value,
                    filledValue: isFilled || isPartial ? filledValue : 0,
                    status: displayStatus,
                    tradeAction: actionMap[rawAction] || rawAction,
                    exchangeOrderId: order.exchange_order_id || '',
                    commission: Number((order as any).commission ?? (order as any).fee ?? 0),
                };
            });
            setTrades(mapped);
        } catch (e) {
            console.error("Failed to load orders", e);
        } finally {
            loadingRef.current = false;
        }
    }, [timeRange, tradingMode, userId]);

    React.useEffect(() => {
        if (isActive && userId) {
            void loadOrders();
            const interval = setInterval(() => {
                void loadOrders();
            }, 5000);
            return () => clearInterval(interval);
        }
    }, [isActive, loadOrders, userId]);

    React.useEffect(() => {
        setCurrentPage(1);
    }, [searchTerm, timeRange, statusFilter, directionFilter]);

    const filteredTrades = useMemo(() => {
        const keyword = searchTerm.trim().toLowerCase();
        return trades.filter((trade) => {
            const matchesKeyword = !keyword
                || trade.code.toLowerCase().includes(keyword)
                || trade.name.toLowerCase().includes(keyword);
            
            const matchesStatus = statusFilter === 'all'
                || (statusFilter === 'filled' && trade.status === 'filled')
                || (statusFilter === 'pending' && ['pending', 'submitted', 'open', 'partial', 'partially_filled'].includes(trade.status))
                || (statusFilter === 'cancelled' && trade.status === 'cancelled')
                || (statusFilter === 'rejected' && ['rejected', 'expired'].includes(trade.status));
            
            const matchesDirection = directionFilter === 'all'
                || trade.direction === directionFilter;

            return matchesKeyword && matchesStatus && matchesDirection;
        });
    }, [searchTerm, trades, statusFilter, directionFilter]);

    const totalPages = Math.ceil(filteredTrades.length / itemsPerPage);
    const startIndex = (currentPage - 1) * itemsPerPage;
    const paginatedTrades = filteredTrades.slice(startIndex, startIndex + itemsPerPage);

    React.useEffect(() => {
        if (totalPages === 0 && currentPage !== 1) {
            setCurrentPage(1);
            return;
        }
        if (totalPages > 0 && currentPage > totalPages) {
            setCurrentPage(totalPages);
        }
    }, [currentPage, totalPages]);

    const stats = {
        total: filteredTrades.length,
        filled: filteredTrades.filter(t => t.status === 'filled').length,
        partial: filteredTrades.filter(t => ['partial', 'partially_filled'].includes(t.status)).length,
        pending: filteredTrades.filter(t => ['pending', 'submitted', 'open'].includes(t.status)).length,
        cancelled: filteredTrades.filter(t => t.status === 'cancelled').length,
        rejected: filteredTrades.filter(t => ['rejected', 'expired'].includes(t.status)).length,
        buyAmount: filteredTrades.filter(t => t.direction === 'buy' && t.status === 'filled').reduce((sum, t) => sum + t.filledValue, 0),
        sellAmount: filteredTrades.filter(t => t.direction === 'sell' && t.status === 'filled').reduce((sum, t) => sum + t.filledValue, 0),
        totalCommission: filteredTrades.reduce((sum, t) => sum + t.commission, 0),
        netBuy: 0
    };
    stats.netBuy = stats.buyAmount - stats.sellAmount;

    const getStatusIcon = (status: string) => {
        switch (status) {
            case 'filled':
                return <CheckCircle size={16} className="text-green-500" />;
            case 'pending':
            case 'open':
            case 'submitted':
                return <Clock size={16} className="text-yellow-500" />;
            case 'partial':
            case 'partially_filled':
                return <Activity size={16} className="text-blue-500" />;
            case 'cancelled':
                return <XCircle size={16} className="text-gray-400" />;
            case 'rejected':
                return <AlertTriangle size={16} className="text-red-500" />;
            case 'expired':
                return <RotateCcw size={16} className="text-gray-300" />;
            default:
                return null;
        }
    };

    const getStatusText = useCallback((status: string) => {
        switch (status) {
            case 'filled':
                return '已成交';
            case 'pending':
            case 'submitted':
                return '委托中';
            case 'open':
                return '待成交';
            case 'partial':
            case 'partially_filled':
                return '部分成交';
            case 'cancelled':
                return '已撤单';
            case 'rejected':
                return '已拒绝';
            case 'expired':
                return '已过期';
            default:
                return status;
        }
    }, []);

    const buildExportRows = useCallback((rows: TradeRow[]) => rows.map((trade) => ({
        时间: formatBackendDateTime(trade.createdAt),
        方向: trade.direction === 'buy' ? '买入' : '卖出',
        操作: trade.tradeAction,
        代码: trade.code,
        名称: trade.name,
        委托数量: trade.quantity,
        成交数量: trade.filledQty,
        委托价格: trade.price.toFixed(2),
        成交均价: trade.avgPrice.toFixed(2),
        委托金额: trade.amount.toFixed(2),
        成交金额: trade.filledValue.toFixed(2),
        状态: getStatusText(trade.status),
        交易所委托号: trade.exchangeOrderId,
    })), [getStatusText]);

    const handleExport = useCallback(async (format: ExportFormat, scope: ExportScope) => {
        const sourceTrades = scope === 'current' ? paginatedTrades : filteredTrades;
        if (sourceTrades.length === 0 || exporting) {
            message.warning('当前没有可导出的交易记录');
            return;
        }

        setExporting(true);
        try {
            const rangeLabel = timeRange === 'today' ? 'today' : timeRange === 'week' ? 'week' : timeRange === 'month' ? 'month' : 'all';
            const scopeLabel = scope === 'current' ? 'current-page' : 'all-filtered';
            const localDate = new Date();
            const dateStamp = `${localDate.getFullYear()}-${String(localDate.getMonth() + 1).padStart(2, '0')}-${String(localDate.getDate()).padStart(2, '0')}`;
            const rows = buildExportRows(sourceTrades);
            const filenameBase = `real_trading_orders_${rangeLabel}_${scopeLabel}_${dateStamp}`;

            if (format === 'csv') {
                csvExporter.export(rows, {
                    filename: `${filenameBase}.csv`,
                });
            } else {
                const excelRows: TradeRecordExportRow[] = rows.map((row) => ({
                    时间: row.时间,
                    方向: row.方向,
                    代码: row.代码,
                    名称: row.名称,
                    数量: row.成交数量 || row.委托数量,
                    价格: row.成交均价 !== '0.00' ? row.成交均价 : row.委托价格,
                    金额: Number(row.成交金额) || Number(row.委托金额) || 0,
                    状态: row.状态,
                }));
                await exportTradeRecordsToExcel(excelRows, `${filenameBase}.xlsx`);
            }

            message.success(`已导出 ${scope === 'current' ? '当前页' : '筛选结果'} ${format.toUpperCase()} 文件`);
        } catch (error) {
            console.error('导出交易记录失败', error);
            message.error('交易记录导出失败，请稍后重试');
        } finally {
            setExporting(false);
        }
    }, [buildExportRows, exporting, filteredTrades, getStatusText, paginatedTrades, timeRange]);

    const exportMenuItems = useMemo(() => ([
        { key: 'csv-current', label: '导出当前页 CSV' },
        { key: 'csv-all', label: '导出全部筛选 CSV' },
        { type: 'divider' as const },
        { key: 'xlsx-current', label: '导出当前页 Excel' },
        { key: 'xlsx-all', label: '导出全部筛选 Excel' },
    ]), []);

    const handleExportMenuClick = useCallback(({ key }: { key: string }) => {
        const [format, scope] = key.split('-') as [ExportFormat, ExportScope];
        void handleExport(format, scope);
    }, [handleExport]);

    if (!isActive) return null;

    return (
        <div className="h-full flex flex-col p-6 pb-[65px]">
            {/* Header with Filters */}
            <div className="flex flex-wrap items-center gap-4 mb-4">
                <h3 className="text-base font-bold text-gray-800 flex items-center whitespace-nowrap">
                    <FileText className="mr-2 text-blue-600" size={18} />
                    交易记录
                </h3>
                
                <div className="flex items-center bg-gray-100 p-1 rounded-lg gap-1 border border-gray-200">
                    {(['today', 'week', 'month', 'all'] as const).map((range) => (
                        <button
                            key={range}
                            onClick={() => setTimeRange(range)}
                            className={`px-4 py-1.5 text-xs font-medium rounded-md transition-all ${timeRange === range
                                ? 'bg-white text-blue-600 shadow-sm'
                                : 'text-gray-500 hover:text-gray-700'
                                }`}
                        >
                            {range === 'today' ? '今日' : range === 'week' ? '本周' : range === 'month' ? '本月' : '全部'}
                        </button>
                    ))}
                </div>

                <div className="flex items-center bg-gray-100 p-1 rounded-lg gap-1 border border-gray-200">
                    <button
                        onClick={() => setDirectionFilter('all')}
                        className={`px-3 py-1.5 text-xs font-medium rounded-md transition-all ${directionFilter === 'all' ? 'bg-white text-blue-600 shadow-sm' : 'text-gray-500 hover:text-gray-700'}`}
                    >
                        全部方向
                    </button>
                    <button
                        onClick={() => setDirectionFilter('buy')}
                        className={`px-3 py-1.5 text-xs font-medium rounded-md transition-all ${directionFilter === 'buy' ? 'bg-white text-red-600 shadow-sm' : 'text-gray-500 hover:text-gray-700'}`}
                    >
                        买入
                    </button>
                    <button
                        onClick={() => setDirectionFilter('sell')}
                        className={`px-3 py-1.5 text-xs font-medium rounded-md transition-all ${directionFilter === 'sell' ? 'bg-white text-green-600 shadow-sm' : 'text-gray-500 hover:text-gray-700'}`}
                    >
                        卖出
                    </button>
                </div>

                <div className="flex items-center bg-gray-100 p-1 rounded-lg gap-1 border border-gray-200">
                    <button
                        onClick={() => setStatusFilter('all')}
                        className={`px-3 py-1.5 text-xs font-medium rounded-md transition-all ${statusFilter === 'all' ? 'bg-white text-blue-600 shadow-sm' : 'text-gray-500 hover:text-gray-700'}`}
                    >
                        全部状态
                    </button>
                    <button
                        onClick={() => setStatusFilter('filled')}
                        className={`px-3 py-1.5 text-xs font-medium rounded-md transition-all ${statusFilter === 'filled' ? 'bg-white text-green-600 shadow-sm' : 'text-gray-500 hover:text-gray-700'}`}
                    >
                        已成交
                    </button>
                    <button
                        onClick={() => setStatusFilter('pending')}
                        className={`px-3 py-1.5 text-xs font-medium rounded-md transition-all ${statusFilter === 'pending' ? 'bg-white text-orange-600 shadow-sm' : 'text-gray-500 hover:text-gray-700'}`}
                    >
                        委托中
                    </button>
                    <button
                        onClick={() => setStatusFilter('cancelled')}
                        className={`px-3 py-1.5 text-xs font-medium rounded-md transition-all ${statusFilter === 'cancelled' ? 'bg-white text-gray-600 shadow-sm' : 'text-gray-500 hover:text-gray-700'}`}
                    >
                        已撤单
                    </button>
                </div>

                
                <Dropdown
                    menu={{
                        items: exportMenuItems,
                        onClick: handleExportMenuClick,
                    }}
                    trigger={['click']}
                    placement="bottomRight"
                >
                    <button
                        type="button"
                        disabled={exporting || filteredTrades.length === 0}
                        className="flex items-center gap-2 px-4 py-[7px] bg-gray-100 hover:bg-gray-200 text-gray-700 rounded-lg font-medium transition-all text-xs disabled:opacity-50 disabled:cursor-not-allowed border border-gray-200 shadow-sm hover:shadow-md active:scale-95 ml-auto"
                    >
                        <Download size={14} />
                        {exporting ? '导出中...' : '导出'}
                        <ChevronDown size={12} className="text-gray-500" />
                    </button>
                </Dropdown>
            </div>

            {/* Trades Table */}
            <div className="flex-1 bg-white rounded-xl border border-gray-200 overflow-hidden flex flex-col">
                <div className="overflow-auto flex-1">
                    <table className="w-full text-xs table-fixed">
                        <thead className="bg-gray-50 border-b border-gray-200 sticky top-0">
                            <tr>
                                <th className="px-3 py-2 text-center font-semibold text-gray-600 w-[10%] whitespace-nowrap">时间</th>
                                <th className="px-3 py-2 text-center font-semibold text-gray-600 w-[8%]">方向</th>
                                <th className="px-3 py-2 text-center font-semibold text-gray-600 w-[8%]">操作</th>
                                <th className="px-3 py-2 text-center font-semibold text-gray-600 w-[11%]">代码</th>
                                <th className="px-3 py-2 text-center font-semibold text-gray-600 w-[11%]">名称</th>
                                <th className="px-3 py-2 text-center font-semibold text-gray-600 w-[12%]">成交量/委托量</th>
                                <th className="px-3 py-2 text-center font-semibold text-gray-600 w-[11%]">成交均价</th>
                                <th className="px-3 py-2 text-center font-semibold text-gray-600 w-[11%]">成交金额</th>
                                <th className="px-3 py-2 text-center font-semibold text-gray-600 w-[9%]">手续费</th>
                                <th className="px-3 py-2 text-center font-semibold text-gray-600 w-[9%]">状态</th>
                            </tr>
                        </thead>
                        <tbody className="divide-y divide-gray-100">
                            {paginatedTrades.length === 0 ? (
                                <tr>
                                    <td colSpan={9} className="px-3 py-12 text-center text-gray-400">
                                        <div className="flex flex-col items-center gap-2">
                                            <FileText size={32} className="text-gray-200" />
                                            <span className="text-sm">暂无交易记录</span>
                                            <span className="text-xs text-gray-300">
                                                {timeRange === 'today' ? '今日' : timeRange === 'week' ? '本周' : timeRange === 'month' ? '本月' : ''}内无委托记录
                                            </span>
                                        </div>
                                    </td>
                                </tr>
                            ) : paginatedTrades.map((trade) => {
                                const isFilled = trade.status === 'filled';
                                const isPartial = ['partial', 'partially_filled'].includes(trade.status);
                                const isPending = ['pending', 'submitted', 'open'].includes(trade.status);
                                return (
                                    <tr key={trade.id} className="hover:bg-gray-50 transition-colors">
                                        <td className="px-3 py-2 text-gray-600 text-center whitespace-nowrap font-mono">{trade.time}</td>
                                        <td className="px-3 py-2 text-center">
                                            <div className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full font-semibold ${trade.direction === 'buy'
                                                ? 'bg-red-50 text-red-600'
                                                : 'bg-green-50 text-green-600'
                                                }`}>
                                                {trade.direction === 'buy' ? (
                                                    <><TrendingUp size={11} /> 买入</>
                                                ) : (
                                                    <><TrendingDown size={11} /> 卖出</>
                                                )}
                                            </div>
                                        </td>
                                        <td className="px-3 py-2 text-center text-gray-500">
                                            {trade.tradeAction ? (
                                                <span className="px-1.5 py-0.5 bg-gray-100 rounded text-xs text-gray-600">{trade.tradeAction}</span>
                                            ) : '--'}
                                        </td>
                                        <td className="px-3 py-2 text-center font-mono text-gray-900 overflow-hidden text-ellipsis whitespace-nowrap">{trade.code}</td>
                                        <td className="px-3 py-2 text-center font-medium text-gray-900 overflow-hidden text-ellipsis whitespace-nowrap">{trade.name}</td>
                                        <td className="px-3 py-2 text-center">
                                            {isFilled ? (
                                                <span className="font-semibold text-gray-900">{trade.filledQty.toLocaleString()}</span>
                                            ) : isPartial ? (
                                                <span className="text-blue-600 font-medium">{trade.filledQty.toLocaleString()}/{trade.quantity.toLocaleString()}</span>
                                            ) : isPending ? (
                                                <span className="text-yellow-600 font-medium">0/{trade.quantity.toLocaleString()}</span>
                                            ) : (
                                                <span className="text-gray-400">{trade.filledQty > 0 ? `${trade.filledQty.toLocaleString()}/` : ''}{trade.quantity.toLocaleString()}</span>
                                            )}
                                        </td>
                                        <td className="px-3 py-2 text-center font-semibold text-gray-900">
                                            {(isFilled || isPartial) && trade.avgPrice > 0 ? `¥${trade.avgPrice.toFixed(2)}` : (
                                                <span className="text-gray-400">¥{trade.price.toFixed(2)}</span>
                                            )}
                                        </td>
                                        <td className="px-3 py-2 text-center font-bold text-gray-900">
                                            {(isFilled || isPartial) && trade.filledValue > 0 ? (
                                                trade.filledValue >= 10000
                                                    ? `¥${(trade.filledValue / 10000).toFixed(2)}万`
                                                    : `¥${trade.filledValue.toFixed(2)}`
                                            ) : (
                                                <span className="text-gray-300">--</span>
                                            )}
                                        </td>
                                        <td className="px-3 py-2 text-center font-medium text-amber-600">
                                            {trade.commission > 0 ? `¥${trade.commission.toFixed(2)}` : '--'}
                                        </td>
                                        <td className="px-3 py-2">
                                            <div className="flex items-center justify-center gap-1">
                                                {getStatusIcon(trade.status)}
                                                <span className={`font-medium ${
                                                    trade.status === 'filled' ? 'text-green-600' :
                                                    ['pending', 'open', 'submitted'].includes(trade.status) ? 'text-yellow-600' :
                                                    ['partial', 'partially_filled'].includes(trade.status) ? 'text-blue-600' :
                                                    trade.status === 'rejected' ? 'text-red-600' :
                                                    'text-gray-400'
                                                }`}>
                                                    {getStatusText(trade.status)}
                                                </span>
                                            </div>
                                        </td>
                                    </tr>
                                );
                            })}
                        </tbody>
                    </table>
                </div>

                {/* Statistics Footer */}
                <div className="border-t border-gray-200 bg-white px-6 py-4">
                    <div className="flex items-center justify-between">
                        <div className="flex items-center gap-2 pr-6 border-r border-gray-100">
                            <Activity size={18} className="text-blue-600 animate-pulse" />
                            <div className="flex flex-col">
                                <span className="text-[10px] font-bold text-gray-400 uppercase tracking-wider">今日</span>
                                <span className="text-xs font-black text-gray-800">概览</span>
                            </div>
                        </div>

                        <div className="flex-1 flex items-center justify-center gap-10">
                            <div 
                                className={`flex flex-col items-center cursor-pointer p-2 rounded-lg transition-all hover:bg-gray-50 ${statusFilter === 'all' ? 'bg-blue-50 ring-1 ring-inset ring-blue-100' : ''}`}
                                onClick={() => setStatusFilter('all')}
                            >
                                <span className={`text-[10px] mb-1 font-bold ${statusFilter === 'all' ? 'text-blue-600' : 'text-gray-500'}`}>总委托</span>
                                <span className={`font-bold text-sm ${statusFilter === 'all' ? 'text-blue-700' : 'text-gray-900'}`}>
                                    {stats.total} <span className="text-[10px] font-medium text-gray-400">笔</span>
                                </span>
                            </div>

                            <div 
                                className={`flex flex-col items-center cursor-pointer p-2 rounded-lg transition-all hover:bg-gray-50 ${statusFilter === 'filled' ? 'bg-green-50 ring-1 ring-inset ring-green-100' : ''}`}
                                onClick={() => setStatusFilter('filled')}
                            >
                                <span className={`text-[10px] mb-1 font-bold ${statusFilter === 'filled' ? 'text-green-600' : 'text-gray-500'}`}>已成交</span>
                                <span className={`font-bold text-sm ${statusFilter === 'filled' ? 'text-green-700' : 'text-green-600'}`}>
                                    {stats.filled + stats.partial} <span className="text-[10px] font-medium text-green-400">笔</span>
                                </span>
                            </div>

                            <div 
                                className={`flex flex-col items-center cursor-pointer p-2 rounded-lg transition-all hover:bg-gray-50 ${statusFilter === 'pending' ? 'bg-yellow-50 ring-1 ring-inset ring-yellow-100' : ''}`}
                                onClick={() => setStatusFilter('pending')}
                            >
                                <span className={`text-[10px] mb-1 font-bold ${statusFilter === 'pending' ? 'text-yellow-600' : 'text-gray-500'}`}>委托中</span>
                                <span className={`font-bold text-sm ${statusFilter === 'pending' ? 'text-yellow-700' : 'text-yellow-600'}`}>
                                    {stats.pending} <span className="text-[10px] font-medium text-yellow-400">笔</span>
                                </span>
                            </div>

                            <div 
                                className={`flex flex-col items-center cursor-pointer p-2 rounded-lg transition-all hover:bg-gray-50 ${statusFilter === 'cancelled' ? 'bg-gray-50 ring-1 ring-inset ring-gray-200' : ''}`}
                                onClick={() => setStatusFilter('cancelled')}
                            >
                                <span className={`text-[10px] mb-1 font-bold ${statusFilter === 'cancelled' ? 'text-gray-600' : 'text-gray-500'}`}>撤单</span>
                                <span className={`font-bold text-sm ${statusFilter === 'cancelled' ? 'text-gray-700' : 'text-gray-400'}`}>
                                    {stats.cancelled} <span className="text-[10px] font-medium text-gray-300">笔</span>
                                </span>
                            </div>

                            {stats.rejected > 0 && (
                                <div 
                                    className={`flex flex-col items-center cursor-pointer p-2 rounded-lg transition-all hover:bg-gray-50 ${statusFilter === 'rejected' ? 'bg-red-50 ring-1 ring-inset ring-red-100' : ''}`}
                                    onClick={() => setStatusFilter('rejected')}
                                >
                                    <span className={`text-[10px] mb-1 font-bold ${statusFilter === 'rejected' ? 'text-red-600' : 'text-gray-500'}`}>拒绝/过期</span>
                                    <span className={`font-bold text-sm ${statusFilter === 'rejected' ? 'text-red-700' : 'text-red-500'}`}>
                                        {stats.rejected} <span className="text-[10px] font-medium text-red-400">笔</span>
                                    </span>
                                </div>
                            )}

                            <div className="h-8 w-px bg-gray-100"></div>

                            <div className="flex flex-col items-center">
                                <span className="text-gray-500 text-[10px] mb-1 font-bold">买入金额</span>
                                <span className="font-bold text-red-600 text-sm">
                                    {stats.buyAmount >= 10000 ? `¥${(stats.buyAmount / 10000).toFixed(2)}万` : `¥${stats.buyAmount.toFixed(2)}`}
                                </span>
                            </div>

                            <div className="flex flex-col items-center">
                                <span className="text-gray-500 text-[10px] mb-1 font-bold">卖出金额</span>
                                <span className="font-bold text-green-600 text-sm">
                                    {stats.sellAmount >= 10000 ? `¥${(stats.sellAmount / 10000).toFixed(2)}万` : `¥${stats.sellAmount.toFixed(2)}`}
                                </span>
                            </div>

                            <div className="flex flex-col items-center">
                                <span className="text-gray-500 text-[10px] mb-1 font-bold">净买入</span>
                                <span className={`font-bold text-sm ${stats.netBuy >= 0 ? 'text-red-600' : 'text-green-600'}`}>
                                    {Math.abs(stats.netBuy) >= 10000 ? `¥${(Math.abs(stats.netBuy) / 10000).toFixed(2)}万` : `¥${Math.abs(stats.netBuy).toFixed(2)}`}
                                </span>
                            </div>

                            <div className="h-8 w-px bg-gray-100"></div>

                            <div className="flex flex-col items-center">
                                <span className="text-gray-500 text-[10px] mb-1 font-bold">累计费用</span>
                                <span className="font-bold text-amber-600 text-sm">
                                    ¥{stats.totalCommission.toFixed(2)}
                                </span>
                            </div>
                        </div>
                    </div>

                    {/* Pagination */}
                    <div className="flex items-center justify-between pt-2 border-t border-gray-200">
                        <div className="text-xs text-gray-600">
                            共 {filteredTrades.length} 条记录，当前第 {currentPage}/{totalPages} 页
                        </div>
                        <div className="flex items-center gap-2">
                            <button
                                onClick={() => setCurrentPage(Math.max(1, currentPage - 1))}
                                disabled={currentPage === 1}
                                className={`inline-flex items-center justify-center w-8 h-8 rounded-lg transition-colors ${currentPage === 1
                                        ? 'bg-gray-100 text-gray-400 cursor-not-allowed'
                                        : 'bg-white border border-gray-300 text-gray-700 hover:bg-gray-50'
                                    }`}
                            >
                                <ChevronLeft size={16} />
                            </button>
                            <span className="text-xs text-gray-700 min-w-[60px] text-center">
                                {currentPage} / {totalPages}
                            </span>
                            <button
                                onClick={() => setCurrentPage(Math.min(totalPages, currentPage + 1))}
                                disabled={currentPage === totalPages}
                                className={`inline-flex items-center justify-center w-8 h-8 rounded-lg transition-colors ${currentPage === totalPages
                                        ? 'bg-gray-100 text-gray-400 cursor-not-allowed'
                                        : 'bg-white border border-gray-300 text-gray-700 hover:bg-gray-50'
                                    }`}
                            >
                                <ChevronRight size={16} />
                            </button>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    );
};

export default TradingHistory;
