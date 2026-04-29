/**
 * 策略对比组件
 *
 * 功能：
 * - 选择两个回测进行对比
 * - 指标对比表格
 * - 雷达图对比
 * - 权益曲线对比
 * - 详细差异分析
 */

import React, { useMemo, useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import {
  GitCompare,
  TrendingUp,
  TrendingDown,
  BarChart3,
  AlertCircle,
  X,
  Search,
} from 'lucide-react';
import { useBacktestComparison, useBacktestHistory } from '../../hooks/useBacktestQueries';
import { BacktestResult, ComparisonResult } from '../../services/backtestService';
import { format } from 'date-fns';
import { zhCN } from 'date-fns/locale';
import { resolveStrategyName, resolveBacktestPeriod } from './BacktestHistory';

interface BacktestComparisonProps {
  userId: string;
  defaultBacktest1?: string;
  defaultBacktest2?: string;
}

type BacktestRecordExt = BacktestResult & {
  strategy_display_name?: string;
  strategy_type?: string;
  qlib_strategy_type?: string;
  config?: {
    strategy_display_name?: string;
    strategy_name?: string;
    strategy_type?: string;
    qlib_strategy_type?: string;
  };
};

type ComparisonMetricRow = {
  metric: string;
  metric_key?: string;
  value1: number;
  value2: number;
  difference?: number;
  percentage_diff?: number;
  percentage_dif?: number;
  better?: 1 | 2 | 'equal';
  winner?: 'backtest1' | 'backtest2' | 'tie';
};

type ComparisonPayload = ComparisonResult & {
  backtest1?: BacktestRecordExt;
  backtest2?: BacktestRecordExt;
  result_1?: BacktestRecordExt;
  result_2?: BacktestRecordExt;
  metrics_comparison?: ComparisonMetricRow[];
  insights?: string[];
  summary?: {
    backtest1_wins: number;
    backtest2_wins: number;
    ties: number;
  };
};

const KEY_METRICS: Array<{
  key: string;
  label: string;
  higherIsBetter: boolean;
}> = [
    { key: 'total_return', label: '总收益率', higherIsBetter: true },
    { key: 'annual_return', label: '年化收益', higherIsBetter: true },
    { key: 'max_drawdown', label: '最大回撤', higherIsBetter: false },
    { key: 'sharpe_ratio', label: '夏普比率', higherIsBetter: true },
    { key: 'information_ratio', label: '信息比率', higherIsBetter: true },
    { key: 'volatility', label: '波动率', higherIsBetter: false },
  ];

// 移除本地 resolveStrategyName 和 STRATEGY_NAME_MAP，改用导入的版本

const normalizeComparison = (raw: ComparisonPayload | undefined): ComparisonPayload | undefined => {
  if (!raw) return raw;
  const backtest1 = (raw as any).backtest1 || (raw as any).result_1;
  const backtest2 = (raw as any).backtest2 || (raw as any).result_2;
  const normalizedMetrics = (raw.metrics_comparison || []).map((item: any) => {
    const betterFromWinner =
      item.winner === 'backtest1' ? 1 :
        item.winner === 'backtest2' ? 2 :
          item.winner === 'tie' ? 'equal' :
            undefined;

    return {
      ...item,
      metric: item.metric_key || item.metric,
      difference: item.difference ?? (item.value1 != null && item.value2 != null ? item.value1 - item.value2 : undefined),
      percentage_diff: item.percentage_diff ?? item.percentage_dif,
      better: item.better ?? betterFromWinner,
    };
  });
  return {
    ...raw,
    backtest1,
    backtest2,
    metrics_comparison: normalizedMetrics,
    insights: raw.insights || [],
  };
};

const isDrawdownMetric = (metricKey: string): boolean => {
  const normalized = metricKey.toLowerCase();
  return normalized.includes('max_drawdown') || normalized.includes('drawdown');
};

export const BacktestComparison: React.FC<BacktestComparisonProps> = ({
  userId,
  defaultBacktest1,
  defaultBacktest2,
}) => {
  const [backtest1Id, setBacktest1Id] = useState<string | null>(defaultBacktest1 || null);
  const [backtest2Id, setBacktest2Id] = useState<string | null>(defaultBacktest2 || null);
  const [backtest1Name, setBacktest1Name] = useState<string>('');
  const [backtest2Name, setBacktest2Name] = useState<string>('');
  const [showSelector, setShowSelector] = useState<1 | 2 | null>(null);

  // 查询对比结果
  const { data: rawComparison, isLoading, error } = useBacktestComparison(
    userId,
    backtest1Id,
    backtest2Id
  );
  const comparison = useMemo(
    () => normalizeComparison(rawComparison as ComparisonPayload | undefined),
    [rawComparison]
  );
  const comparisonReady = Boolean(comparison?.backtest1 && comparison?.backtest2);

  return (
    <div className="bg-white rounded-2xl border border-gray-200 overflow-hidden">
      {/* 头部 */}
      <div className="p-6 border-b border-gray-200">
        <div className="flex items-center gap-3 mb-4">
          <GitCompare className="w-6 h-6 text-purple-500" />
          <h2 className="text-xl font-bold text-gray-800">策略对比</h2>
        </div>

        {/* 选择器 */}
        <div className="grid grid-cols-2 gap-4">
          <BacktestSelector
            label="策略 A"
            selectedId={backtest1Id}
            selectedName={backtest1Name}
            onSelect={() => setShowSelector(1)}
            onClear={() => {
              setBacktest1Id(null);
              setBacktest1Name('');
            }}
          />

          <BacktestSelector
            label="策略 B"
            selectedId={backtest2Id}
            selectedName={backtest2Name}
            onSelect={() => setShowSelector(2)}
            onClear={() => {
              setBacktest2Id(null);
              setBacktest2Name('');
            }}
          />
        </div>
      </div>

      {/* 对比内容 */}
      <div className="p-6">
        {!backtest1Id || !backtest2Id ? (
          <EmptyState />
        ) : isLoading ? (
          <LoadingState />
        ) : error ? (
          <ErrorState error={error.message} />
        ) : comparisonReady && comparison ? (
          <ComparisonContent comparison={comparison} />
        ) : comparison ? (
          <ErrorState error="对比结果数据不完整，请重新选择回测记录后再试。" />
        ) : null}
      </div>

      {/* 选择器模态框 */}
      <AnimatePresence>
        {showSelector && (
          <BacktestSelectorModal
            userId={userId}
            excludeId={showSelector === 1 ? backtest2Id : backtest1Id}
            onSelect={(id, strategyName) => {
              if (showSelector === 1) {
                setBacktest1Id(id);
                setBacktest1Name(strategyName);
              } else {
                setBacktest2Id(id);
                setBacktest2Name(strategyName);
              }
              setShowSelector(null);
            }}
            onClose={() => setShowSelector(null)}
          />
        )}
      </AnimatePresence>
    </div>
  );
};

// ============================================================================
// 回测选择器
// ============================================================================

interface BacktestSelectorProps {
  label: string;
  selectedId: string | null;
  selectedName?: string;
  onSelect: () => void;
  onClear: () => void;
}

const BacktestSelector: React.FC<BacktestSelectorProps> = ({
  label,
  selectedId,
  selectedName,
  onSelect,
  onClear,
}) => {
  return (
    <div className="relative">
      <label className="block text-sm text-gray-600 mb-2">{label}</label>
      {selectedId ? (
        <div className="flex items-center gap-2 p-3 bg-gray-50 border border-gray-200 rounded-2xl">
          <BarChart3 className="w-4 h-4 text-blue-500" />
          <span className="flex-1 text-sm text-gray-800 truncate">
            {selectedName || selectedId.slice(0, 8)}
          </span>
          <button
            onClick={onClear}
            className="p-1 hover:bg-gray-200 rounded-2xl transition-colors"
          >
            <X className="w-4 h-4 text-gray-600" />
          </button>
        </div>
      ) : (
        <button
          onClick={onSelect}
          className="w-full p-3 border-2 border-dashed border-gray-300 rounded-2xl hover:border-blue-500 hover:bg-blue-50 transition-colors text-gray-600 hover:text-blue-600"
        >
          点击选择回测
        </button>
      )}
    </div>
  );
};

// ============================================================================
// 选择器模态框
// ============================================================================

interface BacktestSelectorModalProps {
  userId: string;
  excludeId: string | null;
  onSelect: (id: string, strategyName: string) => void;
  onClose: () => void;
}

const BacktestSelectorModal: React.FC<BacktestSelectorModalProps> = ({
  userId,
  excludeId,
  onSelect,
  onClose,
}) => {
  const [searchTerm, setSearchTerm] = useState('');
  const { data: history = [] } = useBacktestHistory(userId, {
    sort_by: 'created_at',
    sort_order: 'desc',
    page_size: 50,
  });

  const filteredHistory = history
    .filter((bt) => bt.backtest_id !== excludeId && bt.status === 'completed')
    .filter((bt) => {
      if (!searchTerm) return true;
      const term = searchTerm.toLowerCase();
      return (
        resolveStrategyName(bt).toLowerCase().includes(term) ||
        bt.symbol?.toLowerCase().includes(term) ||
        bt.backtest_id.toLowerCase().includes(term)
      );
    });

  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/50 backdrop-blur-sm"
      onClick={onClose}
    >
      <motion.div
        initial={{ scale: 0.9, y: 20 }}
        animate={{ scale: 1, y: 0 }}
        exit={{ scale: 0.9, y: 20 }}
        onClick={(e) => e.stopPropagation()}
        className="bg-white rounded-2xl border border-gray-200 w-full max-w-2xl max-h-[80vh] overflow-hidden flex flex-col"
      >
        {/* 头部 */}
        <div className="p-6 border-b border-gray-200">
          <div className="flex items-center justify-between mb-4">
            <h3 className="text-xl font-bold text-gray-800">选择回测</h3>
            <button
              onClick={onClose}
              className="p-2 hover:bg-gray-50 rounded-2xl transition-colors"
            >
              <X className="w-5 h-5 text-gray-600" />
            </button>
          </div>

          {/* 搜索框 */}
          <div className="relative">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400" />
            <input
              type="text"
              placeholder="搜索策略名称、股票代码或回测ID..."
              value={searchTerm}
              onChange={(e) => setSearchTerm(e.target.value)}
              className="w-full pl-10 pr-4 py-2 bg-gray-50 border border-gray-200 rounded-2xl text-gray-800 placeholder-gray-500 focus:outline-none focus:border-blue-500 focus:ring-2 focus:ring-blue-500/20"
            />
          </div>
        </div>

        {/* 列表 */}
        <div className="flex-1 overflow-auto p-4">
          {filteredHistory.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-12 text-gray-600">
              <AlertCircle className="w-12 h-12 mb-4 opacity-50" />
              <p>没有可用的回测记录</p>
            </div>
          ) : (
            <div className="space-y-2">
              {filteredHistory.map((backtest) => (
                <button
                  key={backtest.backtest_id}
                  onClick={() => onSelect(backtest.backtest_id, resolveStrategyName(backtest))}
                  className="w-full p-4 bg-gray-50 hover:bg-gray-100 border border-gray-200 rounded-2xl transition-colors text-left"
                >
                  <div className="flex items-center justify-between mb-2">
                    <span className="text-sm font-medium text-gray-800">
                      {resolveStrategyName(backtest)}
                    </span>
                    <span className="text-xs text-gray-600">
                      {format(new Date(backtest.created_at || ''), 'yyyy-MM-dd', { locale: zhCN })}
                    </span>
                  </div>
                  <div className="flex items-center gap-4 text-xs text-gray-600">
                    <span>收益: {((backtest.total_return || 0) * 100).toFixed(2)}%</span>
                    <span>夏普: {(backtest.sharpe_ratio || 0).toFixed(2)}</span>
                    <span className="font-mono opacity-50">{backtest.backtest_id.slice(0, 8)}</span>
                  </div>
                </button>
              ))}
            </div>
          )}
        </div>
      </motion.div>
    </motion.div>
  );
};

// ============================================================================
// 对比内容
// ============================================================================

interface ComparisonContentProps {
  comparison: ComparisonPayload;
}

const ComparisonContent: React.FC<ComparisonContentProps> = ({ comparison }) => {
  const { backtest1, backtest2, metrics_comparison } = comparison;
  const mergedMetrics = mergeMetricsComparison(metrics_comparison, backtest1, backtest2);

  return (
    <div className="space-y-6">
      {/* 基本信息 */}
      <div className="grid grid-cols-2 gap-4">
        <BacktestCard backtest={backtest1} label="策略 A" />
        <BacktestCard backtest={backtest2} label="策略 B" />
      </div>

      {/* 关键指标对比 */}
      <MetricsComparisonTable metrics={mergedMetrics} />
    </div>
  );
};

// ============================================================================
// 回测卡片
// ============================================================================

interface BacktestCardProps {
  backtest: BacktestResult;
  label: string;
}

const BacktestCard: React.FC<BacktestCardProps> = ({ backtest, label }) => {
  const totalReturn = backtest.total_return || 0;
  const isProfit = totalReturn > 0;
  const modelName = (backtest as any).model_name || '-';

  return (
    <div className="bg-gray-50 rounded-2xl p-6 border border-gray-200 flex flex-col items-center text-center">
      <div className="text-xs font-medium text-gray-500 mb-2 uppercase tracking-wider">{label}</div>
      <div className="text-xl font-bold text-gray-800 mb-1 truncate w-full" title={resolveStrategyName(backtest)}>
        {resolveStrategyName(backtest)}
      </div>
      <div className="text-xs text-gray-400 mb-3 truncate w-full" title={modelName}>
        {modelName}
      </div>
      <div className="flex items-center justify-center gap-2 mb-3">
        {isProfit ? (
          <TrendingUp className="w-6 h-6 text-red-500" />
        ) : (
          <TrendingDown className="w-6 h-6 text-green-500" />
        )}
        <span className={`text-3xl font-black ${isProfit ? 'text-red-500' : 'text-green-500'}`}>
          {(totalReturn * 100).toFixed(2)}%
        </span>
      </div>
      <div className="text-sm text-gray-500">
        {resolveBacktestPeriod(backtest)}
      </div>
    </div>
  );
};

// ============================================================================
// 指标对比表格
// ============================================================================

interface MetricsComparisonTableProps {
  metrics: Array<{
    metric: string;
    label: string;
    value1: number | null;
    value2: number | null;
    difference?: number | null;
    percentage_diff?: number | null;
    better?: 1 | 2 | 'equal';
  }>;
}

const MetricsComparisonTable: React.FC<MetricsComparisonTableProps> = ({ metrics }) => {
  return (
    <div className="bg-gray-50 rounded-2xl border border-gray-200 p-4">
      <h3 className="text-lg font-semibold text-gray-800 mb-4">关键指标对比</h3>
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        {metrics.map((metric) => (
          <div key={metric.metric} className="bg-white rounded-2xl border border-gray-200 p-5 flex flex-col items-center text-center">
            <div className="w-full flex items-center justify-between mb-4">
              <span className="text-sm font-semibold text-gray-700">{metric.label}</span>
              {metric.better === 1 ? (
                <span className="px-2 py-0.5 text-xs font-medium bg-red-100 text-red-700 rounded-full">A 更优</span>
              ) : metric.better === 2 ? (
                <span className="px-2 py-0.5 text-xs font-medium bg-blue-100 text-blue-700 rounded-full">B 更优</span>
              ) : (
                <span className="px-2 py-0.5 text-xs font-medium bg-gray-100 text-gray-600 rounded-full">持平</span>
              )}
            </div>
            <div className="grid grid-cols-2 gap-4 w-full">
              <div className="space-y-1">
                <p className="text-[10px] text-gray-400 uppercase font-bold tracking-tight">策略 A</p>
                <p className="text-lg font-bold text-gray-800">{formatMetricValue(metric.metric, metric.value1)}</p>
              </div>
              <div className="space-y-1">
                <p className="text-[10px] text-gray-400 uppercase font-bold tracking-tight">策略 B</p>
                <p className="text-lg font-bold text-gray-800">{formatMetricValue(metric.metric, metric.value2)}</p>
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
};

// ============================================================================
// 状态组件
// ============================================================================

const EmptyState = () => (
  <div className="flex flex-col items-center justify-center py-20 text-gray-400">
    <GitCompare className="w-16 h-16 mb-4 opacity-50" />
    <p className="text-lg mb-2">选择两个回测进行对比</p>
    <p className="text-sm">点击上方按钮选择回测记录</p>
  </div>
);

const LoadingState = () => (
  <div className="flex items-center justify-center py-20">
    <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-blue-400"></div>
  </div>
);

const ErrorState: React.FC<{ error: string }> = ({ error }) => (
  <div className="flex flex-col items-center justify-center py-20 text-red-400">
    <AlertCircle className="w-12 h-12 mb-4" />
    <p className="text-lg mb-2">加载失败</p>
    <p className="text-sm text-gray-400">{error}</p>
  </div>
);

// ============================================================================
// 工具函数
// ============================================================================

function formatMetricValue(metric: string, value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) {
    return '-';
  }
  if (metric.includes('return') || metric.includes('rate') || metric === 'volatility') {
    return `${(value * 100).toFixed(2)}%`;
  }
  if (metric === 'max_drawdown' || metric === 'drawdown') {
    return `${(value * 100).toFixed(2)}%`;
  }
  return value.toFixed(2);
}

function mergeMetricsComparison(
  metrics: ComparisonMetricRow[],
  backtest1: BacktestResult,
  backtest2: BacktestResult
) {
  const list = Array.isArray(metrics) ? [...metrics] : [];
  const metricMap = new Map<string, ComparisonMetricRow>();
  for (const item of list) {
    const key = item.metric_key || item.metric;
    if (key) metricMap.set(key, item);
  }
  return KEY_METRICS.map((meta) => {
    const existing = metricMap.get(meta.key);
    if (existing) {
      return {
        ...existing,
        metric: meta.key,
        label: meta.label,
        better: existing.better ?? evaluateBetter(existing.value1, existing.value2, meta.higherIsBetter, meta.key),
      };
    }
    return {
      ...buildMetricRow(
        meta.key,
        getMetricValue(backtest1, meta.key),
        getMetricValue(backtest2, meta.key),
        meta.higherIsBetter,
        meta.key
      ),
      label: meta.label,
    };
  });
}

function getMetricValue(backtest: BacktestResult, metricKey: string): number | undefined {
  const value = (backtest as unknown as Record<string, number | undefined>)[metricKey];
  return typeof value === 'number' ? value : undefined;
}

function evaluateBetter(
  value1?: number,
  value2?: number,
  higherIsBetter: boolean = true,
  metricKey: string = ''
): 1 | 2 | 'equal' {
  if (value1 == null || value2 == null) return 'equal';
  if (value1 === value2) return 'equal';
  if (isDrawdownMetric(metricKey)) {
    const abs1 = Math.abs(value1);
    const abs2 = Math.abs(value2);
    if (abs1 === abs2) return 'equal';
    return abs1 < abs2 ? 1 : 2;
  }
  if (higherIsBetter) return value1 > value2 ? 1 : 2;
  return value1 < value2 ? 1 : 2;
}

function buildMetricRow(
  metric: string,
  value1?: number,
  value2?: number,
  higherIsBetter: boolean = true,
  metricKey: string = ''
) {
  if (value1 == null || value2 == null) {
    return {
      metric,
      value1: value1 ?? null,
      value2: value2 ?? null,
      difference: null,
      percentage_diff: null,
      better: 'equal' as const,
    };
  }
  const difference = value1 - value2;
  const percentageDiff = value2 === 0 ? 0 : (difference / Math.abs(value2)) * 100;
  const better = evaluateBetter(value1, value2, higherIsBetter, metricKey);
  return {
    metric,
    value1,
    value2,
    difference,
    percentage_diff: percentageDiff,
    better,
  };
}
