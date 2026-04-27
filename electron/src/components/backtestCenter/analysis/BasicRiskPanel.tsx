/**
 * 基础风险指标分析面板
 *
 * 显示基于Qlib的核心风险指标
 */

import React, { useState, useEffect } from 'react';
import { motion } from 'framer-motion';
import {
  Activity,
  TrendingUp,
  TrendingDown,
  AlertTriangle,
  Info,
  RefreshCw,
} from 'lucide-react';
import ReactECharts from 'echarts-for-react';
import {
  advancedAnalysisService,
  type BasicRiskResponse,
} from '../../../services/advancedAnalysisService';

interface BasicRiskPanelProps {
  backtestId: string;
}

export const BasicRiskPanel: React.FC<BasicRiskPanelProps> = ({ backtestId }) => {
  const [loading, setLoading] = useState(false);
  const [data, setData] = useState<BasicRiskResponse | null>(null);
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
      const result = await advancedAnalysisService.analyzeBasicRisk(backtestId);
      setData(result);
    } catch (err: any) {
      setError(err.message || '分析失败');
      console.error('加载风险分析数据失败:', err);
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

  const hasSeriesData = (series: { dates: string[]; values: number[] }) =>
    Array.isArray(series?.dates) && Array.isArray(series?.values) && series.dates.length > 0 && series.values.length > 0;
  const hasHistogramData = (series: { bins: number[]; counts: number[] }) =>
    Array.isArray(series?.bins) && Array.isArray(series?.counts) && series.bins.length > 0 && series.counts.length > 0;

  return (
    <div className="space-y-6">
      {/* Data source info */}
      <div className="bg-blue-50 border border-blue-200 rounded-2xl p-3">
        <div className="flex items-start gap-2">
          <Info className="w-4 h-4 text-blue-600 mt-0.5 flex-shrink-0" />
          <div className="text-xs text-blue-700">
            <div className="font-medium mb-1">基于Qlib risk_analysis()</div>
            <div>分析数据点: {data.data_points}个交易日 | 分析时间: {new Date(data.analyzed_at).toLocaleString()}</div>
          </div>
        </div>
      </div>

      {/* Metrics */}
      <div className="grid grid-cols-2 lg:grid-cols-5 gap-4">
        <MetricCard label="总收益率" value={data.metrics.total_return} format="percent" icon={TrendingUp} color={data.metrics.total_return >= 0 ? 'red' : 'green'} />
        <MetricCard label="年化收益率" value={data.metrics.annualized_return} format="percent" icon={TrendingUp} color={data.metrics.annualized_return >= 0 ? 'red' : 'green'} />
        <MetricCard label="年化波动率" value={data.metrics.volatility} format="percent" icon={Activity} color="orange" />
        <MetricCard label="夏普比率" value={data.metrics.sharpe_ratio} format="number" icon={TrendingUp} color="gray" decimals={2} />
        <MetricCard label="最大回撤" value={data.metrics.max_drawdown} format="percent" icon={TrendingDown} color={(data.metrics.max_drawdown || 0) <= 0 ? 'green' : 'red'} />
        <MetricCard label="Calmar比率" value={data.metrics.calmar_ratio} format="number" icon={TrendingUp} color="purple" decimals={2} />
        <MetricCard label="Sortino比率" value={data.metrics.sortino_ratio} format="number" icon={TrendingUp} color="gray" decimals={2} />
        <MetricCard label="盈利天数占比" value={data.metrics.positive_days_pct} format="percent" icon={TrendingUp} color="red" />
        <MetricCard label="最大单日涨幅" value={data.metrics.best_day_return} format="percent" icon={TrendingUp} color="red" />
        <MetricCard label="最大单日跌幅" value={data.metrics.worst_day_return} format="percent" icon={TrendingDown} color="green" />
      </div>

      {/* Charts */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Cumulative Returns Chart */}
        <div className="bg-white rounded-2xl border border-gray-200 p-4">
          <h3 className="text-sm font-medium text-gray-700 mb-4">累计收益率曲线</h3>
          {hasSeriesData(data.cumulative_returns) ? (
            <ReactECharts
              option={getCumulativeReturnsOption(data.cumulative_returns)}
              style={{ height: '300px' }}
              opts={{ renderer: 'canvas' }}
            />
          ) : (
            <EmptyChart />
          )}
        </div>

        {/* Drawdown Chart */}
        <div className="bg-white rounded-2xl border border-gray-200 p-4">
          <h3 className="text-sm font-medium text-gray-700 mb-4">回撤曲线</h3>
          {hasSeriesData(data.drawdown) ? (
            <ReactECharts
              option={getDrawdownOption(data.drawdown)}
              style={{ height: '300px' }}
              opts={{ renderer: 'canvas' }}
            />
          ) : (
            <EmptyChart />
          )}
        </div>

        {/* Daily Returns Chart */}
        <div className="bg-white rounded-2xl border border-gray-200 p-4 h-[460px] flex flex-col">
          <h3 className="text-sm font-medium text-gray-700 mb-4">每日收益率</h3>
          {hasSeriesData(data.daily_returns) ? (
            <div className="flex-1 min-h-0">
              <ReactECharts
                option={getDailyReturnsOption(data.daily_returns)}
                style={{ height: '100%' }}
                opts={{ renderer: 'canvas' }}
              />
            </div>
          ) : (
            <EmptyChart />
          )}
        </div>

        {/* Returns Distribution Chart */}
        <div className="bg-white rounded-2xl border border-gray-200 p-4 h-[460px] flex flex-col">
          <h3 className="text-sm font-medium text-gray-700 mb-4">收益率分布</h3>
          {hasHistogramData(data.returns_distribution) ? (
            <div className="flex-1 min-h-0">
              <ReactECharts
                option={getReturnsDistributionOption(data.returns_distribution)}
                style={{ height: '100%' }}
                opts={{ renderer: 'canvas' }}
              />
            </div>
          ) : (
            <EmptyChart />
          )}
        </div>
      </div>
    </div>
  );
};

// Chart option generators
function getCumulativeReturnsOption(data: { dates: string[]; values: number[] }) {
  return {
    tooltip: {
      trigger: 'axis',
      formatter: (params: any) => {
        const value = (params[0].value * 100).toFixed(2);
        return `${params[0].axisValue}<br/>累计收益: ${value}%`;
      },
    },
    dataZoom: [
      { type: 'inside', start: 0, end: 100 },
      { start: 0, end: 100, height: 16 },
    ],
    grid: {
      left: '3%',
      right: '4%',
      bottom: '16%',
      top: '10%',
      containLabel: true,
    },
    xAxis: {
      type: 'category',
      data: data.dates,
      boundaryGap: false,
      axisLabel: {
        formatter: (value: string) => {
          // Show every 30th date to avoid crowding
          const index = data.dates.indexOf(value);
          return index % 30 === 0 ? value.slice(5, 10) : '';
        },
      },
    },
    yAxis: {
      type: 'value',
      axisLabel: {
        formatter: (value: number) => `${(value * 100).toFixed(0)}%`,
      },
    },
    series: [
        {
          name: '累计收益率',
          type: 'line',
          data: data.values,
          smooth: true,
          symbol: 'none',
          lineStyle: {
          color: '#ef4444',
          width: 2,
        },
        areaStyle: {
          color: {
            type: 'linear',
            x: 0,
            y: 0,
            x2: 0,
            y2: 1,
            colorStops: [
              { offset: 0, color: 'rgba(239, 68, 68, 0.4)' },
              { offset: 1, color: 'rgba(239, 68, 68, 0.05)' },
            ],
          },
        },
      },
    ],
  };
}

function getDrawdownOption(data: { dates: string[]; values: number[] }) {
  return {
    tooltip: {
      trigger: 'axis',
      formatter: (params: any) => {
        const value = (params[0].value * 100).toFixed(2);
        return `${params[0].axisValue}<br/>回撤: ${value}%`;
      },
    },
    dataZoom: [
      { type: 'inside', start: 0, end: 100 },
      { start: 0, end: 100, height: 16 },
    ],
    grid: {
      left: '3%',
      right: '4%',
      bottom: '16%',
      top: '10%',
      containLabel: true,
    },
    xAxis: {
      type: 'category',
      data: data.dates,
      boundaryGap: false,
      axisLabel: {
        formatter: (value: string) => {
          const index = data.dates.indexOf(value);
          return index % 30 === 0 ? value.slice(5, 10) : '';
        },
      },
    },
    yAxis: {
      type: 'value',
      axisLabel: {
        formatter: (value: number) => `${(value * 100).toFixed(0)}%`,
      },
    },
    series: [
      {
        name: '回撤',
        type: 'line',
        data: data.values,
        smooth: true,
        symbol: 'none',
        lineStyle: {
          color: '#22c55e',
          width: 2,
        },
        areaStyle: {
          color: {
            type: 'linear',
            x: 0,
            y: 0,
            x2: 0,
            y2: 1,
            colorStops: [
              { offset: 0, color: 'rgba(34, 197, 94, 0.35)' },
              { offset: 1, color: 'rgba(34, 197, 94, 0.05)' },
            ],
          },
        },
      },
    ],
  };
}

function getDailyReturnsOption(data: { dates: string[]; values: number[] }) {
  return {
    tooltip: {
      trigger: 'axis',
      formatter: (params: any) => {
        const value = (params[0].value * 100).toFixed(2);
        return `${params[0].axisValue}<br/>日收益: ${value}%`;
      },
    },
    dataZoom: [
      { type: 'inside', start: 0, end: 100 },
      { start: 0, end: 100, height: 16 },
    ],
    grid: {
      left: '3%',
      right: '4%',
      bottom: '16%',
      top: '10%',
      containLabel: true,
    },
    xAxis: {
      type: 'category',
      data: data.dates,
      axisLabel: {
        formatter: (value: string) => {
          const index = data.dates.indexOf(value);
          return index % 30 === 0 ? value.slice(5, 10) : '';
        },
      },
    },
    yAxis: {
      type: 'value',
      axisLabel: {
        formatter: (value: number) => `${(value * 100).toFixed(1)}%`,
      },
    },
    series: [
      {
        name: '日收益率',
        type: 'bar',
        data: data.values.map((val) => ({
          value: val,
          itemStyle: {
            color: val >= 0 ? '#ef4444' : '#22c55e',
          },
        })),
        barMaxWidth: 3,
      },
    ],
  };
}

function getReturnsDistributionOption(data: { bins: number[]; counts: number[] }) {
  return {
    tooltip: {
      trigger: 'axis',
      formatter: (params: any) => {
        const binValue = (params[0].axisValue * 100).toFixed(1);
        return `收益率: ${binValue}%<br/>频次: ${params[0].value}`;
      },
    },
    grid: {
      left: '3%',
      right: '4%',
      bottom: '10%',
      top: '10%',
      containLabel: true,
    },
    xAxis: {
      type: 'category',
      data: data.bins,
      axisLabel: {
        formatter: (value: number) => `${(value * 100).toFixed(0)}%`,
        interval: Math.floor(data.bins.length / 10), // Show ~10 labels
      },
    },
    yAxis: {
      type: 'value',
      name: '频次',
      nameLocation: 'end',
      nameGap: 14,
      nameTextStyle: {
        color: '#6b7280',
        fontSize: 11,
        align: 'left',
        padding: [0, 0, 2, 0],
      },
    },
    series: [
      {
        name: '分布',
        type: 'bar',
        data: data.counts,
        itemStyle: {
          color: '#f59e0b',
        },
        barMaxWidth: 24,
      },
    ],
  };
}

// Metric Card Component
interface MetricCardProps {
  label: string;
  value: number;
  format: 'percent' | 'number';
  icon?: React.ComponentType<{ className?: string }>;
  color?: 'blue' | 'green' | 'emerald' | 'red' | 'rose' | 'orange' | 'purple' | 'gray';
  decimals?: number;
}

const MetricCard: React.FC<MetricCardProps> = ({
  label,
  value,
  format,
  icon: Icon,
  color = 'gray',
  decimals = 2,
}) => {
  const colorClasses = {
    blue: 'text-blue-600 bg-blue-50',
    green: 'text-green-600 bg-green-50',
    emerald: 'text-emerald-600 bg-emerald-50',
    red: 'text-red-600 bg-red-50',
    rose: 'text-rose-600 bg-rose-50',
    orange: 'text-orange-600 bg-orange-50',
    purple: 'text-purple-600 bg-purple-50',
    gray: 'text-gray-600 bg-gray-50',
  };

  const safeValue = Number.isFinite(value) ? value : 0;
  const formattedValue = format === 'percent'
    ? `${(safeValue * 100).toFixed(decimals)}%`
    : safeValue.toFixed(decimals);

  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      className="bg-white rounded-2xl border border-gray-200 p-4 hover:shadow-md transition-shadow flex flex-col items-center text-center relative overflow-hidden"
    >
      {Icon && (
        <div className={`absolute top-3 right-3 w-6 h-6 rounded-lg ${colorClasses[color]} flex items-center justify-center`}>
          <Icon className="w-3 h-3" />
        </div>
      )}
      <span className="text-xs text-gray-500 mb-1">{label}</span>
      <div className={`text-2xl font-bold ${colorClasses[color].split(' ')[0]}`}>
        {formattedValue}
      </div>
    </motion.div>
  );
};

const EmptyChart: React.FC = () => {
  return (
    <div className="h-[300px] rounded-2xl border border-dashed border-gray-200 flex items-center justify-center text-sm text-gray-400">
      暂无数据
    </div>
  );
};
