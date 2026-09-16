/**
 * TaskContext — Global Task State Management
 *
 * Lifts mining and backtest task state, WebSocket connection, and polling logic
 * to App level, so running state is not lost when switching pages.
 */

import React, { createContext, useContext, useState, useCallback, useRef, useEffect } from 'react';
import type {
  Task,
  TaskConfig,
  LogEntry,
  RealtimeMetrics,
  TimeSeriesData,
  WsMessage,
} from '../types-v2';
import { generateId } from '../utils-v2';
import {
  startMining as apiStartMining,
  getMiningStatus,
  cancelMining as apiCancelMining,
  listTasks,
  startBacktest as apiStartBacktest,
  getBacktestStatus,
  cancelBacktest as apiCancelBacktest,
  connectMiningWs,
  healthCheck,
} from '../services-v2/api';
import type { BacktestStartParams } from '../services-v2/api';
import { getDefaultMiningDirection, getStoredDirectionConfig } from '../utils-v2/miningDirections';

// ========================== Backtest local type ==========================

export interface BacktestTask {
  taskId: string;
  status: string;
  progress: {
    phase: string;
    progress: number;
    message: string;
    timestamp: string;
  };
  logs: LogEntry[];
  metrics: Record<string, any>;
  config: Record<string, any>;
  createdAt: string;
  updatedAt: string;
}

// ========================== Structured factors merge ==========================

/**
 * 把后端任务状态返回的结构化因子（rd_agent_factors 已落库数据）合并进实时指标。
 * 数据来自数据库而非日志文本解析，不受后端日志措辞变化影响。
 */
function mergeStructuredFactors(
  metrics: RealtimeMetrics | undefined,
  rawFactors: any[],
): RealtimeMetrics {
  const base: RealtimeMetrics = metrics || {
    ic: 0, icir: 0, rankIc: 0, rankIcir: 0,
    annualReturn: 0, sharpeRatio: 0, maxDrawdown: 0,
    totalFactors: 0, highQualityFactors: 0, mediumQualityFactors: 0, lowQualityFactors: 0,
    top10Factors: [],
  };
  if (!rawFactors.length) return base;

  const mapped = rawFactors.map((f: any) => ({
    factorId: f.factor_id ?? '',
    factorName: f.factor_name ?? 'unnamed',
    factorExpression:
      f.factor_formulation || f.metadata?.formulation || (f.factor_code || '').slice(0, 120),
    rankIc: f.rank_ic ?? 0,
    rankIcir: f.metadata?.rank_icir ?? 0,
    ic: f.ic_value ?? 0,
    icir: f.metadata?.icir ?? 0,
    annualReturn: f.annual_return ?? 0,
    sharpeRatio: f.sharpe_ratio ?? 0,
    maxDrawdown: f.max_drawdown ?? 0,
    calmarRatio: 0,
    market: f.market,
    cumulativeCurve: [] as Array<{ date: string; value: number }>,
  }));

  // RankIC 降序取 Top10，并重算最优因子指标
  const top10 = [...mapped].sort((a, b) => (b.rankIc || 0) - (a.rankIc || 0)).slice(0, 10);
  const best = top10.reduce((b, c) => ((c.rankIc || 0) > (b.rankIc || 0) ? c : b), top10[0]);
  return {
    ...base,
    totalFactors: Math.max(base.totalFactors || 0, mapped.length),
    top10Factors: top10,
    factorName: best.factorName,
    rankIc: best.rankIc ?? 0,
    rankIcir: best.rankIcir ?? 0,
    ic: best.ic ?? 0,
    icir: best.icir ?? 0,
    annualReturn: best.annualReturn ?? 0,
    sharpeRatio: best.sharpeRatio ?? 0,
    maxDrawdown: best.maxDrawdown ?? 0,
  };
}

// ========================== Context Value ==========================

interface TaskContextValue {
  // Backend health
  backendAvailable: boolean | null;

  // ---- Mining ----
  miningTask: Task | null;
  /** POST /evolve 提交进行中（后端同步建缓存时可能耗时较长） */
  miningStarting: boolean;
  /** 用户主动开始挖掘的序号（仅用于「开始后自动进入演化台」，恢复历史任务不触发） */
  miningStartSeq: number;
  miningEquityCurve: TimeSeriesData[];
  miningDrawdownCurve: TimeSeriesData[];
  miningIcTimeSeries: TimeSeriesData[];
  startMining: (config: TaskConfig) => void;
  stopMining: () => void;
  resetMiningTask: () => void;

  // ---- Backtest ----
  backtestTask: BacktestTask | null;
  backtestLogs: LogEntry[];
  startBacktestTask: (params: BacktestStartParams) => Promise<void>;
  stopBacktestTask: () => void;
}

const TaskContext = createContext<TaskContextValue | null>(null);

// ========================== Provider ==========================

export const TaskProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  // ---- Backend health ----
  const [backendAvailable, setBackendAvailable] = useState<boolean | null>(null);

  useEffect(() => {
    healthCheck()
      .then(() => setBackendAvailable(true))
      .catch(() => setBackendAvailable(false));
  }, []);

  // ==================================================================
  // MINING
  // ==================================================================
  const [miningTask, setMiningTask] = useState<Task | null>(null);
  // 任务提交锁：POST /evolve 进行中（数据源为 parquet 时后端同步建缓存可能耗时 1 分钟+），
  // 期间禁止重复提交
  const [miningStarting, setMiningStarting] = useState(false);
  const [miningStartSeq, setMiningStartSeq] = useState(0);
  const miningStartSeqRef = useRef(0);
  const [miningEquityCurve, setMiningEquityCurve] = useState<TimeSeriesData[]>([]);
  const [miningDrawdownCurve, setMiningDrawdownCurve] = useState<TimeSeriesData[]>([]);
  const [miningIcTimeSeries, setMiningIcTimeSeries] = useState<TimeSeriesData[]>([]);

  const miningWsRef = useRef<WebSocket | null>(null);
  const miningPollingRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const miningWsTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const miningDataPointsRef = useRef(0);
  const mountedRef = useRef(true);
  // 同步到 ref 供 startRealMining 闭包内读取，避免 stale state
  const miningStartingRef = useRef(false);
  const miningTaskRef = useRef<Task | null>(null);
  useEffect(() => {
    miningStartingRef.current = miningStarting;
  }, [miningStarting]);
  useEffect(() => {
    miningTaskRef.current = miningTask;
  }, [miningTask]);

  // Cleanup on unmount: clear all polling intervals, WS connections, and
  // the recursive setTimeout inside connectMiningWs, and prevent stale state updates.
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      // Clear mining polling
      if (miningPollingRef.current) {
        clearInterval(miningPollingRef.current);
        miningPollingRef.current = null;
      }
      // Clear mining WS
      miningWsRef.current?.close();
      miningWsRef.current = null;
      // Clear the recursive setTimeout from connectMiningWs
      if (miningWsTimeoutRef.current) {
        clearTimeout(miningWsTimeoutRef.current);
        miningWsTimeoutRef.current = null;
      }
      // Clear backtest polling
      if (backtestPollingRef.current) {
        clearInterval(backtestPollingRef.current);
        backtestPollingRef.current = null;
      }
      // Clear backtest WS
      backtestWsRef.current?.close();
      backtestWsRef.current = null;
    };
  }, []);

  // WS handler for mining
  const handleMiningWsMessage = useCallback(
    (msg: WsMessage) => {
      if (!mountedRef.current) return;
      setMiningTask(((prev: Task | null) => {
        if (!prev) return prev;
        const updated = { ...prev };
        switch (msg.type) {
          case 'progress':
            updated.progress = msg.data;
            updated.status = msg.data.phase === 'completed' ? 'completed' : 'running';
            if (msg.data.timeline) updated.timeline = msg.data.timeline;
            if (msg.data.tokenUsage) updated.tokenUsage = msg.data.tokenUsage;
            // 结构化因子（后端已落库，优先于日志正则解析），直接更新 Top10 列表
            if (Array.isArray(msg.data.factors) && msg.data.factors.length > 0) {
              updated.metrics = mergeStructuredFactors(updated.metrics, msg.data.factors);
            }
            break;
          case 'log':
            // Increased frontend log retention limit from 99 to 2000
            updated.logs = [...(updated.logs || []).slice(-2000), msg.data as LogEntry];
            
            // Try to extract factor from log message to show it immediately in the list
            // Pattern: "Added new factor: {name} with expression: {expr}"
            const logMsg = (msg.data as LogEntry).message;
            if (logMsg && logMsg.includes("Added new factor:")) {
              const match = logMsg.match(/Added new factor: (.+?) with expression: (.+)/);
              if (match) {
                const [_, name, expr] = match;
                const currentMetrics = updated.metrics || {
                    ic: 0, icir: 0, rankIc: 0, rankIcir: 0,
                    annualReturn: 0, sharpeRatio: 0, maxDrawdown: 0,
                    totalFactors: 0, highQualityFactors: 0, mediumQualityFactors: 0, lowQualityFactors: 0,
                    top10Factors: []
                };
                
                const currentFactors = currentMetrics.top10Factors || [];
                // Avoid duplicates
                if (!currentFactors.some((f: any) => f.factorName === name)) {
                    const newFactor = {
                        factorId: generateId(),
                        factorName: name,
                        factorExpression: expr,
                        rankIc: 0, rankIcir: 0, ic: 0, icir: 0,
                        annualReturn: 0, sharpeRatio: 0, maxDrawdown: 0, calmarRatio: 0,
                        cumulativeCurve: []
                    };
                    
                    // Recalculate best metrics from the updated list
                    const updatedFactors = [newFactor, ...currentFactors];
                    const bestFactor = updatedFactors.reduce((best, current) => {
                        // Prioritize RankIC, but handle potential missing values
                        const bestScore = best.rankIc || 0;
                        const currentScore = current.rankIc || 0;
                        return currentScore > bestScore ? current : best;
                    }, updatedFactors[0]);

                    updated.metrics = {
                        ...currentMetrics,
                        totalFactors: (currentMetrics.totalFactors || 0) + 1,
                        // Prepend new factor to the list so user sees it immediately
                        top10Factors: updatedFactors,
                        // Update best factor metrics
                        factorName: bestFactor.factorName,
                        rankIc: bestFactor.rankIc ?? 0,
                        rankIcir: bestFactor.rankIcir ?? 0,
                        ic: bestFactor.ic ?? 0,
                        icir: bestFactor.icir ?? 0,
                        annualReturn: bestFactor.annualReturn ?? 0,
                        sharpeRatio: bestFactor.sharpeRatio ?? 0,
                        maxDrawdown: bestFactor.maxDrawdown ?? 0,
                    };
                }
              }
            }
            break;
          case 'metrics':
            updated.metrics = {
              ...(updated.metrics || {} as RealtimeMetrics),
              ...msg.data as RealtimeMetrics
            };
            break;
          case 'result':
            updated.status = msg.data.status === 'completed' ? 'completed' : 'failed';
            if (msg.data.metrics) updated.metrics = msg.data.metrics;
            break;
          case 'error':
            updated.status = 'failed';
            updated.logs = [
              ...(updated.logs || []),
              {
                id: generateId(),
                timestamp: new Date().toISOString(),
                level: 'error',
                message: msg.data.error || 'Unknown error',
              },
            ];
            break;
        }
        updated.updatedAt = new Date().toISOString();
        return updated;
      }) as unknown as Task | null);
    },
    [],
  );

  // 绑定某个挖掘任务的实时传输（WebSocket + 轮询兜底），供新任务与恢复共用
  const bindMiningTransport = useCallback(
    (taskId: string) => {
      if (miningWsTimeoutRef.current) {
        clearTimeout(miningWsTimeoutRef.current);
        miningWsTimeoutRef.current = null;
      }
      miningWsRef.current?.close();
      miningWsRef.current = null;
      if (miningPollingRef.current) {
        clearInterval(miningPollingRef.current);
        miningPollingRef.current = null;
      }
      const ws = connectMiningWs(taskId, handleMiningWsMessage, () => {
        if (!mountedRef.current) return;
        getMiningStatus(taskId).then((r) => {
          if (r.data?.task && mountedRef.current) setMiningTask(r.data.task as Task);
        });
      });
      miningWsRef.current = ws;
      miningWsTimeoutRef.current = (ws as any)._pollingTimeoutId ?? null;
      miningPollingRef.current = setInterval(async () => {
        if (!mountedRef.current) {
          clearInterval(miningPollingRef.current!);
          miningPollingRef.current = null;
          return;
        }
        try {
          const r = await getMiningStatus(taskId);
          if (!mountedRef.current) return;
          if (r.data?.task) {
            const t = r.data.task as Task;
            if (t.status === 'completed' || t.status === 'failed') {
              setMiningTask(t);
              clearInterval(miningPollingRef.current!);
              miningPollingRef.current = null;
            }
          }
        } catch {
          // ignore
        }
      }, 10000);
    },
    [handleMiningWsMessage],
  );

  // 恢复：刷新/离开再回来时，若后端仍有运行中的挖掘任务，重新绑定并展示进度
  const miningRecoveredRef = useRef(false);
  useEffect(() => {
    if (miningRecoveredRef.current) return;
    miningRecoveredRef.current = true;
    listTasks()
      .then((r) => {
        if (!mountedRef.current || miningTaskRef.current) return;
        const running = (r.data?.tasks ?? []).find((t) => t.status === 'running');
        if (running) {
          setMiningTask(running);
          bindMiningTransport(running.taskId);
        }
      })
      .catch(() => {});
  }, [bindMiningTransport]);

  // Start mining (real backend)
  const startRealMining = useCallback(
    async (config: TaskConfig) => {
      // 任务锁：已有任务在运行或提交进行中时，忽略重复提交
      if (miningStartingRef.current) return;
      if (miningTaskRef.current?.status === 'running') return;
      try {
        setMiningStarting(true);
        // Load defaults from localStorage
        let defaults: any = {};
        const savedConfig = localStorage.getItem('quantaalpha_config');
        if (savedConfig) {
          try {
            defaults = JSON.parse(savedConfig);
          } catch {}
        }

        const stored = getStoredDirectionConfig();
        const useCustom = Boolean(config.useCustomMiningDirection);
        const direction =
          useCustom
            ? (getDefaultMiningDirection() || '价量因子挖掘')
            : (config.userInput && config.userInput.trim()) || getDefaultMiningDirection() || '价量因子挖掘';
        const resp = await apiStartMining({
          direction,
          directions: useCustom ? stored.labels : undefined,
          directionMode: stored.mode,
          market: config.miningMarket || 'a_share',
          universe: config.universe || defaults.defaultUniverse || 'csi300',
          dataSource: config.dataSource || 'qlib_bin',
          numDirections: config.numDirections || defaults.defaultNumDirections || 2,
          maxRounds: config.maxRounds || defaults.defaultMaxRounds || 3,
          librarySuffix: config.librarySuffix || defaults.defaultLibrarySuffix || undefined,
          qualityGateEnabled: config.qualityGateEnabled ?? defaults.qualityGateEnabled ?? true,
          parallelEnabled: config.parallelExecution ?? defaults.parallelExecution ?? false,
        });
        if (!resp.success || !resp.data) throw new Error(resp.error || 'Failed');

        const taskData = resp.data.task as Task;
        // Initialize metrics with empty top10Factors to avoid stale data
        if (taskData.metrics) {
            taskData.metrics.top10Factors = [];
            taskData.metrics.totalFactors = 0;
            taskData.metrics.highQualityFactors = 0;
            taskData.metrics.mediumQualityFactors = 0;
            taskData.metrics.lowQualityFactors = 0;
        }
        setMiningTask(taskData);
        miningStartSeqRef.current += 1;
        setMiningStartSeq(miningStartSeqRef.current);
        setMiningEquityCurve([]);
        setMiningDrawdownCurve([]);
        setMiningIcTimeSeries([]);
        miningDataPointsRef.current = 0;

        // 绑定 WebSocket + 轮询兜底
        bindMiningTransport(resp.data.taskId);
      } catch (err: any) {
        console.error('Failed to start mining task:', err);
        const detail = err?.response?.data?.detail;
        const failMsg =
          typeof detail === 'string' && detail.trim()
            ? detail
            : (err?.message || '无法连接后端服务');
        // Set error state instead of falling back to mock data
        setMiningTask({
          taskId: '',
          status: 'failed',
          config,
          progress: {
            phase: 'parsing',
            currentRound: 0,
            totalRounds: config.maxRounds || 3,
            progress: 0,
            message: `启动失败: ${failMsg}`,
            timestamp: new Date().toISOString(),
          },
          logs: [{
            id: generateId(),
            timestamp: new Date().toISOString(),
            level: 'error' as const,
            message: `启动挖掘任务失败: ${failMsg}`,
          }],
          createdAt: new Date().toISOString(),
          updatedAt: new Date().toISOString(),
        });
      } finally {
        setMiningStarting(false);
      }
    },
    [bindMiningTransport],
  );

  // Public start mining
  const startMining = useCallback(
    (config: TaskConfig) => {
      startRealMining(config);
    },
    [startRealMining],
  );

  // Stop mining
  const stopMining = useCallback(async () => {
    if (!miningTask) return;
    // Clear the recursive setTimeout from connectMiningWs
    if (miningWsTimeoutRef.current) {
      clearTimeout(miningWsTimeoutRef.current);
      miningWsTimeoutRef.current = null;
    }
    miningWsRef.current?.close();
    miningWsRef.current = null;
    if (miningPollingRef.current) {
      clearInterval(miningPollingRef.current);
      miningPollingRef.current = null;
    }
    if (backendAvailable) {
      try {
        await apiCancelMining(miningTask.taskId);
      } catch {
        // ignore
      }
    }
    setMiningTask((miningTask ? { ...miningTask, status: 'failed' } : null));
  }, [miningTask, backendAvailable]);

  // Reset mining task
  const resetMiningTask = useCallback(() => {
    // Ensure stopped first
    if (miningWsTimeoutRef.current) {
      clearTimeout(miningWsTimeoutRef.current);
      miningWsTimeoutRef.current = null;
    }
    miningWsRef.current?.close();
    miningWsRef.current = null;
    if (miningPollingRef.current) {
      clearInterval(miningPollingRef.current);
      miningPollingRef.current = null;
    }
    setMiningTask(null);
    setMiningEquityCurve([]);
    setMiningDrawdownCurve([]);
    setMiningIcTimeSeries([]);
  }, []);

  // ==================================================================
  // BACKTEST
  // ==================================================================
  const [backtestTask, setBacktestTask] = useState<BacktestTask | null>(null);
  const [backtestLogs, setBacktestLogs] = useState<LogEntry[]>([]);

  const backtestWsRef = useRef<WebSocket | null>(null);
  const backtestPollingRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // WS handler for backtest
  // IMPORTANT: setBacktestLogs must NOT be inside setBacktestTask's updater function,
  // because React StrictMode double-invokes updater functions in development mode,
  // which would cause every log entry to be added twice.
  const handleBacktestWsMessage = useCallback((msg: WsMessage) => {
    if (!mountedRef.current) return;
    switch (msg.type) {
      case 'progress':
        setBacktestTask(((prev: BacktestTask | null) => {
          if (!prev) return prev;
          return { ...prev, progress: msg.data, updatedAt: new Date().toISOString() };
        }) as unknown as BacktestTask | null);
        break;
      case 'log':
        setBacktestLogs(((l: LogEntry[]) => [...l.slice(-499), msg.data as LogEntry]) as unknown as LogEntry[]);
        break;
      case 'metrics':
        setBacktestTask(((prev: BacktestTask | null) => {
          if (!prev) return prev;
          return { ...prev, metrics: msg.data, updatedAt: new Date().toISOString() };
        }) as unknown as BacktestTask | null);
        break;
      case 'result':
        setBacktestTask(((prev: BacktestTask | null) => {
          if (!prev) return prev;
          return {
            ...prev,
            status: msg.data.status === 'completed' ? 'completed' : 'failed',
            metrics: msg.data.metrics || prev.metrics,
            updatedAt: new Date().toISOString(),
          };
        }) as unknown as BacktestTask | null);
        break;
      case 'error':
        setBacktestTask(((prev: BacktestTask | null) => {
          if (!prev) return prev;
          return { ...prev, status: 'failed', updatedAt: new Date().toISOString() };
        }) as unknown as BacktestTask | null);
        break;
    }
  }, []);

  // Start backtest
  const startBacktestTask = useCallback(
    async (params: BacktestStartParams) => {
      setBacktestLogs([]);
      const resp = await apiStartBacktest(params);
      if (!resp.success || !resp.data) throw new Error(resp.error || 'Failed');

      const taskData = resp.data.task as unknown as BacktestTask;
      setBacktestTask(taskData);

      // WebSocket
      const ws = connectMiningWs(
        resp.data.taskId,
        handleBacktestWsMessage,
        () => {
          if (!mountedRef.current) return;
          getBacktestStatus(resp.data!.taskId).then((r) => {
            if (r.data?.task && mountedRef.current) setBacktestTask(r.data.task as unknown as BacktestTask);
          });
        },
      );
      backtestWsRef.current = ws;

      // Polling fallback
      backtestPollingRef.current = setInterval(async () => {
        if (!mountedRef.current) {
          clearInterval(backtestPollingRef.current!);
          backtestPollingRef.current = null;
          return;
        }
        try {
          const r = await getBacktestStatus(resp.data!.taskId);
          if (!mountedRef.current) return;
          if (r.data?.task) {
            const t = r.data.task as unknown as BacktestTask;

            // Always sync progress from polling (in case WS missed updates)
            setBacktestTask(((prev: BacktestTask | null) => {
              if (!prev) return t;
              return {
                ...prev,
                status: t.status,
                progress: t.progress || prev.progress,
                metrics: (t.metrics && Object.keys(t.metrics).length > 0) ? t.metrics : prev.metrics,
                updatedAt: t.updatedAt,
              };
            }) as unknown as BacktestTask | null);

            if (t.status === 'completed' || t.status === 'failed' || t.status === 'cancelled') {
              // Final update: sync task + logs from backend (in case WS missed some)
              setBacktestTask(t);
              if (t.logs && t.logs.length > 0) {
                setBacktestLogs(t.logs.slice(-500));
              }
              clearInterval(backtestPollingRef.current!);
              backtestPollingRef.current = null;
            }
          }
        } catch {
          // ignore
        }
      }, 5000);
    },
    [handleBacktestWsMessage],
  );

  // Stop backtest
  const stopBacktestTask = useCallback(async () => {
    if (!backtestTask) return;
    backtestWsRef.current?.close();
    backtestWsRef.current = null;
    if (backtestPollingRef.current) {
      clearInterval(backtestPollingRef.current);
      backtestPollingRef.current = null;
    }
    try {
      await apiCancelBacktest(backtestTask.taskId);
    } catch {
      // ignore
    }
    setBacktestTask((backtestTask ? { ...backtestTask, status: 'cancelled' } : null));
  }, [backtestTask]);

  // ==================================================================
  // Context value
  // ==================================================================
  const value: TaskContextValue = {
    backendAvailable,
    // Mining
    miningTask,
    miningStarting,
    miningStartSeq,
    miningEquityCurve,
    miningDrawdownCurve,
    miningIcTimeSeries,
    startMining,
    stopMining,
    resetMiningTask,

    // ---- Backtest ----
    backtestTask,
    backtestLogs,
    startBacktestTask,
    stopBacktestTask,
  };

  return <TaskContext.Provider value={value}>{children}</TaskContext.Provider>;
};

// ========================== Hook ==========================

export function useTaskContext(): TaskContextValue {
  const ctx = useContext(TaskContext);
  if (!ctx) throw new Error('useTaskContext must be used inside <TaskProvider>');
  return ctx;
}
