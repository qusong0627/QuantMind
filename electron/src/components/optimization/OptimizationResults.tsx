import React, { useMemo } from 'react';
import { Trophy, SlidersHorizontal, TrendingUp, CheckCircle2 } from 'lucide-react';
import { OptimizationTask } from './OptimizationProgress';

interface BaselineParams {
  topk: number;
  n_drop: number;
}

interface Props {
  tasks: OptimizationTask[];
  config: { metric?: string };
  baselineParams?: BaselineParams | null;
  onApplyBestParams?: (params: { topk: number; n_drop: number }) => void;
}

export const OptimizationResults: React.FC<Props> = ({
  tasks,
  config,
  baselineParams,
  onApplyBestParams,
}) => {
  const completedTasks = useMemo(
    () => tasks.filter((task) => task.status === 'completed' && task.result),
    [tasks]
  );

  const targetMetric = config.metric || 'sharpe_ratio';

  const sortedTasks = useMemo(() => {
    return [...completedTasks].sort((a, b) => {
      const aValue = getMetricValue(a, targetMetric);
      const bValue = getMetricValue(b, targetMetric);
      return bValue - aValue;
    });
  }, [completedTasks, targetMetric]);

  const bestTask = sortedTasks[0];
  const baselineTask = useMemo(() => {
    if (!baselineParams) return null;
    return (
      completedTasks.find(
        (task) => task.topk === baselineParams.topk && task.n_drop === baselineParams.n_drop
      ) || null
    );
  }, [baselineParams, completedTasks]);

  const improvement = useMemo(() => {
    if (!bestTask || !baselineTask) return null;
    const best = getMetricValue(bestTask, targetMetric);
    const base = getMetricValue(baselineTask, targetMetric);
    if (base === 0) {
      return {
        absolute: best - base,
        percent: null as number | null,
      };
    }
    return {
      absolute: best - base,
      percent: ((best - base) / Math.abs(base)) * 100,
    };
  }, [bestTask, baselineTask, targetMetric]);

  if (!bestTask) {
    return (
      <div className="bg-white rounded-2xl border border-gray-200 p-6 text-center text-gray-500">
        暂无可展示的优化结果
      </div>
    );
  }

  return (
    <div className="bg-white rounded-2xl border border-gray-200 p-6 space-y-5">
      <div className="flex items-center justify-between">
        <h3 className="text-lg font-semibold text-gray-900 flex items-center gap-2">
          <Trophy className="w-5 h-5 text-amber-600" />
          优化结果摘要
        </h3>
        <span className="text-sm text-gray-600">已评估 {completedTasks.length} 组参数</span>
      </div>

      <div className="grid grid-cols-3 gap-4">
        <div className="rounded-2xl border border-gray-200 p-4 bg-amber-50/70">
          <div className="text-sm text-gray-600 mb-1">最优参数</div>
          <div className="text-xl font-bold text-gray-900 flex items-center gap-2">
            <SlidersHorizontal className="w-4 h-4 text-amber-700" />
            topk={bestTask.topk}, n_drop={bestTask.n_drop}
          </div>
        </div>

        <div className="rounded-2xl border border-gray-200 p-4">
          <div className="text-sm text-gray-600 mb-1">{getMetricLabel(targetMetric)}</div>
          <div className="text-xl font-bold text-indigo-700">
            {formatMetricValue(getMetricValue(bestTask, targetMetric), targetMetric)}
          </div>
        </div>

        <div className="rounded-2xl border border-gray-200 p-4">
          <div className="text-sm text-gray-600 mb-1">相对基线提升</div>
          <div className="text-xl font-bold text-rose-600 flex items-center gap-2">
            <TrendingUp className="w-4 h-4" />
            {improvement && improvement.percent !== null && improvement.percent !== undefined
              ? `${improvement.percent >= 0 ? '+' : ''}${improvement.percent.toFixed(2)}%`
              : improvement
                ? improvement.absolute.toFixed(4)
                : '暂无可比基线'}
          </div>
          <div className="text-xs text-gray-500 mt-1">
            {baselineTask
              ? `基线：topk=${baselineTask.topk}, n_drop=${baselineTask.n_drop} · ${getMetricLabel(targetMetric)} ${formatMetricValue(getMetricValue(baselineTask, targetMetric), targetMetric)}`
              : baselineParams
                ? `基线组合未命中本轮结果：topk=${baselineParams.topk}, n_drop=${baselineParams.n_drop}`
                : '未提供基线参数，无法计算相对提升'}
          </div>
        </div>
      </div>

      <div className="flex items-center justify-between rounded-2xl border border-blue-200 bg-blue-50 p-4">
        <div className="text-sm text-blue-900">
          <div className="font-semibold">一键回填参数到快速回测</div>
          <div className="mt-1">将最优 topk / n_drop 与优化区间写入回测中心配置</div>
        </div>
        <button
          type="button"
          onClick={() => onApplyBestParams?.({ topk: bestTask.topk, n_drop: bestTask.n_drop })}
          className="inline-flex items-center gap-2 px-4 py-2 bg-blue-600 text-white rounded-xl hover:bg-blue-700 transition-colors"
        >
          <CheckCircle2 className="w-4 h-4" />
          一键回填
        </button>
      </div>
    </div>
  );
};

function getMetricValue(task: OptimizationTask, metric: string): number {
  if (!task.result) return 0;
  const metricMap: Record<string, number> = {
    annual_return: task.result.annual_return,
    sharpe_ratio: task.result.sharpe_ratio,
    max_drawdown: task.result.max_drawdown,
    alpha: task.result.alpha,
    total_return: task.result.annual_return,
  };
  return metricMap[metric] ?? 0;
}

function getMetricLabel(metric: string): string {
  const labels: Record<string, string> = {
    annual_return: '年化收益率',
    sharpe_ratio: '夏普比率',
    max_drawdown: '最大回撤',
    alpha: 'Alpha',
    total_return: '总收益率',
  };
  return labels[metric] || metric;
}

function formatMetricValue(value: number, metric: string): string {
  if (metric === 'annual_return' || metric === 'max_drawdown' || metric === 'total_return') {
    return `${(value * 100).toFixed(2)}%`;
  }
  return value.toFixed(2);
}
