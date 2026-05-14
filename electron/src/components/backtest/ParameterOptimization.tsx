/**
 * 参数优化组件
 *
 * 功能：
 * - 配置优化参数范围
 * - 启动参数优化
 * - 实时优化进度
 * - 结果热力图
 * - 最优参数推荐
 */

import React, { useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import ReactECharts from 'echarts-for-react';
import {
  Settings,
  Play,
  Pause,
  RefreshCw,
  TrendingUp,
  Target,
  Zap,
  AlertCircle,
  CheckCircle,
  Info,
  Plus,
  Minus,
  X,
} from 'lucide-react';
import { useOptimizeParameters, useOptimizationResult } from '../../hooks/useBacktestQueries';
import type { OptimizationConfig, OptimizationResult } from '../../services/backtestService';
import { Strategy } from '../../state/atoms';

interface ParameterOptimizationProps {
  strategy: Strategy | null;
  onOptimizationComplete?: (result: OptimizationResult) => void;
}

export const ParameterOptimization: React.FC<ParameterOptimizationProps> = ({
  strategy,
  onOptimizationComplete,
}) => {
  const [config, setConfig] = useState({
    symbol: '',
    start_date: '2023-01-01',
    end_date: '2024-01-01',
    initial_capital: 100000,
    optimization_target: 'sharpe_ratio' as const,
  });

  const [paramRanges, setParamRanges] = useState<Array<{
    name: string;
    min: number;
    max: number;
    step: number;
  }>>([]);

  const [optimizationId, setOptimizationId] = useState<string | null>(null);
  const [optimizationProgress, setOptimizationProgress] = useState(0);
  const [optimizationStatus, setOptimizationStatus] = useState('');

  // Mutations and Queries
  const optimize = useOptimizeParameters();
  const { data: result, isLoading: resultLoading } = useOptimizationResult(optimizationId);

  // 添加参数
  const addParameter = () => {
    setParamRanges([
      ...paramRanges,
      { name: '', min: 0, max: 100, step: 1 },
    ]);
  };

  // 移除参数
  const removeParameter = (index: number) => {
    setParamRanges(paramRanges.filter((_, i) => i !== index));
  };

  // 更新参数
  const updateParameter = (index: number, field: string, value: any) => {
    const updated = [...paramRanges];
    updated[index] = { ...updated[index], [field]: value };
    setParamRanges(updated);
  };

  // 启动优化
  const handleStartOptimization = async () => {
    if (!strategy || !config.symbol) {
      alert('请选择策略并输入股票代码');
      return;
    }

    if (paramRanges.length === 0) {
      alert('请至少添加一个优化参数');
      return;
    }

    // 构建优化参数配置
    const param_ranges = paramRanges.map((param) => ({
      name: param.name,
      type: 'float' as const,
      min: param.min,
      max: param.max,
      step: param.step,
    }));

    const optimizationConfig: OptimizationConfig = {
      strategy_code: strategy.code,
      symbol: config.symbol!,
      start_date: config.start_date!,
      end_date: config.end_date!,
      initial_capital: config.initial_capital!,
      user_id: 'default_user',
      optimization_target: config.optimization_target,
      param_ranges,
    };

    try {
      setOptimizationProgress(0);
      setOptimizationStatus('pending');
      const result = await optimize.mutateAsync({
        config: optimizationConfig,
        progress: {
          onProgress: (progress, status) => {
            setOptimizationProgress(progress);
            if (status) {
              setOptimizationStatus(status);
            }
          },
        },
      });
      setOptimizationId(result.optimization_id);
      onOptimizationComplete?.(result);
    } catch (error) {
      console.error('优化失败:', error);
    }
  };

  return (
    <div className="bg-white rounded-2xl border border-gray-200 overflow-hidden">
      {/* 头部 */}
      <div className="p-6 border-b border-gray-200">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <Settings className="w-6 h-6 text-yellow-500" />
            <h2 className="text-xl font-bold text-gray-800">参数优化</h2>
            {strategy && (
              <span className="px-3 py-1 text-sm bg-blue-100 text-blue-600 rounded-2xl">
                {strategy.name}
              </span>
            )}
          </div>

          {result && (
            <span className="px-3 py-1 text-sm bg-green-100 text-green-600 rounded-2xl flex items-center gap-2">
              <CheckCircle className="w-4 h-4" />
              优化完成
            </span>
          )}
        </div>
      </div>

      {/* 配置区域 */}
      {!result && (
        <div className="p-6 space-y-6">
          {(optimize.isPending || optimizationStatus) && (
            <div className="bg-blue-50 border border-blue-200 rounded-2xl p-4">
              <div className="flex items-center justify-between text-sm text-blue-800 mb-2">
                <div className="flex items-center gap-2">
                  <RefreshCw className={`w-4 h-4 ${optimize.isPending ? 'animate-spin' : ''}`} />
                  <span>优化进行中</span>
                </div>
                <span>{Math.round(optimizationProgress * 100)}%</span>
              </div>
              <div className="w-full h-2 bg-blue-100 rounded-full overflow-hidden">
                <div
                  className="h-full bg-blue-500 rounded-full transition-all"
                  style={{ width: `${Math.round(optimizationProgress * 100)}%` }}
                />
              </div>
              {optimizationStatus && (
                <div className="text-xs text-blue-700 mt-2">
                  状态: {optimizationStatus}
                </div>
              )}
            </div>
          )}
          {/* 基础配置 */}
          <div>
            <h3 className="text-lg font-semibold text-gray-800 mb-4">基础配置</h3>
            <div className="grid grid-cols-2 gap-4">
              <div>
                <label className="block text-sm text-gray-600 mb-2">股票代码</label>
                <input
                  type="text"
                  placeholder="例如: 000001.SZ"
                  value={config.symbol || ''}
                  onChange={(e) => setConfig({ ...config, symbol: e.target.value })}
                  className="w-full px-4 py-2 bg-gray-50 border border-gray-200 rounded-2xl text-gray-800 placeholder-gray-500 focus:outline-none focus:border-blue-500 focus:ring-2 focus:ring-blue-500/20"
                />
              </div>

              <div>
                <label className="block text-sm text-gray-600 mb-2">优化目标</label>
                <select
                  value={config.optimization_target || 'sharpe_ratio'}
                  onChange={(e) => setConfig({ ...config, optimization_target: e.target.value as any })}
                  className="w-full px-4 py-2 bg-gray-50 border border-gray-200 rounded-2xl text-gray-800 focus:outline-none focus:border-blue-500 focus:ring-2 focus:ring-blue-500/20"
                >
                  <option value="sharpe_ratio">夏普比率</option>
                  <option value="total_return">总收益率</option>
                  <option value="sortino_ratio">索提诺比率</option>
                  <option value="calmar_ratio">卡玛比率</option>
                </select>
              </div>

              <div>
                <label className="block text-sm text-gray-600 mb-2">开始日期</label>
                <input
                  type="date"
                  value={config.start_date}
                  onChange={(e) => setConfig({ ...config, start_date: e.target.value })}
                  className="w-full px-4 py-2 bg-gray-50 border border-gray-200 rounded-2xl text-gray-800 focus:outline-none focus:border-blue-500 focus:ring-2 focus:ring-blue-500/20"
                />
              </div>

              <div>
                <label className="block text-sm text-gray-600 mb-2">结束日期</label>
                <input
                  type="date"
                  value={config.end_date}
                  onChange={(e) => setConfig({ ...config, end_date: e.target.value })}
                  className="w-full px-4 py-2 bg-gray-50 border border-gray-200 rounded-2xl text-gray-800 focus:outline-none focus:border-blue-500 focus:ring-2 focus:ring-blue-500/20"
                />
              </div>
            </div>
          </div>

          {/* 参数范围配置 */}
          <div>
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-lg font-semibold text-gray-800">优化参数</h3>
              <button
                onClick={addParameter}
                className="px-3 py-1.5 bg-blue-100 text-blue-600 rounded-2xl hover:bg-blue-200 transition-colors flex items-center gap-2"
              >
                <Plus className="w-4 h-4" />
                添加参数
              </button>
            </div>

            {paramRanges.length === 0 ? (
              <div className="flex flex-col items-center justify-center py-12 bg-gray-50 rounded-2xl border border-dashed border-gray-300">
                <Settings className="w-12 h-12 text-gray-400 mb-3 opacity-50" />
                <p className="text-gray-600">暂无优化参数</p>
                <p className="text-sm text-gray-500 mt-1">点击"添加参数"开始配置</p>
              </div>
            ) : (
              <div className="space-y-3">
                {paramRanges.map((param, index) => (
                  <ParameterRangeInput
                    key={index}
                    param={param}
                    onUpdate={(field, value) => updateParameter(index, field, value)}
                    onRemove={() => removeParameter(index)}
                  />
                ))}
              </div>
            )}
          </div>

          {/* 操作按钮 */}
          <div className="flex justify-end gap-3">
            <button
              onClick={() => {
                setConfig({
                  symbol: '',
                  start_date: '2023-01-01',
                  end_date: '2024-01-01',
                  initial_capital: 100000,
                  optimization_target: 'sharpe_ratio',
                });
                setParamRanges([]);
              }}
              className="px-6 py-2 bg-gray-100 text-gray-700 rounded-2xl hover:bg-gray-200 transition-colors"
            >
              重置
            </button>
            <button
              onClick={handleStartOptimization}
              disabled={optimize.isPending || !strategy || !config.symbol}
              className="px-6 py-2 bg-gradient-to-r from-yellow-500 to-orange-500 text-white rounded-2xl hover:from-yellow-600 hover:to-orange-600 transition-colors disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-2"
            >
              {optimize.isPending ? (
                <>
                  <RefreshCw className="w-4 h-4 animate-spin" />
                  优化中...
                </>
              ) : (
                <>
                  <Play className="w-4 h-4" />
                  开始优化
                </>
              )}
            </button>
          </div>
        </div>
      )}

      {/* 结果展示 */}
      {result && (
        <OptimizationResults
          result={result}
          onReset={() => {
            setOptimizationId(null);
            setParamRanges([]);
          }}
        />
      )}
    </div>
  );
};

// ============================================================================
// 参数范围输入
// ============================================================================

interface ParameterRangeInputProps {
  param: {
    name: string;
    min: number;
    max: number;
    step: number;
  };
  onUpdate: (field: string, value: any) => void;
  onRemove: () => void;
}

const ParameterRangeInput: React.FC<ParameterRangeInputProps> = ({
  param,
  onUpdate,
  onRemove,
}) => {
  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, x: -100 }}
      className="flex items-center gap-3 p-4 bg-gray-50 rounded-2xl border border-gray-200"
    >
      <div className="flex-1 grid grid-cols-4 gap-3">
        <input
          type="text"
          placeholder="参数名称"
          value={param.name}
          onChange={(e) => onUpdate('name', e.target.value)}
          className="px-3 py-2 bg-white border border-gray-200 rounded-2xl text-gray-800 placeholder-gray-500 focus:outline-none focus:border-blue-500 focus:ring-2 focus:ring-blue-500/20"
        />
        <input
          type="number"
          placeholder="最小值"
          value={param.min}
          onChange={(e) => onUpdate('min', parseFloat(e.target.value))}
          className="px-3 py-2 bg-white border border-gray-200 rounded-2xl text-gray-800 placeholder-gray-500 focus:outline-none focus:border-blue-500 focus:ring-2 focus:ring-blue-500/20"
        />
        <input
          type="number"
          placeholder="最大值"
          value={param.max}
          onChange={(e) => onUpdate('max', parseFloat(e.target.value))}
          className="px-3 py-2 bg-white border border-gray-200 rounded-2xl text-gray-800 placeholder-gray-500 focus:outline-none focus:border-blue-500 focus:ring-2 focus:ring-blue-500/20"
        />
        <input
          type="number"
          placeholder="步长"
          value={param.step}
          onChange={(e) => onUpdate('step', parseFloat(e.target.value))}
          className="px-3 py-2 bg-white border border-gray-200 rounded-2xl text-gray-800 placeholder-gray-500 focus:outline-none focus:border-blue-500 focus:ring-2 focus:ring-blue-500/20"
        />
      </div>
      <button
        onClick={onRemove}
        className="p-2 hover:bg-red-100 rounded-2xl text-red-600 transition-colors"
      >
        <X className="w-5 h-5" />
      </button>
    </motion.div>
  );
};

// ============================================================================
// 优化结果展示
// ============================================================================

interface OptimizationResultsProps {
  result: OptimizationResult;
  onReset: () => void;
}

const OptimizationResults: React.FC<OptimizationResultsProps> = ({ result, onReset }) => {
  return (
    <div className="p-6 space-y-6">
      {/* 最优参数 */}
      <div className="bg-gradient-to-r from-green-50 to-blue-50 rounded-2xl p-6 border border-green-200">
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-2">
            <Zap className="w-5 h-5 text-yellow-500" />
            <h3 className="text-lg font-semibold text-gray-800">最优参数组合</h3>
          </div>
          <span className="px-3 py-1 bg-green-100 text-green-600 rounded-2xl text-sm">
            适应度: {result.best_fitness?.toFixed(4) || 'N/A'}
          </span>
        </div>
        <div className="grid grid-cols-3 gap-4">
          {Object.entries(result.best_params || {}).map(([key, value]) => (
            <div key={key} className="bg-white rounded-2xl p-3 border border-gray-200">
              <div className="text-xs text-gray-600 mb-1">{key}</div>
              <div className="text-lg font-bold text-gray-800">{value}</div>
            </div>
          ))}
        </div>
      </div>

      {/* 优化统计 */}
      <div className="grid grid-cols-2 gap-4">
        <StatCard
          label="优化状态"
          value={result.status}
          icon={CheckCircle}
          color="text-green-400"
        />
        <StatCard
          label="进度"
          value={`${((result.progress || 0) * 100).toFixed(0)}%`}
          icon={RefreshCw}
          color="text-blue-400"
        />
      </div>

      {/* 代数统计 */}
      {result.generation_stats && result.generation_stats.length > 0 && (
        <GenerationStatsChart stats={result.generation_stats} />
      )}

      {/* 操作按钮 */}
      <div className="flex justify-end">
        <button
          onClick={onReset}
          className="px-6 py-2 bg-blue-100 text-blue-600 rounded-2xl hover:bg-blue-200 transition-colors"
        >
          重新优化
        </button>
      </div>
    </div>
  );
};

// ============================================================================
// 统计卡片
// ============================================================================

interface StatCardProps {
  label: string;
  value: number | string;
  icon: React.ComponentType<{ className?: string }>;
  color: string;
}

const StatCard: React.FC<StatCardProps> = ({ label, value, icon: Icon, color }) => {
  return (
    <div className="bg-gray-50 rounded-2xl p-4 border border-gray-200">
      <div className="flex items-center justify-between mb-2">
        <span className="text-sm text-gray-600">{label}</span>
        <Icon className={`w-4 h-4 ${color}`} />
      </div>
      <div className={`text-2xl font-bold ${color}`}>{value}</div>
    </div>
  );
};

// ============================================================================
// 代数统计图表
// ============================================================================

interface GenerationStatsChartProps {
  stats: Array<{
    generation: number;
    best_fitness: number;
    avg_fitness: number;
    worst_fitness: number;
  }>;
}

const GenerationStatsChart: React.FC<GenerationStatsChartProps> = ({ stats }) => {
  const option = {
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'axis',
      backgroundColor: 'rgba(0, 0, 0, 0.8)',
      borderColor: 'rgba(255, 255, 255, 0.1)',
      textStyle: { color: '#fff' },
    },
    legend: {
      data: ['最佳适应度', '平均适应度', '最差适应度'],
      textStyle: { color: '#999' },
      top: 0,
    },
    grid: {
      left: '3%',
      right: '4%',
      bottom: '3%',
      top: '10%',
      containLabel: true,
    },
    xAxis: {
      type: 'category',
      data: stats.map((s) => `第${s.generation}代`),
      axisLine: { lineStyle: { color: '#333' } },
      axisLabel: { color: '#999' },
    },
    yAxis: {
      type: 'value',
      axisLine: { lineStyle: { color: '#333' } },
      axisLabel: { color: '#999' },
      splitLine: { lineStyle: { color: '#222' } },
    },
    series: [
      {
        name: '最佳适应度',
        type: 'line',
        data: stats.map((s) => s.best_fitness),
        smooth: true,
        lineStyle: { width: 2, color: '#10b981' },
      },
      {
        name: '平均适应度',
        type: 'line',
        data: stats.map((s) => s.avg_fitness),
        smooth: true,
        lineStyle: { width: 2, color: '#3b82f6' },
      },
      {
        name: '最差适应度',
        type: 'line',
        data: stats.map((s) => s.worst_fitness),
        smooth: true,
        lineStyle: { width: 2, color: '#ef4444' },
      },
    ],
  };

  return (
    <div className="bg-gray-50 rounded-2xl p-4 border border-gray-200">
      <h3 className="text-lg font-semibold text-gray-800 mb-4">优化进度</h3>
      <ReactECharts option={option} style={{ height: '300px' }} />
    </div>
  );
};

// ============================================================================
// 热力图（已移除，后端未实现）
// ============================================================================

// ============================================================================
// 敏感性分析图（已移除，后端未实现）
// ============================================================================
