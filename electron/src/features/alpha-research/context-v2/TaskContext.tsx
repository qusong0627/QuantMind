/**
 * TaskContext — Global Task State Management
 *
 * Lifts mining and backtest task state, WebSocket connection, and polling logic
 * to App level, so running state is not lost when switching pages.
 */

import React, { createContext, useContext, useState, useCallback, useRef, useEffect } from 'react';
import type {
  Factor,
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
  getFactors,
  normalizeAgentFactor,
  emptyMetrics,
  FACTOR_LIST_MAX_LIMIT,
  healthCheck,
} from '../services-v2/api';
import type { BacktestStartParams } from '../services-v2/api';
import { getDefaultMiningDirection, getStoredDirectionConfig } from '../utils-v2/miningDirections';

/** 回测状态轮询间隔（后端回测为分钟级，2.5s 足够且不压库） */
const BACKTEST_POLL_MS = 2500;

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
 * 把后端返回的结构化因子（rd_agent_factors 已落库数据）合并进实时指标。
 *
 * - 全量合并：挖到多少显示多少（旧实现 slice(0,10) 是双层截断之一）；
 * - 按 factorId 去重，后到覆盖（同一因子的物化/回测状态会更新）；
 * - 走 normalizeAgentFactor：缺失指标保持 undefined（界面显「—」），禁止 `?? 0`；
 * - 质量计数由清单现算（纯派生，不伪造）；
 * - 头条指标 = 全清单里 RankIC 最优因子（没有可比的 RankIC 就留 undefined）。
 */
function mergeTaskFactors(
  metrics: RealtimeMetrics | undefined,
  rawFactors: any[],
): RealtimeMetrics {
  const base: RealtimeMetrics = metrics ?? emptyMetrics();
  const incoming = rawFactors
    .map((raw) => normalizeAgentFactor(raw))
    .filter((f) => f.factorId);
  if (incoming.length === 0) return base;

  const byId = new Map<string, Factor>();
  for (const f of base.factors ?? []) byId.set(f.factorId, f);
  for (const f of incoming) {
    const prev = byId.get(f.factorId);
    byId.set(f.factorId, prev ? { ...prev, ...f } : f);
  }
  const factors = [...byId.values()];

  let best: Factor | undefined;
  for (const f of factors) {
    if (f.rankIc == null) continue;
    if (!best || f.rankIc > (best.rankIc as number)) best = f;
  }

  return {
    ...base,
    totalFactors: factors.length,
    highQualityFactors: factors.filter((f) => f.quality === 'high').length,
    mediumQualityFactors: factors.filter((f) => f.quality === 'medium').length,
    lowQualityFactors: factors.filter((f) => f.quality === 'low').length,
    factors,
    factorName: best?.factorName,
    rankIc: best?.rankIc,
    rankIcir: best?.rankIcir,
    ic: best?.ic,
    icir: best?.icir,
    annualReturn: best?.annualReturn,
    sharpeRatio: best?.sharpeRatio,
    maxDrawdown: best?.maxDrawdown,
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
  /**
   * 拉取当前挖掘任务的**权威全量**因子清单（GET /factors?task_id=…&limit=500），
   * 覆盖 /tasks 载荷的 20 条上限。任务完成沿自动调用；物化/回测结束后可手动调用。
   */
  refreshMiningFactors: (taskId?: string) => Promise<void>;

  // ---- Backtest ----
  backtestTask: BacktestTask | null;
  backtestLogs: LogEntry[];
  startBacktestTask: (params: BacktestStartParams) => Promise<void>;
  /** 「查看回测」：把某因子的既有回测载入回测页（不重跑；running 则续轮询） */
  attachBacktestTask: (factorId: string) => Promise<void>;
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
            // 结构化因子（后端已落库，走 normalizeAgentFactor 全量合并）
            if (Array.isArray(msg.data.factors) && msg.data.factors.length > 0) {
              updated.metrics = mergeTaskFactors(updated.metrics, msg.data.factors);
            }
            break;
          case 'log':
            // 只追加日志行。**不要**再把日志文本正则解析成假因子行：
            // 旧实现用 generateId() 造的行没有真实 factor_id，无法回测/物化，
            // 属纯幻觉数据（真实清单由 progress/refresh 从数据库带上来）。
            updated.logs = [...(updated.logs || []).slice(-2000), msg.data as LogEntry];
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

  // 权威全量清单刷新：/tasks 载荷只有最新 20 条，「挖到多少显示多少」必须以
  // GET /factors?task_id=…（limit=500）为准。合并失败不致命——轮询仍在跑。
  const refreshMiningFactors = useCallback(async (taskId?: string) => {
    const id = taskId ?? miningTaskRef.current?.taskId;
    if (!id) return;
    try {
      const r = await getFactors({ taskId: id, limit: FACTOR_LIST_MAX_LIMIT });
      if (!mountedRef.current || !r.success || !r.data) return;
      const rows = r.data.factors ?? [];
      setMiningTask((prev) => {
        if (!prev || prev.taskId !== id) return prev;
        return { ...prev, metrics: mergeTaskFactors(prev.metrics, rows) };
      });
    } catch (err) {
      console.error('[alpha-research] refresh mining factors failed:', err);
    }
  }, []);

  // 任务完成沿自动拉一次全量清单（覆盖：新任务完成、恢复出的已完成历史任务）
  const factorsRefreshedForTaskRef = useRef<string | null>(null);
  useEffect(() => {
    const t = miningTask;
    if (!t || !t.taskId || t.status !== 'completed') return;
    if (factorsRefreshedForTaskRef.current === t.taskId) return;
    factorsRefreshedForTaskRef.current = t.taskId;
    void refreshMiningFactors(t.taskId);
  }, [miningTask, refreshMiningFactors]);

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
        // 新任务从零开始：清掉任何残留清单与 IC 族头条（缺失=undefined→界面显「—」；
        // 旧实现只清 top10Factors，IC 族残留 0 值，统计卡永远显示 0.0000）
        taskData.metrics = emptyMetrics();
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

  const backtestPollingRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const stopBacktestPolling = useCallback(() => {
    if (backtestPollingRef.current) {
      clearInterval(backtestPollingRef.current);
      backtestPollingRef.current = null;
    }
  }, []);

  /**
   * 拉一次因子回测状态并同步进 backtestTask/backtestLogs。
   * 失败原文（ownerless 404 / 无代码 400 / metadata.backtest_error）进日志面板。
   * 防串台：当前查看的是别的因子时不覆盖（并发提交时旧轮询的在途 tick）。
   */
  const syncBacktestOnce = useCallback(
    async (factorId: string): Promise<BacktestTask | null> => {
      const r = await getBacktestStatus(factorId);
      if (!mountedRef.current) return null;
      const t = (r.data?.task ?? null) as unknown as BacktestTask | null;
      if (!t) return null;
      const errText = r.data?.error;
      setBacktestTask((prev) => {
        if (prev && prev.taskId !== factorId) return prev;
        const base = prev ?? t;
        return {
          ...base,
          status: t.status,
          progress: t.progress || base.progress,
          metrics:
            t.metrics && Object.keys(t.metrics).length > 0 ? t.metrics : base.metrics,
          updatedAt: t.updatedAt,
        };
      });
      if (t.status === 'failed' && errText) {
        setBacktestLogs((logs) => {
          const id = `bt-err-${factorId}`;
          if (logs.some((l) => l.id === id)) return logs; // 幂等：并发 tick 不重复
          return [
            ...logs.slice(-499),
            { id, timestamp: new Date().toISOString(), level: 'error' as const, message: errText },
          ];
        });
      }
      return t;
    },
    [],
  );

  /**
   * 2.5s 轮询直到终态。旧实现的「回测 WebSocket」是假的：connectMiningWs(taskId
   * =factor_id) 实际轮询 /tasks/{factor_id}（factor_id 不是任务 id）恒 404 ——
   * 点了回测什么都没发生。真回测状态只在 GET /factors/{id} 的 factor 行上。
   */
  const bindBacktestPolling = useCallback(
    (factorId: string) => {
      stopBacktestPolling();
      backtestPollingRef.current = setInterval(async () => {
        if (!mountedRef.current) {
          stopBacktestPolling();
          return;
        }
        try {
          const t = await syncBacktestOnce(factorId);
          if (
            t &&
            (t.status === 'completed' || t.status === 'failed' || t.status === 'cancelled')
          ) {
            stopBacktestPolling();
          }
        } catch {
          // transient — keep polling
        }
      }, BACKTEST_POLL_MS);
    },
    [stopBacktestPolling, syncBacktestOnce],
  );

  // Start backtest
  const startBacktestTask = useCallback(
    async (params: BacktestStartParams) => {
      setBacktestLogs([]);
      const resp = await apiStartBacktest(params);
      if (!resp.success || !resp.data) throw new Error(resp.error || 'Failed');

      const taskData = resp.data.task as unknown as BacktestTask;
      setBacktestTask(taskData);

      const factorId = (resp.data.taskId as string) || params.factorId;
      bindBacktestPolling(factorId);
      // 立刻探一次拿到服务端原文（重复提交会得到「回测已在进行中」）
      void syncBacktestOnce(factorId).catch(() => {});
    },
    [bindBacktestPolling, syncBacktestOnce],
  );

  // 「查看回测」：不重跑，只把既有状态/指标载入回测页；running 则续轮询
  const attachBacktestTask = useCallback(
    async (factorId: string) => {
      setBacktestLogs([]);
      stopBacktestPolling(); // 先停旧轮询，避免旧 tick 与新查看对象竞争
      const r = await getBacktestStatus(factorId);
      if (!mountedRef.current) return;
      const t = (r.data?.task ?? null) as unknown as BacktestTask | null;
      if (!t) return;
      setBacktestTask(t); // 显式切换查看对象（不走 syncBacktestOnce 的防串台守卫）
      const errText = r.data?.error;
      if (t.status === 'failed' && errText) {
        setBacktestLogs([
          {
            id: `bt-err-${factorId}`,
            timestamp: new Date().toISOString(),
            level: 'error',
            message: errText,
          },
        ]);
      }
      if (t.status === 'running') {
        bindBacktestPolling(factorId);
      }
    },
    [bindBacktestPolling, stopBacktestPolling],
  );

  // Stop backtest
  const stopBacktestTask = useCallback(async () => {
    if (!backtestTask) return;
    stopBacktestPolling();
    try {
      await apiCancelBacktest(backtestTask.taskId);
    } catch {
      // ignore
    }
    setBacktestTask((prev) => (prev ? { ...prev, status: 'cancelled' } : null));
  }, [backtestTask, stopBacktestPolling]);

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
    refreshMiningFactors,

    // ---- Backtest ----
    backtestTask,
    backtestLogs,
    startBacktestTask,
    attachBacktestTask,
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
