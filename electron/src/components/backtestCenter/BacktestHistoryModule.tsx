/**
 * 回测历史模块（适配新布局）
 */

import React, { useState } from 'react';
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
import type { BacktestResult } from '../../services/backtestService';
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
      <div className="bg-white rounded-2xl border border-gray-200 shadow-sm p-4">
        <BacktestHistory
          userId={userId}
          onViewDetail={(backtest) => setSelectedBacktest(backtest)}
        />
      </div>

      <AnimatePresence>
        {selectedBacktest && (
          <BacktestDetailModal
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

const BacktestDetailModal: React.FC<BacktestDetailModalProps> = ({
  backtest,
  onClose,
}) => {
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
              <h2 className="text-2xl font-bold text-gray-800 mb-1">回测详情</h2>
              <p className="text-sm text-gray-600">
                {strategyName} | {detailStartDate} ~ {detailEndDate}
              </p>
            </div>
            <button
              onClick={onClose}
              className="p-2 hover:bg-gray-100 rounded-2xl transition-colors"
            >
              <X className="w-6 h-6 text-gray-600" />
            </button>
          </div>
        </div>

        <div className="p-4 space-y-6">
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

          {backtest.equity_curve && backtest.equity_curve.length > 0 && (
            <div className="bg-gray-50 rounded-2xl p-4 border border-gray-200">
              <h3 className="text-lg font-semibold text-gray-800 mb-4">权益曲线</h3>
              <ReactECharts option={equityCurveOption} style={{ height: '280px' }} />
            </div>
          )}

          {backtest.drawdown_curve && backtest.drawdown_curve.length > 0 && (
            <div className="bg-gray-50 rounded-2xl p-4 border border-gray-200">
              <h3 className="text-lg font-semibold text-gray-800 mb-4">回撤曲线</h3>
              <ReactECharts option={drawdownOption} style={{ height: '280px' }} />
            </div>
          )}
        </div>
      </motion.div>
    </motion.div>
  );
};
