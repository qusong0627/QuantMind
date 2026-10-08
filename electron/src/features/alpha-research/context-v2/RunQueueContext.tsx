/**
 * RunQueueContext — 因子级后台动作队列（回测 / 物化），跨页共享。
 *
 * 挂载：AppRoot 内、TaskProvider 之内（挖掘结果区与因子库用同一份队列状态，
 * 切页不丢）。挂载点见 pages-v2/AppRoot.tsx。
 *
 * 物化完成判据（照抄 admin RdMinedMaterializePanel 的纪律，别改坏）：
 * - `running` 只由**服务端响应**点亮，start() 不许自己造 running——
 *   子进程冷启才拿锁，自造的 true 会把「运行→结束」的边提前吃完，
 *   整轮运行从界面上消失；
 * - 完成 = 先见服务端 true、再见 false 的那条沿；POST 成功后开 30s 启动宽限；
 * - 快照带 seq，迟到的旧响应不覆盖新快照。
 *
 * 回测队列：每因子 idle→queued→running→completed|failed|cancelled；
 * 并发 2；行级失败取 FastAPI detail 原文（404 归属 / 400 无代码必须可见）；
 * 后端对「已在跑」的重复提交回 200「回测已在进行中」，与正常启动同路处理。
 */

import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
} from 'react';
import {
  cancelBacktest as apiCancelBacktest,
  getBacktestStatus,
  startBacktest as apiStartBacktest,
} from '../services-v2/api';
import type { BacktestStartParams } from '../services-v2/api';
import {
  extractApiDetail,
  getMaterializeStatus,
  startMaterialize as apiStartMaterialize,
} from '../services-v2/materialize';
import type { MaterializeStartResult } from '../services-v2/materialize';

// ========================== 回测队列 ==========================

export type BacktestRunStatus =
  | 'idle'
  | 'queued'
  | 'running'
  | 'completed'
  | 'failed'
  | 'cancelled';

export interface BacktestRunEntry {
  status: BacktestRunStatus;
  /** 失败/取消原文（FastAPI detail 或服务端回包的 message） */
  error?: string;
  queuedAt?: string;
  finishedAt?: string;
}

export interface BacktestEnqueueOptions {
  universe?: string;
  dataSource?: 'qlib_bin' | 'h5';
  startDate?: string;
  endDate?: string;
  /** 行终结（completed/failed/cancelled）时回调（宿主页刷新清单/统计） */
  onSettled?: (factorId: string, entry: BacktestRunEntry) => void;
}

interface BacktestQueueValue {
  /** factorId → 行内运行状态；无条目 = idle */
  entries: Record<string, BacktestRunEntry>;
  /** 排队 + 运行中的因子数 */
  activeCount: number;
  enqueue: (factorIds: string[], opts?: BacktestEnqueueOptions) => void;
  cancel: (factorId: string) => Promise<void>;
  /** 把行内状态清回 idle（清单刷新/重选后调用；活动中的行不动） */
  reset: (factorIds: string[]) => void;
}

/** 回测并发上限（后端各回测独立子进程，两条并跑不互踩） */
const MAX_CONCURRENT_BACKTESTS = 2;
/** 行级状态轮询间隔（与 TaskContext.BACKTEST_POLL_MS 同节奏） */
const BACKTEST_QUEUE_POLL_MS = 2500;
/**
 * 轮询安全阀：后端子进程 600s 超时兜底写 failed，正常远早于此。
 * 60min 仍未终结 = 链路出了问题，停轮询并如实报「未收到终态」，不装死。
 */
const BACKTEST_POLL_TIMEOUT_MS = 60 * 60 * 1000;

// ========================== 物化运行 ==========================

interface MaterializeRunValue {
  /** 服务端全局锁状态（只信服务端） */
  running: boolean;
  /** 本轮启动包含的因子（运行中叠加「物化中」chip；结束即清） */
  runningIds: ReadonlySet<string>;
  /** 最近一次启动的服务端明细（可物化/跳过/拒绝），供物化条展示 */
  lastResult: MaterializeStartResult | null;
  /** 启动或轮询失败的可见告警（成功一次即清） */
  warning: string | null;
  /**
   * 启动一次物化。400（非法 id）/409（锁忙）/5xx 一律抛原文，调用方上屏；
   * 409 时顺带同步一次服务端状态（可能是管理员在跑，界面要如实点亮）。
   */
  start: (
    factorIds: string[],
    opts?: { force?: boolean; onCompleted?: () => void },
  ) => Promise<MaterializeStartResult>;
  /** 主动同步一次服务端快照（空列表不查询——端点要求 ≥1 个 id） */
  refresh: (factorIds: string[]) => Promise<void>;
  clearResult: () => void;
}

/** 运行中轮询间隔：物化以分钟/因子推进，5s 足够跟进度 */
const MATERIALIZE_POLL_MS = 5_000;
/** 启动宽限：POST 成功后先看到 false 不算结束（子进程冷启拿锁 ~4s） */
const MATERIALIZE_START_GRACE_MS = 30_000;
/** 静默轮询降级提示（恢复后自动清除；不得与启动失败告警互相覆盖） */
const POLL_DEGRADED_WARNING = '物化状态刷新失败，面板可能滞后（将自动重试）';

interface RunQueueValue {
  backtest: BacktestQueueValue;
  materialize: MaterializeRunValue;
}

const RunQueueContext = createContext<RunQueueValue | null>(null);

export const RunQueueProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  // ==================================================================
  // 回测队列
  // ==================================================================
  const [btEntries, setBtEntries] = useState<Record<string, BacktestRunEntry>>({});
  /** 排队或运行中的 id（入队去重、行终结判定共用） */
  const btActiveIdsRef = useRef<Set<string>>(new Set());
  const btPendingRef = useRef<Array<{ factorId: string; opts: BacktestEnqueueOptions }>>([]);
  /** 已启动未终结的 id（占用并发槽） */
  const btInFlightRef = useRef<Set<string>>(new Set());
  const btIntervalsRef = useRef<Map<string, ReturnType<typeof setInterval>>>(new Map());
  const pumpRef = useRef<() => void>(() => {});

  /** 行终结的唯一出口：先出在途/排队账，再落状态、回调、补位。幂等。 */
  const finalizeBacktest = useCallback(
    (
      factorId: string,
      entry: BacktestRunEntry,
      onSettled?: BacktestEnqueueOptions['onSettled'],
    ) => {
      const wasPending = btPendingRef.current.some((j) => j.factorId === factorId);
      if (wasPending) {
        btPendingRef.current = btPendingRef.current.filter((j) => j.factorId !== factorId);
      }
      const wasInFlight = btInFlightRef.current.delete(factorId);
      if (!wasPending && !wasInFlight && !btActiveIdsRef.current.has(factorId)) return;
      btActiveIdsRef.current.delete(factorId);
      const interval = btIntervalsRef.current.get(factorId);
      if (interval) {
        clearInterval(interval);
        btIntervalsRef.current.delete(factorId);
      }
      if (mountedRef.current) {
        setBtEntries((prev) => ({ ...prev, [factorId]: entry }));
      }
      onSettled?.(factorId, entry);
      pumpRef.current();
    },
    [],
  );

  const startBacktestJob = useCallback(
    (job: { factorId: string; opts: BacktestEnqueueOptions }) => {
      const { factorId, opts } = job;
      btInFlightRef.current.add(factorId);
      if (mountedRef.current) {
        setBtEntries((prev) => ({
          ...prev,
          [factorId]: { ...prev[factorId], status: 'running' },
        }));
      }

      const params: BacktestStartParams = {
        factorId,
        universe: opts.universe,
        dataSource: opts.dataSource,
        startDate: opts.startDate,
        endDate: opts.endDate,
      };

      apiStartBacktest(params)
        .then((resp) => {
          if (!mountedRef.current) return;
          if (!resp.success || !resp.data) {
            finalizeBacktest(
              factorId,
              {
                status: 'failed',
                error: resp.error || '回测启动失败',
                finishedAt: new Date().toISOString(),
              },
              opts.onSettled,
            );
            return;
          }
          // 启动响应在途时被取消：本地已结算——补一枪后端取消（本地的 cancel
          // 请求可能先于后端登记到达而空放），且不注册轮询
          if (!btActiveIdsRef.current.has(factorId)) {
            void apiCancelBacktest(factorId).catch(() => {});
            return;
          }
          // 真状态只在 factor 行上（GET /factors/{id}）：后端 POST 已把
          // status 写成 backtesting，重复提交回 200「回测已在进行中」——同路。
          const startedAt = Date.now();
          const interval = setInterval(async () => {
            if (!mountedRef.current) {
              const iv = btIntervalsRef.current.get(factorId);
              if (iv) clearInterval(iv);
              btIntervalsRef.current.delete(factorId);
              return;
            }
            if (Date.now() - startedAt > BACKTEST_POLL_TIMEOUT_MS) {
              finalizeBacktest(
                factorId,
                {
                  status: 'failed',
                  error: '长时间未收到回测终态（已停止轮询，请刷新查看）',
                  finishedAt: new Date().toISOString(),
                },
                opts.onSettled,
              );
              return;
            }
            try {
              const r = await getBacktestStatus(factorId);
              if (!mountedRef.current) return;
              const t = r.data?.task;
              if (!t) return;
              // idle：POST 后尚未写入 backtesting 的窗口（正常不应出现）；非终态
              if (t.status === 'idle' || t.status === 'running') return;
              finalizeBacktest(
                factorId,
                t.status === 'completed'
                  ? { status: 'completed', finishedAt: new Date().toISOString() }
                  : {
                      status: 'failed',
                      error: r.data?.error || '回测失败',
                      finishedAt: new Date().toISOString(),
                    },
                opts.onSettled,
              );
            } catch {
              // transient — 继续轮询
            }
          }, BACKTEST_QUEUE_POLL_MS);
          btIntervalsRef.current.set(factorId, interval);
        })
        .catch((err) => {
          finalizeBacktest(
            factorId,
            {
              status: 'failed',
              error: extractApiDetail(err, '回测启动失败'),
              finishedAt: new Date().toISOString(),
            },
            opts.onSettled,
          );
        });
    },
    [finalizeBacktest],
  );

  const pumpBacktestQueue = useCallback(() => {
    while (
      btInFlightRef.current.size < MAX_CONCURRENT_BACKTESTS &&
      btPendingRef.current.length > 0
    ) {
      const job = btPendingRef.current.shift();
      if (!job) break;
      startBacktestJob(job);
    }
  }, [startBacktestJob]);
  pumpRef.current = pumpBacktestQueue;

  const enqueueBacktests = useCallback(
    (factorIds: string[], opts: BacktestEnqueueOptions = {}) => {
      const fresh = [...new Set(factorIds.filter(Boolean))].filter(
        (id) => !btActiveIdsRef.current.has(id),
      );
      if (fresh.length === 0) return;
      const queuedAt = new Date().toISOString();
      for (const factorId of fresh) {
        btActiveIdsRef.current.add(factorId);
        btPendingRef.current.push({ factorId, opts });
      }
      setBtEntries((prev) => {
        const next = { ...prev };
        for (const factorId of fresh) {
          next[factorId] = { status: 'queued', queuedAt };
        }
        return next;
      });
      pumpBacktestQueue();
    },
    [pumpBacktestQueue],
  );

  const cancelBacktestRun = useCallback(
    async (factorId: string) => {
      // 排队中：出队即取消（后端从未启动，无需调 cancel 端点）
      const wasPending = btPendingRef.current.some((j) => j.factorId === factorId);
      if (wasPending) {
        finalizeBacktest(factorId, {
          status: 'cancelled',
          finishedAt: new Date().toISOString(),
        });
        return;
      }
      if (!btInFlightRef.current.has(factorId)) return;
      finalizeBacktest(factorId, {
        status: 'cancelled',
        finishedAt: new Date().toISOString(),
      });
      try {
        await apiCancelBacktest(factorId);
      } catch {
        // 后端可能刚好已终结——本地已是 cancelled，不覆盖
      }
    },
    [finalizeBacktest],
  );

  const resetBacktestRuns = useCallback((factorIds: string[]) => {
    setBtEntries((prev) => {
      const next = { ...prev };
      for (const id of factorIds) {
        if (!btActiveIdsRef.current.has(id)) delete next[id];
      }
      return next;
    });
  }, []);

  // ==================================================================
  // 物化运行
  // ==================================================================
  const [matRunning, setMatRunning] = useState(false);
  const [matGrace, setMatGrace] = useState(false);
  const [matRunningIds, setMatRunningIds] = useState<ReadonlySet<string>>(new Set());
  const [matLastResult, setMatLastResult] = useState<MaterializeStartResult | null>(null);
  const [matWarning, setMatWarning] = useState<string | null>(null);
  const matSeqRef = useRef(0);
  const matConfirmedRunningRef = useRef(false);
  const matGraceTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  /** 当前轮询的因子 id（start 成功时设定；409 时除外——那是别人的运行） */
  const matIdsRef = useRef<string[]>([]);
  const matOnCompletedRef = useRef<(() => void) | null>(null);
  const matPollErrorsRef = useRef(0);

  useEffect(
    () => () => {
      if (matGraceTimerRef.current) {
        clearTimeout(matGraceTimerRef.current);
        matGraceTimerRef.current = null;
      }
      for (const interval of btIntervalsRef.current.values()) {
        clearInterval(interval);
      }
      btIntervalsRef.current.clear();
    },
    [],
  );

  const clearMatGrace = useCallback(() => {
    if (matGraceTimerRef.current) {
      clearTimeout(matGraceTimerRef.current);
      matGraceTimerRef.current = null;
    }
    setMatGrace(false);
  }, []);

  const fetchMaterializeStatus = useCallback(
    async (ids: string[], silent: boolean) => {
      if (ids.length === 0) return;
      const seq = ++matSeqRef.current; // 迟到的旧响应不许覆盖新快照
      try {
        const next = await getMaterializeStatus(ids);
        if (seq !== matSeqRef.current || !mountedRef.current) return;
        matPollErrorsRef.current = 0;
        // 成功刷新只负责清掉「轮询降级」这一条；启动失败（409 锁忙/400 非法 id）
        // 的原文要留在屏上——409 路径紧接着会做一次状态同步，若这里无差别
        // setMatWarning(null)，用户看到的锁忙原因会被自己的同步请求抹掉。
        setMatWarning((prev) => {
          if (!silent) return null; // 用户主动刷新：清一切
          return prev === POLL_DEGRADED_WARNING ? null : prev;
        });
        const finished = matConfirmedRunningRef.current && !next.running;
        matConfirmedRunningRef.current = next.running;
        if (next.running) clearMatGrace();
        setMatRunning(next.running);
        if (finished) {
          setMatRunningIds(new Set());
          const cb = matOnCompletedRef.current;
          matOnCompletedRef.current = null;
          cb?.();
        }
      } catch (err) {
        if (seq !== matSeqRef.current || !mountedRef.current) return;
        if (!silent) {
          setMatWarning(extractApiDetail(err, '物化状态加载失败'));
        } else {
          // 静默轮询失败不能让界面假装正常：第一次可见告警，之后交给重试
          matPollErrorsRef.current += 1;
          if (matPollErrorsRef.current === 1) {
            setMatWarning(POLL_DEGRADED_WARNING);
          }
        }
      }
    },
    [clearMatGrace],
  );

  // 运行中（或启动宽限期内）才轮询；结束即停（完成沿由 fetch 判定）
  useEffect(() => {
    if ((!matRunning && !matGrace) || matIdsRef.current.length === 0) return undefined;
    const timer = setInterval(() => {
      void fetchMaterializeStatus(matIdsRef.current, true);
    }, MATERIALIZE_POLL_MS);
    return () => clearInterval(timer);
  }, [matRunning, matGrace, fetchMaterializeStatus]);

  const startMaterializeRun = useCallback(
    async (
      factorIds: string[],
      opts: { force?: boolean; onCompleted?: () => void } = {},
    ): Promise<MaterializeStartResult> => {
      try {
        const result = await apiStartMaterialize(factorIds, { force: opts.force });
        if (!mountedRef.current) return result;
        setMatLastResult(result);
        setMatWarning(null);
        if (result.started) {
          matIdsRef.current =
            result.materializable.length > 0 ? result.materializable : factorIds;
          matOnCompletedRef.current = opts.onCompleted ?? null;
          setMatRunningIds(new Set(matIdsRef.current));
          // 服务端回包前已确认子进程持锁；宽限期只兜「确认→首次探测」残余窗口，
          // 期间看到 false 不算结束（真正的结束必须服务端确认过 true）
          matConfirmedRunningRef.current = false;
          setMatGrace(true);
          if (matGraceTimerRef.current) clearTimeout(matGraceTimerRef.current);
          matGraceTimerRef.current = setTimeout(() => {
            matGraceTimerRef.current = null;
            setMatGrace(false);
          }, MATERIALIZE_START_GRACE_MS);
          void fetchMaterializeStatus(matIdsRef.current, true);
        }
        return result;
      } catch (err) {
        if (mountedRef.current) {
          const detail = extractApiDetail(err, '启动物化失败');
          setMatWarning(detail);
          // 409 锁忙：同步服务端状态让「运行中」如实点亮（可能是管理员在跑），
          // 并登记轮询 id 与完成回调——结束沿仍要能被我们观察到
          if ((err as any)?.response?.status === 409) {
            const ids = factorIds.slice(0, 100);
            matIdsRef.current = ids;
            matOnCompletedRef.current = opts.onCompleted ?? null;
            void fetchMaterializeStatus(ids, true);
          }
        }
        throw err;
      }
    },
    [fetchMaterializeStatus],
  );

  const refreshMaterialize = useCallback(
    async (ids: string[]) => {
      await fetchMaterializeStatus(ids, false);
    },
    [fetchMaterializeStatus],
  );

  const clearMaterializeResult = useCallback(() => setMatLastResult(null), []);

  // ==================================================================
  // Context value
  // ==================================================================
  const value: RunQueueValue = {
    backtest: {
      entries: btEntries,
      activeCount: Object.values(btEntries).filter(
        (e) => e.status === 'queued' || e.status === 'running',
      ).length,
      enqueue: enqueueBacktests,
      cancel: cancelBacktestRun,
      reset: resetBacktestRuns,
    },
    materialize: {
      running: matRunning,
      runningIds: matRunningIds,
      lastResult: matLastResult,
      warning: matWarning,
      start: startMaterializeRun,
      refresh: refreshMaterialize,
      clearResult: clearMaterializeResult,
    },
  };

  return <RunQueueContext.Provider value={value}>{children}</RunQueueContext.Provider>;
};

// ========================== Hooks ==========================

function useRunQueueContext(): RunQueueValue {
  const ctx = useContext(RunQueueContext);
  if (!ctx) throw new Error('useRunQueue* 必须在 <RunQueueProvider> 内使用');
  return ctx;
}

export function useBacktestQueue(): BacktestQueueValue {
  return useRunQueueContext().backtest;
}

export function useMaterializeRun(): MaterializeRunValue {
  return useRunQueueContext().materialize;
}
