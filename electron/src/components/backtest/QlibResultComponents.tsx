/**
 * Qlib 回测结果展示共享组件
 */

import React, { useState } from 'react';
import {
  TrendingUp, Shield, Target, TrendingDown, Download, X, Copy, Check, Wand2, RefreshCw, AlertCircle
} from 'lucide-react';
import ReactECharts from 'echarts-for-react';
import { QlibBacktestResult, QlibBacktestConfig } from '../../types/backtest/qlib';
import { BacktestResult, BacktestConfig, backtestService } from '../../services/backtestService';
import { backtestClient } from '../../services/aiStrategyClients';

type Trade = {
  date: string;
  symbol: string;
  action: 'buy' | 'sell' | string;
  price?: number;
  quantity?: number;
  adj_price?: number;
  adj_quantity?: number;
  factor?: number;
  commission?: number;
  totalAmount?: number;
  balance?: number;
  cash_after?: number;
  position_value_after?: number;
  equity_after?: number;
};

export const QlibResultDisplay: React.FC<{ result: BacktestResult | QlibBacktestResult; fallbackConfig?: BacktestConfig | QlibBacktestConfig | null }> = ({
  result,
  fallbackConfig,
}) => {
  const getMarketColor = (value: number | undefined | null) => {
    const safeValue = Number(value ?? 0);
    return safeValue >= 0 ? 'text-red-600' : 'text-green-600';
  };

  const getMarketCard = (value: number | undefined | null) => {
    const safeValue = Number(value ?? 0);
    return safeValue >= 0
      ? 'text-red-600 bg-red-50'
      : 'text-green-600 bg-green-50';
  };

  const metrics = [
    {
      label: '总收益率',
      value: formatPercent(result.total_return),
      color: getMarketColor(result.total_return),
      cardColor: getMarketCard(result.total_return),
      icon: (result.total_return || 0) > 0 ? TrendingUp : TrendingDown,
    },
    {
      label: '夏普比率',
      value: formatNumber(result.sharpe_ratio, 2),
      color: 'text-amber-600',
      cardColor: 'text-amber-600 bg-amber-50',
      icon: Shield,
    },
    {
      label: '最大回撤',
      value: formatPercent(result.max_drawdown),
      color: (result.max_drawdown || 0) <= 0 ? 'text-green-600' : 'text-red-600',
      cardColor: (result.max_drawdown || 0) <= 0 ? 'text-green-600 bg-green-50' : 'text-red-600 bg-red-50',
      icon: TrendingDown,
    },
    {
      label: '年化收益',
      value: formatPercent(result.annual_return),
      color: getMarketColor(result.annual_return),
      cardColor: getMarketCard(result.annual_return),
      icon: Target,
    },
  ];

  const secondaryMetrics = [
    {
      label: '波动率',
      value: formatPercent(result.volatility),
      color: 'text-amber-600',
    },
    {
      label: '基准收益',
      value: formatPercent(result.benchmark_return),
      color: getMarketColor(result.benchmark_return),
    },
    {
      label: '信息比率',
      value: formatNumber(result.information_ratio, 2),
      color: 'text-amber-600',
    },
    {
      label: 'CAPM Alpha',
      value: formatPercent(result.alpha),
      color: getMarketColor(result.alpha),
    },
  ];

  const equityCurve = Array.isArray(result.equity_curve) ? result.equity_curve : [];

  const equityOption = {
    backgroundColor: 'transparent',
    tooltip: { trigger: 'axis', formatter: (params: any) => `${params[0].axisValue}<br/>权益: ¥${Number(params[0].value).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}` },
    grid: { left: '3%', right: '4%', bottom: '3%', top: '3%', containLabel: true },
    xAxis: { type: 'category', data: equityCurve.map((d) => d.date) },
    yAxis: { type: 'value' },
    series: [{ name: '权益', type: 'line', data: equityCurve.map((d) => d.value), smooth: true, lineStyle: { width: 2, color: '#3b82f6' }, areaStyle: { color: 'rgba(59, 130, 246, 0.1)' } }],
  };

  const displayConfig = (result as any).config || fallbackConfig;
  const initialCapital = displayConfig?.initial_capital;
  const finalValue = resolveFinalValue(result);
  const backtestId = String((result as any)?.backtest_id || '').trim();
  const tradesList = getResultTrades(result);
  const [lazyTrades, setLazyTrades] = useState<Trade[] | null>(null);
  const [isLoadingTrades, setIsLoadingTrades] = useState(false);

  React.useEffect(() => {
    if (!backtestId || tradesList.length > 0) {
      setLazyTrades(null);
      setIsLoadingTrades(false);
      return;
    }

    let cancelled = false;
    setIsLoadingTrades(true);
    backtestService
      .getTrades(backtestId)
      .then((payload) => {
        if (cancelled) return;
        setLazyTrades(Array.isArray(payload?.trades) ? (payload.trades as Trade[]) : []);
      })
      .catch((err) => {
        console.warn('加载回测交易明细失败:', err);
        if (cancelled) return;
        setLazyTrades([]);
      })
      .finally(() => {
        if (!cancelled) setIsLoadingTrades(false);
      });

    return () => {
      cancelled = true;
    };
  }, [backtestId, tradesList.length]);

  const effectiveTrades = tradesList.length > 0 ? tradesList : (lazyTrades ?? []);
  const tradeRows = normalizeTradeRows(effectiveTrades, initialCapital, equityCurve as any[]);
  const rebalanceSummary = React.useMemo(() => {
    const dates = new Set<string>();
    for (const row of tradeRows) {
      const rawDate = String(row?.date || '').slice(0, 10);
      if (rawDate) dates.add(rawDate);
    }

    const count = dates.size;
    if (!count) return '-';
    return String(count);
  }, [tradeRows]);
  const [showTrades, setShowTrades] = useState(false);
  const [isExporting, setIsExporting] = useState(false);

  const handleExportTradesCsv = async () => {
    const stamp = new Date().toISOString().replace(/[-:]/g, '').slice(0, 15);
    const filename = `quick_backtest_trades_${stamp}.csv`;
    const backtestId = String((result as any)?.backtest_id || '').trim();

    // 统一复用后端导出接口，确保“快速回测”与“回测历史”CSV格式一致。
    if (backtestId) {
      setIsExporting(true);
      try {
        const blob = await backtestService.exportCSV(backtestId);
        backtestService.downloadFile(blob, filename);
        return;
      } catch (err) {
        console.warn('统一导出接口失败，回退前端本地导出:', err);
      } finally {
        setIsExporting(false);
      }
    }

    // 回退：无 backtest_id 或接口不可用时，使用本地交易流水导出。
    let rows = tradeRows;
    if (!rows.length && backtestId) {
      try {
        const detail = await backtestService.getTrades(backtestId);
        const detailedTrades = Array.isArray(detail?.trades) ? (detail.trades as Trade[]) : [];
        rows = normalizeTradeRows(detailedTrades, initialCapital, equityCurve as any[]);
      } catch (err) {
        console.warn('补取交易明细失败，回退空交易流水导出:', err);
      }
    }
    if (!rows.length) {
      rows = normalizeTradeRows([], initialCapital, equityCurve as any[]);
    }
    const headers = ['日期', '代码', '方向', '成交价', '成交量', '成交金额', '手续费', '权益余额'];
    const csvRows = rows.map((t) => [
      t.date || '',
      t.symbol || '',
      t.isBuy ? '买入' : '卖出',
      Number(t.displayPrice || 0).toFixed(2),
      String(Number(t.qtyInt || 0)),
      Number(t.amount || 0).toFixed(2),
      Number(t.commission || 0).toFixed(2),
      Number.isFinite(t.equityBalance as number) ? Number(t.equityBalance).toFixed(2) : '',
    ]);
    const csvContent = [headers, ...csvRows]
      .map((row) => row.map(escapeCsvCell).join(','))
      .join('\n');
    downloadCsv(filename, csvContent);
  };

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-4 gap-4">
        {metrics.map((m, i) => (
          <div key={i} className="bg-white rounded-2xl border border-gray-200 p-4 relative">
            <div className="flex items-center justify-between gap-2 mb-3">
              <span className="text-xs text-gray-500 leading-none">{m.label}</span>
              <span className={`w-4 h-4 rounded-md ${m.cardColor} flex items-center justify-center shrink-0`}>
                <m.icon className="w-3 h-3" />
              </span>
            </div>
            <div className={`w-full text-center text-xl font-bold ${m.color} leading-none`}>
              {m.value}
            </div>
          </div>
        ))}
      </div>
      <div className="grid grid-cols-4 gap-4">
        {secondaryMetrics.map((m) => (
          <div key={m.label} className="bg-white rounded-2xl border border-gray-200 p-4 flex flex-col items-center text-center">
            <div className="text-[10px] text-gray-400 font-bold mb-2">{m.label}</div>
            <div className={`w-full text-center text-lg font-bold ${m.color} leading-none`}>{m.value}</div>
          </div>
        ))}
      </div>
      <div className="bg-white rounded-3xl border border-gray-200 p-6">
        <h3 className="text-sm font-bold text-gray-800 mb-4">净值曲线</h3>
        <ReactECharts option={equityOption} style={{ height: '300px' }} />
      </div>
      <div className="bg-white rounded-3xl border border-gray-200 p-6">
        <h3 className="text-sm font-bold text-gray-800 mb-4">统计概览</h3>
        <div className="grid grid-cols-2 gap-x-8">
          <StatItem label="初始资金" value={initialCapital ? `¥${initialCapital.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}` : '-'} />
          <StatItem label="最终价值" value={finalValue ? `¥${finalValue.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}` : '-'} />
          <StatItem label="回测耗时" value={(result as any).execution_time ? `${(result as any).execution_time.toFixed(2)}s` : '-'} />
          <StatItem label="交易次数" value={Math.floor(result.total_trades || 0).toLocaleString()} />
          <StatItem label="调仓交易日" value={isLoadingTrades ? '加载交易明细中...' : rebalanceSummary} />
          <StatItem label="投资胜率" value={formatPercent((result as any).win_rate)} />
        </div>
        <div className="mt-6 flex gap-3">
          <button onClick={() => setShowTrades(true)} className="flex-1 py-2 bg-gray-50 text-gray-700 rounded-xl text-xs font-bold border border-gray-200">交易详情</button>
          <button
            onClick={handleExportTradesCsv}
            disabled={isExporting}
            className="flex-1 py-2 bg-gray-50 text-gray-700 rounded-xl text-xs font-bold border border-gray-200 inline-flex items-center justify-center gap-1.5 disabled:opacity-60 disabled:cursor-not-allowed"
          >
            <Download className="w-3.5 h-3.5" />
            {isExporting ? '导出中...' : '导出数据'}
          </button>
        </div>
      </div>
      {showTrades && (
        <TradeListModal
          rows={tradeRows}
          onClose={() => setShowTrades(false)}
        />
      )}
    </div>
  );
};

export const ErrorLogModal: React.FC<{
  error: string;
  traceback?: string;
  backtestId?: string;
  onClose: () => void;
  onFixed?: (repairedCode: string, strategyId?: string) => void;
}> = ({ error, traceback, backtestId, onClose, onFixed }) => {
  const [copied, setCopied] = useState(false);
  const [isFixing, setIsFixing] = useState(false);
  const [fixResult, setFixResult] = useState<{ success: boolean; message: string } | null>(null);

  const handleCopy = async () => {
    try {
      const textToCopy = traceback ? `Error: ${error}\n\nTraceback:\n${traceback}` : error;
      await navigator.clipboard.writeText(textToCopy || '');
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      setCopied(false);
    }
  };

  const handleAIFix = async () => {
    if (!backtestId) return;
    setIsFixing(true);
    setFixResult(null);
    try {
      const response = await backtestClient.post('/api/v1/qlib/ai-fix', {
        backtest_id: backtestId,
        error_message: error,
        full_error: traceback
      });
      const data = response.data;
      if (data.success) {
        setFixResult({ success: true, message: data.message ? `${data.message} 请关闭本窗口并重新点击执行回测。` : '修复成功！策略已更新，请重新点击执行回测。' });
        if (onFixed) {
          // 延迟通知，让用户看清成功状态
          setTimeout(() => onFixed(data.repaired_code, data.strategy_id), 2000);
        }
      } else {
        setFixResult({ success: false, message: data.message || '修复失败，请稍后重试。' });
      }
    } catch (err) {
      setFixResult({ success: false, message: '网络请求失败，请检查服务连接。' });
    } finally {
      setIsFixing(false);
    }
  };

  return (
    <div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/60 backdrop-blur-md p-4">
      <div className="bg-white rounded-3xl w-full max-w-3xl max-h-[85vh] overflow-hidden flex flex-col shadow-2xl border border-gray-100">
        <div className="p-6 border-b flex items-center justify-between bg-red-50/50 text-red-700 font-bold">
          <div className="flex items-center gap-2">
            <X className="w-5 h-5 text-red-500 rotate-45" />
            <span>回测执行异常详情</span>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={handleCopy}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold rounded-lg bg-white text-gray-700 border border-gray-200 hover:bg-gray-50 shadow-sm"
            >
              {copied ? <Check className="w-3.5 h-3.5" /> : <Copy className="w-3.5 h-3.5" />}
              {copied ? '已复制' : '复制全文'}
            </button>
            <button
              onClick={onClose}
              className="p-1 hover:bg-gray-200 rounded-lg transition-colors text-gray-400"
            >
              <X className="w-5 h-5" />
            </button>
          </div>
        </div>
        <div className="p-6 bg-gray-900 flex-1 overflow-auto custom-scrollbar">
          <pre className="text-xs font-mono text-red-400 whitespace-pre-wrap leading-relaxed">
            {`Error: ${error}${traceback ? `\n\n${traceback}` : ''}`}
          </pre>
        </div>
        <div className="p-4 border-t bg-gray-50 flex items-center justify-between">
          <div className="flex-1">
            {fixResult && (
              <div className={`flex items-center gap-2 text-xs font-bold ${fixResult.success ? 'text-green-600' : 'text-red-600'} animate-in fade-in slide-in-from-left-2`}>
                {fixResult.success ? <Check className="w-4 h-4" /> : <AlertCircle className="w-4 h-4" />}
                {fixResult.message}
              </div>
            )}
          </div>
          <div className="flex gap-3">
            {backtestId && !fixResult?.success && (
              <button
                onClick={handleAIFix}
                disabled={isFixing}
                className="px-4 py-2 bg-gradient-to-r from-blue-600 to-indigo-600 text-white rounded-xl font-bold hover:shadow-lg transition-all text-sm flex items-center gap-2 disabled:opacity-50"
              >
                {isFixing ? (
                  <>
                    <RefreshCw className="w-4 h-4 animate-spin" />
                    正在分析并修复...
                  </>
                ) : (
                  <>
                    <Wand2 className="w-4 h-4" />
                    AI 智能修复 (Beta)
                  </>
                )}
              </button>
            )}
            <button
              onClick={onClose}
              className="px-6 py-2 bg-white text-gray-700 border border-gray-200 rounded-xl font-bold hover:bg-gray-50 transition-all text-sm"
            >
              关闭
            </button>
          </div>
        </div>
      </div>
    </div>
  );
};

const StatItem: React.FC<{ label: string; value: string | number }> = ({ label, value }) => (
  <div className="flex justify-between gap-4 py-2 border-b border-gray-50 text-xs">
    <span className="text-gray-500">{label}</span>
    <span className="font-bold text-gray-800 text-right break-words max-w-[70%]">{value}</span>
  </div>
);

type TradeDisplayRow = Trade & {
  displayPrice: number;
  displayQuantity: number;
  qtyInt: number;
  amount: number;
  equityBalance: number | null;
  isBuy: boolean;
};

const normalizeTradeRows = (
  trades: Trade[],
  initialCapital?: number,
  equityCurve?: Array<{ date?: string; value?: number }>
): TradeDisplayRow[] => {
  const equityByDate = new Map<string, number>();
  if (Array.isArray(equityCurve)) {
    for (const point of equityCurve) {
      const dateKey = String(point?.date || '').slice(0, 10);
      const value = Number(point?.value);
      if (dateKey && Number.isFinite(value)) {
        equityByDate.set(dateKey, value);
      }
    }
  }

  let runningBalance: number | null = Number.isFinite(initialCapital as number)
    ? Number(initialCapital)
    : null;

  const normalizeQty = (symbol: string, qty: number): number => {
    const qtyInt = Math.round(qty);
    const upper = String(symbol || '').toUpperCase();
    if ((upper.startsWith('SH') || upper.startsWith('SZ') || upper.startsWith('BJ')) && qtyInt >= 100) {
      const lotRounded = Math.round(qtyInt / 100) * 100;
      if (Math.abs(qtyInt - lotRounded) <= 2) return lotRounded;
    }
    return qtyInt;
  };

  return trades.map((t) => {
    const factor = Number(t.factor);
    const hasValidFactor = Number.isFinite(factor) && factor > 0;

    const explicitPrice = Number(t.price);
    const explicitQuantity = Number(t.quantity);
    const adjPrice = Number(t.adj_price);
    const adjQuantity = Number(t.adj_quantity);

    // 优先使用复权字段还原真实成交价，兼容旧后端直接返回复权 price 的场景。
    const hasAdjPrice = hasValidFactor && Number.isFinite(adjPrice);
    const hasAdjQuantity = hasValidFactor && Number.isFinite(adjQuantity);
    const hasExplicitPrice = Number.isFinite(explicitPrice);
    const hasExplicitQuantity = Number.isFinite(explicitQuantity);

    const displayPrice = !hasExplicitPrice && hasAdjPrice
      ? adjPrice / factor
      : hasExplicitPrice
        ? explicitPrice
        : 0;
    const displayQuantity = !hasExplicitQuantity && hasAdjQuantity
      ? adjQuantity * factor
      : hasExplicitQuantity
        ? explicitQuantity
        : 0;

    const qtyInt = normalizeQty(String(t.symbol || ''), displayQuantity);
    const amount = Number.isFinite(Number(t.totalAmount))
      ? Number(t.totalAmount)
      : displayPrice * displayQuantity;
    const fee = Number(t.commission || 0);
    const action = String(t.action || '').toLowerCase();
    const isBuy = action === 'buy';
    const isSell = action === 'sell';

    if (runningBalance != null && !Number.isFinite(t.balance as number) && !Number.isFinite(t.equity_after as number)) {
      if (isBuy) runningBalance -= amount + fee;
      if (isSell) runningBalance += amount - fee;
    }

    const tradeDate = String(t.date || '').slice(0, 10);
    const eqOnDate = equityByDate.get(tradeDate);
    const equityBalance = Number.isFinite(eqOnDate as number)
      ? Number(eqOnDate)
      : Number.isFinite(t.equity_after as number)
        ? Number(t.equity_after)
        : Number.isFinite(t.balance as number)
          ? Number(t.balance)
          : runningBalance;

    return { ...t, displayPrice, displayQuantity, qtyInt, amount, equityBalance, isBuy };
  });
};

const TradeListModal: React.FC<{ rows: TradeDisplayRow[]; onClose: () => void }> = ({
  rows,
  onClose,
}) => {
  return (
    <div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/60 backdrop-blur-md p-4">
      <div className="bg-white rounded-3xl w-[88%] h-[80vh] flex flex-col">
        <div className="p-6 border-b flex justify-between font-bold">
          <span>交易明细流水</span>
          <button onClick={onClose}><X /></button>
        </div>
        <div className="flex-1 overflow-auto">
          <table className="w-full text-left text-xs">
            <thead className="bg-gray-50 sticky top-0">
              <tr>
                <th className="p-4">日期</th>
                <th>代码</th>
                <th>方向</th>
                <th className="text-right">成交价</th>
                <th className="text-right">成交量</th>
                <th className="text-right">成交金额</th>
                <th className="text-right">手续费</th>
                <th className="text-right pr-4">权益余额</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((t, i) => (
                <tr key={i} className="border-b border-gray-50">
                  <td className="p-4">{t.date}</td>
                  <td className="font-bold">{t.symbol}</td>
                  <td>
                    <span className={t.isBuy ? 'text-red-500' : 'text-green-500'}>
                      {t.isBuy ? '买入' : '卖出'}
                    </span>
                  </td>
                  <td className="text-right">¥{Number(t.displayPrice || 0).toFixed(2)}</td>
                  <td className="text-right">{Number(t.qtyInt).toLocaleString()}</td>
                  <td className="text-right">¥{Number(t.amount || 0).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</td>
                  <td className="text-right">¥{Number(t.commission || 0).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</td>
                  <td className="text-right pr-4">
                    {Number.isFinite(t.equityBalance as number)
                      ? `¥${Number(t.equityBalance).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
                      : '-'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
};

function formatPercent(v?: number) { return v != null ? `${(v * 100).toFixed(2)}%` : '-'; }
function formatNumber(v?: number, d = 2) { return v != null ? v.toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d }) : '-'; }
function resolveFinalValue(res: any) { return res.portfolio_metrics?.final_value || (res.equity_curve?.length ? res.equity_curve[res.equity_curve.length - 1].value : null); }
function getResultTrades(result: BacktestResult | QlibBacktestResult): Trade[] {
  const directTrades = Array.isArray((result as any)?.trades) ? (result as any).trades : [];
  if (directTrades.length) return directTrades;
  const legacyTrades = Array.isArray((result as any)?.trade_list) ? (result as any).trade_list : [];
  return legacyTrades;
}
function escapeCsvCell(value: string): string { return `"${String(value ?? '').replace(/"/g, '""')}"`; }
function downloadCsv(filename: string, content: string): void {
  const blob = new Blob([`\ufeff${content}`], { type: 'text/csv;charset=utf-8;' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.setAttribute('download', filename);
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
}
