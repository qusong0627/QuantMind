import React from 'react';
import { Loader2, CheckCircle, XCircle, Clock } from 'lucide-react';

export interface OptimizationTask {
  id: string;
  topk: number;
  n_drop: number;
  status: 'pending' | 'running' | 'completed' | 'failed';
  result?: {
    annual_return: number;
    sharpe_ratio: number;
    max_drawdown: number;
    alpha: number;
  };
  error?: string;
  startTime?: number;
  endTime?: number;
}

interface Props {
  tasks: OptimizationTask[];
  processedCount: number;
  totalTasks: number;
  completedCount: number;
  failedCount: number;
  remainingCount: number;
  progress: number;
}

export const OptimizationProgress: React.FC<Props> = ({
  tasks,
  processedCount,
  totalTasks,
  completedCount,
  failedCount,
  remainingCount,
  progress,
}) => {
  const getStatusIcon = (status: OptimizationTask['status']) => {
    switch (status) {
      case 'running':
        return <Loader2 className="w-4 h-4 text-blue-600 animate-spin" />;
      case 'completed':
        return <CheckCircle className="w-4 h-4 text-green-600" />;
      case 'failed':
        return <XCircle className="w-4 h-4 text-red-600" />;
      case 'pending':
        return <Clock className="w-4 h-4 text-gray-400" />;
    }
  };

  const getStatusColor = (status: OptimizationTask['status']) => {
    switch (status) {
      case 'running':
        return 'bg-blue-50 border-blue-200';
      case 'completed':
        return 'bg-green-50 border-green-200';
      case 'failed':
        return 'bg-red-50 border-red-200';
      case 'pending':
        return 'bg-gray-50 border-gray-200';
    }
  };

  return (
    <div className="bg-white rounded-lg shadow p-6 mb-6">
      <h3 className="text-lg font-semibold mb-4">优化进度</h3>

      {/* 总体进度条 */}
      <div className="mb-6">
        <div className="flex justify-between text-sm text-gray-600 mb-2">
          <span>总进度: {processedCount} / {totalTasks}</span>
          <span>{(progress * 100).toFixed(1)}%</span>
        </div>
        <div className="w-full bg-gray-200 rounded-full h-3 overflow-hidden">
          <div
            className="h-full bg-gradient-to-r from-blue-500 to-indigo-600 transition-all duration-300 ease-out"
            style={{ width: `${progress * 100}%` }}
          />
        </div>
      </div>

      {/* 统计卡片 */}
      <div className="grid grid-cols-3 gap-4 mb-6">
        <div className="bg-green-50 border border-green-200 rounded-lg p-4 text-center">
          <div className="text-2xl font-bold text-green-900">{completedCount}</div>
          <div className="text-xs text-green-700">已完成</div>
        </div>
        <div className="bg-blue-50 border border-blue-200 rounded-lg p-4 text-center">
          <div className="text-2xl font-bold text-blue-900">
            {remainingCount}
          </div>
          <div className="text-xs text-blue-700">待处理</div>
        </div>
        <div className="bg-red-50 border border-red-200 rounded-lg p-4 text-center">
          <div className="text-2xl font-bold text-red-900">{failedCount}</div>
          <div className="text-xs text-red-700">失败</div>
        </div>
      </div>

      {/* 任务列表 (最多显示10个最近的) */}
      <div className="space-y-2 max-h-96 overflow-y-auto">
        {tasks.slice(0, 10).map((task) => (
          <div
            key={task.id}
            className={`p-3 border rounded-md ${getStatusColor(task.status)} transition-all`}
          >
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-3">
                {getStatusIcon(task.status)}
                <span className="text-sm font-medium text-gray-900">
                  topk={task.topk}, n_drop={task.n_drop}
                </span>
              </div>

              {task.result && task.status === 'completed' && (
                <div className="flex gap-4 text-xs text-gray-600">
                  <span>收益: {(task.result.annual_return * 100).toFixed(2)}%</span>
                  <span>夏普: {task.result.sharpe_ratio.toFixed(2)}</span>
                  <span>回撤: {(task.result.max_drawdown * 100).toFixed(2)}%</span>
                </div>
              )}

              {task.error && task.status === 'failed' && (
                <span className="text-xs text-red-600">{task.error}</span>
              )}
            </div>

            {task.startTime && task.endTime && (
              <div className="mt-1 text-xs text-gray-500">
                耗时: {((task.endTime - task.startTime) / 1000).toFixed(1)}s
              </div>
            )}
          </div>
        ))}
      </div>

      {tasks.length > 10 && (
        <div className="mt-3 text-center text-sm text-gray-500">
          显示最近 10 条，共 {tasks.length} 条任务
        </div>
      )}
    </div>
  );
};
