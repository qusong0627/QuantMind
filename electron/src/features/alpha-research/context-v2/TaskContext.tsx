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
  WsMessage,
} from '../types-v2';
import { generateId } from '../utils-v2';
import { useAppSelector } from '../../../store';
import { getSessionGeneration, registerSessionReset } from '../../../store/sessionBoundary';
import { getSessionScopedStorageKey } from '../../../store/sessionScopedStorage';
import {
  startMining as apiStartMining,
  findMiningSubmission,
  previewMining as apiPreviewMining,
  type MiningPreview,
  type MiningStartParams,
  getMiningStatus,
  cancelMining as apiCancelMining,
  listTasks,
  connectMiningWs,
  healthCheck,
} from '../services-v2/api';
import { getDefaultMiningDirection, getStoredDirectionConfig } from '../utils-v2/miningDirections';

// ========================== Structured factors merge ==========================

/**
 * 把后端任务状态返回的结构化因子（rd_agent_factors 已落库数据）合并进实时指标。
 * 数据来自数据库而非日志文本解析，不受后端日志措辞变化影响。
 */
function asRecord(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function finiteNumber(value: unknown): number | null {
  if (value == null || (typeof value !== 'number' && typeof value !== 'string') || String(value).trim() === '') return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function textValue(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback;
}

function stringPreference(
  preferences: Record<string, unknown>,
  key: string,
  fallback: string,
): string {
  return textValue(preferences[key], fallback);
}

function numberPreference(
  preferences: Record<string, unknown>,
  key: string,
  fallback: number,
): number {
  const value = preferences[key];
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback;
}

/**
 * 轮询兜底只同步服务端持有的字段，保留 WS 增量累积的客户端状态。
 *
 * 任务状态接口不返回 logs（日志走 /tasks/:id/log 增量拉取），若用整对象替换，
 * 每 10 秒一次的状态轮询会把已渲染日志清空、offset 却已前移，表现为“实时日志
 * 一直 0 条 / 严重滞后”。
 */
export function mergePolledTask(prev: Task | null, next: Task): Task {
  if (!prev || prev.taskId !== next.taskId) return { ...next, logs: next.logs ?? [] };
  // 状态接口随任务返回的 factors 是「已落库」的权威列表（带 RankIC）；
  // 服务端没给 factors / metrics 时不能拿空对象覆盖客户端已累积的列表，
  // 否则每 10 秒一次的状态轮询会把刚出现的因子清空，表现为列表在
  // 「有因子 / 暂无因子数据」之间来回跳。
  const polledMetrics = next.factors && next.factors.length > 0
    ? metricsFromTaskFactors(next.factors, next.metrics)
    : next.metrics;
  return {
    ...next,
    logs: prev.logs ?? next.logs ?? [],
    metrics: mergeMetrics(prev.metrics, polledMetrics),
    timeline:
      next.timeline && next.timeline.length > 0 ? next.timeline : prev.timeline,
    tokenUsage:
      next.tokenUsage && Object.keys(next.tokenUsage).length > 0
        ? next.tokenUsage
        : prev.tokenUsage,
  };
}

/**
 * 合并两批实时指标：因子列表取更完整的一侧，标量取非空值，计数取历史最大值。
 *
 * 任务运行期间指标有两个来源——状态轮询（服务端已落库因子）和实时事件
 * （日志/结构化因子增量）。任何一侧先到都得保留，不能相互覆盖。
 */
export function mergeMetrics(
  prev: RealtimeMetrics | undefined,
  next: RealtimeMetrics | undefined,
): RealtimeMetrics | undefined {
  if (!prev) return next;
  if (!next) return prev;
  const prevFactors = prev.top10Factors ?? [];
  const nextFactors = next.top10Factors ?? [];
  const factors = nextFactors.length >= prevFactors.length ? nextFactors : prevFactors;
  return {
    ...next,
    totalFactors: Math.max(
      prev.totalFactors ?? 0,
      next.totalFactors ?? 0,
      factors.length,
    ),
    top10Factors: factors,
    factorName: next.factorName ?? prev.factorName,
    rankIc: next.rankIc ?? prev.rankIc,
    rankIcir: next.rankIcir ?? prev.rankIcir,
    ic: next.ic ?? prev.ic,
    icir: next.icir ?? prev.icir,
    annualReturn: next.annualReturn ?? prev.annualReturn,
    sharpeRatio: next.sharpeRatio ?? prev.sharpeRatio,
    maxDrawdown: next.maxDrawdown ?? prev.maxDrawdown,
  };
}

/** 把状态接口返回的已落库因子转成看板指标（RankIC 降序取前 10）。 */
function metricsFromTaskFactors(
  factors: NonNullable<Task['factors']>,
  base: RealtimeMetrics | undefined,
): RealtimeMetrics {
  const mapped = factors.map((factor) => ({
    factorId: factor.factorId,
    factorName: factor.factorName,
    factorExpression: factor.factorExpression,
    rankIc: factor.rankIc ?? null,
    rankIcir: factor.rankIcir ?? null,
    ic: factor.ic ?? null,
    icir: factor.icir ?? null,
    annualReturn: factor.annualReturn ?? null,
    sharpeRatio: factor.sharpeRatio ?? null,
    maxDrawdown: factor.maxDrawdown ?? null,
    market: factor.market,
    cumulativeCurve: [] as Array<{ date: string; value: number }>,
  }));
  const top10 = pickTopFactors(mapped);
  return {
    ...(base ?? emptyMetricsShape()),
    totalFactors: Math.max(base?.totalFactors ?? 0, mapped.length),
    top10Factors: top10,
    ...bestFactorScalars(top10),
  };
}

function emptyMetricsShape(): RealtimeMetrics {
  return {
    ic: null, icir: null, rankIc: null, rankIcir: null,
    annualReturn: null, sharpeRatio: null, maxDrawdown: null,
    totalFactors: 0, highQualityFactors: 0, mediumQualityFactors: 0, lowQualityFactors: 0,
    top10Factors: [],
  };
}

type FactorEntry = NonNullable<RealtimeMetrics['top10Factors']>[number];

/** RankIC 降序取前 10；RankIC 未知的排在后面，避免刚产出的因子挤掉已评测因子。 */
function pickTopFactors(factors: FactorEntry[]): FactorEntry[] {
  return [...factors]
    .sort((a, b) => (b.rankIc ?? -Infinity) - (a.rankIc ?? -Infinity))
    .slice(0, 10);
}

function bestFactorScalars(factors: FactorEntry[]) {
  const best = factors[0];
  if (!best) return {};
  return {
    factorName: best.factorName,
    rankIc: best.rankIc,
    rankIcir: best.rankIcir,
    ic: best.ic,
    icir: best.icir,
    annualReturn: best.annualReturn ?? null,
    sharpeRatio: best.sharpeRatio ?? null,
    maxDrawdown: best.maxDrawdown ?? null,
  };
}

function resolveMiningParams(config: TaskConfig): MiningStartParams {
  let defaults: Record<string, unknown> = {};
  const storageKey = getSessionScopedStorageKey('autoalpha_config');
  const savedConfig = storageKey ? localStorage.getItem(storageKey) : null;
  if (savedConfig) {
    try {
      defaults = asRecord(JSON.parse(savedConfig));
    } catch {
      // Keep safe defaults when optional preferences are malformed.
    }
  }
  const stored = getStoredDirectionConfig();
  const useCustom = Boolean(config.useCustomMiningDirection);
  return {
    direction: useCustom
      ? (getDefaultMiningDirection() || '价量因子挖掘')
      : (config.userInput && config.userInput.trim()) || getDefaultMiningDirection() || '价量因子挖掘',
    algorithm: config.algorithm ?? 'rdagent',
    statisticalConfig: config.algorithm === 'statistical' ? config.statisticalConfig : undefined,
    autoMaterialize: config.algorithm === 'statistical' ? config.autoMaterialize ?? true : false,
    miningStart: config.miningStart,
    miningEnd: config.miningEnd,
    validStart: config.validStart,
    validEnd: config.validEnd,
    directions: useCustom ? stored.labels : undefined,
    directionMode: stored.mode,
    market: config.miningMarket || 'a_share',
    universe: config.universe || stringPreference(defaults, 'defaultUniverse', 'all_a'),
    dataSource: config.algorithm === 'statistical' ? 'qlib_bin' : config.dataSource || 'qlib_bin',
    datasets: config.datasets,
    freq: config.freq ?? 'day',
    maxRounds: config.maxRounds || numberPreference(defaults, 'defaultMaxRounds', 3),
  };
}

function mergeStructuredFactors(
  metrics: RealtimeMetrics | undefined,
  rawFactors: unknown[],
): RealtimeMetrics {
  const base: RealtimeMetrics = metrics || emptyMetricsShape();
  if (!rawFactors.length) return base;

  const mapped = rawFactors.map((rawFactor) => {
    const factor = asRecord(rawFactor);
    const metadata = asRecord(factor.metadata);
    const formulation = textValue(factor.factor_formulation)
      || textValue(metadata.formulation)
      || textValue(factor.factor_code).slice(0, 120);
    return {
      factorId: textValue(factor.factor_id),
      factorName: textValue(factor.factor_name, 'unnamed'),
      factorExpression: formulation,
      rankIc: finiteNumber(factor.rank_ic),
      rankIcir: finiteNumber(metadata.rank_icir),
      ic: finiteNumber(factor.ic_value),
      icir: finiteNumber(metadata.icir),
      annualReturn: finiteNumber(factor.annual_return),
      sharpeRatio: finiteNumber(factor.sharpe_ratio),
      maxDrawdown: finiteNumber(factor.max_drawdown),
      calmarRatio: finiteNumber(metadata.calmar_ratio),
      market: typeof factor.market === 'string' ? factor.market : undefined,
      cumulativeCurve: [] as Array<{ date: string; value: number }>,
    };
  });

  // RankIC 降序取 Top10，并重算最优因子指标
  const top10 = pickTopFactors(mapped);
  return {
    ...base,
    totalFactors: Math.max(base.totalFactors || 0, mapped.length),
    top10Factors: top10,
    ...bestFactorScalars(top10),
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
  miningSubmissionPending: boolean;
  retryMiningSubmission: () => void;
  /** 用户主动开始挖掘的序号（仅用于「开始后自动进入演化台」，恢复历史任务不触发） */
  miningStartSeq: number;
  previewMining: (config: TaskConfig) => Promise<MiningPreview>;
  startMining: (config: TaskConfig) => void;
  stopMining: () => void;
  resetMiningTask: () => void;

}

const TaskContext = createContext<TaskContextValue | null>(null);

// ========================== Provider ==========================

export const TaskProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const isAuthenticated = useAppSelector((state) => state.auth.isAuthenticated);
  const isAuthenticating = useAppSelector((state) => state.auth.isLoading);
  const [sessionEpoch, setSessionEpoch] = useState(() => getSessionGeneration());
  const sessionGenerationRef = useRef(getSessionGeneration());
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

  const miningWsRef = useRef<WebSocket | null>(null);
  const miningPollingRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const miningWsTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const miningDataPointsRef = useRef(0);
  const mountedRef = useRef(true);
  // 同步到 ref 供 startRealMining 闭包内读取，避免 stale state
  const miningStartingRef = useRef(false);
  const pendingSubmissionRef = useRef<{
    config: TaskConfig;
    params: MiningStartParams;
    requestKey: string;
    attempted: boolean;
  } | null>(null);
  const pendingStorageKeyRef = useRef<string | null>(null);
  const submissionController = useRef<AbortController | null>(null);
  const [miningSubmissionPending, setMiningSubmissionPending] = useState(false);
  const clearPendingSubmission = useCallback((removeStored = true) => {
    if (removeStored && pendingStorageKeyRef.current) {
      try { localStorage.removeItem(pendingStorageKeyRef.current); } catch { /* Keep the in-memory boundary even without browser storage. */ }
    }
    pendingStorageKeyRef.current = null;
    pendingSubmissionRef.current = null;
    setMiningSubmissionPending(false);
  }, []);
  useEffect(() => {
    if (!isAuthenticated || isAuthenticating || pendingSubmissionRef.current) return;
    const prefix = getSessionScopedStorageKey('alpha_mining_submission');
    if (!prefix) return;
    try {
      // Separate records keep another tab's unconfirmed submission intact.
      const keys = Object.keys(localStorage).filter((key) => key.startsWith(`${prefix}:`)).sort();
      for (const key of keys) {
        try {
          const saved = JSON.parse(localStorage.getItem(key) || 'null');
          if (!saved || typeof saved.requestKey !== 'string' || !saved.requestKey || saved.requestKey.length > 128 || key !== `${prefix}:${saved.requestKey}` || typeof saved.params?.direction !== 'string' || !saved.config) continue;
          pendingSubmissionRef.current = { config: saved.config, params: saved.params, requestKey: saved.requestKey, attempted: true };
          pendingStorageKeyRef.current = key;
          setMiningSubmissionPending(true);
          break;
        } catch { /* Skip an unreadable record without erasing other intents. */ }
      }
    } catch { /* An unreadable local record cannot be submitted automatically. */ }
  }, [isAuthenticated, isAuthenticating, sessionEpoch, miningSubmissionPending]);
  const miningTaskRef = useRef<Task | null>(null);
  const previewedMiningParamsRef = useRef<{
    config: TaskConfig;
    params: MiningStartParams;
  } | null>(null);
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
      submissionController.current?.abort();
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
            updated.status = msg.data.status ?? (msg.data.phase === 'completed' ? 'completed' : prev.status);
            updated.queuePosition = msg.data.queuePosition;
            if (msg.data.timeline) updated.timeline = msg.data.timeline;
            if (msg.data.tokenUsage) updated.tokenUsage = msg.data.tokenUsage;
            // 结构化因子（后端已落库，优先于日志正则解析），直接更新 Top10 列表
            if (Array.isArray(msg.data.factors) && msg.data.factors.length > 0) {
              updated.metrics = mergeStructuredFactors(updated.metrics, msg.data.factors);
            }
            break;
          case 'log':
          {
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
                    ic: null, icir: null, rankIc: null, rankIcir: null,
                    annualReturn: null, sharpeRatio: null, maxDrawdown: null,
                    totalFactors: 0, highQualityFactors: 0, mediumQualityFactors: 0, lowQualityFactors: 0,
                    top10Factors: []
                };
                
                const currentFactors = currentMetrics.top10Factors || [];
                // Avoid duplicates
                if (!currentFactors.some((factor) => factor.factorName === name)) {
                    const newFactor = {
                        factorId: generateId(),
                        factorName: name,
                        factorExpression: expr,
                        rankIc: null, rankIcir: null, ic: null, icir: null,
                        annualReturn: null, sharpeRatio: null, maxDrawdown: null, calmarRatio: null,
                        cumulativeCurve: []
                    };

                    // 列表按 RankIC 截到 Top10（与结构化因子同一口径），计数只增不减
                    const updatedFactors = pickTopFactors([newFactor, ...currentFactors]);
                    updated.metrics = {
                        ...currentMetrics,
                        totalFactors: Math.max(currentMetrics.totalFactors || 0, updatedFactors.length),
                        top10Factors: updatedFactors,
                        ...bestFactorScalars(updatedFactors),
                    };
                }
              }
            }
            break;
          }
          case 'metrics':
            updated.metrics = mergeMetrics(updated.metrics, msg.data as RealtimeMetrics);
            break;
          case 'result':
            updated.status = msg.data.status === 'completed' ? 'completed' : msg.data.status === 'cancelled' ? 'cancelled' : 'failed';
            if (msg.data.metrics) updated.metrics = mergeMetrics(updated.metrics, msg.data.metrics);
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
      const generation = sessionGenerationRef.current;
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
      const ws = connectMiningWs(taskId, (message) => {
        if (generation === sessionGenerationRef.current) handleMiningWsMessage(message);
      }, () => {
        if (!mountedRef.current || generation !== sessionGenerationRef.current) return;
        getMiningStatus(taskId).then((r) => {
          if (
            r.data?.task
            && mountedRef.current
            && generation === sessionGenerationRef.current
          ) setMiningTask((prev) => mergePolledTask(prev, r.data.task as Task));
        });
      });
      miningWsRef.current = ws;
      miningWsTimeoutRef.current = (
        ws as WebSocket & { _pollingTimeoutId?: ReturnType<typeof setTimeout> }
      )._pollingTimeoutId ?? null;
      miningPollingRef.current = setInterval(async () => {
        if (!mountedRef.current) {
          clearInterval(miningPollingRef.current!);
          miningPollingRef.current = null;
          return;
        }
        try {
          const r = await getMiningStatus(taskId);
          if (!mountedRef.current || generation !== sessionGenerationRef.current) return;
          if (r.data?.task) {
            const t = r.data.task as Task;
            setMiningTask((prev) => mergePolledTask(prev, t));
            if (t.status === 'completed' || t.status === 'failed' || t.status === 'cancelled') {
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
    if (!isAuthenticated || isAuthenticating) {
      miningRecoveredRef.current = false;
      return;
    }
    if (miningRecoveredRef.current) return;
    miningRecoveredRef.current = true;
    const generation = sessionGenerationRef.current;
    listTasks()
      .then((r) => {
        if (
          !mountedRef.current
          || generation !== sessionGenerationRef.current
          || miningTaskRef.current
        ) return;
        const active = (r.data?.tasks ?? []).find((t) => t.status === 'queued' || t.status === 'running' || t.status === 'stopping');
        if (active) {
          setMiningTask(active);
          bindMiningTransport(active.taskId);
        }
      })
      .catch(() => {});
  }, [bindMiningTransport, isAuthenticated, isAuthenticating, sessionEpoch]);

  // Start mining (real backend)
  const previewRealMining = useCallback(async (config: TaskConfig) => {
    if (!isAuthenticated || isAuthenticating) throw new Error('登录状态已失效，请重新登录后预览');
    if (pendingSubmissionRef.current) throw new Error('上次提交结果尚未确认，请先点击“确认原提交”；本次仍使用上次冻结的参数。');
    const generation = sessionGenerationRef.current;
    previewedMiningParamsRef.current = null;
    const params = resolveMiningParams(config);
    const response = await apiPreviewMining(params);
    if (generation !== sessionGenerationRef.current) throw new Error('会话已切换，请重新预览');
    if (!response.success || !response.data) throw new Error(response.error || '参数预览失败');
    previewedMiningParamsRef.current = { config, params };
    return response.data;
  }, [isAuthenticated, isAuthenticating]);

  const startRealMining = useCallback(
    async (config: TaskConfig) => {
      const generation = sessionGenerationRef.current;
      // 任务锁：已有任务在运行或提交进行中时，忽略重复提交
      if (!isAuthenticated || isAuthenticating) return;
      if (miningStartingRef.current) return;
      if (!pendingSubmissionRef.current && (miningTaskRef.current?.status === 'queued' || miningTaskRef.current?.status === 'running' || miningTaskRef.current?.status === 'stopping')) return;
      let posting = false;
      const retrying = Boolean(pendingSubmissionRef.current?.attempted);
      try {
        miningStartingRef.current = true;
        setMiningStarting(true);
        const frozenPreview = previewedMiningParamsRef.current;
        const intent = pendingSubmissionRef.current ?? {
          config: JSON.parse(JSON.stringify(config)) as TaskConfig,
          params: JSON.parse(JSON.stringify(frozenPreview?.config === config ? frozenPreview.params : resolveMiningParams(config))) as MiningStartParams,
          requestKey: globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${generateId()}`,
          attempted: false,
        };
        pendingSubmissionRef.current = intent;
        const storagePrefix = getSessionScopedStorageKey('alpha_mining_submission');
        pendingStorageKeyRef.current ??= storagePrefix ? `${storagePrefix}:${intent.requestKey}` : null;
        if (pendingStorageKeyRef.current) {
          try { localStorage.setItem(pendingStorageKeyRef.current, JSON.stringify(intent)); } catch { /* The same intent remains available in memory. */ }
        }
        setMiningSubmissionPending(true);
        previewedMiningParamsRef.current = null;
        const controller = new AbortController();
        submissionController.current = controller;
        const existing = intent.attempted ? await findMiningSubmission(intent.params, intent.requestKey, controller.signal) : null;
        if (controller.signal.aborted || generation !== sessionGenerationRef.current) return;
        intent.attempted = true;
        posting = !existing;
        const resp = existing ?? await apiStartMining(intent.params, intent.requestKey, controller.signal);
        if (generation !== sessionGenerationRef.current) return;
        if (!resp.success || !resp.data) throw new Error(resp.error || 'Failed');
        clearPendingSubmission();

        const taskData = resp.data.task as Task;
        // Initialize metrics with empty top10Factors to avoid stale data
        if (taskData.metrics) {
            taskData.metrics.top10Factors = [];
            taskData.metrics.totalFactors = 0;
            taskData.metrics.highQualityFactors = 0;
            taskData.metrics.mediumQualityFactors = 0;
            taskData.metrics.lowQualityFactors = 0;
        }
        miningTaskRef.current = taskData;
        setMiningTask(taskData);
        miningStartSeqRef.current += 1;
        setMiningStartSeq(miningStartSeqRef.current);
        miningDataPointsRef.current = 0;

        // 绑定 WebSocket + 轮询兜底
        bindMiningTransport(resp.data.taskId);
      } catch (err: unknown) {
        if (generation !== sessionGenerationRef.current) return;
        console.error('Failed to start mining task:', err);
        const error = asRecord(err);
        const response = asRecord(error.response);
        // These explicit admission failures did not accept this request. Network
        // errors and server errors remain ambiguous and must retain the intent.
        if (posting && !retrying && [400, 401, 403, 404, 409, 412, 422, 429, 507].includes(Number(response.status))) clearPendingSubmission();
        const responseData = asRecord(response.data);
        const detail = responseData.detail;
        const failMsg =
          typeof detail === 'string' && detail.trim()
            ? detail
            : (textValue(error.message) || '无法连接后端服务');
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
            message: pendingSubmissionRef.current ? `提交结果未确认，请确认原提交：${failMsg}` : `启动失败: ${failMsg}`,
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
        if (generation === sessionGenerationRef.current) {
          miningStartingRef.current = false;
          setMiningStarting(false);
        }
      }
    },
    [bindMiningTransport, isAuthenticated, isAuthenticating, clearPendingSubmission],
  );

  // Public start mining
  const startMining = useCallback(
    (config: TaskConfig) => {
      startRealMining(config);
    },
    [startRealMining],
  );
  const retryMiningSubmission = useCallback(() => {
    const intent = pendingSubmissionRef.current;
    if (intent) void startRealMining(intent.config);
  }, [startRealMining]);

  // Stop mining
  const stopMining = useCallback(async () => {
    const task = miningTask;
    if (!task) return;
    const generation = sessionGenerationRef.current;
    if (backendAvailable) {
      try {
        const response = await apiCancelMining(task.taskId);
        if (generation !== sessionGenerationRef.current) return;
        const status = response.data?.status ?? 'stopping';
        setMiningTask({ ...task, status });
      } catch {
        if (generation !== sessionGenerationRef.current) return;
        setMiningTask({
          ...task,
          logs: [...task.logs, {
            id: generateId(),
            timestamp: new Date().toISOString(),
            level: 'error',
            message: '停止请求未确认，请检查连接后重试。',
          }],
        });
      }
    } else {
      setMiningTask({
        ...task,
        logs: [...task.logs, {
          id: generateId(),
          timestamp: new Date().toISOString(),
          level: 'error',
          message: '后端不可用，无法确认停止请求。',
        }],
      });
    }
  }, [miningTask, backendAvailable]);

  // Reset mining task
  const resetMiningTask = useCallback(() => {
    previewedMiningParamsRef.current = null;
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
  }, []);

  const resetPrivateTaskState = useCallback(() => {
    const generation = getSessionGeneration();
    sessionGenerationRef.current = generation;
    submissionController.current?.abort();
    // Preserve owner-scoped recovery receipts across logout; never replay them
    // automatically, and never restore one into a different owner session.
    clearPendingSubmission(false);
    miningStartingRef.current = false;
    miningTaskRef.current = null;
    previewedMiningParamsRef.current = null;
    miningRecoveredRef.current = false;
    miningStartSeqRef.current = 0;
    miningDataPointsRef.current = 0;
    if (miningWsTimeoutRef.current) clearTimeout(miningWsTimeoutRef.current);
    miningWsTimeoutRef.current = null;
    miningWsRef.current?.close();
    miningWsRef.current = null;
    if (miningPollingRef.current) clearInterval(miningPollingRef.current);
    miningPollingRef.current = null;
    setMiningTask(null);
    setMiningStarting(false);
    setMiningStartSeq(0);
    setSessionEpoch(generation);
  }, [clearPendingSubmission]);

  useEffect(
    () => {
      sessionGenerationRef.current = getSessionGeneration();
      return registerSessionReset(resetPrivateTaskState);
    },
    [resetPrivateTaskState],
  );

  // ==================================================================
  // Context value
  // ==================================================================
  const value: TaskContextValue = {
    backendAvailable,
    // Mining
    miningTask,
    miningStarting,
    miningSubmissionPending,
    retryMiningSubmission,
    miningStartSeq,
    previewMining: previewRealMining,
    startMining,
    stopMining,
    resetMiningTask,

  };

  return <TaskContext.Provider value={value}>{children}</TaskContext.Provider>;
};

// ========================== Hook ==========================

export function useTaskContext(): TaskContextValue {
  const ctx = useContext(TaskContext);
  if (!ctx) throw new Error('useTaskContext must be used inside <TaskProvider>');
  return ctx;
}
