/**
 * 「模型管理 → 重新训练」配置映射。
 *
 * 输入是与后端 `GET /models/{id}/retrain-config` 同构的载荷：
 * `config` = 模型目录 `config.yaml` 的解析结果（首选），`metadata` = 模型注册表
 * 的 metadata_json 子集（缺 config 时兜底）。输出是训练页 `parseTrainingConfig`
 * 能直接消费的 `TrainingConfigFile`。
 *
 * 为什么不在后端直接产出训练页格式：训练配置 schema、默认值、市场开关都在
 * 前端单一出处（`trainingUtils`），后端只提供原始事实，避免两套口径漂移。
 */

import {
  DEFAULT_CONTEXT,
  DEFAULT_PARAMS,
  MODEL_TYPE_OPTIONS,
  TRAINING_MARKET_OPTIONS,
  buildTrainingConfigFile,
  normalizeTrainingTarget,
  type ModelType,
  type TrainingConfigFile,
  type TrainingContext,
  type TrainingDraft,
  type TrainingMarket,
  type TrainingParams,
} from './trainingUtils';

export interface RetrainConfigPayload {
  model_id: string;
  display_name?: string;
  market?: string;
  config?: Record<string, unknown> | null;
  metadata?: Record<string, unknown>;
  warnings?: string[];
}

export interface BuiltRetrainConfig {
  config: TrainingConfigFile;
  warnings: string[];
}

type Dict = Record<string, unknown>;

const asRecord = (value: unknown): Dict =>
  value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Dict)
    : {};

const asNumber = (value: unknown): number | undefined => {
  const num = Number(value);
  return Number.isFinite(num) ? num : undefined;
};

const asString = (value: unknown): string | undefined => {
  if (typeof value !== 'string') return undefined;
  const trimmed = value.trim();
  return trimmed ? trimmed : undefined;
};

const asStringArray = (value: unknown): string[] =>
  Array.isArray(value)
    ? value.filter((item): item is string => typeof item === 'string' && item.trim().length > 0).map((item) => item.trim())
    : [];

const asRange = (value: unknown): [string, string] | null => {
  const items = asStringArray(value);
  return items.length === 2 ? [items[0], items[1]] : null;
};

const toRange = (start: unknown, end: unknown): [string, string] | null => {
  const startText = asString(start);
  const endText = asString(end);
  return startText && endText ? [startText, endText] : null;
};

const MODEL_TYPES = new Set<string>(MODEL_TYPE_OPTIONS.map((option) => option.value));
const TRAINING_MARKETS = new Set<string>(TRAINING_MARKET_OPTIONS.map((option) => option.value));

/** 原展示名里「 · 训…/验…/测…」段是旧时间切分的描述，带入后必然过期，直接剥掉。 */
export const stripSplitSuffix = (name: string): string =>
  name.replace(/\s*·\s*训[^·]*$/u, '').trim();

const resolveModelTypes = (raw: unknown, fallback: ModelType): ModelType[] => {
  const types = asStringArray(raw).filter((item): item is ModelType => MODEL_TYPES.has(item));
  return types.length > 0 ? types : [fallback];
};

/**
 * 后端 config.yaml → 训练页草稿。
 *
 * 时间切分按模型原口径带入（train/valid/test 三段），用户可在训练页改完再提交；
 * 特征取 `data.features`（训练实际使用集合，含自动补全），缺失时回退
 * `requested_features` / metadata。
 */
export function buildRetrainTrainingConfig(payload: RetrainConfigPayload): BuiltRetrainConfig {
  const cfg = asRecord(payload.config);
  const meta = asRecord(payload.metadata);
  const data = asRecord(cfg.data);
  const model = asRecord(cfg.model);
  const label = asRecord(cfg.label);
  const preprocessing = asRecord(cfg.preprocessing);
  const sampleWeight = asRecord(cfg.sample_weight);
  const rawContext = asRecord(cfg.context);
  const split = asRecord(cfg.split);

  const warnings = [...(payload.warnings ?? [])];

  const modelTypeRaw = asString(model.type) ?? asString(meta.model_type);
  if (!modelTypeRaw || !MODEL_TYPES.has(modelTypeRaw)) {
    throw new Error(`模型类型不受支持：${modelTypeRaw ?? '未知'}`);
  }
  const modelType = modelTypeRaw as ModelType;

  const marketRaw = (
    asString(rawContext.market)
    ?? asString(payload.market)
    ?? asString(meta.market)
    ?? 'CN'
  ).toUpperCase();
  if (!TRAINING_MARKETS.has(marketRaw)) {
    throw new Error(`模型市场不受支持：${marketRaw}`);
  }
  const market = marketRaw as TrainingMarket;

  // 特征口径：优先用训练产物里的**有效特征**（metadata.features = train.py 的
  // valid_features），它才代表模型真正吃进去的列；config.yaml 的 data.features
  // 是**提交的候选集**（如 100 选 82、目录缺列被剔除时两者不同）。按候选集带入
  // 会把当年没进模型的特征也勾上。
  const effectiveFeatures = asStringArray(meta.features);
  const featureColumns = asStringArray(meta.feature_columns);
  const requestedInConfig = asStringArray(data.features);
  const requestedInConfigMeta = asStringArray(data.requested_features);
  const requestedInMeta = asStringArray(meta.requested_features);
  const features = effectiveFeatures.length > 0
    ? effectiveFeatures
    : featureColumns.length > 0
      ? featureColumns
      : requestedInConfig.length > 0
        ? requestedInConfig
        : requestedInConfigMeta.length > 0
          ? requestedInConfigMeta
          : requestedInMeta;
  const requestedCount = Math.max(
    requestedInConfig.length,
    requestedInConfigMeta.length,
    requestedInMeta.length,
  );
  if (features.length > 0 && requestedCount > features.length) {
    warnings.push(
      `该模型训练时实际使用 ${features.length} 个特征（提交 ${requestedCount} 个），已按实际使用的 ${features.length} 个带入`,
    );
  }
  if (features.length === 0) {
    warnings.push('模型未记录特征清单，训练页将按当前目录默认勾选');
  }

  const trainRange = asRange(split.train) ?? toRange(meta.train_start, meta.train_end);
  const validRange = asRange(split.valid) ?? toRange(meta.val_start, meta.val_end);
  const testRange = asRange(split.test) ?? toRange(meta.test_start, meta.test_end);
  if (!trainRange || !validRange || !testRange) {
    throw new Error('模型缺少完整的时间切分（训练/验证/测试），无法带入');
  }
  if (!cfg.split) {
    warnings.push('未找到 config.yaml 时间切分，已按 metadata 日期回退');
  }

  const params: TrainingParams = {
    ...DEFAULT_PARAMS,
    model_type: modelType,
    model_types: resolveModelTypes(model.types, modelType),
    prediction_mode: model.prediction_mode === 'quantile' ? 'quantile' : 'point',
    ensemble_method: model.ensemble === 'stacking' ? 'stacking' : 'none',
    num_boost_round: asNumber(model.num_boost_round) ?? DEFAULT_PARAMS.num_boost_round,
    early_stopping_rounds:
      asNumber(model.early_stopping_rounds) ?? DEFAULT_PARAMS.early_stopping_rounds,
    preprocessingEnabled:
      typeof preprocessing.enabled === 'boolean'
        ? preprocessing.enabled
        : DEFAULT_PARAMS.preprocessingEnabled,
    sampleTradeableMask: sampleWeight.tradeable_mask === true,
    sampleExcludeST: sampleWeight.st === true,
    sampleDecayHalfLifeDays: asNumber(sampleWeight.time_decay_half_life_days) ?? 0,
  };

  const modelParams = asRecord(model.params);
  const xgbParams = asRecord(model.xgb_params);
  const catParams = asRecord(model.catboost_params);
  const dlParams = asRecord(model.dl_params);

  const applyShared = (source: Dict) => {
    params.learning_rate = asNumber(source.learning_rate) ?? params.learning_rate;
    params.min_data_in_leaf = asNumber(source.min_data_in_leaf) ?? params.min_data_in_leaf;
    params.lambda_l1 = asNumber(source.lambda_l1) ?? params.lambda_l1;
    params.lambda_l2 = asNumber(source.lambda_l2) ?? params.lambda_l2;
    params.feature_fraction = asNumber(source.feature_fraction) ?? params.feature_fraction;
    params.bagging_fraction = asNumber(source.bagging_fraction) ?? params.bagging_fraction;
    params.bagging_freq = asNumber(source.bagging_freq) ?? params.bagging_freq;
    if (source.objective === 'binary' || source.objective === 'regression') {
      params.objective = source.objective;
    }
    if (source.metric === 'l2' || source.metric === 'rmse' || source.metric === 'mae'
      || source.metric === 'auc' || source.metric === 'binary_logloss') {
      params.metric = source.metric;
    }
  };

  if (modelType === 'lightgbm') {
    applyShared({ ...modelParams, objective: asString(meta.objective) ?? modelParams.objective });
    params.num_leaves = asNumber(modelParams.num_leaves) ?? params.num_leaves;
    params.max_depth = asNumber(modelParams.max_depth) ?? params.max_depth;
    params.path_smooth = asNumber(modelParams.path_smooth) ?? params.path_smooth;
  } else if (modelType === 'xgboost') {
    applyShared({ ...xgbParams, objective: asString(meta.objective) ?? xgbParams.objective });
    params.xgb_max_depth = asNumber(xgbParams.max_depth) ?? params.xgb_max_depth;
    params.xgb_learning_rate = asNumber(xgbParams.learning_rate) ?? params.xgb_learning_rate;
    params.xgb_subsample = asNumber(xgbParams.subsample) ?? params.xgb_subsample;
    params.xgb_colsample_bytree = asNumber(xgbParams.colsample_bytree) ?? params.xgb_colsample_bytree;
    params.xgb_reg_alpha = asNumber(xgbParams.reg_alpha) ?? params.xgb_reg_alpha;
    params.xgb_reg_lambda = asNumber(xgbParams.reg_lambda) ?? params.xgb_reg_lambda;
    params.xgb_min_child_weight = asNumber(xgbParams.min_child_weight) ?? params.xgb_min_child_weight;
  } else if (modelType === 'catboost') {
    applyShared({ ...catParams, objective: asString(meta.objective) ?? catParams.objective });
    params.cb_depth = asNumber(catParams.depth) ?? params.cb_depth;
    params.cb_learning_rate = asNumber(catParams.learning_rate) ?? params.cb_learning_rate;
    params.cb_l2_leaf_reg = asNumber(catParams.l2_leaf_reg) ?? params.cb_l2_leaf_reg;
    params.cb_random_strength = asNumber(catParams.random_strength) ?? params.cb_random_strength;
    params.cb_bagging_temperature = asNumber(catParams.bagging_temperature) ?? params.cb_bagging_temperature;
    params.cb_od_wait = asNumber(catParams.od_wait) ?? params.cb_od_wait;
  } else if (modelType === 'linear') {
    params.linear_alpha = asNumber(asRecord(modelParams).alpha) ?? params.linear_alpha;
  } else if (modelType === 'random_forest') {
    params.rf_n_estimators = asNumber(dlParams.n_estimators) ?? params.rf_n_estimators;
    params.rf_max_depth = asNumber(dlParams.max_depth) ?? params.rf_max_depth;
    const maxFeatures = asString(dlParams.max_features);
    if (maxFeatures) params.rf_max_features = maxFeatures;
  } else {
    params.dl_hidden_size = asNumber(dlParams.hidden_size) ?? params.dl_hidden_size;
    params.dl_num_layers = asNumber(dlParams.num_layers) ?? params.dl_num_layers;
    params.dl_dropout = asNumber(dlParams.dropout) ?? params.dl_dropout;
    params.dl_n_epochs = asNumber(dlParams.n_epochs) ?? params.dl_n_epochs;
    params.dl_batch_size = asNumber(dlParams.batch_size) ?? params.dl_batch_size;
    params.dl_lr = asNumber(dlParams.lr) ?? params.dl_lr;
    params.dl_step_len = asNumber(dlParams.dl_step_len) ?? params.dl_step_len;
    params.tcn_kernel_size = asNumber(dlParams.kernel_size) ?? params.tcn_kernel_size;
    params.tft_num_heads = asNumber(dlParams.num_heads) ?? params.tft_num_heads;
  }

  const target = normalizeTrainingTarget({
    mode: label.target_mode === 'classification' || label.target_mode === 'return'
      ? label.target_mode
      : (meta.target_mode === 'classification' ? 'classification' : 'return'),
    horizonDays:
      asNumber(label.target_horizon_days)
      ?? asNumber(meta.target_horizon_days)
      ?? 5,
    executionPrice: asString(label.execution_price) === 'open' ? 'open' : 'close',
    labelTransform:
      typeof label.label_transform === 'string'
        ? (label.label_transform as TrainingDraft['target']['labelTransform'])
        : undefined,
    labelTopPct: asNumber(label.label_top_pct),
    labelDropExtremePct: asNumber(label.label_drop_extreme_pct),
  });

  const context: TrainingContext = {
    ...DEFAULT_CONTEXT,
    initialCapital: asNumber(rawContext.initial_capital) ?? DEFAULT_CONTEXT.initialCapital,
    benchmark: asString(rawContext.benchmark) ?? DEFAULT_CONTEXT.benchmark,
    commissionRate: asNumber(rawContext.commission_rate) ?? DEFAULT_CONTEXT.commissionRate,
    slippage: asNumber(rawContext.slippage) ?? DEFAULT_CONTEXT.slippage,
    dealPrice: asString(rawContext.deal_price) === 'close' ? 'close' : 'open',
    market,
    industry_as_feature:
      typeof rawContext.industry_as_feature === 'boolean'
        ? rawContext.industry_as_feature
        : DEFAULT_CONTEXT.industry_as_feature,
  };

  const factorSource = asString(data.factor_source) ?? asString(meta.factor_source);
  const factorCatalogVersion = asString(data.factor_catalog_version) ?? null;
  const allVersions = asRecord(data.factor_catalog_versions);
  // 跨库：factor_field_sources 记录每个裸特征名的**实际来源库**（`lib:feature`）。
  // 附加库顺序按其在 field_sources 里首次出现的顺序还原——训练页按「锚库优先、
  // 附加库按 extraFactorSources 顺序」判定同名特征归属，顺序错了会把同名特征
  // 静默换到另一个库（后端按裸名去重，换源不报错）。
  const fieldSources = asRecord(data.factor_field_sources);
  const sourceOrder: string[] = [];
  Object.values(fieldSources).forEach((value) => {
    const text = asString(value);
    if (!text) return;
    const sep = text.indexOf(':');
    const lib = sep > 0 ? text.slice(0, sep) : '';
    if (lib && !sourceOrder.includes(lib)) sourceOrder.push(lib);
  });
  const versionLibs = Object.keys(allVersions);
  const extraFactorSources = Array.from(new Set([
    ...sourceOrder,
    ...versionLibs,
  ])).filter((lib) => lib && lib !== factorSource);
  if (extraFactorSources.length > 0) {
    warnings.push(
      `跨库模型：锚库 ${factorSource ?? '未知'} + ${extraFactorSources.length} 个附加库，`
      + '已按原字段来源带入；当前不可用的库/特征会在目录加载后排除',
    );
  }
  // 顶层 extra_factor_catalog_versions 约定只含附加库；锚库版本走 factor_catalog_version。
  const extraCatalogVersions: Record<string, string> = {};
  extraFactorSources.forEach((lib) => {
    const version = asString(allVersions[lib]);
    if (version) extraCatalogVersions[lib] = version;
  });

  const displayName = stripSplitSuffix(asString(payload.display_name) ?? '');

  const draft: Omit<TrainingDraft, 'lastSavedAt'> = {
    displayName,
    displayNameMode: 'manual',
    selectedFeatures: features,
    timePeriods: { train: trainRange, val: validRange, test: testRange },
    target,
    params,
    context,
    wfa: {
      enabled: false,
      strategy: 'rolling',
      nWindows: 4,
      trainYears: 3,
      valMonths: 12,
      stepMonths: 12,
    },
    poolRef: null,
    poolName: null,
    poolId: null,
    extraFactorSources,
  };

  return {
    config: buildTrainingConfigFile(draft, {
      market,
      factor_source: factorSource,
      factor_catalog_version: factorCatalogVersion,
      extra_factor_catalog_versions: extraCatalogVersions,
    }),
    warnings,
  };
}
