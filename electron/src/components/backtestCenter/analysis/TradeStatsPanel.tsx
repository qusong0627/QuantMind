/**
 * 交易统计面板
 */

import React, { useEffect, useState } from 'react';
import { AlertTriangle, BarChart3, Info, RefreshCw, TrendingUp } from 'lucide-react';
import ReactECharts from 'echarts-for-react';
import {
  advancedAnalysisService,
  type TradeStatsResponse,
} from '../../../services/advancedAnalysisService';

interface TradeStatsPanelProps {
  backtestId: string;
}

export const TradeStatsPanel: React.FC<TradeStatsPanelProps> = ({ backtestId }) => {
  const [loading, setLoading] = useState(false);
  const [data, setData] = useState<TradeStatsResponse | null>(null);
  const [error, setError] = useState<string>('');

  useEffect(() => {
    if (backtestId) {
      loadData();
    }
  }, [backtestId]);

  const loadData = async () => {
    setLoading(true);
    setError('');
    try {
      const result = await advancedAnalysisService.analyzeTradeStats(backtestId);
      setData(result);
    } catch (err: unknown) {
      setError(extractErrorMessage(err));
      console.error('加载交易统计失败:', err);
    } finally {
      setLoading(false);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="text-center">
          <RefreshCw className="w-8 h-8 text-blue-500 animate-spin mx-auto mb-2" />
          <p className="text-sm text-gray-600">正在分析...</p>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="bg-red-50 border border-red-200 rounded-2xl p-4">
        <div className="flex items-start gap-2">
          <AlertTriangle className="w-5 h-5 text-red-600 mt-0.5" />
          <div>
            <div className="font-medium text-red-800">分析失败</div>
            <div className="text-sm text-red-700 mt-1">{error}</div>
            <button
              onClick={loadData}
              className="mt-2 text-sm text-red-600 hover:text-red-700 underline"
            >
              重试
            </button>
          </div>
        </div>
      </div>
    );
  }

  if (!data) {
    return (
      <div className="text-center text-gray-500 py-12">
        <Info className="w-12 h-12 mx-auto mb-2 opacity-50" />
        <p>选择回测结果后开始分析</p>
      </div>
    );
  }

  const metrics = [
    {
      label: '交易日盈亏占比',
      value: data.metrics.win_rate,
      format: 'percent' as const,
      icon: TrendingUp,
      color: 'red' as const,
    },
    {
      label: '盈亏天数比',
      value: data.metrics.profit_loss_days_ratio,
      format: 'number' as const,
      icon: BarChart3,
      color: 'red' as const,
    },
    {
      label: '平均持仓天数',
      value: data.metrics.avg_holding_days,
      format: 'number' as const,
      icon: BarChart3,
      color: 'orange' as const,
      decimals: 2,
      suffix: '天',
    },
    {
      label: '交易频率',
      value: data.metrics.trade_frequency,
      format: 'number' as const,
      icon: TrendingUp,
      color: 'purple' as const,
      decimals: 2,
      suffix: '/月',
    },
    {
      label: '交易次数',
      value: data.metrics.total_trades,
      format: 'number' as const,
      icon: BarChart3,
      color: 'gray' as const,
      decimals: 0,
      suffix: '次',
    },
  ];

  const useHoldingFallback = !hasHistogramData(data.holding_days_distribution);

  return (
    <div className="space-y-6">
      <div className="bg-green-50 border border-green-200 rounded-2xl p-4">
        <div className="flex items-start gap-2">
          <Info className="w-4 h-4 text-green-600 mt-0.5 flex-shrink-0" />
          <div className="text-xs text-green-700">
            <div className="font-medium mb-1">交易统计</div>
            <div>按交易日胜率口径，刻画交易风格与执行节奏。</div>
          </div>
        </div>
      </div>

      <div className="grid grid-cols-2 lg:grid-cols-5 gap-4">
        {metrics.map((metric) => (
          <MetricCard key={metric.label} {...metric} />
        ))}
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <ChartCard title="单笔盈亏分布">
          {hasHistogramData(data.pnl_distribution) ? (
            <ReactECharts
              option={getPnlDistributionOption(data.pnl_distribution)}
              style={{ height: '320px' }}
              opts={{ renderer: 'canvas' }}
            />
          ) : (
            <EmptyChart />
          )}
        </ChartCard>

        <ChartCard title={useHoldingFallback ? '月度交易次数分布（替代）' : '持仓天数分布'}>
          {!useHoldingFallback && hasHistogramData(data.holding_days_distribution) ? (
            <ReactECharts
              option={getHoldingDaysOption(data.holding_days_distribution)}
              style={{ height: '320px' }}
              opts={{ renderer: 'canvas' }}
            />
          ) : hasSeriesData(data.trade_frequency_series) ? (
            <ReactECharts
              option={getTradeFrequencyBarOption(data.trade_frequency_series)}
              style={{ height: '320px' }}
              opts={{ renderer: 'canvas' }}
            />
          ) : (
            <EmptyChart />
          )}
        </ChartCard>
      </div>

      <div className="grid grid-cols-1 gap-6">
        <ChartCard title="交易频率趋势">
          {hasSeriesData(data.trade_frequency_series) ? (
            <ReactECharts
              option={getTradeFrequencyOption(data.trade_frequency_series)}
              style={{ height: '320px' }}
              opts={{ renderer: 'canvas' }}
            />
          ) : (
            <EmptyChart />
          )}
        </ChartCard>
      </div>
    </div>
  );
};

const hasHistogramData = (series: { bins: number[]; counts: number[] }) =>
  Array.isArray(series?.bins) && Array.isArray(series?.counts) && series.bins.length > 1 && series.counts.length > 0;

const hasSeriesData = (series: { dates: string[]; values: number[] }) =>
  Array.isArray(series?.dates) && Array.isArray(series?.values) && series.dates.length > 0 && series.values.length > 0;

const ChartCard: React.FC<{ title: string; children: React.ReactNode }> = ({ title, children }) => {
  return (
    <div className="bg-white rounded-2xl border border-gray-200 p-4">
      <h3 className="text-sm font-medium text-gray-700 mb-4">{title}</h3>
      {children}
    </div>
  );
};

function getPnlDistributionOption(data: { bins: number[]; counts: number[] }) {
  const labels = buildHistogramLabels(data.bins, (value) => `${(value * 100).toFixed(1)}%`);
  return {
    tooltip: {
      trigger: 'axis',
      formatter: (params: Array<{ axisValue: string; value: number }>) => `${params[0].axisValue}<br/>频次: ${params[0].value}`,
    },
    grid: { left: '6%', right: '6%', bottom: '14%', top: '10%', containLabel: true },
    xAxis: { type: 'category', data: labels, axisLabel: { interval: 'auto' } },
    yAxis: { type: 'value', name: '频次' },
    series: [
      {
        name: '盈亏分布',
        type: 'bar',
        data: data.counts,
        itemStyle: { color: '#10b981' },
        barMaxWidth: 24,
      },
    ],
  };
}

function getHoldingDaysOption(data: { bins: number[]; counts: number[] }) {
  const normalized = normalizeHoldingDistribution(data);
  const labels = buildHoldingPeriodLabels();
  const total = normalized.reduce((sum, value) => sum + value, 0);
  return {
    tooltip: {
      trigger: 'axis',
      formatter: (params: Array<{ axisValue: string; value: number }>) => {
        const count = Number(params[0].value) || 0;
        const pct = total > 0 ? ((count / total) * 100).toFixed(1) : '0.0';
        return `${params[0].axisValue}<br/>频次: ${count}<br/>占比: ${pct}%`;
      },
    },
    grid: { left: '6%', right: '6%', bottom: '14%', top: '10%', containLabel: true },
    xAxis: {
      type: 'category',
      data: labels,
      axisLabel: { interval: 0, rotate: 20 },
    },
    yAxis: { type: 'value', name: '频次' },
    series: [
      {
        name: '持仓天数',
        type: 'bar',
        data: normalized,
        itemStyle: { color: '#60a5fa' },
        barMaxWidth: 24,
        label: {
          show: true,
          position: 'top',
          formatter: ({ value }: { value: number }) => {
            const pct = total > 0 ? ((Number(value) / total) * 100).toFixed(1) : '0.0';
            return `${value} (${pct}%)`;
          },
          fontSize: 11,
          color: '#475569',
        },
      },
    ],
  };
}

function getTradeFrequencyBarOption(series: { dates: string[]; values: number[] }) {
  return {
    tooltip: {
      trigger: 'axis',
      formatter: (params: Array<{ axisValue: string; value: number }>) => `${params[0].axisValue}<br/>交易次数: ${params[0].value}`,
    },
    grid: { left: '6%', right: '6%', bottom: '14%', top: '10%', containLabel: true },
    xAxis: {
      type: 'category',
      data: series.dates,
      axisLabel: {
        formatter: (value: string) => value.slice(2, 7),
      },
    },
    yAxis: { type: 'value', name: '次数' },
    series: [
      {
        name: '月度交易次数',
        type: 'bar',
        data: series.values,
        itemStyle: { color: '#60a5fa' },
        barMaxWidth: 36,
      },
    ],
  };
}

function getTradeFrequencyOption(series: { dates: string[]; values: number[] }) {
  return {
    tooltip: {
      trigger: 'axis',
      formatter: (params: Array<{ axisValue: string; value: number }>) => `${params[0].axisValue}<br/>交易次数: ${params[0].value}`,
    },
    dataZoom: [
      { type: 'inside', start: 0, end: 100 },
      { start: 0, end: 100, height: 16 },
    ],
    grid: { left: '6%', right: '6%', bottom: '16%', top: '10%', containLabel: true },
    xAxis: {
      type: 'category',
      data: series.dates,
      axisLabel: {
        formatter: (value: string) => value.slice(2, 7),
      },
    },
    yAxis: { type: 'value', name: '次数' },
    series: [
      {
        name: '交易频率',
        type: 'line',
        data: series.values,
        smooth: true,
        symbol: 'none',
        lineStyle: { color: '#f97316', width: 2 },
        areaStyle: { color: 'rgba(249, 115, 22, 0.2)' },
      },
    ],
  };
}

function buildHistogramLabels(bins: number[], formatter: (value: number) => string) {
  const labels: string[] = [];
  for (let i = 0; i < bins.length - 1; i += 1) {
    const mid = (bins[i] + bins[i + 1]) / 2;
    labels.push(formatter(mid));
  }
  return labels;
}

function buildHoldingPeriodLabels() {
  return ['1-7天', '7-30天', '30-90天', '90-180天', '180-365天'];
}

function normalizeHoldingDistribution(data: { bins: number[]; counts: number[] }) {
  const target = [0, 0, 0, 0, 0];
  const bins = Array.isArray(data?.bins) ? data.bins : [];
  const counts = Array.isArray(data?.counts) ? data.counts : [];
  if (!counts.length) return target;

  // 后端已是标准 5 桶时直接使用
  if (counts.length === 5) {
    return counts.map((v) => Number(v) || 0);
  }

  // 兼容历史自动分箱：按每个箱体中点重新映射
  for (let i = 0; i < counts.length; i += 1) {
    const count = Number(counts[i]) || 0;
    const left = Number(bins[i]);
    const right = Number(bins[i + 1]);
    const mid = Number.isFinite(left) && Number.isFinite(right)
      ? (left + right) / 2
      : Number.isFinite(left)
        ? left
        : i + 1;
    const idx = holdingBucketIndex(mid);
    if (idx >= 0) target[idx] += count;
  }
  return target;
}

function holdingBucketIndex(days: number) {
  if (days <= 1) return -1; // 忽略
  if (days <= 7) return 0;
  if (days <= 30) return 1;
  if (days <= 90) return 2;
  if (days <= 180) return 3;
  return 4;
}

function extractErrorMessage(error: unknown): string {
  const err = error as {
    message?: string;
    response?: { data?: { detail?: string } };
  };
  return err?.response?.data?.detail || err?.message || '分析失败';
}

interface MetricCardProps {
  label: string;
  value: number;
  format: 'percent' | 'number';
  icon?: React.ComponentType<{ className?: string }>;
  color?: 'blue' | 'green' | 'red' | 'orange' | 'purple' | 'gray';
  decimals?: number;
  suffix?: string;
}

const MetricCard: React.FC<MetricCardProps> = ({
  label,
  value,
  format,
  icon: Icon,
  color = 'gray',
  decimals = 2,
  suffix = '',
}) => {
  const colorClasses = {
    blue: 'text-blue-600 bg-blue-50',
    green: 'text-green-600 bg-green-50',
    red: 'text-red-600 bg-red-50',
    orange: 'text-orange-600 bg-orange-50',
    purple: 'text-purple-600 bg-purple-50',
    gray: 'text-gray-600 bg-gray-50',
  };

  const formattedValue = format === 'percent'
    ? `${(value * 100).toFixed(decimals)}%`
    : value.toFixed(decimals);

  return (
    <div className="bg-white rounded-2xl border border-gray-200 p-4 hover:shadow-md transition-shadow flex flex-col items-center text-center relative overflow-hidden">
      {Icon && (
        <div className={`absolute top-3 right-3 w-6 h-6 rounded-lg ${colorClasses[color]} flex items-center justify-center`}>
          <Icon className="w-3 h-3" />
        </div>
      )}
      <span className="text-xs text-gray-500 mb-1">{label}</span>
      <div className={`text-2xl font-bold ${colorClasses[color].split(' ')[0]}`}>
        {formattedValue}{suffix}
      </div>
    </div>
  );
};

const EmptyChart: React.FC = () => {
  return (
    <div className="h-[320px] rounded-2xl border border-dashed border-gray-200 flex items-center justify-center text-sm text-gray-400">
      暂无数据
    </div>
  );
};
