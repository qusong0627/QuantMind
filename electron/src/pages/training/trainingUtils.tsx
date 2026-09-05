import React from 'react';
import dayjs, { Dayjs } from 'dayjs';
import { parse as parseYaml, stringify as stringifyYaml } from 'yaml';
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

// 模型类型定义（hybrid_gru_tree：QLIB map 无实现，已剔除）
export type ModelType = 'lightgbm' | 'xgboost' | 'catboost' | 'linear' | 'random_forest' | 'gru' | 'lstm' | 'alstm' | 'transformer' | 'tabnet' | 'tcn' | 'nativetft' | 'mlp';
export type ModelCategory = 'tree' | 'linear' | 'deep_learning';

export interface ModelTypeOption {
  value: ModelType;
  label: string;
  category: ModelCategory;
  description: string;
  framework: string;
  tooltip: string;
}

export const MODEL_TYPE_OPTIONS: ModelTypeOption[] = [
  // 树模型
  { value: 'lightgbm', label: 'LightGBM', category: 'tree', description: '速度快、效果稳，基线必备', framework: 'lightgbm',
    tooltip: 'QuantMind 实证 IC 最稳定的模型。对动量因子（mom_ret_*）和换手率（liq_turnover_os）敏感，训练 3 分钟内完成。建议作为首个基线，再与 XGBoost 做 Stacking 集成可提升 10-15% ICIR。' },
  { value: 'xgboost', label: 'XGBoost', category: 'tree', description: '与LGB异构，集成提升明显', framework: 'xgboost',
    tooltip: '分裂策略与 LightGBM 不同（level-wise vs leaf-wise），集成时互补性强。对资金流因子（flow_vpin、flow_pressure_index）捕获能力略优于 LGB。单模型训练比 LGB 慢约 30%。' },
  { value: 'catboost', label: 'CatBoost', category: 'tree', description: '类别特征友好，自带有序提升', framework: 'catboost',
    tooltip: '开启"行业作为特征"（ind_code_l1）时优势最大——CatBoost 原生支持类别特征，无需 one-hot。对风格因子（style_bp、style_ep_ttm）的交互捕捉较好。训练速度三者最慢，但过拟合风险最低。' },
  // 线性基线模型
  { value: 'linear', label: 'Ridge 线性', category: 'linear', description: '简单线性回归基线，sanity check 用', framework: 'sklearn',
    tooltip: '重要诊断工具：如果 Ridge 的 IC > 0.03，说明特征集有线性可分信号，树模型应该表现更好；如果 Ridge IC ≈ 0 但树模型 IC 很高，说明信号在非线性交互中。IC 应显著低于树模型，否则树模型可能过拟合。' },
  { value: 'random_forest', label: '随机森林', category: 'tree', description: 'Bagging 基线，对比 Boosting 是否真优', framework: 'sklearn',
    tooltip: 'Bagging 思想的多树集成，通过自助采样+特征随机选择降低方差。作为 Boosting（LGB/XGB）的对照基线：若 RF IC 接近 LGB，说明信号主要是线性/低阶交互，无需深树；若 LGB 显著优于 RF，则非线性交互重要。训练比 LGB 慢但无需调 learning_rate。' },
  { value: 'mlp', label: 'MLP', category: 'deep_learning', description: '神经网络最简基线，验证 RNN 是否真优于全连接', framework: 'sklearn',
    tooltip: '多层感知机基线：验证 GRU/LSTM/Transformer 的时序建模是否真的带来增益。若 MLP 在扁平特征上 IC 已接近 GRU，说明时序结构不重要，可直接用树模型。默认结构 [64,32]，L2 正则，早停。' },
  // 深度学习模型
  { value: 'gru', label: 'GRU', category: 'deep_learning', description: '门控循环单元，时序建模性价比最高', framework: 'pytorch',
    tooltip: '默认 20 日滚动窗口（step_len=20），捕捉动量反转模式。对波动率因子（vol_std_*、vol_parkinson_*）时序衰减敏感。GPU 训练约 10-20 分钟，是最推荐的 DL 入门模型。数据量 < 50 万行时慎用，容易过拟合。' },
  { value: 'lstm', label: 'LSTM', category: 'deep_learning', description: '长短期记忆网络', framework: 'pytorch',
    tooltip: '比 GRU 多一个门控单元，理论记忆更长，但 QuantMind A 股数据实测 IC 提升有限（<5%），训练慢约 40%。适合训练窗口 > 5 年的大数据集。如果 GRU 已经效果好，LSTM 通常不会明显更好。' },
  { value: 'alstm', label: 'ALSTM', category: 'deep_learning', description: '带注意力的LSTM', framework: 'pytorch',
    tooltip: '在 LSTM 基础上加注意力机制，自动学习哪些时间步更重要。对事件驱动行情（如业绩公告前后）有更好的捕捉。QuantMind 实测中 ALSTM 偶尔优于 LSTM，但不稳定——建议先跑 GRU，如果 IC > 0.05 再尝试 ALSTM。' },
  { value: 'transformer', label: 'Transformer', category: 'deep_learning', description: '标准Transformer', framework: 'pytorch',
    tooltip: '自注意力机制可捕捉任意时间步依赖，不局限于近邻。对跨周期因子组合（如短期动量 + 长期风格）有独特优势。但参数量大，需至少 100 万行训练数据才能收敛。d_model 默认 64，可调至 128 但需更多数据防过拟合。' },
  { value: 'tabnet', label: 'TabNet', category: 'deep_learning', description: 'Google表格数据SOTA，自带特征选择', framework: 'pytorch',
    tooltip: '唯一不需要滚动窗口的 DL 模型——直接吃扁平特征，类似"可学习的树模型"。自带 mask 机制做特征选择，适合不确定哪些因子有效时探索。对资金流因子（flow_*）和微结构因子有独特偏好。训练需预训练阶段，总时间约为 GRU 的 2 倍。' },
  { value: 'tcn', label: 'TCN', category: 'deep_learning', description: '时间卷积网络，比RNN快', framework: 'pytorch',
    tooltip: '用因果卷积替代循环，训练速度比 GRU/LSTM 快约 50%，推理更快。对波动率突变（如 vol_jump_zadj）和成交量异动（liq_volume_ratio_*）的检测能力较强。适合需要频繁重训模型的场景。kernel_size 默认 5，增大到 7 可捕捉更长期依赖。' },
  { value: 'nativetft', label: 'NativeTFT', category: 'deep_learning', description: '轻量TFT变体，GRU+注意力+门控残差', framework: 'pytorch',
    tooltip: 'QuantMind 自研轻量 TFT：GRU 时序编码 + MultiheadAttention + 门控残差网络(GRN)。比 pytorch_forecasting TFT 轻量 10 倍，无需额外依赖。hidden_dim 默认 64，num_heads 默认 4。适合 GRU 效果好但想尝试注意力机制的场景。' },
];

export interface TrainingTarget {
  mode: TargetMode;
  horizonDays: number;
  /** 多周期训练：非空数组时一次产出多个周期模型（如 [1,3,5,10]），horizonDays 仅作主显示周期 */
  horizonDaysList?: number[];
}

export type EnsembleMethod = 'none' | 'stacking';

export interface TrainingParams {
  model_type: ModelType;
  model_types: ModelType[];
  ensemble_method: EnsembleMethod;
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
  // LightGBM specific (optional, falls back to shared learning_rate/max_depth)
  lgb_learning_rate?: number;
  lgb_max_depth?: number;
  min_child_samples?: number;
  path_smooth?: number;
  bagging_freq?: number;
  // XGBoost specific
  xgb_learning_rate?: number;
  xgb_max_depth?: number;
  xgb_subsample?: number;
  xgb_colsample_bytree?: number;
  xgb_reg_alpha?: number;
  xgb_reg_lambda?: number;
  xgb_min_child_weight?: number;
  // CatBoost specific
  cb_learning_rate?: number;
  cb_depth?: number;
  cb_l2_leaf_reg?: number;
  cb_random_strength?: number;
  cb_bagging_temperature?: number;
  cb_od_wait?: number;
  cb_iterations?: number;
  // Linear specific
  linear_alpha?: number;
  // DL specific
  dl_hidden_size?: number;
  dl_num_layers?: number;
  dl_dropout?: number;
  dl_n_epochs?: number;
  dl_batch_size?: number;
  dl_lr?: number;
  dl_step_len?: number;
  tcn_kernel_size?: number;
  tft_num_heads?: number;
  /** 截面预处理：按 (交易日, 特征) 中位数填充缺失 + 分位缩尾 + 截面 Z-score */
  preprocessingEnabled?: boolean;
  /** Stacking 集成参数 */
  n_folds?: number;
  meta_alpha?: number;
  /** Optuna 自动超参搜索 */
  optunaEnabled?: boolean;
  optunaTrials?: number;
  /** 点预测（默认）或 P10/P50/P90 收益率分位推理。 */
  prediction_mode?: 'point' | 'quantile';
}

export interface TrainingContext {
  initialCapital: number;
  benchmark: string;
  commissionRate: number;
  slippage: number;
  dealPrice: DealPrice;
  market?: 'CN' | 'US' | 'HK' | 'CRYPTO' | 'FUTURES';
  industry_as_feature?: boolean;
}

export interface WfaConfig {
  enabled: boolean;
  strategy: 'rolling' | 'expanding';
  nWindows: number;
  trainYears: number;
  valMonths: number;
  stepMonths: number;
}

export interface WfaWindowResult {
  window_idx: number;
  strategy: string;
  train_start: string;
  train_end: string;
  val_start: string;
  val_end: string;
  train_rows: number;
  val_rows: number;
  ic: number;
  rank_ic: number;
  rank_icir: number;
  rmse: number;
  auc: number;
}

export interface WfaDiagnosticResult {
  enabled: boolean;
  strategy: string;
  n_windows: number;
  ic_mean: number;
  ic_std: number;
  ic_min: number;
  ic_max: number;
  rank_ic_mean: number;
  rank_ic_std: number;
  positive_rate: number;
  stability: string;
  model_type: string;
  overall_icir: number;
  windows: WfaWindowResult[];
  error?: string;
}

export interface PsiDriftResult {
  enabled: boolean;
  train_start?: string;
  train_end?: string;
  recent_start?: string;
  recent_end?: string;
  drift?: { stable: number; medium: number; severe: number };
  top_drift_features?: Array<{ feature: string; psi: number; rank_disp?: number; level: string; benign_scale?: boolean; rank_reliable?: boolean }>;
  max_psi?: number;
  overall?: 'stable' | 'warning' | 'severe';
  reason?: string;
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
  wfa?: WfaConfig;
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
    market?: string;
    generated_at: string;
    // 因子筛选报告（train.py select_top_factors 产出）：漏斗 + 每特征 IC/ICIR/覆盖/原因
    factor_selection?: {
      method?: string;
      thresholds?: Record<string, number>;
      stage_counts?: Record<string, number>;
      train_rows?: number;
      features?: Array<{
        name: string;
        ic: number | null;
        icir: number | null;
        ic_positive_rate: number | null;
        n_days: number;
        coverage: number;
        status: string;
        reason: string;
      }>;
      selected?: string[];
    };
  };
  metrics?: {
    train: { rmse: number; auc: number; ic: number; rank_ic: number; rank_icir: number };
    val: { rmse: number; auc: number; ic: number; rank_ic: number; rank_icir: number };
    test: { rmse: number; auc: number; ic: number; rank_ic: number; rank_icir: number };
    score_direction?: 'normal' | 'reversed';
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
  wfa?: WfaDiagnosticResult;
  drift?: PsiDriftResult;
  multiHorizon?: {
    horizons: string[];
    child_run_ids: string[];
    child_model_ids: string[];
    fusion_model_id: string;
    child_results?: Array<{
      run_id: string;
      target_horizon_days: number;
      result: any;
    }>;
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
  wfa?: WfaConfig;
  lastSavedAt: string;
}

/** 可分享的模型训练配置文件。运行记录、训练节点和模型产物均不写入文件。 */
export interface TrainingConfigFile {
  schema_version: 1;
  kind: 'quantmind-model-training-config';
  exported_at: string;
  market: TrainingContext['market'];
  factor_source?: string;
  factor_catalog_version?: string | null;
  configuration: Omit<TrainingDraft, 'lastSavedAt'>;
}

export interface ImportedTrainingConfig {
  draft: TrainingDraft;
  market: TrainingContext['market'];
  factorSource?: string;
  factorCatalogVersion?: string | null;
}

export interface FeatureOption {
  key: string;
  label: string;
  /** 后端 catalog 标记的默认勾选状态。新 schema 才有，老前端兼容性为可选。*/
  defaultSelected?: boolean;
}

export interface FeatureCategory {
  id: string;
  name: string;
  icon: React.ReactNode;
  features: FeatureOption[];
}

// ─── CONSTANTS ────────────────────────────────────────────────────────────────

export const STORAGE_KEY = 'qm:model-training:draft';
export const TRAINING_CONFIG_KIND = 'quantmind-model-training-config';
export const TRAINING_CONFIG_SCHEMA_VERSION = 1 as const;
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

// PRESET：fallback 列表，当 catalog 没下发 default_selected 时使用
// 数据驱动：基于 baseline_56 模型的 SHAP top-35（覆盖 96% 累积重要性）
// 实证依据：data/training_jobs/train_baseline_56_v1/shap_summary.csv
export const PRESET_DEFAULT_FEATURES = [
  // 极强 (top 5)
  'liq_turnover_os', 'liq_amount', 'style_idio_vol_20',
  'ind_strength_20', 'mom_kdj_k',
  // 强 (6-10)
  'style_size_percentile', 'liq_volume_ratio_5', 'vol_upside_20',
  'ind_ret_20d', 'vol_parkinson_20',
  // 中强 (11-15)
  'liq_amount_ma_5', 'style_bp', 'style_beta_20',
  'flow_qsp', 'mom_ret_60d',
  // 中 (16-20)
  'style_ep_ttm', 'flow_vpin_ma_20', 'style_ln_mv_float',
  'vol_downside_20', 'ind_relative_volume_20',
  // 中弱 (21-25)
  'liq_volume', 'ind_strength_60', 'ind_momentum_rank_20',
  'mom_ma_gap_5', 'mom_ret_1d',
  // 防御深度 (26-35)
  'volume', 'mom_ret_20d', 'vol_realized_rv', 'liq_amihud_20',
  'style_ln_mv_total', 'ind_ret_1d', 'liq_volume_ma_5',
  'mom_breakout_20d', 'mom_rsi_14', 'vol_realized_rrv',
];

export const TRAINING_BASE_FEATURES = [
  'mom_ret_1d', 'mom_ret_5d', 'mom_ret_20d', 'liq_volume', 'liq_amount', 'liq_turnover_os',
];

// Market-specific default feature sets
export const MARKET_DEFAULT_FEATURES: Record<string, string[]> = {
  CN: PRESET_DEFAULT_FEATURES,
  HK: [
    // 基本面因子
    'pe_ttm', 'pb', 'roe', 'ep_ttm', 'bp',
    // 技术面 - Top 15 from LightGBM gain
    'volume_ma_5', 'flow_vpin_ma_20', 'flow_vpin_ma_5',
    'vol_atr_20', 'ma_gap_20', 'style_idio_vol_60',
    'mom_ma_gap_20', 'liq_amihud_20', 'style_beta_60',
    'mom_ma_gap_5', 'vol_parkinson_10', 'return_20d',
    'mom_ret_60d', 'liq_volume_ma_10', 'vol_downside_20',
  ],
  US: [
    // 基本面因子
    'pe_ttm', 'pb', 'roe', 'ep_ttm', 'bp',
    // 技术面 - 动量+波动+流动性
    'mom_ret_20d', 'mom_ma_gap_20', 'mom_rsi_14',
    'vol_std_20', 'vol_atr_20', 'style_idio_vol_60',
    'style_beta_60', 'liq_amihud_20', 'flow_vpin_ma_20',
    'volume_ma_5', 'ma_gap_20', 'vol_downside_20',
    'mom_ret_60d', 'liq_volume_ma_10', 'style_ln_mv_total',
  ],
  CRYPTO: [
    // 加密货币没有基本面，纯技术+资金流
    'mom_ret_1d', 'mom_ret_5d', 'mom_ret_20d',
    'mom_ma_gap_5', 'mom_ma_gap_20', 'mom_rsi_14',
    'vol_std_20', 'vol_atr_20', 'vol_parkinson_20',
    'flow_vpin', 'flow_vpin_ma_5', 'flow_vpin_ma_20',
    'liq_volume_ma_5', 'liq_amihud_20', 'ma_gap_20',
    'volume_ma_5', 'vol_downside_20', 'style_beta_20',
    'mom_breakout_20d', 'vol_jump_zadj',
  ],
  FUTURES: [
    // 期货无基本面，纯技术+波动率+流动性
    'mom_ret_1d', 'mom_ret_5d', 'mom_ret_20d',
    'mom_ma_gap_5', 'mom_ma_gap_20', 'mom_rsi_14',
    'vol_std_20', 'vol_atr_20', 'vol_parkinson_20',
    'flow_vpin', 'flow_vpin_ma_5', 'flow_vpin_ma_20',
    'liq_volume_ma_5', 'liq_amihud_20', 'ma_gap_20',
    'volume_ma_5', 'vol_downside_20', 'style_beta_20',
    'mom_breakout_20d', 'vol_jump_zadj',
  ],
};

export const getDefaultFeaturesForMarket = (market: string): string[] => {
  return MARKET_DEFAULT_FEATURES[market?.toUpperCase()] || PRESET_DEFAULT_FEATURES;
};

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
  ohlcv: <BarChart size={14} />,
  index_membership: <LayoutGrid size={14} />,
  concept_tags: <Filter size={14} />,
  fundamental: <Activity size={14} />,
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
  model_type: 'lightgbm',
  model_types: ['lightgbm'],
  prediction_mode: 'point',
  ensemble_method: 'none',
  learning_rate: 0.02,
  num_leaves: 31,
  max_depth: -1,
  min_data_in_leaf: 300,
  min_child_samples: 150,
  path_smooth: 1.0,
  bagging_freq: 5,
  lambda_l1: 0.5,
  lambda_l2: 1.0,
  feature_fraction: 0.7,
  bagging_fraction: 0.8,
  num_boost_round: 2000,
  early_stopping_rounds: 50,
  objective: 'regression',
  metric: 'l2',
  // XGBoost
  xgb_max_depth: 4,
  xgb_subsample: 0.7,
  xgb_colsample_bytree: 0.65,
  xgb_reg_alpha: 0.5,
  xgb_reg_lambda: 2.0,
  xgb_min_child_weight: 100,
  // CatBoost
  cb_depth: 6,
  cb_l2_leaf_reg: 3.0,
  cb_random_strength: 1.5,
  cb_bagging_temperature: 0.8,
  cb_od_wait: 100,
  // Linear
  linear_alpha: 3.0,
  // DL
  dl_hidden_size: 64,
  dl_num_layers: 2,
  dl_dropout: 0.2,
  dl_n_epochs: 200,
  dl_batch_size: 4000,
  dl_lr: 0.0001,
  dl_step_len: 20,
  tcn_kernel_size: 5,
  tft_num_heads: 4,
  // Stacking 集成
  n_folds: 3,
  meta_alpha: 1.0,
  // Optuna
  optunaEnabled: false,
  optunaTrials: 20,
};

/** 各 DL 模型的推荐默认参数，切换模型时自动填充 */
export const MODEL_DL_DEFAULTS: Record<string, Partial<TrainingParams>> = {
  gru: {
    dl_hidden_size: 64,
    dl_num_layers: 2,
    dl_dropout: 0.2,
    dl_lr: 0.001,
    dl_batch_size: 4000,
    dl_n_epochs: 200,
    dl_step_len: 20,
  },
  lstm: {
    dl_hidden_size: 64,
    dl_num_layers: 2,
    dl_dropout: 0.2,
    dl_lr: 0.001,
    dl_batch_size: 4000,
    dl_n_epochs: 200,
    dl_step_len: 20,
  },
  alstm: {
    dl_hidden_size: 64,
    dl_num_layers: 2,
    dl_dropout: 0.2,
    dl_lr: 0.001,
    dl_batch_size: 4000,
    dl_n_epochs: 200,
    dl_step_len: 20,
  },
  transformer: {
    dl_hidden_size: 64,
    dl_num_layers: 2,
    dl_dropout: 0.2,
    dl_lr: 0.0001,
    dl_batch_size: 4000,
    dl_n_epochs: 200,
    dl_step_len: 20,
  },
  tabnet: {
    dl_hidden_size: 64,
    dl_num_layers: 5,
    dl_dropout: 0.2,
    dl_lr: 0.005,
    dl_batch_size: 4000,
    dl_n_epochs: 200,
    dl_step_len: 20,
  },
  tcn: {
    dl_hidden_size: 128,
    dl_num_layers: 2,
    dl_dropout: 0.2,
    dl_lr: 0.0001,
    dl_batch_size: 4000,
    dl_n_epochs: 200,
    dl_step_len: 20,
  },
  nativetft: {
    dl_hidden_size: 64,
    dl_num_layers: 2,
    dl_dropout: 0.2,
    dl_lr: 0.0005,
    dl_batch_size: 4000,
    dl_n_epochs: 200,
    dl_step_len: 20,
  },
};

export const DEFAULT_CONTEXT: TrainingContext = {
  initialCapital: 1000000,
  benchmark: 'SH000300',
  commissionRate: 0.00025,
  slippage: 0.0005,
  dealPrice: 'open',
  market: 'CN',
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

const CONFIG_MARKETS: NonNullable<TrainingContext['market']>[] = ['CN', 'US', 'HK', 'CRYPTO', 'FUTURES'];

const isRecord = (value: unknown): value is Record<string, unknown> => (
  typeof value === 'object' && value !== null && !Array.isArray(value)
);

const readStringArray = (value: unknown, label: string): string[] => {
  if (!Array.isArray(value) || value.some((item) => typeof item !== 'string' || !item.trim())) {
    throw new Error(`${label} 必须是字符串数组`);
  }
  return Array.from(new Set(value.map((item) => item.trim())));
};

const readDateRange = (value: unknown, label: string): [string, string] => {
  if (!Array.isArray(value) || value.length !== 2 || value.some((item) => typeof item !== 'string')) {
    throw new Error(`${label} 必须包含起止日期`);
  }
  const [start, end] = value as [string, string];
  if (!dayjs(start).isValid() || !dayjs(end).isValid() || dayjs(start).isAfter(dayjs(end), 'day')) {
    throw new Error(`${label} 日期无效或起止顺序错误`);
  }
  return [start, end];
};

/** 将当前前端草稿转换为可在其他设备导入的 YAML 配置。 */
export const buildTrainingConfigFile = (
  draft: Omit<TrainingDraft, 'lastSavedAt'>,
  options: Pick<TrainingConfigFile, 'market' | 'factor_source' | 'factor_catalog_version'>,
): TrainingConfigFile => ({
  schema_version: TRAINING_CONFIG_SCHEMA_VERSION,
  kind: TRAINING_CONFIG_KIND,
  exported_at: new Date().toISOString(),
  market: options.market,
  ...(options.factor_source ? { factor_source: options.factor_source } : {}),
  ...(options.factor_catalog_version ? { factor_catalog_version: options.factor_catalog_version } : {}),
  configuration: draft,
});

export const serializeTrainingConfig = (config: TrainingConfigFile): string =>
  stringifyYaml(config, { lineWidth: 0 });

/**
 * 读取并校验 YAML / TXT 配置。未知的超参数会被忽略，缺失字段以当前默认值补齐，
 * 以保证旧配置在升级后仍可安全导入。
 */
export const parseTrainingConfig = (source: string): ImportedTrainingConfig => {
  let raw: unknown;
  try {
    raw = parseYaml(source);
  } catch (error) {
    throw new Error(`无法解析 YAML：${error instanceof Error ? error.message : '文件格式错误'}`);
  }
  if (!isRecord(raw) || raw.kind !== TRAINING_CONFIG_KIND || raw.schema_version !== TRAINING_CONFIG_SCHEMA_VERSION) {
    throw new Error('不是受支持的 QuantMind 模型训练配置文件');
  }
  if (!CONFIG_MARKETS.includes(raw.market as TrainingContext['market'])) {
    throw new Error('配置中的市场标识无效');
  }
  if (!isRecord(raw.configuration)) throw new Error('配置缺少 configuration 节点');
  const config = raw.configuration;
  if (!isRecord(config.timePeriods) || !isRecord(config.target) || !isRecord(config.params) || !isRecord(config.context)) {
    throw new Error('配置缺少时间切分、训练目标、超参数或训练上下文');
  }

  const targetMode = config.target.mode;
  const horizonDays = Number(config.target.horizonDays);
  if ((targetMode !== 'return' && targetMode !== 'classification') || !Number.isInteger(horizonDays) || horizonDays < 1) {
    throw new Error('训练目标无效');
  }
  const modelType = config.params.model_type;
  const knownModelTypes = MODEL_TYPE_OPTIONS.map((item) => item.value);
  if (typeof modelType !== 'string' || !knownModelTypes.includes(modelType as ModelType)) {
    throw new Error('配置中的模型类型不受支持');
  }
  const requestedModelTypes = Array.isArray(config.params.model_types)
    ? config.params.model_types.filter((item): item is ModelType => typeof item === 'string' && knownModelTypes.includes(item as ModelType))
    : [];
  const timePeriods = {
    train: readDateRange(config.timePeriods.train, '训练集'),
    val: readDateRange(config.timePeriods.val, '验证集'),
    test: readDateRange(config.timePeriods.test, '测试集'),
  };
  if (dayjs(timePeriods.train[1]).isAfter(dayjs(timePeriods.val[0]), 'day') || dayjs(timePeriods.val[1]).isAfter(dayjs(timePeriods.test[0]), 'day')) {
    throw new Error('训练、验证、测试时间段必须按先后顺序排列');
  }
  const rawWfa = isRecord(config.wfa) ? config.wfa : {};
  const wfa: WfaConfig = {
    enabled: typeof rawWfa.enabled === 'boolean' ? rawWfa.enabled : false,
    strategy: rawWfa.strategy === 'expanding' ? 'expanding' : 'rolling',
    nWindows: Number.isInteger(rawWfa.nWindows) && Number(rawWfa.nWindows) > 0 ? Number(rawWfa.nWindows) : 4,
    trainYears: Number.isFinite(Number(rawWfa.trainYears)) && Number(rawWfa.trainYears) > 0 ? Number(rawWfa.trainYears) : 3,
    valMonths: Number.isFinite(Number(rawWfa.valMonths)) && Number(rawWfa.valMonths) > 0 ? Number(rawWfa.valMonths) : 12,
    stepMonths: Number.isFinite(Number(rawWfa.stepMonths)) && Number(rawWfa.stepMonths) > 0 ? Number(rawWfa.stepMonths) : 12,
  };
  const supportedParamValues = Object.fromEntries(
    Object.entries(config.params).filter(([key]) => key in DEFAULT_PARAMS),
  );
  const params = {
    ...DEFAULT_PARAMS,
    ...supportedParamValues,
    model_type: modelType as ModelType,
    model_types: requestedModelTypes.length > 0 ? requestedModelTypes : [modelType as ModelType],
  } as TrainingParams;
  const target: TrainingTarget = {
    mode: targetMode,
    horizonDays,
    ...(Array.isArray(config.target.horizonDaysList)
      ? { horizonDaysList: config.target.horizonDaysList.filter((item): item is number => Number.isInteger(item) && item >= 1) }
      : {}),
  };
  const context = { ...DEFAULT_CONTEXT, ...config.context } as TrainingContext;
  const displayName = typeof config.displayName === 'string' ? config.displayName : '';
  const displayNameMode = config.displayNameMode === 'manual' ? 'manual' : 'auto';

  return {
    draft: {
      displayName,
      displayNameMode,
      selectedFeatures: readStringArray(config.selectedFeatures, 'selectedFeatures'),
      timePeriods,
      target,
      params,
      context,
      wfa,
      lastSavedAt: new Date().toISOString(),
    },
    market: raw.market as TrainingContext['market'],
    factorSource: typeof raw.factor_source === 'string' ? raw.factor_source : undefined,
    factorCatalogVersion: typeof raw.factor_catalog_version === 'string' ? raw.factor_catalog_version : null,
  };
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

export const buildAutoDisplayName = (referenceDate: Dayjs, target: TrainingTarget, featureCount: number, version = DEFAULT_MODEL_VERSION, market?: string) => {
  const dateToken = referenceDate.format('DD');
  const horizons = target.horizonDaysList?.filter((h) => h >= 1) ?? [];
  const returnToken = horizons.length >= 2 ? `T${horizons.join('_')}` : `T${target.horizonDays}`;
  const dimensionToken = `Alpha${Math.max(1, featureCount)}`;
  const marketSuffix = market ? `_${market.toUpperCase()}` : '';
  return `${dateToken}_${returnToken}_${dimensionToken}_${version}${marketSuffix}`;
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

/**
 * Early published QuantDB catalogs mistakenly persisted the long dictionary
 * explanation as a feature name.  The selector and its preview must remain
 * compact even while an old immutable catalog is still in use.
 */
const compactCatalogFeatureLabel = (label: string, fallback: string): string => {
  if (!label.includes('具体计算口径')) return label;
  const summary = label.split(/[。．]/, 1)[0]?.trim() || '';
  const colonIndex = Math.max(summary.lastIndexOf('：'), summary.lastIndexOf(':'));
  return (colonIndex >= 0 ? summary.slice(colonIndex + 1) : summary) || fallback;
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
          label: compactCatalogFeatureLabel(feature.feature_name || feature.key, feature.key),
          // catalog 透传 default_selected（缺失/null 时按 undefined 处理，
          // 由调用方决定 fallback 行为）
          defaultSelected:
            typeof (feature as { default_selected?: boolean }).default_selected === 'boolean'
              ? (feature as { default_selected?: boolean }).default_selected
              : undefined,
        })),
    }))
    .filter((category) => category.features.length > 0);
};

/**
 * 从 catalog（已转 dynamic categories）算默认勾选列表。
 *
 * 优先用 catalog 自带的 `default_selected` flag（truth source）；当后端老版本不返
 * 回该字段时，回落到硬编码的 PRESET 列表，并 console.warn 提示。
 */
export const resolveDefaultSelectedFeatures = (
  categories: FeatureCategory[],
  market: string,
): string[] => {
  const allFeatures = categories.flatMap((c) => c.features);
  const hasAnyFlag = allFeatures.some((f) => typeof f.defaultSelected === 'boolean');

  if (hasAnyFlag) {
    return allFeatures.filter((f) => f.defaultSelected === true).map((f) => f.key);
  }

  // QuantDB 目录必须完整下发默认选择；A 股直读模式禁止以旧前端预设
  // 猜测字段，防止展示的特征与已发布数据契约不一致。
  if (String(market).toUpperCase() === 'CN') return [];

  // 兜底：后端没下发 default_selected，使用 PRESET 并过滤掉 catalog 没有的 key
  const availableKeys = new Set(allFeatures.map((f) => f.key));
  const preset = getDefaultFeaturesForMarket(market);
  const valid = preset.filter((k) => availableKeys.has(k));
  const missing = preset.filter((k) => !availableKeys.has(k));
  if (missing.length > 0) {
    console.warn(
      `[trainingUtils] PRESET 默认特征中有 ${missing.length} 个在 catalog 里不存在，已过滤:`,
      missing,
    );
  }
  return valid.length > 0 ? valid : preset;
};

export const buildTrainingRequest = (
  selectedFeatures: string[],
  categories: FeatureCategory[],
  timePeriods: TimePeriodMap,
  target: TrainingTarget,
  params: TrainingParams,
  context: TrainingContext,
  displayName: string,
  market?: string,
  wfa?: WfaConfig,
): TrainingRequestPayload => {
  const finalFeatures = Array.from(new Set(selectedFeatures));
  const labelFormula = buildLabelFormula(target);
  const effectiveTradeDate = buildEffectiveTradeDate(target, timePeriods.test[0]);
  const trainingWindow = `${formatRange(timePeriods.train)} | ${formatRange(timePeriods.val)} | ${formatRange(timePeriods.test)}`;
  const resolvedContext = market ? { ...context, market: market as TrainingContext['market'] } : context;
  return {
    displayName: displayName.trim() || buildAutoDisplayName(dayjs(), target, finalFeatures.length, undefined, market),
    selectedFeatures: finalFeatures,
    featureCategories: summarizeFeatureCategories(finalFeatures, categories),
    target,
    timePeriods: {
      train: toISOStringRange(timePeriods.train),
      val: toISOStringRange(timePeriods.val),
      test: toISOStringRange(timePeriods.test),
    },
    params,
    context: resolvedContext,
    generatedAt: new Date().toISOString(),
    labelFormula,
    effectiveTradeDate,
    trainingWindow,
    wfa: wfa?.enabled ? wfa : undefined,
  };
};

export interface TrainingFactorFilterConfig {
  enabled: boolean;
  nTop: number;
  icThreshold: number;
  icirThreshold: number;
  correlationThreshold: number;
}

// 与后端编排器默认注入的 factor_selection 配置保持一致
// （backend/services/engine/training/local_docker_orchestrator.py）
export const DEFAULT_FACTOR_FILTER: TrainingFactorFilterConfig = {
  enabled: true,
  nTop: 80,
  icThreshold: 0.01,
  icirThreshold: 0.15,
  correlationThreshold: 0.9,
};

export const buildBackendTrainingPayload = (
  request: TrainingRequestPayload,
  timePeriods: TimePeriodMap,
  options?: { nodeId?: string; maxTimeMinutes?: number; pauseOthers?: boolean; factorFilter?: TrainingFactorFilterConfig },
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

  const modelType = request.params.model_type || 'lightgbm';
  const modelTypes = request.params.model_types?.length > 1 ? request.params.model_types : null;
  const ensembleMethod = request.params.ensemble_method ?? 'none';

  const payload: Record<string, unknown> = {
    job_name: `model_train_t${request.target.horizonDays}_${dayjs().format('YYYYMMDDHHmmss')}`,
    display_name: request.displayName,
    model_type: modelType,
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
    prediction_mode: request.params.prediction_mode ?? 'point',
    label_formula: request.labelFormula,
    effective_trade_date: request.effectiveTradeDate,
    training_window: request.trainingWindow,
    generated_at: request.generatedAt,
    context: {      initial_capital: request.context.initialCapital,
      benchmark: request.context.benchmark,
      commission_rate: request.context.commissionRate,
      slippage: request.context.slippage,
      deal_price: request.context.dealPrice,
      market: request.context.market || 'CN',
      industry_as_feature: request.context.industry_as_feature ?? false,
    },
    lgb_params: {
      learning_rate: request.params.lgb_learning_rate ?? request.params.learning_rate,
      num_leaves: request.params.num_leaves,
      max_depth: request.params.lgb_max_depth ?? request.params.max_depth,
      min_data_in_leaf: request.params.min_data_in_leaf,
      min_child_samples: request.params.min_child_samples,
      path_smooth: request.params.path_smooth,
      bagging_freq: request.params.bagging_freq,
      lambda_l1: request.params.lambda_l1,
      lambda_l2: request.params.lambda_l2,
      feature_fraction: request.params.feature_fraction,
      bagging_fraction: request.params.bagging_fraction,
      objective: request.params.objective,
      metric: request.params.metric,
    },
    xgb_params: {
      max_depth: request.params.xgb_max_depth ?? (request.params.max_depth && request.params.max_depth > 0 ? request.params.max_depth : 4),
      learning_rate: request.params.xgb_learning_rate ?? request.params.learning_rate,
      subsample: request.params.xgb_subsample ?? 0.7,
      colsample_bytree: request.params.xgb_colsample_bytree ?? 0.65,
      reg_alpha: request.params.xgb_reg_alpha ?? 0.5,
      reg_lambda: request.params.xgb_reg_lambda ?? 2.0,
      min_child_weight: request.params.xgb_min_child_weight ?? 100,
      objective: request.params.objective === 'binary' ? 'binary:logistic' : 'reg:squarederror',
    },
    catboost_params: {
      depth: request.params.cb_depth ?? request.params.max_depth ?? 6,
      learning_rate: request.params.cb_learning_rate ?? request.params.learning_rate,
      l2_leaf_reg: request.params.cb_l2_leaf_reg ?? 3.0,
      random_strength: request.params.cb_random_strength ?? 1.5,
      bagging_temperature: request.params.cb_bagging_temperature ?? 0.8,
      od_wait: request.params.cb_od_wait ?? 100,
      loss_function: request.params.metric === 'auc' ? 'Logloss' : 'RMSE',
    },
    dl_params: {
      hidden_size: request.params.dl_hidden_size ?? 64,
      num_layers: request.params.dl_num_layers ?? 2,
      dropout: request.params.dl_dropout ?? 0.2,
      n_epochs: request.params.dl_n_epochs ?? 200,
      batch_size: request.params.dl_batch_size ?? 4000,
      lr: request.params.dl_lr ?? 0.0001,
      step_len: request.params.dl_step_len ?? 20,
      kernel_size: request.params.tcn_kernel_size ?? 5,
      num_heads: request.params.tft_num_heads ?? 4,
      alpha: request.params.linear_alpha ?? 3.0,
    },
  };

  if (modelTypes) {
    payload.model_types = modelTypes;
    payload.ensemble = ensembleMethod;
    // Stacking 集成参数
    payload.n_folds = request.params.n_folds ?? 3;
    payload.meta_alpha = request.params.meta_alpha ?? 1.0;
  }

  // Optuna 自动超参搜索
  if (request.params.optunaEnabled) {
    payload.optuna = {
      enabled: true,
      n_trials: request.params.optunaTrials ?? 20,
    };
  }

  // WFA 稳定性诊断配置
  if (request.wfa?.enabled) {
    payload.wfa = {
      enabled: true,
      strategy: request.wfa.strategy,
      n_windows: request.wfa.nWindows,
      train_years: request.wfa.trainYears,
      val_months: request.wfa.valMonths,
      step_months: request.wfa.stepMonths,
    };
  }

  // 特征截面预处理：按交易日截面 中位数填充缺失 + 分位缩尾 + Z-score
  if (request.params.preprocessingEnabled) {
    payload.preprocessing = { enabled: true, winsor: true };
  }

  // 多周期训练：一次产出 T+1/T+3/T+5/T+10 等周期的模型（编排器按周期展开为多个任务）
  const horizons = request.target.horizonDaysList?.filter((h) => h >= 1) ?? [];
  if (horizons.length >= 2) {
    payload.horizons = horizons;
    // 多周期下 WFA 成本 4×4=16 次训练，禁用避免超时
    delete payload.wfa;
  }

  // 训练节点（local=本机 Docker，autodl-xxx=AutoDL 远程 GPU）
  if (options?.nodeId) {
    payload.node_id = options.nodeId;
  }

  // 训练时长预算（分钟）：编排器默认 120 分钟，GRU/LSTM 等 DL 模型在 CPU 上
  // 每 epoch ~10 分钟 × 200 epochs 远超默认值，必须由前端显式透传。
  if (options?.maxTimeMinutes) {
    payload.max_time_minutes = options.maxTimeMinutes;
  }

  // 训练时是否停掉其他 Docker 容器释放内存（前端开关透传）
  // true=停其他容器（腾内存给训练，默认）；false=保留其他容器运行
  if (typeof options?.pauseOthers === 'boolean') {
    payload.pause_others = options.pauseOthers;
  }

  // 因子筛选（IC/ICIR 阈值 + 相关性剪枝）：默认开启。
  // 开启 = 显式透传 factor_selection，筛选漏斗日志与每特征 IC/ICIR/淘汰原因
  //        写入训练日志与结果元数据（结果页展示"为什么选/为什么不选"）；
  // 关闭 = auto_feature_filter=false，编排器不再注入 factor_selection，全部特征直接训练。
  if (options?.factorFilter) {
    payload.auto_feature_filter = options.factorFilter.enabled;
    if (options.factorFilter.enabled) {
      payload.factor_selection = {
        method: 'ic_icir',
        n_top: options.factorFilter.nTop,
        ic_threshold: options.factorFilter.icThreshold,
        icir_threshold: options.factorFilter.icirThreshold,
        correlation_threshold: options.factorFilter.correlationThreshold,
      };
    }
  }

  return payload;
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
      score_direction: ((metadata as any)?.metrics?.score_direction ?? metrics?.score_direction) === 'reversed' ? 'reversed' : 'normal',
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
    wfa: (rawResult.wfa as WfaDiagnosticResult) || undefined,
    drift: (rawResult.drift as PsiDriftResult) || undefined,
    multiHorizon: (rawResult.multi_horizon as TrainingResult['multiHorizon']) || undefined,
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
