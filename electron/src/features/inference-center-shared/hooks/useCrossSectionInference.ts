/**
 * 截面推理（Cross-Section Inference）状态机。
 *
 * 从 InferenceCenterShell 抽出：注册模型表、前置预检、预测目标日、自动调度、
 * 当前模拟生效、最近一次推理排名。排名是「个股预测」联动的数据源，因此也在这里。
 * 三市场共用，市场差异经参数（market / calendar）注入。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import dayjs, { type Dayjs } from 'dayjs';
import { message } from 'antd';
import {
  modelTrainingService,
  type AutoInferenceSettings,
  type InferencePrecheckResult,
  type InferenceRankingResult,
  type InferenceRunRecord,
  type LatestInferenceRunInfo,
  type UserModelRecord,
} from '../../../services/modelTrainingService';
import { getMeta, systemModelToUserModel } from '../../../pages/modelRegistryUtils';

/** 单日推理股票池选择（仅 A 股使用）：null = 全市场 */
export interface InferencePoolSelection {
  /** 后端识别用的 `pool:<code>` 引用 */
  ref: string;
  name: string;
  /** 本地池 id，仅用于回显选中态 */
  id: string;
}

export interface CrossSectionInference {
  // 模型
  registeredModels: UserModelRecord[];
  modelsLoading: boolean;
  selectedModelId: string;
  setSelectedModelId: (id: string) => void;
  selectedModel: UserModelRecord | null;
  horizonDays: number;

  // 日期
  inferenceDate: Dayjs | null;
  setInferenceDate: (d: Dayjs | null) => void;
  targetDate: string;
  targetDateLoading: boolean;

  // 预检
  precheck: InferencePrecheckResult | null;
  precheckLoading: boolean;
  refreshPrecheck: () => void;

  // 执行
  running: boolean;
  runInference: () => Promise<void>;
  lastRun: InferenceRunRecord | null;
  setDefaultModel: () => Promise<void>;

  // 自动调度 / 生效状态
  autoSettings: AutoInferenceSettings | null;
  autoSaving: boolean;
  toggleAuto: (enabled: boolean) => Promise<void>;
  latestRun: LatestInferenceRunInfo | null;
  latestRunLoading: boolean;

  // 排名（个股预测联动的数据源）
  ranking: InferenceRankingResult | null;
  rankingLoading: boolean;
  /** 非空 = 展示的是更早的批次（最近一批明细未落库），值为该批次 run_id */
  rankingFallbackFrom: string | null;
  deleteHistory: (runId: string) => Promise<void>;

  // 股票池（仅 A 股调用方使用）
  pool: InferencePoolSelection | null;
  setPool: (p: InferencePoolSelection | null) => void;
}

/** HISTORY_PAGE_SIZE 与旧版一致：一次取 20 条，用于回填「最近一次完成的推理」 */
const HISTORY_PAGE_SIZE = 20;

/**
 * 明细回退深度：一次推理的排名明细存在 engine_signal_scores 里。
 * 线上出现过 run 记录已 completed 且写好 signals_count、但明细行没落库的情况
 * （同一模型同一交易日重复跑时尤其容易），此时「最近一条 completed」是空榜。
 * 往前多试几个已完成批次，让排名榜能出数据；回退发生时 UI 会标注实际展示的是哪一批。
 */
const RANKING_FALLBACK_DEPTH = 6;

export function useCrossSectionInference(
  market: string,
  calendar: string,
  initialModelId = '',
): CrossSectionInference {
  const [registeredModels, setRegisteredModels] = useState<UserModelRecord[]>([]);
  const [modelsLoading, setModelsLoading] = useState(false);
  const [selectedModelId, setSelectedModelId] = useState<string>(initialModelId);

  const [inferenceDate, setInferenceDate] = useState<Dayjs | null>(dayjs());
  const [targetDate, setTargetDate] = useState('');
  const [targetDateLoading, setTargetDateLoading] = useState(false);

  const [precheck, setPrecheck] = useState<InferencePrecheckResult | null>(null);
  const [precheckLoading, setPrecheckLoading] = useState(false);

  const [running, setRunning] = useState(false);
  const [lastRun, setLastRun] = useState<InferenceRunRecord | null>(null);

  const [autoSettings, setAutoSettings] = useState<AutoInferenceSettings | null>(null);
  const [autoSaving, setAutoSaving] = useState(false);
  const [latestRun, setLatestRun] = useState<LatestInferenceRunInfo | null>(null);
  const [latestRunLoading, setLatestRunLoading] = useState(false);

  const [ranking, setRanking] = useState<InferenceRankingResult | null>(null);
  const [rankingLoading, setRankingLoading] = useState(false);
  /** 排名明细没落库时的备用批次（按新鲜度），仅存 run_id 避免无谓的重渲染 */
  const [fallbackRuns, setFallbackRuns] = useState<InferenceRunRecord[]>([]);
  /** 实际展示的批次不是「最近一条 completed」时，记下它的 run_id 供 UI 标注 */
  const [rankingFallbackFrom, setRankingFallbackFrom] = useState<string | null>(null);
  const fallbackKeyRef = useRef('');

  const [pool, setPool] = useState<InferencePoolSelection | null>(null);

  const selectedModel = useMemo(
    () => registeredModels.find((m) => m.model_id === selectedModelId) || registeredModels[0] || null,
    [registeredModels, selectedModelId],
  );

  const horizonDays = useMemo(() => {
    if (!selectedModel) return 5;
    const meta = getMeta(selectedModel);
    return Number(meta?.target_horizon_days ?? meta?.target_horizon ?? 5);
  }, [selectedModel]);

  // ── 注册模型（用户模型 + 系统模型，按市场过滤）────────────────
  const loadRegisteredModels = useCallback(async () => {
    setModelsLoading(true);
    try {
      const marketUpper = market.toUpperCase();
      const [uRes, sList] = await Promise.all([
        modelTrainingService.listUserModels(false, marketUpper).catch(() => ({ items: [], total: 0 })),
        modelTrainingService.listSystemModels(marketUpper).catch(() => []),
      ]);
      const combined = [
        ...(uRes.items || []).filter((m) => m.status !== 'archived'),
        ...(sList || []).map(systemModelToUserModel),
      ];
      setRegisteredModels(combined);
    } catch (err) {
      console.error('加载注册模型失败:', err);
    } finally {
      setModelsLoading(false);
    }
  }, [market]);

  useEffect(() => {
    void loadRegisteredModels();
  }, [loadRegisteredModels]);

  // 选中模型对账：模型表变化（含切市场）后，原选中项不在表里就回退到默认/首个模型。
  // 用对账 effect 而非函数式 setState —— 本项目的 useState setter 类型不接受更新函数。
  useEffect(() => {
    if (registeredModels.length === 0) {
      if (selectedModelId) setSelectedModelId('');
      return;
    }
    if (selectedModelId && registeredModels.some((m) => m.model_id === selectedModelId)) return;
    const fallback = registeredModels.find((m) => m.is_default) || registeredModels[0];
    setSelectedModelId(fallback.model_id);
  }, [registeredModels, selectedModelId]);

  // ── 前置预检：基准日跟随后端数据回退，避免输入框与实际数据日脱节 ──
  const loadPrecheck = useCallback(async (modelId: string, checkDate?: string) => {
    setPrecheckLoading(true);
    try {
      const resp = await modelTrainingService.precheckInference(modelId, checkDate);
      setPrecheck(resp);
      const resolvedDataDate = resp?.data_trade_date;
      if (resolvedDataDate && checkDate && resolvedDataDate !== checkDate) {
        setInferenceDate(dayjs(resolvedDataDate));
        message.info(`所选日期 ${checkDate} 无可用数据，已回退到最新数据日 ${resolvedDataDate}`);
      }
      return resp;
    } catch {
      setPrecheck(null);
      return null;
    } finally {
      setPrecheckLoading(false);
    }
  }, []);

  const loadAutoSettings = useCallback(async (modelId: string) => {
    try {
      setAutoSettings(await modelTrainingService.getAutoInferenceSettings(modelId));
    } catch {
      setAutoSettings(null);
    }
  }, []);

  const loadLatestRun = useCallback(async (modelId: string) => {
    setLatestRunLoading(true);
    try {
      setLatestRun(await modelTrainingService.getLatestInferenceRun(modelId));
    } catch {
      setLatestRun(null);
    } finally {
      setLatestRunLoading(false);
    }
  }, []);

  // 选中模型变化或基准日变化时刷新左栏三块状态
  useEffect(() => {
    const modelId = selectedModel?.model_id;
    if (!modelId) return;
    const currentDate = inferenceDate ? inferenceDate.format('YYYY-MM-DD') : undefined;
    void Promise.all([
      loadPrecheck(modelId, currentDate),
      loadAutoSettings(modelId),
      loadLatestRun(modelId),
    ]);
  }, [selectedModel?.model_id, inferenceDate, loadPrecheck, loadAutoSettings, loadLatestRun]);

  // ── 预测目标日 ────────────────────────────────────────────────
  const loadTargetDate = useCallback(async () => {
    if (!inferenceDate) {
      setTargetDate('—');
      return;
    }
    setTargetDateLoading(true);
    try {
      const base = inferenceDate.format('YYYY-MM-DD');
      const resolved = await modelTrainingService.resolveInferenceDateByCalendar(calendar, base);
      const predicted = await modelTrainingService.calcTargetDateByCalendar(calendar, resolved.date, horizonDays);
      setTargetDate(predicted || '—');
    } catch {
      setTargetDate('—');
    } finally {
      setTargetDateLoading(false);
    }
  }, [inferenceDate, horizonDays, calendar]);

  useEffect(() => {
    void loadTargetDate();
  }, [loadTargetDate]);

  // ── 最近一次完成的推理 → 排名 ────────────────────────────────
  // 切模型时清空，再回填该模型最近一条已完成记录；用户手动跑完后由 runInference 直接写入。
  useEffect(() => {
    const modelId = selectedModel?.model_id;
    setLastRun(null);
    if (!modelId) return;
    let cancelled = false;
    modelTrainingService
      .listInferenceHistory(modelId, { page: 1, pageSize: HISTORY_PAGE_SIZE })
      .then((resp) => {
        if (cancelled) return;
        const completed = resp.items.filter((r) => r.status === 'completed');
        setLastRun(completed[0] ?? null);
        // 备用批次：明细没落库时按新鲜度往下找（内容不变就不重设，避免触发排名 effect 重跑）
        const fallbacks = completed.slice(1, 1 + RANKING_FALLBACK_DEPTH);
        const key = fallbacks.map((r) => r.run_id).join(',');
        if (key !== fallbackKeyRef.current) {
          fallbackKeyRef.current = key;
          setFallbackRuns(fallbacks);
        }
      })
      .catch(() => {
        // 该模型没有历史记录属正常空态，保持 null 由 UI 渲染引导文案
      });
    return () => {
      cancelled = true;
    };
  }, [selectedModel?.model_id]);

  useEffect(() => {
    const runId = lastRun?.run_id;
    if (!runId || lastRun?.status !== 'completed') {
      setRanking(null);
      setRankingFallbackFrom(null);
      return;
    }
    let cancelled = false;
    setRankingLoading(true);
    (async () => {
      const candidates = [lastRun, ...fallbackRuns];
      for (const candidate of candidates) {
        try {
          const r = await modelTrainingService.getInferenceResult(candidate.run_id);
          if (cancelled) return;
          if ((r?.rankings?.length ?? 0) > 0 || candidate.run_id === runId) {
            // 命中第一批有明细的；首批本身就是空的也照样展示（UI 会说明原因）
            if ((r?.rankings?.length ?? 0) > 0 && candidate.run_id !== runId) {
              setRankingFallbackFrom(candidate.run_id);
            } else {
              setRankingFallbackFrom(null);
            }
            setRanking(r);
            return;
          }
        } catch {
          if (cancelled) return;
        }
      }
      if (!cancelled) {
        setRanking(null);
        setRankingFallbackFrom(null);
      }
    })()
      .finally(() => {
        if (!cancelled) setRankingLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [lastRun?.run_id, lastRun?.status, fallbackRuns]);
  // 说明：fallbackRuns 由上面的历史加载写入，且写入前做过内容比对，
  // 因此这里的依赖不会因为数组引用变化而空转。

  // ── 动作 ─────────────────────────────────────────────────────
  const runInference = useCallback(async () => {
    if (!selectedModel || !inferenceDate) return;
    setRunning(true);
    setLastRun(null);
    try {
      const requestedDateStr = inferenceDate.format('YYYY-MM-DD');
      const resolvedDate = await modelTrainingService.resolveInferenceDateByCalendar(calendar, requestedDateStr);
      const inferenceDateStr = resolvedDate.date;
      if (resolvedDate.adjusted && inferenceDateStr) {
        setInferenceDate(dayjs(inferenceDateStr));
        message.info(`所选日期 ${requestedDateStr} 非交易日，已自动回退到最近交易日 ${inferenceDateStr}`);
      }
      const checked = await loadPrecheck(selectedModel.model_id, inferenceDateStr);
      if (!checked?.passed) {
        message.error('前置检查未通过，请先处理阻断项');
        return;
      }
      const runInfo = await modelTrainingService.runModelInference(
        selectedModel.model_id,
        inferenceDateStr,
        pool?.ref ?? undefined,
      );
      setLastRun(runInfo);
      message.success(
        `截面推理已完成: 产物已入库（样本数: ${runInfo.signals_count}${pool?.name ? `，股票池: ${pool.name}` : ''}）`,
      );
      void loadLatestRun(selectedModel.model_id);
    } catch (err: any) {
      message.error(`推理失败: ${err?.message ?? '未知错误'}`);
    } finally {
      setRunning(false);
    }
  }, [selectedModel, inferenceDate, calendar, loadPrecheck, pool?.ref, pool?.name, loadLatestRun]);

  const setDefaultModel = useCallback(async () => {
    if (!selectedModel) return;
    const canonicalId = selectedModel.model_id.startsWith('sys-')
      ? selectedModel.model_id.slice(4)
      : selectedModel.model_id;
    try {
      await modelTrainingService.setDefaultModel(canonicalId);
      message.success(`已设为默认模型：${selectedModel.model_id}`);
      await loadRegisteredModels();
    } catch (err: any) {
      message.error(`设置失败: ${err?.message ?? '未知'}`);
    }
  }, [selectedModel, loadRegisteredModels]);

  const toggleAuto = useCallback(
    async (enabled: boolean) => {
      if (!selectedModel || !autoSettings) return;
      setAutoSaving(true);
      try {
        const saved = await modelTrainingService.saveAutoInferenceSettings(selectedModel.model_id, {
          ...autoSettings,
          enabled,
        });
        setAutoSettings(saved);
        message.success(enabled ? '自动推理已开启' : '自动推理已关闭');
      } catch {
        message.error('保存失败');
      } finally {
        setAutoSaving(false);
      }
    },
    [selectedModel, autoSettings],
  );

  const refreshPrecheck = useCallback(() => {
    if (!selectedModel) return;
    void loadPrecheck(selectedModel.model_id, inferenceDate?.format('YYYY-MM-DD'));
  }, [selectedModel, inferenceDate, loadPrecheck]);

  /** 删除一条推理历史：删除后重算「最近一次完成的推理」，排名榜随之刷新 */
  const deleteHistory = useCallback(
    async (runId: string) => {
      const modelId = selectedModel?.model_id;
      try {
        await modelTrainingService.deleteInferenceHistory(runId);
        message.success('历史记录已删除');
        if (!modelId) return;
        const resp = await modelTrainingService.listInferenceHistory(modelId, {
          page: 1,
          pageSize: HISTORY_PAGE_SIZE,
        });
        setLastRun(resp.items.find((r) => r.status === 'completed') ?? null);
        void loadLatestRun(modelId);
      } catch (err: any) {
        message.error(`删除失败: ${err?.message ?? '未知'}`);
      }
    },
    [selectedModel?.model_id, loadLatestRun],
  );

  return {
    registeredModels,
    modelsLoading,
    selectedModelId,
    setSelectedModelId,
    selectedModel,
    horizonDays,

    inferenceDate,
    setInferenceDate,
    targetDate,
    targetDateLoading,

    precheck,
    precheckLoading,
    refreshPrecheck,

    running,
    runInference,
    lastRun,
    setDefaultModel,

    autoSettings,
    autoSaving,
    toggleAuto,
    latestRun,
    latestRunLoading,

    ranking,
    rankingLoading,
    rankingFallbackFrom,
    deleteHistory,

    pool,
    setPool,
  };
}
