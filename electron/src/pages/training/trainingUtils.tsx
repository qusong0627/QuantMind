import React from 'react';
import dayjs, { Dayjs } from 'dayjs';
import { 
  Zap, Activity, BarChart, Database, ListFilter, Filter, LayoutGrid, CheckCircle2, Clock, Archive, XCircle
} from 'lucide-react';
import { 
  AdminModelFeatureCatalog, 
  AdminModelFeatureSuggestedPeriods 
} from '../../features/admin/types';

// ─── TYPES ───────────────────────────────────────────────────────────────────

export type TrainingStatus = 'draft' | 'running' | 'completed';
export type TargetMode = 'return' | 'classification';
export type SplitKey = 'train' | 'val' | 'test';
export type DealPrice = 'open' | 'close';
export type TimePeriodMap = Record<SplitKey, [Dayjs, Dayjs]>;

export interface TrainingTarget {
  mode: TargetMode;
  horizonDays: number;
}

export interface TrainingParams {
  learning_rate: number;
  num_leaves: number;
  max_depth: number;
  min_data_in_leaf: number;
  lambda_l1: number;
  lambda_l2: number;
  feature_fraction: number;
  bagging_fraction: number;
  num_boost_round: number;
  early_stopping_rounds: number;
  objective: 'regression' | 'binary';
  metric: 'l2' | 'rmse' | 'mae' | 'auc' | 'binary_logloss';
}

export interface TrainingContext {
  initialCapital: number;
  benchmark: string;
  commissionRate: number;
  slippage: number;
  dealPrice: DealPrice;
}

export interface TrainingRequestPayload {
  displayName: string;
  selectedFeatures: string[];
  featureCategories: string[];
  target: TrainingTarget;
  timePeriods: {
    train: [string, string];
    val: [string, string];
    test: [string, string];
  };
  params: TrainingParams;
  context: TrainingContext;
  generatedAt: string;
  labelFormula: string;
  effectiveTradeDate: string;
  trainingWindow: string;
}

export interface TrainingResult {
  modelId: string;
  modelName: string;
  request: TrainingRequestPayload;
  metadata: {
    display_name: string;
    target_horizon_days: number;
    target_mode: TargetMode;
    label_formula: string;
    training_window: string;
    feature_count: number;
    requested_feature_count: number;
    requested_features: string[];
    auto_appended_feature_count: number;
    auto_appended_features: string[];
    feature_categories: string[];
    benchmark: string;
    objective: string;
    metric: string;
    generated_at: string;
  };
  metrics?: {
    train: { rmse: number; auc: number; ic: number; rank_ic: number; rank_icir: number };
    val: { rmse: number; auc: number; ic: number; rank_ic: number; rank_icir: number };
    test: { rmse: number; auc: number; ic: number; rank_ic: number; rank_icir: number };
  };
  artifacts: string[];
  summary: {
    status: string;
    notes: string;
  };
  modelRegistration?: {
    modelId: string;
    status: string;
    error: string;
    storagePath: string;
    modelFile: string;
  };
  completedAt: string;
}

export interface TrainingDraft {
  displayName: string;
  displayNameMode: 'auto' | 'manual';
  selectedFeatures: string[];
  timePeriods: {
    train: [string, string];
    val: [string, string];
    test: [string, string];
  };
  target: TrainingTarget;
  params: TrainingParams;
  context: TrainingContext;
  lastSavedAt: string;
}

export interface FeatureOption {
  key: string;
  label: string;
}

export interface FeatureCategory {
  id: string;
  name: string;
  icon: React.ReactNode;
  features: FeatureOption[];
}

// ─── CONSTANTS ────────────────────────────────────────────────────────────────

export const STORAGE_KEY = 'qm:model-training:draft';
export const DEFAULT_MODEL_VERSION = 'Base';

export const DEFAULT_FEATURE_CATEGORIES: FeatureCategory[] = [
  {
    id: 'momentum',
    name: '动量',
    icon: <Zap size={14} />,
    features: [
      { key: 'mom_ret_1d', label: '1日收益率动量' },
      { key: 'mom_ret_3d', label: '3日收益率动量' },
      { key: 'mom_ret_5d', label: '5日收益率动量' },
      { key: 'mom_ret_10d', label: '10日收益率动量' },
      { key: 'mom_ret_20d', label: '20日收益率动量' },
      { key: 'mom_ret_60d', label: '60日收益率动量' },
      { key: 'mom_ma_gap_5', label: '收盘偏离5日均线' },
      { key: 'mom_ma_gap_10', label: '收盘偏离10日均线' },
      { key: 'mom_ma_gap_20', label: '收盘偏离20日均线' },
      { key: 'mom_macd_dif', label: 'MACD-DIF' },
      { key: 'mom_macd_dea', label: 'MACD-DEA' },
      { key: 'mom_macd_hist', label: 'MACD柱值' },
    ],
  },
  {
    id: 'volatility',
    name: '波动率',
    icon: <Activity size={14} />,
    features: [
      { key: 'vol_std_5', label: '5日收益率标准差' },
      { key: 'vol_std_10', label: '10日收益率标准差' },
      { key: 'vol_std_20', label: '20日收益率标准差' },
      { key: 'vol_atr_14', label: 'ATR(14)' },
      { key: 'vol_atr_20', label: 'ATR(20)' },
      { key: 'vol_parkinson_20', label: 'Parkinson波动率20日' },
      { key: 'vol_gk_20', label: 'Garman-Klass波动率20日' },
      { key: 'vol_rs_20', label: 'Rogers-Satchell波动率20日' },
      { key: 'vol_realized_rv', label: '已实现波动率RV' },
      { key: 'vol_realized_rrv', label: '稳健已实现波动率RRV' },
    ],
  },
  {
    id: 'volume',
    name: '成交量',
    icon: <BarChart size={14} />,
    features: [
      { key: 'open', label: '开盘价（复权）' },
      { key: 'high', label: '最高价（复权）' },
      { key: 'low', label: '最低价（复权）' },
      { key: 'close', label: '收盘价（复权）' },
      { key: 'volume', label: '成交量' },
      { key: 'factor', label: '复权因子' },
      { key: 'liq_volume', label: '成交量' },
      { key: 'liq_amount', label: '成交额' },
      { key: 'liq_turnover_os', label: '流通换手率' },
      { key: 'liq_volume_ma_5', label: '5日平均成交量' },
      { key: 'liq_volume_ma_10', label: '10日平均成交量' },
      { key: 'liq_volume_ma_20', label: '20日平均成交量' },
      { key: 'liq_volume_ratio_5', label: '量比(5日)' },
      { key: 'liq_volume_ratio_20', label: '量比(20日)' },
      { key: 'liq_amount_ma_5', label: '5日平均成交额' },
      { key: 'liq_amount_ma_20', label: '20日平均成交额' },
      { key: 'liq_amount_ratio_5', label: '额比(5日)' },
      { key: 'liq_amihud_20', label: 'Amihud非流动性20日' },
    ],
  },
  {
    id: 'fund_flow',
    name: '资金流',
    icon: <Database size={14} />,
    features: [
      { key: 'flow_net_amount_ratio', label: '总净流入占比' },
      { key: 'flow_large_net_ratio', label: '大单净流入占比' },
      { key: 'flow_medium_net_ratio', label: '中单净流入占比' },
      { key: 'flow_small_net_ratio', label: '小单净流入占比' },
      { key: 'flow_net_order_ratio', label: '净买入委托占比' },
      { key: 'flow_vpin', label: 'VPIN当日值' },
      { key: 'flow_esp', label: '有效价差Esp' },
      { key: 'flow_pressure_index', label: '资金压力指数' },
    ],
  },
  {
    id: 'style',
    name: '风格因子',
    icon: <ListFilter size={14} />,
    features: [
      { key: 'style_ln_mv_total', label: '总市值对数' },
      { key: 'style_ln_mv_float', label: '流通市值对数' },
      { key: 'style_bp', label: '账面市净率倒数(B/P)' },
      { key: 'style_ep_ttm', label: '盈利收益率(E/P)' },
      { key: 'style_beta_60', label: '60日市场Beta' },
      { key: 'style_idio_vol_60', label: '60日特质波动' },
      { key: 'style_valuation_composite', label: '估值复合分' },
      { key: 'style_size_percentile', label: '规模分位数' },
    ],
  },
];

export const PRESET_DEFAULT_FEATURES = [
  'open', 'high', 'low', 'close', 'volume', 'factor',
  'mom_ret_1d', 'mom_ret_5d', 'mom_ret_10d', 'mom_ret_20d',
  'mom_ma_gap_5', 'mom_ma_gap_20', 'mom_macd_hist', 'mom_rsi_14',
  'mom_kdj_k', 'mom_breakout_20d',
  'vol_std_20', 'vol_atr_14', 'vol_parkinson_20', 'vol_gk_20',
  'vol_rs_20', 'vol_downside_20', 'vol_realized_rv', 'vol_jump_zadj',
  'liq_volume', 'liq_amount', 'liq_turnover_os',
  'liq_volume_ma_20', 'liq_volume_ratio_5', 'liq_amount_ma_20', 'liq_amount_ratio_5',
  'liq_mfi_14', 'liq_amihud_20', 'liq_amihud_60', 'liq_accdist_20',
  'flow_net_amount', 'flow_net_amount_ratio', 'flow_large_net_amount',
  'flow_vpin', 'flow_vpin_ma_5', 'flow_vpin_ma_20',
  'style_ln_mv_total', 'style_ln_mv_float', 'style_beta_20', 'style_beta_60',
  'style_idio_vol_20', 'style_residual_ret_20',
  'ind_ret_1d', 'ind_ret_20d', 'ind_strength_20', 'ind_momentum_rank_20',
];

export const TRAINING_BASE_FEATURES = [
  'mom_ret_1d', 'mom_ret_5d', 'mom_ret_20d', 'liq_volume', 'liq_amount', 'liq_turnover_os',
];

export const EXTRA_FEATURE_LABELS: Record<string, string> = {
  liq_volume: '当日成交量',
  liq_amount: '当日成交额',
  mom_rsi_14: 'RSI(14)',
  mom_kdj_k: 'KDJ-K值',
  mom_breakout_20d: '20日突破强度',
  vol_downside_20: '下行波动率20日',
  vol_jump_zadj: '跳跃波动Z值',
  liq_mfi_14: '资金流量指标MFI(14)',
  liq_amihud_60: 'Amihud非流动性60日',
  liq_accdist_20: '20日累积派发指标',
  flow_net_amount: '总净流入金额',
  flow_large_net_amount: '大单净流入金额',
  flow_vpin_ma_5: '5日平均VPIN',
  flow_vpin_ma_20: '20日平均VPIN',
  style_beta_20: '20日市场Beta',
  style_idio_vol_20: '20日特质波动',
  style_residual_ret_20: '20日残差收益',
  ind_ret_1d: '所属行业1日收益',
  ind_ret_20d: '所属行业20日收益',
  ind_strength_20: '20日行业强度',
  ind_momentum_rank_20: '20日行业动量排名',
};

export const FEATURE_CATEGORY_ICON_MAP: Record<string, React.ReactNode> = {
  momentum: <Zap size={14} />,
  volatility: <Activity size={14} />,
  volume: <BarChart size={14} />,
  fund_flow: <Database size={14} />,
  style: <ListFilter size={14} />,
  industry: <Filter size={14} />,
  microstructure: <LayoutGrid size={14} />,
};

export const TARGET_PRESETS = [1, 3, 5, 10];

export const DEFAULT_TIME_PERIODS: TimePeriodMap = {
  train: [dayjs('2016-01-01'), dayjs('2023-12-31')],
  val: [dayjs('2024-01-01'), dayjs('2024-12-31')],
  test: [dayjs('2025-01-01'), dayjs('2025-12-31')],
};

export const LEGACY_DEFAULT_TIME_PERIODS: TimePeriodMap = {
  train: [dayjs('2020-01-01'), dayjs('2024-12-31')],
  val: [dayjs('2025-01-01'), dayjs('2025-06-30')],
  test: [dayjs('2025-07-01'), dayjs('2025-12-31')],
};

export const DEFAULT_PARAMS: TrainingParams = {
  learning_rate: 0.02,
  num_leaves: 31,
  max_depth: -1,
  min_data_in_leaf: 300,
  lambda_l1: 0.5,
  lambda_l2: 1.0,
  feature_fraction: 0.7,
  bagging_fraction: 0.8,
  num_boost_round: 2000,
  early_stopping_rounds: 50,
  objective: 'regression',
  metric: 'l2',
};

export const DEFAULT_CONTEXT: TrainingContext = {
  initialCapital: 1000000,
  benchmark: 'SH000300',
  commissionRate: 0.00025,
  slippage: 0.0005,
  dealPrice: 'open',
};

export const DEFAULT_TARGET: TrainingTarget = {
  mode: 'return',
  horizonDays: 5,
};

// ─── HELPERS ─────────────────────────────────────────────────────────────────

export const formatRange = ([start, end]: [Dayjs, Dayjs]) => 
  `${start.format('YYYY-MM-DD')} → ${end.format('YYYY-MM-DD')}`;

export const daysBetween = ([start, end]: [Dayjs, Dayjs]) => 
  Math.max(1, end.diff(start, 'day'));

export const toISOStringRange = ([start, end]: [Dayjs, Dayjs]) => 
  [start.toISOString(), end.toISOString()] as [string, string];

export const restoreRange = (range: [string, string] | undefined, fallback: [Dayjs, Dayjs]): [Dayjs, Dayjs] => {
  if (!range?.[0] || !range?.[1]) return fallback;
  const start = dayjs(range[0]);
  const end = dayjs(range[1]);
  if (!start.isValid() || !end.isValid()) return fallback;
  return [start, end];
};

export const isSameRange = (left: [Dayjs, Dayjs], right: [Dayjs, Dayjs]) => {
  return left[0].format('YYYY-MM-DD') === right[0].format('YYYY-MM-DD') && 
         left[1].format('YYYY-MM-DD') === right[1].format('YYYY-MM-DD');
};

export const shouldMigrateLegacyDraftPeriods = (draftPeriods?: TrainingDraft['timePeriods']) => {
  if (!draftPeriods) return false;
  const restoredLegacy = {
    train: restoreRange(draftPeriods.train, LEGACY_DEFAULT_TIME_PERIODS.train),
    val: restoreRange(draftPeriods.val, LEGACY_DEFAULT_TIME_PERIODS.val),
    test: restoreRange(draftPeriods.test, LEGACY_DEFAULT_TIME_PERIODS.test),
  };
  return (
    isSameRange(restoredLegacy.train, LEGACY_DEFAULT_TIME_PERIODS.train) &&
    isSameRange(restoredLegacy.val, LEGACY_DEFAULT_TIME_PERIODS.val) &&
    isSameRange(restoredLegacy.test, LEGACY_DEFAULT_TIME_PERIODS.test)
  );
};

export const parseSuggestedTimePeriods = (
  suggested?: AdminModelFeatureSuggestedPeriods | null
): TimePeriodMap | null => {
  if (!suggested?.train || !suggested?.val || !suggested?.test) return null;
  const train = [dayjs(suggested.train[0]), dayjs(suggested.train[1])] as [Dayjs, Dayjs];
  const val = [dayjs(suggested.val[0]), dayjs(suggested.val[1])] as [Dayjs, Dayjs];
  const test = [dayjs(suggested.test[0]), dayjs(suggested.test[1])] as [Dayjs, Dayjs];
  if (!train[0].isValid() || !train[1].isValid() || !val[0].isValid() || !val[1].isValid() || !test[0].isValid() || !test[1].isValid()) {
    return null;
  }
  return { train, val, test };
};

export const buildLabelFormula = (target: TrainingTarget) => {
  if (target.mode === 'classification') {
    return `label = 1[ future_return(T, T+${target.horizonDays}) > 0 ]`;
  }
  return `label = future_return(T, T+${target.horizonDays}) = close(T+${target.horizonDays}) / close(T) - 1`;
};

export const buildEffectiveTradeDate = (target: TrainingTarget, referenceDate: Dayjs) => {
  return referenceDate.add(target.horizonDays, 'day').format('YYYY-MM-DD');
};

export const buildAutoDisplayName = (referenceDate: Dayjs, target: TrainingTarget, featureCount: number, version = DEFAULT_MODEL_VERSION) => {
  const dateToken = referenceDate.format('DD');
  const returnToken = `T${target.horizonDays}`;
  const dimensionToken = `Alpha${Math.max(1, featureCount)}`;
  return `${dateToken}_${returnToken}_${dimensionToken}_${version}`;
};

export const summarizeFeatureCategories = (features: string[], categories: FeatureCategory[]) => {
  return categories
    .filter((category) => features.some((featureKey) => category.features.some((feature) => feature.key === featureKey)))
    .map((category) => category.name);
};

export const buildFeatureLabelMap = (categories: FeatureCategory[] = DEFAULT_FEATURE_CATEGORIES) => {
  const labels: Record<string, string> = { ...EXTRA_FEATURE_LABELS };
  categories.forEach((category) => {
    category.features.forEach((feature) => {
      if (feature.key && feature.label) labels[feature.key] = feature.label;
    });
  });
  return labels;
};

export const toDynamicCategories = (catalog: AdminModelFeatureCatalog): FeatureCategory[] => {
  return (catalog.categories || [])
    .slice()
    .sort((a, b) => (a.order || 0) - (b.order || 0))
    .map((category) => ({
      id: category.id,
      name: category.name,
      icon: FEATURE_CATEGORY_ICON_MAP[category.id] ?? <Database size={14} />,
      features: (category.features || [])
        .filter((feature) => feature.enabled !== false && feature.key)
        .sort((a, b) => (a.order_no || 0) - (b.order_no || 0))
        .map((feature) => ({
          key: feature.key,
          label: feature.feature_name || feature.key,
        })),
    }))
    .filter((category) => category.features.length > 0);
};

export const buildTrainingRequest = (
  selectedFeatures: string[],
  categories: FeatureCategory[],
  timePeriods: TimePeriodMap,
  target: TrainingTarget,
  params: TrainingParams,
  context: TrainingContext,
  displayName: string
): TrainingRequestPayload => {
  const finalFeatures = Array.from(new Set(selectedFeatures));
  const labelFormula = buildLabelFormula(target);
  const effectiveTradeDate = buildEffectiveTradeDate(target, timePeriods.test[0]);
  const trainingWindow = `${formatRange(timePeriods.train)} | ${formatRange(timePeriods.val)} | ${formatRange(timePeriods.test)}`;
  return {
    displayName: displayName.trim() || buildAutoDisplayName(dayjs(), target, finalFeatures.length),
    selectedFeatures: finalFeatures,
    featureCategories: summarizeFeatureCategories(finalFeatures, categories),
    target,
    timePeriods: {
      train: toISOStringRange(timePeriods.train),
      val: toISOStringRange(timePeriods.val),
      test: toISOStringRange(timePeriods.test),
    },
    params,
    context,
    generatedAt: new Date().toISOString(),
    labelFormula,
    effectiveTradeDate,
    trainingWindow,
  };
};

export const buildBackendTrainingPayload = (
  request: TrainingRequestPayload,
  timePeriods: TimePeriodMap,
): any => {
  const features = Array.from(new Set(request.selectedFeatures));
  const trainStart = dayjs(request.timePeriods.train[0]).format('YYYY-MM-DD');
  const trainEnd = dayjs(request.timePeriods.train[1]).format('YYYY-MM-DD');
  const validStart = dayjs(request.timePeriods.val[0]).format('YYYY-MM-DD');
  const validEnd = dayjs(request.timePeriods.val[1]).format('YYYY-MM-DD');
  const testStart = dayjs(request.timePeriods.test[0]).format('YYYY-MM-DD');
  const testEnd = dayjs(request.timePeriods.test[1]).format('YYYY-MM-DD');
  
  const splitTotal = Math.max(1, daysBetween(timePeriods.train) + daysBetween(timePeriods.val));
  const valRatio = Math.min(0.5, Math.max(0.01, daysBetween(timePeriods.val) / splitTotal));

  return {
    job_name: `model_train_t${request.target.horizonDays}_${dayjs().format('YYYYMMDDHHmmss')}`,
    display_name: request.displayName,
    model_type: 'lightgbm',
    train_start: trainStart,
    train_end: trainEnd,
    valid_start: validStart,
    valid_end: validEnd,
    test_start: testStart,
    test_end: testEnd,
    val_ratio: Number(valRatio.toFixed(4)),
    num_boost_round: request.params.num_boost_round,
    early_stopping_rounds: request.params.early_stopping_rounds,
    features,
    feature_categories: request.featureCategories,
    target_horizon_days: request.target.horizonDays,
    target_mode: request.target.mode,
    label_formula: request.labelFormula,
    effective_trade_date: request.effectiveTradeDate,
    training_window: request.trainingWindow,
    generated_at: request.generatedAt,
    context: {
      initial_capital: request.context.initialCapital,
      benchmark: request.context.benchmark,
      commission_rate: request.context.commissionRate,
      slippage: request.context.slippage,
      deal_price: request.context.dealPrice,
    },
    lgb_params: {
      learning_rate: request.params.learning_rate,
      num_leaves: request.params.num_leaves,
      max_depth: request.params.max_depth,
      min_data_in_leaf: request.params.min_data_in_leaf,
      lambda_l1: request.params.lambda_l1,
      lambda_l2: request.params.lambda_l2,
      feature_fraction: request.params.feature_fraction,
      bagging_fraction: request.params.bagging_fraction,
      objective: request.params.objective,
      metric: request.params.metric,
    },
  };
};

const normalizeFeatureKeys = (features?: Array<string | null | undefined> | null): string[] => {
  if (!Array.isArray(features)) return [];
  return Array.from(
    new Set(
      features
        .map((feature) => String(feature ?? '').trim())
        .filter(Boolean),
    ),
  );
};

const TRAINING_BASE_FEATURES_NAMES = [
  'mom_ret_1d', 'mom_ret_5d', 'mom_ret_20d', 'liq_volume', 'liq_amount', 'liq_turnover_os',
];

export const resolveAutoAppendedFeatures = (request: TrainingRequestPayload, metadata: TrainingResult['metadata']): string[] => {
  const requestedFeatures = normalizeFeatureKeys(metadata.requested_features?.length ? metadata.requested_features : request.selectedFeatures);
  const autoAppendedFromMeta = normalizeFeatureKeys(metadata.auto_appended_features);
  if (autoAppendedFromMeta.length > 0) return autoAppendedFromMeta;
  return TRAINING_BASE_FEATURES_NAMES.filter((feature) => !requestedFeatures.includes(feature));
};

export const parseTrainingResult = (
  request: TrainingRequestPayload,
  runId: string,
  rawResult: any
): TrainingResult | null => {
  if (!rawResult) return null;

  const metrics = rawResult.metrics;
  const train = metrics?.train;
  const val = metrics?.val;
  const test = metrics?.test;
  if (!train || !val || !test) return null;

  const artifacts: string[] = Array.isArray(rawResult.artifacts)
    ? rawResult.artifacts
      .map((item: any) => {
        if (typeof item === 'string') return item;
        if (item && typeof item === 'object') {
          return String(item.name || item.filename || item.file || '').trim();
        }
        return '';
      })
      .filter(Boolean)
    : [];

  if (artifacts.length === 0) return null;

  const metadata = rawResult.metadata || {};
  const summary = rawResult.summary || {};
  const rawRegistration = rawResult.model_registration || {};
  const defaultMetadata = {
    display_name: request.displayName,
    target_horizon_days: request.target.horizonDays,
    target_mode: request.target.mode,
    label_formula: request.labelFormula,
    training_window: request.trainingWindow,
    feature_count: request.selectedFeatures.length,
    requested_feature_count: request.selectedFeatures.length,
    requested_features: request.selectedFeatures,
    auto_appended_feature_count: 0,
    auto_appended_features: [] as string[],
    feature_categories: request.featureCategories,
    benchmark: request.context.benchmark,
    objective: request.params.objective,
    metric: request.params.metric,
    generated_at: request.generatedAt,
  };

  return {
    modelId: String(metadata.model_id || runId),
    modelName: String(metadata.display_name || metadata.model_name || request.displayName || `T+${request.target.horizonDays} Horizon Model`),
    request,
    metadata: {
      ...defaultMetadata,
      ...metadata,
    },
    metrics: {
      train: {
        rmse: Number(train.rmse), auc: Number(train.auc),
        ic: Number(train.ic ?? (metadata as any)?.metrics?.train_ic ?? 0),
        rank_ic: Number(train.rank_ic ?? (metadata as any)?.metrics?.train_rank_ic ?? 0),
        rank_icir: Number(train.rank_icir ?? (metadata as any)?.metrics?.train_rank_icir ?? 0),
      },
      val: {
        rmse: Number(val.rmse), auc: Number(val.auc),
        ic: Number(val.ic ?? (metadata as any)?.metrics?.val_ic ?? 0),
        rank_ic: Number(val.rank_ic ?? (metadata as any)?.metrics?.val_rank_ic ?? 0),
        rank_icir: Number(val.rank_icir ?? (metadata as any)?.metrics?.val_rank_icir ?? 0),
      },
      test: {
        rmse: Number(test.rmse), auc: Number(test.auc),
        ic: Number(test.ic ?? (metadata as any)?.metrics?.test_ic ?? 0),
        rank_ic: Number(test.rank_ic ?? (metadata as any)?.metrics?.test_rank_ic ?? 0),
        rank_icir: Number(test.rank_icir ?? (metadata as any)?.metrics?.test_rank_icir ?? 0),
      },
    },
    artifacts,
    summary: {
      status: String(summary.status || '训练完成'),
      notes: String(summary.message || summary.notes || '训练结果已回传。'),
    },
    modelRegistration: {
      modelId: String(rawRegistration.model_id || metadata.model_id || runId),
      status: String(rawRegistration.status || ''),
      error: String(rawRegistration.error || ''),
      storagePath: String(rawRegistration.storage_path || ''),
      modelFile: String(rawRegistration.model_file || ''),
    },
    completedAt: new Date().toISOString(),
  };
};

export const getTargetModeDescription = (mode: TargetMode): string => {
  if (mode === 'classification') return '分类（预测涨跌方向）';
  return '回归（预测未来收益率）';
};

export const getObjectiveMetricDescription = (objective: string, metric: string): string => {
  const objectiveMap: Record<string, string> = {
    regression: '回归',
    binary: '二分类',
  };
  const metricMap: Record<string, string> = {
    l2: '均方误差 L2',
    rmse: '均方根误差 RMSE',
    mae: '平均绝对误差 MAE',
    auc: 'AUC',
    binary_logloss: '二分类对数损失',
  };
  const objectiveText = objectiveMap[objective] || objective;
  const metricText = metricMap[metric] || metric;
  return `${objectiveText} / ${metricText}`;
};

export function getStatusConfig(status: string) {
  switch (status) {
    case 'active':
    case 'ready':
      return { color: 'text-emerald-600', bg: 'bg-emerald-50', border: 'border-emerald-200', label: '已就绪', icon: <CheckCircle2 size={9} /> };
    case 'candidate':
      return { color: 'text-blue-600', bg: 'bg-blue-50', border: 'border-blue-200', label: '候选', icon: <Clock size={9} /> };
    case 'syncing':
      return { color: 'text-indigo-600', bg: 'bg-indigo-50', border: 'border-indigo-200', label: '已同步', icon: <CheckCircle2 size={9} /> };
    case 'failed':
      return { color: 'text-red-500', bg: 'bg-red-50', border: 'border-red-200', label: '失败', icon: <XCircle size={9} /> };
    case 'archived':
      return { color: 'text-slate-400', bg: 'bg-slate-100', border: 'border-slate-200', label: '已归档', icon: <Archive size={9} /> };
    default:
      return { color: 'text-slate-400', bg: 'bg-slate-100', border: 'border-slate-200', label: status || '未知', icon: <Clock size={9} /> };
  }
}
