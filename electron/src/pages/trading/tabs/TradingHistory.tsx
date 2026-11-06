import { useAppDispatch, useAppSelector } from '../../../store';
import { selectCurrentMarket, setMarket, type AppMarket } from '../../../store/slices/uiSlice';
import React, { useCallback, useMemo, useRef, useState } from 'react';
import { FileText, Search, Download, TrendingUp, TrendingDown, Clock, CheckCircle, XCircle, ChevronLeft, ChevronRight, AlertTriangle, Activity, RotateCcw, ChevronDown } from 'lucide-react';
import { DatePicker, Dropdown, Modal, message } from 'antd';
import dayjs, { type Dayjs } from 'dayjs';
import type { Order } from '../../../services/realTradingService';
import { marketDataService } from '../../../services/marketDataService';
import { csvExporter } from '../../../services/export';
import { exportTradeRecordsToExcel } from '../../../utils/excelExport';
import { formatBackendDate, formatBackendDateTime, formatBackendTime } from '../../../utils/format';
import type { TradeRecordExportRow } from '../../../utils/excelExport';
// 市场推断统一走 utils/marketInfer（与后端 market_rules.infer_market 同口径）
import { inferMarketOfSymbol } from '../../../utils/marketInfer';
import {
    applyColumnFilters,
    collectUniqueValues,
    type ColumnAccessors,
    type ColumnFilterState,
} from './tradingHistory/columnFilters';
import ExcelFilterDropdown from './tradingHistory/ExcelFilterDropdown';
import { displayReason, displayReasonSummary } from './tradingHistory/orderReason';

interface TradingHistoryProps {
    userId: string;
    isActive: boolean;
    tradingMode?: 'real' | 'simulation';
}

interface TradeRow {
    id: string;
    createdAt: string;
    date: string;             // 上海时区日历日（表格「日期」列/该列的 Excel 筛选）
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
    /** 拒单/撤单原因（后端 `remarks`；v1 台账由 mark_rejected 写入） */
    reason: string;
    /** 状态列短原因（拒因冒号后的尾巴）；全文进 title 与导出 */
    reasonSummary: string;
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

/** 状态码 → 界面文案（表格、筛选面板、导出共用同一份，避免三处各写一套） */
const STATUS_TEXT: Record<string, string> = {
    filled: '已成交',
    pending: '委托中',
    submitted: '委托中',
    open: '待成交',
    partial: '部分成交',
    partially_filled: '部分成交',
    cancelled: '已撤单',
    rejected: '已拒绝',
    expired: '已过期',
};

const statusDisplayText = (status: string): string => STATUS_TEXT[status] ?? status;

/** 拒单/撤单原因人话化（实现见 `tradingHistory/orderReason.ts`，已单测） */

/**
 * Excel 表头筛选按**显示值**取值（用户筛的是看到的那一列文本，不是内部状态码）。
 * 取值口径与单元格渲染保持一致：这里怎么写，单元格就怎么显示。
 */
const COLUMN_ACCESSORS: ColumnAccessors<TradeRow> = {
    date: (trade) => trade.date,
    direction: (trade) => (trade.direction === 'buy' ? '买入' : '卖出'),
    action: (trade) => trade.tradeAction || '--',
    status: (trade) => statusDisplayText(trade.status),
};

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


const BROKER_LABELS: Record<string, string> = {
    qmt_exec: '大 QMT',
    tdx: '通达信',
};

/** 柜台行（QMT/TDX 两套字段）→ 表格行。柜台只回当日数据。 */
const toBrokerTradeRow = (row: Record<string, unknown>, idx: number): TradeRow => {
    const pick = (...keys: string[]): unknown => {
        for (const key of keys) {
            const value = row[key];
            if (value !== undefined && value !== null && value !== '') return value;
        }
        return undefined;
    };
    const code = String(pick('symbol', 'stock_code') ?? '');
    const rawSide = String(pick('side', 'direction') ?? '').toLowerCase();
    const direction = rawSide === 'buy' || rawSide === 'sell' ? rawSide : rawSide || '—';
    const quantity = Number(pick('order_volume', 'total_volume', 'volume') ?? 0);
    const filledQty = Number(pick('traded_volume', 'filled_volume') ?? 0);
    const price = Number(pick('price', 'order_price') ?? 0);
    const avgPrice = Number(pick('traded_price', 'filled_price') ?? 0);
    const rawTime = pick('time', 'order_time', 'traded_time', 'created_at');
    let when = new Date();
    let timeText = '';
    if (typeof rawTime === 'number' || /^\d{10,13}$/.test(String(rawTime ?? ''))) {
        const raw = Number(rawTime);
        when = new Date(raw < 1e12 ? raw * 1000 : raw);
        timeText = when.toLocaleTimeString('zh-CN', { hour12: false });
    } else if (typeof rawTime === 'string' && rawTime.includes(':')) {
        timeText = rawTime.slice(-8);
    } else if (rawTime) {
        const parsed = new Date(String(rawTime));
        if (!Number.isNaN(parsed.getTime())) {
            when = parsed;
            timeText = parsed.toLocaleTimeString('zh-CN', { hour12: false });
        }
    }
    const dateText = `${when.getFullYear()}-${String(when.getMonth() + 1).padStart(2, '0')}-${String(when.getDate()).padStart(2, '0')}`;
    return {
        id: String(pick('order_id', 'order_sysid', 'order_sys_id') ?? `${code}-${idx}`),
        createdAt: when.toISOString(),
        date: dateText,
        time: timeText,
        direction,
        code,
        name: String(pick('instrument_name', 'stock_name', 'name') ?? ''),
        quantity,
        filledQty,
        price,
        avgPrice,
        amount: quantity * price,
        filledValue: filledQty * avgPrice,
        status: String(pick('status', 'order_status') ?? '').toLowerCase(),
        reason: '',
        reasonSummary: '',
        tradeAction: direction === 'buy' ? '买入' : direction === 'sell' ? '卖出' : '',
        exchangeOrderId: String(pick('order_sysid', 'order_id') ?? ''),
        commission: 0,
    };
};


const TradingHistory: React.FC<TradingHistoryProps> = ({ userId, isActive, tradingMode }) => {
    const currentMarket = useAppSelector(selectCurrentMarket);
    const dispatch = useAppDispatch();
    const [timeRange, setTimeRange] = useState<'today' | 'week' | 'month' | 'all'>('all');
    const [searchTerm, setSearchTerm] = useState('');
    const [statusFilter, setStatusFilter] = useState<string>('all');
    const [directionFilter, setDirectionFilter] = useState<string>('all');
    const [currentPage, setCurrentPage] = useState(1);
    const [trades, setTrades] = useState<TradeRow[]>([]);
    const [columnFilters, setColumnFilters] = useState<ColumnFilterState>({});
    // 数据源：系统账本（PG，含历史）/ 券商柜台（跟随「券商接入」，仅当日）
    const [recordSource, setRecordSource] = useState<'ledger' | 'broker'>('ledger');
    const [brokerLabel, setBrokerLabel] = useState('券商');
    const [brokerNote, setBrokerNote] = useState('');
    // 柜台视图可在 QMT / 通达信 之间显式切换（auto = 跟随「券商接入」）
    const [brokerSource, setBrokerSource] = useState<'auto' | 'qmt_exec' | 'tdx'>('auto');
    // 柜台历史区间（留空=当日；QMT 支持历史区间，TDX 只有当日）
    const [brokerRange, setBrokerRange] = useState<[Dayjs, Dayjs] | null>(null);
    // Agent 对账单弹窗
    const [statementOpen, setStatementOpen] = useState(false);
    const [statementLoading, setStatementLoading] = useState(false);
    const [statement, setStatement] = useState<import('../../../services/realTradingService').AgentStatementResponse | null>(null);
    // 每日日结（智能体台账口径：成交/平仓 + 执行事实 + 真实资金锚）
    const [settlementOpen, setSettlementOpen] = useState(false);
    const [settlementLoading, setSettlementLoading] = useState(false);
    const [settlementRows, setSettlementRows] = useState<import('../../../services/realTradingService').DailySettlementRow[]>([]);
    // 当前市场无记录、但别的市场有：给出可一键切换的提示，避免"看起来全空"
    const [marketHint, setMarketHint] = useState<{ market: string; count: number } | null>(null);
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
            if (recordSource === 'broker') {
                const resp = await realTradingService.getBrokerRecords(
                    'orders',
                    brokerSource === 'auto' ? undefined : brokerSource,
                    undefined,
                    brokerRange ? brokerRange[0].format('YYYY-MM-DD') : undefined,
                    brokerRange ? brokerRange[1].format('YYYY-MM-DD') : undefined,
                );
                setBrokerLabel(BROKER_LABELS[resp.broker] || resp.broker || '券商');
                setBrokerNote(resp.note || '');
                const allRows = (resp.rows || []).map((row, idx) => toBrokerTradeRow(row, idx));
                const rows = allRows.filter(
                    (o) => inferMarketOfSymbol(o.code) === currentMarket.toUpperCase(),
                );
                setMarketHint(
                    rows.length === 0 && allRows.length > 0
                        ? { market: inferMarketOfSymbol(allRows[0].code), count: allRows.length }
                        : null,
                );
                setTrades(rows);
                return;
            }
            setBrokerNote('');
            setMarketHint(null);
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

            // 按当前市场过滤订单（symbol 格式推断，与后端 market_rules 同口径）
            const marketFilteredOrders = allOrders.filter(
                o => inferMarketOfSymbol(o.symbol) === currentMarket.toUpperCase()
            );
            setMarketHint(
                marketFilteredOrders.length === 0 && allOrders.length > 0
                    ? { market: inferMarketOfSymbol(allOrders[0].symbol), count: allOrders.length }
                    : null,
            );

            const uniqueCodes = Array.from(new Set(
                marketFilteredOrders
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

            const mapped: TradeRow[] = marketFilteredOrders.map(order => {
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
                // 拒因：v1 台账在 remarks（mark_rejected 加 `Execution rejected:` 前缀），
                // 投影行另有 rejected_reason。全文进 title/导出，短原因进状态列。
                const rawReason = order.remarks ?? (order as any).rejected_reason;

                return {
                    id: order.id.toString(),
                    createdAt: order.submitted_at || order.created_at,
                    date: formatBackendDate(order.submitted_at || order.created_at),
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
                    reason: displayReason(rawReason),
                    reasonSummary: displayReasonSummary(rawReason),
                    tradeAction: actionMap[rawAction] || rawAction,
                    exchangeOrderId: order.exchange_order_id || '',
                    commission: Number((order as any).commission ?? (order as any).fee ?? 0),
                };
            });
            setTrades(mapped);
        } catch (e) {
            console.error("Failed to load orders", e);
            if (recordSource === 'broker') {
                // 柜台查询失败（QMT 通道卡死 / TDX 桥不可达）必须落到界面上，
                // 否则用户只看到一张空表，分不清「没单」还是「查不了」。
                setTrades([]);
                const detail = e instanceof Error ? e.message : String(e);
                setBrokerNote(`柜台查询失败：${detail}`);
            }
        } finally {
            loadingRef.current = false;
        }
    }, [recordSource, brokerSource, brokerRange, timeRange, tradingMode, userId, currentMarket]);

    React.useEffect(() => {
        if (isActive && userId) {
            void loadOrders();
            // 历史区间查询重（QMT 交易上下文 RPC），不轮询；当日柜台 30s 一次，
            // 系统账本 5s 一次（纯 PG，不碰券商通道）。
            if (recordSource === 'broker' && brokerRange) return;
            const interval = setInterval(() => {
                void loadOrders();
            }, recordSource === 'broker' ? 30000 : 5000);
            return () => clearInterval(interval);
        }
    }, [isActive, loadOrders, userId, recordSource, brokerRange]);

    React.useEffect(() => {
        setCurrentPage(1);
    }, [searchTerm, timeRange, statusFilter, directionFilter, columnFilters]);

    const setColumnFilter = useCallback((column: string, next: string[] | undefined) => {
        setColumnFilters((prev) => ({ ...prev, [column]: next }));
    }, []);

    // 筛选面板的可选值取**全量**数据域，而不是当前筛选结果——否则筛选项会自己吃掉自己
    const filterOptions = useMemo(() => ({
        date: collectUniqueValues(trades, COLUMN_ACCESSORS.date).reverse(), // 日期倒序：近的在前
        direction: collectUniqueValues(trades, COLUMN_ACCESSORS.direction),
        action: collectUniqueValues(trades, COLUMN_ACCESSORS.action),
        status: collectUniqueValues(trades, COLUMN_ACCESSORS.status),
    }), [trades]);

    const filteredTrades = useMemo(() => {
        const keyword = searchTerm.trim().toLowerCase();
        const base = trades.filter((trade) => {
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
        return applyColumnFilters(base, columnFilters, COLUMN_ACCESSORS);
    }, [searchTerm, trades, statusFilter, directionFilter, columnFilters]);

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

    const getStatusText = useCallback((status: string) => statusDisplayText(status), []);

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
        备注: trade.reason,
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

    const openStatement = useCallback(async () => {
        setStatementOpen(true);
        setStatementLoading(true);
        try {
            const { realTradingService } = await import('../../../services/realTradingService');
            const range = buildDateRange(timeRange);
            const start = range.startDate
                ? String(range.startDate).slice(0, 10)
                : new Date(Date.now() - 30 * 86400000).toISOString().slice(0, 10);
            const end = new Date().toISOString().slice(0, 10);
            setStatement(await realTradingService.getAgentStatement(start, end));
        } catch {
            message.error('对账单加载失败');
        } finally {
            setStatementLoading(false);
        }
    }, [timeRange]);

    const openSettlement = useCallback(async () => {
        setSettlementOpen(true);
        setSettlementLoading(true);
        try {
            const { realTradingService } = await import('../../../services/realTradingService');
            const range = buildDateRange(timeRange);
            // 默认从 9 月 1 日（实盘起账日）看到今天；「今日/本周/本月」跟随筛选
            const start = range.startDate
                ? String(range.startDate).slice(0, 10)
                : '2026-09-01';
            const end = new Date().toISOString().slice(0, 10);
            const resp = await realTradingService.getDailySettlements(start, end);
            setSettlementRows(resp.rows || []);
        } catch {
            message.error('日结加载失败');
        } finally {
            setSettlementLoading(false);
        }
    }, [timeRange]);

    if (!isActive) return null;

    return (
        <div className="h-full flex flex-col p-6 pb-[65px]">
            {/* Header with Filters */}
            <div className="flex flex-wrap items-center gap-4 mb-4">
                <h3 className="text-base font-bold text-gray-800 flex items-center whitespace-nowrap">
                    <FileText className="mr-2 text-blue-600" size={18} />
                    交易记录
                </h3>
                
                <button
                    onClick={() => void openStatement()}
                    className="px-3 py-1.5 text-xs font-medium rounded-full border border-blue-200 text-blue-600 bg-blue-50 hover:bg-blue-100"
                    title="按 agent 汇总实盘委托/成交（PG 真源，可对券商记录）"
                >
                    Agent 对账单
                </button>

                <button
                    onClick={() => void openSettlement()}
                    className="px-3 py-1.5 text-xs font-medium rounded-full border border-emerald-200 text-emerald-600 bg-emerald-50 hover:bg-emerald-100"
                    title="每日日结：智能体台账（成交/平仓）+ 执行事实（委托/费用）+ 真实资金锚"
                >
                    每日日结
                </button>

                <div className="flex items-center bg-gray-100 p-1 rounded-full gap-1 border border-gray-200">
                    <button
                        onClick={() => setRecordSource('ledger')}
                        className={`px-3 py-1.5 text-xs font-medium rounded-full transition-all ${recordSource === 'ledger' ? 'bg-white text-blue-600 shadow-sm' : 'text-gray-500 hover:text-gray-700'}`}
                        title="系统账本：我们自己的委托台账（含跨日历史）"
                    >
                        系统账本
                    </button>
                    <button
                        onClick={() => setRecordSource('broker')}
                        className={`px-3 py-1.5 text-xs font-medium rounded-full transition-all ${recordSource === 'broker' ? 'bg-white text-blue-600 shadow-sm' : 'text-gray-500 hover:text-gray-700'}`}
                        title="券商柜台：按「设置 → 券商接入」选定的券商查询（仅当日）"
                    >
                        {brokerLabel}柜台
                    </button>
                </div>

                {recordSource === 'broker' && (
                    <DatePicker.RangePicker
                        size="small"
                        value={brokerRange}
                        onChange={(range) =>
                            setBrokerRange(
                                range && range[0] && range[1]
                                    ? [range[0], range[1]]
                                    : null,
                            )
                        }
                        placeholder={['开始日期', '结束日期']}
                        allowClear
                        presets={[
                            { label: '今日', value: [dayjs(), dayjs()] },
                            { label: '9 月 1 日起', value: [dayjs('2026-09-01'), dayjs()] },
                            {
                                label: '近 30 天',
                                value: [dayjs().subtract(29, 'day'), dayjs()],
                            },
                        ]}
                    />
                )}

                {recordSource === 'broker' && (
                    <div className="flex items-center bg-gray-100 p-1 rounded-full gap-1 border border-gray-200">
                        {([['auto', '跟随设置'], ['qmt_exec', '大 QMT'], ['tdx', '通达信']] as const).map(([key, label]) => (
                            <button
                                key={key}
                                onClick={() => setBrokerSource(key)}
                                className={`px-3 py-1.5 text-xs font-medium rounded-full transition-all ${brokerSource === key ? 'bg-white text-blue-600 shadow-sm' : 'text-gray-500 hover:text-gray-700'}`}
                            >
                                {label}
                            </button>
                        ))}
                    </div>
                )}

                <div className={`flex items-center bg-gray-100 p-1 rounded-full gap-1 border border-gray-200 ${recordSource === 'broker' ? 'opacity-60' : ''}`}>
                    {(['today', 'week', 'month', 'all'] as const).map((range) => (
                        <button
                            key={range}
                            onClick={() => setTimeRange(range)}
                            className={`px-4 py-1.5 text-xs font-medium rounded-full transition-all ${timeRange === range
                                ? 'bg-white text-blue-600 shadow-sm'
                                : 'text-gray-500 hover:text-gray-700'
                                }`}
                        >
                            {range === 'today' ? '今日' : range === 'week' ? '本周' : range === 'month' ? '本月' : '全部'}
                        </button>
                    ))}
                </div>

                <div className="flex items-center bg-gray-100 p-1 rounded-full gap-1 border border-gray-200">
                    <button
                        onClick={() => setDirectionFilter('all')}
                        className={`px-3 py-1.5 text-xs font-medium rounded-full transition-all ${directionFilter === 'all' ? 'bg-white text-blue-600 shadow-sm' : 'text-gray-500 hover:text-gray-700'}`}
                    >
                        全部方向
                    </button>
                    <button
                        onClick={() => setDirectionFilter('buy')}
                        className={`px-3 py-1.5 text-xs font-medium rounded-full transition-all ${directionFilter === 'buy' ? 'bg-white text-red-600 shadow-sm' : 'text-gray-500 hover:text-gray-700'}`}
                    >
                        买入
                    </button>
                    <button
                        onClick={() => setDirectionFilter('sell')}
                        className={`px-3 py-1.5 text-xs font-medium rounded-full transition-all ${directionFilter === 'sell' ? 'bg-white text-green-600 shadow-sm' : 'text-gray-500 hover:text-gray-700'}`}
                    >
                        卖出
                    </button>
                </div>

                <div className="flex items-center bg-gray-100 p-1 rounded-full gap-1 border border-gray-200">
                    <button
                        onClick={() => setStatusFilter('all')}
                        className={`px-3 py-1.5 text-xs font-medium rounded-full transition-all ${statusFilter === 'all' ? 'bg-white text-blue-600 shadow-sm' : 'text-gray-500 hover:text-gray-700'}`}
                    >
                        全部状态
                    </button>
                    <button
                        onClick={() => setStatusFilter('filled')}
                        className={`px-3 py-1.5 text-xs font-medium rounded-full transition-all ${statusFilter === 'filled' ? 'bg-white text-green-600 shadow-sm' : 'text-gray-500 hover:text-gray-700'}`}
                    >
                        已成交
                    </button>
                    <button
                        onClick={() => setStatusFilter('pending')}
                        className={`px-3 py-1.5 text-xs font-medium rounded-full transition-all ${statusFilter === 'pending' ? 'bg-white text-orange-600 shadow-sm' : 'text-gray-500 hover:text-gray-700'}`}
                    >
                        委托中
                    </button>
                    <button
                        onClick={() => setStatusFilter('cancelled')}
                        className={`px-3 py-1.5 text-xs font-medium rounded-full transition-all ${statusFilter === 'cancelled' ? 'bg-white text-gray-600 shadow-sm' : 'text-gray-500 hover:text-gray-700'}`}
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
                        className="flex items-center gap-2 px-4 py-[7px] bg-gray-100 hover:bg-gray-200 text-gray-700 rounded-full font-medium transition-all text-xs disabled:opacity-50 disabled:cursor-not-allowed border border-gray-200 shadow-sm hover:shadow-md active:scale-95 ml-auto"
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
                                <th className="px-3 py-2 text-center font-semibold text-gray-600 w-[9%] whitespace-nowrap">
                                    <span className="inline-flex items-center justify-center gap-1">
                                        日期
                                        <ExcelFilterDropdown title="日期" options={filterOptions.date} selected={columnFilters.date} onApply={(next) => setColumnFilter('date', next)} />
                                    </span>
                                </th>
                                <th className="px-3 py-2 text-center font-semibold text-gray-600 w-[8%] whitespace-nowrap">时间</th>
                                <th className="px-3 py-2 text-center font-semibold text-gray-600 w-[6%]">
                                    <span className="inline-flex items-center justify-center gap-1">
                                        方向
                                        <ExcelFilterDropdown title="方向" options={filterOptions.direction} selected={columnFilters.direction} onApply={(next) => setColumnFilter('direction', next)} />
                                    </span>
                                </th>
                                <th className="px-3 py-2 text-center font-semibold text-gray-600 w-[6%]">
                                    <span className="inline-flex items-center justify-center gap-1">
                                        操作
                                        <ExcelFilterDropdown title="操作" options={filterOptions.action} selected={columnFilters.action} onApply={(next) => setColumnFilter('action', next)} />
                                    </span>
                                </th>
                                <th className="px-3 py-2 text-center font-semibold text-gray-600 w-[10%]">代码</th>
                                <th className="px-3 py-2 text-center font-semibold text-gray-600 w-[10%]">名称</th>
                                <th className="px-3 py-2 text-center font-semibold text-gray-600 w-[12%]">成交量/委托量</th>
                                <th className="px-3 py-2 text-center font-semibold text-gray-600 w-[10%]">成交均价</th>
                                <th className="px-3 py-2 text-center font-semibold text-gray-600 w-[10%]">成交金额</th>
                                <th className="px-3 py-2 text-center font-semibold text-gray-600 w-[7%]">手续费</th>
                                <th className="px-3 py-2 text-center font-semibold text-gray-600 w-[12%]">
                                    <span className="inline-flex items-center justify-center gap-1">
                                        状态
                                        <ExcelFilterDropdown title="状态" options={filterOptions.status} selected={columnFilters.status} onApply={(next) => setColumnFilter('status', next)} />
                                    </span>
                                </th>
                            </tr>
                        </thead>
                        <tbody className="divide-y divide-gray-100">
                            {paginatedTrades.length === 0 ? (
                                <tr>
                                    <td colSpan={11} className="px-3 py-12 text-center text-gray-400">
                                        <div className="flex flex-col items-center gap-2">
                                            <FileText size={32} className="text-gray-200" />
                                            <span className="text-sm">
                                                {recordSource === 'broker'
                                                    ? (brokerRange
                                                        ? `${brokerRange[0].format('YYYY-MM-DD')} ~ ${brokerRange[1].format('YYYY-MM-DD')} 柜台无记录`
                                                        : '券商柜台今日无委托记录')
                                                    : '暂无交易记录'}
                                            </span>
                                            <span className="text-xs text-gray-300">
                                                {recordSource === 'broker'
                                                    ? (brokerNote || '柜台当日走实时接口；历史区间仅 QMT 支持（通达信无历史接口）')
                                                    : `${timeRange === 'today' ? '今日' : timeRange === 'week' ? '本周' : timeRange === 'month' ? '本月' : ''}内无委托记录`}
                                            </span>
                                            {marketHint && (
                                                <button
                                                    onClick={() => dispatch(setMarket(marketHint.market as AppMarket))}
                                                    className="text-xs text-blue-600 hover:underline"
                                                >
                                                    当前市场（{currentMarket}）无记录；切到 {marketHint.market} 查看 {marketHint.count} 条
                                                </button>
                                            )}
                                        </div>
                                    </td>
                                </tr>
                            ) : paginatedTrades.map((trade) => {
                                const isFilled = trade.status === 'filled';
                                const isPartial = ['partial', 'partially_filled'].includes(trade.status);
                                const isPending = ['pending', 'submitted', 'open'].includes(trade.status);
                                return (
                                    <tr key={trade.id} className="hover:bg-gray-50 transition-colors">
                                        <td className="px-3 py-2 text-gray-600 text-center whitespace-nowrap font-mono">{trade.date}</td>
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
                                            {/* 拒单/撤单原因常驻一行（title 给全文）：历史记录只写「已拒绝」
                                                不写为什么，用户只能去日志里翻（2026-10-22 实证）。 */}
                                            {!isFilled && trade.reasonSummary && (
                                                <div
                                                    className={`mt-0.5 truncate text-[10px] leading-3 ${
                                                        trade.status === 'rejected' ? 'text-red-500' : 'text-gray-400'
                                                    }`}
                                                    title={trade.reason}
                                                >
                                                    {trade.reasonSummary}
                                                </div>
                                            )}
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

                        <div className="flex-1 flex items-center justify-center gap-6">
                            <div 
                                className={`flex flex-col items-center cursor-pointer p-2 rounded-lg transition-all hover:bg-gray-50 w-[76px] shrink-0 ${statusFilter === 'all' ? 'bg-blue-50 ring-1 ring-inset ring-blue-100' : ''}`}
                                onClick={() => setStatusFilter('all')}
                            >
                                <span className={`text-[10px] mb-1 font-bold whitespace-nowrap ${statusFilter === 'all' ? 'text-blue-600' : 'text-gray-500'}`}>总委托</span>
                                <span className={`font-bold text-sm tabular-nums whitespace-nowrap ${statusFilter === 'all' ? 'text-blue-700' : 'text-gray-900'}`}>
                                    {stats.total} <span className="text-[10px] font-medium text-gray-400">笔</span>
                                </span>
                            </div>

                            <div 
                                className={`flex flex-col items-center cursor-pointer p-2 rounded-lg transition-all hover:bg-gray-50 w-[76px] shrink-0 ${statusFilter === 'filled' ? 'bg-green-50 ring-1 ring-inset ring-green-100' : ''}`}
                                onClick={() => setStatusFilter('filled')}
                            >
                                <span className={`text-[10px] mb-1 font-bold whitespace-nowrap ${statusFilter === 'filled' ? 'text-green-600' : 'text-gray-500'}`}>已成交</span>
                                <span className={`font-bold text-sm tabular-nums whitespace-nowrap ${statusFilter === 'filled' ? 'text-green-700' : 'text-green-600'}`}>
                                    {stats.filled + stats.partial} <span className="text-[10px] font-medium text-green-400">笔</span>
                                </span>
                            </div>

                            <div 
                                className={`flex flex-col items-center cursor-pointer p-2 rounded-lg transition-all hover:bg-gray-50 w-[76px] shrink-0 ${statusFilter === 'pending' ? 'bg-yellow-50 ring-1 ring-inset ring-yellow-100' : ''}`}
                                onClick={() => setStatusFilter('pending')}
                            >
                                <span className={`text-[10px] mb-1 font-bold whitespace-nowrap ${statusFilter === 'pending' ? 'text-yellow-600' : 'text-gray-500'}`}>委托中</span>
                                <span className={`font-bold text-sm tabular-nums whitespace-nowrap ${statusFilter === 'pending' ? 'text-yellow-700' : 'text-yellow-600'}`}>
                                    {stats.pending} <span className="text-[10px] font-medium text-yellow-400">笔</span>
                                </span>
                            </div>

                            <div 
                                className={`flex flex-col items-center cursor-pointer p-2 rounded-lg transition-all hover:bg-gray-50 w-[76px] shrink-0 ${statusFilter === 'cancelled' ? 'bg-gray-50 ring-1 ring-inset ring-gray-200' : ''}`}
                                onClick={() => setStatusFilter('cancelled')}
                            >
                                <span className={`text-[10px] mb-1 font-bold whitespace-nowrap ${statusFilter === 'cancelled' ? 'text-gray-600' : 'text-gray-500'}`}>撤单</span>
                                <span className={`font-bold text-sm tabular-nums whitespace-nowrap ${statusFilter === 'cancelled' ? 'text-gray-700' : 'text-gray-400'}`}>
                                    {stats.cancelled} <span className="text-[10px] font-medium text-gray-300">笔</span>
                                </span>
                            </div>

                            {stats.rejected > 0 && (
                                <div 
                                    className={`flex flex-col items-center cursor-pointer p-2 rounded-lg transition-all hover:bg-gray-50 w-[76px] shrink-0 ${statusFilter === 'rejected' ? 'bg-red-50 ring-1 ring-inset ring-red-100' : ''}`}
                                    onClick={() => setStatusFilter('rejected')}
                                >
                                    <span className={`text-[10px] mb-1 font-bold whitespace-nowrap ${statusFilter === 'rejected' ? 'text-red-600' : 'text-gray-500'}`}>拒绝/过期</span>
                                    <span className={`font-bold text-sm tabular-nums whitespace-nowrap ${statusFilter === 'rejected' ? 'text-red-700' : 'text-red-500'}`}>
                                        {stats.rejected} <span className="text-[10px] font-medium text-red-400">笔</span>
                                    </span>
                                </div>
                            )}

                            <div className="h-8 w-px bg-gray-100 shrink-0"></div>

                            <div className="flex flex-col items-center min-w-[92px] shrink-0">
                                <span className="text-gray-500 text-[10px] mb-1 font-bold whitespace-nowrap">买入金额</span>
                                <span className="font-bold text-red-600 text-sm tabular-nums whitespace-nowrap">
                                    {stats.buyAmount >= 10000 ? `¥${(stats.buyAmount / 10000).toFixed(2)}万` : `¥${stats.buyAmount.toFixed(2)}`}
                                </span>
                            </div>

                            <div className="flex flex-col items-center min-w-[92px] shrink-0">
                                <span className="text-gray-500 text-[10px] mb-1 font-bold whitespace-nowrap">卖出金额</span>
                                <span className="font-bold text-green-600 text-sm tabular-nums whitespace-nowrap">
                                    {stats.sellAmount >= 10000 ? `¥${(stats.sellAmount / 10000).toFixed(2)}万` : `¥${stats.sellAmount.toFixed(2)}`}
                                </span>
                            </div>

                            <div className="flex flex-col items-center min-w-[92px] shrink-0">
                                <span className="text-gray-500 text-[10px] mb-1 font-bold whitespace-nowrap">净买入</span>
                                <span className={`font-bold text-sm tabular-nums whitespace-nowrap ${stats.netBuy >= 0 ? 'text-red-600' : 'text-green-600'}`}>
                                    {Math.abs(stats.netBuy) >= 10000 ? `¥${(Math.abs(stats.netBuy) / 10000).toFixed(2)}万` : `¥${Math.abs(stats.netBuy).toFixed(2)}`}
                                </span>
                            </div>

                            <div className="h-8 w-px bg-gray-100 shrink-0"></div>

                            <div className="flex flex-col items-center min-w-[92px] shrink-0">
                                <span className="text-gray-500 text-[10px] mb-1 font-bold whitespace-nowrap">累计费用</span>
                                <span className="font-bold text-amber-600 text-sm tabular-nums whitespace-nowrap">
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

            <Modal
                title="Agent 交易对账单"
                open={statementOpen}
                onCancel={() => setStatementOpen(false)}
                footer={null}
                width={720}
            >
                {statementLoading ? (
                    <div className="py-8 text-center text-gray-400 text-sm">加载中…</div>
                ) : statement ? (
                    <div>
                        <div className="text-xs text-gray-500 mb-2">
                            {statement.start} ~ {statement.end}
                            {' · '}委托 {statement.total.orders} 笔
                            {' · '}成交额 {statement.total.filled_value.toFixed(2)}
                            {' · '}手续费 {statement.total.commission.toFixed(2)}
                        </div>
                        <table className="w-full text-xs">
                            <thead>
                                <tr className="bg-gray-50 text-gray-500">
                                    <th className="px-2 py-1.5 text-left">Agent</th>
                                    <th className="px-2 py-1.5 text-right">委托数</th>
                                    <th className="px-2 py-1.5 text-right">成交笔数</th>
                                    <th className="px-2 py-1.5 text-right">成交额</th>
                                    <th className="px-2 py-1.5 text-right">手续费</th>
                                </tr>
                            </thead>
                            <tbody className="divide-y divide-gray-100">
                                {statement.items.map((item) => (
                                    <tr key={item.agent}>
                                        <td className="px-2 py-1.5">{item.agent}</td>
                                        <td className="px-2 py-1.5 text-right">{item.orders}</td>
                                        <td className="px-2 py-1.5 text-right">{item.filled_orders}</td>
                                        <td className="px-2 py-1.5 text-right">{item.filled_value.toFixed(2)}</td>
                                        <td className="px-2 py-1.5 text-right">{item.commission.toFixed(2)}</td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                        {statement.note && (
                            <div className="text-[10px] text-gray-400 mt-2">{statement.note}</div>
                        )}
                    </div>
                ) : (
                    <div className="py-8 text-center text-gray-400 text-sm">暂无数据</div>
                )}
            </Modal>

            <Modal
                title="每日日结（智能体台账）"
                open={settlementOpen}
                onCancel={() => setSettlementOpen(false)}
                footer={null}
                width={960}
            >
                {settlementLoading ? (
                    <div className="py-8 text-center text-gray-400 text-sm">加载中…</div>
                ) : settlementRows.length === 0 ? (
                    <div className="py-8 text-center text-gray-400 text-sm">暂无日结</div>
                ) : (
                    <div>
                        <div className="text-xs text-gray-500 mb-2">
                            {settlementRows[settlementRows.length - 1]?.trade_date} ~ {settlementRows[0]?.trade_date}
                            {' · '}共 {settlementRows.length} 个交易日
                            {' · '}成交额合计{' '}
                            {settlementRows.reduce((s, r) => s + (r.buy_value + r.sell_value), 0).toFixed(2)}
                            {' · '}费用合计{' '}
                            {settlementRows.reduce((s, r) => s + Number(r.total_fee || 0), 0).toFixed(2)}
                            {' · '}已实现盈亏合计{' '}
                            {settlementRows.reduce((s, r) => s + Number(r.realized_pnl || 0), 0).toFixed(2)}
                        </div>
                        <div className="max-h-[520px] overflow-auto">
                            <table className="w-full text-xs">
                                <thead className="sticky top-0 bg-gray-50 text-gray-500">
                                    <tr>
                                        <th className="px-2 py-1.5 text-left">日期</th>
                                        <th className="px-2 py-1.5 text-right">委托(成交)</th>
                                        <th className="px-2 py-1.5 text-right">买入</th>
                                        <th className="px-2 py-1.5 text-right">卖出</th>
                                        <th className="px-2 py-1.5 text-right">费用</th>
                                        <th className="px-2 py-1.5 text-right">净现金流</th>
                                        <th className="px-2 py-1.5 text-right">已实现盈亏</th>
                                        <th className="px-2 py-1.5 text-right">总资产</th>
                                        <th className="px-2 py-1.5 text-center">状态</th>
                                    </tr>
                                </thead>
                                <tbody className="divide-y divide-gray-100">
                                    {settlementRows.map((row) => (
                                        <tr key={row.trade_date} className="hover:bg-gray-50">
                                            <td className="px-2 py-1.5 font-mono">{row.trade_date}</td>
                                            <td className="px-2 py-1.5 text-right">
                                                {row.orders_total}({row.orders_filled})
                                            </td>
                                            <td className="px-2 py-1.5 text-right">{Number(row.buy_value).toFixed(2)}</td>
                                            <td className="px-2 py-1.5 text-right">{Number(row.sell_value).toFixed(2)}</td>
                                            <td className="px-2 py-1.5 text-right">{Number(row.total_fee).toFixed(2)}</td>
                                            <td className="px-2 py-1.5 text-right text-gray-700">
                                                {Number(row.net_cash_flow).toFixed(2)}
                                            </td>
                                            <td className={`px-2 py-1.5 text-right ${Number(row.realized_pnl) < 0 ? 'text-green-600' : 'text-red-600'}`}>
                                                {Number(row.realized_pnl).toFixed(2)}
                                            </td>
                                            <td className="px-2 py-1.5 text-right">
                                                {row.total_asset == null ? '—' : Number(row.total_asset).toFixed(2)}
                                            </td>
                                            <td className="px-2 py-1.5 text-center text-[10px] text-gray-400">
                                                {row.status === 'closed' ? '已结' : '无快照'}
                                            </td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        </div>
                        <div className="text-[10px] text-gray-400 mt-2">
                            口径：智能体台账（成交/平仓）+ 执行事实（委托/费用/执行券商）+ 真实资金锚（快照）；
                            金额只认成交，费率为 0 表示券商回报里没有费用字段（不估算）。
                        </div>
                    </div>
                )}
            </Modal>
        </div>
    );
};

export default TradingHistory;
