import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AlertCircle, ChevronLeft, ChevronRight, Clock3, RefreshCw } from 'lucide-react';

import {
  backtestService,
  OptimizationHistoryDetail,
  OptimizationHistoryItem,
  OptimizationRunStatus,
} from '../../services/backtestService';
import { authService } from '../../features/auth/services/authService';
import { useBacktestCenterStore } from '../../stores/backtestCenterStore';
import { OptimizationProgress, OptimizationTask } from './OptimizationProgress';
import { OptimizationResults } from './OptimizationResults';
import { ParameterGrid, GridSearchConfig } from './ParameterGrid';

type GridMetrics = {
  annual_return?: number;
  sharpe_ratio?: number;
  max_drawdown?: number;
  alpha?: number;
};

type OptimizationDisplaySummary = {
  totalTasks: number;
  processedCount: number;
  completedCount: number;
  failedCount: number;
  remainingCount: number;
  progress: number;
};

function getQueueAhead(resultSummary?: Record<string, any> | null): number | null {
  const rawValue = resultSummary?.queue_ahead;
  const queueAhead = Number(rawValue);
  return Number.isFinite(queueAhead) && queueAhead >= 0 ? queueAhead : null;
}

function getQueuedHint(queueAhead: number | null): string {
  if (queueAhead === null) {
    return '该任务正在等待参数优化执行槽位，轮到后会自动开始。';
  }
  if (queueAhead <= 0) {
    return '当前前方没有其他排队任务，待执行槽位释放后将立即开始。';
  }
  return `该任务正在排队，前方还有 ${queueAhead} 个任务。`;
}

const HISTORY_LIMIT = 20;

function buildTasksFromGridConfig(gridConfig: GridSearchConfig): OptimizationTask[] {
  const tasks: OptimizationTask[] = [];
  const { topk, n_drop } = gridConfig.parameters;
  topk.values!.forEach((topkValue) => {
    n_drop.values!.forEach((nDropValue) => {
      tasks.push({
        id: `task_${topkValue}_${nDropValue}`,
        topk: topkValue,
        n_drop: nDropValue,
        status: 'pending',
      });
    });
  });
  return tasks;
}

function taskKey(params?: Record<string, any> | null): string | null {
  if (!params) return null;
  const topk = Number(params.topk);
  const nDrop = Number(params.n_drop);
  if (Number.isNaN(topk) || Number.isNaN(nDrop)) return null;
  return `${topk}-${nDrop}`;
}

function mapMetrics(metrics?: GridMetrics): OptimizationTask['result'] | undefined {
  if (!metrics) return undefined;
  return {
    annual_return: metrics.annual_return || 0,
    sharpe_ratio: metrics.sharpe_ratio || 0,
    max_drawdown: metrics.max_drawdown || 0,
    alpha: metrics.alpha || 0,
  };
}

function buildTasksFromDetail(
  detail: OptimizationHistoryDetail,
  baseTasks: OptimizationTask[]
): OptimizationTask[] {
  const resultMap = new Map<string, any>();
  const failedSet = new Set<string>();

  detail.all_results.forEach((result) => {
    const key = taskKey(result.params);
    if (key) resultMap.set(key, result);
  });

  const failedTasks = Array.isArray(detail.result_summary?.failed_tasks)
    ? detail.result_summary.failed_tasks
    : [];
  failedTasks.forEach((item: any) => {
    const key = taskKey(item?.params);
    if (key) failedSet.add(key);
  });

  const currentKey = taskKey(detail.current_params);
  const completedCount = Math.max(0, detail.completed_count || 0);
  let completedSeen = 0;

  return baseTasks.map((task) => {
    const key = `${task.topk}-${task.n_drop}`;
    const completedResult = resultMap.get(key);
    if (completedResult) {
      return {
        ...task,
        status: 'completed',
        result: mapMetrics(completedResult.metrics),
      };
    }
    if (failedSet.has(key)) {
      return {
        ...task,
        status: 'failed',
        error: '该参数组合执行失败',
      };
    }
    if (currentKey === key && detail.status === 'running') {
      return {
        ...task,
        status: 'running',
      };
    }
    if (completedSeen < completedCount) {
      completedSeen += 1;
      return {
        ...task,
        status: 'completed',
      };
    }
    return {
      ...task,
      status: 'pending',
    };
  });
}

function deriveGridConfigFromHistory(detail: OptimizationHistoryDetail): GridSearchConfig | null {
  const snapshot = detail.config_snapshot || {};
  const baseRequest = detail.base_request || {};
  const topkRange = detail.param_ranges.find((item) => item.name === 'topk');
  const nDropRange = detail.param_ranges.find((item) => item.name === 'n_drop');
  if (!topkRange || !nDropRange) return null;
  const topkValues: number[] = [];
  for (let current = Number(topkRange.min); current <= Number(topkRange.max); current += Number(topkRange.step)) {
    topkValues.push(current);
  }
  const nDropValues: number[] = [];
  for (let current = Number(nDropRange.min); current <= Number(nDropRange.max); current += Number(nDropRange.step)) {
    nDropValues.push(current);
  }
  return {
    strategy: 'TopkDropoutStrategy',
    parameters: {
      topk: {
        name: 'topk',
        min: Number(topkRange.min),
        max: Number(topkRange.max),
        step: Number(topkRange.step),
        values: topkValues,
      },
      n_drop: {
        name: 'n_drop',
        min: Number(nDropRange.min),
        max: Number(nDropRange.max),
        step: Number(nDropRange.step),
        values: nDropValues,
      },
    },
    metric: (detail.optimization_target as GridSearchConfig['metric']) || 'sharpe_ratio',
    initialCapital: Number(baseRequest.initial_capital || snapshot.base_request?.initial_capital || 1000000),
    dateRange: {
      startDate: String(baseRequest.start_date || snapshot.base_request?.start_date || ''),
      endDate: String(baseRequest.end_date || snapshot.base_request?.end_date || ''),
    },
  };
}

function deriveDisplaySummary(
  tasks: OptimizationTask[],
  currentRunInfo: OptimizationRunStatus | null,
  rawProgress: number
): OptimizationDisplaySummary {
  const taskCompleted = tasks.filter((task) => task.status === 'completed').length;
  const taskFailed = tasks.filter((task) => task.status === 'failed').length;
  const taskProcessed = taskCompleted + taskFailed;
  const totalTasks = Math.max(tasks.length, currentRunInfo?.total_tasks || 0);
  const backendProcessed = currentRunInfo?.completed_count || 0;
  const backendFailed = currentRunInfo?.failed_count || 0;
  const processedCount = totalTasks > 0 ? Math.min(Math.max(taskProcessed, backendProcessed), totalTasks) : 0;
  const failedCount = Math.min(Math.max(taskFailed, backendFailed), processedCount);
  const completedCount = Math.max(0, Math.min(Math.max(taskCompleted, processedCount - failedCount), processedCount));
  const remainingCount = Math.max(totalTasks - processedCount, 0);
  
  // 优先使用后端返回的进度（0-1），避免重复计算导致跳动
  const backendProgress = currentRunInfo?.progress ?? rawProgress;
  const progress = totalTasks > 0 ? Math.max(backendProgress, processedCount / totalTasks) : backendProgress;

  return {
    totalTasks,
    processedCount,
    completedCount,
    failedCount,
    remainingCount,
    progress,
  };
}

export const GridSearchPanel: React.FC = () => {
  const [isRunning, setIsRunning] = useState(false);
  const [tasks, setTasks] = useState<OptimizationTask[]>([]);
  const [config, setConfig] = useState<GridSearchConfig | null>(null);
  const [globalError, setGlobalError] = useState('');
  const [optimizationProgress, setOptimizationProgress] = useState(0);
  const [optimizationStatus, setOptimizationStatus] = useState('');
  const [workerReady, setWorkerReady] = useState<boolean | null>(null);
  const [workerHint, setWorkerHint] = useState('');
  const [isCheckingWorker, setIsCheckingWorker] = useState(false);
  const [applySuccess, setApplySuccess] = useState(false);
  const [currentTaskId, setCurrentTaskId] = useState<string | null>(null);
  const [currentOptimizationId, setCurrentOptimizationId] = useState<string | null>(null);
  const [currentRunInfo, setCurrentRunInfo] = useState<OptimizationRunStatus | null>(null);
  const [logMessages, setLogMessages] = useState<string[]>([]);
  const [historyItems, setHistoryItems] = useState<OptimizationHistoryItem[]>([]);
  const [selectedOptimizationId, setSelectedOptimizationId] = useState<string | null>(null);
  const [selectedDetail, setSelectedDetail] = useState<OptimizationHistoryDetail | null>(null);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [isHistoryPanelOpen, setIsHistoryPanelOpen] = useState(false);
  const [clearingHistory, setClearingHistory] = useState(false);

  const abortControllerRef = useRef<AbortController | null>(null);
  const isAbortedRef = useRef(false);
  const selectedOptimizationIdRef = useRef<string | null>(null);
  const logMessagesRef = useRef<string[]>([]);
  const updateBacktestConfig = useBacktestCenterStore((state) => state.updateBacktestConfig);
  const setActiveModule = useBacktestCenterStore((state) => state.setActiveModule);

  const getCurrentUserId = () => {
    const storedUser = authService.getStoredUser() as any;
    return String(storedUser?.user_id || storedUser?.id || storedUser?.username || '').trim();
  };

  const checkWorkerHealth = useCallback(async () => {
    setIsCheckingWorker(true);
    try {
      const health = await backtestService.getQlibHealth();
      const redisOk = health?.redis_ok === true;
      setWorkerReady(redisOk);
      setWorkerHint(redisOk ? '引擎与队列可用' : '检测到队列不可用，请确认 Redis/Celery worker 已启动');
    } catch (error) {
      setWorkerReady(false);
      setWorkerHint(error instanceof Error ? error.message : '无法连接优化服务');
    } finally {
      setIsCheckingWorker(false);
    }
  }, []);

  const loadHistory = useCallback(async () => {
    setHistoryLoading(true);
    try {
      const items = await backtestService.getOptimizationHistory(HISTORY_LIMIT);
      setHistoryItems(items);
      if (!selectedOptimizationIdRef.current && items[0]?.optimization_id) {
        selectedOptimizationIdRef.current = items[0].optimization_id;
        setSelectedOptimizationId(items[0].optimization_id);
      }
      return items;
    } catch (error) {
      setGlobalError(error instanceof Error ? error.message : '加载优化历史失败');
      return [];
    } finally {
      setHistoryLoading(false);
    }
  }, []);

  const loadDetail = useCallback(async (optimizationId: string) => {
    setDetailLoading(true);
    try {
      const detail = await backtestService.getOptimizationDetail(optimizationId);
      setSelectedDetail(detail);
      const derivedConfig = deriveGridConfigFromHistory(detail);
      if (derivedConfig) {
        setConfig(derivedConfig);
        const baseTasks = buildTasksFromGridConfig(derivedConfig);
        setTasks(buildTasksFromDetail(detail, baseTasks));
        const total = detail.total_tasks || baseTasks.length || 1;
        setOptimizationProgress(total > 0 ? (detail.completed_count || 0) / total : 0);
      }
      setOptimizationStatus(detail.status);
      setCurrentOptimizationId(detail.optimization_id);
      setCurrentTaskId(detail.task_id || null);
      setCurrentRunInfo({
        optimization_id: detail.optimization_id,
        progress: detail.total_tasks > 0 ? detail.completed_count / detail.total_tasks : 0,
        status: detail.status,
        message: detail.error_message || detail.status,
        total_tasks: detail.total_tasks,
        completed_count: detail.completed_count,
        failed_count: detail.failed_count,
        current_params: detail.current_params,
        best_params: detail.best_params,
        best_metric_value: detail.best_metric_value,
        result_summary: detail.result_summary,
      });
    } catch (error) {
      setGlobalError(error instanceof Error ? error.message : '加载优化详情失败');
    } finally {
      setDetailLoading(false);
    }
  }, []);

  const applyHistoryDetail = useCallback((detail: OptimizationHistoryDetail) => {
    const derivedConfig = deriveGridConfigFromHistory(detail);
    if (!derivedConfig) return;
    setConfig(derivedConfig);
    const nextTasks = buildTasksFromDetail(detail, buildTasksFromGridConfig(derivedConfig));
    setTasks(nextTasks);
    const total = detail.total_tasks || nextTasks.length || 1;
    setOptimizationProgress(total > 0 ? (detail.completed_count || 0) / total : 0);
    setOptimizationStatus(detail.status);
  }, []);

  const handleClearHistory = useCallback(async () => {
    if (!window.confirm('确定要清除所有参数优化历史记录吗？\n此操作将从数据库中移除记录并清理相关的服务器物理目录，且无法撤销。')) {
      return;
    }
    
    setClearingHistory(true);
    try {
      await backtestService.clearOptimizationHistory();
      setHistoryItems([]);
      setSelectedOptimizationId(null);
      setSelectedDetail(null);
      setCurrentOptimizationId(null);
      setCurrentTaskId(null);
      setCurrentRunInfo(null);
      // 同时清理当前显示的图表任务
      setTasks([]);
      setOptimizationProgress(0);
      setOptimizationStatus('');
    } catch (error) {
      setGlobalError(error instanceof Error ? error.message : '清理优化历史失败');
    } finally {
      setClearingHistory(false);
    }
  }, []);

  const watchExistingTask = useCallback(
    async (taskId: string) => {
      abortControllerRef.current = new AbortController();
      setIsRunning(true);
      isAbortedRef.current = false;
      try {
        const response = await backtestService.watchOptimizationTask<any>(taskId, {
          signal: abortControllerRef.current.signal,
          onProgress: async (progress, status, info) => {
            if (isAbortedRef.current) return;
            setOptimizationProgress(progress);
            if (status) setOptimizationStatus(status);
            if (info) {
              setCurrentRunInfo(info);
              setCurrentOptimizationId(info.optimization_id || null);
              const detailId = info.optimization_id;
              if (detailId) {
                setSelectedOptimizationId(detailId);
                selectedOptimizationIdRef.current = detailId;
                const detail = await backtestService.getOptimizationDetail(detailId);
                setSelectedDetail(detail);
                applyHistoryDetail(detail);
              }
            }
          },
          onLog: (message) => {
            const nextLogs = [...logMessagesRef.current, message].slice(-200);
            logMessagesRef.current = nextLogs;
            setLogMessages(nextLogs);
          },
        });
        if (response?.optimization_id) {
          await loadHistory();
          await loadDetail(response.optimization_id);
        }
      } catch (error) {
        if ((error as Error)?.name !== 'AbortError') {
          setGlobalError(error instanceof Error ? error.message : '恢复优化任务失败');
        }
      } finally {
        setIsRunning(false);
      }
    },
    [applyHistoryDetail, loadDetail, loadHistory]
  );

  const runOptimization = async (gridConfig: GridSearchConfig) => {
    setGlobalError('');
    setConfig(gridConfig);
    setIsRunning(true);
    isAbortedRef.current = false;
    setOptimizationProgress(0);
    setOptimizationStatus('pending');
    setApplySuccess(false);
    setSelectedDetail(null);
    setCurrentRunInfo(null);
    logMessagesRef.current = [];
    setLogMessages([]);

    if (workerReady !== true) {
      setGlobalError(workerHint || '引擎队列不可用，无法启动参数优化');
      setIsRunning(false);
      return;
    }

    const initialTasks = buildTasksFromGridConfig(gridConfig);
    setTasks(initialTasks);
    abortControllerRef.current = new AbortController();

    const initialTopk = Math.round((gridConfig.parameters.topk.min + gridConfig.parameters.topk.max) / 2);
    const initialNDrop = Math.round((gridConfig.parameters.n_drop.min + gridConfig.parameters.n_drop.max) / 2);

    try {
      const response = await backtestService.optimizeQlibParameters(
        {
          symbol: 'all',
          start_date: gridConfig.dateRange.startDate,
          end_date: gridConfig.dateRange.endDate,
          initial_capital: gridConfig.initialCapital,
          user_id: getCurrentUserId(),
          qlib_strategy_type: 'TopkDropout',
          qlib_strategy_params: { topk: initialTopk, n_drop: initialNDrop },
          param_ranges: [
            {
              name: 'topk',
              min: gridConfig.parameters.topk.min,
              max: gridConfig.parameters.topk.max,
              step: gridConfig.parameters.topk.step,
            },
            {
              name: 'n_drop',
              min: gridConfig.parameters.n_drop.min,
              max: gridConfig.parameters.n_drop.max,
              step: gridConfig.parameters.n_drop.step,
            },
          ],
          optimization_target: gridConfig.metric,
          max_parallel: 3,
          commission: 0.00025,
          min_commission: 5,
          stamp_duty: 0.0005,
          transfer_fee: 0.00001,
        },
        {
          signal: abortControllerRef.current.signal,
          onTaskCreated: (taskId) => {
            setCurrentTaskId(taskId);
            void loadHistory();
          },
          onProgress: async (progress, status, info) => {
            if (isAbortedRef.current) return;
            setOptimizationProgress(progress);
            if (status) setOptimizationStatus(status);
            if (info) {
              setCurrentRunInfo(info);
              setCurrentOptimizationId(info.optimization_id || null);
              const nextTasks = buildTasksFromDetail(
                {
                  optimization_id: info.optimization_id || '',
                  task_id: currentTaskId || undefined,
                  mode: 'grid_search',
                  user_id: getCurrentUserId(),
                  tenant_id: authService.getTenantId() || 'default',
                  status: (info.status as any) || 'running',
                  created_at: new Date().toISOString(),
                  updated_at: new Date().toISOString(),
                  total_tasks: info.total_tasks || initialTasks.length,
                  completed_count: info.completed_count || 0,
                  failed_count: info.failed_count || 0,
                  current_params: info.current_params,
                  best_params: info.best_params,
                  best_metric_value: info.best_metric_value,
                  config_snapshot: {},
                  can_apply: false,
                  base_request: {},
                  param_ranges: [],
                  result_summary: info.result_summary || {},
                  all_results: [],
                },
                initialTasks
              );
              setTasks(nextTasks);
            }
          },
          onLog: (message) => {
            const nextLogs = [...logMessagesRef.current, message].slice(-200);
            logMessagesRef.current = nextLogs;
            setLogMessages(nextLogs);
          },
        }
      );

      if (isAbortedRef.current) return;
      if (response?.optimization_id) {
        await loadHistory();
        setSelectedOptimizationId(response.optimization_id);
        selectedOptimizationIdRef.current = response.optimization_id;
        await loadDetail(response.optimization_id);
      }
    } catch (error) {
      if (!isAbortedRef.current && (error as Error)?.name !== 'AbortError') {
        setGlobalError(error instanceof Error ? error.message : '并行优化过程出现错误');
      }
    } finally {
      setIsRunning(false);
      abortControllerRef.current = null;
      setCurrentTaskId(null);
    }
  };

  const handleStop = async () => {
    isAbortedRef.current = true;
    const taskId = currentTaskId || selectedDetail?.task_id || null;
    const optimizationId = currentOptimizationId || selectedOptimizationIdRef.current;
    if (taskId) {
      try {
        await backtestService.stopTask(taskId);
        await loadHistory();
        if (optimizationId) {
          await loadDetail(optimizationId);
        }
      } catch (error) {
        console.warn('停止参数优化任务失败', error);
      }
    }
    abortControllerRef.current?.abort();
    setOptimizationStatus('cancelled');
    setIsRunning(false);
  };

  const handleApplyBestParams = (params: { topk: number; n_drop: number }) => {
    if (!config) return;
    updateBacktestConfig({
      start_date: config.dateRange.startDate,
      end_date: config.dateRange.endDate,
      initial_capital: config.initialCapital,
      qlib_strategy_type: 'TopkDropout',
      qlib_strategy_params: {
        topk: params.topk,
        n_drop: params.n_drop,
      },
      strategy_params: {
        topk: params.topk,
        n_drop: params.n_drop,
      },
    });
    setActiveModule('quick-backtest');
    setApplySuccess(true);
  };

  const selectedHistorySummary = useMemo(
    () => historyItems.find((item) => item.optimization_id === selectedOptimizationId) || null,
    [historyItems, selectedOptimizationId]
  );

  const displaySummary = useMemo(
    () => deriveDisplaySummary(tasks, currentRunInfo, optimizationProgress),
    [tasks, currentRunInfo, optimizationProgress]
  );

  const baselineParams = useMemo(() => {
    const rawParams =
      selectedDetail?.base_request?.qlib_strategy_params ||
      selectedDetail?.config_snapshot?.base_request?.qlib_strategy_params;
    const topk = Number(rawParams?.topk ?? 50);
    const nDrop = Number(rawParams?.n_drop ?? 5);
    if (Number.isNaN(topk) || Number.isNaN(nDrop)) {
      return null;
    }
    return { topk, n_drop: nDrop };
  }, [selectedDetail]);

  const progressStatusText = useMemo(() => {
    const statusMap: Record<string, string> = {
      pending: '待启动',
      queued: '排队中',
      initializing: '初始化中',
      running: '运行中',
      completed: '已完成',
      failed: '失败',
      cancelled: '已取消',
      retrying: '重试中',
    };
    return statusMap[optimizationStatus] || optimizationStatus || '未开始';
  }, [optimizationStatus]);

  const queueAhead = useMemo(
    () => getQueueAhead(currentRunInfo?.result_summary),
    [currentRunInfo]
  );

  const queuedHint = useMemo(() => getQueuedHint(queueAhead), [queueAhead]);

  useEffect(() => {
    void checkWorkerHealth();
    void (async () => {
      const items = await loadHistory();
      const latestRunning = items.find((item) => item.status === 'running' || item.status === 'queued' || item.status === 'pending');
      if (latestRunning?.optimization_id) {
        setSelectedOptimizationId(latestRunning.optimization_id);
        selectedOptimizationIdRef.current = latestRunning.optimization_id;
        await loadDetail(latestRunning.optimization_id);
        if (latestRunning.task_id) {
          void watchExistingTask(latestRunning.task_id);
        }
      } else if (items[0]?.optimization_id) {
        await loadDetail(items[0].optimization_id);
      }
    })();
  }, [checkWorkerHealth, loadDetail, loadHistory, watchExistingTask]);

  useEffect(() => {
    return () => {
      isAbortedRef.current = true;
      abortControllerRef.current?.abort();
    };
  }, []);

  return (
    <div className="space-y-4">
      <div className="flex justify-end">
        <button
          type="button"
          onClick={() => setIsHistoryPanelOpen(!isHistoryPanelOpen)}
          className="inline-flex items-center gap-2 px-3 py-2 text-sm rounded-xl border border-gray-200 bg-white text-gray-700 hover:bg-gray-50"
        >
          {isHistoryPanelOpen ? <ChevronRight className="w-4 h-4" /> : <ChevronLeft className="w-4 h-4" />}
          {isHistoryPanelOpen ? '隐藏优化历史' : '显示优化历史'}
        </button>
      </div>

      <div className={`grid grid-cols-1 gap-6 ${isHistoryPanelOpen ? 'xl:grid-cols-[minmax(0,1fr)_360px]' : ''}`}>
      <div className="space-y-6">
        <div className="bg-gradient-to-r from-blue-50 to-indigo-50 border border-blue-200 rounded-2xl p-4">
          <h2 className="text-xl font-bold text-gray-900 mb-2">网格搜索 (Grid Search)</h2>
          <p className="text-sm text-gray-700">
            受限模式：仅支持 TopkDropout 策略参数（topk / n_drop），系统最多允许 40 组组合；当前任务的实际组数以下方“本次预计评估组数”为准。
          </p>
          <div className="mt-3 flex items-center gap-3 text-sm">
            <span className={`px-2 py-1 rounded-lg border ${workerReady ? 'text-green-700 border-green-300 bg-green-50' : 'text-red-700 border-red-300 bg-red-50'}`}>
              {workerReady ? '队列状态：可用' : '队列状态：不可用'}
            </span>
            <button
              type="button"
              onClick={checkWorkerHealth}
              disabled={isCheckingWorker}
              className="inline-flex items-center gap-1 text-blue-700 hover:text-blue-800 disabled:text-gray-400"
            >
              <RefreshCw className={`w-4 h-4 ${isCheckingWorker ? 'animate-spin' : ''}`} />
              刷新状态
            </button>
            <span className="text-gray-600">{workerHint}</span>
          </div>
        </div>

        {globalError && (
          <div className="bg-red-50 border border-red-200 rounded-2xl p-4 flex items-start gap-2">
            <AlertCircle className="w-5 h-5 text-red-600 flex-shrink-0 mt-0.5" />
            <div>
              <div className="font-semibold text-red-900">优化失败</div>
              <div className="text-sm text-red-800 mt-1">{globalError}</div>
            </div>
          </div>
        )}

        <ParameterGrid
          onStartOptimization={runOptimization}
          isRunning={isRunning}
          workerReady={workerReady === true}
          workerMessage={workerHint}
        />

        {isRunning && (
          <div className="flex justify-center">
            <button
              onClick={handleStop}
              className="px-6 py-2 bg-red-600 text-white rounded-2xl hover:bg-red-700 transition-colors"
            >
              停止优化
            </button>
          </div>
        )}

        {tasks.length > 0 && (
          <>
            <div className="bg-white rounded-2xl border border-gray-200 p-4">
              <div className="flex items-center justify-between text-sm text-gray-700 mb-2">
                <span>优化进度</span>
                <span>{Math.round(displaySummary.progress * 100)}%</span>
              </div>
              <div className="w-full h-2 bg-gray-100 rounded-full overflow-hidden">
                <div
                  className="h-full bg-indigo-500 rounded-full transition-all"
                  style={{ width: `${Math.round(displaySummary.progress * 100)}%` }}
                />
              </div>
              {optimizationStatus && (
                <div className="text-xs text-gray-500 mt-2">
                  状态: {progressStatusText} · 已评估 {displaySummary.processedCount}/{displaySummary.totalTasks} 组参数
                  {(optimizationStatus === 'queued' || optimizationStatus === 'pending') && currentRunInfo?.message
                    ? ` · ${queueAhead !== null && queueAhead > 0 ? `前方还有 ${queueAhead} 个任务` : currentRunInfo.message}`
                    : ''}
                </div>
              )}
            </div>

            <OptimizationProgress
              tasks={tasks}
              processedCount={displaySummary.processedCount}
              totalTasks={displaySummary.totalTasks}
              completedCount={displaySummary.completedCount}
              failedCount={displaySummary.failedCount}
              remainingCount={displaySummary.remainingCount}
              progress={displaySummary.progress}
            />

            {config && (
              <OptimizationResults
                tasks={tasks}
                config={config}
                baselineParams={baselineParams}
                onApplyBestParams={handleApplyBestParams}
              />
            )}

            {logMessages.length > 0 && (
              <div className="bg-slate-950 text-slate-100 rounded-2xl p-4">
                <div className="text-sm font-semibold mb-3">运行日志</div>
                <div className="max-h-64 overflow-y-auto space-y-1 text-xs font-mono">
                  {logMessages.slice(-50).map((line, index) => (
                    <div key={`${index}-${line.slice(0, 12)}`}>{line}</div>
                  ))}
                </div>
              </div>
            )}
          </>
        )}

        {applySuccess && (
          <div className="bg-green-50 border border-green-200 rounded-2xl p-4 text-sm text-green-800">
            最优参数已回填到“快速回测”配置，可直接切换模块执行回测。
          </div>
        )}
      </div>

      {isHistoryPanelOpen && (
      <aside className="bg-white rounded-2xl border border-gray-200 p-4 space-y-4 h-fit">
        <div className="flex items-center justify-between">
          <div>
            <h3 className="text-lg font-semibold text-gray-900">优化历史</h3>
            <p className="text-xs text-gray-500 mt-1">支持恢复运行中任务与历史结果回填</p>
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => void loadHistory()}
              disabled={historyLoading || clearingHistory}
              className="text-sm text-blue-600 hover:text-blue-800 disabled:text-gray-400"
            >
              刷新
            </button>
            <button
              type="button"
              onClick={handleClearHistory}
              disabled={historyLoading || clearingHistory}
              className="text-sm text-red-500 hover:text-red-700 disabled:text-gray-400"
            >
              {clearingHistory ? '清理中...' : '清除历史'}
            </button>
            <button
              type="button"
              onClick={() => setIsHistoryPanelOpen(false)}
              className="text-sm text-gray-500 hover:text-gray-700"
            >
              收起
            </button>
          </div>
        </div>

        <div className="space-y-2 max-h-80 overflow-y-auto">
          {historyLoading && <div className="text-sm text-gray-500">正在加载历史...</div>}
          {!historyLoading && historyItems.length === 0 && (
            <div className="text-sm text-gray-500">暂无优化历史</div>
          )}
          {historyItems.map((item) => (
            <button
              key={item.optimization_id}
              type="button"
              onClick={() => {
                setSelectedOptimizationId(item.optimization_id);
                selectedOptimizationIdRef.current = item.optimization_id;
                void loadDetail(item.optimization_id);
                if ((item.status === 'running' || item.status === 'queued' || item.status === 'pending') && item.task_id) {
                  void watchExistingTask(item.task_id);
                }
              }}
              className={`w-full text-left rounded-2xl border p-3 transition-colors ${
                selectedOptimizationId === item.optimization_id
                  ? 'border-blue-400 bg-blue-50'
                  : 'border-gray-200 hover:border-gray-300'
              }`}
            >
              <div className="flex items-center justify-between gap-3">
                <span className="text-sm font-medium text-gray-900">
                  {item.best_params
                    ? `topk=${item.best_params.topk ?? '-'}, n_drop=${item.best_params.n_drop ?? '-'}`
                    : item.current_params
                      ? `运行中: topk=${item.current_params.topk ?? '-'}, n_drop=${item.current_params.n_drop ?? '-'}`
                      : item.optimization_target || 'Grid Search'}
                </span>
                <span className={`text-xs px-2 py-1 rounded-full ${
                  item.status === 'completed'
                    ? 'bg-green-100 text-green-700'
                    : item.status === 'failed'
                      ? 'bg-red-100 text-red-700'
                      : item.status === 'queued' || item.status === 'pending'
                        ? 'bg-amber-100 text-amber-700'
                      : item.status === 'cancelled'
                        ? 'bg-gray-100 text-gray-700'
                        : 'bg-blue-100 text-blue-700'
                }`}>
                  {item.status === 'running'
                    ? '运行中'
                    : item.status === 'queued'
                      ? '排队中'
                      : item.status === 'pending'
                        ? '待启动'
                    : item.status === 'completed'
                      ? '已完成'
                      : item.status === 'failed'
                        ? '失败'
                        : '已取消'}
                </span>
              </div>
              <div className="text-xs text-gray-500 mt-2">
                {new Date(item.created_at).toLocaleString()} · {item.completed_count}/{item.total_tasks}
              </div>
            </button>
          ))}
        </div>

        <div className="border-t border-gray-100 pt-4">
          <div className="text-sm font-semibold text-gray-900 mb-3">历史详情</div>
          {detailLoading && <div className="text-sm text-gray-500">正在加载详情...</div>}
          {!detailLoading && !selectedDetail && (
            <div className="text-sm text-gray-500">选择一条优化记录查看详情</div>
          )}
          {!detailLoading && selectedDetail && (
            <div className="space-y-3">
              <div className="rounded-2xl bg-gray-50 border border-gray-200 p-3 text-sm">
                <div className="flex items-center justify-between">
                  <span className="font-medium text-gray-900">当前状态</span>
                  <span className="text-gray-600">{selectedDetail.status}</span>
                </div>
                <div className="mt-2 text-xs text-gray-500">
                  进度 {selectedDetail.completed_count}/{selectedDetail.total_tasks}
                  {selectedDetail.best_params
                    ? ` · 最优 topk=${selectedDetail.best_params.topk ?? '-'}, n_drop=${selectedDetail.best_params.n_drop ?? '-'}`
                    : ''}
                </div>
                {selectedDetail.error_message && (
                  <div className="mt-2 text-xs text-red-600">{selectedDetail.error_message}</div>
                )}
              </div>

              {(selectedDetail.status === 'running' || selectedDetail.status === 'queued' || selectedDetail.status === 'pending') && (
                <div className="rounded-2xl border border-blue-200 bg-blue-50 p-3 text-sm text-blue-800 flex items-center gap-2">
                  <Clock3 className="w-4 h-4" />
                  {selectedDetail.status === 'running'
                    ? '该任务正在运行，右侧详情会持续刷新，完成后可直接回填。'
                    : getQueuedHint(getQueueAhead(selectedDetail.result_summary))}
                </div>
              )}

              {selectedDetail.can_apply && selectedDetail.best_params && (
                <button
                  type="button"
                  onClick={() => handleApplyBestParams({
                    topk: Number(selectedDetail.best_params?.topk || 50),
                    n_drop: Number(selectedDetail.best_params?.n_drop || 5),
                  })}
                  className="w-full px-4 py-2 rounded-2xl bg-blue-600 text-white hover:bg-blue-700 transition-colors"
                >
                  回填最优参数到快速回测
                </button>
              )}

              {selectedHistorySummary && (
                <div className="text-xs text-gray-500">
                  目标指标：{selectedHistorySummary.optimization_target || 'sharpe_ratio'}
                </div>
              )}
            </div>
          )}
        </div>
      </aside>
      )}
      </div>
    </div>
  );
};
