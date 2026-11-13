/**
 * 个股预测推理（Individual Stock Inference）状态机。
 *
 * 从 InferenceCenterShell 抽出：标的联想、周期/基准日、模型选型、预测结果与 K 线。
 * 支持被「截面推理排名」联动触发 —— 传 symbol 即按该标的出图，不重新输入代码。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import dayjs, { type Dayjs } from 'dayjs';
import { message } from 'antd';
import {
  inferenceCenterService,
  type AvailableModelOption,
  type FeatureDriverItem,
  type KlineItem,
  type SingleStockPredictionResponse,
} from '../../../services/inferenceCenterService';
import type { SuggestionItem } from '../adapter';
import { useStockScoreSeries } from '../../shared/hooks/useStockScoreSeries';
import type {
  StockScoreHistoryItem,
  StockScoreModelMeta,
  StockScoreWindow,
} from '../../../services/modelTrainingService';

/** 可用模型卡片：在市场返回的模型上补出分类与标签，供 UI 直接渲染 */
export type ModelCardOption = AvailableModelOption & {
  category: 'tree' | 'dl' | 'ensemble';
  tag: string;
  /** 周期描述：由模型真实训练周期生成（不再硬编码「T+1 ~ T+10 灵活周期」） */
  horizonDesc: string;
};

/** 一个可选的预测周期及其在该市场的模型覆盖 */
export interface HorizonOption {
  horizon: number;
  modelCount: number;
}

export interface IndividualPrediction {
  symbol: string;
  inputCode: string;
  setInputCode: (v: string) => void;

  horizon: number;
  setHorizon: (h: number) => void;
  date: Dayjs | null;
  setDate: (d: Dayjs | null) => void;

  models: ModelCardOption[];
  filteredModels: ModelCardOption[];
  /** 该市场真实存在的预测周期（按 T+N 升序）；周期选择器只渲染这些 */
  horizonOptions: HorizonOption[];
  modelId: string;
  setModelId: (id: string) => void;
  selectedModel: ModelCardOption | undefined;
  categoryFilter: ModelCategoryFilter;
  setCategoryFilter: (c: ModelCategoryFilter) => void;

  /**
   * 点名参与共识的模型（最多 CONSENSUS_PICK_LIMIT 个）。
   * 空 = 只显示当日已落库的分数；非空 + execute=true 时后端会**现场补算**
   * 这些模型在该标的该日的分数——这是「多模型共识」唯一能拿到多行数据的路径。
   */
  consensusModelIds: string[];
  setConsensusModelIds: (ids: string[]) => void;

  loading: boolean;
  /** 同标的换模型/换周期：保留旧图，背景刷新（不要清空整块结果区） */
  refreshing: boolean;
  prediction: SingleStockPredictionResponse | null;
  kline: KlineItem[];

  /** 与上方预测同源、同模型、同 as-of 的历史分数序列（供 K 线叠加） */
  scorePoints: StockScoreHistoryItem[];
  scoreModels: StockScoreModelMeta[];
  scoreWindow: StockScoreWindow | null;
  scoreWindowWarning: string | null;
  scoreLastDate: string | null;
  scoreStaleDays: number | null;
  scoreSource: string;
  scoreLoading: boolean;
  scoreRefreshing: boolean;
  scoreError: Error | null;

  /** SHAP 归因（核心图先出，异步单独取；旧后端内联时直接复用） */
  drivers: FeatureDriverItem[];
  driversSource: 'shap' | null;
  driversNote: string | null;
  driversModelName: string | null;
  driversLoading: boolean;
  driversError: Error | null;

  suggestions: SuggestionItem[];
  showSuggestions: boolean;
  setShowSuggestions: (v: boolean) => void;
  selectSuggestion: (item: SuggestionItem) => void;
  /** 输入框提交（回车 / 失焦）：归一代码后按已有分数出图，不触发新推理 */
  commitCode: (raw: string) => void;
  /** 点「开始个股推理」：真实执行一次模型推理（execute=true，较慢） */
  execute: () => void;
  /** 联动入口：按指定标的出图（execute=false，直读已落库分数） */
  predictFor: (symbol: string, options?: { modelId?: string }) => void;
}

export type ModelCategoryFilter = 'all' | 'dl' | 'tree' | 'ensemble';

/** 联想输入最短字数：低于此长度不请求 */
const MIN_SUGGEST_LENGTH = 2;
/** 输入防抖：避免逐字符打联想源 */
const SUGGEST_DEBOUNCE_MS = 250;
/** 联想下拉最多展示条数 */
const SUGGEST_LIMIT = 8;
/** K 线回看天数：覆盖基准日前的形态窗口 */
const KLINE_LOOKBACK_DAYS = 60;
/**
 * 基准日往前多取的天数。
 *
 * 太短会看不出一段完整形态：100 自然日≈68 根，图上只有三个多月的走势，
 * 而 T+5 的预测扇形本身要占掉 5 根 —— 历史像是被预测挤在左边。
 * 放到 150≈100 根（约五个月），历史撑住画面，预测只剩不到 5% 的宽度，
 * 「历史看趋势、预测看方向」的主次才成立。不参与任何数字口径，纯展示窗口。
 */
const KLINE_PRE_BASE_DAYS = 150;

/**
 * 共识点名上限：与后端 `CONSENSUS_EXEC_LIMIT` 一致。
 *
 * 这不是随手定的数——每点名一个模型，`execute=true` 时后端就要现场起一个完整
 * 推理进程（实测单模型约 30s），并发度也等于这个上限。所以它同时是「一次研判
 * 最多点几个模型」的产品约束和 CPU 护栏，前后端必须同步改。
 */
export const CONSENSUS_PICK_LIMIT = 4;

/** 模型 id/类型 → 分类：ensemble > dl > tree */
function classifyModel(kind: string): ModelCardOption['category'] {
  const k = kind.toLowerCase();
  if (k.includes('ensemble') || k.includes('stacking')) return 'ensemble';
  if (
    k.includes('tft') || k.includes('gru') || k.includes('lstm') ||
    k.includes('transformer') || k.includes('pytorch') || k.includes('tensorflow') || k.includes('dl')
  ) {
    return 'dl';
  }
  return 'tree';
}

export function useIndividualPrediction(
  adapter: { market: string; defaultSymbol: string; search: (kw: string) => Promise<SuggestionItem[]>; normalize: (raw: string) => string; toSymbol: (item: SuggestionItem) => string; toSuffixSymbol: (symbol: string) => string; fetchKline: (symbol: string, days: number, endDate?: string, startDate?: string) => Promise<KlineItem[]> },
): IndividualPrediction {
  const { market, defaultSymbol, search, normalize, toSymbol, toSuffixSymbol, fetchKline } = adapter;

  const [symbol, setSymbol] = useState(defaultSymbol);
  const [inputCode, setInputCode] = useState(defaultSymbol);
  const [horizon, setHorizon] = useState(5);
  const [date, setDate] = useState<Dayjs | null>(dayjs());

  const [models, setModels] = useState<ModelCardOption[]>([]);
  const [modelId, setModelId] = useState('');
  const [categoryFilter, setCategoryFilter] = useState<ModelCategoryFilter>('all');
  /** 点名参与共识的模型 id；空 = 自动模式（只读当日已落库分数） */
  const [consensusModelIds, setConsensusModelIds] = useState<string[]>([]);

  const [loading, setLoading] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [kline, setKline] = useState<KlineItem[]>([]);
  const [prediction, setPrediction] = useState<SingleStockPredictionResponse | null>(null);
  const predictionRef = useRef<SingleStockPredictionResponse | null>(null);
  useEffect(() => {
    predictionRef.current = prediction;
  }, [prediction]);

  // 异步 SHAP 归因：核心图先出，归因随后到；旧后端内联返回时直接复用。
  const [drivers, setDrivers] = useState<FeatureDriverItem[]>([]);
  const [driversSource, setDriversSource] = useState<'shap' | null>(null);
  const [driversNote, setDriversNote] = useState<string | null>(null);
  const [driversModelName, setDriversModelName] = useState<string | null>(null);
  const [driversLoading, setDriversLoading] = useState(false);
  const [driversError, setDriversError] = useState<Error | null>(null);
  const driversKeyRef = useRef<string>('');

  const [suggestions, setSuggestions] = useState<SuggestionItem[]>([]);
  const [showSuggestions, setShowSuggestions] = useState(false);

  // ── 可用模型（按市场）───────────────────────────────────────
  useEffect(() => {
    let cancelled = false;
    inferenceCenterService
      .getAvailableModels(market)
      .then((list) => {
        if (cancelled) return;
        const options: ModelCardOption[] = (list || [])
          .filter((m) => Boolean(m.modelId))
          .map((m) => {
            const h = typeof m.horizon === 'number' && m.horizon > 0 ? m.horizon : null;
            return {
              ...m,
              category: classifyModel(String(m.modelType || m.modelId || '')),
              tag: m.hasInference ? '已训练' : '生产可用',
              // 周期描述取自模型 metadata；老模型未记录周期时如实说明而非编造
              horizonDesc: h ? `${h === 1 ? '次日' : `${h} 日`}周期 T+${h}` : '周期未记录',
            };
          });
        setModels(options);
      })
      .catch((err) => {
        console.warn('获取个股推理模型列表失败:', err);
      });
    return () => {
      cancelled = true;
    };
  }, [market]);

  // 模型对账：首次拿到列表、或原选中项已不在列表里时回退。
  // 优先「默认 + 有 pred 产物」→「默认」→「有 pred」→ 第一个；默认模型是
  // 用户显式设置的口径，不能因为列表排序把它静默换掉。
  useEffect(() => {
    if (models.length === 0) return;
    if (modelId && models.some((m) => m.modelId === modelId)) return;
    const preferred =
      models.find((m) => m.isDefault && m.hasPred !== false) ||
      models.find((m) => m.isDefault) ||
      models.find((m) => m.hasPred !== false) ||
      models[0];
    setModelId(preferred.modelId);
  }, [models, modelId]);

  // 周期与模型对账：用户切到 T+20 后，当前 T+5 模型必须让位给同周期模型，
  // 否则后端会回填 horizon_warning，而图上的分数仍是 T+5 的语义。
  useEffect(() => {
    if (!modelId || models.length === 0) return;
    const current = models.find((m) => m.modelId === modelId);
    if (!current) return;
    if (current.horizon == null || current.horizon === horizon) return;
    const same = models.find((m) => m.horizon === horizon);
    if (same) setModelId(same.modelId);
  }, [horizon, models, modelId]);

  // 共识点名对账：切换市场会换掉整份模型列表，旧市场点名的 id 留在状态里会变成
  // 一次注定失败的现场推理（后端按 id 解析、解不到就记失败）。这里按下沉列表剔除，
  // 并保留用户原有的相对顺序。
  useEffect(() => {
    if (consensusModelIds.length === 0 || models.length === 0) return;
    const alive = new Set(models.map((m) => m.modelId));
    const kept = consensusModelIds.filter((id) => alive.has(id));
    if (kept.length !== consensusModelIds.length) setConsensusModelIds(kept);
  }, [models, consensusModelIds]);

  // ── 标的联想（防抖）─────────────────────────────────────────
  useEffect(() => {
    const kw = inputCode.trim();
    if (!showSuggestions || kw.length < MIN_SUGGEST_LENGTH) {
      setSuggestions([]);
      return;
    }
    let cancelled = false;
    const timer = setTimeout(async () => {
      try {
        const results = await search(kw);
        if (!cancelled) setSuggestions(results.slice(0, SUGGEST_LIMIT));
      } catch {
        if (!cancelled) setSuggestions([]);
      }
    }, SUGGEST_DEBOUNCE_MS);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [inputCode, showSuggestions, search]);

  const filteredModels = useMemo(() => {
    // 先按当前预测周期过滤：周期选择器的语义就是「用哪个 T+N 模型」，
    // 把 T+5 模型留在 T+20 的下拉里只会制造 horizon_warning。
    // 该周期一个模型都没有时保留全量（老模型可能没记录 horizon），由后端告警。
    const horizonMatched = models.filter((m) => m.horizon === horizon);
    const base = horizonMatched.length > 0 ? horizonMatched : models;
    return categoryFilter === 'all' ? base : base.filter((m) => m.category === categoryFilter);
  }, [models, categoryFilter, horizon]);

  const selectedModel = useMemo(
    () => models.find((m) => m.modelId === modelId) || models[0],
    [models, modelId],
  );

  // 可用周期 = 该市场模型真实覆盖的周期并集。前端不再凭空提供 T+1/T+3/T+5/T+10
  // （实测 T+3 在部分市场一个模型都没有，选了也只是回显请求值、分数不变）。
  const horizonOptions = useMemo<HorizonOption[]>(() => {
    const counts = new Map<number, number>();
    for (const m of models) {
      const h = typeof m.horizon === 'number' && m.horizon > 0 ? m.horizon : null;
      if (h) counts.set(h, (counts.get(h) || 0) + 1);
    }
    return [...counts.entries()]
      .map(([h, modelCount]) => ({ horizon: h, modelCount }))
      .sort((a, b) => a.horizon - b.horizon);
  }, [models]);

  // 周期对账：当前选中周期在该市场无模型时，落到最近的有模型周期。
  // 直接发请求也能跑（后端会代选并回告警），但 UI 先对齐能少一次无效往返。
  useEffect(() => {
    if (horizonOptions.length === 0) return;
    if (horizonOptions.some((o) => o.horizon === horizon)) return;
    const nearest = horizonOptions.reduce((best, o) =>
      Math.abs(o.horizon - horizon) < Math.abs(best.horizon - horizon) ? o : best,
    );
    setHorizon(nearest.horizon);
  }, [horizonOptions, horizon]);

  const run = useCallback(
    async (target: { symbol: string; modelId?: string; execute: boolean }) => {
      const sym = target.symbol.trim();
      if (!sym) {
        message.warning('请输入有效的股票代码');
        return;
      }
      const dateStr = date ? date.format('YYYY-MM-DD') : undefined;
      // K 线取 [基准日前 100 天, 最新] 全窗口：既覆盖基准日锚点，又能展示基准日后
      // 实际走势对照预测；数字口径（基准价/扇形）仍按基准日截断，无前视泄露
      const startStr = date ? date.subtract(KLINE_PRE_BASE_DAYS, 'day').format('YYYY-MM-DD') : undefined;

      // 同标的换模型/换周期：保留旧图，避免整块结果区闪成空白；换标的一律先清空，
      // 防止上一只股票的预测扇形配着新股票 K 线（历史踩过）。
      const sameSymbol = sym === symbol;
      const hadPrediction = Boolean(predictionRef.current) && sameSymbol;
      if (!hadPrediction) setPrediction(null);
      setLoading(!hadPrediction);
      setRefreshing(hadPrediction);
      try {
        const klineData = await fetchKline(sym, KLINE_LOOKBACK_DAYS, undefined, startStr);
        // 必须先清空再写入：早先只在「有数据」时 setKline，取数失败/该标的无行情时
        // 会**保留上一只股票的 K 线**，于是 A 股的价格曲线配着港股/美股的预测扇形
        // 同屏显示 —— 对研判是灾难级误导。
        setKline(klineData || []);

        const res = await inferenceCenterService.predictSingleStock({
          symbol: sym,
          // 用户显式选的模型必须真正下发；target.modelId 优先（排名联动），
          // 否则用当前下拉选中项。早先这里只认 target.modelId，下拉等于装饰。
          model_id: target.modelId || modelId || undefined,
          date: dateStr,
          horizon,
          market,
          consensus_model_ids: consensusModelIds.length ? consensusModelIds : undefined,
          execute: target.execute,
          // 核心图先出：归因单独走 /predict-stock/drivers，树模型 SHAP 不再阻塞
          // 分数与扇形（旧后端会忽略该字段并内联返回，下面的 effect 会自动复用）。
          drivers_mode: 'none',
        });

        if (res && res.status === 'success') {
          setPrediction(res);
          // 所选日期无数据时后端会回退到最近数据日，明确提示避免误以为选中日期生效
          if (dateStr && res.as_of_date && res.as_of_date !== dateStr) {
            message.info(`所选 ${dateStr} 无可用数据，已回退到最近数据日 ${res.as_of_date}`);
          }
          if (!modelId && res.model_id) setModelId(res.model_id);
        }
      } catch (e: any) {
        console.error('获取真实推理数据失败:', e);
        const apiMessage =
          e?.response?.data?.detail ||
          e?.response?.data?.error?.message ||
          e?.response?.data?.message;
        message.error(apiMessage || `推理接口异常: ${e?.message || '未知错误'}`);
      } finally {
        setLoading(false);
        setRefreshing(false);
      }
    },
    [date, horizon, market, modelId, symbol, consensusModelIds, fetchKline],
  );

  const commitCode = useCallback(
    (raw: string) => {
      if (!raw.trim()) return;
      const normalized = normalize(raw.trim());
      setSymbol(normalized);
      setInputCode(normalized);
      // 提交代码只读已有分数，不触发新的模型执行；带上当前模型，避免换完模型
      // 再回车又回到后端按周期挑第一个模型。
      void run({ symbol: normalized, modelId: modelId || undefined, execute: false });
    },
    [normalize, run, modelId],
  );

  const predictFor = useCallback(
    (target: string, options?: { modelId?: string }) => {
      if (!target.trim()) return;
      const normalized = normalize(target.trim());
      setSymbol(normalized);
      setInputCode(normalized);
      // 联动场景：截面推理刚跑完，分数已落库，直读即可（execute=false 秒出）
      if (options?.modelId) {
        const linked = models.find((m) => m.modelId === options.modelId);
        // 把周期/模型下拉同步到联动模型，避免图上是 T+20、下拉还停在 T+5。
        if (linked?.horizon) setHorizon(linked.horizon);
        setModelId(options.modelId);
        // 本次联动已经显式跑了一次，别让自动重拉 effect 再补一发。
        autoKeyRef.current = null;
      }
      void run({ symbol: normalized, modelId: options?.modelId, execute: false });
    },
    [normalize, run, models],
  );

  const execute = useCallback(() => {
    void run({ symbol, modelId: modelId || undefined, execute: true });
  }, [run, symbol, modelId]);

  // 下拉选完模型 / 切完周期立即出图：只读已落库分数（execute=false），
  // 不要求用户再点「开始个股推理」。首次由空 modelId 对账到默认模型时跳过，
  // 保持首屏不自动打接口的既有行为。
  const autoKeyRef = useRef<string | null>(null);
  useEffect(() => {
    if (!symbol || !modelId) return;
    // key 不含 symbol：换标的由 commitCode/predictFor 显式跑一次，避免再触发一次重复请求；
    // 模型/周期/基准日变化才是「同一张图换口径」，必须自动重拉。
    const key = `${modelId}|${horizon}|${date ? date.format('YYYY-MM-DD') : ''}`;
    if (autoKeyRef.current === null) {
      autoKeyRef.current = key;
      return;
    }
    if (autoKeyRef.current === key) return;
    autoKeyRef.current = key;
    const timer = setTimeout(() => {
      void run({ symbol, modelId, execute: false });
    }, 200);
    return () => clearTimeout(timer);
  }, [symbol, modelId, horizon, date, run]);

  // 与上方 K 线同窗口、同模型、同 as-of 的历史分数（右轴叠加 + 兑现日模式的底料）。
  // endDate 必须给：as-of 之后已落库的分数属于前视，不能出现在图上。
  // 用所选日期做终点并提前发起：预测接口较慢时分数可以先到，不串行等两次往返。
  const asOfDate = prediction?.as_of_date || (date ? date.format('YYYY-MM-DD') : undefined);
  const scoreQuery = useStockScoreSeries({
    symbol: toSuffixSymbol(symbol),
    market,
    // 用「预测实际使用的模型」而不是下拉当前值：换模型时先保留旧分数，
    // 等新预测回来再一起切换，避免新分数配旧扇形。
    modelId: prediction?.model_id || modelId || undefined,
    days: 260,
    endDate: asOfDate,
    enabled: Boolean(symbol && modelId),
  });

  // 归因 effect：旧后端 inline 返回时直接复用；新后端返回空 drivers 时单独取。
  useEffect(() => {
    if (!prediction) {
      setDrivers([]);
      setDriversSource(null);
      setDriversNote(null);
      setDriversModelName(null);
      setDriversError(null);
      setDriversLoading(false);
      driversKeyRef.current = '';
      return;
    }
    const inline = prediction.drivers ?? [];
    if (inline.length || prediction.drivers_note) {
      setDrivers(inline);
      setDriversSource(prediction.drivers_source ?? null);
      setDriversNote(prediction.drivers_note ?? null);
      setDriversModelName(prediction.drivers_model_name ?? null);
      setDriversError(null);
      setDriversLoading(false);
      return;
    }
    const key = `${prediction.symbol || symbol}|${prediction.model_id || ''}|${prediction.as_of_date || ''}|${prediction.horizon ?? horizon}`;
    if (driversKeyRef.current === key) return;
    driversKeyRef.current = key;
    let cancelled = false;
    setDrivers([]);
    setDriversSource(null);
    setDriversNote(null);
    setDriversModelName(null);
    setDriversError(null);
    setDriversLoading(true);
    inferenceCenterService
      .getPredictionDrivers({
        symbol: prediction.symbol || symbol,
        model_id: prediction.model_id || undefined,
        date: prediction.as_of_date || undefined,
        horizon: prediction.horizon ?? horizon,
        market,
        // 带上核心响应解析出的实际上下文：execute 路径不落库时，异步端重跑可能
        // 解析到另一个模型/日期，归因就会描述错对象。
        as_of_date: prediction.as_of_date || undefined,
        model_name: prediction.model_name || undefined,
        consensus_model_ids: (prediction.consensus || [])
          .map((c) => c.model_id)
          .filter(Boolean),
      })
      .then((res) => {
        if (cancelled) return;
        if (res.status === 'success') {
          setDrivers(res.drivers ?? []);
          setDriversSource(res.drivers_source ?? null);
          setDriversNote(res.drivers_note ?? null);
          setDriversModelName(res.drivers_model_name ?? null);
        } else {
          setDriversNote(res.drivers_note || res.error || '归因不可用');
        }
      })
      .catch((err: any) => {
        if (cancelled) return;
        const msg =
          err?.response?.data?.detail || err?.response?.data?.error?.message || err?.message || '归因请求失败';
        setDriversError(new Error(String(msg)));
        setDriversNote(`归因请求失败：${msg}`);
      })
      .finally(() => {
        if (!cancelled) setDriversLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [
    prediction?.model_id,
    prediction?.as_of_date,
    prediction?.symbol,
    prediction?.horizon,
    prediction,
    symbol,
    market,
    horizon,
  ]);

  const selectSuggestion = useCallback(
    (item: SuggestionItem) => {
      setShowSuggestions(false);
      setSuggestions([]);
      commitCode(toSymbol(item));
    },
    [commitCode, toSymbol],
  );

  return {
    symbol,
    inputCode,
    setInputCode,
    horizon,
    setHorizon,
    date,
    setDate,
    models,
    filteredModels,
    horizonOptions,
    modelId,
    setModelId,
    selectedModel,
    categoryFilter,
    setCategoryFilter,
    consensusModelIds,
    setConsensusModelIds,
    loading,
    refreshing,
    prediction,
    kline,
    scorePoints: scoreQuery.points,
    scoreModels: scoreQuery.models,
    scoreWindow: scoreQuery.window,
    scoreWindowWarning: scoreQuery.windowWarning,
    scoreLastDate: scoreQuery.lastScoreDate,
    scoreStaleDays: scoreQuery.staleDays,
    scoreSource: scoreQuery.source,
    scoreLoading: scoreQuery.loading,
    scoreRefreshing: scoreQuery.refreshing,
    scoreError: scoreQuery.error,
    drivers,
    driversSource,
    driversNote,
    driversModelName,
    driversLoading,
    driversError,
    suggestions,
    showSuggestions,
    setShowSuggestions,
    selectSuggestion,
    commitCode,
    execute,
    predictFor,
  };
}
