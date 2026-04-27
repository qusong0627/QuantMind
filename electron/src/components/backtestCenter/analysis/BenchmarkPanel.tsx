/**
 * 基准对比面板
 */

import React, { useEffect, useState } from 'react';
import { AlertTriangle, Info, RefreshCw, TrendingUp } from 'lucide-react';
import ReactECharts from 'echarts-for-react';
import {
  advancedAnalysisService,
  type BenchmarkComparisonResponse,
} from '../../../services/advancedAnalysisService';

interface BenchmarkPanelProps {
  backtestId: string;
}

const BENCHMARK_OPTIONS = [
  { id: 'SH000300', name: '沪深300' },
  { id: 'SH000905', name: '中证500' },
  { id: 'SH000852', name: '中证1000' },
];

export const BenchmarkPanel: React.FC<BenchmarkPanelProps> = ({ backtestId }) => {
  const [benchmarkId, setBenchmarkId] = useState<string>(BENCHMARK_OPTIONS[0].id);
  const [loading, setLoading] = useState(false);
  const [data, setData] = useState<BenchmarkComparisonResponse | null>(null);
  const [error, setError] = useState<string>('');

  useEffect(() => {
    if (backtestId) {
      loadData();
    }
  }, [backtestId, benchmarkId]);

  const loadData = async () => {
    setLoading(true);
    setError('');
    try {
      const result = await advancedAnalysisService.compareBenchmark(backtestId, benchmarkId);
      setData(result);
    } catch (err: unknown) {
      setError(extractErrorMessage(err));
      console.error('加载基准对比失败:', err);
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
      label: '超额收益',
      value: data.metrics.excess_return,
      format: 'percent' as const,
      icon: TrendingUp,
      useMarketColor: true,
    },
    {
      label: 'Beta',
      value: data.metrics.beta,
      format: 'number' as const,
      icon: TrendingUp,
    },
    {
      label: 'Alpha',
      value: data.metrics.alpha,
      format: 'percent' as const,
      icon: TrendingUp,
      useMarketColor: true,
    },
  ];

  return (
    <div className="space-y-6">
      <div className="bg-purple-50 border border-purple-200 rounded-2xl p-4">
        <div className="flex items-start gap-2">
          <Info className="w-4 h-4 text-purple-600 mt-0.5 flex-shrink-0" />
          <div className="text-xs text-purple-700">
            <div className="font-medium mb-1">基准对比</div>
            <div>选择基准指数，对比收益与风险暴露。</div>
          </div>
        </div>
      </div>

      <div className="bg-white rounded-2xl border border-gray-200 p-4">
        <label className="text-xs font-medium text-gray-600">基准指数</label>
        <div className="mt-2 flex flex-wrap gap-2">
          {BENCHMARK_OPTIONS.map((option) => (
            <button
              key={option.id}
              onClick={() => setBenchmarkId(option.id)}
              className={`px-3 py-1 rounded-full text-xs border transition-colors ${benchmarkId === option.id
                ? 'bg-purple-600 text-white border-purple-600'
                : 'bg-white text-gray-600 border-gray-200 hover:border-purple-300'
                }`}
            >
              {option.name}
            </button>
          ))}
        </div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        {metrics.map((metric) => (
          <MetricCard key={metric.label} {...metric} />
        ))}
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <ChartCard title="策略 vs 基准累计收益">
          <ReactECharts
            option={getComparisonOption(data)}
            style={{ height: '320px' }}
            opts={{ renderer: 'canvas' }}
          />
        </ChartCard>
        <ChartCard title="超额收益曲线">
          <ReactECharts
            option={getExcessOption(data)}
            style={{ height: '320px' }}
            opts={{ renderer: 'canvas' }}
          />
        </ChartCard>
      </div>

      <div className="grid grid-cols-1 gap-6">
        <ChartCard title="上行/下行捕获">
          <ReactECharts
            option={getCaptureOption(data)}
            style={{ height: '320px' }}
            opts={{ renderer: 'canvas' }}
          />
        </ChartCard>
      </div>
    </div>
  );
};

const ChartCard: React.FC<{ title: string; children: React.ReactNode }> = ({ title, children }) => {
  return (
    <div className="bg-white rounded-2xl border border-gray-200 p-4">
      <h3 className="text-sm font-medium text-gray-700 mb-4">{title}</h3>
      {children}
    </div>
  );
};

function getComparisonOption(data: BenchmarkComparisonResponse) {
  return {
    color: ['#3b82f6', '#f97316'],
    tooltip: {
      trigger: 'axis',
      formatter: (params: any[]) => {
        let result = `${params[0].axisValue}<br/>`;
        params.forEach(item => {
          result += `${item.marker}${item.seriesName}: ${(item.value * 100).toFixed(2)}%<br/>`;
        });
        return result;
      }
    },
    dataZoom: [
      { type: 'inside', start: 0, end: 100 },
      { start: 0, end: 100, height: 16, bottom: '8%' },
    ],
    grid: { left: '6%', right: '6%', bottom: '26%', top: '12%', containLabel: true },
    legend: { data: ['策略', '基准'], bottom: '0%', left: 'center' },
    xAxis: {
      type: 'category',
      data: data.strategy_returns.dates,
      axisLabel: { formatter: (value: string) => value.slice(5, 10) },
    },
    yAxis: {
      type: 'value',
      axisLabel: { formatter: (value: number) => `${(value * 100).toFixed(2)}%` },
    },
    series: [
      {
        name: '策略',
        type: 'line',
        data: data.strategy_returns.values,
        smooth: true,
        symbol: 'none',
        lineStyle: { width: 2 },
      },
      {
        name: '基准',
        type: 'line',
        data: data.benchmark_returns.values,
        smooth: true,
        symbol: 'none',
        lineStyle: { width: 2 },
      },
    ],
  };
}

function getExcessOption(data: BenchmarkComparisonResponse) {
  return {
    color: ['#10b981'],
    tooltip: {
      trigger: 'axis',
      formatter: (params: any[]) => {
        return `${params[0].axisValue}<br/>${params[0].marker}${params[0].seriesName}: ${(params[0].value * 100).toFixed(2)}%`;
      }
    },
    dataZoom: [
      { type: 'inside', start: 0, end: 100 },
      { start: 0, end: 100, height: 16 },
    ],
    grid: { left: '6%', right: '6%', bottom: '16%', top: '10%', containLabel: true },
    xAxis: {
      type: 'category',
      data: data.excess_returns.dates,
      axisLabel: { formatter: (value: string) => value.slice(5, 10) },
    },
    yAxis: {
      type: 'value',
      axisLabel: { formatter: (value: number) => `${(value * 100).toFixed(2)}%` },
    },
    series: [
      {
        name: '超额收益',
        type: 'line',
        data: data.excess_returns.values,
        smooth: true,
        symbol: 'none',
        lineStyle: { width: 2 },
        areaStyle: { color: 'rgba(16, 185, 129, 0.2)' },
      },
    ],
  };
}

function getCaptureOption(data: BenchmarkComparisonResponse) {
  return {
    tooltip: {
      trigger: 'axis',
      formatter: (params: Array<{ axisValue: string; value: number }>) => `${params[0].axisValue}<br/>比例: ${(params[0].value * 100).toFixed(2)}%`,
    },
    grid: { left: '6%', right: '6%', bottom: '10%', top: '10%', containLabel: true },
    xAxis: { type: 'category', data: ['上行捕获', '下行捕获'] },
    yAxis: {
      type: 'value',
      axisLabel: { formatter: (value: number) => `${(value * 100).toFixed(0)}%` }
    },
    series: [
      {
        name: '捕获比',
        type: 'bar',
        data: [data.metrics.upside_capture, data.metrics.downside_capture],
        itemStyle: {
          color: (params: { dataIndex: number }) => (params.dataIndex === 0 ? '#6366f1' : '#f59e0b'),
        },
        barMaxWidth: 40,
      },
    ],
  };
}

interface MetricCardProps {
  label: string;
  value: number | null;
  format: 'percent' | 'number';
  icon?: React.ComponentType<{ className?: string }>;
  useMarketColor?: boolean;
  decimals?: number;
}

const MetricCard: React.FC<MetricCardProps> = ({
  label,
  value,
  format,
  icon: Icon,
  useMarketColor = false,
  decimals = 2,
}) => {
  const hasFiniteValue = value != null && Number.isFinite(value);
  const marketColor = hasFiniteValue
    ? value > 0
      ? 'text-red-600 bg-red-50'
      : value < 0
        ? 'text-green-600 bg-green-50'
        : 'text-gray-600 bg-gray-50'
    : 'text-gray-400 bg-gray-50';
  const neutralColor = 'text-orange-600 bg-orange-50';
  const appliedColor = hasFiniteValue ? (useMarketColor ? marketColor : neutralColor) : marketColor;

  const formattedValue = !hasFiniteValue
    ? '-'
    : format === 'percent'
      ? `${(value * 100).toFixed(decimals)}%`
      : value.toFixed(decimals);

  return (
    <div className="bg-white rounded-2xl border border-gray-200 p-4 hover:shadow-md transition-shadow flex flex-col items-center text-center relative overflow-hidden">
      {Icon && (
        <div className={`absolute top-3 right-3 w-6 h-6 rounded-lg ${appliedColor} flex items-center justify-center`}>
          <Icon className="w-3 h-3" />
        </div>
      )}
      <span className="text-xs text-gray-500 mb-1">{label}</span>
      <div className={`text-2xl font-bold ${appliedColor.split(' ')[0]}`}>
        {formattedValue}
      </div>
    </div>
  );
};

function extractErrorMessage(error: unknown): string {
  const err = error as {
    message?: string;
    response?: { data?: { detail?: string } };
  };
  return err?.response?.data?.detail || err?.message || '分析失败';
}
