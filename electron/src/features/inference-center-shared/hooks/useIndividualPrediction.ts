/**
 * 个股预测推理（Individual Stock Inference）状态机。
 *
 * 从 InferenceCenterShell 抽出：标的联想、周期/基准日、模型选型、预测结果与 K 线。
 * 支持被「截面推理排名」联动触发 —— 传 symbol 即按该标的出图，不重新输入代码。
 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import dayjs, { type Dayjs } from 'dayjs';
import { message } from 'antd';
import {
  inferenceCenterService,
  type AvailableModelOption,
  type KlineItem,
  type SingleStockPredictionResponse,
} from '../../../services/inferenceCenterService';
import type { SuggestionItem } from '../adapter';

/** 可用模型卡片：在市场返回的模型上补出分类与标签，供 UI 直接渲染 */
export type ModelCardOption = AvailableModelOption & {
  category: 'tree' | 'dl' | 'ensemble';
  tag: string;
  horizonDesc: string;
};

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
  modelId: string;
  setModelId: (id: string) => void;
  selectedModel: ModelCardOption | undefined;
  categoryFilter: ModelCategoryFilter;
  setCategoryFilter: (c: ModelCategoryFilter) => void;

  loading: boolean;
  prediction: SingleStockPredictionResponse | null;
  kline: KlineItem[];

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
/** 基准日往前多取的天数：用于展示基准日之后的真实走势做对照（不参与预测口径） */
const KLINE_PRE_BASE_DAYS = 100;

/**
 * 共识矩阵成员：当前版本没有选择 UI，空数组 = 后端自动取当日全部有分数的模型。
 * 保留该常量是为了让请求里的语义显式，后续加多选时只改这里。
 */
const CONSENSUS_MODEL_IDS: string[] = [];

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
  adapter: { market: string; defaultSymbol: string; search: (kw: string) => Promise<SuggestionItem[]>; normalize: (raw: string) => string; toSymbol: (item: SuggestionItem) => string; fetchKline: (symbol: string, days: number, endDate?: string, startDate?: string) => Promise<KlineItem[]> },
): IndividualPrediction {
  const { market, defaultSymbol, search, normalize, toSymbol, fetchKline } = adapter;

  const [symbol, setSymbol] = useState(defaultSymbol);
  const [inputCode, setInputCode] = useState(defaultSymbol);
  const [horizon, setHorizon] = useState(5);
  const [date, setDate] = useState<Dayjs | null>(dayjs());

  const [models, setModels] = useState<ModelCardOption[]>([]);
  const [modelId, setModelId] = useState('');
  const [categoryFilter, setCategoryFilter] = useState<ModelCategoryFilter>('all');

  const [loading, setLoading] = useState(false);
  const [kline, setKline] = useState<KlineItem[]>([]);
  const [prediction, setPrediction] = useState<SingleStockPredictionResponse | null>(null);

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
          .map((m) => ({
            ...m,
            category: classifyModel(String(m.modelType || m.modelId || '')),
            tag: m.hasInference ? '已训练' : '生产可用',
            horizonDesc: 'T+1 ~ T+10 灵活周期',
          }));
        setModels(options);
      })
      .catch((err) => {
        console.warn('获取个股推理模型列表失败:', err);
      });
    return () => {
      cancelled = true;
    };
  }, [market]);

  // 模型对账：首次拿到列表、或原选中项已不在列表里时回退到首个模型。
  // 用对账 effect 而非函数式 setState —— 本项目的 useState setter 类型不接受更新函数。
  useEffect(() => {
    if (models.length === 0) return;
    if (modelId && models.some((m) => m.modelId === modelId)) return;
    setModelId(models[0].modelId);
  }, [models, modelId]);

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

  const filteredModels = useMemo(
    () => (categoryFilter === 'all' ? models : models.filter((m) => m.category === categoryFilter)),
    [models, categoryFilter],
  );

  const selectedModel = useMemo(
    () => models.find((m) => m.modelId === modelId) || models[0],
    [models, modelId],
  );

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

      setPrediction(null);
      setLoading(true);
      try {
        const klineData = await fetchKline(sym, KLINE_LOOKBACK_DAYS, undefined, startStr);
        if (klineData && klineData.length > 0) setKline(klineData);

        const res = await inferenceCenterService.predictSingleStock({
          symbol: sym,
          model_id: target.modelId || undefined,
          date: dateStr,
          horizon,
          market,
          consensus_model_ids: CONSENSUS_MODEL_IDS.length ? CONSENSUS_MODEL_IDS : undefined,
          execute: target.execute,
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
      }
    },
    [date, horizon, market, modelId, fetchKline],
  );

  const commitCode = useCallback(
    (raw: string) => {
      if (!raw.trim()) return;
      const normalized = normalize(raw.trim());
      setSymbol(normalized);
      setInputCode(normalized);
      // 提交代码只读已有分数，不触发新的模型执行
      void run({ symbol: normalized, execute: false });
    },
    [normalize, run],
  );

  const predictFor = useCallback(
    (target: string, options?: { modelId?: string }) => {
      if (!target.trim()) return;
      const normalized = normalize(target.trim());
      setSymbol(normalized);
      setInputCode(normalized);
      // 联动场景：截面推理刚跑完，分数已落库，直读即可（execute=false 秒出）
      void run({ symbol: normalized, modelId: options?.modelId, execute: false });
    },
    [normalize, run],
  );

  const execute = useCallback(() => {
    void run({ symbol, execute: true });
  }, [run, symbol]);

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
    modelId,
    setModelId,
    selectedModel,
    categoryFilter,
    setCategoryFilter,
    loading,
    prediction,
    kline,
    suggestions,
    showSuggestions,
    setShowSuggestions,
    selectSuggestion,
    commitCode,
    execute,
    predictFor,
  };
}
