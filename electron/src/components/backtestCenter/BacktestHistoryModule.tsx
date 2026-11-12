/**
 * 回测历史模块（适配新布局）
 */

import React, { useEffect, useState } from 'react';
import { AnimatePresence, motion } from 'framer-motion';
import ReactECharts from 'echarts-for-react';
import {
  Activity,
  DollarSign,
  Shield,
  Target,
  TrendingDown,
  TrendingUp,
  X,
} from 'lucide-react';
import { BacktestHistory, resolveStrategyName } from '../backtest/BacktestHistory';
import { useBacktestCenterStore } from '../../stores/backtestCenterStore';
import { backtestService, type BacktestResult } from '../../services/backtestService';
import { normalizeSymbol } from '../../utils/portfolioUtils';
import { authService } from '../../features/auth/services/authService';
import { normalizeUserId } from '../../features/strategy-wizard/utils/userId';

export const BacktestHistoryModule: React.FC = () => {
  const { backtestConfig } = useBacktestCenterStore();
  const storedUser = authService.getStoredUser() as
    | { id?: string | number; user_id?: string | number }
    | null;
  const resolvedUserId = storedUser?.id ?? storedUser?.user_id;
  const userId = normalizeUserId(resolvedUserId || backtestConfig.user_id || 'default');
  const [selectedBacktest, setSelectedBacktest] = useState<BacktestResult | null>(null);

  return (
    <div className="h-full p-4">
      <motion.div
        initial={{ opacity: 0, y: 8 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.25 }}
        className="bg-white rounded-2xl border border-gray-200 shadow-sm p-4"
      >
        <BacktestHistory
          userId={userId}
          onViewDetail={(backtest) => setSelectedBacktest(backtest)}
        />
      </motion.div>

      <AnimatePresence>
        {selectedBacktest && (
          <BacktestDetailModal
            key={selectedBacktest.backtest_id}
            backtest={selectedBacktest}
            onClose={() => setSelectedBacktest(null)}
          />
        )}
      </AnimatePresence>
    </div>
  );
};

// ============================================================================
// 回测详情模态框
// ============================================================================

interface BacktestDetailModalProps {
  backtest: BacktestResult;
  onClose: () => void;
}

export const BacktestDetailModal: React.FC<BacktestDetailModalProps> = ({
  backtest: summary,
  onClose,
}) => {
  const [detail, setDetail] = useState<BacktestResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [attempt, setAttempt] = useState(0);
  const [page, setPage] = useState(1);
  const [query, setQuery] = useState('');
  const [side, setSide] = useState('all');
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError('');
    setDetail(null);
    setPage(1);
    backtestService.getResult(summary.backtest_id, false).then((result) => {
      if (!cancelled) setDetail(result);
    }).catch(() => {
      if (!cancelled) setError('完整回测结果加载失败，请重试。');
    }).finally(() => {
      if (!cancelled) setLoading(false);
    });
    return () => { cancelled = true; };
  }, [summary.backtest_id, attempt]);
  const backtest = detail ? { ...summary, ...detail } : summary;
  const trades = (detail?.trades || []).filter((trade) =>
    (side === 'all' || trade.action === side) &&
    (!query || normalizeSymbol(trade.symbol).toLowerCase().includes(query.trim().toLowerCase()) || trade.date.includes(query.trim()))
  );
  const pageCount = Math.max(1, Math.ceil(trades.length / 20));
  const currentPage = Math.min(page, pageCount);
  const number = (value: number | undefined, digits = 2) =>
    typeof value === 'number' && Number.isFinite(value)
      ? value.toLocaleString('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits }) : '—';
  const config = (backtest as BacktestResult & { config?: Record<string, unknown> }).config || {};
  const strategyName = resolveStrategyName(backtest);
  const detailStartDate = String(backtest.start_date || config.start_date || '-');
  const detailEndDate = String(backtest.end_date || config.end_date || '-');

  const formatMoneyAxisLabel = (value: number) => {
    if (!Number.isFinite(value)) return '0.00';
    const abs = Math.abs(value);
    if (abs >= 100000000) return `${(value / 100000000).toFixed(2)}亿`;
    if (abs >= 10000) return `${(value / 10000).toFixed(2)}万`;
    return value.toFixed(2);
  };

  const formatMoneyTooltip = (value: number) => {
    if (!Number.isFinite(value)) return '¥0.00';
    return `¥${value.toLocaleString('zh-CN', {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    })}`;
  };

  const metrics = [
    {
      label: '总收益率',
      value: `${((backtest.total_return || 0) * 100).toFixed(2)}%`,
      icon: TrendingUp,
      color: (backtest.total_return || 0) > 0 ? 'text-red-600' : 'text-green-600',
    },
    {
      label: '年化收益',
      value: `${((backtest.annual_return || 0) * 100).toFixed(2)}%`,
      icon: Activity,
      color: (backtest.annual_return || 0) >= 0 ? 'text-red-600' : 'text-green-600',
    },
    {
      label: '夏普比率',
      value: (backtest.sharpe_ratio || 0).toFixed(2),
      icon: Shield,
      color: 'text-orange-600',
    },
    {
      label: '最大回撤',
      value: `${((backtest.max_drawdown || 0) * 100).toFixed(2)}%`,
      icon: TrendingDown,
      color: 'text-green-600',
    },
    {
      label: '胜率',
      value: `${((backtest.win_rate || 0) * 100).toFixed(2)}%`,
      icon: Target,
      color: 'text-red-600',
    },
    {
      label: '盈亏比',
      value: (backtest.profit_factor || 0).toFixed(2),
      icon: DollarSign,
      color: 'text-gray-600',
    },
  ];

  const equityCurveOption = {
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'axis',
      valueFormatter: (value: number) => formatMoneyTooltip(Number(value)),
    },
    grid: { left: '3%', right: '4%', bottom: '3%', top: '6%', containLabel: true },
    xAxis: {
      type: 'category',
      data: backtest.equity_curve?.map((d) => d.date) || [],
      axisLabel: { color: '#6b7280' },
      axisLine: { lineStyle: { color: '#d1d5db' } },
    },
    yAxis: {
      type: 'value',
      axisLabel: {
        color: '#6b7280',
        formatter: (value: number) => formatMoneyAxisLabel(value),
      },
      axisLine: { lineStyle: { color: '#d1d5db' } },
      splitLine: { lineStyle: { color: '#e5e7eb' } },
    },
    series: [
      {
        name: '权益',
        type: 'line',
        data: backtest.equity_curve?.map((d) => d.value) || [],
        smooth: true,
        lineStyle: { width: 2, color: '#ef4444' },
        areaStyle: { color: 'rgba(239, 68, 68, 0.15)' },
      },
    ],
  };

  const drawdownOption = {
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'axis',
      valueFormatter: (value: number) => `${(Number(value) * 100).toFixed(2)}%`,
    },
    grid: { left: '3%', right: '4%', bottom: '3%', top: '6%', containLabel: true },
    xAxis: {
      type: 'category',
      data: backtest.drawdown_curve?.map((d) => d.date) || [],
      axisLabel: { color: '#6b7280' },
      axisLine: { lineStyle: { color: '#d1d5db' } },
    },
    yAxis: {
      type: 'value',
      axisLabel: {
        color: '#6b7280',
        formatter: (value: number) => `${(value * 100).toFixed(2)}%`,
      },
      axisLine: { lineStyle: { color: '#d1d5db' } },
      splitLine: { lineStyle: { color: '#e5e7eb' } },
    },
    series: [
      {
        name: '回撤',
        type: 'line',
        data: backtest.drawdown_curve?.map((d: { value?: number; drawdown?: number }) => d.value ?? d.drawdown ?? 0) || [],
        smooth: true,
        lineStyle: { width: 2, color: '#22c55e' },
        areaStyle: { color: 'rgba(34, 197, 94, 0.15)' },
      },
    ],
  };

  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/50 backdrop-blur-sm"
      onClick={onClose}
    >
      <motion.div
        initial={{ scale: 0.95, y: 10 }}
        animate={{ scale: 1, y: 0 }}
        exit={{ scale: 0.95, y: 10 }}
        onClick={(e) => e.stopPropagation()}
        className="bg-white rounded-2xl border border-gray-200 shadow-xl max-w-5xl w-full max-h-[90vh] overflow-auto"
      >
        <div className="sticky top-0 z-10 bg-white border-b border-gray-200 p-4">
          <div className="flex items-center justify-between">
            <div>
              <p className="text-[11px] font-semibold text-gray-500 uppercase tracking-wider mb-1">Backtest Detail</p>
              <h2 className="text-lg font-bold text-slate-800 tracking-tight mb-1">回测详情</h2>
              <p className="text-sm text-gray-600">
                {strategyName} | {detailStartDate} ~ {detailEndDate}
              </p>
            </div>
            <button
              aria-label="关闭回测详情"
              onClick={onClose}
              className="p-2 hover:bg-gray-100 rounded-2xl transition-colors"
            >
              <X className="w-6 h-6 text-gray-600" />
            </button>
          </div>
        </div>

        <div className="p-4 space-y-6">
          {loading && <p role="status" className="text-blue-600">正在加载收益曲线和成交记录…</p>}
          {error && <div role="alert" className="text-red-600">{error} <button onClick={() => setAttempt((n) => n + 1)} className="underline">重新加载</button></div>}
          <div className="grid grid-cols-3 gap-4">
            {metrics.map((metric) => (
              <div
                key={metric.label}
                className="bg-gray-50 rounded-2xl p-4 border border-gray-200"
              >
                <div className="flex items-center justify-between mb-2">
                  <span className="text-sm text-gray-600">{metric.label}</span>
                  <metric.icon className={`w-4 h-4 ${metric.color}`} />
                </div>
                <div className={`text-2xl font-bold ${metric.color}`}>{metric.value}</div>
              </div>
            ))}
          </div>

          {detail?.equity_curve && detail.equity_curve.length > 0 && (
            <div className="bg-gray-50 rounded-2xl p-4 border border-gray-200">
              <h3 className="text-lg font-bold text-slate-800 tracking-tight mb-4">收益曲线（账户权益）</h3>
              <ReactECharts option={equityCurveOption} style={{ height: '280px' }} />
            </div>
          )}

          {detail?.drawdown_curve && detail.drawdown_curve.length > 0 && (
            <div className="bg-gray-50 rounded-2xl p-4 border border-gray-200">
              <h3 className="text-lg font-bold text-slate-800 tracking-tight mb-4">回撤曲线</h3>
              <ReactECharts option={drawdownOption} style={{ height: '280px' }} />
            </div>
          )}
          {detail && (
            <section className="bg-gray-50 rounded-2xl p-4 border border-gray-200">
              {!detail.equity_curve?.length && <p className="text-gray-500 mb-4">此记录未保存权益曲线。</p>}
              <h3 className="text-lg font-bold text-slate-800 mb-3">成交记录 · {detail.trades?.length || 0} 笔</h3>
              <div className="flex gap-3 mb-3">
                <input aria-label="搜索成交记录" placeholder="股票代码或交易日期" value={query} onChange={(e) => { setQuery(e.target.value); setPage(1); }} className="border rounded-lg px-3 py-2 flex-1 min-w-0" />
                <select aria-label="成交方向" value={side} onChange={(e) => { setSide(e.target.value); setPage(1); }} className="border rounded-lg px-3">
                  <option value="all">全部买卖</option><option value="buy">买入</option><option value="sell">卖出</option>
                </select>
              </div>
              <div className="overflow-x-auto">
                <table className="w-full text-sm whitespace-nowrap">
                  <thead><tr>{['交易日期', '股票代码', '方向', '成交价', '数量', '成交金额', '交易费用'].map((label) => <th key={label} className="p-2 text-left border-b">{label}</th>)}</tr></thead>
                  <tbody>{trades.slice((currentPage - 1) * 20, currentPage * 20).map((trade, index) => (
                    <tr key={`${currentPage}-${index}`} className="border-b border-gray-200">
                      <td className="p-2">{trade.date}</td><td className="p-2">{normalizeSymbol(trade.symbol)}</td>
                      <td className={`p-2 ${trade.action === 'buy' ? 'text-red-600' : 'text-green-600'}`}>{trade.action === 'buy' ? '买入' : trade.action === 'sell' ? '卖出' : trade.action}</td>
                      <td className="p-2">{number(trade.price, 4)}</td><td className="p-2">{number(trade.quantity)}</td>
                      <td className="p-2">{number(trade.amount ?? trade.totalAmount)}</td><td className="p-2">{number(trade.commission)}</td>
                    </tr>
                  ))}</tbody>
                </table>
                {!trades.length && <p className="py-6 text-gray-500">{detail.trades?.length ? '没有符合筛选条件的成交。' : '此记录没有已保存的成交明细。'}</p>}
              </div>
              <div className="flex justify-end items-center gap-4 mt-4">
                <span>共 {trades.length} 笔 · 第 {currentPage} / {pageCount} 页</span>
                <button disabled={currentPage <= 1} onClick={() => setPage(currentPage - 1)} className="border rounded-lg px-3 py-1 disabled:opacity-40">上一页</button>
                <button disabled={currentPage >= pageCount} onClick={() => setPage(currentPage + 1)} className="border rounded-lg px-3 py-1 disabled:opacity-40">下一页</button>
              </div>
            </section>
          )}
        </div>
      </motion.div>
    </motion.div>
  );
};
