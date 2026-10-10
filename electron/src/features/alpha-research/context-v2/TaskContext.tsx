/**
 * TaskContext — Global Task State Management
 *
 * Lifts mining and backtest task state, WebSocket connection, and polling logic
 * to App level, so running state is not lost when switching pages.
 */

import React, {
  createContext,
  useContext,
  useState,
  useCallback,
  useRef,
  useEffect,
  useMemo,
} from 'react';
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

  // ---- Mining（多任务注册表：可多条并存，相互独立）----
  /** 聚焦任务（= miningTasks 里 focusedTaskId 指向的一条）；演化台展示对象 */
  miningTask: Task | null;
  /** 注册表全量（按启动先后排序）：运行中的、本会话已跑完的都在这里 */
  miningTasks: Task[];
  /** 当前聚焦任务 id（演化台/因子清单跟随它） */
  focusedTaskId: string | null;
  /** 切换聚焦任务（认不出的 id 忽略，不产生悬空焦点） */
  focusMiningTask: (taskId: string) => void;
  /**
   * 接纳一批已派发任务（拆解面板批量派发回执）：逐条入库并绑定传输，
   * 焦点给该批最后一条。**不**触发「开始后自动进演化台」——那 bump
   * miningStartSeq 的待遇只属于用户在输入框亲手提交的单条任务。
   */
  adoptDispatchedTasks: (tasks: Task[]) => void;
  /** POST /evolve 提交进行中（后端同步建缓存时可能耗时较长） */
  miningStarting: boolean;
  /** 最近一次提交失败的原文（429 并发上限 / 建缓存失败等）；新提交时清空 */
  miningStartError: string | null;
  dismissMiningStartError: () => void;
  /** 用户主动开始挖掘的序号（仅用于「开始后自动进入演化台」，恢复历史任务不触发） */
  miningStartSeq: number;
  miningEquityCurve: TimeSeriesData[];
  miningDrawdownCurve: TimeSeriesData[];
  miningIcTimeSeries: TimeSeriesData[];
  startMining: (config: TaskConfig) => void;
  /** 停止指定任务（缺省=聚焦任务）：只断该任务的传输与后端任务，互不影响 */
  stopMining: (taskId?: string) => Promise<void>;
  /** 从注册表移除指定任务（缺省=聚焦任务）；只做前端清场，不触发后端取消 */
  resetMiningTask: (taskId?: string) => void;
  /**
   * 拉取某挖掘任务的**权威全量**因子清单（GET /factors?task_id=…&limit=500），
   * 覆盖 /tasks 载荷的 20 条上限。缺省=聚焦任务；任务完成沿自动调用；
   * 物化/回测结束后可手动调用。
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

/** 每个挖掘任务一套传输句柄（伪 WS 轮询 + 10s 兜底轮询），按 taskId 隔离 */
interface MiningTransport {
  ws: WebSocket;
  pollingId: ReturnType<typeof setInterval>;
}

/**
 * 曲线占位：这三条曲线从没有任何代码写入过（历史遗留字段），消费者拿到的一直是
 * 空数组。保留接口字段不破坏消费者，但不再用 state 假装它会变。
 */
const EMPTY_SERIES: TimeSeriesData[] = [];

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
  // MINING（多任务注册表：任务相互独立，可并行）
  // ==================================================================
  const [miningTasks, setMiningTasks] = useState<Task[]>([]);
  const [focusedTaskId, setFocusedTaskId] = useState<string | null>(null);
  const [miningStartError, setMiningStartError] = useState<string | null>(null);
  // 提交锁：POST /evolve 在途时禁止重复提交；已有任务运行**不**拦（多任务）
  const [miningStarting, setMiningStarting] = useState(false);
  const [miningStartSeq, setMiningStartSeq] = useState(0);
  const miningStartSeqRef = useRef(0);

  // 聚焦任务派生：演化台/因子清单等既有消费者继续只读 miningTask
  const miningTask = useMemo(
    () => miningTasks.find((t) => t.taskId === focusedTaskId) ?? null,
    [miningTasks, focusedTaskId],
  );

  const transportsRef = useRef<Map<string, MiningTransport>>(new Map());
  const mountedRef = useRef(true);
  // 同步到 ref 供 startRealMining / 恢复效应闭包内读取，避免 stale state
  const miningStartingRef = useRef(false);
  const miningTasksRef = useRef<Task[]>([]);
  const focusedTaskIdRef = useRef<string | null>(null);
  useEffect(() => {
    miningStartingRef.current = miningStarting;
  }, [miningStarting]);
  useEffect(() => {
    miningTasksRef.current = miningTasks;
  }, [miningTasks]);
  useEffect(() => {
    focusedTaskIdRef.current = focusedTaskId;
  }, [focusedTaskId]);

  /**
   * 拆掉某任务的实时传输。先摘注册表再 close：伪 WS 的 close() 会触发 onClose，
   * 而 onClose 只在「传输仍在注册表」时才做终态权威对齐——主动拆除（停止/重置/
   * 卸载）后不该再拉一次可能晚于取消请求的旧状态。
   * connectMiningWs 的递归 setTimeout 每轮都会改写 _pollingTimeoutId，必须现场
   * 读取（bind 时缓存的值会过期，clearTimeout 变成空操作）。
   */
  const teardownMiningTransport = useCallback((taskId: string) => {
    const transport = transportsRef.current.get(taskId);
    if (!transport) return;
    transportsRef.current.delete(taskId);
    const pending = (transport.ws as any)?._pollingTimeoutId;
    if (pending) clearTimeout(pending);
    transport.ws.close();
    if (transport.pollingId) clearInterval(transport.pollingId);
  }, []);

  // Cleanup on unmount: tear down every task transport (pseudo-WS recursive
  // timers + fallback intervals) and prevent stale state updates.
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      for (const id of [...transportsRef.current.keys()]) {
        teardownMiningTransport(id);
      }
      // Clear backtest polling
      if (backtestPollingRef.current) {
        clearInterval(backtestPollingRef.current);
        backtestPollingRef.current = null;
      }
    };
  }, [teardownMiningTransport]);

  /** 按任务更新注册表（任务已被 reset 移除时静默忽略） */
  const patchMiningTask = useCallback((taskId: string, updater: (prev: Task) => Task) => {
    setMiningTasks((prev) => prev.map((t) => (t.taskId === taskId ? updater(t) : t)));
  }, []);

  /** 插入或整体替换某任务（新任务提交、恢复、终态权威对齐共用） */
  const upsertMiningTask = useCallback((task: Task) => {
    setMiningTasks((prev) => {
      const idx = prev.findIndex((t) => t.taskId === task.taskId);
      if (idx === -1) return [...prev, task];
      const next = [...prev];
      next[idx] = task;
      return next;
    });
  }, []);

  // WS handler for mining（消息按 taskId 路由到对应任务，多任务互不串台）
  const handleMiningWsMessage = useCallback(
    (taskId: string, msg: WsMessage) => {
      if (!mountedRef.current) return;
      patchMiningTask(taskId, (prev) => {
        const updated = { ...prev };
        switch (msg.type) {
          case 'progress':
            updated.progress = msg.data;
            // 状态以传输携带的后端原始状态为准：queued 不是 running——
            // 批量派发的排队任务绑定伪 WS 后，首条 progress 曾把排队行
            // 直接翻成运行中（位次显示随之消失）
            updated.status =
              msg.data.phase === 'completed'
                ? 'completed'
                : msg.data.status === 'queued'
                  ? 'queued'
                  : 'running';
            updated.queuePosition =
              updated.status === 'queued' && typeof msg.data.queuePosition === 'number'
                ? msg.data.queuePosition
                : null;
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
      });
    },
    [patchMiningTask],
  );

  // 绑定某任务的实时传输（伪 WS + 轮询兜底），供新任务与恢复共用；同 id 重绑先拆旧
  const bindMiningTransport = useCallback(
    (taskId: string) => {
      teardownMiningTransport(taskId);
      const ws = connectMiningWs(
        taskId,
        (msg) => handleMiningWsMessage(taskId, msg),
        () => {
          // 传输自然终结（轮询到终态）→ 用后端权威任务行对齐一次；
          // 主动拆除已先从注册表摘除，跳过这次对齐（避免与取消请求竞态）
          if (!mountedRef.current || !transportsRef.current.has(taskId)) return;
          getMiningStatus(taskId)
            .then((r) => {
              if (r.data?.task && mountedRef.current) {
                const authoritative = r.data.task as Task;
                patchMiningTask(taskId, () => authoritative);
              }
            })
            .catch(() => {});
        },
      );
      const pollingId = setInterval(async () => {
        if (!mountedRef.current) return;
        try {
          const r = await getMiningStatus(taskId);
          if (!mountedRef.current || !r.data?.task) return;
          const t = r.data.task as Task;
          const terminal = t.status === 'completed' || t.status === 'failed';
          // 兜底轮询只在「状态/排队位次变化」或终态时权威对齐：排队期
          // 每 10s 无条件 upsert 会用 /tasks 的 20 条载荷覆盖掉 2s 伪 WS
          // 刚送进来的完整进度；位次前移与 queued→running 仍由这里兜住
          const local = miningTasksRef.current.find((x) => x.taskId === taskId);
          const changed =
            t.status !== local?.status ||
            (t.queuePosition ?? null) !== (local?.queuePosition ?? null);
          if (changed || terminal) upsertMiningTask(t);
          if (terminal) teardownMiningTransport(taskId);
        } catch {
          // ignore
        }
      }, 10000);
      transportsRef.current.set(taskId, { ws, pollingId });
    },
    [handleMiningWsMessage, teardownMiningTransport, patchMiningTask, upsertMiningTask],
  );

  // 恢复：刷新/离开再回来时，把后端仍在运行/排队的全部挖掘任务接管回来
  // （逐条绑定传输；焦点给最新一条）。历史已完成任务走「挖掘历史」页，不在这里铺。
  const miningRecoveredRef = useRef(false);
  useEffect(() => {
    if (miningRecoveredRef.current) return;
    miningRecoveredRef.current = true;
    listTasks()
      .then((r) => {
        if (!mountedRef.current) return;
        // 用户可能在请求在途时已自己提交了任务——注册表非空就不恢复（不夺焦点）
        if (miningTasksRef.current.length > 0) return;
        const adoptable = (r.data?.tasks ?? []).filter(
          (t) => t.status === 'running' || t.status === 'queued',
        );
        if (adoptable.length === 0) return;
        setMiningTasks(adoptable);
        for (const t of adoptable) bindMiningTransport(t.taskId);
        const newest = adoptable.reduce((a, b) =>
          Date.parse(b.createdAt) >= Date.parse(a.createdAt) ? b : a,
        );
        setFocusedTaskId(newest.taskId);
      })
      .catch(() => {});
  }, [bindMiningTransport]);

  // 权威全量清单刷新：/tasks 载荷只有最新 20 条，「挖到多少显示多少」必须以
  // GET /factors?task_id=…（limit=500）为准。合并失败不致命——轮询仍在跑。
  const refreshMiningFactors = useCallback(async (taskId?: string) => {
    const id = taskId ?? focusedTaskIdRef.current;
    if (!id) return;
    try {
      const r = await getFactors({ taskId: id, limit: FACTOR_LIST_MAX_LIMIT });
      if (!mountedRef.current || !r.success || !r.data) return;
      const rows = r.data.factors ?? [];
      patchMiningTask(id, (prev) => ({ ...prev, metrics: mergeTaskFactors(prev.metrics, rows) }));
    } catch (err) {
      console.error('[alpha-research] refresh mining factors failed:', err);
    }
  }, [patchMiningTask]);

  // 任务完成沿自动拉一次全量清单（覆盖：新任务完成、恢复出的已完成历史任务）。
  // 逐任务记账：A 完成触发 A 的清单、B 完成触发 B 的，互不顶替。
  const factorsRefreshedRef = useRef<Set<string>>(new Set());
  useEffect(() => {
    for (const t of miningTasks) {
      if (!t.taskId || t.status !== 'completed') continue;
      if (factorsRefreshedRef.current.has(t.taskId)) continue;
      factorsRefreshedRef.current.add(t.taskId);
      void refreshMiningFactors(t.taskId);
    }
  }, [miningTasks, refreshMiningFactors]);

  // Start mining (real backend)
  const startRealMining = useCallback(
    async (config: TaskConfig) => {
      // 只锁「提交在途」；已有任务运行不拦——任务相互独立，并发上限由后端
      // 429 兜底（默认 2/人），失败原文进 miningStartError 上屏。
      if (miningStartingRef.current) return;
      try {
        setMiningStarting(true);
        setMiningStartError(null);
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
          // 文档血统（文档链提交时带上）：落任务 source=doc + 回写文档 task_id
          docId: config.docId,
        });
        if (!resp.success || !resp.data) throw new Error(resp.error || 'Failed');

        // 并行方向数（T-MV-04）：N>1 的回执逐条接纳——每任务进注册表并绑定
        // 自己的传输（与拆解面板 adoptDispatchedTasks 同一语义），焦点给第一条，
        // 同样触发「开始后自动进演化台」。部分失败随摘要上屏；全失败不留假任务行
        const dispatched = resp.data.tasks;
        if (dispatched) {
          for (const t of dispatched) {
            upsertMiningTask(t);
            bindMiningTransport(t.taskId);
          }
          if (dispatched.length > 0) {
            setFocusedTaskId(dispatched[0].taskId);
            miningStartSeqRef.current += 1;
            setMiningStartSeq(miningStartSeqRef.current);
          }
          const failures = resp.data.failures ?? [];
          if (failures.length > 0) {
            const detail = failures
              .map((f) => `${f.direction || '未指明方向'}：${f.error}`)
              .join('；');
            setMiningStartError(
              dispatched.length > 0
                ? `已派发 ${dispatched.length} 条方向任务，另有 ${failures.length} 条失败——${detail}`
                : `启动失败——${detail}`,
            );
          }
          return;
        }

        const taskData = resp.data.task;
        if (!taskData) throw new Error(resp.error || 'Failed');
        // 新任务从零开始：清掉任何残留清单与 IC 族头条（缺失=undefined→界面显「—」；
        // 旧实现只清 top10Factors，IC 族残留 0 值，统计卡永远显示 0.0000）
        taskData.metrics = emptyMetrics();
        upsertMiningTask(taskData);
        // 新任务获得焦点（AppRoot 沿 miningStartSeq 自动进演化台，看到的就是它）
        setFocusedTaskId(taskData.taskId);
        miningStartSeqRef.current += 1;
        setMiningStartSeq(miningStartSeqRef.current);

        // 绑定该任务的实时传输（与其它运行中任务的传输并存、互不干扰）
        bindMiningTransport(taskData.taskId);
      } catch (err: any) {
        console.error('Failed to start mining task:', err);
        const detail = err?.response?.data?.detail;
        const failMsg =
          typeof detail === 'string' && detail.trim()
            ? detail
            : (err?.message || '无法连接后端服务');
        // 提交失败没有任务可展示：错误单独上屏，**不**再伪造 taskId='' 的失败任务行
        setMiningStartError(`启动失败: ${failMsg}`);
      } finally {
        setMiningStarting(false);
      }
    },
    [bindMiningTransport, upsertMiningTask],
  );

  // Public start mining
  const startMining = useCallback(
    (config: TaskConfig) => {
      startRealMining(config);
    },
    [startRealMining],
  );

  /** 聚焦切换（认不出的 id 忽略，防止悬空焦点） */
  const focusMiningTask = useCallback((taskId: string) => {
    if (!miningTasksRef.current.some((t) => t.taskId === taskId)) return;
    setFocusedTaskId(taskId);
  }, []);

  /**
   * 批量派发接纳：拆解面板拿到逐条回执后，把任务并进注册表并绑定传输。
   * queued 任务同样绑定——传输是 2s 状态流 + 10s 兜底轮询，排队期渲染
   * 「排队中 · 第 N 位」，排到后同一传输无缝续流（状态翻转由传输的
   * 变化检测处理，见 handleMiningWsMessage / 轮询兜底）。
   */
  const adoptDispatchedTasks = useCallback(
    (tasks: Task[]) => {
      if (tasks.length === 0) return;
      for (const t of tasks) {
        upsertMiningTask(t);
        bindMiningTransport(t.taskId);
      }
      setFocusedTaskId(tasks[tasks.length - 1].taskId);
    },
    [bindMiningTransport, upsertMiningTask],
  );

  const dismissMiningStartError = useCallback(() => setMiningStartError(null), []);

  // Stop mining（缺省=聚焦任务）：只拆指定任务的传输并取消它，其它任务不受影响
  const stopMining = useCallback(async (taskId?: string) => {
    const id = taskId ?? focusedTaskIdRef.current;
    if (!id) return;
    teardownMiningTransport(id);
    if (backendAvailable) {
      try {
        await apiCancelMining(id);
      } catch {
        // ignore
      }
    }
    // TaskStatus 无 cancelled：本地终态沿用 failed（与 normalizeTaskStatus
    // 把后端 cancelled 归并为 failed 同一口径），后端原文留在 progress.message
    patchMiningTask(id, (prev) => ({ ...prev, status: 'failed' }));
  }, [backendAvailable, teardownMiningTransport, patchMiningTask]);

  // Reset mining task（缺省=聚焦任务）：前端清场；聚焦对象被移除时改焦剩余最新
  const resetMiningTask = useCallback((taskId?: string) => {
    const id = taskId ?? focusedTaskIdRef.current;
    if (!id) return;
    teardownMiningTransport(id);
    setMiningTasks((prev) => prev.filter((t) => t.taskId !== id));
    if (focusedTaskIdRef.current === id) {
      const rest = miningTasksRef.current.filter((t) => t.taskId !== id);
      setFocusedTaskId(rest.length ? rest[rest.length - 1].taskId : null);
    }
  }, [teardownMiningTransport]);

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
    // Mining（多任务注册表）
    miningTask,
    miningTasks,
    focusedTaskId,
    focusMiningTask,
    adoptDispatchedTasks,
    miningStarting,
    miningStartError,
    dismissMiningStartError,
    miningStartSeq,
    miningEquityCurve: EMPTY_SERIES,
    miningDrawdownCurve: EMPTY_SERIES,
    miningIcTimeSeries: EMPTY_SERIES,
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
